"""Issue #12 — authenticate the push request.

Three modes, no default:

    iam    the platform authenticated the caller before the request arrived
    none   NOTHING authenticates this endpoint
    oidc   verify the bearer token in this process

`iam` and `none` do the same nothing in-process and are still separate values,
because a config value is documentation the operator writes: `none` in a
production deploy is an alarm, and one merged value would hide it behind the
reading that is fine.

There is no default because a typo must not silently ship an open endpoint.
Configuration failures crash the container at startup, so Cloud Run keeps the
previous revision serving — the same rule as ingest.
"""

from __future__ import annotations

import pytest

from tests.processor.conftest import (
    FakeHandler,
    body,
    capture_processor_output,
    emitted,
    push_envelope,
)

PUSH_PATH = "/_pubsub/push"

SERVICE_ACCOUNT = "pubsub-push@example-project.iam.gserviceaccount.com"
AUDIENCE = "https://processor-abc123-uc.a.run.app"


def env(**overrides) -> dict[str, str]:
    """A complete oidc-mode environment, before overrides."""
    return {
        "PUSH_AUTH_MODE": "oidc",
        "PUSH_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
        "PUSH_AUDIENCE": AUDIENCE,
        **overrides,
    }


def claims(**overrides) -> dict:
    """What Google's verifier returns for a valid Pub/Sub push token."""
    return {
        "email": SERVICE_ACCOUNT,
        "email_verified": True,
        "aud": AUDIENCE,
        "exp": 4_102_444_800,  # 2100-01-01
        "iss": "https://accounts.google.com",
        **overrides,
    }


# -- configuration --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["iam", "none", "oidc"])
def test_every_valid_mode_is_accepted(mode):
    from processor.config import Settings

    settings = Settings.from_env(env(PUSH_AUTH_MODE=mode))

    assert settings.push_auth_mode == mode


def test_the_mode_is_case_insensitive():
    """`--set-env-vars PUSH_AUTH_MODE=IAM` must not fail the revision."""
    from processor.config import Settings

    assert Settings.from_env(env(PUSH_AUTH_MODE="IAM")).push_auth_mode == "iam"


@pytest.mark.parametrize("value", ["", "   ", "trusted", "platform", "yes", "true", "off"])
def test_a_missing_or_unrecognised_mode_raises(value):
    """Including `trusted`, which an earlier draft of this design used — an
    old config must fail loudly rather than be quietly reinterpreted."""
    from processor.config import Settings

    with pytest.raises(ValueError):
        Settings.from_env(env(PUSH_AUTH_MODE=value))


def test_an_absent_mode_raises():
    from processor.config import Settings

    environment = env()
    del environment["PUSH_AUTH_MODE"]

    with pytest.raises(ValueError):
        Settings.from_env(environment)


@pytest.mark.parametrize("missing", ["PUSH_SERVICE_ACCOUNT", "PUSH_AUDIENCE"])
def test_oidc_mode_requires_its_claims(missing):
    """Verifying against an unset expectation is not verification. Failing at
    startup means Cloud Run keeps the previous revision serving."""
    from processor.config import Settings

    environment = env()
    del environment[missing]

    with pytest.raises(ValueError):
        Settings.from_env(environment)


@pytest.mark.parametrize("mode", ["iam", "none"])
def test_the_other_modes_do_not_require_the_oidc_claims(mode):
    from processor.config import Settings

    settings = Settings.from_env({"PUSH_AUTH_MODE": mode})

    assert settings.push_auth_mode == mode


def test_from_env_does_not_read_the_ambient_environment(monkeypatch):
    """A pure function of the mapping it is handed — that is what lets tests
    pass an explicit dict and keeps a developer's .env out of a test run."""
    from processor.config import Settings

    monkeypatch.setenv("PUSH_AUTH_MODE", "iam")

    with pytest.raises(ValueError):
        Settings.from_env({})


def test_settings_are_frozen():
    from dataclasses import FrozenInstanceError

    from processor.config import Settings

    settings = Settings.from_env({"PUSH_AUTH_MODE": "iam"})

    with pytest.raises(FrozenInstanceError):
        settings.push_auth_mode = "none"


# -- the startup warning ---------------------------------------------------------


def test_selecting_none_warns_at_startup():
    """The one mode that leaves the endpoint open. It is legitimate locally, so
    it is allowed — but an accidental production deploy has to be visible."""
    from processor.app import create_app
    from processor.config import Settings

    with capture_processor_output() as stream:
        create_app(settings=Settings.from_env({"PUSH_AUTH_MODE": "none"}), handler=FakeHandler())

    warnings = [line for line in emitted(stream) if line["severity"] == "WARNING"]
    assert warnings, "PUSH_AUTH_MODE=none must warn at startup"
    assert any("none" in line["message"] for line in warnings)


@pytest.mark.parametrize("mode", ["iam", "oidc"])
def test_the_authenticated_modes_do_not_warn(mode):
    """Otherwise the warning is noise and nobody reads it in the one case that
    matters."""
    from processor.app import create_app
    from processor.config import Settings

    with capture_processor_output() as stream:
        create_app(settings=Settings.from_env(env(PUSH_AUTH_MODE=mode)), handler=FakeHandler())

    assert not [line for line in emitted(stream) if line["severity"] == "WARNING"]


# -- the endpoint ----------------------------------------------------------------


def make_app_client(mode: str, *, handler=None, verify=None):
    from fastapi.testclient import TestClient

    from processor.app import create_app
    from processor.config import Settings

    kwargs = {} if verify is None else {"verify": verify}
    return TestClient(
        create_app(
            settings=Settings.from_env(env(PUSH_AUTH_MODE=mode)),
            handler=handler or FakeHandler(),
            **kwargs,
        ),
        raise_server_exceptions=False,
    )


@pytest.mark.parametrize("mode", ["iam", "none"])
def test_an_unauthenticated_request_is_accepted_in_the_no_verify_modes(mode):
    """Cloud Run already rejected anything unauthorised in `iam`; in `none`
    there is nothing to reject with."""
    client = make_app_client(mode)

    response = client.post(PUSH_PATH, content=body(push_envelope()))

    assert response.status_code == 204


def test_oidc_mode_rejects_a_request_with_no_authorization_header():
    client = make_app_client("oidc", verify=lambda *a, **k: True)

    response = client.post(PUSH_PATH, content=body(push_envelope()))

    assert response.status_code == 401


def test_oidc_mode_accepts_a_verified_token():
    client = make_app_client("oidc", verify=lambda *a, **k: True)

    response = client.post(
        PUSH_PATH,
        content=body(push_envelope()),
        headers={"Authorization": "Bearer a.valid.token"},
    )

    assert response.status_code == 204


def test_oidc_mode_rejects_a_token_that_fails_verification():
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    response = client.post(
        PUSH_PATH,
        content=body(push_envelope()),
        headers={"Authorization": "Bearer forged"},
    )

    assert response.status_code == 401


def test_the_401_precedes_envelope_parsing():
    """An unauthenticated caller must learn nothing about the request's other
    faults — the same ordering ingest uses. A 204 here would also tell an
    attacker their body was malformed, which is information they have not
    earned."""
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    response = client.post(PUSH_PATH, content=b"not json at all")

    assert response.status_code == 401


def test_the_401_precedes_handler_dispatch():
    handler = FakeHandler()
    client = make_app_client("oidc", handler=handler, verify=lambda *a, **k: False)

    client.post(PUSH_PATH, content=body(push_envelope()))

    assert handler.call_count == 0


def test_the_401_carries_no_detail_about_why():
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    response = client.post(
        PUSH_PATH,
        content=body(push_envelope()),
        headers={"Authorization": "Bearer forged"},
    )

    assert b"forged" not in response.content


def test_a_rejected_request_is_logged_at_warning():
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    with capture_processor_output() as stream:
        client.post(PUSH_PATH, content=body(push_envelope()), headers={"Authorization": "Bearer x"})

    assert [line for line in emitted(stream) if line["severity"] == "WARNING"]


def test_the_token_never_reaches_the_logs():
    """A push token is a bearer credential. It is short-lived, but it is valid
    now, and logs outlive it."""
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    with capture_processor_output() as stream:
        client.post(
            PUSH_PATH,
            content=body(push_envelope()),
            headers={"Authorization": "Bearer SECRET-TOKEN-8fe31a9c"},
        )

    assert "SECRET-TOKEN-8fe31a9c" not in stream.getvalue()


def test_healthz_stays_open_in_oidc_mode():
    """Cloud Run's prober carries no identity, so authenticating this would
    make the revision permanently unhealthy."""
    client = make_app_client("oidc", verify=lambda *a, **k: False)

    assert client.get("/healthz").status_code == 200


def test_verify_is_called_from_exactly_one_place():
    """The seam only stays a seam if there is one call site to change."""
    from tests.conftest import REPO_ROOT

    source = (REPO_ROOT / "processor" / "app.py").read_text(encoding="utf-8")

    assert source.count("verify(") == 1


# -- verify_push_token -----------------------------------------------------------


def verify(authorization, **overrides):
    from processor.security import verify_push_token

    kwargs = {
        "audience": AUDIENCE,
        "service_account": SERVICE_ACCOUNT,
        "decode": lambda token, audience: claims(),
        "now": lambda: 1_700_000_000.0,
        **overrides,
    }
    return verify_push_token(authorization, **kwargs)


def test_a_well_formed_token_verifies():
    assert verify("Bearer a.valid.token") is True


def test_the_bearer_scheme_is_case_insensitive():
    """RFC 7235: the auth scheme is case-insensitive, and nothing guarantees
    which casing a client sends."""
    assert verify("bearer a.valid.token") is True


@pytest.mark.parametrize(
    "header",
    [None, "", "   ", "a.valid.token", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "],
)
def test_a_missing_or_malformed_header_fails(header):
    assert verify(header) is False


def test_a_token_for_a_different_service_account_fails():
    """The whole point: only OUR push subscription may invoke this. Any Google
    account can mint a validly-signed token."""
    other = claims(email="someone-else@evil.example")

    assert verify("Bearer t", decode=lambda t, a: other) is False


def test_an_unverified_email_claim_fails():
    assert verify("Bearer t", decode=lambda t, a: claims(email_verified=False)) is False


def test_a_missing_email_claim_fails():
    bad = claims()
    del bad["email"]
    assert verify("Bearer t", decode=lambda t, a: bad) is False


def test_an_expired_token_fails():
    """Checked here as well as in the decoder, so the expiry rule does not
    depend on which library is doing the decoding."""
    assert verify("Bearer t", decode=lambda t, a: claims(exp=1_600_000_000)) is False


def test_a_token_with_no_expiry_fails():
    bad = claims()
    del bad["exp"]
    assert verify("Bearer t", decode=lambda t, a: bad) is False


def test_a_decoder_that_raises_is_a_failure_not_a_crash():
    """An invalid signature raises. A 500 here would be a nack, so Pub/Sub
    would redeliver a forged message forever."""

    def boom(token, audience):
        raise ValueError("invalid signature")

    assert verify("Bearer t", decode=boom) is False


def test_the_audience_is_passed_to_the_decoder():
    """Without it, a token minted for a DIFFERENT Cloud Run service verifies
    here — which is the whole reason `aud` exists."""
    seen = {}

    def record(token, audience):
        seen["audience"] = audience
        return claims()

    verify("Bearer t", decode=record)

    assert seen["audience"] == AUDIENCE


def test_the_token_is_passed_to_the_decoder_without_the_scheme():
    seen = {}

    def record(token, audience):
        seen["token"] = token
        return claims()

    verify("Bearer a.valid.token", decode=record)

    assert seen["token"] == "a.valid.token"


# -- dependencies -----------------------------------------------------------------


def test_the_oidc_dependency_is_an_optional_extra():
    """So `iam`/`none` deployments and the test suite install nothing extra —
    the same reason `google-cloud-pubsub` is the `[gcp]` extra on the ingest
    side."""
    import tomllib

    from tests.conftest import REPO_ROOT

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"].get("optional-dependencies", {})

    assert "oidc" in extras, "declare an [oidc] extra"
    assert any("google-auth" in spec for spec in extras["oidc"])

    base = " ".join(data["project"]["dependencies"])
    assert "google" not in base, "the base install must stay free of GCP libraries"
