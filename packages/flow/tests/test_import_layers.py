"""The core runtime and verifier never import app, benchmark, UI or lab code.

``import_layers.json`` (next to ``pyproject.toml``) names the core modules and
four forbidden groups: app-specific, benchmark, desktop-UI, and lab and
harness code. The checks here read every module's imports from its source, so
nothing is imported to run them. A lazy import inside a function counts: it
runs on the same code path, just later. So does ``importlib.import_module`` or
``__import__`` with a literal module name; one built at run time can't be
read. Imports a package ``__getattr__`` re-exports (including through a
``_LAZY_EXPORTS``-style table) count where a caller asks for the name.

Core may not import a forbidden module directly, with no exceptions. Core may
not reach one through any chain of ``openadapt_flow`` imports either; an edge
that cannot be removed yet is listed in the contract's ``exceptions`` with a
reason and an issue, and an exception that no longer exists fails too, so the
list only shrinks.
"""

from __future__ import annotations

import ast
import json
import re
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


_DYNAMIC = {"import_module", "__import__"}


_IMPORTER_MODULES = ("importlib", "builtins")
_IMPORTER_NAMES = {
    ("importlib", "import_module"),
    ("importlib", "__import__"),
    ("builtins", "__import__"),
}


class _Scope:
    """One lexical scope: the module, a function or lambda, or a class body."""

    def __init__(self, kind: str, parent: _Scope | None) -> None:
        self.kind = kind
        self.parent = parent
        # name -> [(line, what the binding makes it)]; "import_module" or
        # "__import__" for an importer, "module" for importlib/builtins,
        # "other" for anything else.
        self.bindings: dict[str, list[tuple[int, str]]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()


class Importers:
    """What each call's own lexical scope makes an importing call.

    A plain name is an importer where the scope Python resolves it in binds it
    to ``importlib.import_module`` or ``__import__`` (``from importlib import
    import_module as load``), or where no scope binds ``__import__`` (the
    builtin). ``m.import_module``/``m.__import__`` imports where ``m`` resolves
    to ``importlib`` or ``builtins``. A shadow or an alias in one function
    does not change what the same name means in another; within the call's
    own scope the latest binding above the call wins, and in an enclosing
    scope any importer binding counts. A same-named local function or an
    unrelated method is not an importer.
    """

    def __init__(self, tree: ast.AST) -> None:
        self._scope_of: dict[int, _Scope] = {}
        self._module = _Scope("module", None)
        self._visit(tree, self._module)

    def _bind(self, scope: _Scope, name: str, what: str, line: int) -> None:
        if name in scope.globals:
            scope = self._module
        elif name in scope.nonlocals:
            outer = scope.parent
            while outer is not None and outer.kind != "function":
                outer = outer.parent
            scope = outer or self._module
        scope.bindings.setdefault(name, []).append((line, what))

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        self._scope_of[id(node)] = scope
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                self._bind(scope, node.name, "other", line)
                for sub in [*node.decorator_list, node.returns]:
                    if sub is not None:
                        self._visit(sub, scope)
            args = node.args
            for sub in [*args.defaults, *(d for d in args.kw_defaults if d)]:
                self._visit(sub, scope)
            inner = _Scope("function", scope)
            for arg in [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *([args.vararg] if args.vararg else []),
                *([args.kwarg] if args.kwarg else []),
            ]:
                self._bind(inner, arg.arg, "other", line)
            body = node.body if isinstance(node.body, list) else [node.body]
            for sub in body:
                self._visit(sub, inner)
            return
        if isinstance(node, ast.ClassDef):
            self._bind(scope, node.name, "other", line)
            for sub in [*node.decorator_list, *node.bases, *node.keywords]:
                self._visit(sub, scope)
            inner = _Scope("class", scope)
            for sub in node.body:
                self._visit(sub, inner)
            return
        if isinstance(node, ast.Global):
            scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            scope.nonlocals.update(node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                if alias.asname:
                    what = "module" if alias.name in _IMPORTER_MODULES else "other"
                    self._bind(scope, alias.asname, what, line)
                else:
                    what = "module" if top in _IMPORTER_MODULES else "other"
                    self._bind(scope, top, what, line)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                what = (
                    alias.name
                    if not node.level and (node.module, alias.name) in _IMPORTER_NAMES
                    else "other"
                )
                self._bind(scope, alias.asname or alias.name, what, line)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(scope, node.id, "other", line)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, "other", line)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            self._bind(scope, node.name, "other", line)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(scope, node.rest, "other", line)
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _resolve(self, node: ast.AST, name: str) -> set[str]:
        """What ``name`` may be where ``node`` reads it; empty for the builtin."""

        scope: _Scope | None = self._scope_of.get(id(node), self._module)
        line = getattr(node, "lineno", 0)
        own = True
        while scope is not None:
            if name in scope.globals and scope is not self._module:
                scope, own = self._module, False
                continue
            if name in scope.nonlocals:
                scope, own = scope.parent, False
                continue
            if (own or scope.kind != "class") and name in scope.bindings:
                binds = scope.bindings[name]
                if own:
                    prior = [what for at, what in binds if at <= line]
                    if prior:
                        return {prior[-1]}
                    if scope.kind == "function":
                        return {what for _, what in binds}
                else:
                    return {what for _, what in binds}
            scope, own = scope.parent, False
        return set()

    def called(self, call: ast.Call) -> str | None:
        """``import_module`` or ``__import__`` when ``call`` is one, else None."""

        func = call.func
        if isinstance(func, ast.Name):
            found = self._resolve(call, func.id)
            if not found:
                return "__import__" if func.id == "__import__" else None
            return next((what for what in sorted(found) if what in _DYNAMIC), None)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _DYNAMIC
            and isinstance(func.value, ast.Name)
            and "module" in self._resolve(call, func.value.id)
        ):
            return func.attr
        return None


def _str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _arg(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _dynamic_targets(
    node: ast.AST, module: str, importers: Importers, *, is_package: bool
) -> list[str]:
    """Modules a call ``import_module(...)`` or ``__import__(...)`` loads, when
    its module name is a string literal, positional or ``name=``; ``[]``
    when it can't be read or the call is not an importer."""

    if not isinstance(node, ast.Call):
        return []
    called = importers.called(node)
    name = _str(_arg(node, 0, "name"))
    if called is None or not name:
        return []
    if called == "import_module" and name.startswith("."):
        package = _arg(node, 1, "package")
        if _str(package):
            base = _str(package)
        elif isinstance(package, ast.Name) and package.id == "__name__":
            base = module
        elif isinstance(package, ast.Name) and package.id == "__package__":
            base = module if is_package else module.rpartition(".")[0]
        else:
            return []
        level = len(name) - len(name.lstrip("."))
        parts = base.split(".")
        parts = parts[: len(parts) - (level - 1)]
        name = ".".join(parts + ([name.lstrip(".")] if name.lstrip(".") else []))
    out = [name]
    if called == "__import__":
        fromlist = _arg(node, 3, "fromlist")
        if isinstance(fromlist, (ast.List, ast.Tuple)):
            out += [f"{name}.{_str(e)}" for e in fromlist.elts if _str(e)]
    return out


def _const_table(node: ast.AST) -> dict[str, str]:
    """A dict literal of strings, or ``{n: "mod" for n in ("a", "b")}``."""

    if isinstance(node, ast.Dict):
        return {
            _str(k): _str(v)
            for k, v in zip(node.keys, node.values)
            if _str(k) and _str(v)
        }
    if (
        isinstance(node, ast.DictComp)
        and len(node.generators) == 1
        and isinstance(node.generators[0].target, ast.Name)
        and isinstance(node.key, ast.Name)
        and node.key.id == node.generators[0].target.id
        and _str(node.value)
        and isinstance(node.generators[0].iter, (ast.Tuple, ast.List))
    ):
        names = [_str(e) for e in node.generators[0].iter.elts]
        return {n: _str(node.value) for n in names if n}
    return {}


def _module_tables(tree: ast.Module) -> dict[str, dict[str, str]]:
    """Top-level string tables (``_LAZY_EXPORTS = {...}`` and its ``.update``)."""

    tables: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "update"
            and isinstance(node.value.func.value, ast.Name)
            and len(node.value.args) == 1
        ):
            name = node.value.func.value.id
            tables.setdefault(name, {}).update(_const_table(node.value.args[0]))
            continue
        else:
            continue
        if isinstance(target, ast.Name):
            tables[target.id] = _const_table(value)
    return tables


def _guard_names(test: ast.AST) -> set[str]:
    """String literals an ``if`` compares against: ``name == "X"``, ``in (...)``."""

    found: set[str] = set()
    for sub in ast.walk(test):
        if isinstance(sub, ast.Compare):
            for side in [sub.left, *sub.comparators]:
                elts = (
                    side.elts
                    if isinstance(side, (ast.Tuple, ast.List, ast.Set))
                    else [side]
                )
                found |= {_str(e) for e in elts if _str(e)}
    return found


def _fed_tables(
    func: ast.FunctionDef, tables: set[str], importers: Importers
) -> set[str]:
    """Tables whose values ``func`` passes to an importing call.

    The module name is the table read itself (``_T[name]``, ``_T.get(name)``)
    or a local assigned from one, directly or through other locals; a table
    read for anything else is not an import.
    """

    def reads(expr: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(expr) if isinstance(n, ast.Name) and n.id in tables
        }

    # Each local's sources: the names its assigned values read, so a table
    # read forwarded through other locals (``m = _T[n]; t = m``) still counts.
    sources: dict[str, set[str]] = {}
    for node in ast.walk(func):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name):
                sources.setdefault(target.id, set()).update(
                    n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)
                )
    fed: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or importers.called(node) is None:
            continue
        arg = _arg(node, 0, "name")
        if arg is None:
            continue
        fed |= reads(arg)
        seen: set[str] = set()
        pending = [n.id for n in ast.walk(arg) if isinstance(n, ast.Name)]
        while pending:
            local = pending.pop()
            if local in seen:
                continue
            seen.add(local)
            if local in tables:
                fed.add(local)
            pending.extend(sources.get(local, ()))
    return fed


def _lazy_exports(tree: ast.Module, module: str) -> dict[str, set[str]]:
    """Names a package ``__getattr__`` imports on demand, mapped to modules.

    Read from ``from m import n [as a]`` (the name it binds), ``import a.b``
    and literal ``import_module``/``__import__`` calls (the module's last
    name, any ``as`` name, and every name an enclosing ``if`` compares
    against), and the string tables, such as ``_LAZY_EXPORTS``, whose values
    ``__getattr__`` passes to an importing call.
    """

    out: dict[str, set[str]] = {}
    tables = _module_tables(tree)
    importers = Importers(tree)

    def add(key: str, target: str) -> None:
        out.setdefault(key, set()).add(target)

    def visit(node: ast.AST, guards: set[str]) -> None:
        if isinstance(node, ast.ImportFrom):
            targets = _from_targets(node, module, is_package=True)
            for alias, target in zip(node.names, targets):
                for key in {alias.asname or alias.name, *guards}:
                    add(key, target)
            add(targets[-1].rpartition(".")[2], targets[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for key in {alias.name.rpartition(".")[2], alias.asname, *guards}:
                    if key:
                        add(key, alias.name)
        for target in _dynamic_targets(node, module, importers, is_package=True):
            for key in {target.rpartition(".")[2], *guards}:
                add(key, target)
        inner = guards | _guard_names(node.test) if isinstance(node, ast.If) else guards
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            visit(node, set())
            for table in _fed_tables(node, set(tables), importers):
                for key, target in tables[table].items():
                    add(key, target)
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
        importers = Importers(tree)
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
                    raw.extend(sorted(lazy.get(prefix, {}).get(alias.name, ())))
            else:
                raw = _dynamic_targets(node, name, importers, is_package=is_package)
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
    """Every edge into forbidden code reachable from core, with one path to it.

    The walk continues through an excepted edge, so an exception admits only
    that edge: whatever the excepted module imports in turn is still checked.
    """

    forbidden = contract["forbidden"]
    allowed = {(e["importer"], e["imported"]) for e in contract["exceptions"]}
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
            if _forbidden_group(imported, forbidden) and not any(
                (module, target) in allowed
                and (target == imported or target.startswith(imported + "."))
                for target in _forbidden_imports(graph, module, contract)
            ):
                # Not walked past, unless an exception admits this edge
                # (or the deeper edge it is the package of: importing a.b.c
                # runs a.b too).
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


def policy_problems(contract: object) -> list[str]:
    """Why ``contract`` is not a complete policy; empty when it is.

    An empty section would make every check pass vacuously.
    """

    if not isinstance(contract, dict):
        return ["the contract is not an object"]

    def names(value: object) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(v, str) and v for v in value)
        )

    problems = []
    if not names(contract.get("core")):
        problems.append("core must be a nonempty list of module names")
    forbidden = contract.get("forbidden")
    if not isinstance(forbidden, dict) or not forbidden:
        problems.append("forbidden must be a nonempty object of groups")
    else:
        for group, prefixes in forbidden.items():
            if not group or not names(prefixes):
                problems.append(f"forbidden group {group!r} names no modules")
    exceptions = contract.get("exceptions")
    if not isinstance(exceptions, list) or not all(
        isinstance(e, dict)
        and isinstance(e.get("importer"), str)
        and isinstance(e.get("imported"), str)
        for e in exceptions
    ):
        problems.append("exceptions must be a list of importer/imported edges")
    return problems


@pytest.fixture(scope="module")
def contract() -> dict:
    loaded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert policy_problems(loaded) == []
    return loaded


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
        assert re.fullmatch(
            r"https://github\.com/OpenAdaptAI/OpenAdapt/issues/\d+", exception["issue"]
        ), f"exception needs a public issue link: {edge}"


def name_problems(graph: Graph, contract: dict) -> list[str]:
    """Contract names that match no module; empty when all do.

    Every core name must select a module of the package: a core list that
    selects nothing would check nothing. A forbidden name may also be the
    top-level ``benchmark`` package, which is outside it.
    """

    problems = [
        f"no core module {prefix}"
        for prefix in contract["core"]
        if not any(_under(m, [prefix]) for m in graph)
    ]
    names = set(graph) | {"benchmark"}
    for group in contract["forbidden"].values():
        problems += [
            f"no module {prefix}"
            for prefix in group
            if not any(_under(m, [prefix]) for m in names)
        ]
    return problems


def test_contract_names_real_modules(graph: Graph, contract: dict) -> None:
    assert name_problems(graph, contract) == []


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


# Dynamic imports with a literal module name are imports too.


@pytest.mark.parametrize(
    "engine",
    [
        "import importlib\n"
        "def go():\n    return importlib.import_module('planted.demo.app')\n",
        "from importlib import import_module\n"
        "def go():\n    return import_module('.app', 'planted.demo')\n",
        "from importlib import import_module\n"
        "def go():\n    return import_module('planted.demo.app', package=None)\n",
        "import sys\ndef go():\n    return __import__('planted.demo.app')\n",
        "import sys\ndef go():\n    return __import__('planted.demo', fromlist=['app'])\n",
        # a function alias, a module alias, and the module name as a keyword
        "from importlib import import_module as load\n"
        "def go():\n    return load('planted.demo.app')\n",
        "import importlib as il\ndef go():\n    return il.import_module('planted.demo.app')\n",
        "import importlib\n"
        "def go():\n    return importlib.import_module(name='planted.demo.app')\n",
        "import sys\ndef go():\n    return __import__(name='planted.demo.app')\n",
    ],
)
def test_checker_finds_a_literal_dynamic_import(tmp_path: Path, engine: str) -> None:
    graph = _plant(tmp_path, **{"core/engine.py": engine})
    assert direct_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine -> planted.demo.app (app, lines [3])",
    ]


@pytest.mark.parametrize(
    "engine",
    [
        # a local function that happens to be named import_module
        "def import_module(name):\n    return name\n"
        "def go():\n    return import_module('planted.demo.app')\n",
        # an unrelated method of that name
        "def go(registry):\n    return registry.import_module('planted.demo.app')\n",
        # a module that defines its own __import__
        "def __import__(name):\n    return name\n"
        "def go():\n    return __import__('planted.demo.app')\n",
        # a nested function that only returns the module's name
        "def go():\n    def name():\n        return 'planted.demo.app'\n    return name()\n",
    ],
)
def test_checker_ignores_a_string_that_is_not_imported(
    tmp_path: Path, engine: str
) -> None:
    graph = _plant(tmp_path, **{"core/engine.py": engine, "helper.py": ""})
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


# A binding means what Python's lexical scoping makes it mean where it's read.


@pytest.mark.parametrize(
    "engine",
    [
        # a shadow inside one function leaves the builtin in another
        "def other():\n"
        "    def __import__(name):\n"
        "        return name\n"
        "    return __import__('planted.ui.text')\n"
        "def go():\n    return __import__('planted.demo.app')\n",
        "def other():\n"
        "    __import__ = str\n"
        "    return __import__('planted.ui.text')\n"
        "def go():\n    return __import__('planted.demo.app')\n",
        # an alias bound in the calling function, after a non-importer
        "def go():\n"
        "    load = str\n"
        "    from importlib import import_module as load\n"
        "    return load('planted.demo.app')\n",
        # an alias bound in an enclosing function
        "def go():\n"
        "    from importlib import import_module as load\n"
        "    def inner():\n"
        "        return load('planted.demo.app')\n"
        "    return inner()\n",
    ],
    ids=["nested-def-shadow", "local-shadow", "rebound-alias", "enclosing-alias"],
)
def test_checker_resolves_an_importer_in_its_own_scope(
    tmp_path: Path, engine: str
) -> None:
    graph = _plant(tmp_path, **{"core/engine.py": engine, "helper.py": ""})
    line = next(
        n for n, text in enumerate(engine.splitlines(), 1) if "demo.app" in text
    )
    assert direct_violations(graph, _PLANTED_CONTRACT) == [
        f"planted.core.engine -> planted.demo.app (app, lines [{line}])",
    ]


@pytest.mark.parametrize(
    "engine",
    [
        # an importer alias bound only inside another function
        "def load(name):\n    return name\n"
        "def other():\n"
        "    from importlib import import_module as load\n"
        "    return load('planted.ui.text')\n"
        "def go():\n    return load('planted.demo.app')\n",
        # the alias rebound to something else before the call
        "def go():\n"
        "    from importlib import import_module as load\n"
        "    load = str\n"
        "    return load('planted.demo.app')\n",
        # a class-body alias is not visible inside its methods
        "def load(name):\n    return name\n"
        "class C:\n"
        "    from importlib import import_module as load\n"
        "    def go(self):\n"
        "        return load('planted.demo.app')\n",
        # importlib bound in another function only
        "def other():\n"
        "    import importlib\n"
        "    return importlib.import_module('planted.ui.text')\n"
        "def go(importlib):\n    return importlib.import_module('planted.demo.app')\n",
    ],
    ids=["alias-in-other-function", "rebound", "class-body", "module-param"],
)
def test_checker_ignores_a_name_another_scope_binds(
    tmp_path: Path, engine: str
) -> None:
    graph = _plant(tmp_path, **{"core/engine.py": engine, "helper.py": ""})
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


def test_checker_ignores_a_dynamic_import_it_cannot_read(tmp_path: Path) -> None:
    engine = (
        "import importlib\ndef go(name):\n    return importlib.import_module(name)\n"
    )
    graph = _plant(tmp_path, **{"core/engine.py": engine})
    assert direct_violations(graph, _PLANTED_CONTRACT) == []


@pytest.mark.parametrize(
    "ui",
    [
        # import x.y inside the package __getattr__
        "def __getattr__(name):\n"
        "    if name == 'Window':\n"
        "        import planted.ui.window\n"
        "        return planted.ui.window.window\n"
        "    raise AttributeError(name)\n",
        # a guard name that is not the module's own name
        "def __getattr__(name):\n"
        "    if name in ('Window', 'other'):\n"
        "        import planted.ui.window as w\n"
        "        return w.window\n"
        "    raise AttributeError(name)\n",
        # a literal import_module inside __getattr__
        "import importlib\n"
        "def __getattr__(name):\n"
        "    if name == 'Window':\n"
        "        return importlib.import_module('planted.ui.window').window\n"
        "    raise AttributeError(name)\n",
        # a _LAZY_EXPORTS table, as openadapt_flow.runtime does
        "from importlib import import_module\n"
        "_LAZY_EXPORTS = {'TEXT': 'planted.ui.text'}\n"
        "_LAZY_EXPORTS.update({n: 'planted.ui.window' for n in ('Window', 'Frame')})\n"
        "def __getattr__(name):\n"
        "    module_name = _LAZY_EXPORTS.get(name)\n"
        "    if module_name is None:\n"
        "        raise AttributeError(name)\n"
        "    return getattr(import_module(module_name), name)\n",
        # the table read straight into the call
        "import importlib\n"
        "_LAZY_EXPORTS = {'Window': 'planted.ui.window'}\n"
        "def __getattr__(name):\n"
        "    return getattr(importlib.import_module(_LAZY_EXPORTS[name]), name)\n",
        # the table read forwarded through more than one local
        "from importlib import import_module\n"
        "_T = {'Window': 'planted.ui.window'}\n"
        "def __getattr__(name):\n"
        "    module_name = _T[name]\n"
        "    target = module_name\n"
        "    return getattr(import_module(target), name)\n",
        # a from-import re-exported under another name, with no guard
        "def __getattr__(name):\n"
        "    from planted.ui.window import window as Window\n"
        "    return {'Window': Window}[name]\n",
    ],
)
def test_checker_follows_every_kind_of_lazy_reexport(tmp_path: Path, ui: str) -> None:
    # Window is an exported name, not a module, so only the lazy table finds it.
    graph = _plant(
        tmp_path,
        **{"ui/__init__.py": ui, "helper.py": "from planted.ui import Window\n"},
    )
    assert transitive_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine > planted.helper > planted.ui.window"
    ]
    clean = _plant(
        tmp_path,
        **{"ui/__init__.py": ui, "helper.py": "from planted.ui import text\n"},
    )
    assert transitive_violations(clean, _PLANTED_CONTRACT) == []


def test_checker_ignores_a_table_no_import_reads(tmp_path: Path) -> None:
    # A string table __getattr__ returns from, without importing its values.
    ui = (
        "from importlib import import_module\n"
        "_DOCS = {'Window': 'planted.ui.window'}\n"
        "def __getattr__(name):\n"
        "    if name in _DOCS:\n"
        "        return _DOCS[name]\n"
        "    return import_module('planted.ui.text')\n"
    )
    graph = _plant(
        tmp_path,
        **{"ui/__init__.py": ui, "helper.py": "from planted.ui import Window\n"},
    )
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


def test_a_core_name_must_select_a_package_module(tmp_path: Path) -> None:
    # The external benchmark package may be forbidden, but as the only core
    # name it selects nothing, and a core import of the app would pass.
    graph = _plant(tmp_path, **{"core/engine.py": "import planted.demo.app\n"})
    selects_nothing = {**_PLANTED_CONTRACT, "core": ["benchmark"]}
    assert policy_problems(selects_nothing) == []
    assert direct_violations(graph, selects_nothing) == []
    assert name_problems(graph, selects_nothing) == ["no core module benchmark"]
    assert name_problems(graph, _PLANTED_CONTRACT) == []
    forbids_benchmark = {
        **_PLANTED_CONTRACT,
        "forbidden": {**_PLANTED_CONTRACT["forbidden"], "bench": ["benchmark"]},
    }
    assert name_problems(graph, forbids_benchmark) == []


@pytest.mark.parametrize(
    "policy",
    [
        {"core": [], "forbidden": {}, "exceptions": []},
        {**_PLANTED_CONTRACT, "core": []},
        {**_PLANTED_CONTRACT, "forbidden": {}},
        {**_PLANTED_CONTRACT, "forbidden": {"app": []}},
        {**_PLANTED_CONTRACT, "forbidden": {"app": ["planted.demo"], "ui": [""]}},
        {**_PLANTED_CONTRACT, "core": "planted.core"},
        {"forbidden": _PLANTED_CONTRACT["forbidden"], "exceptions": []},
        {"core": ["planted.core"], "forbidden": {"app": ["planted.demo"]}},
        {**_PLANTED_CONTRACT, "exceptions": [{"importer": "planted.helper"}]},
        [],
    ],
)
def test_an_incomplete_policy_is_refused(policy: object) -> None:
    assert policy_problems(policy) != []


def test_a_complete_policy_is_accepted() -> None:
    assert policy_problems(_PLANTED_CONTRACT) == []


def test_an_exception_admits_only_its_own_edge(tmp_path: Path) -> None:
    # core > helper > demo.app is excepted; demo.app's own import of the UI
    # window is not, and must still be reported.
    graph = _plant(
        tmp_path, **{"demo/app.py": "from planted.ui.window import window\n"}
    )
    contract = {
        **_PLANTED_CONTRACT,
        "exceptions": [{"importer": "planted.helper", "imported": "planted.demo.app"}],
    }
    assert transitive_violations(graph, contract) == [
        "planted.core.engine > planted.helper > planted.demo.app > planted.ui.window"
    ]


def test_an_exception_still_admits_its_edge(tmp_path: Path) -> None:
    # The control: with nothing beyond the excepted edge, nothing is reported.
    graph = _plant(tmp_path)
    contract = {
        **_PLANTED_CONTRACT,
        "exceptions": [{"importer": "planted.helper", "imported": "planted.demo.app"}],
    }
    assert transitive_violations(graph, contract) == []
