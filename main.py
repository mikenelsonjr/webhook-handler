"""Entrypoint: the only place that reads the ambient environment.

    uvicorn main:app --env-file .env        # local, against the emulator
    uvicorn main:app --host 0.0.0.0 --port $PORT   # container

Settings are built at import, so a missing or malformed variable crashes the
container at startup. Cloud Run then marks the revision unhealthy and keeps the
previous one serving, instead of the service booting fine and 503-ing the first
real webhook — which, since Aptly never retries, would be a lost event.
"""

from __future__ import annotations

import os

from common.log import configure_logging
from ingest.app import create_app
from ingest.config import Settings
from ingest.publisher import Publisher
from ingest.pubsub_publisher import PubSubPublisher
from ingest.retry import RetryingPublisher

# Before anything else logs. The formatter emits `extra=` fields, which is what
# puts `event_id` — the only identifier spanning ingest, topic, and processor —
# into the output rather than merely onto the LogRecord.
configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

settings = Settings.from_env(os.environ)

# RetryingPublisher wraps the real one. Aptly delivers each event once and
# ignores the response, so a 503 earns no redelivery — a few bounded attempts
# here are the only thing standing between a transient blip and a lost event.
publisher: Publisher = RetryingPublisher(PubSubPublisher(settings))

app = create_app(settings=settings, publisher=publisher)
