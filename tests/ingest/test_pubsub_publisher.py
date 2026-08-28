"""The concrete Pub/Sub publisher.

Skipped unless the optional ``[gcp]`` extra is installed, so a developer can
run the suite without the grpc toolchain. CI installs it, so these do run there.

No network: ``PubSubPublisher`` accepts a client, and these drive a fake one.
The behaviour under test is the translation layer — blocking on the future, and
turning every google-specific failure into the two exceptions the route knows.
"""

from __future__ import annotations

import concurrent.futures

import pytest

pytest.importorskip("google.cloud.pubsub_v1", reason="requires the [gcp] extra")

from google.api_core import exceptions as gcp_exceptions  # noqa: E402
from google.auth import exceptions as auth_exceptions  # noqa: E402


class FakeFuture:
    def __init__(self, *, result: str | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.timeout_used: float | None = None

    def result(self, timeout=None):
        self.timeout_used = timeout
        if self._error is not None:
            raise self._error
        return self._result


class FakeClient:
    def __init__(self, future: FakeFuture):
        self.future = future
        self.calls: list[tuple[str, bytes, dict]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs):
        self.calls.append((topic, data, attrs))
        return self.future


def _publisher(future: FakeFuture, settings, **kwargs):
    from ingest.pubsub_publisher import PubSubPublisher

    client = FakeClient(future)
    return PubSubPublisher(settings, client=client, **kwargs), client


def test_returns_the_message_id_on_success(settings):
    pub, client = _publisher(FakeFuture(result="msg-123"), settings)

    assert pub.publish(b'{"a":1}', {"event_id": "e1"}) == "msg-123"
    assert len(client.calls) == 1


def test_it_blocks_on_the_future(settings):
    """The original never resolved the future, so a failed publish looked
    exactly like a successful one and the sender got a 200 regardless."""
    future = FakeFuture(result="msg-1")
    pub, _ = _publisher(future, settings, timeout_seconds=7.5)

    pub.publish(b"{}", {})

    assert future.timeout_used == 7.5, "result() must be called, and with the timeout"


def test_topic_path_comes_from_settings(settings):
    pub, client = _publisher(FakeFuture(result="m"), settings)

    pub.publish(b"{}", {})
    topic, _, _ = client.calls[0]

    assert topic == f"projects/{settings.gcp_project}/topics/{settings.pubsub_topic}"


def test_attributes_are_passed_through(settings):
    pub, client = _publisher(FakeFuture(result="m"), settings)
    attrs = {"event_id": "e1", "source": "aptly", "received_at": "2026-08-28T00:00:00+00:00"}

    pub.publish(b"{}", attrs)
    _, _, sent = client.calls[0]

    assert sent == attrs


def test_body_is_passed_through_unmodified(settings):
    pub, client = _publisher(FakeFuture(result="m"), settings)
    raw = b'{"z": 1,   "a": 2}'

    pub.publish(raw, {})
    _, data, _ = client.calls[0]

    assert data == raw


def test_timeout_becomes_publish_timeout(settings):
    from ingest.publisher import PublishTimeout

    pub, _ = _publisher(FakeFuture(error=concurrent.futures.TimeoutError()), settings)

    with pytest.raises(PublishTimeout):
        pub.publish(b"{}", {})


@pytest.mark.parametrize(
    "error",
    [
        gcp_exceptions.NotFound("topic does not exist"),
        gcp_exceptions.PermissionDenied("no publisher role"),
        gcp_exceptions.ServiceUnavailable("backend down"),
        gcp_exceptions.TooManyRequests("quota"),
        gcp_exceptions.RetryError("gave up", cause=None),
        auth_exceptions.DefaultCredentialsError("no credentials"),
        ValueError("message exceeds 10MB"),
    ],
)
def test_every_client_failure_becomes_a_publish_error(settings, error):
    """The route knows PublishError and nothing else. A google exception
    escaping this class would surface as a 500 instead of a 503."""
    from ingest.publisher import PublishError

    pub, _ = _publisher(FakeFuture(error=error), settings)

    with pytest.raises(PublishError):
        pub.publish(b"{}", {})


def test_no_google_exception_escapes(settings):
    """Same property, stated as the thing that actually matters."""
    pub, _ = _publisher(FakeFuture(error=gcp_exceptions.NotFound("gone")), settings)

    try:
        pub.publish(b"{}", {})
    except Exception as exc:
        assert not type(exc).__module__.startswith("google"), (
            f"{type(exc).__name__} leaked out of the publisher"
        )


def test_failure_message_does_not_repeat_the_topic_path(settings):
    """Google's message text contains projects/<project>/topics/<name>. The
    route puts nothing from the exception into a response, but keeping the
    project name out of the exception itself is one less way to leak it."""
    from ingest.publisher import PublishError

    pub, _ = _publisher(
        FakeFuture(error=gcp_exceptions.NotFound(f"projects/{settings.gcp_project}/topics/x")),
        settings,
    )

    with pytest.raises(PublishError) as exc:
        pub.publish(b"{}", {})

    assert settings.gcp_project not in str(exc.value)


def test_the_debug_line_carries_the_event_id(settings):
    """The GCP error text is logged here and deliberately kept out of the
    response body — so this is the only line that says *why* a publish failed,
    and it is worthless if you cannot tie it to a delivery. Issue #9."""
    import io
    import json
    import logging

    from common.log import JsonFormatter
    from ingest.publisher import PublishError

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("ingest.pubsub_publisher")
    logger.addHandler(handler)
    # setLevel, NOT `logger.level = ...`. isEnabledFor() memoizes its answer in
    # Logger._cache, and only setLevel() invalidates it. Earlier tests in this
    # file already logged at DEBUG while the level was WARNING, so a direct
    # assignment leaves the cached False in place and the record is dropped.
    previous = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        pub, _ = _publisher(
            FakeFuture(error=gcp_exceptions.NotFound('topic "webhook-events" is gone')),
            settings,
        )
        with pytest.raises(PublishError):
            pub.publish(b'{"a":1}', {"event_id": "e-42"})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "the GCP failure should be logged at DEBUG"
    assert all(line.get("event_id") == "e-42" for line in lines)
    assert any("NotFound" in line.get("exception", "") for line in lines)


def test_it_satisfies_the_publisher_protocol(settings):
    """Structural check: it can stand in wherever the route expects a Publisher."""
    from ingest.publisher import Publisher

    pub, _ = _publisher(FakeFuture(result="m"), settings)
    typed: Publisher = pub

    assert typed.publish(b"{}", {}) == "m"


def test_it_composes_with_the_retry_wrapper(settings):
    from ingest.retry import RetryingPublisher

    pub, client = _publisher(FakeFuture(result="msg-9"), settings)

    assert RetryingPublisher(pub, attempts=2, sleep=lambda _: None).publish(b"{}", {}) == "msg-9"
    assert len(client.calls) == 1
