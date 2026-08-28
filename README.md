# Webhook Handler

[![CI](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml/badge.svg)](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml)

A Python webhook ingest service for Cloud Run that validates inbound requests
and publishes them to a Pub/Sub topic.

The pattern it implements: **accept fast, process later.** The handler does the
minimum synchronous work — verify the signature, shape the payload, publish —
and returns. Everything expensive happens downstream in a subscriber, so a slow
consumer can never cause the sender to time out and retry.

Part of the [Accelerators](https://github.com/mikenelsonjr/Accelerators)
catalog.

> **Status: greenfield.** The design and the decisions behind it are settled and
> written up in [CONTEXT.md](CONTEXT.md); the implementation is not written yet.
> The repo scaffolding — CI, secret guards, license — is in place so the first
> commit of real code lands in a repo that is already correct.

## Use this template

Click **Use this template** above, or:

```bash
gh repo create my-service --template mikenelsonjr/webhook-handler --private
cd my-service
git config core.hooksPath .githooks   # enable the secret-scanning hook
cp .env.example .env                  # then fill it in
```

## What to change first

1. **`PUBSUB_TOPIC` and `GCP_PROJECT`** in `.env` — point at your project and
   topic. The topic must exist before the service starts.
2. **Signature verification** — every provider signs differently (Stripe's
   `Stripe-Signature`, GitHub's `X-Hub-Signature-256`, a plain shared secret).
   Replace the verification step with your provider's scheme; do not ship the
   placeholder.
3. **The payload contract** — decide what goes on the wire to Pub/Sub. Publish
   a stable envelope you control, not the provider's raw body, so a change on
   their side doesn't break every subscriber.
4. **`WEBHOOK_SIGNING_SECRET`** — source it from Secret Manager in every
   deployed environment. Never a literal, never in this repo.

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
more. Do not deploy with `--allow-unauthenticated` until signature verification
is implemented and tested — an open, unverified ingest endpoint is a free way
for anyone to inject messages into your queue.

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
