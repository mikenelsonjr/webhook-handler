"""Issue #3 — inbound requests must be authenticated before anything else happens.

The property under test is not "a valid signature is accepted" but the harder
one: *nothing reaches the topic without a valid signature*. Every rejection
case therefore asserts `publisher.call_count == 0` as well as the status code.
"""

from __future__ import annotations

import inspect

import pytest

from tests.ingest.conftest import TEST_SECRET, FakePublisher, body_bytes, sign


def test_valid_signature_is_accepted(make_client):
    pub = FakePublisher()
    client = make_client(pub)
    data = body_bytes({"event": "ping"})

    r = client.post(
        "/webhook",
        content=data,
        headers={"Content-Type": "application/json", "X-Signature-256": sign(data)},
    )

    assert r.status_code == 202
    assert pub.call_count == 1


def test_missing_signature_is_rejected(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=body_bytes({"event": "ping"}),
                    headers={"Content-Type": "application/json"})

    assert r.status_code == 401
    assert pub.call_count == 0, "an unsigned request must never reach the topic"


def test_wrong_signature_is_rejected(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post(
        "/webhook",
        content=body_bytes({"event": "ping"}),
        headers={"Content-Type": "application/json", "X-Signature-256": "sha256=" + "0" * 64},
    )

    assert r.status_code == 401
    assert pub.call_count == 0


def test_signature_from_a_different_secret_is_rejected(make_client):
    pub = FakePublisher()
    client = make_client(pub)
    data = body_bytes({"event": "ping"})

    r = client.post(
        "/webhook",
        content=data,
        headers={
            "Content-Type": "application/json",
            "X-Signature-256": sign(data, secret="an-attackers-guess"),  # pragma: allow-secret
        },
    )

    assert r.status_code == 401
    assert pub.call_count == 0


def test_signature_covers_the_body(make_client):
    """A signature valid for one body must not authenticate a different one."""
    pub = FakePublisher()
    client = make_client(pub)
    signature_for_other_body = sign(body_bytes({"event": "harmless"}))

    r = client.post(
        "/webhook",
        content=body_bytes({"event": "malicious"}),
        headers={"Content-Type": "application/json", "X-Signature-256": signature_for_other_body},
    )

    assert r.status_code == 401
    assert pub.call_count == 0


def test_signature_is_computed_over_raw_bytes_not_reserialized_json(make_client):
    """`json.dumps(json.loads(body))` changes the bytes, so a signature checked
    against re-serialized JSON rejects legitimate senders. Whitespace and key
    order must survive."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = b'{"b"  :  2,\n  "a": 1}'  # valid JSON, not what json.dumps would emit

    r = client.post(
        "/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature-256": sign(raw)},
    )

    assert r.status_code == 202
    assert pub.call_count == 1


def test_authentication_precedes_content_type_validation(make_client):
    """Unauthenticated callers learn nothing about the request's other faults."""
    pub = FakePublisher()
    client = make_client(pub)

    r = client.post("/webhook", content=b"not json at all",
                    headers={"Content-Type": "text/plain"})

    assert r.status_code == 401
    assert pub.call_count == 0


def test_service_fails_closed_when_no_secret_is_configured(make_client):
    """An empty secret must never mean 'accept everything'."""
    pub = FakePublisher()

    try:
        client = make_client(pub, signing_secret="")
    except Exception:
        return  # refusing to construct the app is an acceptable way to fail closed

    data = body_bytes({"event": "ping"})
    r = client.post("/webhook", content=data,
                    headers={"Content-Type": "application/json", "X-Signature-256": sign(data, "")})

    assert r.status_code >= 400, "an unconfigured secret must not authenticate anyone"
    assert pub.call_count == 0


def test_comparison_is_constant_time():
    """`==` on a digest leaks its prefix through timing. Use hmac.compare_digest."""
    from ingest import security

    source = inspect.getsource(security)

    assert "compare_digest" in source, "verify_signature must use hmac.compare_digest"


def test_compute_signature_matches_an_independent_implementation():
    from ingest.security import compute_signature

    body = b'{"event":"ping"}'

    assert compute_signature(TEST_SECRET, body) == sign(body)


@pytest.mark.parametrize(
    "header",
    ["", "sha256=", "garbage", "sha256=nothex", "md5=" + "0" * 32, "sha256=" + "0" * 63],
)
def test_malformed_signature_headers_are_rejected_not_crashed(make_client, header):
    pub = FakePublisher()
    client = make_client(pub)
    data = body_bytes({"event": "ping"})

    r = client.post("/webhook", content=data,
                    headers={"Content-Type": "application/json", "X-Signature-256": header})

    assert r.status_code == 401, f"{header!r} should be a clean 401, not a 500"
    assert pub.call_count == 0
