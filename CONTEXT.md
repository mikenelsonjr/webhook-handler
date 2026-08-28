# CONTEXT — Webhook Handler

A Python webhook ingest service running on Cloud Run that verifies inbound
requests and publishes them to a Pub/Sub topic. Built as an accelerator: it is
meant to be cloned into real projects and hardened, not rewritten.

## The pattern

**Accept fast, process later.** The handler does the minimum synchronous work —
authenticate, publish, acknowledge — and returns. Everything expensive
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

**A shared secret in a header, not HMAC — because that is what Aptly sends.**
Aptly delivers a static token in `x-signingkey`; it computes no signature over
the body, so there is nothing to verify against one. Verification is a
constant-time comparison of that token. An unset secret fails closed.

This is **weaker than HMAC** and the weakness is worth stating plainly: the
token does not bind to the payload, so anyone who observes one request can
forge any other; it is replayable; and it is identical on every request, so it
leaks anywhere headers are logged. None of that is fixable from this side — you
cannot verify a signature the sender never produced. Compensate with TLS-only
transport, restricted Cloud Run ingress, and scheduled rotation.

A consumer whose provider *does* sign (Stripe, GitHub, Shopify) should replace
`verify_signing_key` with an HMAC comparison over the raw body. The route calls
one function, so that is a one-file change — which is the point of the seam.

**Bounded publish retry, because Aptly never retries.** Aptly delivers each
event once and ignores the response, so a 503 does not earn a redelivery — a
failed publish is a lost event, whatever status code we return. The durability
has to live on this side of the wire.

`RetryingPublisher` wraps any `Publisher` with a few attempts and exponential
backoff: enough to absorb a transient Pub/Sub blip, and no more. It is
deliberately not a durable spool (write failures to GCS, reconcile later),
which is the upgrade path if losses ever show up in practice.

It is a wrapper rather than logic in the route because it both satisfies and
consumes the `Publisher` protocol, so it composes at the entrypoint:

```python
publisher = RetryingPublisher(PubSubPublisher(settings))
```

The route stays one `publisher.publish(...)` call and never learns that
retrying happens. `attempts=1` turns it off.

The tradeoff: retrying a publish that timed out *after* Pub/Sub durably stored
the message produces a duplicate. Since the sender never retries, losing an
event is the worse outcome, so this errs toward at-least-once — which is what
the `event_id` attribute exists to let the processor clean up. Only
`PublishError` is retried; anything else is a bug, and retrying a bug just
delays the failure while burying the cause.

**No CORS.** This is a server-to-server endpoint. The original returned
`Access-Control-Allow-Origin: *` and answered OPTIONS preflights, which is
meaningless here and invites browser-driven abuse.

**Configuration is read from `os.environ` in exactly one place.**
`Settings.from_env(env)` is a pure function of a mapping — no `os.environ`, no
`.env` loading, no import-time I/O. Only the entrypoint reads the ambient
environment, at module scope:

```python
settings = Settings.from_env(os.environ)     # main.py, module scope
```

| Environment | Config source | Who reads it |
|---|---|---|
| Local | `.env` via `uvicorn --env-file .env` | uvicorn, before import |
| CI / tests | an explicit dict | the test, never the process env |
| Cloud Run | `--set-env-vars` + `--set-secrets` | the platform |

`.env` is a local-development convenience, never a configuration mechanism.
Loading it inside `config.py` would mean importing the module reads whatever
file happens to be on the developer's machine, so tests pass locally and fail
in CI for reasons unrelated to the code. `uvicorn[standard]` already depends on
`python-dotenv` and exposes `--env-file`, so local `.env` support costs no code
and has no production surface. **Do not import `dotenv` in the service.**

Rejected `pydantic-settings`: nearly free given FastAPI already brings pydantic,
but it reads `os.environ` and `.env` from inside the settings class, which
reintroduces the import-time coupling this design removes.

**Configuration failures crash the container, not the request.** `from_env`
validates and raises, and the entrypoint calls it at import. A missing variable
fails the revision at startup, Cloud Run keeps the previous revision serving,
and no traffic is routed to the broken one. Discovering it on the first webhook
instead means 503-ing a real event that may never be retried.

**Secrets arrive as environment variables, not SDK calls.** Cloud Run's
`--set-secrets` mounts a Secret Manager version into the environment, so the
service needs no GCP client, no IAM at request time, and gains no failure mode
on the hot path. Tradeoff: `:latest` resolves at *instance start*, so a rotated
secret takes effect as instances recycle rather than immediately. Fetching from
Secret Manager at runtime would close that gap and is not worth the complexity
here.

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
  security.py          verify_signing_key
  publisher.py         Publisher protocol + PublishError, PublishTimeout
  retry.py             RetryingPublisher — bounded retry, composes with any Publisher
  pubsub_publisher.py  concrete client (needs the [gcp] extra)
  app.py               create_app(settings, publisher) -> FastAPI
```

| Route | Auth | Success | Notes |
|---|---|---|---|
| `GET /healthz` | none | 200 | Cloud Run probes cannot authenticate |
| `POST /webhook` | signing key | 202 | body `{message_id, event_id}` |

Rejections, in the order they are checked:

| Condition | Status |
|---|---|
| Wrong method | 405 |
| Body over `MAX_BODY_BYTES` | 413 |
| Missing / wrong signing key | 401 |
| Non-JSON content type | 415 |
| Malformed JSON | 400 |
| Publish failed or timed out | 503 |

Authentication precedes content-type and payload validation, so an
unauthenticated caller learns nothing about the request's other faults.

Every published message carries exactly three attributes: `event_id`,
`source`, and `received_at`. Attributes are **chosen, not forwarded** — an
earlier draft published `dict(request.headers)`, which put the caller's
`Authorization`, `Cookie`, and the signing key itself onto the topic for every
subscriber to read.

`event_id` is a SHA-256 of the raw body. Aptly sends no delivery-id header, so
there is no better source. Two consequences worth knowing:

- **Retries dedup only if Aptly replays identical bytes.** If it regenerates
  the payload — the observed body carries `viewedAt` and `lockUntil`
  timestamps — the hash differs and the duplicate gets through. Verify this
  against a real retry before relying on it.
- **Byte-identical distinct events collide.** In practice those same moving
  timestamps make that unlikely, but it is a real failure mode: the second
  event would be silently dropped as a duplicate.

`received_at` deliberately varies per delivery. `event_id` identifies the
event; `received_at` identifies the attempt.

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
