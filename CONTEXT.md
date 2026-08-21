# CONTEXT — Webhook Handler

A Python webhook ingest service running on Cloud Run that verifies inbound
requests and publishes them to a Pub/Sub topic. Built as an accelerator: it is
meant to be cloned into real projects and hardened, not rewritten.

## The pattern

**Accept fast, process later.** The handler does the minimum synchronous work —
verify the signature, publish, acknowledge — and returns. Everything expensive
happens in a subscriber, so a slow consumer can never make the sender time out
and retry.

One rule dominates the design: **a 2xx is a promise.** Webhook senders treat it
as "you own this now" and will not retry. So the service acknowledges only
after the publish is confirmed durable, and returns 503 otherwise. Returning
200 before confirming turns every transient Pub/Sub error into permanent data
loss.

## Status

Being rebuilt from a working Cloud Function original. The failing acceptance
suite in `tests/` defines the target; the implementation is in progress.
Findings tracked as issues 1–7 in the
[catalog repo](https://github.com/mikenelsonjr/Accelerators/issues),
labelled `epic:harden-ingest`.

## Decisions

**FastAPI, not functions-framework.** The original was a Cloud Function using
`functions_framework.http`. Cloud Run is the target, and FastAPI gives a real
test client, explicit routing, and a natural place for request-level middleware.
Cost: a Dockerfile and an explicit uvicorn entrypoint.

**The payload is opaque bytes.** Pydantic validates request *shape*, but a
webhook body is whatever the provider sends and its schema is not ours to
police. The raw bytes are published unmodified — re-serializing through
`json.dumps` reorders keys and destroys any signature a downstream consumer
might want to re-verify. JSON is parsed only to reject malformed input.

**The Pub/Sub client is injected, never constructed at import.** The original
built a `PublisherClient()` at module scope, so importing it called
`google.auth.default()` and raised `DefaultCredentialsError` anywhere
credentials were absent — including CI. `ingest/publisher.py` therefore defines
only a `Publisher` protocol with no `google` import; the concrete client lives
in its own module that only the entrypoint touches. That is why
`google-cloud-pubsub` is an optional `[gcp]` extra: the suite installs and runs
without the grpc toolchain.

**HMAC-SHA256 over the raw body, constant-time compare.** Every provider signs
differently, so the header name is configurable and the scheme is documented as
the first thing a consumer should replace. An unset secret fails closed.

**No CORS.** This is a server-to-server endpoint. The original returned
`Access-Control-Allow-Origin: *` and answered OPTIONS preflights, which is
meaningless here and invites browser-driven abuse.

## Repo layout

Four components, one repo, because they are one deployable system: the topic
schema is the seam between them and changing it in isolation is the failure
mode worth designing against.

```
ingest/       FastAPI receiver -> publishes to the topic
processor/    subscriber stub  -> reads from the topic
infra/        Terraform: Cloud Run service, topic, subscription, IAM
tests/
  conftest.py           repo-wide helpers
  test_dependencies.py  manifest + stdlib integrity
  ingest/               receiver suite
  processor/            subscriber suite
  contract/             the envelope both sides must agree on
```

`tests/contract/` is not ceremony. Ingest publishes an envelope and the
processor consumes it; if each side asserts its own idea of that shape, they
drift and no test notices. The contract tests belong to neither component, so
they get their own directory and both suites are checked against them.

Terraform is **not** driven from pytest. `terraform fmt -check`, `validate`,
and `tflint` run as a separate CI job — folding them in would mean the Python
suite needs a Terraform toolchain on PATH to run at all.

## Interface

```
ingest/
  config.py            Settings, loaded from env, validated at startup
  security.py          compute_signature / verify_signature
  publisher.py         Publisher protocol + PublishError, PublishTimeout
  pubsub_publisher.py  concrete client (needs the [gcp] extra)
  app.py               create_app(settings, publisher) -> FastAPI
```

| Route | Auth | Success | Notes |
|---|---|---|---|
| `GET /healthz` | none | 200 | Cloud Run probes cannot sign |
| `POST /webhook` | HMAC | 202 | body `{message_id, event_id}` |

Rejections, in the order they are checked:

| Condition | Status |
|---|---|
| Wrong method | 405 |
| Body over `MAX_BODY_BYTES` | 413 |
| Missing / bad signature | 401 |
| Non-JSON content type | 415 |
| Malformed JSON | 400 |
| Publish failed or timed out | 503 |

Authentication precedes content-type and payload validation, so an
unauthenticated caller learns nothing about the request's other faults.

Every published message carries `event_id` (the sender's delivery id when
present, otherwise a deterministic hash of the body, so a retry dedups),
`source`, and `received_at`.

## Testing

```bash
python -m venv .venv && .venv/Scripts/activate   # POSIX: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The suite never touches GCP. `tests/ingest/conftest.py` provides a
`FakePublisher` that records what was published and can be armed to fail —
that is how the delivery-guarantee tests work — and its module docstring
documents the full contract the implementation must satisfy. Read it first.

Run one component's suite with `pytest tests/ingest`.
