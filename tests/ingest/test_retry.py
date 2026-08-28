"""Bounded publish retry.

Aptly delivers once and ignores the response, so a 503 does not earn a retry
from the sender. These tests pin the modest amount of durability the service
provides on its own — and, just as importantly, its limits.
"""

from __future__ import annotations

import pytest

from tests.ingest.conftest import FakePublisher, body_bytes, json_headers


class FlakyPublisher:
    """Fails the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int, error: Exception | None = None):
        from ingest.publisher import PublishError

        self.fail_times = fail_times
        self.error = error or PublishError("transient")
        self.calls = 0

    def publish(self, data: bytes, attributes=None) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return "msg-after-retry"


class SleepSpy:
    """Stands in for time.sleep so the suite stays fast and deterministic."""

    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _wrap(inner, **kwargs):
    from ingest.retry import RetryingPublisher

    kwargs.setdefault("sleep", SleepSpy())
    return RetryingPublisher(inner, **kwargs)


def test_success_on_first_attempt_does_not_sleep():
    sleep = SleepSpy()
    inner = FakePublisher(message_id="msg-1")

    result = _wrap(inner, sleep=sleep).publish(b"{}", {"event_id": "e"})

    assert result == "msg-1"
    assert inner.call_count == 1
    assert sleep.delays == [], "a successful publish must not add latency"


def test_transient_failure_is_retried_and_succeeds():
    sleep = SleepSpy()
    inner = FlakyPublisher(fail_times=1)

    result = _wrap(inner, attempts=3, sleep=sleep).publish(b"{}", {"event_id": "e"})

    assert result == "msg-after-retry"
    assert inner.calls == 2
    assert len(sleep.delays) == 1


def test_timeout_is_retried_too():
    """PublishTimeout subclasses PublishError precisely so both are handled."""
    from ingest.publisher import PublishTimeout

    inner = FlakyPublisher(fail_times=1, error=PublishTimeout("slow"))

    assert _wrap(inner, attempts=2).publish(b"{}", {}) == "msg-after-retry"
    assert inner.calls == 2


def test_gives_up_after_the_configured_attempts_and_reraises():
    from ingest.publisher import PublishError

    sleep = SleepSpy()
    inner = FlakyPublisher(fail_times=99)

    with pytest.raises(PublishError):
        _wrap(inner, attempts=3, sleep=sleep).publish(b"{}", {})

    assert inner.calls == 3, "bounded: it must not retry forever"
    assert len(sleep.delays) == 2, "no sleep after the final attempt"


def test_backoff_is_exponential():
    from ingest.publisher import PublishError

    sleep = SleepSpy()
    inner = FlakyPublisher(fail_times=99)

    with pytest.raises(PublishError):
        _wrap(inner, attempts=4, backoff_seconds=0.1, sleep=sleep).publish(b"{}", {})

    assert sleep.delays == pytest.approx([0.1, 0.2, 0.4])


def test_non_publish_errors_are_not_retried():
    """A ValueError is a bug, not a transient fault. Retrying it just delays
    the failure and buries the cause."""
    sleep = SleepSpy()

    class Broken:
        calls = 0

        def publish(self, data, attributes=None):
            Broken.calls += 1
            raise ValueError("programming error")

    with pytest.raises(ValueError):
        _wrap(Broken(), attempts=3, sleep=sleep).publish(b"{}", {})

    assert Broken.calls == 1
    assert sleep.delays == []


def test_attempts_of_one_disables_retrying():
    """The off switch: a single attempt behaves exactly like no wrapper."""
    from ingest.publisher import PublishError

    sleep = SleepSpy()
    inner = FlakyPublisher(fail_times=99)

    with pytest.raises(PublishError):
        _wrap(inner, attempts=1, sleep=sleep).publish(b"{}", {})

    assert inner.calls == 1
    assert sleep.delays == []


def test_zero_attempts_is_rejected():
    from ingest.retry import RetryingPublisher

    with pytest.raises(ValueError):
        RetryingPublisher(FakePublisher(), attempts=0)


def test_it_satisfies_the_publisher_protocol_end_to_end(make_client):
    """The wrapper drops in wherever a Publisher goes, without the route
    knowing retries happen."""
    inner = FlakyPublisher(fail_times=1)
    client = make_client(_wrap(inner, attempts=3))

    r = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers())

    assert r.status_code == 202
    assert r.json()["message_id"] == "msg-after-retry"
    assert inner.calls == 2


def test_exhausted_retries_still_return_503(make_client):
    """Retrying changes how often the caller sees a failure, never what a
    failure means: a 2xx is still only sent for a durable message."""
    inner = FlakyPublisher(fail_times=99)
    client = make_client(_wrap(inner, attempts=2))

    r = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers())

    assert r.status_code == 503
    assert inner.calls == 2
