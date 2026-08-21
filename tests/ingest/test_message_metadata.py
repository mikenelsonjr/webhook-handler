"""Issue #7 — what actually goes onto the topic.

Downstream consumers cannot deduplicate what the publisher does not label, and
they cannot verify what the publisher rewrote.
"""

from __future__ import annotations

import datetime as dt

from tests.ingest.conftest import FakePublisher, body_bytes, sign


def _post(client, raw: bytes, **headers):
    hdrs = {"Content-Type": "application/json", "X-Signature-256": sign(raw)}
    hdrs.update(headers)
    return client.post("/webhook", content=raw, headers=hdrs)


def test_raw_body_is_published_byte_for_byte(make_client):
    """The original round-tripped through `json.dumps(request.get_json())`, which
    reorders keys and drops whitespace. Consumers that verify the sender's own
    signature downstream then fail, and the bytes no longer match what arrived."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = b'{"z": 1,   "a": {"nested":  true},\n "unicode": "caf\\u00e9"}'

    _post(client, raw)

    assert pub.last_data == raw


def test_event_id_uses_the_sender_delivery_id_when_present(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}), **{"X-Delivery-Id": "delivery-9f3c"})

    assert pub.last_attributes["event_id"] == "delivery-9f3c"


def test_event_id_is_deterministic_when_the_sender_supplies_none(make_client):
    """A retried delivery of the same bytes must carry the same id, or dedup
    downstream is impossible."""
    pub = FakePublisher()
    client = make_client(pub)
    raw = body_bytes({"event": "ping", "seq": 7})

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


def test_message_carries_source_and_received_at(make_client):
    pub = FakePublisher()
    client = make_client(pub, source_name="aptly")

    _post(client, body_bytes({"a": 1}))
    attrs = pub.last_attributes

    assert attrs["source"] == "aptly"
    assert "received_at" in attrs


def test_received_at_is_iso8601_utc(make_client):
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}))
    parsed = dt.datetime.fromisoformat(pub.last_attributes["received_at"])

    assert parsed.tzinfo is not None, "received_at must be timezone-aware"


def test_all_attribute_values_are_strings(make_client):
    """Pub/Sub attributes are a map<string,string>; a non-str value raises at
    publish time, in production, on a payload shape you did not test."""
    pub = FakePublisher()
    client = make_client(pub)

    _post(client, body_bytes({"a": 1}))

    bad = {k: type(v).__name__ for k, v in pub.last_attributes.items() if not isinstance(v, str)}
    assert not bad, f"non-string attribute values: {bad}"


def test_response_echoes_the_event_id(make_client):
    """So the sender can correlate its delivery with what landed on the topic."""
    pub = FakePublisher()
    client = make_client(pub)

    r = _post(client, body_bytes({"a": 1}), **{"X-Delivery-Id": "delivery-42"})

    assert r.json()["event_id"] == "delivery-42"
