"""Processor configuration, built from a mapping and validated once at startup.

``from_env`` is a pure function of the mapping it is handed: no ``os.environ``,
no ``.env`` loading, no import-time I/O — the same rule as ``ingest/config.py``.
That is what lets tests pass an explicit dict, and it keeps a developer's local
``.env`` from leaking into a test run.

THE AUTH MODE HAS NO DEFAULT, ON PURPOSE
----------------------------------------
There is no safe value to fall back to. Defaulting to ``iam`` would mean a
service deployed without IAM silently trusts anyone who can reach it;
defaulting to ``oidc`` would fail every local run. So the variable is required,
and an absent or unrecognised value raises here — at import, from the
entrypoint — which crashes the container at startup and leaves Cloud Run
serving the previous revision. A typo cannot ship an open endpoint.

WHY ``iam`` AND ``none`` ARE SEPARATE VALUES
--------------------------------------------
They do the same nothing in this process. They are still distinct because a
config value is documentation the operator writes, and these describe opposite
situations: ``iam`` means Cloud Run authenticated the caller before the request
arrived, ``none`` means nothing did. Merging them into one name — an earlier
draft called it ``trusted`` — makes a production deployment and a laptop look
identical in ``--set-env-vars``, and hides the alarming reading behind the
harmless one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: The platform authenticated the caller before the request arrived. Cloud Run
#: with ``--no-allow-unauthenticated`` and ``roles/run.invoker`` on the push
#: service account — the intended production posture.
IAM = "iam"

#: Nothing authenticates this endpoint. Legitimate for the local emulator,
#: which sends no token, and an alarm anywhere else.
NONE = "none"

#: Verify the bearer token in this process. For hosting that is not Cloud Run,
#: or as defence in depth. Needs the ``[oidc]`` extra.
OIDC = "oidc"

PUSH_AUTH_MODES = (IAM, NONE, OIDC)


@dataclass(frozen=True)
class Settings:
    """Immutable, fully-populated configuration.

    By the time one of these exists, every field is present and valid — so no
    caller downstream ever has to check for missing config. In particular, in
    ``oidc`` mode both claims are guaranteed non-empty, because verifying a
    token against an unset expectation is not verification.
    """

    push_auth_mode: str
    push_service_account: str | None = None
    push_audience: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Build from environment-style values; raise if anything is missing or wrong."""
        # Case-folded because `--set-env-vars PUSH_AUTH_MODE=IAM` should not
        # fail a revision over capitalisation.
        mode = env.get("PUSH_AUTH_MODE", "").strip().lower()
        if mode not in PUSH_AUTH_MODES:
            valid = ", ".join(PUSH_AUTH_MODES)
            raise ValueError(
                f"PUSH_AUTH_MODE must be one of {valid}; got {mode or '<unset>'}"
            )

        def require(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ValueError(f"{key} is required when PUSH_AUTH_MODE={OIDC}")
            return value

        if mode != OIDC:
            return cls(push_auth_mode=mode)

        return cls(
            push_auth_mode=mode,
            push_service_account=require("PUSH_SERVICE_ACCOUNT"),
            push_audience=require("PUSH_AUDIENCE"),
        )
