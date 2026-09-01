"""Issue #13 — idempotency, because delivery is at-least-once by construction.

Two independent sources of duplicates:

1. `RetryingPublisher` retries a publish that timed out *after* Pub/Sub stored
   the message, so one webhook becomes two topic messages.
2. Pub/Sub redelivers on every nack and every missed ack deadline.

**When the store is written matters more than which store it is.** Recording
`event_id` *before* handling means a crash mid-handle leaves the event marked
done and never processed — silent loss, the exact failure ingest was built to
avoid. Recording *after* means a crash leaves it unclaimed and it is processed
twice. `claim`/`release` splits the difference: claim before, release on
failure so a redelivery may retry, leave the claim standing on success.
"""

from __future__ import annotations

import pytest

from tests.processor.conftest import (
    EVENT_ID,
    FakeHandler,
    body,
    capture_processor_output,
    emitted,
    push_envelope,
)

PUSH_PATH = "/_pubsub/push"


def store(**kwargs):
    from processor.dedup import InMemorySeenStore

    return InMemorySeenStore(**kwargs)


class Clock:
    """A hand-cranked clock, so the TTL tests do not sleep."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- claim / release ------------------------------------------------------------


def test_a_first_claim_succeeds():
    assert store().claim("event-1") is True


def test_a_second_claim_of_the_same_id_fails():
    seen = store()
    seen.claim("event-1")

    assert seen.claim("event-1") is False


def test_different_ids_do_not_collide():
    seen = store()

    assert seen.claim("event-1") is True
    assert seen.claim("event-2") is True


def test_release_allows_the_id_to_be_claimed_again():
    """This is what lets a redelivery retry after a failure."""
    seen = store()
    seen.claim("event-1")

    seen.release("event-1")

    assert seen.claim("event-1") is True


def test_releasing_something_never_claimed_is_harmless():
    """The endpoint releases on the failure paths without checking, and a
    KeyError there would turn a handler failure into a 500."""
    store().release("never-seen")  # must not raise


def test_release_only_affects_the_id_given():
    seen = store()
    seen.claim("event-1")
    seen.claim("event-2")

    seen.release("event-1")

    assert seen.claim("event-1") is True
    assert seen.claim("event-2") is False


# -- expiry ---------------------------------------------------------------------


def test_a_claim_expires_after_the_ttl():
    """Unbounded retention would be a memory leak on a long-lived instance."""
    clock = Clock()
    seen = store(ttl_seconds=60, now=clock)
    seen.claim("event-1")

    clock.advance(61)

    assert seen.claim("event-1") is True


def test_a_claim_inside_the_ttl_still_blocks():
    clock = Clock()
    seen = store(ttl_seconds=60, now=clock)
    seen.claim("event-1")

    clock.advance(59)

    assert seen.claim("event-1") is False


def test_reclaiming_an_expired_id_restarts_its_ttl():
    clock = Clock()
    seen = store(ttl_seconds=60, now=clock)
    seen.claim("event-1")
    clock.advance(61)
    seen.claim("event-1")

    clock.advance(30)

    assert seen.claim("event-1") is False


# -- bounded size ---------------------------------------------------------------


def test_the_store_is_bounded():
    """Cloud Run gives an instance a fixed memory limit, and an unbounded map
    of every event ever seen reaches it eventually."""
    seen = store(max_entries=3)

    for n in range(5):
        seen.claim(f"event-{n}")

    assert len(seen) <= 3


def test_eviction_removes_the_oldest_first():
    seen = store(max_entries=3)
    for n in range(3):
        seen.claim(f"event-{n}")

    seen.claim("event-3")  # evicts event-0

    assert seen.claim("event-0") is True, "the oldest should have been evicted"


def test_eviction_keeps_the_newest():
    seen = store(max_entries=3)
    for n in range(5):
        seen.claim(f"event-{n}")

    assert seen.claim("event-4") is False, "the newest must still be claimed"


# -- the protocol ----------------------------------------------------------------


def test_the_in_memory_store_satisfies_the_protocol():
    from processor.dedup import InMemorySeenStore, SeenStore

    typed: SeenStore = InMemorySeenStore()

    assert callable(typed.claim)
    assert callable(typed.release)


def test_the_module_says_plainly_that_it_is_not_distributed():
    """This is a template. Someone will deploy it to N instances and assume
    dedup holds across them, and the only thing standing between them and that
    assumption is this docstring — so it is asserted, not merely written."""
    import processor.dedup as dedup

    text = (dedup.__doc__ or "").lower()

    assert "per-instance" in text or "per instance" in text
    assert "firestore" in text or "redis" in text


# -- configuration ---------------------------------------------------------------


def test_the_dedup_bounds_have_defaults():
    from processor.config import Settings

    settings = Settings.from_env({"PUSH_AUTH_MODE": "iam"})

    assert settings.dedup_ttl_seconds == 3600
    assert settings.dedup_max_entries == 10000


def test_the_dedup_bounds_come_from_the_environment():
    from processor.config import Settings

    settings = Settings.from_env(
        {"PUSH_AUTH_MODE": "iam", "DEDUP_TTL_SECONDS": "60", "DEDUP_MAX_ENTRIES": "5"}
    )

    assert settings.dedup_ttl_seconds == 60
    assert settings.dedup_max_entries == 5


@pytest.mark.parametrize("value", ["nonsense", "", "-1", "0", "3.5"])
def test_a_bad_dedup_bound_fails_at_startup(value):
    """Same rule as every other setting: crash the container, do not discover
    it on the first real message."""
    from processor.config import Settings

    with pytest.raises(ValueError):
        Settings.from_env({"PUSH_AUTH_MODE": "iam", "DEDUP_TTL_SECONDS": value})


# -- the endpoint ----------------------------------------------------------------


def post(client, envelope=None):
    return client.post(PUSH_PATH, content=body(envelope or push_envelope()))


def test_a_duplicate_is_acked_without_reaching_the_handler(make_client):
    handler = FakeHandler()
    client = make_client(handler)

    first = post(client)
    second = post(client)

    assert (first.status_code, second.status_code) == (204, 204)
    assert handler.call_count == 1, "the duplicate should not have been handled"


def test_a_duplicate_is_logged_with_its_event_id(make_client):
    client = make_client(FakeHandler())
    post(client)

    with capture_processor_output() as stream:
        post(client)

    assert any(line.get("event_id") == EVENT_ID for line in emitted(stream))


def test_a_distinct_event_is_still_handled(make_client):
    handler = FakeHandler()
    client = make_client(handler)

    post(client)
    post(client, push_envelope(b'{"different":"payload"}', attrs=_attrs("other-event")))

    assert handler.call_count == 2


def _attrs(event_id: str) -> dict[str, str]:
    from tests.processor.conftest import attributes

    return attributes(event_id=event_id)


def test_a_retryable_failure_releases_the_claim(make_client, seen):
    """Otherwise the nack asks for a redelivery that the store then swallows,
    and the event is lost — a nack and a dedup entry contradict each other."""
    from processor.handler import RetryableError

    client = make_client(FakeHandler(raise_with=RetryableError("downstream down")))

    assert post(client).status_code == 503
    assert seen.claim(EVENT_ID) is True, "the claim should have been released"


def test_an_unexpected_failure_releases_the_claim(make_client, seen):
    client = make_client(FakeHandler(raise_with=ValueError("bug")))

    assert post(client).status_code == 503
    assert seen.claim(EVENT_ID) is True


def test_a_permanent_failure_releases_the_claim(make_client, seen):
    from processor.handler import PermanentError

    client = make_client(FakeHandler(raise_with=PermanentError("unknown type")))

    assert post(client).status_code == 204
    assert seen.claim(EVENT_ID) is True


def test_a_success_keeps_the_claim(make_client, seen):
    client = make_client(FakeHandler())

    assert post(client).status_code == 204
    assert seen.claim(EVENT_ID) is False, "a handled event must stay claimed"


def test_a_redelivery_after_a_failure_reaches_the_handler_again(make_client):
    """The whole point of releasing: Pub/Sub was told to send it again, so the
    second attempt must actually run."""
    from processor.handler import RetryableError

    handler = FakeHandler(raise_with=RetryableError("transient"))
    client = make_client(handler)

    post(client)
    handler.raise_with = None
    assert post(client).status_code == 204

    assert handler.call_count == 2


def test_an_unparseable_envelope_claims_nothing(make_client, seen):
    """There is no event_id to claim — the envelope carrying it is what failed
    to parse — so nothing may be recorded."""
    client = make_client(FakeHandler())

    client.post(PUSH_PATH, content=b"not json")

    assert len(seen) == 0


def test_the_store_is_injected_not_constructed():
    """A store built inside create_app could not be swapped for Firestore,
    which is the entire reason it is a protocol."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / "processor" / "app.py").read_text(encoding="utf-8")

    assert "InMemorySeenStore(" not in source
