"""What to do with an event, and how a failure is classified.

This module is where a consumer's real logic goes. It deliberately knows
nothing about HTTP — no web framework types, no status codes — so the same
handler runs unchanged behind a pull subscriber if push ever turns out to be
the wrong shape. That is what keeps the transport decision reversible; see
CONTEXT.md, "Push, not pull".

THE TWO ERRORS ARE THE INTERFACE
--------------------------------
The endpoint cannot know whether a failure is worth retrying, and guessing is
how a service either loses events or redelivers a poison pill forever. So the
handler says which it is, and there are only two answers:

``RetryableError``
    Transient. The same event might succeed in ten seconds — a downstream
    timeout, a rate limit, a lock held elsewhere. The endpoint nacks and
    Pub/Sub redelivers with backoff.

``PermanentError``
    This event will never succeed, however many times it arrives: an event type
    this consumer does not implement, a payload that is well-formed but
    meaningless here. The endpoint acks, because redelivering it just burns the
    retry budget on the way to the dead-letter topic.

Anything else — a ``KeyError``, a bug — is neither, and the endpoint treats it
as retryable on purpose. Pub/Sub bounds the attempts and then dead-letters, so
an unrecognised failure gets investigated rather than silently dropped.
"""

from __future__ import annotations

import logging
from typing import Protocol

from processor.envelope import Event

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """The event might succeed on a later delivery. Nack, and let Pub/Sub retry."""


class PermanentError(Exception):
    """The event will never succeed. Ack, and let the operator read the log."""


class Handler(Protocol):
    def handle(self, event: Event) -> None:
        """Process one event.

        Return normally to acknowledge it. Raise ``RetryableError`` to ask for
        a redelivery, or ``PermanentError`` to give up on this event for good.

        Must be idempotent: delivery is at-least-once, from Pub/Sub redelivery
        and from ingest retrying a publish that timed out after the message was
        already stored.
        """


class LoggingHandler:
    """The default: log that the event arrived, and do nothing else.

    Exists so the service is runnable end to end on day one, and so the place a
    consumer plugs in their own logic is a named class rather than a TODO.

    It logs the ``event_id`` and the payload's *size* — never the payload. This
    service is the first place a decoded webhook body exists (ingest only ever
    hashed opaque bytes), so writing one to Cloud Logging is a new risk here,
    and the shipped example should not be the thing that demonstrates how.
    """

    def handle(self, event: Event) -> None:
        logger.info(
            "event received",
            extra={
                "event_id": event.event_id,
                "source": event.source,
                "message_id": event.message_id,
                "payload_bytes": len(event.data),
                "delivery_attempt": event.delivery_attempt,
            },
        )
