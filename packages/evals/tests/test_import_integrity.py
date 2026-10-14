"""Static import-integrity checks for the openadapt_evals package.

Guards against the failure class behind OpenAdaptAI/OpenAdapt#999:
``from openadapt_evals.cloud.local import serve_dashboard`` parsed fine,
only exploded at call time, and a bare ``except ImportError`` reported
it as "openadapt-ml not installed". Imports inside function bodies are
invisible to plain import-the-module tests, so these checks walk the
AST instead and need no heavy runtime dependencies.

Two checks:

1. test_no_phantom_imports — every ``from openadapt_evals.x import y``
   anywhere in the package (including inside functions) names something
   that actually exists in the target module.
2. test_no_phantom_kwargs — every call to a function imported from an
   internal module passes only keyword arguments that exist in that
   function's signature. Conservative: decorated functions, classes,
   and functions taking **kwargs are skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_NAME = "openadapt_evals"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent / PACKAGE_NAME

# Known-acceptable exceptions, as (module, imported_name). Keep empty
# unless a module defines names dynamically in a way the AST walk
# cannot see.
PHANTOM_IMPORT_ALLOWLIST: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Module discovery
# ---------------------------------------------------------------------------


def _module_map() -> dict[str, Path]:
    """Map dotted module names to file paths for the whole package."""
    modules: dict[str, Path] = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        rel = path.relative_to(PACKAGE_ROOT.parent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


MODULES = _module_map()


# ---------------------------------------------------------------------------
# Definition collection
# ---------------------------------------------------------------------------


def _collect_defined(tree: ast.Module) -> tuple[set[str], bool]:
    """Names defined at module level, and whether the module is dynamic.

    Walks module-level statements, descending into If/Try/With bodies
    (TYPE_CHECKING guards, optional-import fallbacks) but not into
    function or class bodies. A module is "dynamic" if it star-imports
    or defines module-level __getattr__; we skip checking those.
    """
    defined: set[str] = set()
    dynamic = False

    def visit_body(body: list[ast.stmt]) -> None:
        nonlocal dynamic
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
                if node.name == "__getattr__":
                    dynamic = True
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for name_node in ast.walk(target):
                        if isinstance(name_node, ast.Name):
                            defined.add(name_node.id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        dynamic = True
                    else:
                        defined.add(alias.asname or alias.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for sub in ast.iter_child_nodes(node):
                    if isinstance(sub, list):
                        continue
                visit_body(getattr(node, "body", []))
                visit_body(getattr(node, "orelse", []))
                visit_body(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    visit_body(handler.body)

    visit_body(tree.body)
    return defined, dynamic


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


_DEFINED_CACHE: dict[str, tuple[set[str], bool]] = {}


def _defined_in(module: str) -> tuple[set[str], bool] | None:
    """Defined names for a module in the package, or None if not ours."""
    if module not in MODULES:
        return None
    if module not in _DEFINED_CACHE:
        _DEFINED_CACHE[module] = _collect_defined(_parse(MODULES[module]))
    return _DEFINED_CACHE[module]


def _resolve_relative(current_module: str, node: ast.ImportFrom) -> str | None:
    """Resolve a (possibly relative) ImportFrom to a dotted module name."""
    if node.level == 0:
        return node.module
    parts = current_module.split(".")
    # level=1 from a module means its containing package; packages
    # (__init__) count as themselves.
    if MODULES.get(current_module, Path()).name != "__init__.py":
        parts = parts[:-1]
    cut = node.level - 1
    if cut:
        parts = parts[:-cut] if cut <= len(parts) else []
    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base or None


# ---------------------------------------------------------------------------
# Check 1: phantom imports
# ---------------------------------------------------------------------------


def test_no_phantom_imports():
    problems: list[str] = []

    for current, path in sorted(MODULES.items()):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _resolve_relative(current, node)
            if not target or not (target == PACKAGE_NAME or target.startswith(PACKAGE_NAME + ".")):
                continue
            info = _defined_in(target)
            if info is None:
                # Importing from a module we can't find at all.
                if target in MODULES or f"{target}.__init__" in MODULES:
                    continue
                problems.append(f"{path}:{node.lineno}: imports from missing module '{target}'")
                continue
            defined, dynamic = info
            if dynamic:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in defined:
                    continue
                # Importing a submodule: from openadapt_evals.cloud import local
                if f"{target}.{alias.name}" in MODULES:
                    continue
                if (target, alias.name) in PHANTOM_IMPORT_ALLOWLIST:
                    continue
                problems.append(
                    f"{path}:{node.lineno}: 'from {target} import "
                    f"{alias.name}' but '{alias.name}' is not defined in "
                    f"{MODULES[target]}"
                )

    assert not problems, (
        "Phantom imports detected (names imported from internal modules "
        "that do not exist there). These typically only explode at call "
        "time and get masked by 'except ImportError':\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# Check 2: phantom keyword arguments
# ---------------------------------------------------------------------------


def _function_params(module: str, func_name: str) -> set[str] | None:
    """Param names of an undecorated top-level function, else None.

    None means "cannot safely check" (missing, decorated, a class,
    has **kwargs, or module is dynamic).
    """
    info = _defined_in(module)
    if info is None or info[1]:
        return None
    tree = _parse(MODULES[module])
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            if node.decorator_list or node.args.kwarg is not None:
                return None
            params = [a.arg for a in node.args.posonlyargs]
            params += [a.arg for a in node.args.args]
            params += [a.arg for a in node.args.kwonlyargs]
            return set(params)
    return None


def test_no_phantom_kwargs():
    problems: list[str] = []

    for current, path in sorted(MODULES.items()):
        tree = _parse(path)

        # local alias -> (target_module, original_name), from ALL
        # ImportFroms in the file, including inside function bodies.
        imported: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_relative(current, node)
                if target and (target == PACKAGE_NAME or target.startswith(PACKAGE_NAME + ".")):
                    for alias in node.names:
                        if alias.name != "*":
                            imported[alias.asname or alias.name] = (
                                target,
                                alias.name,
                            )

        if not imported:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in imported:
                continue
            target_module, original = imported[node.func.id]
            params = _function_params(target_module, original)
            if params is None:
                continue
            for kw in node.keywords:
                if kw.arg is not None and kw.arg not in params:
                    problems.append(
                        f"{path}:{node.lineno}: call to "
                        f"{target_module}.{original}(... {kw.arg}=...) but "
                        f"its parameters are {sorted(params)}"
                    )

    assert not problems, (
        "Keyword arguments passed to internal functions that do not "
        "accept them (TypeError at call time):\n  " + "\n  ".join(problems)
    )
