"""A bounded retry wrapper around any Publisher.

WHY THIS EXISTS
---------------
Aptly delivers each event exactly once and ignores the response, so a 503 does
not buy a retry from the sender — a failed publish is a lost event. The
durability has to live on this side of the wire.

This is deliberately modest: a few attempts with exponential backoff, enough to
absorb a transient Pub/Sub blip, and no more. It is not a substitute for a
durable spool (write failures to GCS and reconcile), which is the upgrade path
if losses ever show up in practice.

WHY A WRAPPER AND NOT CODE IN THE ROUTE
---------------------------------------
It satisfies the ``Publisher`` protocol and consumes one, so it composes:

    publisher = RetryingPublisher(PubSubPublisher(settings))

The route stays a single ``publisher.publish(...)`` call and does not know
retrying happens. Removing it is a one-line change at the entrypoint, and the
whole thing is testable against the same FakePublisher as everything else.

THE TRADEOFF, STATED PLAINLY
----------------------------
Retrying a publish that timed out *after* Pub/Sub durably stored the message
produces a duplicate. Given the sender never retries, losing an event is the
worse outcome, so this errs toward at-least-once — which is what the
``event_id`` attribute exists to let the processor clean up.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

from ingest.publisher import Publisher, PublishError

logger = logging.getLogger(__name__)


class RetryingPublisher:
    """Retry a wrapped publisher a bounded number of times on PublishError.

    Only ``PublishError`` (and its subclass ``PublishTimeout``) is retried.
    Anything else is a bug rather than a transient fault, and retrying it would
    just delay the failure while hiding the cause.
    """

    def __init__(
        self,
        inner: Publisher,
        *,
        attempts: int = 3,
        backoff_seconds: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._inner = inner
        self._attempts = attempts
        self._backoff_seconds = backoff_seconds
        # Injected so tests do not actually sleep.
        self._sleep = sleep

    def publish(self, data: bytes, attributes: Mapping[str, str]) -> str:
        # Every path through the loop either returns or raises, so there is no
        # fall-through: `attempts` is validated >= 1 in __init__.
        for attempt in range(1, self._attempts + 1):
            try:
                return self._inner.publish(data, attributes)
            except PublishError as exc:
                if attempt == self._attempts:
                    raise  # out of attempts; the caller turns this into a 503
                delay = self._backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "publish attempt %d/%d failed: %s; retrying in %.2fs",
                    attempt,
                    self._attempts,
                    type(exc).__name__,
                    delay,
                    extra={"event_id": dict(attributes or {}).get("event_id", "")},
                )
                self._sleep(delay)
