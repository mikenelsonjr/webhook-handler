"""Issue #14 — the processor's logs, and the entrypoint that configures them.

`event_id` is the only identifier that spans ingest -> topic -> processor, so
one value grepped across both services has to return the whole story. That only
works if every line emitted while handling a delivery carries it.

The payload rule matters MORE here than it did in ingest. Ingest never held a
decoded body — it hashed opaque bytes — while this service has the real thing
in hand, so a leak is one careless format string away.

Every assertion here is on **formatted output parsed as JSON**, never on
`LogRecord` attributes. Issue #9 shipped precisely because a test asserted the
call site rather than the emitted line.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys

import pytest

from tests.processor.conftest import (
    EVENT_ID,
    FakeHandler,
    body,
    capture_processor_output,
    emitted,
    push_envelope,
)

PUSH_PATH = "/_pubsub/push"
ENTRYPOINT = "processor_main"

COMPLETE_ENV = {
    "PUSH_AUTH_MODE": "iam",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def restore_logging():
    """Importing the entrypoint reconfigures the root logger process-wide."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def import_entrypoint(monkeypatch, env: dict[str, str] | None = None):
    monkeypatch.delenv("PUSH_AUTH_MODE", raising=False)
    for key, value in (COMPLETE_ENV if env is None else env).items():
        monkeypatch.setenv(key, value)
    sys.modules.pop(ENTRYPOINT, None)
    return importlib.import_module(ENTRYPOINT)


# -- the entrypoint ---------------------------------------------------------------


def test_the_entrypoint_builds_an_app(monkeypatch, restore_logging):
    module = import_entrypoint(monkeypatch)

    assert module.app is not None


def test_the_entrypoint_configures_logging_through_common_log():
    """Both services must format identically, or the two halves of one trace
    disagree about what a field is called."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / f"{ENTRYPOINT}.py").read_text(encoding="utf-8")

    assert "common.log" in source
    assert "configure_logging" in source


def test_the_entrypoint_installs_the_json_formatter(monkeypatch, restore_logging):
    from common.log import JsonFormatter

    import_entrypoint(monkeypatch)

    root = logging.getLogger()
    assert root.handlers
    assert all(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


def test_missing_configuration_crashes_at_import(monkeypatch, restore_logging):
    """The Cloud Run rule: fail the revision at startup so the platform keeps
    the previous one serving. Discovering it on the first real message means
    503-ing an event instead."""
    sys.modules.pop(ENTRYPOINT, None)
    monkeypatch.delenv("PUSH_AUTH_MODE", raising=False)

    with pytest.raises(ValueError):
        importlib.import_module(ENTRYPOINT)

    sys.modules.pop(ENTRYPOINT, None)


def test_the_entrypoint_is_the_only_place_that_reads_the_environment():
    """`from_env` is a pure function of a mapping; only the entrypoint touches
    the ambient environment. Otherwise importing a module reads whatever .env
    happens to be on a developer's machine, and tests pass locally while
    failing in CI for reasons unrelated to the code.

    Parsed rather than grepped: `config.py`'s docstring says the words
    "os.environ" while promising not to call it, and a line scan cannot tell
    the promise from the breach.
    """
    import ast

    from tests.conftest import package_sources

    offenders = []
    for path in package_sources("processor"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"the environment is read inside the package at {offenders}"


def test_the_entrypoint_does_not_use_the_ingest_app(monkeypatch, restore_logging):
    """Two deployables, one image: the processor entrypoint must not drag in
    the receiver, which would need the ingest configuration to import."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / f"{ENTRYPOINT}.py").read_text(encoding="utf-8")

    assert "from ingest" not in source
    assert "import ingest" not in source


# -- event_id on every line --------------------------------------------------------


def each_line_of_a_delivery(make_client, handler=None, envelope=None):
    client = make_client(handler or FakeHandler())
    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(envelope or push_envelope()))
    return emitted(stream)


def test_every_line_of_a_successful_delivery_carries_the_event_id(make_client):
    lines = each_line_of_a_delivery(make_client)

    assert lines
    assert all(line.get("event_id") == EVENT_ID for line in lines), lines


def test_every_line_of_a_failed_delivery_carries_the_event_id(make_client):
    from processor.handler import RetryableError

    lines = each_line_of_a_delivery(make_client, FakeHandler(raise_with=RetryableError("down")))

    assert lines
    assert all(line.get("event_id") == EVENT_ID for line in lines), lines


def test_the_delivery_attempt_is_logged_when_present(make_client):
    """A climbing count is the only visible sign a message is heading for the
    dead-letter topic."""
    lines = each_line_of_a_delivery(make_client, envelope=push_envelope(delivery_attempt=4))

    assert any(line.get("delivery_attempt") == 4 for line in lines)


def test_the_message_id_is_logged(make_client):
    lines = each_line_of_a_delivery(make_client)

    assert any(line.get("message_id") for line in lines)


# -- the level table ----------------------------------------------------------------


def levels_for(make_client, handler=None, content=None):
    client = make_client(handler or FakeHandler())
    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=content if content is not None else body(push_envelope()))
    return {line["severity"] for line in emitted(stream) if line["logger"].startswith("processor")}


def test_a_success_is_info(make_client):
    assert "INFO" in levels_for(make_client)
    assert "ERROR" not in levels_for(make_client)


def test_a_dropped_malformed_message_is_error(make_client):
    """Acked, but never silently: this is the only record it ever existed."""
    assert "ERROR" in levels_for(make_client, content=b"not json")


def test_a_permanent_failure_is_error(make_client):
    from processor.handler import PermanentError

    assert "ERROR" in levels_for(make_client, FakeHandler(raise_with=PermanentError("nope")))


def test_a_retryable_failure_is_warning_not_error(make_client):
    """It is expected and self-correcting — Pub/Sub will send it again. At
    ERROR it becomes noise, and then nobody reads the real ones."""
    from processor.handler import RetryableError

    levels = levels_for(make_client, FakeHandler(raise_with=RetryableError("down")))

    assert "WARNING" in levels
    assert "ERROR" not in levels


def test_an_unexpected_failure_is_error(make_client):
    assert "ERROR" in levels_for(make_client, FakeHandler(raise_with=ValueError("bug")))


def test_an_unexpected_failure_logs_the_traceback_in_the_record(make_client):
    """As a field, not as text appended after the JSON object."""
    client = make_client(FakeHandler(raise_with=ValueError("a genuine bug")))

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(push_envelope()))

    assert any("ValueError" in line.get("exception", "") for line in emitted(stream))


# -- the payload must not leak --------------------------------------------------------

CANARY = "PII-CANARY-8fe31a9c"
PAYLOAD_WITH_CANARY = b'{"customer_email":"PII-CANARY-8fe31a9c","action":"update"}'


@pytest.mark.parametrize(
    "raise_with",
    [None, "PermanentError", "RetryableError", ValueError("bug")],
)
def test_the_payload_leaks_on_no_path(make_client, raise_with, capsys):
    """Every outcome, at DEBUG, checked against both the logs and the streams.
    `print()` bypasses logging entirely: no severity, no filtering."""
    import processor.handler as handler_module

    if isinstance(raise_with, str):
        raise_with = getattr(handler_module, raise_with)("boom")

    client = make_client(FakeHandler(raise_with=raise_with))

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(push_envelope(PAYLOAD_WITH_CANARY)))

    captured = capsys.readouterr()
    assert CANARY not in stream.getvalue()
    assert CANARY not in captured.out
    assert CANARY not in captured.err


def test_the_default_handler_does_not_log_the_payload_at_debug():
    """`LoggingHandler` is the shipped example. It must not be the thing that
    demonstrates how to leak a customer record."""
    from processor.envelope import parse_push_envelope
    from processor.handler import LoggingHandler

    event = parse_push_envelope(body(push_envelope(PAYLOAD_WITH_CANARY)))

    with capture_processor_output() as stream:
        LoggingHandler().handle(event)

    assert CANARY not in stream.getvalue()
    assert any(line.get("payload_bytes") for line in emitted(stream)), (
        "the size is useful and safe; log that instead of the body"
    )


def test_the_event_repr_is_not_a_leak_route(make_client):
    """The most likely accident is `logger.info('handling %s', event)`."""
    from processor.envelope import parse_push_envelope

    event = parse_push_envelope(body(push_envelope(PAYLOAD_WITH_CANARY)))

    assert CANARY not in f"{event}"
    assert CANARY not in f"{event!r}"


# -- the output is machine-readable ----------------------------------------------------


def test_every_emitted_line_is_valid_json(make_client):
    """A line Cloud Logging cannot parse is filed as unstructured text, which
    loses the severity and every field you would query on."""
    client = make_client(FakeHandler(raise_with=ValueError('boom "with quotes" and\nnewline')))

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(push_envelope()))

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines
    for line in lines:
        json.loads(line)  # raises if the formatter emitted something malformed
