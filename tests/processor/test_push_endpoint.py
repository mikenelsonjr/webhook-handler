"""Issue #11 — the response status IS the acknowledgement.

Ingest's rule is *a 2xx is a promise*: never acknowledge what is not durable.
This is the same sentence read from the other end — **a 2xx means "never send
me this again"** — and every case here is one row of that table.

The row worth staring at is `204 on a malformed envelope`. Any non-2xx in push
delivery is a nack, so the reflexive `400 Bad Request` on unparseable input
asks Pub/Sub to redeliver it, with backoff, until it ages out of retention
seven days later. Bad bytes do not become good bytes on the third attempt.
"""

from __future__ import annotations

import pytest

from tests.processor.conftest import (
    EVENT_ID,
    PAYLOAD,
    FakeHandler,
    body,
    capture_processor_output,
    emitted,
    push,
    push_envelope,
)

PUSH_PATH = "/_pubsub/push"


# -- health --------------------------------------------------------------------


def test_healthz_is_open_and_returns_200(client):
    """Cloud Run's prober carries no identity, so this cannot be authenticated."""
    response = client.get("/healthz")

    assert response.status_code == 200


# -- the ack table -------------------------------------------------------------


def test_a_handled_message_is_acked_with_204(client, handler):
    response = push(client)

    assert response.status_code == 204
    assert handler.call_count == 1


def test_the_204_carries_no_body(client):
    """A 204 with a body is malformed HTTP, and Pub/Sub reads only the status."""
    response = push(client)

    assert response.content == b""


def test_a_permanent_error_is_acked_with_204(make_client):
    """It will never succeed, so redelivering it just burns the retry budget
    on the way to the dead-letter topic."""
    from processor.handler import PermanentError

    client = make_client(FakeHandler(raise_with=PermanentError("unknown event type")))

    assert push(client).status_code == 204


def test_an_unparseable_envelope_is_acked_with_204(make_client):
    """THE load-bearing case. A 400 here is a nack, and this message will never
    parse, so Pub/Sub would redeliver it until the retention window expires."""
    client = make_client(FakeHandler())

    response = client.post(
        PUSH_PATH, content=b"this is not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 204


def test_an_unparseable_envelope_never_reaches_the_handler(make_client):
    handler = FakeHandler()
    client = make_client(handler)

    client.post(PUSH_PATH, content=b"{}", headers={"Content-Type": "application/json"})

    assert handler.call_count == 0


def test_a_retryable_error_nacks_with_503(make_client):
    from processor.handler import RetryableError

    client = make_client(FakeHandler(raise_with=RetryableError("downstream is down")))

    assert push(client).status_code == 503


def test_an_unexpected_exception_nacks_with_503(make_client):
    """Deliberately opposite to ingest/retry.py, which lets an unknown failure
    propagate rather than retrying it. Pub/Sub bounds the retries and then
    dead-letters, so the failure gets investigated instead of lost."""
    client = make_client(FakeHandler(raise_with=ValueError("a genuine bug")))

    assert push(client).status_code == 503


def test_an_unexpected_exception_does_not_surface_as_500(make_client):
    """A 500 is still a nack, so the behaviour would look right — but it means
    the exception escaped the endpoint, and a traceback would go out with it."""
    client = make_client(FakeHandler(raise_with=ValueError("a genuine bug")))

    response = push(client)

    assert response.status_code != 500
    assert b"Traceback" not in response.content
    assert b"a genuine bug" not in response.content


@pytest.mark.parametrize(
    ("raise_with", "expected"),
    [
        (None, 204),
        ("PermanentError", 204),
        ("RetryableError", 503),
        (ValueError("bug"), 503),
        (RuntimeError("bug"), 503),
        (KeyError("bug"), 503),
    ],
)
def test_the_ack_table_holds(make_client, raise_with, expected):
    """The table from CONTEXT.md, asserted as one thing rather than six."""
    import processor.handler as handler_module

    if isinstance(raise_with, str):
        raise_with = getattr(handler_module, raise_with)("boom")

    client = make_client(FakeHandler(raise_with=raise_with))

    assert push(client).status_code == expected


# -- what the handler receives --------------------------------------------------


def test_the_handler_receives_the_parsed_event(client, handler):
    push(client)
    event = handler.last_event

    assert event.event_id == EVENT_ID
    assert event.data == PAYLOAD


def test_the_handler_receives_the_delivery_attempt(make_client):
    handler = FakeHandler()
    client = make_client(handler)

    client.post(
        PUSH_PATH,
        content=body(push_envelope(delivery_attempt=4)),
        headers={"Content-Type": "application/json"},
    )

    assert handler.last_event.delivery_attempt == 4


def test_the_handler_is_injected_not_constructed(make_client):
    """A handler built inside create_app could not be swapped or faked, which
    is the entire reason this endpoint is testable without a real consumer."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / "processor" / "app.py").read_text(encoding="utf-8")

    assert "LoggingHandler(" not in source, "app.py should not construct a handler"


# -- request hygiene ------------------------------------------------------------


def test_a_get_on_the_push_path_is_405(client):
    assert client.get(PUSH_PATH).status_code == 405


def test_an_empty_body_is_acked_with_204(make_client):
    """Not a Pub/Sub message, and it will never become one."""
    client = make_client(FakeHandler())

    assert client.post(PUSH_PATH, content=b"").status_code == 204


# -- logging --------------------------------------------------------------------


def test_a_malformed_envelope_is_logged_at_error(make_client):
    """Acked, but never silently: this is the only record that a message was
    dropped, and nothing downstream will ever mention it again."""
    client = make_client(FakeHandler())

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=b"not json")

    errors = [line for line in emitted(stream) if line["severity"] == "ERROR"]
    assert errors, "a dropped message must be logged at ERROR"


def test_a_permanent_error_is_logged_at_error(make_client):
    from processor.handler import PermanentError

    client = make_client(FakeHandler(raise_with=PermanentError("unknown event type")))

    with capture_processor_output() as stream:
        push(client)

    errors = [line for line in emitted(stream) if line["severity"] == "ERROR"]
    assert errors
    assert any(line.get("event_id") == EVENT_ID for line in errors)


def test_a_success_is_logged_with_the_event_id(make_client):
    client = make_client(FakeHandler())

    with capture_processor_output() as stream:
        push(client)

    assert any(line.get("event_id") == EVENT_ID for line in emitted(stream))


def test_the_payload_never_reaches_the_logs(make_client):
    """The processor holds the DECODED payload, unlike ingest which only ever
    hashes opaque bytes. A leak here is one careless format string away."""
    canary = b'{"customer_email":"PII-CANARY-8fe31a9c"}'
    client = make_client(FakeHandler(raise_with=ValueError("boom")))

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(push_envelope(canary)))

    assert "PII-CANARY-8fe31a9c" not in stream.getvalue()


def test_the_payload_never_reaches_the_response(make_client):
    canary = b'{"customer_email":"PII-CANARY-8fe31a9c"}'
    client = make_client(FakeHandler(raise_with=ValueError("boom")))

    response = client.post(PUSH_PATH, content=body(push_envelope(canary)))

    assert b"PII-CANARY-8fe31a9c" not in response.content


def test_the_processor_does_not_use_print():
    from tests.conftest import package_sources

    offenders = [
        f"{path.name}:{n}"
        for path in package_sources("processor")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith("print(")
    ]

    assert not offenders, f"print() found at {offenders}; use the logger"


# -- the handler seam -----------------------------------------------------------


def test_the_fake_satisfies_the_handler_protocol():
    """Structural check: anything with handle(event) can stand in."""
    from processor.handler import Handler

    typed: Handler = FakeHandler()

    assert callable(typed.handle)


def test_the_default_handler_runs_without_raising():
    """`LoggingHandler` is what makes the service runnable on day one, so the
    swap point is a named thing rather than a TODO."""
    from processor.envelope import parse_push_envelope
    from processor.handler import LoggingHandler

    event = parse_push_envelope(body(push_envelope()))

    LoggingHandler().handle(event)  # must not raise


def test_the_default_handler_logs_the_event_id_and_not_the_payload():
    from processor.envelope import parse_push_envelope
    from processor.handler import LoggingHandler

    canary = b'{"customer_email":"PII-CANARY-8fe31a9c"}'
    event = parse_push_envelope(body(push_envelope(canary)))

    with capture_processor_output() as stream:
        LoggingHandler().handle(event)

    lines = emitted(stream)
    assert any(line.get("event_id") == EVENT_ID for line in lines)
    assert "PII-CANARY-8fe31a9c" not in stream.getvalue()


def test_the_handler_module_knows_nothing_about_http():
    """So a pull runner can drive the same handler unchanged if push is ever
    the wrong shape — that is what keeps the transport decision reversible."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / "processor" / "handler.py").read_text(encoding="utf-8")

    for web in ("fastapi", "starlette", "Request", "Response"):
        assert web not in source, f"{web} referenced in handler.py"


def test_the_errors_are_distinct_types():
    """The endpoint switches on these, so they cannot be the same class and
    neither may be a bare Exception."""
    from processor.handler import PermanentError, RetryableError

    assert issubclass(RetryableError, Exception)
    assert issubclass(PermanentError, Exception)
    assert not issubclass(RetryableError, PermanentError)
    assert not issubclass(PermanentError, RetryableError)
