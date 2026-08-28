"""Issue #6 — observable without being a data-leak.

Webhook bodies routinely carry names, addresses, and contact details. The
original `print`ed the payload on every publish, which writes it into Cloud
Logging with no severity and an indefinite retention policy.
"""

from __future__ import annotations

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


def test_log_records_carry_a_correlation_id(make_client, caplog):
    """Without one you cannot tie a failure back to a specific delivery."""
    caplog.set_level(logging.INFO)
    client = make_client(FakePublisher())

    _post(client)

    ingest_records = [r for r in caplog.records if r.name.startswith("ingest")]
    assert ingest_records, "the service should log something at INFO for each request"
    assert any(
        hasattr(r, "event_id") or "event_id" in r.getMessage() for r in ingest_records
    ), "log records should carry the event_id"


def test_successful_request_is_logged(make_client, caplog):
    caplog.set_level(logging.INFO)
    client = make_client(FakePublisher())

    _post(client, {"event": "ping"})

    assert [r for r in caplog.records if r.name.startswith("ingest")]
