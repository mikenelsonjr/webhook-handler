"""Authentication for inbound webhook deliveries.

Aptly sends a *static token* in an ``x-signingkey`` header. It computes no
signature over the body, so there is nothing to verify against one and
authentication reduces to comparing that token against the configured secret.

See CONTEXT.md for why this is weaker than HMAC and what compensates for it. A
consumer whose provider does sign should replace this module's one function
with an HMAC comparison over the raw body; the route calls nothing else.
"""

from __future__ import annotations

import hmac


def verify_signing_key(expected: str, provided: str | None) -> bool:
    """Return True only if ``provided`` is exactly the configured signing key.

    Never raises. Header values are arbitrary caller-controlled input, so every
    malformed one must produce a clean 401 — a crash here would be a
    denial-of-service anyone could trigger by sending a odd header.

    An empty ``expected`` fails closed. Without that check an unconfigured
    service would authenticate a caller who simply sends an empty header to
    match it, turning a missing configuration value into an open endpoint.
    """
    if not expected or not provided:
        return False

    try:
        expected_bytes = expected.encode("utf-8")
        provided_bytes = provided.encode("utf-8")
    except (AttributeError, UnicodeError):
        return False

    # Compared as bytes, not str: hmac.compare_digest raises TypeError on
    # non-ASCII str input. compare_digest keeps the comparison constant-time,
    # so response timing does not reveal how many leading characters matched.
    return hmac.compare_digest(expected_bytes, provided_bytes)
