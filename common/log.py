"""Structured logging, shared by the ingest and processor entrypoints.

WHY THIS EXISTS
---------------
The service used to configure logging like this::

    logging.basicConfig(format='{"severity":"%(levelname)s",...,"message":"%(message)s"}')

which has two bugs, both invisible until you need the logs.

**It drops every ``extra=`` field.** ``extra=`` sets attributes on the
``LogRecord``; a ``Formatter`` emits only what its format string names. So
``ingest/app.py`` passed ``event_id`` on every publish and the emitted line
contained none of it. Adding ``%(event_id)s`` to the format string is not the
fix either — every record *without* that attribute (uvicorn's, any library's)
would then raise at format time.

**It emits invalid JSON.** The object is built by string interpolation, so a
message containing a ``"`` — a GCP error quoting the topic path, say — produces
unparseable output, and Cloud Logging demotes exactly the ERROR line you were
chasing to an unstructured text blob.

Both go away by building a dict and handing it to ``json.dumps``.

WHY IT IS SHARED
----------------
``event_id`` is the only identifier that spans ingest -> topic -> processor, so
one value grepped across both services has to return the whole story. Two
copies of a twenty-line formatter is how the two halves of one trace end up
disagreeing about what a field is called, which is the moment the trace was
supposed to be useful.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# The keys this formatter owns. An `extra=` field may not overwrite them:
# `logging` guards its own reserved names but knows nothing about "severity",
# so without this a caller could silently downgrade an ERROR line.
_OWNED = ("severity", "logger", "message", "exception", "stack")

# Everything the logging machinery itself puts on a record — absolute source
# paths, process ids, monotonic clocks. Anything *not* in here arrived via
# `extra=` and is a field someone meant to be read.
#
# Derived from a throwaway record rather than hardcoded, because the set grows:
# `taskName` appeared in 3.12 and a hand-maintained list would have leaked it
# into every line. `message` and `asctime` are added by Formatter.format().
_PROBE = logging.LogRecord(
    name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
)
_RESERVED = frozenset(_PROBE.__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record, including whatever `extra=` carried.

    ``severity`` is spelled that way on purpose: Cloud Logging reads that exact
    key to set a log entry's level. Under any other name every line is filed as
    DEFAULT, and severity-based alerting quietly stops working.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "logger": record.name,
            # getMessage(), not record.msg — %-style args are interpolated here.
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED or key in _OWNED or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            # A field rather than text appended after the object: the base
            # class appends the traceback *outside* what it formatted, which
            # would put raw multi-line text after the closing brace.
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text

        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str so an object nobody thought to make serializable degrades
        # to its repr instead of raising. Logging must never be the thing that
        # takes the service down.
        return json.dumps(payload, default=str)


def configure_logging(level: str | int = "INFO", *, stream=None) -> None:
    """Install the JSON formatter as the process-wide logging configuration.

    Replaces ``logging.basicConfig``. Call it once, from an entrypoint, before
    anything logs.

    uvicorn installs handlers of its own with plain-text formatters. Left
    alone, half the container's output is unstructured — including the access
    log, which is the half you would want to join against an ``event_id``. So
    its handlers are removed and its records left to propagate to the root
    handler installed here.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
