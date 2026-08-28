# Webhook Handler

[![CI](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml/badge.svg)](https://github.com/mikenelsonjr/webhook-handler/actions/workflows/ci.yml)

Two Python services for Cloud Run: a **receiver** that authenticates inbound
webhooks and publishes them to a Pub/Sub topic, and a **processor** that Pub/Sub
pushes them back to. Both halves ship, because the interesting failures live in
the seam between them.

The pattern: **accept fast, process later.** The receiver does the minimum
synchronous work — authenticate, publish, acknowledge — and returns. Everything
expensive happens in the processor, so a slow consumer can never make the
sender time out.

One rule drives both sides, read from opposite ends.

**A 2xx is a promise.** Senders treat it as "you own this now", so the receiver
acknowledges only after the publish is confirmed durable, and returns 503
otherwise. Returning 200 first turns every transient Pub/Sub error into
permanent, silent data loss.

**A 2xx means "never send me this again."** In push delivery the response
status *is* the acknowledgement, so the processor returns 204 when an event is
handled — and also when the envelope is unparseable, because any non-2xx is a
nack and bad bytes never become good bytes on the third attempt.

```
provider ──▶ ingest ──▶ Pub/Sub topic ──push──▶ processor
             (202)                              (204 ack / 503 nack)
             └──────────── one event_id ────────────┘
```

That `event_id` is the same value in the receiver's response and the
processor's logs. One id, grepped across two services, returns the whole story.

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

6. **`LoggingHandler` — this is the one you came for.** It is the processor's
   default handler and it does nothing but log that an event arrived. Replace
   it in `processor_main.py` with your own: anything with `handle(event)`
   satisfies the protocol, and it never learns that HTTP or Pub/Sub exist.

   Classify your failures. Raise `RetryableError` for something that might
   work on the next delivery and `PermanentError` for something that never
   will — the endpoint cannot tell them apart, and guessing is how a service
   either loses events or redelivers a poison pill forever. **Make it
   idempotent:** delivery is at-least-once by construction.

7. **`InMemorySeenStore`, before you rely on deduplication.** It is
   per-instance. Cloud Run runs N containers and a redelivery lands wherever
   the load balancer sends it, so it defuses a redelivery storm on a warm
   instance and nothing more. A real guarantee is a Firestore
   create-if-absent with a TTL, or a Redis `SET NX EX`, behind the same two
   methods — `claim` and `release`.

8. **The ack deadline**, once you know what your handler costs. Set it above
   the handler's *worst* case: exceed it and Pub/Sub redelivers while the
   first attempt is still running, so one event is processed twice
   concurrently. Work that cannot fit belongs on another topic or in Cloud
   Tasks — the same accept-fast-process-later move, one layer down.

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

### Infrastructure checks

`infra/` holds the Terraform. Its checks are a **separate CI job** and are not
driven from `pytest`: folding them in would mean the Python suite needs a
Terraform toolchain on PATH to run at all.

You do not need Terraform installed — Docker is enough, and nothing below
touches GCP or needs credentials:

```bash
tf()     { docker run --rm -v "$PWD/infra:/w" -w /w hashicorp/terraform:latest "$@"; }
tflint() { docker run --rm -v "$PWD/infra:/data" -v tflint-plugins:/root/.tflint.d \
             ghcr.io/terraform-linters/tflint:latest "$@"; }

tf fmt -check -diff
tf init -backend=false     # no state, no backend, no credentials
tf validate
tflint --init && tflint --format compact
```

`tflint --init` installs the Google ruleset into the named volume, so it is
downloaded once rather than on every run. That ruleset is what catches values
the provider schema accepts and the API later rejects — `validate` cannot see
those, because it only checks shape.

Copy `infra/terraform.tfvars.example` to `terraform.tfvars` and fill it in.
`project_id` and `region` deliberately have **no defaults**: a default there
silently deploys to the wrong project.

### Running the whole thing

`docker compose` brings **both services** up against a **Pub/Sub emulator** — no
GCP project, no credentials, and no way to write to a real topic by accident.
The client switches to the emulator purely because `PUBSUB_EMULATOR_HOST` is
set; nothing in the application code knows the difference.

```bash
docker compose up --build
curl -sS localhost:8080/healthz     # ingest
curl -sS localhost:8081/healthz     # processor
```

Send a delivery shaped like a real one (the key matches `docker-compose.yml`):

```bash
curl -X POST localhost:8080/webhook \
  -H 'Content-Type: application/json' \
  -H 'x-signingkey: local-dev-key-not-a-real-credential' \
  -d '{"action":"update","data":{"_id":"abc"}}'
# {"message_id":"1","event_id":"a76b8cc3..."}
```

The emulator then **pushes** that message to the processor, exactly as Pub/Sub
does to Cloud Run. Watch it arrive:

```bash
docker compose logs processor
# {"severity":"INFO","logger":"processor.handler","message":"event received",
#  "event_id":"a76b8cc3...","source":"aptly","message_id":"1","payload_bytes":39}
```

**The `event_id` in the ingest response and in the processor's log line are the
same value.** That is what it is for, and this is where you can see it working:
one id, grepped across both services, returns the whole story.

Read the exact bytes that landed on the topic — raw body and attributes:

```bash
docker compose run --rm pull
docker compose down -v          # stop and discard emulator state
```

`topic-init` creates the topic and **both** subscriptions before ingest starts:
a pull subscription for `run --rm pull`, and the push subscription that feeds
the processor. Each gets its own copy of every message, so they do not compete
— which keeps *did ingest publish it?* and *did the processor handle it?* two
separately answerable questions. All of it has to exist up front: Pub/Sub only
retains a message for subscriptions that already existed when it was published,
so creating one later leaves everything sent so far silently unreadable.

The processor runs `PUSH_AUTH_MODE=none` here, because the emulator sends no
OIDC token. It warns loudly at startup, and it is the one mode that must never
reach production — see [Deploy](#the-processor).

> `docker compose run --rm pull` prints message bodies. It is a debugging tool
> in the `tools` profile, not part of `up`, and it is the only thing here that
> will show you a payload — the services themselves log an `event_id` and a
> byte count and never the body.

Without Docker: `uvicorn main:app --reload --env-file .env` for the receiver and
`uvicorn processor_main:app --port 8081 --env-file .env` for the subscriber, but
you will need a real topic and credentials.

## Deploy

### Ingest

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

### The processor

The processor is a **push** subscriber: Pub/Sub POSTs each message to it over
HTTPS, so it scales to zero and Pub/Sub owns retry, backoff, and the
dead-letter policy. Deploy it with authentication **on**:

```bash
gcloud run deploy webhook-processor \
  --source . \
  --region us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars PUSH_AUTH_MODE=iam
```

`PUSH_AUTH_MODE=iam` means *Cloud Run already authenticated the caller*. With
`--no-allow-unauthenticated`, the platform validates the Pub/Sub OIDC token
before the request reaches the container, so the application needs no
verification code and the endpoint is unreachable from the internet. There is
**no default** for this variable — an unset or unrecognised value crashes the
container at startup, so Cloud Run keeps the previous revision serving and a
typo cannot ship an open endpoint.

| Mode | Use it when |
|---|---|
| `iam` | Cloud Run with `--no-allow-unauthenticated`. **The production setting.** |
| `none` | Local emulator only. Warns loudly at startup. |
| `oidc` | Not on Cloud Run, or you want the app to pin `aud`/`email` itself. Needs `pip install '.[oidc]'`. |

**Two service accounts, not one.** The *push* service account is the identity
Pub/Sub uses to call the service; the service's own *runtime* identity is
separate and needs only what your handler touches. Do not reuse one for both —
the push account should have no permissions beyond invoking this service.

#### Three grants that fail silently

Nothing errors when these are missing. Delivery just stops, or the dead-letter
topic stays empty while the retry count climbs, which looks exactly like a
broken consumer:

| Grant | On | Symptom if missing |
|---|---|---|
| `roles/run.invoker` → push SA | the Cloud Run service | 403 on every push; redelivered until retention expires |
| `roles/iam.serviceAccountTokenCreator` → the Pub/Sub service agent | the **push SA** | Pub/Sub cannot mint the token; the subscription never delivers |
| `roles/pubsub.publisher` → the Pub/Sub service agent | the **dead-letter topic** | dead-lettering never happens; poison messages retry forever |

The service agent is `service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com`.
The last two catch people out because they are grants *to Google's own
identity*, on resources the operator is not thinking about while wiring up a
subscription.

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding webhook-processor \
  --member="serviceAccount:${PUSH_SA}" --role=roles/run.invoker --region us-central1

gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" \
  --member="serviceAccount:${AGENT}" --role=roles/iam.serviceAccountTokenCreator

gcloud pubsub topics add-iam-policy-binding webhook-events-dead-letter \
  --member="serviceAccount:${AGENT}" --role=roles/pubsub.publisher
```

Set the subscription's ack deadline above your handler's **worst case**, not
its median: exceed it and Pub/Sub redelivers while the first attempt is still
running, so the same event is processed twice concurrently.

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
