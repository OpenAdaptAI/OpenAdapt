#!/usr/bin/env python3
"""Prove that a change only moves Python code, without editing it.

Used for the move commits of the decomposition plan
(packages/flow/docs/design/DECOMPOSITION.md, section 4.2). For every Python
file that changed between ``--base`` and the working tree, each function,
method and other statement that was in the base version must still exist,
with an identical AST, somewhere under the search roots of the working tree.
Positions, comments and formatting are ignored; names, signatures, decorators
and bodies must match exactly.

A move commit may add three kinds of code, which are reported, not refused:

* imports, and re-export assignments such as ``name = other.name``;
* protocol stubs: functions whose body is only ``...`` or a docstring;
* class headers whose bases changed (``class Replayer(IdentityChecks, ...)``).

Any other new function body fails the check unless ``--allow-new`` is given,
because a move commit must not contain an edit.

It also lists test patches that name a moved module global by its old module
(``"pkg.old.name"`` strings), since the moved code no longer reads that global
from the old module.

Usage:
    python scripts/verify_moved_code.py [--base origin/main] [--root PATH]
        (compares with the merge base of --base and HEAD)
        [--search packages] [--allow-new]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Unit:
    """One comparable piece of code: where it was, and its normalized AST."""

    file: str
    qualname: str
    lineno: int
    dump: str
    is_function: bool
    # Code that runs nothing new: a docstring, an annotation with no value (a
    # protocol attribute), ``pass``, a protocol stub or a re-export.
    is_declaration: bool = False
    # Module-level names a function reads (it looks them up in its module).
    reads: frozenset[str] = frozenset()


@dataclass
class Result:
    missing: list[Unit] = field(default_factory=list)
    new_bodies: list[Unit] = field(default_factory=list)
    new_stubs: list[Unit] = field(default_factory=list)
    changed_bases: list[str] = field(default_factory=list)
    stale_patches: list[str] = field(default_factory=list)
    moved: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.new_bodies


def _dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _is_import(node: ast.stmt) -> bool:
    return isinstance(node, (ast.Import, ast.ImportFrom))


def _is_type_checking_block(node: ast.stmt) -> bool:
    """``if TYPE_CHECKING:``. Its branches are compared statement by statement.

    Such blocks hold imports and the mixin base alias. The ``else`` branch runs
    at import time, so neither branch is skipped as a whole: imports in it are
    ignored and every other statement is a unit like any other. Each unit keeps
    the branch it sits in as part of its identity (see ``units``), so code that
    moves from one branch to the other is a deletion plus new code.
    """
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )


def _has_call(nodes: list[ast.AST]) -> bool:
    """True if any of ``nodes`` contains a call, which runs code."""
    return any(
        isinstance(sub, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr))
        for node in nodes
        for sub in ast.walk(node)
    )


def _is_dotted_name(node: ast.expr) -> bool:
    """``name`` or ``module.sub.name``, with no call or subscript anywhere."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _is_reexport(node: ast.stmt) -> bool:
    """``name = module.name`` or ``name = other_name``: a re-export, not logic.

    ``name = make().value`` is not one: the call runs at import time.
    """
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and _is_dotted_name(node.value)
    )


def _is_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # Decorators, defaults and annotations run when the ``def`` runs.
    header: list[ast.AST] = [*node.decorator_list, node.args]
    if node.returns is not None:
        header.append(node.returns)
    if _has_call(header):
        return False
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]  # docstring
    return all(
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
        for stmt in body
    )


def units(source: str, file: str) -> tuple[list[Unit], dict[str, list[str]]]:
    """Comparable units of a module, and each class's base expressions."""
    tree = ast.parse(source, filename=file)
    out: list[Unit] = []
    bases: dict[str, list[str]] = {}

    def walk(body: list[ast.stmt], prefix: str, scope: str = "") -> None:
        # ``scope`` is the chain of ``TYPE_CHECKING`` branches around ``body``.
        # Whether code runs depends on it, so it is part of every unit's
        # comparable dump: the same statement, function or method in the other
        # branch is a different unit.
        def key(dump: str) -> str:
            return f"{scope}{dump}" if scope else dump

        for node in body:
            if _is_import(node):
                continue
            if _is_type_checking_block(node):
                walk(node.body, prefix, f"{scope}[TYPE_CHECKING:if]")
                walk(node.orelse, prefix, f"{scope}[TYPE_CHECKING:else]")
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                reads = frozenset(
                    sub.id
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                )
                out.append(
                    Unit(
                        file,
                        prefix + node.name,
                        node.lineno,
                        key(_dump(node)),
                        True,
                        is_declaration=_is_stub(node),
                        reads=reads,
                    )
                )
            elif isinstance(node, ast.ClassDef):
                name = prefix + node.name
                bases[name] = [ast.unparse(b) for b in node.bases] + [
                    ast.unparse(k) for k in node.keywords
                ]
                header = [*node.bases, *node.keywords]
                if _has_call(header):
                    # A base built by a call runs code when the class is made,
                    # so it must survive a move unchanged like any statement.
                    out.append(
                        Unit(
                            file,
                            f"{name}(<bases>)",
                            node.lineno,
                            key("\n".join(_dump(n) for n in header)),
                            False,
                        )
                    )
                for deco in node.decorator_list:
                    out.append(
                        Unit(
                            file,
                            f"{name}@{ast.unparse(deco)}",
                            node.lineno,
                            key(_dump(deco)),
                            False,
                        )
                    )
                walk(node.body, name + ".", scope)
            else:
                label = f"{prefix}<statement line {node.lineno}>"
                declaration = (
                    (
                        isinstance(node, ast.AnnAssign)
                        and node.value is None
                        and not _has_call([node.annotation])
                    )
                    or isinstance(node, ast.Pass)
                    or _is_reexport(node)
                    or (
                        isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                    )
                )
                out.append(
                    Unit(file, label, node.lineno, key(_dump(node)), False, declaration)
                )

    walk(tree.body, "")
    return out, bases


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _in_roots(path: str, search: list[str]) -> bool:
    """Tests and scripts are outside the roots: retargeted patches are edits."""
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in search)


def changed_python_files(root: Path, base: str, search: list[str]) -> list[str]:
    names = _git(root, "diff", "--name-only", base, "--", "*.py").split()
    return sorted(n for n in names if _in_roots(n, search) and "/tests/" not in n)


def base_source(root: Path, base: str, path: str) -> str | None:
    try:
        return _git(root, "show", f"{base}:{path}")
    except subprocess.CalledProcessError:
        return None  # new file


def _module_name(path: str) -> str | None:
    """``packages/flow/openadapt_flow/runtime/replayer.py`` -> dotted module."""
    parts = Path(path).with_suffix("").parts
    for i, part in enumerate(parts):
        if part.startswith("openadapt"):
            return ".".join(parts[i:])
    return None


def verify(root: Path, base: str, search: list[str]) -> Result:
    result = Result()
    changed = changed_python_files(root, base, search)

    # Code in the working tree that the base did not have, in changed or new files.
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", "*.py")
    all_changed = set(changed) | {
        n for n in untracked.split() if _in_roots(n, search) and "/tests/" not in n
    }

    head_by_dump: dict[str, list[Unit]] = {}
    for search_root in search:
        for path in sorted((root / search_root).rglob("*.py")):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            try:
                found, _ = units(path.read_text(encoding="utf-8"), rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for unit in found:
                head_by_dump.setdefault(unit.dump, []).append(unit)

    # Each old unit must be matched by its own copy in the working tree: a
    # surviving identical copy must not hide the deletion of another one.
    # Unchanged files are identical in both trees, so only the changed and new
    # files can supply copies. A unit that stayed in its file uses that file's
    # copy first; the copies left over can receive units moved from elsewhere.
    leftover: Counter[str] = Counter()
    stayed: dict[str, Counter[str]] = {}
    for path in sorted(all_changed):
        new_file = root / path
        if not new_file.is_file():
            continue
        new_counts = Counter(
            u.dump for u in units(new_file.read_text(encoding="utf-8"), path)[0]
        )
        old = base_source(root, base, path)
        old_counts = Counter(u.dump for u in units(old, path)[0]) if old else Counter()
        stayed[path] = new_counts & old_counts
        leftover.update(new_counts - old_counts)

    base_units: Counter[str] = Counter()
    # (old module, name) -> modules whose moved code now reads that name.
    moved_reads: dict[tuple[str, str], set[str]] = {}
    for path in changed:
        old = base_source(root, base, path)
        if old is None:
            continue
        old_units, old_bases = units(old, path)
        new_file = root / path
        new_bases: dict[str, list[str]] = {}
        if new_file.is_file():
            _, new_bases = units(new_file.read_text(encoding="utf-8"), path)
        here = stayed.get(path, Counter())
        old_module = _module_name(path)
        module_level = _module_level_names(old)
        for unit in old_units:
            if here[unit.dump] > 0:
                here[unit.dump] -= 1  # still in this file
            elif leftover[unit.dump] <= 0:
                result.missing.append(unit)
            else:
                leftover[unit.dump] -= 1
                base_units[unit.dump] += 1  # left this file; landed elsewhere
                result.moved += 1
                landed = [
                    m
                    for u in head_by_dump.get(unit.dump, [])
                    if u.file != path and (m := _module_name(u.file))
                ]
                if old_module and landed:
                    names = set(unit.reads & module_level)
                    if unit.is_function and "." not in unit.qualname:
                        names.add(unit.qualname)
                    for name in names:
                        moved_reads.setdefault((old_module, name), set()).update(landed)
        for cls, old_cls_bases in old_bases.items():
            if cls in new_bases and new_bases[cls] != old_cls_bases:
                result.changed_bases.append(
                    f"{path}: class {cls}({', '.join(old_cls_bases)}) -> "
                    f"({', '.join(new_bases[cls])})"
                )

    for path in sorted(all_changed):
        new_file = root / path
        if not new_file.is_file():
            continue
        old = base_source(root, base, path)
        old_dumps = Counter(u.dump for u in units(old, path)[0]) if old else Counter()
        for unit in units(new_file.read_text(encoding="utf-8"), path)[0]:
            if unit.dump in old_dumps:
                old_dumps[unit.dump] -= 1
                if old_dumps[unit.dump] == 0:
                    del old_dumps[unit.dump]
                continue
            if base_units[unit.dump] > 0:
                base_units[unit.dump] -= 1  # moved here from a changed file
                continue
            # Stubs, re-exports, declarations and docstrings are not logic.
            if unit.is_declaration:
                result.new_stubs.append(unit)
            else:
                result.new_bodies.append(unit)

    result.stale_patches = stale_patches(root, moved_reads)
    return result


def _module_level_names(source: str) -> set[str]:
    """Names bound at module level: imports, functions, classes, constants."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _patches(text: str, module: str, name: str) -> list[int]:
    """Lines that patch ``module.name``, by dotted string or module alias."""
    lines = []
    pattern = rf"[\"']{re.escape(module)}\.{re.escape(name)}[\"']"
    for match in re.finditer(pattern, text):
        lines.append(text.count("\n", 0, match.start()) + 1)
    parent, _, leaf = module.rpartition(".")
    aliases = set(re.findall(rf"import {re.escape(module)} as (\w+)", text))
    aliases |= set(
        re.findall(rf"from {re.escape(parent)} import {leaf} as (\w+)", text)
    )
    if re.search(rf"from {re.escape(parent)} import {leaf}\b(?! as)", text):
        aliases.add(leaf)
    for alias in aliases:
        for match in re.finditer(
            rf"setattr\(\s*{re.escape(alias)}\s*,\s*[\"']{re.escape(name)}[\"']", text
        ):
            lines.append(text.count("\n", 0, match.start()) + 1)
    return lines


def stale_patches(root: Path, moved: dict[tuple[str, str], set[str]]) -> list[str]:
    """Tests that patch a name through its old module only.

    Moved code reads module-level names from its new module, so a test that
    patches ``old.name`` no longer affects it. A test is flagged unless it
    also patches the name in every module the moved code now reads it from.
    """
    if not moved:
        return []
    hits = []
    for path in sorted(root.rglob("test*.py")):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root).as_posix()
        for (old, name), new_modules in sorted(moved.items()):
            old_lines = _patches(text, old, name)
            if not old_lines:
                continue
            unpatched = sorted(m for m in new_modules if not _patches(text, m, name))
            if unpatched:
                hits.append(
                    f"{rel}:{old_lines[0]}: patches {old}.{name}; moved code reads "
                    f"it from {', '.join(unpatched)}"
                )
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--search", action="append", default=None)
    parser.add_argument("--allow-new", action="store_true")
    args = parser.parse_args(argv)
    # Compare with where this branch left the base, not with the base's tip,
    # so commits that landed on the base since then do not count as edits.
    fork_point = _git(args.root, "merge-base", args.base, "HEAD").strip()
    result = verify(args.root, fork_point, args.search or ["packages"])

    print(f"moved units: {result.moved}")
    for line in result.changed_bases:
        print(f"class bases changed: {line}")
    for unit in result.new_stubs:
        print(f"new stub or re-export: {unit.file}:{unit.lineno} {unit.qualname}")
    for hit in result.stale_patches:
        print(f"retarget patch: {hit}")
    for unit in result.missing:
        print(
            f"error: {unit.file}:{unit.lineno} {unit.qualname} is gone or was edited",
            file=sys.stderr,
        )
    for unit in result.new_bodies:
        level = "note" if args.allow_new else "error"
        print(
            f"{level}: {unit.file}:{unit.lineno} {unit.qualname} is new code",
            file=sys.stderr,
        )
    if result.missing or result.stale_patches:
        return 1
    if result.new_bodies and not args.allow_new:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
