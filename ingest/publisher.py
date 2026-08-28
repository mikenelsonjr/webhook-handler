from collections.abc import Mapping
from typing import Protocol


class Publisher(Protocol):

    def publish(self, data: bytes, attributes: Mapping[str, str]) -> str:
        """Publish and BLOCK until the message is durably stored.

        Returns the Pub/Sub message id. Raises PublishError or PublishTimeout
        if the message was not stored — the caller must NOT acknowledge the
        webhook in that case.
        """

class PublishError(Exception):
    """Raised when a publish fails."""

class PublishTimeout(PublishError):
    """Raised when a publish times out."""