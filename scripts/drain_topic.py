"""Print whatever the ingest service published, for local verification.

    docker compose run --rm pull

Creates a throwaway subscription on the topic, pulls what is there, prints the
raw bytes and attributes of each message, and exits. Local emulator only — it
refuses to run against real GCP so a stray invocation cannot drain a production
subscription.
"""

from __future__ import annotations

import os
import sys

from google.api_core import exceptions
from google.cloud import pubsub_v1

PROJECT = os.environ.get("GCP_PROJECT", "local-project")
TOPIC = os.environ.get("PUBSUB_TOPIC", "webhook-events")
SUBSCRIPTION = "local-drain"


def main() -> int:
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        print("refusing to run: PUBSUB_EMULATOR_HOST is unset, so this would "
              "hit real Pub/Sub.", file=sys.stderr)
        return 2

    subscriber = pubsub_v1.SubscriberClient()
    topic_path = subscriber.topic_path(PROJECT, TOPIC)
    sub_path = subscriber.subscription_path(PROJECT, SUBSCRIPTION)

    try:
        subscriber.create_subscription(name=sub_path, topic=topic_path)
        print(f"created subscription {SUBSCRIPTION}")
    except exceptions.AlreadyExists:
        pass

    response = subscriber.pull(subscription=sub_path, max_messages=50, timeout=10)
    if not response.received_messages:
        print("topic is empty — post a webhook first")
        return 0

    for received in response.received_messages:
        message = received.message
        print("-" * 72)
        print(f"message_id : {message.message_id}")
        print("attributes :")
        for key, value in sorted(message.attributes.items()):
            print(f"    {key:14} {value}")
        print(f"data ({len(message.data)} bytes):")
        print(f"    {message.data.decode('utf-8', errors='replace')}")

    subscriber.acknowledge(
        subscription=sub_path,
        ack_ids=[r.ack_id for r in response.received_messages],
    )
    print("-" * 72)
    print(f"{len(response.received_messages)} message(s), acknowledged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
