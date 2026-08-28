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


def test_legacy_requirements_file_is_gone():
    """A stale ingest/requirements.txt would still be found by a `pip install -r`
    in a Dockerfile, reintroducing everything above."""
    legacy = pathlib.Path(__file__).resolve().parents[1] / "ingest" / "requirements.txt"

    assert not legacy.exists(), f"{legacy} still present; dependencies belong in pyproject.toml"
