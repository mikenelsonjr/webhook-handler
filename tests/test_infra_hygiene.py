"""Repo-wide guards on the Terraform, read as text.

These do not run Terraform — that is `.github/workflows/terraform.yml`'s job,
and requiring a Terraform toolchain on PATH would make the Python suite
unrunnable for anyone who does not have one. What is checked here is the class
of mistake `validate` and `tflint` are both blind to, because the configuration
would be perfectly valid: it would just publish a secret, or open an endpoint.

Everything is skipped if `infra/` is absent, so the accelerator stays usable
for a consumer who deletes it and brings their own.
"""

from __future__ import annotations

import pathlib

INFRA = pathlib.Path(__file__).resolve().parents[1] / "infra"


def infra_sources() -> list[pathlib.Path]:
    return sorted(INFRA.rglob("*.tf")) if INFRA.exists() else []


def test_terraform_does_not_manage_a_secret_version():
    """A `google_secret_manager_secret_version` with real data writes the
    secret into Terraform state in plaintext.

    State holds every attribute of every managed resource, including anything
    the provider marks sensitive. So the value would live wherever state lives,
    in every backup of it, and in the scrollback of anyone who has run
    `terraform show` — which is strictly worse than not using Secret Manager,
    because it looks managed.

    The first version goes in out of band with `gcloud`.
    """
    offenders = [
        f"{path.name}:{n}"
        for path in infra_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith('resource "google_secret_manager_secret_version"')
    ]

    assert not offenders, (
        f"secret version managed by Terraform at {offenders}; its value would be "
        "written into state in plaintext"
    )


def test_no_secret_data_attribute_anywhere():
    """The same rule, one level down: any attribute literally carrying secret
    material is the thing state would capture."""
    offenders = [
        f"{path.name}:{n}"
        for path in infra_sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "secret_data" in line and not line.lstrip().startswith("#")
    ]

    assert not offenders, f"secret material assigned in Terraform at {offenders}"


def test_secret_access_is_granted_on_one_secret_not_the_project():
    """A project-level `secretAccessor` lets a compromised receiver read every
    secret in the project, which for most projects includes the database
    password."""
    offenders = []
    for path in infra_sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('resource "google_project_iam_') and "secret" in stripped:
                offenders.append(f"{path.name}:{n}")

    assert not offenders, f"project-level secret access at {offenders}"
