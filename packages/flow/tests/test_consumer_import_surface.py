"""Every Flow import path another package or repository uses keeps working.

The monorepo refactor (openadapt-internal ``docs/design/monorepo-refactor-
2026-11/PLAN.md``) moves code out of large Flow modules and leaves shims at the
old paths. ``test_import_surface.py`` pins the names of four large modules.
This test pins what Flow's consumers import from any Flow module:

* Desktop (``packages/desktop/openadapt_desktop``), the launcher
  (``openadapt/``), Verify (``packages/verify/src``), Agent
  (``packages/agent/src``) and Tray (``packages/tray/src``), read from source
  here, including imports inside functions;
* openadapt-cloud, which runs released Flow wheels. Its imports are collected
  from a checkout when the snapshot is regenerated, because the repository is
  not available to this test.

Each entry is a module path, or a ``module:name`` pair for ``from module import
name``. The snapshot is ``fixtures/consumer_import_surface.json``. Regenerate it
after a deliberate change, and say why in the pull request:

    OPENADAPT_WRITE_CONSUMER_SURFACE=1 \\
    OPENADAPT_CLOUD_CHECKOUT=/path/to/openadapt-cloud \\
        pytest tests/test_consumer_import_surface.py

Regeneration reads ``origin/main`` of the Cloud checkout and keeps the Cloud
entries already pinned when no checkout is given. An entry is pinned unless it
fails to import on this tree when the snapshot is written; entries that already fail
(for example a guarded import of a name an older Flow had) are listed under
``not_importable_when_written`` and are not checked. An entry that needs
a third-party module this environment lacks (an optional extra) is skipped,
not failed.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1]
REPO = FLOW.parents[1]
SNAPSHOT = Path(__file__).parent / "fixtures" / "consumer_import_surface.json"
CONSUMERS = {
    "desktop": "packages/desktop/openadapt_desktop",
    "launcher": "openadapt",
    "verify": "packages/verify/src",
    "agent": "packages/agent/src",
    "tray": "packages/tray/src",
}
CLOUD = "openadapt-cloud"


def flow_imports(source: str) -> set[str]:
    """``module`` and ``module:name`` entries for every openadapt_flow import."""
    entries: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return entries
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "openadapt_flow":
                    entries.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module.split(".")[0] != "openadapt_flow":
                continue
            for alias in node.names:
                if alias.name != "*":
                    entries.add(f"{node.module}:{alias.name}")
    return entries


def _monorepo(root: str) -> set[str]:
    entries: set[str] = set()
    for path in (REPO / root).rglob("*.py"):
        if {".venv", "node_modules", "tests"} & set(path.parts):
            continue
        entries |= flow_imports(path.read_text(encoding="utf-8", errors="replace"))
    return entries


def _cloud(checkout: Path) -> set[str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    entries: set[str] = set()
    for name in git("ls-tree", "-r", "--name-only", "origin/main").splitlines():
        if name.endswith(".py"):
            entries |= flow_imports(git("show", f"origin/main:{name}"))
    return entries


class MissingDependency(Exception):
    """A third-party module the entry needs is not installed here."""


def _import(module: str) -> object:
    try:
        return importlib.import_module(module)
    except ImportError as error:
        missing = (error.name or "").split(".")[0]
        if missing and not missing.startswith("openadapt"):
            # An optional extra (a browser driver, a console server) that this
            # environment lacks. That says nothing about Flow's paths.
            raise MissingDependency(missing) from error
        raise


def importable(entry: str) -> bool:
    """Whether ``entry`` imports; raises MissingDependency for an absent extra."""
    module, _, name = entry.partition(":")
    try:
        imported = _import(module)
    except ImportError:
        return False
    if not name or hasattr(imported, name):
        return True
    try:
        _import(f"{module}.{name}")
    except ImportError:
        return False
    return True


def _broken(entries: Iterable[str]) -> list[str]:
    broken = []
    for entry in entries:
        try:
            if not importable(entry):
                broken.append(entry)
        except MissingDependency:
            continue
    return broken


def _split(entries: Iterable[str]) -> tuple[list[str], list[str]]:
    failing = _broken(sorted(set(entries)))
    return [e for e in sorted(set(entries)) if e not in failing], failing


def _snapshot() -> dict[str, dict[str, list[str]]]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_snapshot_is_current_when_regenerating() -> None:
    if os.environ.get("OPENADAPT_WRITE_CONSUMER_SURFACE") != "1":
        pytest.skip("set OPENADAPT_WRITE_CONSUMER_SURFACE=1 to regenerate")
    found = {name: _monorepo(root) for name, root in CONSUMERS.items()}
    checkout = os.environ.get("OPENADAPT_CLOUD_CHECKOUT")
    if checkout:
        found[CLOUD] = _cloud(Path(checkout))
    elif SNAPSHOT.exists():
        old = _snapshot().get(CLOUD, {})
        found[CLOUD] = set(old.get("pinned", [])) | set(
            old.get("not_importable_when_written", [])
        )
    else:
        pytest.fail("the first snapshot needs OPENADAPT_CLOUD_CHECKOUT")
    snapshot = {}
    for consumer, entries in sorted(found.items()):
        ok, failing = _split(entries)
        snapshot[consumer] = {"pinned": ok, "not_importable_when_written": failing}
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1) + "\n", encoding="utf-8")


@pytest.mark.parametrize("consumer", sorted([*CONSUMERS, CLOUD]))
def test_every_pinned_consumer_import_still_works(consumer: str) -> None:
    pinned = _snapshot()[consumer]["pinned"]
    broken = _broken(pinned)
    assert broken == [], (
        f"{consumer} imports {broken}, which no longer import. A move must leave a "
        "shim at the old path; see this test's docstring to regenerate after a "
        "deliberate removal."
    )


@pytest.mark.parametrize("consumer", sorted(CONSUMERS))
def test_current_monorepo_consumer_imports_work(consumer: str) -> None:
    """Imports added since the snapshot was written must work too."""
    known_failing = set(_snapshot()[consumer]["not_importable_when_written"])
    current = _monorepo(CONSUMERS[consumer]) - known_failing
    broken = _broken(sorted(current))
    assert broken == [], f"{consumer} imports {broken}, which do not import"


def test_flow_imports_reads_every_form() -> None:
    source = (
        "import openadapt_flow.ir\n"
        "from openadapt_flow import receipt as r\n"
        "from openadapt_types import x\n"
        "from . import local\n"
        "def f():\n"
        "    from openadapt_flow.runtime.replayer import (Replayer,\n"
        "        _ProgramHalt)\n"
    )
    assert flow_imports(source) == {
        "openadapt_flow.ir",
        "openadapt_flow:receipt",
        "openadapt_flow.runtime.replayer:Replayer",
        "openadapt_flow.runtime.replayer:_ProgramHalt",
    }


def test_importable_checks_names_and_submodules() -> None:
    assert importable("openadapt_flow.ir:Workflow")
    assert importable("openadapt_flow:ir")
    assert not importable("openadapt_flow.ir:NoSuchName")
    assert not importable("openadapt_flow.no_such_module")
