"""Entrypoint for the processor: the only place that reads the environment.

    uvicorn processor_main:app --env-file .env             # local, via compose
    uvicorn processor_main:app --host 0.0.0.0 --port $PORT # container

One image, two deployables. The receiver's entrypoint is ``main.py``; this file
is the subscriber's, and it deliberately imports nothing from ``ingest`` — that
package needs the receiver's configuration to import, and a processor revision
must not fail because a webhook secret is unset.

Settings are built at import, so a missing or malformed variable crashes the
container at startup. Cloud Run then marks the revision unhealthy and keeps the
previous one serving, instead of the service booting fine and nacking the first
real message — which, on a subscription with a dead-letter policy, spends that
message's retry budget on a configuration error.
"""

from __future__ import annotations

import os

from common.log import configure_logging
from processor.app import create_app
from processor.config import Settings
from processor.dedup import InMemorySeenStore
from processor.handler import Handler, LoggingHandler

# Before anything else logs. The formatter emits `extra=` fields, which is what
# puts `event_id` — the only identifier spanning ingest, the topic, and this
# service — into the output rather than merely onto the LogRecord.
configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

settings = Settings.from_env(os.environ)

# The swap point. Replace LoggingHandler with your own consumer: anything with
# `handle(event)` satisfies the protocol, and it never learns that HTTP or
# Pub/Sub exist, so the same object runs behind a pull subscriber unchanged.
handler: Handler = LoggingHandler()

# Per-instance and NOT a distributed guarantee — see processor/dedup.py. Cloud
# Run runs N containers and a redelivery lands wherever the load balancer sends
# it, so this defuses a redelivery storm on a warm instance and nothing more.
# Swap in Firestore or Redis behind the same two methods for a real one.
seen = InMemorySeenStore(
    ttl_seconds=settings.dedup_ttl_seconds,
    max_entries=settings.dedup_max_entries,
)

app = create_app(settings=settings, handler=handler, seen=seen)
