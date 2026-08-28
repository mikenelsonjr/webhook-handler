"""Issue #6 — observable without being a data-leak.

Webhook bodies routinely carry names, addresses, and contact details. The
original `print`ed the payload on every publish, which writes it into Cloud
Logging with no severity and an indefinite retention policy.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging

from tests.ingest.conftest import FakePublisher, body_bytes, json_headers

# A distinctive value planted in the payload so any leak is unambiguous.
CANARY = "PII-CANARY-8fe31a9c"


def _post(client, payload=None):
    raw = body_bytes(payload if payload is not None else {"customer_email": CANARY})
    return client.post("/webhook", content=raw, headers=json_headers())


def test_payload_is_never_written_to_logs(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    client = make_client(FakePublisher())

    _post(client)

    assert CANARY not in caplog.text, "the webhook payload leaked into the logs"


def test_payload_is_never_written_to_stdout(make_client, capsys):
    """`print()` bypasses logging entirely: no severity, no filtering."""
    client = make_client(FakePublisher())

    _post(client)
    captured = capsys.readouterr()

    assert CANARY not in captured.out
    assert CANARY not in captured.err


def test_service_does_not_use_print():
    from tests.ingest.conftest import ingest_sources

    offenders = [
        f"{path.name}:{n}"
        for path in ingest_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith("print(")
    ]

    assert not offenders, f"print() found at {offenders}; use the logger"


def test_publish_failure_is_logged_at_error(make_client, caplog):
    from ingest.publisher import PublishError

    caplog.set_level(logging.DEBUG)
    client = make_client(FakePublisher(fail_with=PublishError("topic vanished")))

    _post(client)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a failed publish must be logged at ERROR"
    assert "PublishError" in caplog.text, "the exception type should be identifiable"


def test_failure_log_does_not_contain_the_payload(make_client, caplog):
    from ingest.publisher import PublishError

    caplog.set_level(logging.DEBUG)
    client = make_client(FakePublisher(fail_with=PublishError("topic vanished")))

    _post(client)

    assert CANARY not in caplog.text


def _emitted_lines(stream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@contextlib.contextmanager
def capture_ingest_output():
    """Capture what the ingest logger actually EMITS, formatted and parsed.

    `caplog` captures records *before* formatting, which is precisely the blind
    spot that let issue #9 ship: the old version of the correlation test
    asserted `hasattr(record, "event_id")` and passed for months while the
    configured formatter dropped every `extra=` field, so the emitted line
    carried no event_id at all.
    """
    from common.log import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("ingest")
    logger.addHandler(handler)
    # setLevel, NOT `logger.level = ...`: isEnabledFor() memoizes its answer in
    # Logger._cache and only setLevel() invalidates it, so a direct assignment
    # can raise the level and still have records silently dropped.
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_the_emitted_log_line_carries_the_event_id(make_client):
    """Without one you cannot tie a delivery to a line — and `event_id` is the
    only identifier that spans ingest, the topic, and the processor."""
    with capture_ingest_output() as stream:
        client = make_client(FakePublisher())
        response = _post(client, {"event": "ping"})

    lines = _emitted_lines(stream)
    assert lines, "the service should log something at INFO for each request"

    event_id = response.json()["event_id"]
    assert any(line.get("event_id") == event_id for line in lines), (
        f"no emitted line carried event_id={event_id}: {lines}"
    )


def test_the_emitted_failure_line_carries_the_event_id(make_client):
    """The 503 path is the one you most need to trace back to a delivery."""
    from ingest.publisher import PublishError

    with capture_ingest_output() as stream:
        client = make_client(FakePublisher(fail_with=PublishError("topic vanished")))
        response = _post(client)

    assert response.status_code == 503
    errors = [line for line in _emitted_lines(stream) if line["severity"] == "ERROR"]
    assert errors, "a failed publish must be emitted at ERROR"
    assert all("event_id" in line for line in errors)


def test_the_emitted_retry_warning_carries_the_event_id(make_client):
    """`RetryingPublisher` logs each failed attempt. Without the event_id you
    cannot tell whether one delivery retried three times or three deliveries
    failed once each — which is the difference between a blip and an outage."""
    from ingest.publisher import PublishError
    from ingest.retry import RetryingPublisher

    inner = FakePublisher(fail_with=PublishError("transient"))
    publisher = RetryingPublisher(inner, attempts=3, backoff_seconds=0, sleep=lambda _: None)

    # Every attempt fails, so the response is a 503 with no event_id in it —
    # the logs are the only place the id exists, which is the point.
    body = body_bytes({"customer_email": CANARY})
    expected = hashlib.sha256(body).hexdigest()

    with capture_ingest_output() as stream:
        client = make_client(publisher)
        response = client.post("/webhook", content=body, headers=json_headers())

    assert response.status_code == 503
    warnings = [line for line in _emitted_lines(stream) if line["severity"] == "WARNING"]
    assert len(warnings) == 2, f"expected a warning per non-final attempt, got {warnings}"
    assert all(line.get("event_id") == expected for line in warnings)


def test_successful_request_is_logged(make_client, caplog):
    caplog.set_level(logging.INFO)
    client = make_client(FakePublisher())

    _post(client, {"event": "ping"})

    assert [r for r in caplog.records if r.name.startswith("ingest")]
