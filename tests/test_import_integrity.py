"""Static import-integrity checks for the openadapt meta-package.

Issue #999 class of bug: cli.py lazily imported names from openadapt-ml
that didn't exist (`serve_dashboard`, `train_main`). Imports inside
function bodies never execute during plain import tests, so this walks
the AST instead — for the local `openadapt` package AND across the seam
into the installed `openadapt_ml` package.

Cross-package checks skip when openadapt-ml isn't installed; CI
installs it so they always run there.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

LOCAL_PACKAGE = "openadapt"
LOCAL_ROOT = Path(__file__).resolve().parent.parent / LOCAL_PACKAGE

# Every sibling package the meta-package imports from (cli.py and the
# lazy __getattr__ in __init__.py). Cross-package checks skip gracefully
# for any that aren't installed; CI installs all of them.
EXTERNAL_PACKAGES = (
    "openadapt_ml",
    "openadapt_capture",
    "openadapt_evals",
    "openadapt_viewer",
    "openadapt_grounding",
    "openadapt_retrieval",
)

# Optional compatibility imports intentionally support multiple sibling
# versions and are guarded by ImportError at the call site.
PHANTOM_IMPORT_ALLOWLIST: set[tuple[str, str]] = {
    ("openadapt_ml.cloud.local", "resolve_config_path"),
}


def _package_module_map(name: str, root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root.parent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _build_module_map() -> dict[str, Path]:
    modules = _package_module_map(LOCAL_PACKAGE, LOCAL_ROOT)
    for pkg in EXTERNAL_PACKAGES:
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            root = Path(next(iter(spec.submodule_search_locations)))
            modules.update(_package_module_map(pkg, root))
    return modules


MODULES = _build_module_map()
CHECKED_PREFIXES = tuple(
    p
    for p in (LOCAL_PACKAGE, *EXTERNAL_PACKAGES)
    if p == LOCAL_PACKAGE or any(m.startswith(p) for m in MODULES)
)


def _is_checked(target: str) -> bool:
    return any(target == p or target.startswith(p + ".") for p in CHECKED_PREFIXES)


def _collect_defined(tree: ast.Module) -> tuple[set[str], bool]:
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
    if module not in MODULES:
        return None
    if module not in _DEFINED_CACHE:
        _DEFINED_CACHE[module] = _collect_defined(_parse(MODULES[module]))
    return _DEFINED_CACHE[module]


def _resolve_relative(current_module: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    parts = current_module.split(".")
    if MODULES.get(current_module, Path()).name != "__init__.py":
        parts = parts[:-1]
    cut = node.level - 1
    if cut:
        parts = parts[:-cut] if cut <= len(parts) else []
    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base or None


def _local_modules() -> list[tuple[str, Path]]:
    return sorted(
        (m, p)
        for m, p in MODULES.items()
        if m == LOCAL_PACKAGE or m.startswith(LOCAL_PACKAGE + ".")
    )


def test_external_packages_installed_in_ci():
    """In CI, every sibling package must be importable so the
    cross-package seam checks actually run instead of silently
    degrading to skips. (The #999 meta-lesson: a green check that
    verifies nothing is worse than no check.) Locally this is allowed
    to skip."""
    if not os.environ.get("CI"):
        pytest.skip("sibling install only enforced in CI")
    missing = [p for p in EXTERNAL_PACKAGES if importlib.util.find_spec(p) is None]
    assert not missing, (
        f"CI must install all sibling packages so the cross-package "
        f"seam tests run, but these are not importable: {missing}"
    )


def test_no_phantom_imports():
    problems: list[str] = []

    for current, path in _local_modules():
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _resolve_relative(current, node)
            if not target or not _is_checked(target):
                continue
            info = _defined_in(target)
            if info is None:
                if any(m.startswith(target + ".") for m in MODULES):
                    continue
                problems.append(
                    f"{path}:{node.lineno}: imports from missing module '{target}'"
                )
                continue
            defined, dynamic = info
            if dynamic:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in defined:
                    continue
                if f"{target}.{alias.name}" in MODULES:
                    continue
                if (target, alias.name) in PHANTOM_IMPORT_ALLOWLIST:
                    continue
                problems.append(
                    f"{path}:{node.lineno}: 'from {target} import "
                    f"{alias.name}' but '{alias.name}' is not defined "
                    f"there"
                )

    assert not problems, (
        "Phantom imports detected (#999 failure class):\n  " + "\n  ".join(problems)
    )


def _function_params(module: str, func_name: str) -> set[str] | None:
    info = _defined_in(module)
    if info is None or info[1]:
        return None
    tree = _parse(MODULES[module])
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            if node.decorator_list or node.args.kwarg is not None:
                return None
            params = [a.arg for a in node.args.posonlyargs]
            params += [a.arg for a in node.args.args]
            params += [a.arg for a in node.args.kwonlyargs]
            return set(params)
    return None


def test_no_phantom_kwargs():
    problems: list[str] = []

    for current, path in _local_modules():
        tree = _parse(path)

        imported: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_relative(current, node)
                if target and _is_checked(target):
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
                        f"{target_module}.{original}(... {kw.arg}=...) "
                        f"but its parameters are {sorted(params)}"
                    )

    assert not problems, (
        "Phantom keyword arguments (TypeError at call time):\n  "
        + "\n  ".join(problems)
    )
