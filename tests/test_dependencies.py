"""Issue #4 — the dependency set must not shadow the standard library.

`futures==3.*` resolves to futures-3.0.5, which installs a top-level
`concurrent/` package into site-packages: the Python 2 backport of
`concurrent.futures`. `Callable==0.1.2` is an unrelated third-party package
standing in for `typing.Callable`. Both were in the original manifest, and a
public template propagates them to everyone who clones it.
"""

from __future__ import annotations

import importlib.metadata as md
import pathlib
import sysconfig

BANNED = {"futures", "callable"}


def _installed_names() -> set[str]:
    return {(d.metadata["Name"] or "").strip().lower() for d in md.distributions()}


def test_python2_backport_and_stdlib_impostors_are_not_installed():
    present = BANNED & _installed_names()

    assert not present, (
        f"{sorted(present)} installed. `concurrent.futures` and `typing.Callable` "
        "are stdlib; these PyPI packages are a Python 2 backport and an unrelated "
        "library respectively."
    )


def test_concurrent_futures_resolves_to_the_standard_library():
    import concurrent.futures

    stdlib = pathlib.Path(sysconfig.get_paths()["stdlib"]).resolve()
    actual = pathlib.Path(concurrent.futures.__file__).resolve()

    assert stdlib in actual.parents, (
        f"concurrent.futures loaded from {actual}, not the stdlib at {stdlib} — "
        "something has shadowed it"
    )


def test_declared_dependencies_exclude_the_banned_packages():
    import re
    import tomllib

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    declared = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared += extra

    names = {re.split(r"[<>=~!\[; ]", spec.strip(), maxsplit=1)[0].lower() for spec in declared}

    assert not (BANNED & names), f"{sorted(BANNED & names)} declared in pyproject.toml"


def test_dependencies_are_not_bare_wildcards():
    """`3.*` let pip backtrack to a 2015 release with no requires-python bound.
    That is how the Python 2 backport got installed under Python 3."""
    import tomllib

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    loose = [d for d in data["project"].get("dependencies", []) if d.rstrip().endswith(".*")]

    assert not loose, f"pin these to a compatible-release range: {loose}"


def test_terraform_versions_are_pinned_to_a_major():
    """Same lesson as the Python manifest above, on the other toolchain.

    An open `>= 6.0` lets a major provider release land in a `terraform init`
    on a Tuesday and change what `plan` says about resources nobody touched.
    `~> 6.0` takes minors and patches and stops at 7.

    Text, not a parser: reading HCL would mean a dependency, and running
    Terraform from pytest would mean the Python suite needs a Terraform
    toolchain on PATH — which is exactly what `.github/workflows/terraform.yml`
    exists to avoid.
    """
    import re

    versions = pathlib.Path(__file__).resolve().parents[1] / "infra" / "versions.tf"
    if not versions.exists():
        return  # infra/ is optional; the accelerator is usable without it

    declared = re.findall(r'version\s*=\s*"([^"]+)"', versions.read_text(encoding="utf-8"))

    assert declared, "no version constraints found in infra/versions.tf"
    unpinned = [v for v in declared if not v.startswith("~>") and "<" not in v]
    assert not unpinned, (
        f"unbounded Terraform version constraints: {unpinned}. Use `~> X.Y`, "
        "or an explicit upper bound, so a major release cannot arrive unasked."
    )


def test_terraform_state_is_not_committed():
    """State holds every attribute of every managed resource in plaintext,
    including anything a provider marks sensitive. This repo is public."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        return  # nothing to check without git; CI always has it

    root = pathlib.Path(__file__).resolve().parents[1]
    tracked = subprocess.run(  # noqa: S603 - fixed argv, resolved binary, no shell
        [git, "ls-files"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.splitlines()

    leaked = [f for f in tracked if f.endswith((".tfstate", ".tfstate.backup"))]
    leaked += [f for f in tracked if f.endswith("terraform.tfvars")]

    assert not leaked, f"these must never be committed: {leaked}"


def test_legacy_requirements_file_is_gone():
    """A stale ingest/requirements.txt would still be found by a `pip install -r`
    in a Dockerfile, reintroducing everything above."""
    legacy = pathlib.Path(__file__).resolve().parents[1] / "ingest" / "requirements.txt"

    assert not legacy.exists(), f"{legacy} still present; dependencies belong in pyproject.toml"
