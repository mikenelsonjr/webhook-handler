"""Idempotency: has this event already been handled?

WHY THIS EXISTS
---------------
Delivery is at-least-once by construction, from two independent sources:

1. ``RetryingPublisher`` retries a publish that timed out *after* Pub/Sub
   durably stored the message, so one webhook becomes two topic messages.
2. Pub/Sub redelivers on every nack and every missed ack deadline.

Neither is a bug. Both are the correct behaviour of a system that would rather
deliver twice than lose an event, which pushes the deduplicating onto the
consumer — here.

READ THIS BEFORE TRUSTING IT: ``InMemorySeenStore`` IS PER-INSTANCE
--------------------------------------------------------------------
It buys **nothing across instances**. Cloud Run runs N containers and a
redelivery lands wherever the load balancer sends it, so two copies of one
event routinely land on two different instances and both are handled. What it
does buy is real but narrow: it defuses a rapid redelivery storm hitting a warm
instance, and it makes the seam concrete and tested.

For an actual guarantee, implement ``SeenStore`` against something shared —
Firestore with a create-if-absent and a TTL field, or Redis ``SET NX EX``. Both
are two methods, and nothing outside this module changes.

Inherited from ingest, and worth re-reading before leaning on any of this:
``event_id`` is a SHA-256 of the raw body, so it only dedups a *sender* retry
if the sender replays identical bytes — unverified for Aptly, whose payload
carries moving ``viewedAt``/``lockUntil`` timestamps — and two genuinely
distinct events with byte-identical bodies would collide, dropping the second.

WHEN THE STORE IS WRITTEN MATTERS MORE THAN WHICH STORE IT IS
--------------------------------------------------------------
Recording ``event_id`` *before* handling means a crash mid-handle leaves the
event marked done and never processed — silent loss, the exact failure ingest
was built to avoid. Recording it *after* means a crash leaves it unclaimed and
it is processed twice.

``claim``/``release`` splits the difference: claim before, release on failure so
a redelivery may retry, leave the claim standing on success. If the process
dies between the two it still errs toward a duplicate rather than a loss, which
is the same trade the rest of this service makes deliberately.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_ENTRIES = 10_000


class SeenStore(Protocol):
    def claim(self, event_id: str) -> bool:
        """Record this event as being handled.

        Returns True if the claim was taken, False if it was already held — in
        which case the caller must not process the event again.
        """

    def release(self, event_id: str) -> None:
        """Give up a claim, so a redelivery may retry.

        Must be safe to call for an id that is not claimed: the endpoint
        releases on its failure paths without checking first.
        """


class InMemorySeenStore:
    """A bounded, expiring set of handled event ids, local to this process.

    **Not a distributed guarantee** — see the module docstring. Bounded because
    Cloud Run gives an instance a fixed memory limit and a map of every event
    ever seen reaches it; expiring because a claim older than the redelivery
    window cannot prevent anything.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        now: Callable[[], float] = time.monotonic,
    ):
        # monotonic, not wall-clock: a clock adjustment must not expire every
        # claim at once or push them an hour into the future.
        self._now = now
        self._ttl = ttl_seconds
        self._max = max_entries
        # Insertion-ordered, so the oldest claim is the one at the front and
        # eviction is O(1) rather than a scan.
        self._claims: OrderedDict[str, float] = OrderedDict()

    def claim(self, event_id: str) -> bool:
        now = self._now()
        expires_at = self._claims.get(event_id)

        if expires_at is not None and expires_at > now:
            return False

        # Either unseen, or seen and expired. Re-claiming an expired id restarts
        # its TTL, and move_to_end keeps insertion order meaning "most recently
        # claimed" so eviction stays honest.
        self._claims[event_id] = now + self._ttl
        self._claims.move_to_end(event_id)
        self._evict(now)
        return True

    def release(self, event_id: str) -> None:
        self._claims.pop(event_id, None)

    def _evict(self, now: float) -> None:
        # Expired entries first — they are free to drop and cost nothing to
        # keep looking at. Only then trim to size.
        while self._claims:
            oldest_id, expires_at = next(iter(self._claims.items()))
            if expires_at > now:
                break
            self._claims.popitem(last=False)

        while len(self._claims) > self._max:
            self._claims.popitem(last=False)

    def __len__(self) -> int:
        return len(self._claims)
