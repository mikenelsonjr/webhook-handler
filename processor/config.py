"""Processor configuration.

Same shape as ``ingest/config.py``: a frozen dataclass, and ``from_env`` will be
a pure function of the mapping it is handed — no ``os.environ``, no ``.env``
loading, no import-time I/O. Only the entrypoint reads the ambient environment.

This is currently a shell. The endpoint (#11) needs a settings object to hold,
but every field it will carry belongs to a later story: ``PUSH_AUTH_MODE`` and
the OIDC claims are #12, the dedup bounds are #13. ``from_env`` arrives with
#12, because its whole job is validating variables that do not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable, fully-populated configuration.

    Empty by design for now — see the module docstring. Kept as a real type
    rather than passing ``None`` so ``create_app`` has the signature it will
    keep, and #12 fills it in without touching the endpoint's callers.
    """
