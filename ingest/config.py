"""Service configuration, built from a mapping and validated once at startup.

``from_env`` is a pure function of the mapping it is handed: no ``os.environ``,
no ``.env`` loading, no import-time I/O. Only the entrypoint reads the ambient
environment. That is what lets the tests pass an explicit dict, and it keeps a
developer's local ``.env`` from leaking into a test run. See CONTEXT.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable, fully-populated configuration.

    By the time one of these exists, every field is present and valid — so no
    caller downstream ever has to check for missing config.
    """

    gcp_project: str
    pubsub_topic: str
    signing_secret: str
    signing_key_header: str = "X-SigningKey"
    max_body_bytes: int = 1024 * 1024
    source_name: str = "aptly"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Build from environment-style values; raise if anything required is absent.

        Defaults come from the field declarations above rather than repeated
        literals, so the two ways into this class cannot disagree.
        """

        def require(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ValueError(f"Missing required environment variable: {key}")
            return value

        return cls(
            gcp_project=require("GCP_PROJECT"),
            pubsub_topic=require("PUBSUB_TOPIC"),
            signing_secret=require("WEBHOOK_SIGNING_SECRET"),
            signing_key_header=env.get("WEBHOOK_SIGNING_KEY_HEADER", cls.signing_key_header),
            max_body_bytes=int(env.get("MAX_BODY_BYTES", cls.max_body_bytes)),
            source_name=env.get("WEBHOOK_SOURCE", cls.source_name),
        )
