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
import json

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
