"""The core runtime and verifier never import app, benchmark, UI or lab code.

``import_layers.json`` (next to ``pyproject.toml``) names the core modules and
four forbidden groups: app-specific, benchmark, desktop-UI, and lab and
harness code. The checks here read every module's imports from its source, so
nothing is imported to run them. A lazy import inside a function counts: it
runs on the same code path, just later. Imports a package ``__getattr__``
re-exports count where a caller asks for the name.

Core may not import a forbidden module directly, with no exceptions. Core may
not reach one through any chain of ``openadapt_flow`` imports either; an edge
that cannot be removed yet is listed in the contract's ``exceptions`` with a
reason and an issue, and an exception that no longer exists fails too, so the
list only shrinks.
"""

from __future__ import annotations

import ast
import json
from collections import deque
from pathlib import Path

import pytest

import openadapt_flow

ROOT = Path(openadapt_flow.__file__).parent
CONTRACT = ROOT.parent / "import_layers.json"

Graph = dict[str, dict[str, list[int]]]


def _module_names(root: Path, package: str) -> dict[str, Path]:
    mods: dict[str, Path] = {}
    for path in root.rglob("*.py"):
        parts = [package, *path.relative_to(root).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts.pop()
        mods[".".join(parts)] = path
    return mods


def _lazy_exports(tree: ast.Module, module: str) -> dict[str, str]:
    """Names a package ``__getattr__`` imports on demand, mapped to modules."""

    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    for target in _from_targets(sub, module, is_package=True):
                        out[target.rpartition(".")[2]] = target
    return out


def _from_targets(node: ast.ImportFrom, module: str, *, is_package: bool) -> list[str]:
    if node.level:
        base = module.split(".") if is_package else module.split(".")[:-1]
        base = base[: len(base) - (node.level - 1)]
        prefix = ".".join(base + ([node.module] if node.module else []))
    else:
        prefix = node.module or ""
    return [f"{prefix}.{alias.name}" for alias in node.names] + [prefix]


def build_graph(root: Path, package: str) -> Graph:
    """Map each module to the package modules (or ``benchmark``) it imports."""

    mods = _module_names(root, package)
    trees = {
        name: ast.parse(path.read_text(encoding="utf-8")) for name, path in mods.items()
    }
    lazy = {
        name: _lazy_exports(trees[name], name)
        for name, path in mods.items()
        if path.name == "__init__.py"
    }

    def resolve(name: str) -> str | None:
        while name and name not in mods:
            name = name.rpartition(".")[0]
        return name or None

    graph: Graph = {}
    for name, tree in trees.items():
        is_package = mods[name].name == "__init__.py"
        skip: set[int] = set()
        if is_package:
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                    skip |= {id(sub) for sub in ast.walk(node)}
        out: dict[str, set[int]] = {}
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            raw: list[str] = []
            if isinstance(node, ast.Import):
                raw = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                raw = _from_targets(node, name, is_package=is_package)
                prefix = raw[-1]
                for alias in node.names:
                    if alias.name in lazy.get(prefix, {}):
                        raw.append(lazy[prefix][alias.name])
            for target in raw:
                if target == "benchmark" or target.startswith("benchmark."):
                    out.setdefault("benchmark", set()).add(node.lineno)
                    continue
                if not (target == package or target.startswith(package + ".")):
                    continue
                resolved = resolve(target)
                # Importing a.b.c runs a and a.b first.
                while resolved:
                    if resolved != name:
                        out.setdefault(resolved, set()).add(node.lineno)
                    resolved = resolved.rpartition(".")[0]
        graph[name] = {k: sorted(v) for k, v in out.items()}
    return graph


def _under(module: str, prefixes: list[str]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _forbidden_group(module: str, forbidden: dict[str, list[str]]) -> str | None:
    for group, prefixes in forbidden.items():
        if _under(module, prefixes):
            return group
    return None


def _forbidden_imports(graph: Graph, importer: str, contract: dict) -> list[str]:
    """Forbidden modules ``importer`` names, deepest only.

    ``import a.b.c`` also runs ``a.b``; report ``a.b.c`` once, not both.
    """

    hits = [
        m for m in graph.get(importer, {}) if _forbidden_group(m, contract["forbidden"])
    ]
    return sorted(m for m in hits if not any(h.startswith(m + ".") for h in hits))


def direct_violations(graph: Graph, contract: dict) -> list[str]:
    found = []
    for importer in sorted(graph):
        if not _under(importer, contract["core"]):
            continue
        for imported in _forbidden_imports(graph, importer, contract):
            group = _forbidden_group(imported, contract["forbidden"])
            lines = graph[importer][imported]
            found.append(f"{importer} -> {imported} ({group}, lines {lines})")
    return found


def reached_edges(graph: Graph, contract: dict) -> dict[tuple[str, str], list[str]]:
    """Every edge into forbidden code reachable from core, with one path to it."""

    forbidden = contract["forbidden"]
    parent: dict[str, str | None] = {
        m: None for m in sorted(graph) if _under(m, contract["core"])
    }
    queue = deque(parent)
    edges: dict[tuple[str, str], list[str]] = {}
    while queue:
        module = queue.popleft()
        for imported in _forbidden_imports(graph, module, contract):
            path, step = [imported], module
            while step is not None:
                path.append(step)
                step = parent[step]
            edges.setdefault((module, imported), path[::-1])
        for imported in sorted(graph.get(module, {})):
            if _forbidden_group(imported, forbidden):
                continue
            if imported not in parent and imported in graph:
                parent[imported] = module
                queue.append(imported)
    return edges


def transitive_violations(graph: Graph, contract: dict) -> list[str]:
    allowed = {(e["importer"], e["imported"]) for e in contract["exceptions"]}
    return [
        " > ".join(path)
        for edge, path in sorted(reached_edges(graph, contract).items())
        if edge not in allowed
    ]


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def graph() -> Graph:
    return build_graph(ROOT, "openadapt_flow")


def test_core_imports_nothing_forbidden_directly(graph: Graph, contract: dict) -> None:
    assert direct_violations(graph, contract) == []


def test_core_reaches_nothing_forbidden(graph: Graph, contract: dict) -> None:
    assert transitive_violations(graph, contract) == [], (
        "fix the import, or list the edge in import_layers.json with an issue"
    )


def test_every_exception_is_documented_and_still_needed(
    graph: Graph, contract: dict
) -> None:
    reached = reached_edges(graph, contract)
    for exception in contract["exceptions"]:
        edge = (exception["importer"], exception["imported"])
        assert edge in reached, f"stale exception, remove it: {edge}"
        assert _forbidden_group(edge[1], contract["forbidden"])
        assert not _under(edge[0], contract["core"]), "core has no exceptions"
        assert exception["reason"].strip()
        assert exception["issue"].strip()


def test_contract_names_real_modules(graph: Graph, contract: dict) -> None:
    names = set(graph) | {"benchmark"}
    prefixes = list(contract["core"])
    for group in contract["forbidden"].values():
        prefixes += group
    for prefix in prefixes:
        assert any(_under(m, [prefix]) for m in names), f"no module {prefix}"


# The checker itself, against a planted package.

_PLANTED = {
    "__init__.py": "",
    "core/__init__.py": "",
    "core/engine.py": "from planted import helper\n",
    "helper.py": "def run():\n    from planted.demo import app\n    return app\n",
    "demo/__init__.py": "",
    "demo/app.py": "",
    "ui/__init__.py": (
        "def __getattr__(name):\n"
        "    if name == 'window':\n"
        "        from planted.ui.window import window\n"
        "        return window\n"
        "    raise AttributeError(name)\n"
    ),
    "ui/window.py": "window = 1\n",
    "ui/text.py": "TEXT = 'ok'\n",
}

_PLANTED_CONTRACT = {
    "core": ["planted.core"],
    "forbidden": {"app": ["planted.demo"], "ui": ["planted.ui.window"]},
    "exceptions": [],
}


def _plant(tmp_path: Path, **changes: str) -> Graph:
    root = tmp_path / "planted"
    for rel, text in {**_PLANTED, **changes}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return build_graph(root, "planted")


def test_checker_finds_a_lazy_chain_into_forbidden_code(tmp_path: Path) -> None:
    graph = _plant(tmp_path)
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine > planted.helper > planted.demo.app"
    ]
    excepted = {
        **_PLANTED_CONTRACT,
        "exceptions": [{"importer": "planted.helper", "imported": "planted.demo.app"}],
    }
    assert transitive_violations(graph, excepted) == []


def test_checker_finds_a_direct_import(tmp_path: Path) -> None:
    graph = _plant(
        tmp_path,
        **{"core/engine.py": "import planted.demo.app\n", "helper.py": ""},
    )
    assert direct_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine -> planted.demo.app (app, lines [1])",
    ]


def test_checker_follows_a_lazy_package_reexport(tmp_path: Path) -> None:
    clean = _plant(tmp_path, **{"helper.py": "from planted.ui import text\n"})
    assert transitive_violations(clean, _PLANTED_CONTRACT) == []
    graph = _plant(tmp_path, **{"helper.py": "from planted.ui import window\n"})
    assert transitive_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine > planted.helper > planted.ui.window"
    ]
