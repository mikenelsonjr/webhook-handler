"""Turn a Pub/Sub push body into an ``Event``.

Pub/Sub wraps the published message in an envelope and POSTs it::

    {
      "message": {
        "data": "<base64 of the exact bytes the provider sent>",
        "attributes": {"event_id": ..., "source": ..., "received_at": ...},
        "messageId": "12345",
        "publishTime": "2026-08-28T15:04:05.123Z"
      },
      "subscription": "projects/<p>/subscriptions/<s>",
      "deliveryAttempt": 3
    }

``data`` is base64 of the **raw provider body**, because ingest publishes those
bytes untouched. Decoding must return them byte-for-byte: that is what lets a
consumer re-verify a provider signature, or diff against what the provider
claims it sent. Round-tripping through ``json.loads``/``json.dumps`` would
reorder keys and drop whitespace and quietly destroy it — which is why the
payload stays opaque ``bytes`` here and is never parsed.

WHY THE ERRORS SAY SO LITTLE
----------------------------
``EnvelopeError`` messages describe the *shape* that was wrong and never quote
the body. The endpoint logs this failure, and a malformed envelope still
carries a real customer payload.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The contract with ingest, pinned from the other side in tests/contract/.
# Attributes are chosen, not forwarded, so this list is the whole set.
REQUIRED_ATTRIBUTES = ("event_id", "source", "received_at")


class EnvelopeError(Exception):
    """A push body that is not a well-formed Pub/Sub envelope.

    Deliberately its own type, and deliberately narrow. The endpoint catches
    this specifically to answer 204 — a malformed message will never parse, and
    since any non-2xx in push delivery is a nack, returning an error status
    would ask Pub/Sub to redeliver it until the retention window expires.
    Catching a broader exception there would give the same permanent ack to
    genuine bugs, which must nack and reach the dead-letter topic instead.
    """


@dataclass(frozen=True)
class Event:
    """One webhook delivery, as it arrived from the topic.

    Frozen because a handler that rewrote it would make the dedup key and the
    logs disagree about what actually arrived.
    """

    event_id: str
    source: str
    received_at: str
    # repr=False: this is the decoded webhook payload — names, addresses,
    # contact details. Ingest never holds it in decoded form (it hashes opaque
    # bytes), but this service does, so a single `logger.debug("handling %s",
    # event)` would write a customer record into Cloud Logging with indefinite
    # retention. Excluding it here makes the safe thing the default.
    data: bytes = field(repr=False)
    message_id: str
    publish_time: str
    # Present ONLY when the subscription has a dead-letter policy, so its
    # absence is a subscription-configuration fact, not a malformed message.
    delivery_attempt: int | None = None


def parse_push_envelope(body: bytes) -> Event:
    """Parse a push request body, or raise ``EnvelopeError``.

    Every failure is permanent by construction: nothing here depends on state
    that could change between deliveries, so a body that fails once fails
    forever. That is what makes acking it the correct response.
    """
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvelopeError("body is not JSON") from exc

    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope is not a JSON object")

    message = envelope.get("message")
    if not isinstance(message, dict):
        raise EnvelopeError("envelope has no message object")

    attributes = message.get("attributes")
    if not isinstance(attributes, Mapping):
        raise EnvelopeError("message has no attributes object")

    return Event(
        event_id=_require_string(attributes, "event_id", "message.attributes"),
        source=_require_string(attributes, "source", "message.attributes"),
        received_at=_require_string(attributes, "received_at", "message.attributes"),
        data=_decode(message.get("data")),
        # Pub/Sub always sends both. Their absence means this is not a push
        # envelope, and an Event with a blank message_id logs as a blank.
        message_id=_require_string(message, "messageId", "message"),
        publish_time=_require_string(message, "publishTime", "message"),
        delivery_attempt=_delivery_attempt(envelope.get("deliveryAttempt")),
    )


def _require_string(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        # Empty counts as missing: a blank event_id would dedup against every
        # other blank one, which is worse than refusing the message.
        raise EnvelopeError(f"{where}.{key} is missing or not a non-empty string")
    return value


def _decode(encoded: Any) -> bytes:
    """Base64-decode the payload, or raise.

    ``validate=True`` matters. Without it ``b64decode`` silently discards every
    character outside the base64 alphabet, so a corrupted payload decodes to
    something plausible and is handed to the handler as though the provider had
    sent it::

        >>> base64.b64decode('eyJh!!!bW91bnQiOjEwMH0=')
        b'{"amount":100}'                      # junk stripped, no complaint

    Corruption should be a rejected message, not a silently repaired one.
    (Truncation is caught either way — that one fails the padding check.)
    """
    if encoded is None:
        return b""  # Pub/Sub omits `data` for an attribute-only message.
    if not isinstance(encoded, str):
        raise EnvelopeError("message.data is not a string")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError("message.data is not valid base64") from exc


def _delivery_attempt(value: Any) -> int | None:
    if value is None:
        return None
    # bool is a subclass of int, and `deliveryAttempt: true` is not attempt 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeError("deliveryAttempt is not an integer")
    return value
