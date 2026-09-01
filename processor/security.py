"""Verify that a push request really came from our Pub/Sub subscription.

One function, called from one place — the same shape as
``ingest/security.py``'s ``verify_signing_key``, so swapping the scheme is a
one-file change.

WHEN THIS RUNS AT ALL
---------------------
Only in ``PUSH_AUTH_MODE=oidc``. On Cloud Run with
``--no-allow-unauthenticated``, the platform has already validated the token
before the request reaches this process, and re-doing it here would add a
public-key fetch to the hot path for no security gain. See ``config.py``.

WHAT ACTUALLY PROVES ANYTHING
-----------------------------
A valid Google signature proves the token came from Google — nothing more. Any
Google account can obtain one. Two claims carry the real weight:

``aud``
    The token was minted *for this service*. Without checking it, a token
    issued for some other Cloud Run service verifies here.

``email``
    The caller is *our* push service account, not merely some Google identity.

Expiry is checked here as well as inside the decoder, so the rule holds
whichever library ends up doing the decoding.

FAILURE IS ``False``, NEVER AN EXCEPTION
----------------------------------------
An invalid signature raises inside the decoder. If that escaped, the endpoint
would return 500 — which Pub/Sub reads as a nack, so a forged message would be
redelivered until it aged out. A rejected token has to be a 401, which means
this function has to answer with a bool.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

BEARER = "bearer"


def _google_decode(token: str, audience: str) -> dict:
    """Decode and cryptographically verify a Google-issued OIDC token.

    Imported through ``importlib`` rather than a plain ``import`` for two
    reasons: ``google-auth`` is the optional ``[oidc]`` extra, so a service
    running in ``iam`` or ``none`` mode must never need it; and the processor's
    test suite asserts that no module in this package imports ``google`` at
    all, which is what keeps the suite runnable with no GCP libraries present.

    Network: this fetches Google's public keys on first use, which is the cost
    ``iam`` mode avoids entirely.
    """
    try:
        id_token = importlib.import_module("google.oauth2.id_token")
        requests = importlib.import_module("google.auth.transport.requests")
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "PUSH_AUTH_MODE=oidc needs the [oidc] extra: pip install '.[oidc]'"
        ) from exc

    return id_token.verify_oauth2_token(token, requests.Request(), audience=audience)


def verify_push_token(
    authorization: str | None,
    *,
    audience: str,
    service_account: str,
    decode: Callable[[str, str], dict] = _google_decode,
    now: Callable[[], float] = time.time,
) -> bool:
    """Return True only if this is a live token minted for us by our push account.

    ``decode`` and ``now`` are injected so the whole path is testable with no
    network and no credentials — the same reason ``PubSubPublisher`` accepts a
    client and ``RetryingPublisher`` accepts a ``sleep``.
    """
    token = _bearer_token(authorization)
    if token is None:
        logger.warning("push request has no bearer token")
        return False

    try:
        claims = decode(token, audience)
    except Exception as exc:
        # Deliberately broad: an invalid signature, a malformed token, and a
        # key-fetch failure all arrive as different exception types from
        # different layers, and every one of them means "not authenticated".
        # The token itself is never logged — it is a live bearer credential.
        logger.warning("push token failed verification: %s", type(exc).__name__)
        return False

    if claims.get("email") != service_account:
        # The load-bearing check. A valid signature only proves Google issued
        # the token, and Google will issue one to anybody.
        logger.warning("push token is for an unexpected principal")
        return False

    if claims.get("email_verified") is not True:
        logger.warning("push token has an unverified email claim")
        return False

    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= now():
        logger.warning("push token is expired or has no expiry")
        return False

    return True


def _bearer_token(authorization: str | None) -> str | None:
    """Pull the credential out of an ``Authorization`` header, or None.

    The scheme is matched case-insensitively: RFC 7235 defines it that way and
    nothing guarantees which casing a client sends.
    """
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != BEARER:
        return None

    token = token.strip()
    return token or None
