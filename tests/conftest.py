"""Fixtures and helpers shared across every component's suite.

LAYOUT
------
    tests/
      conftest.py            <- this file: repo-wide, component-agnostic
      test_dependencies.py      manifest + stdlib integrity (applies to all)
      ingest/                   the webhook receiver
      processor/                the Pub/Sub subscriber
      contract/                 the envelope both sides must agree on

Anything specific to one component belongs in that component's own
``conftest.py``, not here — pytest layers them automatically, so a fixture
defined in ``tests/ingest/conftest.py`` is invisible to the processor suite.

Terraform is deliberately absent. `terraform validate` / `fmt` / `tflint` run
as their own CI job: wiring them into pytest would mean this suite needs a
Terraform toolchain on PATH to run at all.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def package_sources(package: str) -> list[pathlib.Path]:
    """Every ``.py`` file in a top-level package, read from disk.

    Source-level checks (no hardcoded config, no ``print()``) read files rather
    than importing them. Importing would pull in optional extras — the concrete
    Pub/Sub client needs ``[gcp]`` — and fail for reasons unrelated to what is
    under test.
    """
    return sorted((REPO_ROOT / package).rglob("*.py"))
