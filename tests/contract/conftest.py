"""Helpers for the contract suite — the envelope both components must agree on.

WHY THIS DIRECTORY EXISTS
-------------------------
Ingest publishes an envelope and the processor consumes it. If each side
asserts its own idea of that shape, they drift and no test notices: ingest's
suite passes because ingest does what ingest expects, and the processor's
passes for the mirror-image reason. The failure only shows up in production,
as a message that was published fine and cannot be read.

So these tests belong to neither component and get their own directory.

WHY NOTHING HERE IMPORTS THE COMPONENT SUITES
---------------------------------------------
This file deliberately re-declares its own publisher double and its own
rendering of a Pub/Sub push envelope rather than importing
``tests/ingest/conftest.py`` or ``tests/processor/conftest.py``.

That duplication is the entire point. A contract test that borrows one side's
fixtures has adopted one side's assumptions, and can no longer catch the case
where that side is wrong. The envelope below is written from the Pub/Sub push
format, not from either implementation — it is the third, independent
statement that the other two are checked against.

If you change this file to make a test pass, stop: you are editing the
contract, and that is a decision about the system rather than about a test.
"""

from __future__ import annotations

import base64
import json

SIGNING_KEY = "contract-suite-key-not-a-real-credential"  # pragma: allow-secret
SIGNING_KEY_HEADER = "X-SigningKey"
SOURCE = "aptly"

#: Exactly what ingest is allowed to put on a message. Attributes are chosen,
#: not forwarded — an earlier draft published dict(request.headers), which put
#: the caller's Authorization, Cookie, and the signing key itself onto the
#: topic for every subscriber to read.
CONTRACT_ATTRIBUTES = frozenset({"event_id", "source", "received_at"})

#: Pub/Sub's own hard limits. A message that exceeds one of these is rejected
#: at publish time, so they bound what the two sides may agree to.
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
MAX_ATTRIBUTE_VALUE_BYTES = 1024
MAX_ATTRIBUTE_KEY_BYTES = 256
MAX_ATTRIBUTES = 100


class RecordingPublisher:
    """Captures exactly what ingest handed to the Pub/Sub client.

    Deliberately not ingest's own FakePublisher: this suite must observe the
    boundary, not reuse the double the other side is already tested against.
    """

    def __init__(self, message_id: str = "contract-msg-1"):
        self.message_id = message_id
        self.published: list[tuple[bytes, dict]] = []

    def publish(self, data: bytes, attributes=None) -> str:
        self.published.append((data, dict(attributes or {})))
        return self.message_id

    @property
    def last(self) -> tuple[bytes, dict]:
        assert self.published, "ingest published nothing"
        return self.published[-1]


def ingest_client(publisher, **setting_overrides):
    """An ingest app wired to a recording publisher."""
    from fastapi.testclient import TestClient

    from ingest.app import create_app
    from ingest.config import Settings

    settings = Settings(
        gcp_project="contract-project",
        pubsub_topic="contract-topic",
        signing_secret=SIGNING_KEY,
        signing_key_header=SIGNING_KEY_HEADER,
        source_name=SOURCE,
        **setting_overrides,
    )
    return TestClient(
        create_app(settings=settings, publisher=publisher),
        raise_server_exceptions=False,
    )


def deliver(publisher, raw: bytes):
    """Send `raw` to ingest exactly as a provider would, and return the response."""
    return ingest_client(publisher).post(
        "/webhook",
        content=raw,
        headers={"Content-Type": "application/json", SIGNING_KEY_HEADER: SIGNING_KEY},
    )


def as_push_envelope(
    data: bytes,
    attributes: dict,
    *,
    message_id: str = "1",
    publish_time: str = "2026-08-28T15:04:05.123Z",
):
    """Render what Pub/Sub POSTs to a push endpoint.

    Written from the Pub/Sub push format rather than from either side's code.
    ``data`` is base64 of the message body; the attributes ride alongside it,
    untouched.
    """
    return json.dumps(
        {
            "message": {
                "data": base64.b64encode(data).decode(),
                "attributes": attributes,
                "messageId": message_id,
                "publishTime": publish_time,
            },
            "subscription": "projects/contract-project/subscriptions/contract-sub",
        }
    ).encode()


def round_trip(raw: bytes):
    """Drive one payload the whole way: provider -> ingest -> topic -> processor.

    Returns (ingest response, parsed Event, published attributes).
    """
    from processor.envelope import parse_push_envelope

    publisher = RecordingPublisher()
    response = deliver(publisher, raw)
    data, attributes = publisher.last
    event = parse_push_envelope(as_push_envelope(data, attributes))
    return response, event, attributes
