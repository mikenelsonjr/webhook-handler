"""Issue #1 — configuration comes from the environment, not from literals."""

from __future__ import annotations

import pytest

ENV = {
    "GCP_PROJECT": "proj-from-env",
    "PUBSUB_TOPIC": "topic-from-env",
    "WEBHOOK_SIGNING_SECRET": "secret-from-env-value", 
}


def test_from_env_reads_required_values():
    from ingest.config import Settings

    cfg = Settings.from_env(ENV)

    assert cfg.gcp_project == "proj-from-env"
    assert cfg.pubsub_topic == "topic-from-env"
    assert cfg.signing_secret == "secret-from-env-value"


@pytest.mark.parametrize("missing", sorted(ENV))
def test_missing_required_setting_fails_fast(missing):
    """A missing setting must fail at startup, not on the first live request."""
    from ingest.config import Settings

    env = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(Exception) as exc:
        Settings.from_env(env)

    assert missing in str(exc.value), "the error should name the setting that is missing"


def test_defaults_are_applied():
    from ingest.config import Settings

    cfg = Settings.from_env(ENV)

    assert cfg.signature_header == "X-Signature-256"
    assert cfg.max_body_bytes > 0
    assert cfg.max_body_bytes <= 10 * 1024 * 1024, "must not exceed the Pub/Sub 10MB message cap"


def test_overrides_are_read_from_env():
    from ingest.config import Settings

    cfg = Settings.from_env({**ENV, "MAX_BODY_BYTES": "2048", "WEBHOOK_SOURCE": "custom"})

    assert cfg.max_body_bytes == 2048
    assert cfg.source_name == "custom"


def test_no_hardcoded_project_or_topic_survives():
    """The original handler hardcoded these. They must not appear in the package."""
    from tests.ingest.conftest import ingest_sources

    offenders = [
        f"{path.name}:{n}"
        for path in ingest_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "project-roundtable" in line or "aptly-messages" in line
    ]

    assert not offenders, f"hardcoded project/topic still present at {offenders}"
