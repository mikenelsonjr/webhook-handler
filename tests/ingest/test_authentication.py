"""Issue #3 — inbound requests must be authenticated before anything else happens.

Aptly sends a static token in an ``x-signingkey`` header rather than an HMAC
signature over the body, so verification is a constant-time comparison of that
token against the configured secret.

The property under test is not "a valid key is accepted" but the harder one:
*nothing reaches the topic without a valid key*. Every rejection case therefore
asserts ``publisher.call_count == 0`` as well as the status code.
"""

from __future__ import annotations

import inspect

import pytest

from tests.ingest.conftest import (
    SIGNING_KEY_HEADER,
    TEST_SIGNING_KEY,
    FakePublisher,
    auth_header,
    body_bytes,
    json_headers,
)


def test_valid_key_is_accepted(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=body_bytes({"action": "update"}), headers=json_headers())

    assert r.status_code == 202
    assert pub.call_count == 1


def test_missing_key_header_is_rejected(make_client):
    """The original bug: `if signature:` made verification opt-in for the caller."""
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post(
        "/webhook",
        content=body_bytes({"action": "update"}),
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 401
    assert pub.call_count == 0, "an unauthenticated request must never reach the topic"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "wrong-key",
        TEST_SIGNING_KEY + "x",          # right prefix, extra char
        TEST_SIGNING_KEY[:-1],           # one char short
        TEST_SIGNING_KEY.upper(),        # comparison must be case-sensitive
        " " + TEST_SIGNING_KEY,          # leading whitespace is not the key
        "x" * 10_000,                    # oversized value must not be special
        # Non-ASCII is deliberately absent: HTTP headers are latin-1 on the
        # wire and the test client refuses to encode it, so it cannot be
        # exercised here. uvicorn *can* hand a non-ASCII str to the app from a
        # raw socket, so it is covered directly in
        # test_verify_signing_key_returns_false_rather_than_raising.
    ],
)
def test_wrong_key_is_rejected(make_client, key):
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers(key))

    assert r.status_code == 401, f"{key!r} should be a clean 401, not a 500"
    assert pub.call_count == 0


def test_authentication_precedes_content_type_validation(make_client):
    """An unauthenticated caller learns nothing about the request's other faults."""
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=b"not json at all", headers={"Content-Type": "text/plain"})

    assert r.status_code == 401
    assert pub.call_count == 0


def test_service_fails_closed_when_no_secret_is_configured(make_client):
    """An unset secret must never mean 'accept everything' — and specifically
    must not authenticate a caller who sends an empty header to match it."""
    pub = FakePublisher()

    try:
        client = make_client(pub, signing_secret="")
    except Exception:
        return  # refusing to construct the app is an acceptable way to fail closed

    for key in ("", "anything"):
        r = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers(key))
        assert r.status_code >= 400, "an unconfigured secret must not authenticate anyone"

    assert pub.call_count == 0


def test_header_name_comes_from_settings(make_client):
    """Providers differ: Aptly uses x-signingkey, others use their own header.
    The name is a setting, not a literal in the route."""
    pub = FakePublisher()
    client = make_client(pub, signing_key_header="X-Custom-Auth")

    rejected = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers())
    assert rejected.status_code == 401, "the default header must no longer be honoured"

    accepted = client.post(
        "/webhook",
        content=body_bytes({"a": 1}),
        headers={"Content-Type": "application/json", "X-Custom-Auth": TEST_SIGNING_KEY},
    )
    assert accepted.status_code == 202


def test_header_matching_is_case_insensitive(make_client):
    """HTTP header names are case-insensitive; Aptly sends `x-signingkey` lower."""
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post(
        "/webhook",
        content=body_bytes({"a": 1}),
        headers={"Content-Type": "application/json", "x-signingkey": TEST_SIGNING_KEY},
    )

    assert r.status_code == 202


def test_signing_key_is_never_published(make_client):
    """The credential must not travel with the message it authenticates.
    Publishing `dict(request.headers)` puts it on the topic for every
    subscriber, dead-letter queue, and log of that topic to read."""
    pub = FakePublisher()
    client = make_client(pub)

    client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers())

    flat = " ".join(f"{k}={v}" for k, v in pub.last_attributes.items()).lower()
    assert TEST_SIGNING_KEY.lower() not in flat, "the signing key leaked into message attributes"
    assert SIGNING_KEY_HEADER.lower() not in flat


def test_signing_key_is_never_echoed_to_the_caller(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=body_bytes({"a": 1}), headers=json_headers())

    assert TEST_SIGNING_KEY not in r.text


def test_comparison_is_constant_time():
    """`==` on a secret leaks its prefix through timing: an attacker measures
    response time to learn how many leading characters they guessed right."""
    from ingest import security

    source = inspect.getsource(security)

    assert "compare_digest" in source, "verify_signing_key must use hmac.compare_digest"


def test_verify_signing_key_returns_false_rather_than_raising():
    """A malformed header is a 401, not a 500. Anyone can send arbitrary bytes
    in a header, so a crash here is a denial-of-service anyone can trigger."""
    from ingest.security import verify_signing_key

    for provided in (None, "", "wrong", "café", "\x00\x01", "x" * 10_000):
        assert verify_signing_key(TEST_SIGNING_KEY, provided) is False

    assert verify_signing_key(TEST_SIGNING_KEY, TEST_SIGNING_KEY) is True


def test_verify_signing_key_fails_closed_on_empty_expected():
    from ingest.security import verify_signing_key

    assert verify_signing_key("", "") is False
    assert verify_signing_key("", "anything") is False


def test_healthz_needs_no_key(client):
    """Cloud Run's health probe cannot send credentials."""
    assert client.get("/healthz").status_code == 200


def test_auth_header_helper_matches_the_configured_header(settings):
    """Guards against the test helper and the app drifting apart."""
    assert SIGNING_KEY_HEADER in auth_header()
    assert settings.signing_key_header == SIGNING_KEY_HEADER
