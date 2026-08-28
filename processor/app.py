"""FastAPI application factory for the Pub/Sub push subscriber.

A factory rather than a module-level ``app``, so each caller — every test, and
the entrypoint — supplies its own settings and handler. The routes close over
those, so there is no global state to configure or reset.

THE RESPONSE STATUS IS THE ACKNOWLEDGEMENT
------------------------------------------
Ingest's rule is *a 2xx is a promise*: never acknowledge what is not durable.
This is that sentence read from the other end — **a 2xx means "never send me
this again"** — and it decides every line below.

===========================  ======  ==========================================
Outcome                      Status  Meaning to Pub/Sub
===========================  ======  ==========================================
handled                      204     ack
``PermanentError``           204     ack — it will never succeed
envelope unparseable         204     ack — see below
``RetryableError``           503     nack — redeliver with backoff
any unexpected exception     503     nack — redeliver, then dead-letter
===========================  ======  ==========================================

**Acking a malformed envelope is the counterintuitive one, and it is the whole
point.** In push delivery *any* non-2xx is a nack, so the reflexive
``400 Bad Request`` on unparseable input tells Pub/Sub to send it again — and it
will, with backoff, until the message ages out of the retention window seven
days later. Bad bytes do not become good bytes on the third attempt, so the
message is logged, acked, and left for the operator to find in the log.

Note this **inverts** ``ingest/retry.py``, which retries only ``PublishError``
and lets anything unexpected propagate, on the grounds that an unknown failure
is a bug and retrying it buries the cause. Here an unknown exception nacks. The
difference is the backstop: Pub/Sub retries a bounded number of times and then
dead-letters, so an unknown failure is investigated from the DLQ rather than
lost, and a genuinely transient fault gets the second chance it deserves.
In-process retry had neither a ceiling nor a dead-letter.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response

from processor.config import Settings
from processor.envelope import EnvelopeError, parse_push_envelope
from processor.handler import Handler, PermanentError, RetryableError

logger = logging.getLogger(__name__)

ACK = 204
NACK = 503


def create_app(settings: Settings, handler: Handler, seen=None) -> FastAPI:
    """Build the processor application.

    ``seen`` is the idempotency store and is accepted but unused until #13.
    Delivery is at-least-once — from Pub/Sub redelivery and from ingest
    retrying a publish that timed out after the message was already stored — so
    until that story lands, a duplicate is dispatched to the handler twice and
    the handler must be idempotent on its own.
    """
    # Docs are disabled: the only caller is Pub/Sub, and an interactive schema
    # browser is attack surface with no audience.
    app = FastAPI(title="Webhook processor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness probe. Unauthenticated — Cloud Run's prober has no identity."""
        return Response(status_code=200)

    @app.post("/_pubsub/push")
    async def push(request: Request) -> Response:
        body = await request.body()

        # 1. Parse. A failure here is permanent by construction: nothing in the
        #    parse depends on state that could change between deliveries, so a
        #    body that fails once fails forever. Ack it rather than asking for
        #    a redelivery that cannot possibly go differently.
        try:
            event = parse_push_envelope(body)
        except EnvelopeError as exc:
            # Logged at ERROR because this is the ONLY record that a message
            # was dropped — nothing downstream will ever mention it again. The
            # reason is safe to log; the body is not, and is never included.
            #
            # There is no event_id to correlate on: the envelope carrying it is
            # the thing that failed to parse. The byte count is the little that
            # can be said safely, and it separates "something arrived and was
            # wrong" from "an empty probe hit the endpoint".
            logger.error(
                "dropping unparseable push message: %s",
                exc,
                extra={"body_bytes": len(body)},
            )
            return Response(status_code=ACK)

        # 2. Dispatch. The handler classifies its own failures, because the
        #    endpoint cannot tell a transient fault from a permanent one and
        #    guessing is how a service either loses events or redelivers a
        #    poison pill forever.
        try:
            handler.handle(event)
        except PermanentError as exc:
            logger.error(
                "permanent failure, acking: %s",
                type(exc).__name__,
                extra=_context(event),
            )
            return Response(status_code=ACK)
        except RetryableError as exc:
            logger.warning(
                "retryable failure, nacking: %s",
                type(exc).__name__,
                extra=_context(event),
            )
            return Response(status_code=NACK)
        except Exception as exc:
            # Deliberately broad, and deliberately a nack. An unrecognised
            # failure might be transient, and Pub/Sub's bounded retry plus the
            # dead-letter topic means it gets investigated rather than lost.
            # Caught here rather than allowed to escape: an unhandled exception
            # becomes a 500, which is also a nack, but sends a traceback out
            # with it and loses the event_id from the log line.
            logger.exception(
                "unexpected failure, nacking: %s",
                type(exc).__name__,
                extra=_context(event),
            )
            return Response(status_code=NACK)

        logger.info("event handled", extra=_context(event))
        return Response(status_code=ACK)

    return app


def _context(event) -> dict:
    """The fields every delivery log line carries.

    ``event_id`` is the only identifier that spans ingest, the topic, and this
    service, so one value grepped across both returns the whole story.
    ``delivery_attempt`` is included because a climbing count is the only
    visible sign that a message is heading for the dead-letter topic.

    The payload is deliberately absent.
    """
    return {
        "event_id": event.event_id,
        "message_id": event.message_id,
        "delivery_attempt": event.delivery_attempt,
    }
