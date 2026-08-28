"""Issue #7 — what actually goes onto the topic.

Aptly sends no delivery-id header, so `event_id` is always a deterministic hash
of the raw body. Downstream consumers cannot deduplicate what the publisher
does not label, and cannot verify what the publisher rewrote.
"""

from __future__ import annotations

import datetime as dt

from tests.ingest.conftest import FakePublisher, body_bytes, json_headers

# A trimmed but structurally faithful Aptly card-update payload.
APTLY_PAYLOAD = {
    "action": "update",
    "fields": [{"key": "name", "label": "Title"}, {"key": "stage", "label": "Stage"}],
    "data": {
        "_id": "MbsuCATkjcaFrMqgX",
        "companyId": "GAu5bZR9uPiBskbXs",
        "name": "From Name in Aptly",
        "stage": "NEW",
        "viewedAt": "2026-08-28T13:41:11.166Z",
    },
    "changes": [{"field": "T8G27FGGdvQ2LsXYP", "value": "e8CFbTzci44oubGZ7"}],
}


def _post(client, raw: bytes, **headers):
    return client.post("/webhook", content=raw, headers={**json_headers(), **headers})


def test_raw_body_is_published_byte_for_byte(make_client):
    """The original round-tripped through `json.dumps(request.get_json())`,
    which reorders keys and drops whitespace. Consumers comparing against what
    Aptly actually sent would see something different from what arrived."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = b'{"z": 1,   "a": {"nested":  true},\n "unicode": "caf\\u00e9"}'

    _post(client, raw)

    assert pub.last_data == raw


def test_realistic_aptly_payload_is_published_unchanged(make_client):
    pub = FakePublisher()
    client = make_client(pub, max_body_bytes=1024 * 1024)
    raw = body_bytes(APTLY_PAYLOAD)

    r = _post(client, raw)

    assert r.status_code == 202
    assert pub.last_data == raw


def test_event_id_is_deterministic_for_the_same_body(make_client):
    """A retried delivery of the same bytes must carry the same id, or dedup
    downstream is impossible."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = body_bytes(APTLY_PAYLOAD)

    _post(client, raw)
    first = pub.last_attributes["event_id"]
    _post(client, raw)
    second = pub.last_attributes["event_id"]

    assert first == second and first


def test_different_bodies_get_different_event_ids(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"seq": 1}))
    first = pub.last_attributes["event_id"]
    _post(client, body_bytes({"seq": 2}))
    second = pub.last_attributes["event_id"]

    assert first != second


def test_event_id_does_not_depend_on_headers(make_client):
    """Only the body decides the id. If a per-request header leaked into it,
    two deliveries of one event would get different ids and dedup would fail."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = body_bytes(APTLY_PAYLOAD)

    _post(client, raw)
    first = pub.last_attributes["event_id"]
    _post(client, raw, **{"User-Agent": "something-else", "Accept": "*/*"})
    second = pub.last_attributes["event_id"]

    assert first == second


def test_message_carries_source_and_received_at(make_client):
    pub = FakePublisher()
    client = make_client(pub, source_name="aptly")

    _post(client, body_bytes({"a": 1}))
    attrs = pub.last_attributes

    assert attrs["source"] == "aptly"
    assert "received_at" in attrs


def test_received_at_is_iso8601_utc(make_client):
    """A naive timestamp from a Cloud Run container is unreadable later — you
    cannot tell what zone it meant."""
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}))
    parsed = dt.datetime.fromisoformat(pub.last_attributes["received_at"])

    assert parsed.tzinfo is not None, "received_at must be timezone-aware"


def test_received_at_varies_between_deliveries(make_client):
    """event_id identifies the event; received_at identifies this attempt."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = body_bytes(APTLY_PAYLOAD)

    _post(client, raw)
    _post(client, raw)

    ids = {a["event_id"] for _, a in pub.published}
    stamps = {a["received_at"] for _, a in pub.published}
    assert len(ids) == 1, "the same event keeps one id"
    assert len(stamps) == 2, "each delivery attempt gets its own timestamp"


def test_attributes_are_a_closed_set(make_client):
    """Publishing `dict(request.headers)` sent the caller's Authorization,
    Cookie and signing key to the topic. Attributes are chosen, not forwarded."""
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}), **{"Cookie": "session=abc123", "X-Whatever": "leak-me"})

    assert set(pub.last_attributes) == {"event_id", "source", "received_at"}


def test_all_attribute_values_are_strings(make_client):
    """Pub/Sub attributes are a map<string,string>; a non-str value raises at
    publish time, in production, on a payload shape you did not test."""
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}))

    bad = {k: type(v).__name__ for k, v in pub.last_attributes.items() if not isinstance(v, str)}
    assert not bad, f"non-string attribute values: {bad}"


def test_response_echoes_the_event_id(make_client):
    """So a delivery can be correlated with what landed on the topic."""
    pub = FakePublisher()
    client = make_client(pub)

    r = _post(client, body_bytes(APTLY_PAYLOAD))

    assert r.json()["event_id"] == pub.last_attributes["event_id"]
