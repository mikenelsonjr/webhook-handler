
# from inspect import signature

import hashlib
from venv import logger

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ingest.config import Settings
from ingest.publisher import Publisher, PublishError
from ingest.security import verify_signature

def create_app(settings: Settings, publisher: Publisher) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def health_check():
        return {"status": "healthy"}

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        body = await request.body()
        attributes = dict(request.headers)
        signature = request.headers.get(settings.signature_header)

        event_id = attributes.get(settings.delivery_id_header) or hashlib.sha256(body).hexdigest()

        if not verify_signature(settings.webhook_secret, body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            message_id = publisher.publish(body, attributes)

           
        except PublishError as e:
            # return JSONResponse({"error": str(e)}, status_code=503)
            logger.error("publish failed", exc_info=e, extra={"event_id": event_id})
            raise HTTPException(status_code=503, detail="Failed to publish message") from e

         # Return a 202 Accepted response with the message ID as json
        return JSONResponse({"message_id": message_id, "event_id": event_id}, status_code=202)

    return app