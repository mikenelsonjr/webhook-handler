"""Fixtures and helpers for the processor suite.

Scoped to this directory: pytest layers conftest files, so nothing here is
visible to the ingest or contract suites. Repo-wide helpers live in
``tests/conftest.py``.

THE CONTRACT THESE TESTS ASSUME
-------------------------------
Build these and the tests have something to bind to::

    processor/envelope.py        <- must NOT import google.cloud
        class EnvelopeError(Exception): ...

        @dataclass(frozen=True)
        class Event:
            event_id: str
            source: str
            received_at: str
            data: bytes           # repr=False — see below
            message_id: str
            publish_time: str
            delivery_attempt: int | None = None

        def parse_push_envelope(body: bytes) -> Event

    processor/handler.py         <- must NOT import fastapi or starlette
        class RetryableError(Exception): ...   # -> 503, Pub/Sub redelivers
        class PermanentError(Exception): ...   # -> 204, it will never succeed
        class Handler(Protocol):
            def handle(self, event: Event) -> None: ...
        class LoggingHandler: ...              # the runnable default stub

    processor/app.py
        def create_app(settings, handler, seen=None) -> FastAPI

Routes: ``GET /healthz`` (unauthenticated) and ``POST /_pubsub/push``.

THE ACK SEMANTICS, WHICH ARE THE WHOLE STORY
--------------------------------------------
In push delivery the response status **is** the acknowledgement, so:

===========================  ======  ==========================================
Outcome                      Status  Meaning to Pub/Sub
===========================  ======  ==========================================
handled                      204     ack
PermanentError               204     ack — it will never succeed
envelope unparseable         204     ack — bad bytes never become good bytes
RetryableError               503     nack — redeliver with backoff
any unexpected exception     503     nack — redeliver, then dead-letter
===========================  ======  ==========================================

The third row is the counterintuitive one. **Any** non-2xx is a nack, so the
reflexive ``400 Bad Request`` on unparseable input tells Pub/Sub to send it
again — and it will, with backoff, until the message ages out of retention.

Note this inverts ``ingest/retry.py``, which retries only ``PublishError`` and
lets anything unexpected propagate because an unknown failure is a bug and
retrying buries it. Here an unknown exception nacks, because Pub/Sub retries a
bounded number of times and then dead-letters — so the failure is investigated
from the DLQ rather than lost, and a transient fault gets its second chance.

WHAT A PUSH ENVELOPE LOOKS LIKE
-------------------------------
Pub/Sub POSTs this JSON — no client library involved, which is why the
processor imports nothing from ``google``::

    {
      "message": {
        "data": "<base64 of the exact bytes the provider sent>",
        "attributes": {"event_id": ..., "source": ..., "received_at": ...},
        "messageId": "12345",
        "publishTime": "2026-08-28T15:04:05.000Z"
      },
      "subscription": "projects/<p>/subscriptions/<s>",
      "deliveryAttempt": 3
    }

WHY ``data`` IS NOT IN THE REPR
-------------------------------
It is the decoded webhook payload — names, addresses, contact details. Ingest
never holds it in decoded form (it hashes opaque bytes), but the processor
does, so one ``logger.debug("handling %s", event)`` would put a customer record
into Cloud Logging with indefinite retention. ``field(repr=False)`` makes the
safe thing the default.

WHY ``deliveryAttempt`` IS OPTIONAL
-----------------------------------
Pub/Sub includes it **only** when the subscription has a dead-letter policy.
Its absence is a subscription-configuration fact, not a malformed message, so
requiring it would reject every message on a correctly-working subscription
that happens to have no DLQ yet.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging

import pytest

EVENT_ID = "a" * 64  # a SHA-256 hex digest, as ingest produces
SOURCE = "aptly"
RECEIVED_AT = "2026-08-28T15:04:05.000000+00:00"
PUBLISH_TIME = "2026-08-28T15:04:05.123Z"
MESSAGE_ID = "12345"
SUBSCRIPTION = "projects/local-project/subscriptions/webhook-events-push"

PAYLOAD = b'{"action":"update","data":{"_id":"abc"}}'


def attributes(**overrides) -> dict[str, str]:
    """The three attributes ingest publishes, and nothing else."""
    return {
        "event_id": EVENT_ID,
        "source": SOURCE,
        "received_at": RECEIVED_AT,
        **overrides,
    }


def push_envelope(
    data: bytes | None = PAYLOAD,
    *,
    attrs: dict[str, str] | None = None,
    message_id: str = MESSAGE_ID,
    publish_time: str = PUBLISH_TIME,
    delivery_attempt: int | None = None,
    encoded_data: str | None = None,
) -> dict:
    """Build a push envelope as a dict, so a test can mutate it before sending.

    ``encoded_data`` bypasses the base64 encoding to plant something invalid;
    ``data=None`` omits the key entirely, which is what Pub/Sub does for an
    attribute-only message.
    """
    message: dict = {
        "attributes": attributes() if attrs is None else attrs,
        "messageId": message_id,
        "publishTime": publish_time,
    }
    if encoded_data is not None:
        message["data"] = encoded_data
    elif data is not None:
        message["data"] = base64.b64encode(data).decode()

    envelope: dict = {"message": message, "subscription": SUBSCRIPTION}
    if delivery_attempt is not None:
        envelope["deliveryAttempt"] = delivery_attempt
    return envelope


def body(envelope: dict) -> bytes:
    """Serialize an envelope the way Pub/Sub puts it on the wire."""
    return json.dumps(envelope).encode()


class FakeHandler:
    """In-memory stand-in for the business logic behind the endpoint.

    Records every event so a test can assert both *that* it ran and *what* it
    received, and can be armed to raise — which is how the ack-semantics tests
    work, since the whole story is which exception maps to which status.
    """

    def __init__(self, *, raise_with: Exception | None = None):
        self.raise_with = raise_with
        self.handled: list = []

    def handle(self, event) -> None:
        # Recorded before raising: a test needs to know the handler was reached
        # even on the paths where it then fails.
        self.handled.append(event)
        if self.raise_with is not None:
            raise self.raise_with

    @property
    def call_count(self) -> int:
        return len(self.handled)

    @property
    def last_event(self):
        assert self.handled, "the handler was never called"
        return self.handled[-1]


@pytest.fixture
def settings():
    from processor.config import Settings

    return Settings()


@pytest.fixture
def handler():
    return FakeHandler()


@pytest.fixture
def make_client(settings):
    """Build a TestClient over an app wired to a given handler."""
    from fastapi.testclient import TestClient

    from processor.app import create_app

    def _make(hand=None, **setting_overrides):
        import dataclasses

        cfg = dataclasses.replace(settings, **setting_overrides) if setting_overrides else settings
        return TestClient(
            create_app(settings=cfg, handler=hand or FakeHandler()),
            # So an unhandled exception surfaces as the response the endpoint
            # would really return, rather than being re-raised into the test.
            raise_server_exceptions=False,
        )

    return _make


@pytest.fixture
def client(make_client, handler):
    return make_client(handler)


def push(client, envelope: dict | None = None):
    """POST a push envelope the way Pub/Sub would."""
    return client.post(
        "/_pubsub/push",
        content=body(push_envelope() if envelope is None else envelope),
        headers={"Content-Type": "application/json"},
    )


@contextlib.contextmanager
def capture_processor_output():
    """Capture what the processor logger actually EMITS, formatted and parsed.

    A real handler rather than ``caplog``: caplog captures records before
    formatting, which is the blind spot that hid issue #9 for months.
    """
    from common.log import JsonFormatter

    stream = io.StringIO()
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("processor")
    logger.addHandler(stream_handler)
    # setLevel, NOT `logger.level = ...`: isEnabledFor() memoizes into
    # Logger._cache and only setLevel() invalidates it, so a direct assignment
    # can raise the level and still have records silently dropped.
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        logger.removeHandler(stream_handler)
        logger.setLevel(previous)


def emitted(stream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
