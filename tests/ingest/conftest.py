"""Fixtures and test doubles for the ingest service.

Scoped to this directory: pytest layers conftest files, so nothing here is
visible to the processor or contract suites. Repo-wide helpers live in
``tests/conftest.py``.

THE CONTRACT THESE TESTS ASSUME
-------------------------------
The suite drives the service through four small seams. Build these and the
tests have something to bind to:

    ingest/config.py
        @dataclass(frozen=True)
        class Settings:
            gcp_project: str
            pubsub_topic: str
            signing_secret: str
            signature_header: str = "X-Signature-256"
            delivery_id_header: str = "X-Delivery-Id"
            max_body_bytes: int = 1_048_576
            source_name: str = "aptly"

            @classmethod
            def from_env(cls, env: Mapping[str, str]) -> "Settings": ...

    ingest/security.py
        def compute_signature(secret: str, body: bytes) -> str      # "sha256=<hex>"
        def verify_signature(secret: str, body: bytes, header: str | None) -> bool

    ingest/publisher.py          <- must NOT import google.cloud
        class PublishError(Exception): ...
        class PublishTimeout(PublishError): ...
        class Publisher(Protocol):
            def publish(self, data: bytes, attributes: Mapping[str, str]) -> str:
                '''Block until the publish is durable. Return the message id.
                Raise PublishError / PublishTimeout on failure.'''

    ingest/app.py
        def create_app(settings: Settings, publisher: Publisher) -> FastAPI

Routes: ``GET /healthz`` (unauthenticated) and ``POST /webhook``.

The concrete Pub/Sub client belongs in a separate module (e.g.
``ingest/pubsub_publisher.py``) that only the entrypoint imports — that is why
``google-cloud-pubsub`` is an optional ``[gcp]`` extra and the suite installs
without it.

WHY THE IMPORTS ARE INSIDE FIXTURES
-----------------------------------
So a missing module fails the individual tests that need it, rather than
erroring out collection for the whole file. You can watch the suite go green
module by module as you build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib

import pytest

from tests.conftest import REPO_ROOT, package_sources

TEST_SECRET = "test-signing-secret-not-a-real-credential"  # pragma: allow-secret

INGEST_DIR = REPO_ROOT / "ingest"


def ingest_sources() -> list[pathlib.Path]:
    """Every .py file in the ingest package, read from disk."""
    return package_sources("ingest")


def sign(body: bytes, secret: str = TEST_SECRET) -> str:
    """Compute the expected signature header value.

    Deliberately implemented here with stdlib rather than by calling
    ``ingest.security.compute_signature``. A test that signs with the same
    function it is testing passes even when that function is wrong.
    """
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def body_bytes(payload) -> bytes:
    """Serialize a payload the way a real sender would put it on the wire."""
    return json.dumps(payload).encode()


class FakePublisher:
    """In-memory stand-in for the Pub/Sub publisher.

    Records every publish so tests can assert both *that* something was
    published and *what* was published. Can be armed to fail, which is how the
    "never acknowledge an event that was not published" tests work.
    """

    def __init__(self, *, fail_with: Exception | None = None, message_id: str = "msg-0001"):
        self.fail_with = fail_with
        self.message_id = message_id
        self.published: list[tuple[bytes, dict[str, str]]] = []

    def publish(self, data: bytes, attributes=None) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.published.append((data, dict(attributes or {})))
        return self.message_id

    # -- convenience accessors -------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.published)

    @property
    def last_data(self) -> bytes:
        assert self.published, "nothing was published"
        return self.published[-1][0]

    @property
    def last_attributes(self) -> dict[str, str]:
        assert self.published, "nothing was published"
        return self.published[-1][1]


@pytest.fixture
def settings():
    from ingest.config import Settings

    return Settings(
        gcp_project="test-project",
        pubsub_topic="test-topic",
        signing_secret=TEST_SECRET,
        signature_header="X-Signature-256",
        delivery_id_header="X-Delivery-Id",
        max_body_bytes=1024,
        source_name="aptly",
    )


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def make_client(settings):
    """Build a TestClient over an app wired to a given publisher."""
    from fastapi.testclient import TestClient
    from ingest.app import create_app

    def _make(pub=None, **setting_overrides):
        import dataclasses

        cfg = dataclasses.replace(settings, **setting_overrides) if setting_overrides else settings
        return TestClient(create_app(settings=cfg, publisher=pub or FakePublisher()),
                          raise_server_exceptions=False)

    return _make


@pytest.fixture
def client(make_client, publisher):
    return make_client(publisher)


@pytest.fixture
def post_signed(client):
    """POST a JSON payload with a correct signature."""

    def _post(payload=None, *, raw: bytes | None = None, headers=None, secret=TEST_SECRET):
        data = raw if raw is not None else body_bytes(payload if payload is not None else {"a": 1})
        hdrs = {
            "Content-Type": "application/json",
            "X-Signature-256": sign(data, secret),
        }
        hdrs.update(headers or {})
        return client.post("/webhook", content=data, headers=hdrs)

    return _post
