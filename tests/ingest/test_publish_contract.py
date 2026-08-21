"""Issue #2 — never acknowledge an event that was not published.

This is the defining property of an ingest service. A webhook sender treats a
2xx as "you own this now" and will not retry. Returning 200 before the publish
is confirmed converts every transient Pub/Sub error into permanent data loss.
"""

from __future__ import annotations

import pytest

from tests.ingest.conftest import FakePublisher, body_bytes, sign


def _post(client, payload=None):
    data = body_bytes(payload if payload is not None else {"event": "ping"})
    return client.post(
        "/webhook",
        content=data,
        headers={"Content-Type": "application/json", "X-Signature-256": sign(data)},
    )


def test_successful_publish_returns_202_with_the_message_id(make_client):
    pub = FakePublisher(message_id="msg-abc-123")
    r = _post(make_client(pub))

    assert r.status_code == 202
    assert r.json()["message_id"] == "msg-abc-123"


def test_publish_error_returns_503(make_client):
    from ingest.publisher import PublishError

    r = _post(make_client(FakePublisher(fail_with=PublishError("topic not found"))))

    assert r.status_code == 503, "the sender must be told to retry"


def test_publish_timeout_returns_503(make_client):
    from ingest.publisher import PublishTimeout

    r = _post(make_client(FakePublisher(fail_with=PublishTimeout("timed out"))))

    assert r.status_code == 503


def test_unexpected_publisher_exception_is_never_a_2xx(make_client):
    """Even an error the service did not anticipate must not be acknowledged."""
    r = _post(make_client(FakePublisher(fail_with=RuntimeError("something unforeseen"))))

    assert not (200 <= r.status_code < 300), (
        f"returned {r.status_code}: the sender will treat this as delivered and never retry"
    )


def test_failure_response_does_not_leak_internal_detail(make_client):
    from ingest.publisher import PublishError

    r = _post(make_client(FakePublisher(fail_with=PublishError("projects/secret-proj/topics/x"))))

    assert "secret-proj" not in r.text


def test_publish_is_called_exactly_once_per_request(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    _post(client)

    assert pub.call_count == 1


def test_no_module_level_state_accumulates_across_requests(make_client):
    """The original kept a global `publish_futures` list that was never cleared,
    so each request re-waited on every future the instance had ever created —
    unbounded memory and latency that grows with uptime."""
    import ingest.app
    import ingest.publisher

    def snapshot():
        return {
            f"{mod.__name__}.{name}": len(value)
            for mod in (ingest.app, ingest.publisher)
            for name, value in vars(mod).items()
            if isinstance(value, (list, set, dict)) and not name.startswith("__")
        }

    client = make_client(FakePublisher())
    before = snapshot()
    for _ in range(5):
        _post(client)
    after = snapshot()

    grew = {k: (before.get(k), v) for k, v in after.items() if before.get(k, 0) != v}
    assert not grew, f"module-level collections grew across requests: {grew}"


@pytest.mark.parametrize("attempt", range(3))
def test_repeated_failures_stay_failures(make_client, attempt):
    """No accidental success on retry from leftover state."""
    from ingest.publisher import PublishError

    r = _post(make_client(FakePublisher(fail_with=PublishError("still down"))))

    assert r.status_code == 503
