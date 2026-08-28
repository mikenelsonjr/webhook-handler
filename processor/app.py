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

from processor.config import NONE, OIDC, Settings
from processor.dedup import SeenStore
from processor.envelope import EnvelopeError, parse_push_envelope
from processor.handler import Handler, PermanentError, RetryableError
from processor.security import verify_push_token

logger = logging.getLogger(__name__)

ACK = 204
NACK = 503
UNAUTHORIZED = 401


def create_app(
    settings: Settings,
    handler: Handler,
    seen: SeenStore,
    *,
    verify=verify_push_token,
) -> FastAPI:
    """Build the processor application.

    ``seen`` is required rather than optional. Delivery is at-least-once — from
    Pub/Sub redelivery and from ingest retrying a publish that timed out after
    the message was already stored — so an app running with no idempotency at
    all should be a decision someone made, not a default they inherited. Pass a
    store that always claims to opt out.

    ``verify`` and ``seen`` are both injected so the whole path is testable with
    no network and no credentials, and so the in-memory store can be swapped
    for Firestore or Redis without touching this file.
    """
    # Docs are disabled: the only caller is Pub/Sub, and an interactive schema
    # browser is attack surface with no audience.
    app = FastAPI(title="Webhook processor", docs_url=None, redoc_url=None, openapi_url=None)

    if settings.push_auth_mode == NONE:
        # Legitimate against the emulator, which sends no token, and an alarm
        # anywhere else. Logged rather than refused because refusing would make
        # local development impossible — but it must be visible in the logs of
        # a deployment that reached this state by accident.
        logger.warning(
            "PUSH_AUTH_MODE=none: this endpoint is unauthenticated. "
            "Local development only — production wants iam or oidc.",
            extra={"push_auth_mode": settings.push_auth_mode},
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness probe. Unauthenticated — Cloud Run's prober has no identity,
        so requiring a token here would make the revision permanently unhealthy."""
        return Response(status_code=200)

    @app.post("/_pubsub/push")
    async def push(request: Request) -> Response:
        # 0. Authenticate, before anything that would reveal another fault.
        #    In `iam` mode Cloud Run already did this and the request could not
        #    have arrived otherwise; in `none` mode there is nothing to check.
        if settings.push_auth_mode == OIDC:
            authorization = request.headers.get("Authorization")
            # The absent-header case is refused here rather than delegated, so
            # a swapped-in verifier that mishandles None cannot turn "no
            # credential at all" into an accepted request.
            if not authorization or not verify(
                authorization,
                audience=settings.push_audience,
                service_account=settings.push_service_account,
            ):
                # Logged by the endpoint, not only by the verifier: this is the
                # record that a request was refused, and it has to exist
                # whatever decided it. The header is never logged — it carries
                # a live bearer credential.
                logger.warning("rejecting unauthenticated push request")
                # 401, not 204: this is not an acknowledgement decision at all.
                # A real Pub/Sub delivery is authenticated, so a request that
                # is not is something else, and Pub/Sub is not waiting on it.
                return Response(status_code=UNAUTHORIZED)

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

        # 2. Claim, before handling. Claiming after would mean a crash between
        #    the two leaves the event unclaimed and it runs twice; claiming
        #    before means a crash leaves the claim standing and it never runs.
        #    Neither is free, and this one errs toward a duplicate rather than
        #    a loss — the same trade the rest of the service makes.
        if not seen.claim(event.event_id):
            # Already handled. Ack: whatever produced the duplicate — a
            # redelivery, or ingest retrying a publish that had actually
            # succeeded — is satisfied by the same answer the first one got.
            logger.info("duplicate event, acking without handling", extra=_context(event))
            return Response(status_code=ACK)

        # 3. Dispatch. The handler classifies its own failures, because the
        #    endpoint cannot tell a transient fault from a permanent one and
        #    guessing is how a service either loses events or redelivers a
        #    poison pill forever.
        #
        #    Every failure path releases the claim. For a nack that is not
        #    optional: a nack asks Pub/Sub to send the event again, and a
        #    surviving claim would make the endpoint swallow that redelivery,
        #    so the two together would lose the event outright.
        try:
            handler.handle(event)
        except PermanentError as exc:
            # Released even though this acks. The event was abandoned, not
            # completed, so a genuine duplicate arriving later should be
            # abandoned again visibly rather than skipped in silence.
            seen.release(event.event_id)
            logger.error(
                "permanent failure, acking: %s",
                type(exc).__name__,
                extra=_context(event),
            )
            return Response(status_code=ACK)
        except RetryableError as exc:
            seen.release(event.event_id)
            logger.warning(
                "retryable failure, nacking: %s",
                type(exc).__name__,
                extra=_context(event),
            )
            return Response(status_code=NACK)
        except Exception as exc:
            seen.release(event.event_id)
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
