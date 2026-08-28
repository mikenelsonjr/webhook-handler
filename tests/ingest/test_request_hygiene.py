"""Issue #5 — method, size, content type, malformed bodies, and CORS."""

from __future__ import annotations

import pytest

from tests.ingest.conftest import FakePublisher, auth_header, body_bytes


def _authed_post(client, raw: bytes, content_type="application/json", **headers):
    hdrs = {"Content-Type": content_type, **auth_header()}
    hdrs.update(headers)
    return client.post("/webhook", content=raw, headers=hdrs)


# --- method ------------------------------------------------------------------

@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_non_post_methods_are_405(client, method):
    r = getattr(client, method)("/webhook")

    assert r.status_code == 405, "a wrong method is not a bad payload"


def test_options_is_not_a_cors_preflight(client):
    """The original answered OPTIONS with 204 and permissive CORS headers.
    A server-to-server webhook has no browser preflight to satisfy."""
    r = client.options("/webhook")

    assert r.status_code == 405
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# --- size --------------------------------------------------------------------

def test_oversized_body_is_413_and_publishes_nothing(make_client):
    pub = FakePublisher()
    client = make_client(pub, max_body_bytes=1024)
    raw = body_bytes({"blob": "x" * 4000})

    r = _authed_post(client, raw)

    assert r.status_code == 413
    assert pub.call_count == 0, "an oversized body must be rejected before publishing"


def test_body_at_the_limit_is_accepted(make_client):
    pub = FakePublisher()
    client = make_client(pub, max_body_bytes=2048)
    raw = body_bytes({"blob": "x" * 100})
    assert len(raw) <= 2048

    assert _authed_post(client, raw).status_code == 202


# --- content type ------------------------------------------------------------

def test_non_json_content_type_is_415(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    r = _authed_post(client, b'{"a": 1}', content_type="text/plain")

    assert r.status_code == 415
    assert pub.call_count == 0


def test_json_content_type_with_charset_is_accepted(make_client):
    """`application/json; charset=utf-8` is a normal thing for senders to emit."""
    pub = FakePublisher()
    client = make_client(pub)

    r = _authed_post(client, b'{"a": 1}', content_type="application/json; charset=utf-8")

    assert r.status_code == 202


# --- payload -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"{not json", b"", b"{'single': 'quotes'}", b"[1,2", b"\x00\x01"])
def test_malformed_json_is_400_and_publishes_nothing(make_client, raw):
    pub = FakePublisher()
    client = make_client(pub)

    r = _authed_post(client, raw)

    assert r.status_code == 400
    assert pub.call_count == 0


@pytest.mark.parametrize("payload", [{}, [], 0, False, "", None])
def test_valid_but_falsy_json_is_accepted(make_client, payload):
    """The original used `if request_json:` and so returned 400 for every one of
    these — all of which are valid JSON documents a sender may legitimately post."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = body_bytes(payload)

    r = _authed_post(client, raw)

    assert r.status_code == 202, f"{payload!r} is valid JSON and must be accepted"
    assert pub.call_count == 1


# --- CORS --------------------------------------------------------------------

def test_no_cors_headers_on_success(post_authed):
    r = post_authed({"event": "ping"})

    assert not [k for k in r.headers if k.lower().startswith("access-control-")]


def test_no_cors_headers_on_rejection(client):
    r = client.post("/webhook", content=b"{}", headers={"Content-Type": "application/json"})

    assert r.status_code == 401
    assert not [k for k in r.headers if k.lower().startswith("access-control-")]


def test_no_cors_headers_on_health(client):
    r = client.get("/healthz")

    assert not [k for k in r.headers if k.lower().startswith("access-control-")]
