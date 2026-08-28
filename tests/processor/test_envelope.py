"""Issue #10 — parse a Pub/Sub push envelope into an Event.

One case per acceptance criterion, plus the edge cases the AC implies.

The property that matters most is **byte-fidelity**: ingest publishes the raw
provider body untouched, so `data` must come back byte-identical. That is what
lets a consumer re-verify a provider signature or diff against what the
provider claims it sent. Round-tripping through `json.loads`/`json.dumps` would
reorder keys and drop whitespace and quietly destroy it.

Imports are inside the tests so a missing module fails the individual cases
rather than erroring collection for the whole file.
"""

from __future__ import annotations

import base64
import json

import pytest

from tests.processor.conftest import (
    EVENT_ID,
    MESSAGE_ID,
    PAYLOAD,
    PUBLISH_TIME,
    RECEIVED_AT,
    SOURCE,
    attributes,
    body,
    push_envelope,
)


def parse(envelope: dict):
    from processor.envelope import parse_push_envelope

    return parse_push_envelope(body(envelope))


# -- the happy path ------------------------------------------------------------


def test_it_returns_an_event_with_every_field_populated():
    event = parse(push_envelope(delivery_attempt=3))

    assert event.event_id == EVENT_ID
    assert event.source == SOURCE
    assert event.received_at == RECEIVED_AT
    assert event.data == PAYLOAD
    assert event.message_id == MESSAGE_ID
    assert event.publish_time == PUBLISH_TIME
    assert event.delivery_attempt == 3


def test_the_event_is_frozen():
    """Nothing downstream may rewrite what arrived — a handler that mutated
    the event would make the dedup key and the logs disagree."""
    import dataclasses

    event = parse(push_envelope())

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.event_id = "something-else"


# -- byte-fidelity -------------------------------------------------------------


def test_data_is_byte_identical_to_what_was_published():
    raw = b'{"z": 1,   "a": 2}'  # significant whitespace and key order

    event = parse(push_envelope(raw))

    assert event.data == raw


def test_non_ascii_utf8_survives_the_round_trip():
    raw = json.dumps({"name": "Renée Åberg", "note": "café — ✓"}).encode()

    event = parse(push_envelope(raw))

    assert event.data == raw
    assert json.loads(event.data)["name"] == "Renée Åberg"


def test_data_is_bytes_not_str():
    """A str would mean the parser decoded it, and decoding is the consumer's
    choice — the payload is opaque here, exactly as it was in ingest."""
    event = parse(push_envelope())

    assert isinstance(event.data, bytes)


def test_a_payload_that_is_not_valid_utf8_still_parses():
    """`data` is opaque bytes. The parser has no business decoding it, so a
    payload that is not text must not be the thing that breaks the endpoint."""
    raw = b"\xff\xfe\x00binary"

    event = parse(push_envelope(raw))

    assert event.data == raw


# -- optional fields -----------------------------------------------------------


def test_a_message_with_no_data_key_yields_empty_bytes():
    """Pub/Sub omits `data` entirely for an attribute-only message. That is a
    valid message, not a malformed one."""
    envelope = push_envelope(data=None)
    assert "data" not in envelope["message"], "the fixture should have omitted it"

    event = parse(envelope)

    assert event.data == b""


def test_delivery_attempt_is_none_when_absent():
    """Present only when the subscription has a dead-letter policy, so its
    absence is a subscription-configuration fact rather than an error."""
    envelope = push_envelope()
    assert "deliveryAttempt" not in envelope

    event = parse(envelope)

    assert event.delivery_attempt is None


def test_delivery_attempt_is_read_when_present():
    event = parse(push_envelope(delivery_attempt=1))

    assert event.delivery_attempt == 1


def test_extra_attributes_are_ignored_not_rejected():
    """A future ingest may add an attribute. An older processor must keep
    working — this is a rolling deploy, the two are never updated together."""
    event = parse(push_envelope(attrs=attributes(trace_id="t-1")))

    assert event.event_id == EVENT_ID


# -- what must be rejected -----------------------------------------------------


def test_a_non_json_body_raises_envelope_error():
    from processor.envelope import EnvelopeError, parse_push_envelope

    with pytest.raises(EnvelopeError):
        parse_push_envelope(b"this is not json")


def test_an_empty_body_raises_envelope_error():
    from processor.envelope import EnvelopeError, parse_push_envelope

    with pytest.raises(EnvelopeError):
        parse_push_envelope(b"")


def test_a_json_body_that_is_not_an_object_raises_envelope_error():
    from processor.envelope import EnvelopeError, parse_push_envelope

    with pytest.raises(EnvelopeError):
        parse_push_envelope(b"[1, 2, 3]")


def test_a_missing_message_key_raises_envelope_error():
    from processor.envelope import EnvelopeError

    with pytest.raises(EnvelopeError):
        parse({"subscription": "projects/p/subscriptions/s"})


def test_a_message_that_is_not_an_object_raises_envelope_error():
    from processor.envelope import EnvelopeError

    with pytest.raises(EnvelopeError):
        parse({"message": "not-an-object"})


def test_invalid_base64_raises_envelope_error():
    from processor.envelope import EnvelopeError

    with pytest.raises(EnvelopeError):
        parse(push_envelope(encoded_data="!!!not base64!!!"))


def test_base64_with_bad_padding_raises_envelope_error():
    """`b64decode` without validate=True silently discards characters outside
    the alphabet, so truncated data would decode to something plausible and
    wrong rather than failing."""
    from processor.envelope import EnvelopeError

    truncated = base64.b64encode(PAYLOAD).decode()[:-3]

    with pytest.raises(EnvelopeError):
        parse(push_envelope(encoded_data=truncated))


def test_data_that_is_not_a_string_raises_envelope_error():
    from processor.envelope import EnvelopeError

    with pytest.raises(EnvelopeError):
        parse(push_envelope(encoded_data=12345))


@pytest.mark.parametrize("missing", ["event_id", "source", "received_at"])
def test_a_missing_required_attribute_raises_envelope_error(missing):
    """These three are the contract with ingest. Defaulting a missing one to
    "" would produce an Event that dedups against every other broken message."""
    from processor.envelope import EnvelopeError

    attrs = attributes()
    del attrs[missing]

    with pytest.raises(EnvelopeError):
        parse(push_envelope(attrs=attrs))


def test_a_missing_attributes_block_raises_envelope_error():
    from processor.envelope import EnvelopeError

    envelope = push_envelope()
    del envelope["message"]["attributes"]

    with pytest.raises(EnvelopeError):
        parse(envelope)


@pytest.mark.parametrize("field", ["messageId", "publishTime"])
def test_a_missing_delivery_identifier_raises_envelope_error(field):
    """Pub/Sub always sends both. Their absence means this is not a push
    envelope at all, and an Event with a blank message_id logs as a blank."""
    from processor.envelope import EnvelopeError

    envelope = push_envelope()
    del envelope["message"][field]

    with pytest.raises(EnvelopeError):
        parse(envelope)


def test_a_non_integer_delivery_attempt_raises_envelope_error():
    from processor.envelope import EnvelopeError

    with pytest.raises(EnvelopeError):
        parse(push_envelope(delivery_attempt="lots"))


def test_envelope_error_is_not_a_bare_exception():
    """The endpoint catches this specifically to decide 204-vs-503. Catching
    Exception there would swallow the bugs that must nack."""
    from processor.envelope import EnvelopeError

    assert issubclass(EnvelopeError, Exception)
    assert EnvelopeError is not Exception


# -- the payload must not leak --------------------------------------------------


def test_the_repr_does_not_contain_the_payload():
    """One `logger.debug("handling %s", event)` is all it takes. Ingest never
    holds a decoded payload; the processor does, so the default has to be safe."""
    canary = b'{"customer_email":"PII-CANARY-8fe31a9c"}'

    event = parse(push_envelope(canary))

    assert "PII-CANARY-8fe31a9c" not in repr(event)
    assert event.event_id in repr(event), "the id you DO want should still be there"


def test_the_repr_does_not_contain_the_payload_via_str():
    canary = b'{"customer_email":"PII-CANARY-8fe31a9c"}'

    event = parse(push_envelope(canary))

    assert "PII-CANARY-8fe31a9c" not in str(event)


# -- no GCP dependency ----------------------------------------------------------


def test_the_envelope_module_does_not_import_google():
    """The whole point of push: a push body is plain JSON over HTTP, so the
    processor needs neither the grpc toolchain nor credentials to be tested."""
    from tests.conftest import package_sources

    offenders = [
        f"{path.name}:{n}"
        for path in package_sources("processor")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith(("import google", "from google"))
    ]

    assert not offenders, f"google imported at {offenders}"
