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
import builtins
import importlib
import json
import re
from bisect import bisect_left
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from textwrap import indent
from types import SimpleNamespace

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

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


def _import_binding(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    """One import's binding provenance, shared by calls and lazy exports.

    Importer machinery is a binding/dependency in its own right. It does
    not become the target of every name guarded by a later importing call.
    """
    if isinstance(node, ast.Import):
        top = alias.name.partition(".")[0]
        if alias.asname:
            return alias.name if alias.name in _IMPORTER_MODULES else "other"
        return top if top in _IMPORTER_MODULES else "other"
    return (
        alias.name
        if not node.level and (node.module, alias.name) in _IMPORTER_NAMES
        else "other"
    )


class _Scope:
    """One lexical scope, including a comprehension's private iteration targets."""

    def __init__(self, kind: str, parent: _Scope | None) -> None:
        self.kind = kind
        self.parent = parent
        # name -> [(evaluation position, what the binding makes it)]; "import_module" or
        # "__import__" for an importer, "module" for importlib/builtins,
        # "other" for an ordinary value, "unbound" after deletion/handler cleanup.
        self.bindings: dict[str, list[tuple[int, str]]] = {}
        self.writes: dict[str, list[tuple[int, str]]] = {}
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
    own scope the latest binding evaluated before the call wins, and in an enclosing
    scope any importer binding counts. A same-named local function or an
    unrelated method is not an importer.
    """

    def __init__(self, tree: ast.AST) -> None:
        self._scope_of: dict[int, _Scope] = {}
        self._position_of: dict[int, int] = {}
        self._position = 0
        self._unevaluated: set[int] = set()
        self._future_annotations = isinstance(tree, ast.Module) and any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        self._scopes: list[_Scope] = []
        self._current: dict[_Scope, dict[str, dict[str, frozenset[str]]]] = {}
        self._state_index: dict | None = None
        self._roots: dict[_Scope, ast.AST] = {}
        self._parents = {
            id(child): node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        self._module = self._new_scope("module", None, tree)
        self._visit(tree, self._module)

    def _new_scope(self, kind: str, parent: _Scope | None, root: ast.AST) -> _Scope:
        scope = _Scope(kind, parent)
        self._scopes.append(scope)
        self._current[scope] = {"bindings": {}, "writes": {}}
        self._roots[scope] = root
        return scope

    @staticmethod
    def _latest(events: list[tuple[int, str]], before: int | None = None) -> set[str]:
        # Transition positions are appended monotonically. A strict cutpoint
        # excludes every transition at that position, not just one origin.
        end = len(events) if before is None else bisect_left(events, (before,))
        if not end:
            return set()
        position = events[end - 1][0]
        values = set()
        while end and events[end - 1][0] == position:
            end -= 1
            values.add(events[end][1])
        return values

    def _state_values(self) -> dict:
        # Match the original scope/channel/name order, including empty histories.
        # New keys invalidate the ordered index; replacing an existing head does
        # not change its order. Only _append_history may update these heads.
        if self._state_index is None:
            self._state_index = {
                (scope, channel, name): values
                for scope in self._scopes
                for channel in ("bindings", "writes")
                for name, values in self._current[scope][channel].items()
            }
        return self._state_index

    def _checkpoint(self) -> dict:
        # Internal branch states share immutable heads, not caller-owned sets.
        # The mapping is detached so a later index update cannot alter a cutpoint.
        return self._state_values().copy()

    def _snapshot(self) -> dict:
        # Caller-owned plain sets: snapshots cannot mutate cached state or history.
        return {key: set(values) for key, values in self._state_values().items()}

    def _append_history(
        self, scope: _Scope, channel: str, name: str, events: list
    ) -> None:
        history = getattr(scope, channel).setdefault(name, [])
        history.extend(events)
        # A requested empty restore appends no event. Cache the ACTUAL journal
        # head: an existing history keeps its old value; a new history stays empty.
        values = frozenset(self._latest(history))
        self._current[scope][channel][name] = values
        if self._state_index is not None:
            key = (scope, channel, name)
            if key in self._state_index:
                self._state_index[key] = values
            else:
                self._state_index = None

    @staticmethod
    def _missing(key: tuple) -> set[str]:
        return {"not-written" if key[1] == "writes" else "unbound"}

    def _restore(self, state: dict, scopes: set[_Scope] | None = None) -> None:
        current = self._state_values()
        for key in current.keys() | state.keys():
            scope, channel, name = key
            if scopes is not None and scope not in scopes:
                continue
            values = state[key] if key in state else self._missing(key)
            present = current[key] if key in current else self._missing(key)
            if present != values:
                self._position += 1
                self._append_history(
                    scope,
                    channel,
                    name,
                    [(self._position, value) for value in sorted(values)],
                )

    def _join(self, *states: dict, scopes: set[_Scope] | None = None) -> None:
        keys = set().union(*(state.keys() for state in states))
        first, remaining = (states[0], states[1:]) if states else ({}, ())
        joined = {}
        for key in keys:
            values = first[key] if key in first else self._missing(key)
            for state in remaining:
                other = state[key] if key in state else self._missing(key)
                if values is not other and values != other:
                    # Never mutate a supplied snapshot. Most branch heads are
                    # unchanged and can retain their existing immutable value.
                    values = values | other
            joined[key] = values
        self._restore(joined, scopes)

    def _arms(self, arms: list[list[ast.AST]], scope: _Scope) -> None:
        entry = self._checkpoint()
        exits = []
        for arm in arms:
            self._restore(entry)
            for node in arm:
                self._visit(node, scope)
            exits.append(self._checkpoint())
        self._join(*exits)

    @staticmethod
    def _must_bind(node: ast.AST) -> set[str]:
        if isinstance(
            node,
            (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp),
        ):
            return set()
        if isinstance(node, ast.IfExp):
            return Importers._must_bind(node.test) | (
                Importers._must_bind(node.body) & Importers._must_bind(node.orelse)
            )
        if isinstance(node, ast.BoolOp):
            return Importers._must_bind(node.values[0])
        names = set().union(
            *(Importers._must_bind(child) for child in ast.iter_child_nodes(node))
        )
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        return names

    def _consumer_shadow(self, root: ast.AST, name: str) -> bool:
        # A proof is deliberately refused for any local consumer binding,
        # including a later binding. Do not confuse a same-spelled function
        # or parameter with an unshadowed builtin.
        def binds(node: ast.AST, outer: bool = False) -> bool:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not outer:
                prelude = [
                    *node.decorator_list,
                    *node.args.defaults,
                    *(item for item in node.args.kw_defaults if item is not None),
                ]
                return node.name == name or any(binds(item) for item in prelude)
            if isinstance(node, ast.ClassDef) and not outer:
                return node.name == name or any(
                    binds(item)
                    for item in [*node.decorator_list, *node.bases, *node.keywords]
                )
            if isinstance(node, ast.Lambda) and not outer:
                return False
            if isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                # A MAY walrus is still a lexical binding, whereas ordinary
                # comprehension iteration targets do not escape.
                return any(
                    isinstance(child, ast.NamedExpr)
                    and isinstance(child.target, ast.Name)
                    and child.target.id == name
                    for child in ast.walk(node)
                )
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return node.id == name
            if isinstance(node, ast.arg):
                return node.arg == name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return any(
                    alias.name == "*"
                    or (alias.asname or alias.name.split(".")[0]) == name
                    for alias in node.names
                )
            if isinstance(node, ast.ExceptHandler) and node.name == name:
                return True
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
                return True
            if isinstance(node, ast.MatchMapping) and node.rest == name:
                return True
            return any(binds(child) for child in ast.iter_child_nodes(node))

        return binds(root, True)

    def _eager_nonempty(self, node: ast.AST) -> bool:
        if isinstance(node, ast.GeneratorExp):
            parent = self._parents.get(id(node))
            if not (
                isinstance(parent, ast.Call)
                and parent.args == [node]
                and not parent.keywords
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "list"
                and not self._resolve(parent, "list")
            ):
                return False
            scope: _Scope | None = self._scope_of[id(parent)]
            own = True
            while scope is not None:
                if (own or scope.kind != "class") and self._consumer_shadow(
                    self._roots[scope], "list"
                ):
                    return False
                scope, own = scope.parent, False
        return all(
            not generator.is_async
            and not generator.ifs
            and isinstance(generator.iter, (ast.Tuple, ast.List))
            and bool(generator.iter.elts)
            and not any(isinstance(item, ast.Starred) for item in generator.iter.elts)
            for generator in node.generators
        )

    def _bind(self, scope: _Scope, name: str, what: str) -> None:
        self._position += 1
        event = (self._position, what)
        self._append_history(scope, "writes", name, [event])
        if name in scope.globals:
            scope = self._module
        elif name in scope.nonlocals:
            outer = scope.parent
            while outer is not None and outer.kind != "function":
                outer = outer.parent
            scope = outer or self._module
        self._append_history(scope, "bindings", name, [event])

    def _annotation(
        self, node: ast.AST | None, scope: _Scope, *, evaluated: bool
    ) -> None:
        if node is None:
            return
        if evaluated:
            self._visit(node, scope)
        else:
            self._unevaluated.update(id(sub) for sub in ast.walk(node))

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        self._scope_of[id(node)] = scope
        self._position += 1
        self._position_of[id(node)] = self._position
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                for sub in node.decorator_list:
                    self._visit(sub, scope)
            args = node.args
            for sub in [*args.defaults, *(d for d in args.kw_defaults if d)]:
                self._visit(sub, scope)
            parameters = [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *([args.vararg] if args.vararg else []),
                *([args.kwarg] if args.kwarg else []),
            ]
            for arg in parameters:
                self._annotation(
                    arg.annotation, scope, evaluated=not self._future_annotations
                )
            if not isinstance(node, ast.Lambda):
                self._annotation(
                    node.returns, scope, evaluated=not self._future_annotations
                )
                self._bind(scope, node.name, "other")
            outer_scopes = set(self._scopes)
            entry = self._checkpoint()
            inner = self._new_scope("function", scope, node)
            for arg in parameters:
                self._bind(inner, arg.arg, "other")
            body = node.body if isinstance(node.body, list) else [node.body]
            for sub in body:
                self._visit(sub, inner)
            # Defining a function does not execute its outer writes. Retain
            # their optional provenance without making them current facts.
            self._join(entry, self._checkpoint(), scopes=outer_scopes)
            return
        if isinstance(node, ast.ClassDef):
            for sub in [*node.decorator_list, *node.bases, *node.keywords]:
                self._visit(sub, scope)
            inner = self._new_scope("class", scope, node)
            for sub in node.body:
                self._visit(sub, inner)
            self._bind(scope, node.name, "other")
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            # Python evaluates only the first iterable outside the implicit
            # scope; every generator target belongs to the comprehension.
            self._visit(node.generators[0].iter, scope)
            outer_scopes = set(self._scopes)
            entry = self._checkpoint()
            inner = self._new_scope("comprehension", scope, node)
            for index, generator in enumerate(node.generators):
                if index:
                    self._visit(generator.iter, inner)
                self._visit(generator.target, inner)
                for condition in generator.ifs:
                    self._visit(condition, inner)
            elements = (
                [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            )
            for element in elements:
                self._visit(element, inner)
            after = self._checkpoint()
            definite = (
                set().union(*(self._must_bind(element) for element in elements))
                if self._eager_nonempty(node)
                else set()
            )
            joined = {}
            for key in entry.keys() | after.keys():
                values = after[key] if key in after else self._missing(key)
                previous = entry[key] if key in entry else self._missing(key)
                joined[key] = (
                    values
                    if key[2] in definite or values is previous or values == previous
                    else values | previous
                )
            self._restore(joined, outer_scopes)
            return
        if isinstance(node, (ast.If, ast.IfExp)):
            self._visit(node.test, scope)
            self._arms(
                [node.body, node.orelse]
                if isinstance(node, ast.If)
                else [[node.body], [node.orelse]],
                scope,
            )
            return
        if isinstance(node, ast.BoolOp):
            self._visit(node.values[0], scope)
            for value in node.values[1:]:
                self._arms([[], [value]], scope)
            return
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            entry = self._checkpoint()
            for sub in node.body:
                self._visit(sub, scope)
            normal = self._checkpoint()
            exits = []
            # A literal final raise cannot produce the normal try exit.
            for sub in node.orelse:
                self._visit(sub, scope)
            if not node.body or not isinstance(node.body[-1], (ast.Raise, ast.Return)):
                exits.append(self._checkpoint())
            for handler in node.handlers:
                self._join(entry, normal)
                self._visit(handler, scope)
                exits.append(self._checkpoint())
            self._join(*(exits or [normal]))
            for sub in node.finalbody:
                self._visit(sub, scope)
            return
        if isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                self._visit(node.type, scope)
            if node.name:
                self._bind(scope, node.name, "other")
            for sub in node.body:
                self._visit(sub, scope)
            if node.name:
                self._bind(scope, node.name, "unbound")
            return
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            self._visit(node.test if isinstance(node, ast.While) else node.iter, scope)
            body = (
                node.body if isinstance(node, ast.While) else [node.target, *node.body]
            )
            self._arms([[], body], scope)
            self._arms([[], node.orelse], scope)
            return
        if isinstance(node, ast.Match):
            self._visit(node.subject, scope)
            arms = [[]]
            for case in node.cases:
                arms.append(
                    [case.pattern, *([case.guard] if case.guard else []), *case.body]
                )
            self._arms(arms, scope)
            return
        if isinstance(node, ast.AugAssign):
            # A simple target is loaded before the RHS and stored afterward.
            # An attribute/subscript evaluates its target before the RHS.
            if isinstance(node.target, ast.Name):
                self._visit(node.value, scope)
                self._visit(node.target, scope)
            else:
                self._visit(node.target, scope)
                self._visit(node.value, scope)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            # AST field order puts targets first; Python evaluates the RHS
            # before stores, even for a same-line self-rebinding assignment.
            if node.value is not None:
                self._visit(node.value, scope)
            target_scope = scope
            if isinstance(node, ast.NamedExpr):
                # PEP572: a walrus escapes all surrounding comprehensions.
                while target_scope.kind == "comprehension":
                    assert target_scope.parent is not None
                    target_scope = target_scope.parent
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(node, ast.AnnAssign)
                    and node.value is None
                    and isinstance(target, ast.Name)
                ):
                    # A declaration without a value creates a function local,
                    # but never overwrites a value an earlier import bound.
                    if scope.kind == "function" and target.id not in scope.bindings:
                        self._bind(scope, target.id, "other")
                    continue
                self._visit(target, target_scope)
            if isinstance(node, ast.AnnAssign):
                self._annotation(
                    node.annotation,
                    scope,
                    evaluated=scope.kind != "function" and not self._future_annotations,
                )
            return
        if isinstance(node, ast.Global):
            scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            scope.nonlocals.update(node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.partition(".")[0]
                self._bind(scope, name, _import_binding(node, alias))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                self._bind(
                    scope, alias.asname or alias.name, _import_binding(node, alias)
                )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(
                scope, node.id, "unbound" if isinstance(node.ctx, ast.Del) else "other"
            )
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            self._bind(scope, node.name, "other")
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(scope, node.rest, "other")
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _resolve(self, node: ast.AST, name: str) -> set[str]:
        """Possible values at this evaluation point, retaining optional writes."""
        scope: _Scope | None = self._scope_of.get(id(node), self._module)
        position = self._position_of[id(node)]
        own = True
        deferred = False
        found: set[str] = set()

        def crosses_deferred_scope(item: _Scope) -> bool:
            return item.kind == "function" or (
                item.kind == "comprehension"
                and isinstance(self._roots[item], ast.GeneratorExp)
                and not self._eager_nonempty(self._roots[item])
            )

        while scope is not None:
            if own and name in scope.globals | scope.nonlocals:
                writes = self._latest(scope.writes.get(name, []), position)
                if writes:
                    found |= writes - {"not-written"}
                    if (
                        "unbound" in writes
                        and name in scope.globals
                        and name == "__import__"
                    ):
                        found.add("__import__")
                    if "not-written" not in writes:
                        return found
            if name in scope.globals and scope is not self._module:
                deferred |= crosses_deferred_scope(scope)
                scope, own = self._module, False
                continue
            if name in scope.nonlocals:
                deferred |= crosses_deferred_scope(scope)
                scope, own = scope.parent, False
                continue
            if (own or scope.kind != "class") and name in scope.bindings:
                events = scope.bindings[name]
                values = (
                    self._latest(events, position)
                    if own or not deferred
                    else {value for _, value in events}
                )
                if (
                    deferred
                    and scope is self._module
                    and name == "__import__"
                    and not self._latest(events, position)
                ):
                    # A deferred call may run before the module's first
                    # explicit binding, when the builtin is still present.
                    values.add("__import__")
                if not values and scope.kind in {"function", "comprehension"}:
                    values = {"unbound"}
                if values:
                    found |= values - {"unbound"}
                    if "unbound" not in values:
                        return found
                    if scope.kind in {"function", "comprehension"}:
                        return found | {"unbound"}
            # Class bodies and eager comprehensions execute at this
            # cutpoint. Crossing a function/deferred-generator boundary may
            # observe later enclosing bindings, so retain their full history.
            deferred |= crosses_deferred_scope(scope)
            scope, own = scope.parent, False
        if name == "__import__":
            found.add("__import__")
        return found

    def called(self, call: ast.Call) -> str | None:
        """``import_module`` or ``__import__`` when ``call`` is one, else None."""

        if id(call) in self._unevaluated:
            return None
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
            and any(
                (module, func.attr) in _IMPORTER_NAMES
                for module in self._resolve(call, func.value.id)
            )
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
    helper_values: dict[str, set[tuple[str, str]]] = {}
    for imported in ast.walk(tree):
        if not isinstance(imported, (ast.Import, ast.ImportFrom)):
            continue
        targets = (
            [a.name if a.asname else a.name.partition(".")[0] for a in imported.names]
            if isinstance(imported, ast.Import)
            else _from_targets(imported, module, is_package=True)
        )
        for alias, target in zip(imported.names, targets):
            role = _import_binding(imported, alias)
            if role != "other":
                name = alias.asname or alias.name.partition(".")[0]
                helper_values.setdefault(name, set()).add((role, target))

    def add(key: str, target: str) -> None:
        out.setdefault(key, set()).add(target)

    def returned_values(node: ast.AST | None) -> Iterable[ast.AST]:
        if node is None:
            return
        if isinstance(node, ast.Call):
            # A callee is machinery, not the value returned by its call.
            for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                yield from returned_values(arg)
        elif isinstance(node, ast.Name) or (
            isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        ):
            yield node
        else:
            for child in ast.iter_child_nodes(node):
                yield from returned_values(child)

    def visit(node: ast.AST, guards: set[str]) -> None:
        if isinstance(node, ast.ImportFrom):
            targets = _from_targets(node, module, is_package=True)
            for alias, target in zip(node.names, targets):
                selected = guards if _import_binding(node, alias) == "other" else set()
                for key in {alias.asname or alias.name, *selected}:
                    add(key, target)
            add(targets[-1].rpartition(".")[2], targets[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                selected = guards if _import_binding(node, alias) == "other" else set()
                for key in {alias.name.rpartition(".")[2], alias.asname, *selected}:
                    if key:
                        add(key, alias.name)
        elif isinstance(node, ast.Return):
            # A helper explicitly returned as a value really is an export.
            # Do not confuse its use as a call's callee with its return value.
            for value in returned_values(node.value):
                if isinstance(value, ast.Name):
                    roles = importers._resolve(value, value.id)
                    for role, target in helper_values.get(value.id, set()):
                        if role in roles:
                            for key in guards:
                                add(key, target)
                elif isinstance(value, ast.Attribute) and isinstance(
                    value.value, ast.Name
                ):
                    name = value.value.id
                    roles = importers._resolve(value.value, name)
                    for role, target in helper_values.get(name, set()):
                        if role in roles and (role, value.attr) in _IMPORTER_NAMES:
                            for key in guards:
                                add(key, target + "." + value.attr)
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


# Fixed synthetic programs exercise Python's binding rules independently of
# the checker's traversal. The spy never imports a planted or product module.
_BINDING_FORMS = [
    ("", "__import__", "__import__", "fromlist"),
    ("from importlib import import_module as load\n", "load", "load", "package"),
    ("import importlib as il\n", "il.import_module", "il", "package"),
    ("import builtins as bi\n", "bi.__import__", "bi", "fromlist"),
]


def _assert_observed_binding_edges(
    tmp_path, monkeypatch, source, expected, *, lazy=False
):
    observed = []
    original_builtin = builtins.__import__
    original_module = importlib.import_module

    def builtin_spy(name, *args, **kwargs):
        if name == "planted.demo.app":
            observed.append(name)
            return SimpleNamespace()
        return original_builtin(name, *args, **kwargs)

    def module_spy(name, *args, **kwargs):
        if name == "planted.demo.app":
            observed.append(name)
            return SimpleNamespace()
        return original_module(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", builtin_spy)
        patch.setattr(importlib, "import_module", module_spy)
        exec(
            compile(source, "<synthetic-binding-control>", "exec", dont_inherit=True),
            {"ordinary": SimpleNamespace(import_module=str, __import__=str)}
            if ".import_module" in source or ".__import__" in source
            else {"ordinary": str},
        )
    assert observed == (["planted.demo.app"] if expected else [])
    graph = (
        _plant(
            tmp_path,
            **{
                "ui/__init__.py": source,
                "helper.py": "from planted.ui import Window\n",
            },
        )
        if lazy
        else _plant(tmp_path, **{"core/engine.py": source, "helper.py": ""})
    )
    line = next(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and node.args
        and _str(node.args[0]) == "planted.demo.app"
    )
    assert direct_violations(graph, _PLANTED_CONTRACT) == (
        [f"planted.core.engine -> planted.demo.app (app, lines [{line}])"]
        if expected and not lazy
        else []
    )
    path = (
        "planted.core.engine > planted.helper > planted.demo.app"
        if lazy
        else "planted.core.engine > planted.demo.app"
    )
    assert transitive_violations(graph, _PLANTED_CONTRACT) == (
        [path] if expected else []
    )


def _binding_program(setup, body, scope):
    if scope == "function":
        # Rebinding makes the name local throughout a function; give the
        # builtin arm the same genuine initial local binding as the aliases.
        setup = setup or "from builtins import __import__\n"
        return "def probe():\n" + indent(setup + body, "    ") + "\nprobe()\n"
    return setup + body


@pytest.mark.parametrize("scope", ["module", "function"])
@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("call_then_store", True),
        ("store_then_call", False),
        ("assignment_rhs", True),
        ("annotated_rhs", True),
        ("walrus_rhs", True),
        ("declaration_only", True),
        ("for_iterable", True),
        ("augmented_rhs", True),
        ("exception_type", True),
        ("definition_default", True),
        ("class_body", True),
        ("parameter_annotation", True),
        ("callee_before_argument", True),
    ],
)
def test_checker_matches_binding_evaluation_order(
    tmp_path, monkeypatch, scope, form, shape, expected
):
    setup, importer, binding, keyword = form
    call = f"{importer}('planted.demo.app')"
    bodies = {
        "call_then_store": f"{call}; {binding} = ordinary\n",
        "store_then_call": f"{binding} = ordinary; {call}\n",
        "assignment_rhs": f"{binding} = {call}\n",
        "annotated_rhs": f"{binding}: object = {call}\n",
        "walrus_rhs": f"({binding} := {call})\n",
        "declaration_only": f"{binding}: object; {call}\n",
        "for_iterable": f"for {binding} in ({call},):\n    pass\n",
        "augmented_rhs": (
            f"try:\n    {binding} += {call}\nexcept TypeError:\n    pass\n"
        ),
        "exception_type": (
            f"try:\n    raise ValueError('synthetic')\n"
            f"except ({call}, ValueError)[1] as {binding}:\n    pass\n"
        ),
        "definition_default": f"def {binding}(value={call}):\n    pass\n",
        "class_body": f"class {binding}:\n    value = {call}\n",
        "parameter_annotation": f"def annotated(value: {call}):\n    pass\n",
        "callee_before_argument": (
            f"{importer}('planted.demo.app', {keyword}=({binding} := None))\n"
        ),
    }
    source = _binding_program(setup, bodies[shape], scope)
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, expected)


@pytest.mark.parametrize("scope", ["module", "function"])
@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("family", ["list", "set", "dict", "generator"])
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("target_does_not_leak", True),
        ("target_shadows_body", False),
        ("target_shadows_filter", False),
        ("first_iterable_is_outer", True),
        ("later_iterable_is_inner", False),
        ("walrus_escapes", False),
    ],
)
def test_checker_matches_comprehension_binding_scopes(
    tmp_path, monkeypatch, scope, form, family, shape, expected
):
    setup, importer, binding, _ = form
    call = f"{importer}('planted.demo.app')"
    expression, clause, tail = "0", f"{binding} in (ordinary,)", ""
    if shape == "target_does_not_leak":
        tail = f"\n{call}"
    elif shape == "target_shadows_body":
        expression = call
    elif shape == "target_shadows_filter":
        clause += f" if {call}"
    elif shape == "first_iterable_is_outer":
        clause = f"{binding} in ({call},)"
    elif shape == "later_iterable_is_inner":
        clause += f" for item in ({call},)"
    else:
        expression = f"(({binding} := ordinary), 0)[1]"
        clause, tail = "item in (1,)", f"\n{call}"
    displays = {
        "list": f"[{expression} for {clause}]",
        "set": f"{{{expression} for {clause}}}",
        "dict": f"{{0: {expression} for {clause}}}",
        "generator": f"list({expression} for {clause})",
    }
    source = _binding_program(setup, displays[family] + tail + "\n", scope)
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, expected)


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("family", ["same-line", "list", "set", "dict", "generator"])
@pytest.mark.parametrize("expected", [True, False])
def test_lazy_exports_share_the_binding_model(
    tmp_path, monkeypatch, form, family, expected
):
    setup, importer, binding, _ = form
    setup = setup or "from builtins import __import__\n"
    call = f"{importer}('planted.demo.app')"
    if family == "same-line":
        body = (
            f"{call}; {binding} = ordinary\n"
            if expected
            else f"{binding} = ordinary; {call}\n"
        )
    else:
        expression = "0" if expected else call
        clause = f"{binding} in (ordinary,)"
        displays = {
            "list": f"[{expression} for {clause}]",
            "set": f"{{{expression} for {clause}}}",
            "dict": f"{{0: {expression} for {clause}}}",
            "generator": f"list({expression} for {clause})",
        }
        body = displays[family] + (f"\n{call}" if expected else "") + "\n"
    source = (
        "def __getattr__(name):\n"
        + indent(setup + "if name == 'Window':\n" + indent(body, "    "), "    ")
        + "\n__getattr__('Window')\n"
    )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, expected, lazy=True)


_UNBINDING_CASES = (
    [("class", form, True) for form in _BINDING_FORMS]
    + [("function", form, False) for form in _BINDING_FORMS]
    + [(scope, _BINDING_FORMS[0], True) for scope in ("module", "global", "deferred")]
    + [("global_before", form, True) for form in _BINDING_FORMS]
    + [("global_rebound", form, False) for form in _BINDING_FORMS]
)


@pytest.mark.parametrize("scope,form,expected", _UNBINDING_CASES)
@pytest.mark.parametrize("transition", ["delete", "handler_cleanup"])
def test_checker_tracks_binding_removal_and_fallback(
    tmp_path, monkeypatch, scope, form, expected, transition
):
    setup, importer, binding, _ = form
    call = f"{importer}('planted.demo.app')"
    unbind = (
        f"{binding} = ordinary\ndel {binding}\n"
        if transition == "delete"
        else f"try:\n    raise ValueError('synthetic')\nexcept ValueError as {binding}:\n    pass\n"
    )
    if scope == "class":
        source = setup + "class C:\n" + indent(unbind + f"value = {call}\n", "    ")
    elif scope == "function":
        # A deleted local stays local/unbound. This is an explicit refusal
        # control, never credit for a supposedly reached importing call.
        body = (
            unbind + f"try:\n    {call}\nexcept UnboundLocalError:\n    return\n"
            "raise AssertionError('expected an unbound local')\n"
        )
        source = _binding_program(setup, body, "function")
    elif scope == "module":
        source = unbind + call + "\n"
    elif scope == "deferred":
        source = unbind + "def probe():\n" + indent(call + "\n", "    ") + "probe()\n"
    else:
        setup = setup or "from builtins import __import__\n"
        body = (
            call + "\n" + unbind
            if scope == "global_before"
            else unbind
            + (f"{binding} = ordinary\n" if scope == "global_rebound" else "")
            + call
            + "\n"
        )
        source = (
            setup
            + "def probe():\n"
            + indent(f"global {binding}\n" + body, "    ")
            + "probe()\n"
        )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, expected)


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("site", ["module", "class", "parameter", "return", "local"])
@pytest.mark.parametrize("future", [False, True])
def test_checker_only_counts_annotations_python_evaluates(
    tmp_path, monkeypatch, form, site, future
):
    setup, importer, _, _ = form
    call = f"{importer}('planted.demo.app')"
    bodies = {
        "module": f"value: {call}\n",
        "class": f"class C:\n    value: {call}\n",
        "parameter": f"def annotated(value: {call}):\n    pass\n",
        "return": f"def annotated() -> {call}:\n    pass\n",
        "local": f"def annotated():\n    value: {call}\nannotated()\n",
    }
    source = (
        ("from __future__ import annotations\n" if future else "")
        + setup
        + bodies[site]
    )
    _assert_observed_binding_edges(
        tmp_path, monkeypatch, source, not future and site != "local"
    )


@pytest.mark.parametrize("form", _BINDING_FORMS)
def test_future_annotation_does_not_hide_its_evaluated_assignment_rhs(
    tmp_path, monkeypatch, form
):
    setup, importer, _, _ = form
    source = (
        "from __future__ import annotations\n"
        + setup
        + f"value: object = {importer}('planted.demo.app')\n"
    )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, True)


@pytest.mark.parametrize("scope", ["module", "function"])
@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("family", ["list", "set", "dict", "generator"])
@pytest.mark.parametrize(
    "shape", ["empty", "filtered", "deferred", "conditional_write"]
)
def test_optional_comprehension_write_retains_importer(
    tmp_path, monkeypatch, scope, form, family, shape
):
    setup, importer, binding, _ = form
    write = f"(({binding} := ordinary), 0)[1]"
    expression = f"False and {write}" if shape == "conditional_write" else write
    clause = "item in ()" if shape == "empty" else "item in (1,)"
    if shape == "filtered":
        clause += " if False"
    displays = {
        "list": f"[{expression} for {clause}]",
        "set": f"{{{expression} for {clause}}}",
        "dict": f"{{0: {expression} for {clause}}}",
        "generator": f"({expression} for {clause})",
    }
    expression = displays[family]
    if family == "generator" and shape != "deferred":
        expression = f"list({expression})"
    # Eager comprehensions always evaluate their nonempty, unfiltered body.
    expected = shape != "deferred" or family == "generator"
    body = expression + f"\n{importer}('planted.demo.app')\n"
    _assert_observed_binding_edges(
        tmp_path, monkeypatch, _binding_program(setup, body, scope), expected
    )


@pytest.mark.parametrize("scope", ["module", "function"])
@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize(
    "shape", ["conditional", "empty_loop", "unselected_handler", "deferred_global"]
)
def test_optional_statement_write_retains_importer(
    tmp_path, monkeypatch, scope, form, shape
):
    setup, importer, binding, _ = form
    bodies = {
        "conditional": f"if False:\n    {binding} = ordinary\n",
        "empty_loop": f"for {binding} in ():\n    pass\n",
        "unselected_handler": f"try:\n    pass\nexcept ValueError as {binding}:\n    pass\n",
        "deferred_global": f"def never_called():\n    global {binding}\n    {binding} = ordinary\n",
    }
    body = bodies[shape] + f"{importer}('planted.demo.app')\n"
    _assert_observed_binding_edges(
        tmp_path, monkeypatch, _binding_program(setup, body, scope), True
    )


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("family", ["list", "set", "dict", "generator"])
@pytest.mark.parametrize(
    "shape", ["empty", "filtered", "deferred", "conditional_write"]
)
def test_lazy_optional_comprehension_uses_same_join(
    tmp_path, monkeypatch, form, family, shape
):
    setup, importer, binding, _ = form
    setup = setup or "from builtins import __import__\n"
    write = f"(({binding} := ordinary), 0)[1]"
    expression = f"False and {write}" if shape == "conditional_write" else write
    clause = "item in ()" if shape == "empty" else "item in (1,)"
    if shape == "filtered":
        clause += " if False"
    displays = {
        "list": f"[{expression} for {clause}]",
        "set": f"{{{expression} for {clause}}}",
        "dict": f"{{0: {expression} for {clause}}}",
        "generator": f"({expression} for {clause})",
    }
    expression = displays[family]
    if family == "generator" and shape != "deferred":
        expression = f"list({expression})"
    expected = shape != "deferred" or family == "generator"
    body = setup + expression + f"\n{importer}('planted.demo.app')\n"
    source = (
        "def __getattr__(name):\n"
        + indent("if name == 'Window':\n" + indent(body, "    "), "    ")
        + "\n__getattr__('Window')\n"
    )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, expected, lazy=True)


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("site", ["module", "local", "later_enclosing", "parameter"])
def test_shadowed_list_cannot_prove_generator_completion(
    tmp_path, monkeypatch, form, site
):
    setup, importer, binding, _ = form
    setup = setup or "from builtins import __import__\n"
    expression = f"list((({binding} := ordinary), 0)[1] for item in (1,))\n"
    tail = f"{importer}('planted.demo.app')\n"
    shadow = "def list(value):\n    return None\n"
    if site == "module":
        source = setup + shadow + expression + tail
    elif site == "local":
        source = (
            "def probe():\n"
            + indent(setup + shadow + expression + tail, "    ")
            + "\nprobe()\n"
        )
    elif site == "later_enclosing":
        source = (
            "def probe():\n"
            + indent(setup + expression + tail, "    ")
            + "\n"
            + shadow
            + "probe()\n"
        )
    else:
        source = (
            shadow.replace("list", "consumer")
            + "def probe(list):\n"
            + indent(setup + expression + tail, "    ")
            + "\nprobe(consumer)\n"
        )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, True)


def test_importer_modules_do_not_invent_nonexistent_methods(tmp_path, monkeypatch):
    source = "import builtins as bi\ntry:\n    bi.import_module('planted.demo.app')\nexcept AttributeError:\n    pass\n"
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, False)


@given(
    events=st.lists(
        st.tuples(
            st.integers(min_value=-10, max_value=10),
            st.sampled_from(
                (
                    "a",
                    "b",
                    "other",
                    "unbound",
                    "not-written",
                    "import_module",
                    "__import__",
                )
            ),
        ),
        max_size=128,
    ),
    before=st.one_of(st.none(), st.integers(min_value=-12, max_value=12)),
)
@example(events=[], before=None)
@example(events=[], before=0)
@example(events=[(1, "a"), (1, "b"), (2, "other")], before=None)
@example(events=[(1, "a"), (1, "b"), (2, "other")], before=2)
@example(events=[(1, "a"), (1, "b"), (2, "other")], before=1)
@example(events=[(1, "a"), (1, "a"), (1, "b")], before=3)
@example(events=[(-2, "a"), (-2, "b"), (0, "other")], before=-1)
def test_latest_history_index_matches_original_scan(events, before):
    # Independent original implementation, including its strict-before rule.
    def reference(history, cutpoint):
        prior = [
            (at, value) for at, value in history if cutpoint is None or at < cutpoint
        ]
        if not prior:
            return set()
        position = prior[-1][0]
        return {value for at, value in prior if at == position}

    history = sorted(events, key=lambda event: event[0])
    assert Importers._latest(history, before) == reference(history, before)


class _JournalOracle:
    """Original uncached transitions over independent histories and shared scope ids."""

    def __init__(self, owner):
        self.scopes = owner._scopes
        self.module = owner._module
        self.position = owner._position
        self.histories = {
            scope: {"bindings": {}, "writes": {}} for scope in self.scopes
        }

    @staticmethod
    def latest(events, before=None):
        prior = [(at, value) for at, value in events if before is None or at < before]
        if not prior:
            return set()
        position = prior[-1][0]
        return {value for at, value in prior if at == position}

    def snapshot(self):
        return {
            (scope, channel, name): self.latest(events)
            for scope in self.scopes
            for channel in ("bindings", "writes")
            for name, events in self.histories[scope][channel].items()
        }

    @staticmethod
    def missing(key):
        return {"not-written" if key[1] == "writes" else "unbound"}

    def bind(self, scope, name, what):
        self.position += 1
        event = (self.position, what)
        self.histories[scope]["writes"].setdefault(name, []).append(event)
        if name in scope.globals:
            scope = self.module
        elif name in scope.nonlocals:
            outer = scope.parent
            while outer is not None and outer.kind != "function":
                outer = outer.parent
            scope = outer or self.module
        self.histories[scope]["bindings"].setdefault(name, []).append(event)

    def restore(self, state, scopes=None):
        current = self.snapshot()
        for key in current.keys() | state.keys():
            scope, channel, name = key
            if scopes is not None and scope not in scopes:
                continue
            values = state.get(key, self.missing(key))
            if current.get(key, self.missing(key)) != values:
                self.position += 1
                self.histories[scope][channel].setdefault(name, []).extend(
                    (self.position, value) for value in sorted(values)
                )

    def join(self, *states, scopes=None):
        keys = set().union(*(state.keys() for state in states))
        joined = {
            key: set().union(*(state.get(key, self.missing(key)) for state in states))
            for key in keys
        }
        self.restore(joined, scopes)


def _journal_histories(owner):
    return {
        (scope, channel, name): list(events)
        for scope in owner._scopes
        for channel in ("bindings", "writes")
        for name, events in getattr(scope, channel).items()
    }


@given(
    commands=st.lists(
        st.tuples(
            st.sampled_from(
                ("bind", "save", "restore", "join", "empty_restore", "mutate_saved")
            ),
            st.integers(min_value=0, max_value=4),
            st.integers(min_value=0, max_value=3),
            st.integers(min_value=0, max_value=3),
            st.sampled_from(("alias", "global_name", "nonlocal_name", "__import__")),
            st.sampled_from(
                (
                    "other",
                    "unbound",
                    "not-written",
                    "importlib",
                    "builtins",
                    "import_module",
                    "__import__",
                )
            ),
            st.booleans(),
        ),
        max_size=35,
    )
)
@example(commands=[("empty_restore", 0, 0, 0, "alias", "other", False)])
@example(
    commands=[
        ("bind", 0, 0, 0, "alias", "import_module", False),
        ("empty_restore", 0, 0, 0, "alias", "other", False),
    ]
)
@example(
    commands=[
        ("bind", 0, 0, 0, "alias", "import_module", False),
        ("save", 0, 0, 0, "alias", "other", False),
        ("bind", 0, 0, 0, "alias", "other", False),
        ("save", 0, 1, 0, "alias", "other", False),
        ("join", 0, 0, 1, "alias", "other", False),
    ]
)
def test_current_cache_stateful_matches_original_journals(commands):
    owner = Importers(ast.parse(""))
    root = owner._module
    function = owner._new_scope("function", root, ast.parse(""))
    nested = owner._new_scope("function", function, ast.parse(""))
    owner._new_scope("comprehension", nested, ast.parse(""))
    owner._new_scope("class", root, ast.parse(""))
    function.globals.add("global_name")
    nested.nonlocals.add("nonlocal_name")
    oracle = _JournalOracle(owner)
    saved = [{} for _ in range(4)]
    expected_saved = [{} for _ in range(4)]
    for operation, scope_index, first, second, name, value, filtered in commands:
        scope = owner._scopes[scope_index]
        selected = {root, scope} if filtered else None
        previous_histories = _journal_histories(owner)
        if operation == "bind":
            owner._bind(scope, name, value)
            oracle.bind(scope, name, value)
        elif operation == "save":
            saved[first] = owner._snapshot()
            expected_saved[first] = oracle.snapshot()
        elif operation == "restore":
            owner._restore(saved[first], selected)
            oracle.restore(expected_saved[first], selected)
        elif operation == "join":
            owner._join(saved[first], saved[second], scopes=selected)
            oracle.join(expected_saved[first], expected_saved[second], scopes=selected)
        elif operation == "empty_restore":
            state = {(scope, "bindings", name): set()}
            owner._restore(state, selected)
            oracle.restore(state, selected)
        else:
            for state in (saved[first], expected_saved[first]):
                for origins in state.values():
                    origins.add("snapshot_only")
        actual_histories = _journal_histories(owner)
        expected_histories = {
            (item, channel, key): list(events)
            for item in oracle.scopes
            for channel in ("bindings", "writes")
            for key, events in oracle.histories[item][channel].items()
        }
        assert actual_histories == expected_histories
        assert owner._position == oracle.position
        assert owner._snapshot() == oracle.snapshot()
        assert saved == expected_saved
        for key, history in previous_histories.items():
            assert actual_histories[key][: len(history)] == history
        for history in actual_histories.values():
            assert [at for at, _ in history] == sorted(at for at, _ in history)
            for before in (
                None,
                -1,
                owner._position // 2,
                owner._position,
                owner._position + 1,
            ):
                assert owner._latest(history, before) == oracle.latest(history, before)
        detached = owner._snapshot()
        assert all(type(origins) is set for origins in detached.values())
        for origins in detached.values():
            origins.clear()
            origins.add("detached_snapshot_only")
        detached.clear()
        assert owner._snapshot() == oracle.snapshot()


class _UncachedImporters(Importers):
    """Original reconstruction path for public checker entry-point equivalence."""

    def _checkpoint(self):
        return self._snapshot()

    def _snapshot(self):
        return {
            (scope, channel, name): _JournalOracle.latest(events)
            for scope in self._scopes
            for channel in ("bindings", "writes")
            for name, events in getattr(scope, channel).items()
        }

    def _restore(self, state, scopes=None):
        current = self._snapshot()
        for key in current.keys() | state.keys():
            scope, channel, name = key
            if scopes is not None and scope not in scopes:
                continue
            values = state.get(key, self._missing(key))
            if current.get(key, self._missing(key)) != values:
                self._position += 1
                getattr(scope, channel).setdefault(name, []).extend(
                    (self._position, value) for value in sorted(values)
                )

    def _join(self, *states, scopes=None):
        keys = set().union(*(state.keys() for state in states))
        joined = {
            key: set().union(*(state.get(key, self._missing(key)) for state in states))
            for key in keys
        }
        self._restore(joined, scopes)

    def _bind(self, scope, name, what):
        self._position += 1
        event = (self._position, what)
        scope.writes.setdefault(name, []).append(event)
        if name in scope.globals:
            scope = self._module
        elif name in scope.nonlocals:
            outer = scope.parent
            while outer is not None and outer.kind != "function":
                outer = outer.parent
            scope = outer or self._module
        scope.bindings.setdefault(name, []).append(event)


@given(
    commands=st.lists(
        st.tuples(
            st.sampled_from(("bind", "save", "restore", "join", "empty_restore")),
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=2),
            st.sampled_from(("alias", "global_name", "nonlocal_name")),
            st.sampled_from(("other", "import_module", "__import__", "unbound")),
            st.booleans(),
        ),
        max_size=35,
    )
)
@example(
    commands=[
        ("save", 0, 0, 0, "alias", "other", False),
        ("bind", 0, 0, 0, "alias", "import_module", False),
        ("save", 0, 1, 0, "alias", "other", False),
        ("restore", 0, 0, 0, "alias", "other", False),
        ("join", 0, 0, 1, "alias", "other", False),
    ]
)
@example(
    commands=[
        ("bind", 2, 0, 0, "nonlocal_name", "__import__", True),
        ("save", 2, 0, 0, "alias", "other", False),
        ("empty_restore", 1, 0, 0, "nonlocal_name", "other", True),
        ("bind", 1, 0, 0, "global_name", "import_module", False),
        ("restore", 2, 0, 0, "alias", "other", True),
    ]
)
def test_immutable_checkpoints_match_original_journals(commands):
    owner = Importers(ast.parse(""))
    root = owner._module
    function = owner._new_scope("function", root, ast.parse(""))
    owner._new_scope("function", function, ast.parse(""))
    function.globals.add("global_name")
    owner._scopes[2].nonlocals.add("nonlocal_name")
    oracle = _JournalOracle(owner)
    saved = [{} for _ in range(3)]
    expected_saved = [{} for _ in range(3)]
    for operation, scope_index, first, second, name, value, filtered in commands:
        scope = owner._scopes[scope_index]
        selected = {root, scope} if filtered else None
        if operation == "bind":
            owner._bind(scope, name, value)
            oracle.bind(scope, name, value)
        elif operation == "save":
            saved[first] = owner._checkpoint()
            expected_saved[first] = oracle.snapshot()
        elif operation == "restore":
            owner._restore(saved[first], selected)
            oracle.restore(expected_saved[first], selected)
        elif operation == "join":
            owner._join(saved[first], saved[second], scopes=selected)
            oracle.join(expected_saved[first], expected_saved[second], scopes=selected)
        else:
            state = {(scope, "bindings", name): set()}
            owner._restore(state, selected)
            oracle.restore(state, selected)
        assert owner._snapshot() == oracle.snapshot()
        assert owner._position == oracle.position
        assert saved == expected_saved
        assert all(
            type(origins) is frozenset for state in saved for origins in state.values()
        )
        assert _journal_histories(owner) == {
            (item, channel, key): list(events)
            for item in oracle.scopes
            for channel in ("bindings", "writes")
            for key, events in oracle.histories[item][channel].items()
        }


def test_internal_checkpoints_detach_mapping_and_keep_old_heads():
    owner = Importers(ast.parse(""))
    root = owner._module
    owner._bind(root, "alias", "import_module")
    saved = owner._checkpoint()
    owner._bind(root, "alias", "other")
    key = (root, "bindings", "alias")
    assert saved[key] == {"import_module"}
    assert owner._snapshot()[key] == {"other"}
    saved[key] = frozenset({"checkpoint_only"})
    saved.clear()
    assert owner._snapshot()[key] == {"other"}


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("scope", ("module", "function"))
@pytest.mark.parametrize(
    "shape",
    (
        "sequence",
        "branch",
        "loop",
        "handler",
        "empty",
        "filtered",
        "deferred",
        "consumed",
    ),
)
def test_current_cache_preserves_all_checker_entrypoints(
    tmp_path, monkeypatch, form, scope, shape
):
    setup, importer, binding, _ = form
    bodies = {
        "sequence": "",
        "branch": f"if False:\n    {binding} = ordinary\n",
        "loop": f"for {binding} in ():\n    pass\n",
        "handler": f"try:\n    pass\nexcept ValueError as {binding}:\n    pass\n",
        "empty": f"[({binding} := ordinary) for item in ()]\n",
        "filtered": f"[({binding} := ordinary) for item in (1,) if False]\n",
        "deferred": f"unused = (({binding} := ordinary) for item in (1,))\n",
        "consumed": f"list((({binding} := ordinary), 0)[1] for item in (1,))\n",
    }
    body = bodies[shape] + f"{importer}('planted.demo.app')\n"
    source = _binding_program(setup, body, scope)
    lazy_source = (
        "def __getattr__(name):\n    if name == 'Window':\n"
        + indent(source, "        ")
        + "\n"
    )
    lazy_map_source = (
        "_LAZY_EXPORTS = {'Window': 'planted.demo.app'}\n"
        + lazy_source.replace("'planted.demo.app'", "_LAZY_EXPORTS[name]")
    )
    tree = ast.parse(source)
    cached = Importers(tree)
    uncached = _UncachedImporters(tree)
    assert cached._position_of == uncached._position_of
    assert [
        cached.called(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
    ] == [
        uncached.called(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
    ]
    assert [
        _dynamic_targets(node, "planted.core.engine", cached, is_package=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ] == [
        _dynamic_targets(node, "planted.core.engine", uncached, is_package=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    cached_lazy = _lazy_exports(ast.parse(lazy_source), "planted.ui")
    cached_map = _lazy_exports(ast.parse(lazy_map_source), "planted.ui")
    planted = {
        "core/engine.py": source + "from planted import helper\n",
        "helper.py": "from planted.ui import Window\n",
        "ui/__init__.py": lazy_map_source,
    }
    cached_graph = _plant(tmp_path, **planted)
    with monkeypatch.context() as patch:
        patch.setitem(globals(), "Importers", _UncachedImporters)
        uncached_lazy = _lazy_exports(ast.parse(lazy_source), "planted.ui")
        uncached_map = _lazy_exports(ast.parse(lazy_map_source), "planted.ui")
        uncached_graph = _plant(tmp_path, **planted)
    assert cached_lazy == uncached_lazy
    assert cached_map == uncached_map
    if shape == "sequence":
        assert cached_lazy["Window"] == {"planted.demo.app"}
        assert cached_map["Window"] == {"planted.demo.app"}
        assert "planted.demo.app" in cached_graph["planted.helper"]
    assert cached_graph == uncached_graph
    assert direct_violations(cached_graph, _PLANTED_CONTRACT) == direct_violations(
        uncached_graph, _PLANTED_CONTRACT
    )
    assert transitive_violations(
        cached_graph, _PLANTED_CONTRACT
    ) == transitive_violations(uncached_graph, _PLANTED_CONTRACT)


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("scope", ("module", "function"))
@pytest.mark.parametrize(
    "eager", ("class", "list", "set", "dict", "consumed_generator")
)
def test_eager_enclosing_lookup_precedes_later_binding(
    tmp_path, monkeypatch, form, scope, eager
):
    @given(depth=st.integers(min_value=1, max_value=3))
    def check(depth):
        setup, importer, binding, _ = form
        call = f"{importer}('planted.demo.app')"
        expression = {
            "class": f"value = {call}\n",
            "list": f"[{call} for item in (1,)]\n",
            "set": f"{{({call}, 0)[1] for item in (1,)}}\n",
            "dict": f"{{item: {call} for item in (1,)}}\n",
            "consumed_generator": f"list({call} for item in (1,))\n",
        }[eager]
        for level in range(depth):
            expression = f"class Frame{level}:\n" + indent(expression, "    ")
        source = _binding_program(setup, expression + f"{binding} = ordinary\n", scope)
        _assert_observed_binding_edges(tmp_path, monkeypatch, source, True)

    check()


def test_deferred_builtin_call_can_precede_its_first_explicit_module_store(
    tmp_path, monkeypatch
):
    source = (
        "def probe():\n    return __import__('planted.demo.app')\n"
        "probe()\n__import__ = ordinary\n"
    )
    _assert_observed_binding_edges(tmp_path, monkeypatch, source, True)


@pytest.mark.parametrize("form", _BINDING_FORMS)
def test_deferred_lookup_still_keeps_later_importer_binding(
    tmp_path, monkeypatch, form
):
    @given(depth=st.integers(min_value=1, max_value=3))
    def check(depth):
        setup, importer, binding, _ = form
        setup = setup or "from builtins import __import__\n"
        body = f"{importer}('planted.demo.app')\n"
        for level in range(depth):
            body = f"class Frame{level}:\n" + indent(body, "    ")
        source = (
            f"{binding} = ordinary\ndef probe():\n"
            + indent(body, "    ")
            + setup
            + "probe()\n"
        )
        _assert_observed_binding_edges(tmp_path, monkeypatch, source, True)

    check()


@pytest.mark.parametrize("form", _BINDING_FORMS)
@pytest.mark.parametrize("scope", ("module", "function"))
@pytest.mark.parametrize("shadowed", (False, True))
@given(export=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=10))
def test_lazy_export_target_is_not_its_importer_machinery(
    form, scope, shadowed, export
):
    setup, importer, binding, _ = form
    call = f"{importer}('planted.demo.app')\n"
    body = (f"{binding} = ordinary\n" if shadowed else "") + call
    nested = _binding_program(setup, body, scope)
    source = (
        "def __getattr__(name):\n    if name == "
        + repr(export)
        + ":\n"
        + indent(nested, "        ")
    )
    targets = _lazy_exports(ast.parse(source), "planted.ui")
    assert targets.get(export, set()) == (set() if shadowed else {"planted.demo.app"})


@pytest.mark.parametrize(
    "setup,value,target",
    [
        ("from builtins import __import__ as load", "load", "builtins.__import__"),
        (
            "from importlib import import_module as load",
            "load",
            "importlib.import_module",
        ),
        ("import importlib as machinery", "machinery", "importlib"),
        ("import builtins as machinery", "machinery", "builtins"),
        (
            "import importlib as machinery",
            "machinery.import_module",
            "importlib.import_module",
        ),
        ("import builtins as machinery", "machinery.__import__", "builtins.__import__"),
    ],
)
@pytest.mark.parametrize("container", ("direct", "mapping", "argument"))
def test_importer_explicitly_returned_as_a_value_remains_a_lazy_export(
    setup, value, target, container
):
    expression = (
        value
        if container == "direct"
        else "{'Window': " + value + "}[name]"
        if container == "mapping"
        else "identity(" + value + ")"
    )
    source = "def __getattr__(name):\n    if name == 'Window':\n" + indent(
        setup + "\nreturn " + expression + "\n", "        "
    )
    if container == "argument":
        source = "def identity(value):\n    return value\n" + source
    assert _lazy_exports(ast.parse(source), "planted.ui")["Window"] == {target}


@pytest.mark.parametrize("form", _BINDING_FORMS)
def test_lazy_machinery_binding_and_real_module_dependencies_are_both_retained(form):
    setup, importer, binding, _ = form
    setup = setup or "from builtins import __import__\n"
    source = "def __getattr__(name):\n    if name == 'Window':\n" + indent(
        setup + f"return {importer}('planted.demo.app')\n", "        "
    )
    targets = _lazy_exports(ast.parse(source), "planted.ui")
    assert targets["Window"] == {"planted.demo.app"}
    assert targets[binding]
    assert all(not target.startswith("planted.demo") for target in targets[binding])
