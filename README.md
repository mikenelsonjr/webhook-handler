# Webhook Handler

[![CI](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml/badge.svg)](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml)

A Python webhook ingest service for Cloud Run that authenticates inbound
deliveries and publishes them to a Pub/Sub topic.

The pattern it implements: **accept fast, process later.** The handler does the
minimum synchronous work — authenticate, publish, acknowledge — and returns.
Everything expensive happens downstream in a subscriber, so a slow consumer can
never make the sender time out.

One rule drives the design: **a 2xx is a promise.** Senders treat it as "you
own this now." So the service acknowledges only after the publish is confirmed
durable, and returns 503 otherwise. Returning 200 first turns every transient
Pub/Sub error into permanent, silent data loss.

Part of the [Accelerators](https://github.com/mikenelsonjr/Accelerators)
catalog.

## Built for Aptly first, designed to be extended

This was built against a real provider — [Aptly](https://getaptly.com) card
webhooks — rather than an imagined one, because a template validated only
against its own assumptions tends to be wrong in ways nobody notices until
production. Everything here has been driven end to end against a running
service and a Pub/Sub emulator.

That means a few defaults reflect Aptly's actual behaviour, and those are
exactly the places to look first when adapting it:

| Aptly does this | Your provider may not | Where to change it |
|---|---|---|
| Sends a static token in `x-signingkey` | Most sign the body (HMAC) | `ingest/security.py` |
| Sends no delivery-id header | Many send one (`X-GitHub-Delivery`) | `event_id` in `ingest/app.py` |
| Never retries a failed delivery | Most retry on 5xx | `ingest/retry.py` |

None of that is baked into the structure. Authentication is one function behind
one call site; the publisher is a `Protocol` with a swappable implementation;
retry is a wrapper you can delete in one line. The parts that generalise — the
delivery guarantee, the rejection ordering, request hygiene, structured logging
without payload leakage — are provider-agnostic.

**[CONTEXT.md](CONTEXT.md) explains why each decision was made**, including the
security tradeoff of a shared secret versus HMAC and what compensates for it.
Read it before changing anything load-bearing.

## Use this template

Click **Use this template** above, or:

```bash
gh repo create my-service --template mikenelsonjr/webhook-handler --private
cd my-service
git config core.hooksPath .githooks   # enable the secret-scanning hook
cp .env.example .env                  # then fill it in
```

## What to change first

1. **`GCP_PROJECT` and `PUBSUB_TOPIC`** — point at your project and topic. Both
   the topic *and* a subscription must exist before the first publish: Pub/Sub
   only retains messages for subscriptions that already existed, so creating
   one later silently loses everything sent so far.

2. **`WEBHOOK_SIGNING_SECRET`** — from Secret Manager in every deployed
   environment (`--set-secrets`). Never a literal, never in this repo. An unset
   value fails closed, so the service refuses everything rather than accepting
   everything.

3. **Authentication, if your provider signs the body.** `ingest/security.py`
   holds one function, `verify_signing_key(expected, provided)`, called from
   one place. For an HMAC provider, replace it with a comparison over the raw
   request body — which the route already has in hand precisely so this swap
   stays a one-file change. Keep `hmac.compare_digest`.

4. **`event_id`, if your provider sends a delivery id.** It is currently a
   SHA-256 of the raw body, because Aptly sends no such header. A
   provider-supplied id is strictly better — it stays stable across the
   *sender's* retries, and it cannot collide when two genuinely distinct events
   carry identical payloads.

5. **Retry, if your provider does retry.** `RetryingPublisher` exists because
   Aptly delivers once and ignores the response, so a 503 earns no
   redelivery. If yours retries on 5xx, the sender is a better safety net than
   in-process backoff — drop the wrapper from `main.py`.

**What not to change:** the raw request body is published byte-for-byte,
deliberately. Re-serializing through `json.dumps` reorders keys and drops
whitespace, so a consumer can no longer verify or compare against what the
provider actually sent. If you want a stable envelope for subscribers, build it
in the processor where you control both sides — not by rewriting bytes in
flight.

## Local development

### Tests

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m pytest          # tests
ruff check .              # lint
```

The suite never touches GCP — it drives the app through a fake publisher. Add
the `[gcp]` extra to also run the concrete Pub/Sub publisher's tests, which use
an injected fake client and still need no credentials:

```bash
pip install -e ".[dev,gcp]"
```

### Running the whole thing

`docker compose` brings the service up against a **Pub/Sub emulator** — no GCP
project, no credentials, and no way to write to a real topic by accident. The
client switches to the emulator purely because `PUBSUB_EMULATOR_HOST` is set;
nothing in the application code knows the difference.

```bash
docker compose up --build
curl -sS localhost:8080/healthz
```

Send a delivery shaped like a real one (the key matches `docker-compose.yml`):

```bash
curl -X POST localhost:8080/webhook \
  -H 'Content-Type: application/json' \
  -H 'x-signingkey: local-dev-key-not-a-real-credential' \
  -d '{"action":"update","data":{"_id":"abc"}}'
# {"message_id":"1","event_id":"cf709a70..."}
```

Then read exactly what landed on the topic — raw bytes and attributes:

```bash
docker compose run --rm pull
docker compose down -v          # stop and discard emulator state
```

`topic-init` creates the topic **and** the subscription before the service
starts. Both are needed up front: Pub/Sub only retains a message for
subscriptions that already existed when it was published, so creating the
subscription later leaves everything sent so far silently unreadable.

Without Docker, `uvicorn main:app --reload --env-file .env` works too, but you
will need a real topic and credentials.

## Deploy

```bash
gcloud run deploy webhook-handler \
  --source . \
  --region us-central1 \
  --set-env-vars GCP_PROJECT=<project>,PUBSUB_TOPIC=<topic> \
  --set-secrets WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest
```

Give the service's identity `roles/pubsub.publisher` on the topic and nothing
more.

`--allow-unauthenticated` is required for a provider that cannot present a
Google identity, which is the usual case — the signing key is then the only
thing standing between the internet and your topic. Confirm it is set and
working before the endpoint is reachable: an unset secret fails closed, so the
failure mode is a service that rejects everything rather than one that accepts
everything, but verify rather than assume. Restrict ingress to the provider's
IP ranges if they publish them.

## Security

Secrets are gitignored and a pre-commit hook scans staged diffs for
credential-shaped content. Enable it after cloning:

```bash
git config core.hooksPath .githooks
```

This repo is public: a key that lands here is compromised immediately, and
force-pushing does not recall it from forks or GitHub's cache. Rotate first,
scrub second.

## License

[MIT](LICENSE)
