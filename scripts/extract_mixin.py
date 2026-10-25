#!/usr/bin/env python3
"""Move methods of a large class into a mixin module, without editing them.

The tool behind the move commits of the decomposition plan
(packages/flow/docs/design/DECOMPOSITION.md, section 4.1). Given a source
module, a class and a list of its methods, it:

1. cuts each method out of the class verbatim (decorators and the comment
   lines directly above it included);
2. writes them into a new mixin class in the target module, importing every
   module-level name the bodies use from where the source module got it;
3. generates a host class that declares, for mypy only, exactly the
   attributes and other methods the moved code reads from ``self``, with
   the source class's own annotations (``Any`` when the source has none).
   It is a plain class, not a ``Protocol``: mypy treats a protocol attribute
   without a value as abstract and re-infers its type from the mixin's own
   assignments, and implicitly abstract protocol methods could shadow the
   real ones earlier in the method resolution order. The stubs' empty
   bodies are allowed by a per-module ``empty-body`` exemption;
4. makes the source class inherit from the mixin.

``--module-defs`` moves module-level functions and constants instead and
re-exports them from the source module, so their import paths keep working.

The result must pass ``scripts/verify_moved_code.py``: identical ASTs for
everything moved, and only imports, re-exports and protocol stubs added.
Run ``ruff check --fix`` and ``ruff format`` on the touched files afterwards.

Usage:
    python scripts/extract_mixin.py SOURCE --class Replayer --target MODULE_PATH
        --mixin NAME --methods m1,m2,...
    python scripts/extract_mixin.py SOURCE --module-defs --target MODULE_PATH
        --names n1,n2,...
"""

from __future__ import annotations

import argparse
import ast
import builtins
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BUILTINS = set(dir(builtins))


@dataclass
class Imports:
    """Where each module-level name of the source module comes from."""

    runtime: dict[str, str]  # name -> import statement
    type_checking: dict[str, str]
    defined: dict[str, ast.stmt]  # module-level defs in the source

    @classmethod
    def of(cls, tree: ast.Module) -> Imports:
        runtime: dict[str, str] = {}
        type_checking: dict[str, str] = {}
        defined: dict[str, ast.stmt] = {}

        def record(node: ast.stmt, into: dict[str, str]) -> None:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    text = f"from {'.' * node.level}{node.module or ''} import {alias.name}"
                    into[name] = text + (f" as {alias.asname}" if alias.asname else "")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        into[alias.asname] = f"import {alias.name} as {alias.asname}"
                    else:
                        into[alias.name.split(".")[0]] = f"import {alias.name}"

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                record(node, runtime)
            elif (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            ):
                for sub in node.body:
                    record(sub, type_checking)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined[node.name] = node
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined[target.id] = node
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined[node.target.id] = node
        return cls(runtime, type_checking, defined)


def names_in(nodes: list[ast.AST]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                found.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                base = sub
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name):
                    found.add(base.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                # String annotations ("Foo", "Optional[Bar]") name types too.
                for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", sub.value):
                    found.add(word)
    return found


def annotation_names(nodes: list[ast.AST]) -> set[str]:
    """Names used only in annotations of the given functions."""
    found: set[str] = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.arg) and sub.annotation is not None:
                found |= names_in([sub.annotation])
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.returns:
                found |= names_in([sub.returns])
            if isinstance(sub, ast.AnnAssign):
                found |= names_in([sub.annotation])
    return found


def span(lines: list[str], node: ast.AST) -> tuple[int, int]:
    """0-based [start, end) of a definition with decorators and comments above."""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    end = node.end_lineno  # type: ignore[attr-defined]
    return start, end


def runtime_names(nodes: list[ast.AST]) -> set[str]:
    """Names the bodies need at run time (annotations are strings here)."""
    found: set[str] = set()
    for node in nodes:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for deco in node.decorator_list:
            found |= names_in([deco])
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            found |= names_in([default])
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
                elif isinstance(sub, ast.AnnAssign):
                    pass
    return found


def import_lines(names: set[str], imports: Imports, shared: dict[str, str]) -> tuple[list[str], list[str]]:
    """Import statements for ``names``; second list is names nobody provides."""
    out, missing = [], []
    for name in sorted(names):
        if name in BUILTINS or name == "self":
            continue
        if name in shared:
            out.append(shared[name])
        elif name in imports.runtime:
            out.append(imports.runtime[name])
        elif name in imports.type_checking:
            out.append(imports.type_checking[name])
        elif name in imports.defined:
            missing.append(name)
    return sorted(set(out)), missing


def class_annotations(cls: ast.ClassDef) -> dict[str, str]:
    """Attribute annotations the class states: class-level, then ``__init__``."""
    found: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = ast.unparse(node.annotation)
    init = next(
        (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
        None,
    )
    if init is None:
        return found
    params = {
        a.arg: ast.unparse(a.annotation)
        for a in init.args.args + init.args.kwonlyargs
        if a.annotation is not None
    }
    for sub in ast.walk(init):
        target = None
        if isinstance(sub, ast.AnnAssign):
            target, ann = sub.target, ast.unparse(sub.annotation)
        elif isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            target = sub.targets[0]
            value = sub.value
            ann = params.get(value.id) if isinstance(value, ast.Name) else None
        else:
            continue
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr not in found
            and ann
        ):
            found[target.attr] = ann
    return found


def stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    copy = ast.parse(ast.unparse(node)).body[0]
    assert isinstance(copy, (ast.FunctionDef, ast.AsyncFunctionDef))
    copy.body = [ast.Expr(ast.Constant(Ellipsis))]
    return ast.unparse(copy)


def extract_methods(args: argparse.Namespace) -> None:
    source_path = Path(args.source)
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    imports = Imports.of(tree)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == args.cls)
    methods = {
        n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    wanted = [m for m in args.methods.split(",") if m]
    unknown = [m for m in wanted if m not in methods]
    if unknown:
        sys.exit(f"not methods of {args.cls}: {unknown}")
    moved = [methods[m] for m in wanted]
    shared = shared_imports(args.shared, imports)

    # Runtime imports for the bodies.
    runtime, missing = import_lines(runtime_names(moved) - set(wanted), imports, shared)
    if args.cls in runtime_names(moved):
        sys.exit(f"{args.cls} is referenced by name in the moved bodies; leave those methods")
    if missing:
        sys.exit(f"module-level names not yet in --shared: {missing}")

    # The host protocol: what the moved code reads from self.
    used: set[str] = set()
    for node in moved:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "self"
                and not sub.attr.startswith("__")
            ):
                used.add(sub.attr)
    used -= set(wanted)
    annotations = class_annotations(cls)
    host_lines: list[str] = []
    stub_nodes: list[ast.AST] = []
    for name in sorted(used):
        if name in methods:
            host_lines.append(_indent(stub(methods[name])))
            stub_nodes.append(methods[name])
        else:
            host_lines.append(f"    {name}: {annotations.get(name, 'Any')}")
    ann_text = [ast.parse(a, mode="eval").body for a in annotations.values()]
    type_names = (
        annotation_names(moved)
        | annotation_names(stub_nodes)
        | names_in([n for n in ann_text if any(f": {ast.unparse(n)}" in h for h in host_lines)])
    )
    type_lines, type_missing = import_lines(type_names - set(wanted), imports, shared)
    type_only = sorted(set(type_lines) - set(runtime))
    if type_missing:
        type_only += [f"from {args.source_module} import {n}" for n in sorted(type_missing)]

    host = f"_{args.mixin}Host"
    body = "".join("".join(lines[a:b]) + "\n" for a, b in (span(lines, n) for n in moved))
    module = [
        '# mypy: disable-error-code="empty-body"\n',
        f'"""{args.doc}\n\nMoved verbatim from ``{args.source_module}.{args.cls}`` by\n'
        "scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).\n"
        f'``{host}`` declares, for mypy only, what this code reads from the\n'
        f'{args.cls} it is mixed into.\n"""\n\n',
        "from __future__ import annotations\n\n",
        "from typing import TYPE_CHECKING, Any  # noqa: F401\n",
        *[line + "\n" for line in runtime],
        "\nif TYPE_CHECKING:\n",
        *([f"    {line}\n" for line in type_only] or ["    pass\n"]),
        f"\n\nclass {host}:\n",
        "\n".join(host_lines) + "\n" if host_lines else "    pass\n",
        "\n\nif TYPE_CHECKING:\n",
        f"    _Base = {host}\nelse:\n    _Base = object\n\n\n",
        f"class {args.mixin}(_Base):\n",
        f'    """{args.doc.splitlines()[0]}"""\n\n',
        body,
    ]
    target = Path(args.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    init = target.parent / "__init__.py"
    if not init.exists():
        init.write_text(f'"""{args.package_doc}"""\n', encoding="utf-8")
    target.write_text("".join(module), encoding="utf-8")

    # Cut the methods out of the source class, bottom up.
    for a, b in sorted((span(lines, n) for n in moved), reverse=True):
        del lines[a:b]
        while a < len(lines) and a > 0 and not lines[a].strip() and not lines[a - 1].strip():
            del lines[a]
    out = "".join(lines)
    mixin_module = _module_of(target)
    header = re.search(rf"^class {args.cls}(\((?P<bases>[^)]*)\))?:", out, re.M)
    assert header
    bases = [b.strip() for b in (header.group("bases") or "").split(",") if b.strip()]
    bases.append(args.mixin)
    out = out[: header.start()] + f"class {args.cls}({', '.join(bases)}):" + out[header.end() :]
    out = _add_import(out, f"from {mixin_module} import {args.mixin}")
    source_path.write_text(out, encoding="utf-8")
    print(f"moved {len(moved)} methods into {target}")


def extract_module_defs(args: argparse.Namespace) -> None:
    source_path = Path(args.source)
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    imports = Imports.of(tree)
    wanted = [n for n in args.names.split(",") if n]
    nodes = []
    for name in wanted:
        node = imports.defined.get(name)
        if node is None:
            sys.exit(f"not a module-level definition: {name}")
        if node not in nodes:
            nodes.append(node)
    names = set()
    for node in nodes:
        names |= names_in([node])
    provided = {n for node in nodes for n in _defined_names(node)}
    runtime, missing = import_lines(names - provided, imports, {})
    if missing:
        sys.exit(f"these definitions use other module-level names; move them too: {missing}")
    body = "\n\n".join("".join(lines[a:b]).rstrip("\n") for a, b in (span(lines, n) for n in nodes))
    target = Path(args.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    init = target.parent / "__init__.py"
    if not init.exists():
        init.write_text(f'"""{args.package_doc}"""\n', encoding="utf-8")
    target.write_text(
        f'"""{args.doc}\n\nMoved verbatim from ``{args.source_module}`` by\n'
        'scripts/extract_mixin.py; the source module re-exports every name.\n"""\n\n'
        "from __future__ import annotations\n\n"
        + "".join(line + "\n" for line in runtime)
        + "\n\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    for a, b in sorted((span(lines, n) for n in nodes), reverse=True):
        del lines[a:b]
    out = "".join(lines)
    exported = sorted(provided)
    out = _add_import(
        out,
        f"from {_module_of(target)} import (  # noqa: F401  re-exported\n"
        + "".join(f"    {n},\n" for n in exported)
        + ")",
    )
    source_path.write_text(out, encoding="utf-8")
    print(f"moved {len(exported)} names into {target}")


def _defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def shared_imports(paths: list[str] | None, imports: Imports) -> dict[str, str]:
    """Names other moved modules now provide: name -> import statement."""
    found: dict[str, str] = {}
    for path in paths or []:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        module = _module_of(Path(path))
        for node in tree.body:
            for name in _defined_names(node):
                found[name] = f"from {module} import {name}"
    return found


def _module_of(path: Path) -> str:
    parts = path.with_suffix("").parts
    start = next(i for i, p in enumerate(parts) if p.startswith("openadapt"))
    return ".".join(parts[start:])


def _indent(code: str) -> str:
    return "".join("    " + line if line.strip() else line for line in code.splitlines(True))


def _add_import(source: str, statement: str) -> str:
    """Insert an import after the last top-level import of the module."""
    tree = ast.parse(source)
    last = max(
        n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    )
    lines = source.splitlines(keepends=True)
    lines.insert(last, statement + "\n")
    return "".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source")
    parser.add_argument("--class", dest="cls", default="Replayer")
    parser.add_argument("--source-module", default="openadapt_flow.runtime.replayer")
    parser.add_argument("--target", required=True)
    parser.add_argument("--mixin")
    parser.add_argument("--methods", default="")
    parser.add_argument("--module-defs", action="store_true")
    parser.add_argument("--names", default="")
    parser.add_argument("--shared", action="append")
    parser.add_argument("--doc", default="Moved replayer code.")
    parser.add_argument("--package-doc", default="Replayer concerns, one module each.")
    args = parser.parse_args(argv)
    if args.module_defs:
        extract_module_defs(args)
    else:
        extract_methods(args)


if __name__ == "__main__":
    main()
