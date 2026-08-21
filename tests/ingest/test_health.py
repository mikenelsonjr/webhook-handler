"""Issue #1 — the app must be importable and startable without GCP credentials."""

from __future__ import annotations

import pathlib
import subprocess
import sys


def test_healthz_returns_200(client):
    r = client.get("/healthz")

    assert r.status_code == 200


def test_healthz_needs_no_signature(client):
    """Cloud Run's health probe cannot sign requests."""
    r = client.get("/healthz")

    assert r.status_code == 200
    assert r.status_code != 401


def test_app_imports_without_gcp_credentials():
    """The original built a PublisherClient at module scope, so importing it
    called google.auth.default() and raised DefaultCredentialsError anywhere
    credentials were absent — including CI.

    Runs in a subprocess with every credential-bearing variable stripped, but
    the rest of the environment intact: blanking it entirely breaks the
    interpreter on Windows for reasons unrelated to what is under test.
    """
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GOOGLE_", "GCLOUD_", "CLOUDSDK_", "GCP_"))
    }

    result = subprocess.run(
        [sys.executable, "-c", "import ingest.app; print('ok')"],
        capture_output=True,
        text=True,
        env=env,
        cwd=pathlib.Path(__file__).resolve().parents[1],
    )

    assert result.returncode == 0, f"import failed:\n{result.stderr}"


def test_publisher_module_does_not_import_google():
    """Keeping the protocol free of the concrete client is what lets the test
    suite install without the grpc toolchain."""
    import inspect

    from ingest import publisher

    source = inspect.getsource(publisher)

    assert "google" not in source, (
        "ingest/publisher.py should define the Publisher protocol only; put the "
        "concrete Pub/Sub client in its own module"
    )
