"""A fresh clone must install from the committed lockfile.

`git clone` followed by the README's `uv sync --extra dev` has to work on a
machine with no other OpenAdapt checkouts next to this one. A `path` source in
`[tool.uv.sources]` that points outside the repository breaks that for anyone
without the matching sibling directory, and any source that uv.lock was not
resolved with makes `uv sync --locked` fail. CI runs `uv lock --check` without
`--no-sources` for the same reason.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 only
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parents[1]


def _uv_sources() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject.get("tool", {}).get("uv", {}).get("sources", {})


def test_no_uv_source_points_outside_the_repository() -> None:
    outside = {}
    for name, spec in _uv_sources().items():
        for item in spec if isinstance(spec, list) else [spec]:
            path = item.get("path") if isinstance(item, dict) else None
            if path is None:
                continue
            resolved = (ROOT / path).resolve()
            if resolved != ROOT and ROOT not in resolved.parents:
                outside[name] = path
    assert outside == {}, (
        f"[tool.uv.sources] points outside the repository: {outside}. A fresh "
        "clone has no sibling checkouts. Install a local copy with "
        "`uv pip install -e ../<repo>` instead of committing it."
    )


def test_every_git_source_is_the_one_the_lockfile_resolved() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    disagree = {}
    for name, spec in _uv_sources().items():
        for item in spec if isinstance(spec, list) else [spec]:
            url = item.get("git") if isinstance(item, dict) else None
            if url is None:
                continue
            block = re.search(
                rf'(?m)^name = "{re.escape(name)}"\nversion = "[^"]+"\nsource = \{{ (\w+) = "([^"]+)"',
                lock,
            )
            if block is None or block.group(1) != "git" or not block.group(2).startswith(url):
                disagree[name] = url
    assert disagree == {}, (
        f"[tool.uv.sources] names git sources that uv.lock did not resolve: {disagree}. "
        "`uv sync --locked` fails in a fresh clone until the two agree."
    )


def test_lockfile_resolves_only_from_registries_and_this_project() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    local = re.findall(r'source = \{ (?:path|directory|editable) = "([^"]+)" \}', lock)
    assert local == ["."], f"uv.lock resolves packages from local paths: {local}"


def test_ci_checks_the_lockfile_the_way_a_fresh_clone_uses_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    checks = re.findall(r"(?m)^\s*(?:run: )?uv lock --check(.*)$", workflow)
    assert checks, "test.yml must run `uv lock --check`"
    assert all("--no-sources" not in rest for rest in checks)
