
from dataclasses import dataclass
from collections.abc import Mapping

# from numpy import require

@dataclass(frozen=True)
class Settings:
    gcp_project: str
    pubsub_topic: str
    signing_secret: str
    signature_header: str = "X-Signature-256"
    delivery_id_header: str = "X-Delivery-Id"
    max_body_bytes: int = 1024 * 1024  # 1MB default, can be overridden by env
    source_name: str = "default"
    
    
    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":

        def require(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ValueError(f"Missing required environment variable: {key}")
            return value

        return cls(
            gcp_project=require("GCP_PROJECT"),
            pubsub_topic=require("PUBSUB_TOPIC"),
            signing_secret=require("WEBHOOK_SIGNING_SECRET"),
            max_body_bytes=int(env.get("MAX_BODY_BYTES", 1024 * 1024)),
            source_name=env.get("WEBHOOK_SOURCE", "aptly"),
            signature_header=env.get("WEBHOOK_SIGNATURE_HEADER", "X-Signature-256"),
            delivery_id_header=env.get("WEBHOOK_DELIVERY_ID_HEADER", "X-Delivery-Id"),
        )