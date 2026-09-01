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

**Both services are built.** 320 tests, and the whole path runs on a Pub/Sub
emulator with no GCP project and no credentials: a signed webhook to `ingest`
is published, pushed to `processor`, and handled — with the same `event_id` in
the ingest response and the processor's log line.

`ingest/` was rebuilt from a working Cloud Function original
(`epic:harden-ingest`, issues 1–8). `processor/` was built story by story as
`epic:build-processor`, issues 10–16, with issue 9 fixing a log formatter that
was silently dropping every `extra=` field. All are closed in the
[catalog repo](https://github.com/mikenelsonjr/Accelerators/issues).

`infra/` provisions all of it in Terraform (`epic:build-infra`, issues 17–22):
both services, both topics, three service accounts, every IAM grant, and the
push subscription.

**This is a template, and it is complete as one.** Nothing here has been
applied to a GCP project, and that is the design rather than an omission — the
consumer applies it to theirs. So the Terraform is verified to the level that
is meaningful without a project: `terraform fmt`, `validate`, `tflint` with the
Google ruleset, and variable validation exercised through `plan` (which checks
variables before it authenticates). What that cannot catch is IAM that is
valid-but-wrong, which is exactly why the three grants that fail silently are
called out in comments, in the README's triage table, and here.

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
processor/    push subscriber  -> Pub/Sub POSTs the message here
common/       the little that both deployables must agree on (log formatter)
infra/        Terraform: Cloud Run services, topic, push subscription, IAM
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

## Interface — ingest

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

---

# The processor

Ingest's rule is *a 2xx is a promise* — do not acknowledge until the message is
durable. The processor is that same sentence read from the other end:

> **A 2xx means "never send me this again."**

Every design decision below falls out of taking that literally.

## Push, not pull

Pub/Sub POSTs each message to an HTTPS endpoint on Cloud Run. The alternative —
a long-running worker holding a `StreamingPull` — was rejected for three
reasons:

- **It scales to zero.** A pull subscriber must be running to receive anything,
  so it needs `min-instances=1` or a VM, and it idles at full cost between
  webhooks. Push instances exist only while a message is being handled.
- **Pub/Sub already owns retry.** Backoff, the redelivery ceiling, and the
  dead-letter topic are subscription settings. A pull subscriber reimplements
  the first two in application code and gets them subtly wrong.
- **A push body is plain JSON over HTTP.** The processor imports nothing from
  `google.cloud` — no grpc toolchain, no credentials, no `[gcp]` extra to run
  its suite. Ingest had to fight for that property (see the injected-publisher
  decision above); the processor gets it for free.

What is given up: no flow control beyond Cloud Run's concurrency and
`max-instances`, no ordering keys, and a hard ceiling on how long one message
may take (below). Those are the conditions under which pull is the right
answer, and the upgrade path is deliberately short — `Handler` knows nothing
about HTTP, so a pull runner drives the same handler unchanged.

## The response code *is* the ack

| Outcome | Response | Meaning to Pub/Sub |
|---|---|---|
| Handled successfully | 204 | ack — delete it |
| `event_id` already handled | 204 | ack — it was done the first time |
| **Envelope unparseable / payload undecodable** | **204** | ack — see below |
| Handler raised `PermanentError` | 204 | ack — this will never succeed |
| Handler raised `RetryableError` | 503 | nack — redeliver with backoff |
| Any unexpected exception | 503 | nack — redeliver with backoff |
| Request did not come from Pub/Sub | 401 | not an ack decision at all |

**Acking a malformed envelope is the non-obvious one, and it is the whole
point.** In push delivery *any* non-2xx is a nack, so the reflexive
`400 Bad Request` on unparseable input tells Pub/Sub to send it again — and it
will, with backoff, until the message ages out of the retention window seven
days later. Bad bytes do not become good bytes on the third attempt. So the
processor logs it, acks it, and moves on; the dead-letter topic is where such
a message goes to be looked at, not the redelivery loop.

Note where this **differs from `RetryingPublisher`**, which retries only
`PublishError` and lets anything unexpected propagate on the grounds that an
unknown failure is a bug and retrying just buries it. Here an unknown exception
nacks. The difference is the backstop: Pub/Sub retries a bounded number of
times and then dead-letters, so an unknown failure is investigated from the
DLQ rather than lost, and a genuinely transient fault gets the second chance it
deserves. In-process retry had no such ceiling and no such dead-letter.

## The ack deadline bounds the work

Pub/Sub gives a push endpoint a fixed window to respond — 10 seconds by
default, settable to 600. Exceed it and the message is redelivered **while the
first attempt is still running**, so the same event is processed twice
concurrently. That is not a hypothetical: it is the standard way a push
subscriber gets into trouble.

Two consequences, both of which belong in `infra/`:

- `ackDeadlineSeconds` must exceed the handler's worst case, not its median.
- Cloud Run's request timeout must exceed the ack deadline, or the platform
  kills the request before Pub/Sub gives up on it.

Work that cannot fit under a sane deadline does not belong in the handler.
Publish to a second topic or enqueue a Cloud Task and ack — the same
accept-fast-process-later move ingest makes, one layer down.

## Authentication: enforced by the platform, with a seam

Pub/Sub signs each push request with an OIDC token when the subscription is
configured with a service account. Deploy with `--no-allow-unauthenticated` and
grant that account `roles/run.invoker`, and **Cloud Run validates the token
before the request reaches the container** — the application then needs no
verification code at all, and the endpoint is unreachable from the internet.

That is the intended production posture, and in-process verification there
would be a step backwards: it re-does work the platform already did, adds a
dependency to the image, and puts a public-key fetch on the hot path — a new
failure mode buying nothing.

But it is not the only posture — the local emulator sends no token, and a
consumer running this outside Cloud Run has nothing validating anything — so
verification stays a seam, `PUSH_AUTH_MODE`, with **no default**:

| Mode | Meaning | Where |
|---|---|---|
| `iam` | The platform authenticated the caller before the request arrived; this service trusts it. | Cloud Run + `--no-allow-unauthenticated` — **the production default** |
| `none` | **Nothing** authenticates this endpoint. | The emulator, local development |
| `oidc` | Verify the bearer token in-process: signature, `aud`, and the `email` claim against `PUSH_SERVICE_ACCOUNT`. | Non-Cloud-Run hosting, or defence in depth |

`iam` and `none` do the same nothing in-process, and are still separate values
on purpose. A config value is documentation the operator writes, so grepping
`PUSH_AUTH_MODE` across environments has to distinguish "Cloud Run enforced
this" from "nothing enforces this" — `none` in a production deploy is an alarm,
and a single merged value would hide it behind the reading that is fine. `none`
logs a WARNING at startup for the same reason.

An earlier draft merged the two under the name `trusted`, chosen so it would
read uncomfortably. That was backwards: the mode it stigmatises is the correct
production setting, so the name argued for `oidc` where `oidc` buys nothing.

Requiring the variable follows ingest's rule that **configuration failures
crash the container at startup** — there is no safe default here, so there is
no default, and a typo cannot silently ship an open endpoint.

`oidc` mode is one function, `verify_push_token`, called from one place,
mirroring `verify_signing_key` on the ingest side.

### The grants that fail silently

Three of them, in the standard setup — a push service account distinct from the
service's own runtime identity. None of these produce an error anywhere
obvious; delivery simply stops, or the dead-letter topic stays empty while the
retry count climbs:

| Grant | On | Without it |
|---|---|---|
| `roles/run.invoker` → push SA | the Cloud Run service | 403 on every push, redelivered until retention expires |
| `roles/iam.serviceAccountTokenCreator` → the Pub/Sub service agent, `service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com` | the **push SA** | Pub/Sub cannot mint the token; the subscription never delivers |
| `roles/pubsub.publisher` → that same service agent | the **dead-letter topic** | dead-lettering never happens; poison messages retry forever |

The runtime identity stays separate and needs only what the handler touches.

## Idempotency is the processor's problem

The system is at-least-once by construction, from two independent sources:

1. `RetryingPublisher` retries a publish that timed out *after* Pub/Sub stored
   the message, producing two topic messages for one webhook.
2. Pub/Sub redelivers on every nack and every missed ack deadline.

So `dedup.py` defines a `SeenStore` protocol — `claim(event_id) -> bool`,
`release(event_id)` — and ships `InMemorySeenStore`, a bounded TTL map.

**Be clear about what the in-memory one buys you: nothing across instances.**
Cloud Run runs N containers and a redelivery lands wherever the load balancer
sends it. It defuses a rapid redelivery storm hitting a warm instance, and it
makes the seam real and tested. It is not a distributed guarantee, and the
accelerator must say so where someone will read it rather than only here. The
production implementation is a Firestore create-if-absent with a TTL field, or
a Redis `SET NX EX`, behind the same two methods.

**When the store is written matters more than which store it is.** Recording
`event_id` *before* handling means a crash mid-handle leaves the event marked
done and it is never processed — silent loss, the failure ingest was built to
avoid. Recording *after* means a crash leaves it unclaimed and it is processed
twice. The claim/release shape splits the difference: claim before, release on
failure so a redelivery may retry, leave the claim standing on success. It
still favours duplicates over loss when a process dies between the two, which
is the same tradeoff the rest of the service makes deliberately.

Two properties of `event_id` are inherited from ingest and worth re-reading
before leaning on dedup: it is a SHA-256 of the raw body, so it only dedups a
sender retry if the sender replays **identical bytes** (unverified for Aptly —
the payload carries moving `viewedAt`/`lockUntil` timestamps), and two
genuinely distinct events with byte-identical bodies would collide and the
second would be dropped.

## The envelope, and why `tests/contract/` exists

Pub/Sub wraps the published message; the processor unwraps it:

```json
{
  "message": {
    "data": "<base64 of the exact bytes the provider sent>",
    "attributes": {"event_id": "...", "source": "aptly", "received_at": "..."},
    "messageId": "12345",
    "publishTime": "2026-08-28T15:04:05.000Z"
  },
  "subscription": "projects/<p>/subscriptions/<s>",
  "deliveryAttempt": 3
}
```

`data` is base64 of the **raw provider body**, because ingest publishes the
bytes untouched — so a processor can still re-verify a provider signature, or
diff against what the provider claims it sent. That property is only worth
anything if both sides agree it holds, which is what the contract suite is for:
it feeds ingest's published output straight into the processor's parser and
asserts byte-fidelity and the three attributes. Neither component's own suite
can catch that drift, because each would be asserting its own assumption.

`deliveryAttempt` is present **only** when the subscription has a dead-letter
policy, so it is read as an optional field and used for logging, never
required. Its absence is a subscription-configuration fact, not an error.

Poison-pill handling is the subscription's `deadLetterPolicy`
(`maxDeliveryAttempts: 5`), not an in-application counter — an in-app counter
is per-instance, so it counts a fraction of the attempts and reports a number
that is confidently wrong.

## Logging: `event_id` is the trace key

`event_id` is the only identifier that spans the whole path — it is computed at
ingest, published as an attribute, and arrives intact at the processor. Nothing
else does that job:

| Field | Spans the path? | What it identifies |
|---|---|---|
| `event_id` | **yes** | the event itself |
| `message_id` | no — assigned per publish | one message on the topic; a duplicate publish of the same event has a different one |
| `received_at` | no — regenerated per delivery | the ingest attempt |
| `delivery_attempt` | no — per redelivery | how close this message is to the DLQ |

So **every processor log line carries `event_id`**, and one value grepped
across both services returns the complete story: received, published,
delivered, handled or dead-lettered. Log `message_id` and `delivery_attempt`
alongside it — a climbing `delivery_attempt` is the signal that something is
heading for the dead-letter topic, and it is visible nowhere else.

**The formatter has to actually render them, which today's does not.**
`logging.basicConfig(format=...)` in `main.py` hand-builds JSON from a fixed
`%`-style string, so fields passed via `extra=` are set on the `LogRecord` and
then silently dropped — ingest currently passes `event_id` on every publish and
emits none of it. Adding `%(event_id)s` does not fix it: any record lacking the
attribute (uvicorn's, or a library's) then fails to format. It needs a
`logging.Formatter` subclass that serializes the record's non-standard
attributes through `json.dumps`. That also fixes the second bug in the same
line — interpolating a message containing a `"` produces invalid JSON, so
Cloud Logging demotes exactly the error line you were trying to trace to an
unparsed blob.

Both services need the identical formatter, so it lives in `common/log.py`
rather than being copied into two entrypoints. Duplicating twenty lines is
cheaper right up until the two halves of one trace disagree about what a field
is called, which is the moment the trace was supposed to be useful.

The `severity` key is kept: Cloud Logging reads that name specifically and maps
it to the real log level, rather than filing everything as `DEFAULT`.

`test_logging.py`'s payload-leak tests carry over to the processor and matter
more there. Ingest never decodes the body — it hashes opaque bytes — while the
processor holds the decoded payload in hand, so a leak is one careless
`logger.debug("handling %s", event)` away. `Event.__repr__` should not include
`data` for the same reason.

## Interface — processor

```
processor/
  config.py     Settings + from_env — pure function of a mapping, as ingest
  envelope.py   parse_push_envelope(body) -> Event; EnvelopeError
  security.py   verify_push_token — the OIDC seam
  dedup.py      SeenStore protocol + InMemorySeenStore
  handler.py    Handler protocol, RetryableError, PermanentError, LoggingHandler
  app.py        create_app(settings, handler, seen) -> FastAPI

common/
  log.py        JsonFormatter + configure_logging — used by BOTH entrypoints
```

`common/` is kept deliberately thin. It holds the log formatter because a trace
spanning two services needs one spelling of `event_id`, and nothing else: the
message envelope stays pinned by `tests/contract/` rather than by a shared
constants module, so neither side can quietly redefine the contract by editing
a file it owns.

| Route | Auth | Success | Notes |
|---|---|---|---|
| `GET /healthz` | none | 200 | as ingest — the prober has no identity |
| `POST /_pubsub/push` | `PUSH_AUTH_MODE` | 204 | body empty; the status *is* the ack |

`Event` is a frozen dataclass: `event_id`, `source`, `received_at`, `data`
(decoded bytes), `message_id`, `publish_time`, `delivery_attempt: int | None`.

`LoggingHandler` is the starter's default — it logs the `event_id` and payload
size and does nothing else. It exists so the service is runnable end to end on
day one and so the swap point is a single named thing rather than a TODO.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `PUSH_AUTH_MODE` | yes | — | `iam`, `none`, or `oidc`; no default, by design |
| `PUSH_SERVICE_ACCOUNT` | if `oidc` | — | expected `email` claim |
| `PUSH_AUDIENCE` | if `oidc` | — | expected `aud` claim |
| `DEDUP_TTL_SECONDS` | no | 3600 | in-memory store only |
| `DEDUP_MAX_ENTRIES` | no | 10000 | bounds the map; oldest evicted |
| `LOG_LEVEL` | no | `INFO` | |

## Local development

`docker-compose.yml` gains a `processor` service, and `topic-init` creates a
**push** subscription aimed at `http://processor:8080/_pubsub/push` — the
emulator supports push, so the full path runs locally with no GCP at all. The
emulator sends no OIDC token, which is exactly why `PUSH_AUTH_MODE=none`
exists — and why it is a distinct value from `iam` rather than sharing one with
the production setting.

The existing `local-drain` pull subscription stays. Each subscription gets its
own copy of every message, so `docker compose run --rm pull` still shows what
was published even though the processor is consuming in parallel — which makes
"did ingest publish it?" and "did the processor handle it?" two separately
answerable questions instead of one confusing one.

## Testing

```bash
python -m venv .venv && .venv/Scripts/activate   # POSIX: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The suite never touches GCP — no credentials, no grpc toolchain, no network.
That is not incidental: it is why the concrete Pub/Sub client lives behind a
`Publisher` protocol in its own `[gcp]` extra, why push-token verification is
behind `[oidc]` and loaded lazily, and why the processor imports nothing from
`google` at all.

Each component's `conftest.py` opens with the contract its implementation must
satisfy — object shapes, seam signatures, and the reasoning. **Read those
first**; they are the shortest accurate description of the system.

| Suite | What it holds |
|---|---|
| `pytest tests/ingest` | the receiver, driven through a `FakePublisher` that records what was published and can be armed to fail — that is how the delivery-guarantee tests work |
| `pytest tests/processor` | the subscriber: envelope parsing, the ack table, auth, dedup, logging |
| `pytest tests/contract` | the envelope both sides depend on |
| `pytest tests/common` | the JSON log formatter both services share |

`tests/contract/` imports **neither** component's fixtures, and a test enforces
that. A contract test that borrows one side's fixtures has adopted one side's
assumptions and can no longer catch that side being wrong — so it re-declares
its own publisher double and its own rendering of the push envelope, written
from the Pub/Sub format rather than from either implementation.

Two conventions worth keeping when adding tests:

- **Assert on formatter output, not on `LogRecord` attributes.** Issue 9
  shipped a broken formatter for months because a test checked
  `hasattr(record, "event_id")` — the call site — while the emitted line
  contained no such field. `caplog` captures records *before* formatting, which
  is exactly that blind spot.
- **Parse the source, do not grep it,** when a test asserts something about the
  code itself. `config.py`'s docstring says the words "os.environ" while
  promising not to call it, and a line scan cannot tell the promise from the
  breach.
