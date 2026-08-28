"""The concrete Pub/Sub publisher.

This is the only module that imports ``google.cloud``, which is why
``google-cloud-pubsub`` is an optional ``[gcp]`` extra: the test suite drives
the app through a fake and needs neither the grpc toolchain nor credentials.
Import it from the entrypoint, never from ``app.py``.

Local development points at the emulator instead of real GCP — see
docker-compose.yml. The client picks that up from ``PUBSUB_EMULATOR_HOST``
entirely on its own; nothing here needs to know which one it is talking to.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Mapping

from google.api_core import exceptions as gcp_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import pubsub_v1

from ingest.config import Settings
from ingest.publisher import PublishError, PublishTimeout

logger = logging.getLogger(__name__)

DEFAULT_PUBLISH_TIMEOUT_SECONDS = 10.0


class PubSubPublisher:
    """Publishes to a Pub/Sub topic and blocks until the message is durable.

    Blocking is the point. The `publish()` call on the Google client is
    fire-and-forget — it hands back a future — and the original service ignored
    that future, so a failed publish looked identical to a successful one and
    the sender got a 200 either way. Here the future is resolved before the
    caller is told anything.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        timeout_seconds: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
        client: pubsub_v1.PublisherClient | None = None,
    ):
        # One message per batch with no added latency. The client's default is
        # to wait ~10ms hoping to batch more, which is pure latency for a
        # request that publishes exactly one message and waits for the result.
        self._client = client or pubsub_v1.PublisherClient(
            batch_settings=pubsub_v1.types.BatchSettings(max_messages=1, max_latency=0)
        )
        self._topic_path = self._client.topic_path(settings.gcp_project, settings.pubsub_topic)
        self._timeout_seconds = timeout_seconds

    def publish(self, data: bytes, attributes: Mapping[str, str]) -> str:
        """Publish and block until Pub/Sub confirms the message is stored.

        Returns the Pub/Sub message id. Raises PublishTimeout or PublishError —
        never anything google-specific, so the route stays free of GCP types.
        """
        try:
            future = self._client.publish(self._topic_path, data, **dict(attributes or {}))
            return future.result(timeout=self._timeout_seconds)

        except concurrent.futures.TimeoutError as exc:
            # The message may or may not have landed. Treated as a failure:
            # the caller must not acknowledge something unconfirmed.
            raise PublishTimeout(
                f"publish did not confirm within {self._timeout_seconds}s"
            ) from exc

        except (
            gcp_exceptions.GoogleAPIError,      # covers GoogleAPICallError and RetryError
            auth_exceptions.GoogleAuthError,    # missing/invalid credentials
        ) as exc:
            # The message text can contain the full topic path, so it is logged
            # rather than raised onward into a response body. This is the only
            # line holding the real GCP error, so it carries the event_id too —
            # otherwise the one log line that says *why* a publish failed cannot
            # be tied to the delivery that failed.
            logger.debug(
                "pubsub publish failed",
                exc_info=exc,
                extra={"event_id": dict(attributes or {}).get("event_id", "")},
            )
            raise PublishError(f"{type(exc).__name__} publishing to the topic") from exc

        except ValueError as exc:
            # Raised for a message over the 10MB cap, or a non-str attribute
            # value. A bug on this side, but still a failed publish.
            raise PublishError(f"rejected by the client: {type(exc).__name__}") from exc
