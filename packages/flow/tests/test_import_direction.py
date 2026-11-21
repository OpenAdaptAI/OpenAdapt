"""Flow's layers import downward only, and low modules stay cheap to import.

``import_layers.json`` lists five layers, lowest first: foundations, policy
and formats, runtime, products, surfaces (openadapt-internal
``docs/design/monorepo-refactor-2026-11/PLAN.md``, section 3). A module may
import from its own layer or a lower one. An import of a higher layer, at
module level or inside a function, is an upward edge.

* Every upward edge that exists today is listed in
  ``import_direction_debt.json``. A new one fails. A listed edge that no
  longer exists fails too, so the list only shrinks: delete its row.
* ``import_time_budgets`` caps how many ``openadapt_flow`` modules a fresh
  interpreter has loaded after importing each listed module. Over budget
  fails; under budget prints the new number so the change can lower it.

Modules that match no layer pattern are not checked yet; the plan assigns them
as code moves. ``tests/test_import_layers.py`` keeps its own, separate rule
that core code never reaches app, benchmark, UI or lab code.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1]
CONTRACT = FLOW / "import_layers.json"
DEBT = FLOW / "import_direction_debt.json"
PACKAGE = "openadapt_flow"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def module_names(root: Path, package: str = PACKAGE) -> dict[str, Path]:
    names = {}
    for path in (root / package).rglob("*.py"):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names[".".join(parts)] = path
    return names


def layer_of(module: str, layers: list[dict]) -> int | None:
    """Index of the layer whose longest pattern matches ``module``."""
    best: tuple[int, int] | None = None
    for index, layer in enumerate(layers):
        for pattern in layer["modules"]:
            if pattern.endswith("*"):
                hit = module.startswith(pattern[:-1])
            else:
                hit = module == pattern or module.startswith(pattern + ".")
            if hit and (best is None or len(pattern) > best[0]):
                best = (len(pattern), index)
    return None if best is None else best[1]


def _targets(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def upward_edges(root: Path, layers: list[dict]) -> set[str]:
    """``importer -> imported`` for every import of a higher layer."""
    modules = module_names(root)

    def resolve(name: str) -> str | None:
        while name not in modules:
            if "." not in name:
                return None
            name = name.rsplit(".", 1)[0]
        return name

    edges = set()
    for module, path in modules.items():
        low = layer_of(module, layers)
        if low is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for target in _targets(tree):
            imported = resolve(target)
            if imported is None or imported == module:
                continue
            high = layer_of(imported, layers)
            if high is not None and high > low:
                edges.add(f"{module} -> {imported}")
    return edges


def import_time_count(module: str) -> int:
    """How many openadapt_flow modules a fresh interpreter loads for ``module``."""
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({module!r})\n"
        f"print(sum(1 for k in sys.modules if k == {PACKAGE!r} "
        f"or k.startswith({PACKAGE + '.'!r})))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=FLOW,
    )
    return int(result.stdout.strip().splitlines()[-1])


def test_no_new_upward_import() -> None:
    contract = _contract()
    debt = set(json.loads(DEBT.read_text(encoding="utf-8"))["edges"])
    current = upward_edges(FLOW, contract["layers"])
    new = sorted(current - debt)
    assert new == [], (
        f"imports from a lower layer into a higher one: {new}. Move the shared "
        "code down a layer instead of importing up."
    )
    gone = sorted(debt - current)
    assert gone == [], (
        f"these listed upward edges no longer exist; delete them from "
        f"{DEBT.name}: {gone}"
    )


def test_every_layer_pattern_names_a_module() -> None:
    modules = module_names(FLOW)
    layers = _contract()["layers"]
    unused = [
        pattern
        for layer in layers
        for pattern in layer["modules"]
        if not any(layer_of(m, [{"modules": [pattern]}]) == 0 for m in modules)
    ]
    assert unused == [], f"layer patterns that match no module: {unused}"


@pytest.mark.parametrize("module", sorted(_contract()["import_time_budgets"]))
def test_import_time_budget(module: str, capsys: pytest.CaptureFixture[str]) -> None:
    budget = _contract()["import_time_budgets"][module]
    count = import_time_count(module)
    assert count <= budget, (
        f"importing {module} loads {count} openadapt_flow modules, budget "
        f"{budget}. Import the new dependency inside the function that needs it, "
        "or move it lower; never raise the budget."
    )
    if count < budget:
        with capsys.disabled():
            print(f"\n{module}: {count} modules, budget {budget}; lower it to {count}")


def test_checker_finds_upward_and_ignores_downward(tmp_path: Path) -> None:
    pkg = tmp_path / PACKAGE
    (pkg / "low").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "low" / "__init__.py").write_text("")
    (pkg / "low" / "a.py").write_text(
        "def f():\n    from openadapt_flow.high import thing\n"
    )
    (pkg / "high.py").write_text("from openadapt_flow.low import a\nthing = 1\n")
    (pkg / "other.py").write_text("import openadapt_flow.high\n")
    layers = [
        {"name": "low", "modules": ["openadapt_flow.low"]},
        {"name": "high", "modules": ["openadapt_flow.high*"]},
    ]
    assert upward_edges(tmp_path, layers) == {
        "openadapt_flow.low.a -> openadapt_flow.high"
    }


def test_longest_pattern_decides_the_layer() -> None:
    layers = [
        {"name": "low", "modules": ["openadapt_flow.runtime.authorization"]},
        {"name": "mid", "modules": ["openadapt_flow.runtime"]},
        {"name": "high", "modules": ["openadapt_flow.cli_*"]},
    ]
    assert layer_of("openadapt_flow.runtime.authorization.x", layers) == 0
    assert layer_of("openadapt_flow.runtime.replayer", layers) == 1
    assert layer_of("openadapt_flow.cli_undo", layers) == 2
    assert layer_of("openadapt_flow.runtimes", layers) is None
