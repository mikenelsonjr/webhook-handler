"""Issue #15 — the envelope both sides depend on, checked end to end.

Every test here drives one payload the whole way: provider -> ingest ->
(captured at the publish boundary) -> a Pub/Sub push envelope -> the
processor's parser. Neither component's suite can catch drift in this shape,
because each would be asserting its own assumption.

The property that carries the most weight is **byte-fidelity**. Ingest
publishes the raw provider body untouched, which is what lets a consumer
re-verify a provider signature or diff against what the provider claims it
sent. It costs nothing to preserve and cannot be recovered once lost.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.contract.conftest import (
    CONTRACT_ATTRIBUTES,
    MAX_ATTRIBUTE_KEY_BYTES,
    MAX_ATTRIBUTE_VALUE_BYTES,
    MAX_ATTRIBUTES,
    MAX_MESSAGE_BYTES,
    SOURCE,
    RecordingPublisher,
    as_push_envelope,
    deliver,
    round_trip,
)

SIMPLE = b'{"action":"update","data":{"_id":"abc"}}'


# -- the payload survives the trip ------------------------------------------------


def test_the_processor_sees_exactly_the_bytes_the_provider_sent():
    response, event, _ = round_trip(SIMPLE)

    assert response.status_code == 202
    assert event.data == SIMPLE


def test_whitespace_and_key_order_are_preserved():
    """Re-serializing through json.dumps would reorder keys and drop
    whitespace, so a consumer could no longer compare against what the
    provider actually sent — or verify a signature over it."""
    raw = b'{"z": 1,\n   "a": 2,  "nested": {"b": 3}}'

    _, event, _ = round_trip(raw)

    assert event.data == raw


def test_non_ascii_utf8_survives_the_base64_round_trip():
    raw = json.dumps({"name": "Renée Åberg", "note": "café — ✓"}, ensure_ascii=False).encode()

    _, event, _ = round_trip(raw)

    assert event.data == raw
    assert json.loads(event.data)["name"] == "Renée Åberg"


def test_a_payload_that_is_not_ascii_json_still_round_trips():
    """Ingest parses only to reject malformed input; the bytes it publishes are
    never the bytes it parsed."""
    raw = '{"emoji":"🎣","cyrillic":"Привет"}'.encode()

    _, event, _ = round_trip(raw)

    assert event.data == raw


# -- the attributes are the contract ------------------------------------------------


def test_exactly_the_three_contract_attributes_are_published():
    """A fourth attribute fails this test on purpose. Attributes are chosen,
    not forwarded: an earlier draft published dict(request.headers), which put
    the caller's signing key on the topic for every subscriber to read."""
    _, _, attributes = round_trip(SIMPLE)

    assert set(attributes) == set(CONTRACT_ATTRIBUTES)


def test_the_event_id_the_processor_reads_is_the_one_ingest_returned():
    """The sender is told an id in the 202 body. If the processor reads a
    different one, no operator can follow a delivery across the two."""
    response, event, _ = round_trip(SIMPLE)

    assert event.event_id == response.json()["event_id"]


def test_the_event_id_is_the_sha256_of_the_raw_body():
    """Pinned independently of either implementation, so a change to how it is
    derived has to be a deliberate change to the contract."""
    _, event, _ = round_trip(SIMPLE)

    assert event.event_id == hashlib.sha256(SIMPLE).hexdigest()


def test_the_source_survives():
    _, event, _ = round_trip(SIMPLE)

    assert event.source == SOURCE


def test_received_at_is_an_iso_8601_instant_with_a_timezone():
    """A naive timestamp is ambiguous the moment it leaves the process that
    made it, and this one is read by a different service."""
    from datetime import datetime

    _, event, _ = round_trip(SIMPLE)
    parsed = datetime.fromisoformat(event.received_at)

    assert parsed.tzinfo is not None


def test_every_attribute_is_a_string():
    """Pub/Sub attribute values must be strings; the client raises otherwise,
    which would turn a publish into a 503 for every message."""
    _, _, attributes = round_trip(SIMPLE)

    assert all(isinstance(k, str) and isinstance(v, str) for k, v in attributes.items())


@pytest.mark.parametrize("attribute", sorted(CONTRACT_ATTRIBUTES))
def test_the_processor_refuses_a_message_missing_any_contract_attribute(attribute):
    """Neither side may quietly tolerate a missing attribute: a default would
    let a half-broken publisher look healthy indefinitely."""
    from processor.envelope import EnvelopeError, parse_push_envelope

    publisher = RecordingPublisher()
    deliver(publisher, SIMPLE)
    data, attributes = publisher.last
    del attributes[attribute]

    with pytest.raises(EnvelopeError):
        parse_push_envelope(as_push_envelope(data, attributes))


def test_an_unknown_extra_attribute_does_not_break_the_processor():
    """The other direction: the two services deploy separately, so an older
    processor has to survive a newer ingest that adds a field."""
    from processor.envelope import parse_push_envelope

    publisher = RecordingPublisher()
    deliver(publisher, SIMPLE)
    data, attributes = publisher.last

    event = parse_push_envelope(as_push_envelope(data, {**attributes, "trace_id": "t-1"}))

    assert event.event_id == hashlib.sha256(SIMPLE).hexdigest()


# -- the two sides must agree with Pub/Sub itself -------------------------------------


def test_the_attributes_fit_inside_pubsubs_limits():
    """Exceeding one of these is rejected at publish time, so the contract is
    not just between the two services — Pub/Sub is a party to it."""
    _, _, attributes = round_trip(SIMPLE)

    assert len(attributes) <= MAX_ATTRIBUTES
    for key, value in attributes.items():
        assert len(key.encode()) <= MAX_ATTRIBUTE_KEY_BYTES
        assert len(value.encode()) <= MAX_ATTRIBUTE_VALUE_BYTES


def test_the_accepted_body_size_cannot_exceed_what_pubsub_will_store():
    """The cross-component invariant nobody owns. Raise MAX_BODY_BYTES past
    Pub/Sub's message limit and ingest starts accepting bodies it can never
    publish — a 413 replaced by a 503, on the largest and most interesting
    payloads, discovered in production."""
    from ingest.config import Settings

    default = Settings(
        gcp_project="p", pubsub_topic="t", signing_secret="s"  # pragma: allow-secret
    ).max_body_bytes

    assert default <= MAX_MESSAGE_BYTES


def test_base64_expansion_still_fits_the_push_body():
    """Push delivers the payload base64-encoded, which is 4/3 the size. A body
    at the accepted limit must still be deliverable after that expansion."""
    from ingest.config import Settings

    default = Settings(
        gcp_project="p", pubsub_topic="t", signing_secret="s"  # pragma: allow-secret
    ).max_body_bytes

    assert default * 4 / 3 <= MAX_MESSAGE_BYTES


# -- the suite owes nothing to GCP -----------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "tests/contract/conftest.py",
        "tests/contract/test_envelope_contract.py",
        "ingest/app.py",
        "ingest/config.py",
        "processor/envelope.py",
    ],
)
def test_nothing_on_the_contract_path_imports_gcp_libraries(module):
    """Every module this suite actually drives, checked for a GCP import.

    Not `sys.modules`: pytest shares one process, and tests/ingest legitimately
    imports google.cloud to exercise the concrete publisher — so a global check
    passes alone and fails in the full run while proving nothing either way.
    What matters is that no module on THIS path needs the library, which is
    what lets the suite run on every push with no credentials.
    """
    import ast

    from tests.conftest import REPO_ROOT

    tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("google"):
            imported.append(f"{module}:{node.lineno}")
        elif isinstance(node, ast.Import):
            imported += [
                f"{module}:{node.lineno}" for a in node.names if a.name.startswith("google")
            ]

    assert not imported, f"GCP libraries imported on the contract path: {imported}"


def test_the_contract_suite_does_not_borrow_either_components_fixtures():
    """A contract test that imports one side's fixtures has adopted that
    side's assumptions and can no longer catch it being wrong.

    Imports are read from the parse tree, not grepped: this very test mentions
    both module names, and a text scan would match itself.
    """
    import ast

    from tests.conftest import REPO_ROOT

    borrowed = []
    for name in ("conftest.py", "test_envelope_contract.py"):
        path = REPO_ROOT / "tests" / "contract" / name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = " ".join(alias.name for alias in node.names)
            else:
                continue
            if module.startswith(("tests.ingest", "tests.processor")):
                borrowed.append(f"{name}:{node.lineno}")

    assert not borrowed, f"contract tests must not import a component suite: {borrowed}"
