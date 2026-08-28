"""FastAPI application factory for the webhook ingest service.

A factory rather than a module-level ``app`` so each caller — every test, and
the entrypoint — supplies its own settings and publisher. The routes close over
those, so there is no global state to configure or reset.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ingest.config import Settings
from ingest.publisher import Publisher, PublishError
from ingest.security import verify_signing_key

logger = logging.getLogger(__name__)


def create_app(settings: Settings, publisher: Publisher) -> FastAPI:
    """Build the ingest application.

    Rejections happen in a deliberate order — size, authentication, content
    type, payload — so that an unauthenticated caller learns nothing about the
    request's other faults, and so no work is done on an oversized body.
    """
    # Docs are disabled: this endpoint has exactly one machine caller, and an
    # interactive schema browser is attack surface with no audience.
    app = FastAPI(title="Webhook ingest", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness probe. Unauthenticated — Cloud Run's prober has no key."""
        return JSONResponse({"status": "ok"})

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        # 1. Size. Check the declared length first so an oversized body is
        #    refused before it is buffered, then the real length in case the
        #    header lied or was absent.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")

        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")

        # 2. Authentication, before anything that would reveal other faults.
        if not verify_signing_key(
            settings.signing_secret,
            request.headers.get(settings.signing_key_header),
        ):
            raise HTTPException(status_code=401, detail="invalid signing key")

        # 3. Content type. Tolerate parameters: "application/json; charset=utf-8".
        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="expected application/json")

        # 4. Payload. Parsed only to reject malformed input — the RAW bytes are
        #    what gets published. Re-serializing would reorder keys and drop
        #    whitespace, so consumers would no longer see what Aptly sent.
        try:
            json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="malformed JSON") from exc

        # Attributes are chosen, not forwarded. Publishing the request headers
        # would put the caller's signing key on the topic for every subscriber.
        event_id = hashlib.sha256(body).hexdigest()
        attributes = {
            "event_id": event_id,
            "source": settings.source_name,
            "received_at": datetime.now(UTC).isoformat(),
        }

        # 5. Publish. A 2xx is a promise the sender will not retry, so it is
        #    sent only once the message is durably queued.
        try:
            message_id = publisher.publish(body, attributes)
        except PublishError as exc:
            # The operator needs the topic; the caller gets none of it.
            logger.error(
                "publish failed: %s",
                type(exc).__name__,
                extra={"event_id": event_id, "topic": settings.pubsub_topic},
            )
            raise HTTPException(status_code=503, detail="unable to queue event") from exc

        logger.info(
            "event published",
            extra={"event_id": event_id, "message_id": message_id},
        )
        return JSONResponse(
            {"message_id": message_id, "event_id": event_id},
            status_code=202,
        )

    return app
