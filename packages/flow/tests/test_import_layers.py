"""The core runtime and verifier never import app, benchmark, UI or lab code.

``import_layers.json`` (next to ``pyproject.toml``) names the core modules and
four forbidden groups: app-specific, benchmark, desktop-UI, and lab and
harness code. The checks here read every module's imports from its source, so
nothing is imported to run them. A lazy import inside a function counts: it
runs on the same code path, just later. So does ``importlib.import_module`` or
``__import__`` with a literal module name. The existing direct and transitive contract gates
refuses nonliteral targets and importer capabilities escaping the supported
call model; a literal-edge projection alone does not certify a contract. Imports a package ``__getattr__`` re-exports (including through a
``_LAZY_EXPORTS``-style table) count where a caller asks for the name.

Core may not import a forbidden module directly, with no exceptions. Core may
not reach one through any chain of ``openadapt_flow`` imports either; an edge
that cannot be removed yet is listed in the contract's ``exceptions`` with a
reason and an issue, and an exception that no longer exists fails too, so the
list only shrinks.
The repository migration tests separately enforce ``import_layers_debt.json``:
its exact source-bound unresolved ceiling must shrink as migration removes sites.
That ceiling is not target admission or proof of capability safety. The raw
direct/transitive violation APIs always report all unresolved diagnostics.

"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib
import json
import re
from bisect import bisect_left
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from textwrap import indent
from types import MappingProxyType, SimpleNamespace

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import openadapt_flow

ROOT = Path(openadapt_flow.__file__).parent
CONTRACT = ROOT.parent / "import_layers.json"


class Graph(dict[str, dict[str, list[int]]]):
    """Literal dependency edges and mandatory unresolved-capability evidence."""

    def __init__(self) -> None:
        super().__init__()
        self.dynamic_problems: dict[str, list[str]] = {}
        self.admission_facts: dict[str, dict[int, dict]] = {}
        self.source_closure_sha256 = ""
        self.package: str | None = None
        self.admission_problems: list[str] = []
        self.admitted_dynamic_problems: list[str] = []
        self.debt_sites: dict[str, dict[str, dict]] = {}
        self.repository_debt_observations: dict[str, dict] = {}


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
_NAMESPACE_FACTORIES = {"globals", "locals", "vars"}
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
    if (
        not node.level
        and node.module == "builtins"
        and alias.name in _NAMESPACE_FACTORIES
    ):
        return "namespace-factory:" + alias.name
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


class _BranchState:
    """Private detached branch value; never replaces the public dict checkpoint."""

    def __init__(self, token, rows):
        self.token = token
        self.rows = tuple(rows)
        self.by_scope = MappingProxyType(dict(self.rows))
        self._flat = None

    def flat(self):
        if self._flat is None:
            self._flat = MappingProxyType(
                {
                    (scope, channel, name): values
                    for scope, heads in self.rows
                    for channel, items in zip(("bindings", "writes"), heads)
                    for name, values in items
                }
            )
        return self._flat


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
        self._branch_head_cache: dict = {}
        self._branch_head_cache_keys = 0
        self._branch_token = object()
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

    _BRANCH_CACHE_MAX_SCOPES = 512
    _BRANCH_CACHE_MAX_KEYS = 16384

    def _branch_heads(self, scope):
        cached = self._branch_head_cache.get(scope)
        if cached is not None:
            return cached
        heads = tuple(
            tuple(self._current[scope][channel].items())
            for channel in ("bindings", "writes")
        )
        size = sum(map(len, heads))
        if self._BRANCH_CACHE_MAX_SCOPES > 0 and size <= self._BRANCH_CACHE_MAX_KEYS:
            if (
                len(self._branch_head_cache) >= self._BRANCH_CACHE_MAX_SCOPES
                or self._branch_head_cache_keys + size > self._BRANCH_CACHE_MAX_KEYS
            ):
                self._branch_head_cache.clear()
                self._branch_head_cache_keys = 0
            self._branch_head_cache[scope] = heads
            self._branch_head_cache_keys += size
        return heads

    def _branch_checkpoint(self):
        return _BranchState(
            self._branch_token,
            ((scope, self._branch_heads(scope)) for scope in self._scopes),
        )

    def _branch_restore_noop(self, state, scopes):
        if state.token is not self._branch_token:
            return False
        for scope in self._scopes if scopes is None else scopes:
            if scope not in self._current:
                continue
            current = self._branch_heads(scope)
            requested = state.by_scope.get(scope, ((), ()))
            if current == requested:
                continue
            for channel, present_items, wanted_items in zip(
                ("bindings", "writes"), current, requested
            ):
                present, wanted = dict(present_items), dict(wanted_items)
                for name in present.keys() | wanted.keys():
                    key = (scope, channel, name)
                    if present.get(name, self._missing(key)) != wanted.get(
                        name, self._missing(key)
                    ):
                        return False
        return True

    def _branch_join_noop(self, states, scopes):
        if not states or any(state.token is not self._branch_token for state in states):
            return False
        for scope in self._scopes if scopes is None else scopes:
            if scope not in self._current:
                continue
            current = self._branch_heads(scope)
            requested = [state.by_scope.get(scope, ((), ())) for state in states]
            if all(heads == current for heads in requested):
                continue
            for index, channel in enumerate(("bindings", "writes")):
                present = dict(current[index])
                branches = [dict(heads[index]) for heads in requested]
                keys = set().union(*(branch.keys() for branch in branches))
                for name in present.keys() | keys:
                    key = (scope, channel, name)
                    if name in keys:
                        values = set().union(
                            *(
                                branch.get(name, self._missing(key))
                                for branch in branches
                            )
                        )
                    else:
                        values = self._missing(key)
                    if present.get(name, self._missing(key)) != values:
                        return False
        return True

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

        cached = self._branch_head_cache.pop(scope, None)
        if cached is not None:
            self._branch_head_cache_keys -= sum(map(len, cached))

    @staticmethod
    def _missing(key: tuple) -> set[str]:
        return {"not-written" if key[1] == "writes" else "unbound"}

    def _flat_restore(self, state: dict, scopes: set[_Scope] | None = None) -> None:
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

    def _flat_join(self, *states: dict, scopes: set[_Scope] | None = None) -> None:
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

    def _restore(self, state, scopes: set[_Scope] | None = None) -> None:
        if isinstance(state, _BranchState):
            if self._branch_restore_noop(state, scopes):
                return
            state = state.flat()
        self._flat_restore(state, scopes)

    def _join(self, *states, scopes: set[_Scope] | None = None) -> None:
        if states and all(isinstance(state, _BranchState) for state in states):
            if self._branch_join_noop(states, scopes):
                return
        flat = tuple(
            state.flat() if isinstance(state, _BranchState) else state
            for state in states
        )
        self._flat_join(*flat, scopes=scopes)

    def _arms(self, arms: list[list[ast.AST]], scope: _Scope) -> None:
        entry = self._branch_checkpoint()
        exits = []
        for arm in arms:
            self._restore(entry)
            for node in arm:
                self._visit(node, scope)
            exits.append(self._branch_checkpoint())
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
            entry = self._branch_checkpoint()
            inner = self._new_scope("function", scope, node)
            for arg in parameters:
                self._bind(inner, arg.arg, "other")
            body = node.body if isinstance(node.body, list) else [node.body]
            for sub in body:
                self._visit(sub, inner)
            # Defining a function does not execute its outer writes. Retain
            # their optional provenance without making them current facts.
            self._join(entry, self._branch_checkpoint(), scopes=outer_scopes)
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
            entry = self._branch_checkpoint()
            for sub in node.body:
                self._visit(sub, scope)
            normal = self._branch_checkpoint()
            exits = []
            # A literal final raise cannot produce the normal try exit.
            for sub in node.orelse:
                self._visit(sub, scope)
            if not node.body or not isinstance(node.body[-1], (ast.Raise, ast.Return)):
                exits.append(self._branch_checkpoint())
            for handler in node.handlers:
                self._join(entry, normal)
                self._visit(handler, scope)
                exits.append(self._branch_checkpoint())
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


class _ImportProvenance:
    """The same temporal lookup, with package-local import/reexport provenance.

    Only import statements transfer a capability. Any other transfer is
    rejected at its source instead of guessing what an arbitrary value does.
    """

    def __init__(self, tree, module, is_package, exports, capability_namespaces=None):
        self.module = module
        self.is_package = is_package
        self.exports = exports
        self.capability_namespaces = (
            _capability_namespaces(exports)
            if capability_namespaces is None
            else capability_namespaces
        )
        super().__init__(tree)

    def _visit(self, node, scope):
        super()._visit(node, scope)
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name if alias.asname else alias.name.partition(".")[0]
                if target == "sys":
                    self._bind(scope, alias.asname or target, "namespace:sys")
                elif target in self.exports:
                    self._bind(scope, alias.asname or target, "module:" + target)
        elif isinstance(node, ast.ClassDef):
            self._bind(scope, node.name, _class_namespace(self.module, node))
        elif isinstance(node, ast.ImportFrom):
            targets = _from_targets(node, self.module, is_package=self.is_package)
            prefix = targets[-1]
            for alias, target in zip(node.names, targets):
                if alias.name == "*":
                    for name, roles in self.exports.get(prefix, {}).items():
                        for role in sorted(roles):
                            self._bind(scope, name, "strict:" + "|".join(sorted(roles)))
                else:
                    roles = self.exports.get(prefix, {}).get(alias.name, set())
                    if target in self.exports:
                        roles = roles | {"module:" + target}
                    if roles:
                        self._bind(
                            scope,
                            alias.asname or alias.name,
                            "strict:" + "|".join(sorted(roles)),
                        )

    def roles(self, node):
        if id(node) in self._unevaluated:
            return set()
        if isinstance(node, ast.Name):
            roles = self._resolve(node, node.id)
            roles = set().union(
                *(
                    set(role.removeprefix("strict:").split("|"))
                    if role.startswith("strict:")
                    else {role}
                    for role in roles
                )
            )
            if node.id == "__builtins__" and not roles:
                return {"builtins"}
            if node.id in _NAMESPACE_FACTORIES and not roles:
                return {"namespace-factory:" + node.id}
            return roles
        if isinstance(node, ast.Attribute):
            out = set()
            for role in self.roles(node.value):
                if role == "namespace:sys" and node.attr == "modules":
                    out.add("registry:sys.modules")
                elif role == "builtins" and node.attr in _NAMESPACE_FACTORIES:
                    out.add("namespace-factory:" + node.attr)
                elif (role, node.attr) in _IMPORTER_NAMES:
                    out.add(node.attr)
                elif role.startswith("class:"):
                    out.update(self.exports.get(role, {}).get(node.attr, set()))
                elif role.startswith("module:"):
                    module = role.removeprefix("module:")
                    out.update(self.exports.get(module, {}).get(node.attr, set()))
                    target = module + "." + node.attr
                    if target in self.exports:
                        out.add("module:" + target)
            return out
        if isinstance(node, ast.Subscript) and "registry:sys.modules" in self.roles(
            node.value
        ):
            target = _str(node.slice)
            if target == "sys":
                return {"namespace:sys"}
            if target in _IMPORTER_MODULES:
                return {target}
            if target in self.exports:
                return {"module:" + target}
        if isinstance(node, ast.Call):
            if self.called(node):
                target = _str(_arg(node, 0, "name"))
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and "registry:sys.modules" in self.roles(node.func.value)
            ):
                target = _str(_arg(node, 0, "key"))
            else:
                target = None
            if target == "sys":
                return {"namespace:sys"}
            if target in _IMPORTER_MODULES:
                return {target}
            if target in self.exports:
                return {"module:" + target}
        return set()

    def called(self, call):
        if id(call) in self._unevaluated:
            return None
        return next(
            (role for role in sorted(self.roles(call.func)) if role in _DYNAMIC), None
        )


def _strict_importers(tree, module, is_package, exports, *, capability_namespaces=None):
    # Resolve the current temporal model at construction, preserving the
    # original uncached-oracle substitution used by equivalence tests.
    owner = type("_BoundImporters", (_ImportProvenance, Importers), {})
    return owner(tree, module, is_package, exports, capability_namespaces)


def _class_namespace(module, node):
    return f"class:{module}:{node.lineno}:{node.col_offset}"


def _capability_namespaces(exports):
    """Finite reverse reachability over immutable namespace binding facts."""
    direct = {
        *_DYNAMIC,
        *_IMPORTER_MODULES,
        "namespace:sys",
        "registry:sys.modules",
        *("namespace-factory:" + name for name in _NAMESPACE_FACTORIES),
    }
    reverse = {}
    capable = set()
    for namespace, bindings in exports.items():
        for roles in bindings.values():
            for role in roles:
                if role in direct:
                    capable.add(namespace)
                target = (
                    role.removeprefix("module:") if role.startswith("module:") else role
                )
                if target in exports:
                    reverse.setdefault(target, set()).add(namespace)
    queue = deque(sorted(capable))
    while queue:
        for namespace in sorted(reverse.get(queue.popleft(), ())):
            if namespace not in capable:
                capable.add(namespace)
                queue.append(namespace)
    return frozenset(capable)


def _literal_class_value(node):
    """Certify literal syntax, not literal_eval's special empty-set call.

    No Name/Call/Attribute/Subscript/unpacking or other executable syntax is
    permitted before literal_eval. Containers contain only the same finite
    literal grammar. This certificate applies to fields and method preludes.
    """
    allowed = {
        ast.Constant,
        ast.Tuple,
        ast.List,
        ast.Set,
        ast.Dict,
        ast.Load,
        ast.UnaryOp,
        ast.UAdd,
        ast.USub,
        ast.BinOp,
        ast.Add,
        ast.Sub,
    }
    if not isinstance(node, ast.AST) or any(
        type(item) not in allowed for item in ast.walk(node)
    ):
        return False
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _plain_class_namespace(node):
    """Only plain builtin class construction can retain an unused capability.

    Unknown metaclasses/bases/decorators and arbitrary descriptor-producing
    class statements may implicitly receive the class namespace. Refuse that
    exposure instead of exempting classes or pretending to execute Python.
    """
    if node.decorator_list or node.bases or node.keywords:
        return False
    for statement in node.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(statement, ast.ClassDef) and _plain_class_namespace(statement):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = [
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
                *([statement.args.vararg] if statement.args.vararg else []),
                *([statement.args.kwarg] if statement.args.kwarg else []),
            ]
            prelude = [
                *statement.args.defaults,
                *(value for value in statement.args.kw_defaults if value is not None),
                *(arg.annotation for arg in parameters if arg.annotation is not None),
                *([statement.returns] if statement.returns is not None else []),
            ]
            if not statement.decorator_list and all(
                _literal_class_value(value) for value in prelude
            ):
                continue
        elif isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ):
            if isinstance(statement.value.value, str):
                continue
        elif isinstance(statement, ast.Assign):
            if _literal_class_value(statement.value):
                continue
        elif isinstance(statement, ast.AnnAssign):
            if _literal_class_value(statement.annotation) and (
                statement.value is None or _literal_class_value(statement.value)
            ):
                continue
        return False
    return True


def _importer_exports(trees, mods):
    """Finite namespace/import-binding closure, driven only by changed inputs.

    Class bodies own bindings distinct from lexical method lookup. Reexports
    preserve class namespace identity; no ordinary assignment transfers an
    importer, since its source value is separately refused as an escape.
    """
    # A source-known namespace package may have no __init__.py. Keep its
    # identity so importing a.b.c cannot erase the capability of an imported
    # child when only the root name a is locally bound.
    namespace_names = set()
    for name in trees:
        while name:
            namespace_names.add(name)
            name = name.rpartition(".")[0]
    exports = {name: {} for name in sorted(namespace_names)}
    records = {}
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                exports[_class_namespace(module, node)] = {}

        def statements(node, namespace):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return
            if isinstance(node, ast.ClassDef):
                records[namespace].append(("class", node))
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                records[namespace].append(("import", node))
            for child in ast.iter_child_nodes(node):
                statements(child, namespace)

        records[module] = []
        statements(tree, module)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                namespace = _class_namespace(module, node)
                records[namespace] = []
                for statement in node.body:
                    statements(statement, namespace)
    # Importing a known child populates each ancestor's attribute namespace.
    # These are finite source-backed MAY facts, not invented runtime targets.
    # Ordinary local name shadows still use the unchanged temporal owner;
    # cross-module attribute overwrites remain a conservative refusal boundary.
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = _from_targets(
                    node, module, is_package=mods[module].name == "__init__.py"
                )
            else:
                continue
            for target in targets:
                if target not in namespace_names:
                    continue
                parts = target.split(".")
                for length in range(1, len(parts)):
                    parent = ".".join(parts[:length])
                    child = ".".join(parts[: length + 1])
                    exports[parent].setdefault(parts[length], set()).add(
                        "module:" + child
                    )
    for namespace in exports:
        records.setdefault(namespace, [])
    owners = {}
    dependencies = {namespace: set() for namespace in records}
    for module, tree in trees.items():
        owners[module] = module
        owners.update(
            {
                _class_namespace(module, n): module
                for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef)
            }
        )
    for namespace, nodes in records.items():
        if not nodes:
            continue
        module = owners[namespace]
        for kind, node in nodes:
            if kind == "import" and isinstance(node, ast.ImportFrom):
                prefix = _from_targets(
                    node, module, is_package=mods[module].name == "__init__.py"
                )[-1]
                if prefix in exports:
                    dependencies[prefix].add(namespace)
    queue = deque(records)
    queued = set(records)
    while queue:
        namespace = queue.popleft()
        queued.remove(namespace)
        if not records[namespace]:
            continue
        module = owners[namespace]
        changed = False

        def bind(name, roles):
            nonlocal changed
            old = exports[namespace].get(name, set())
            if not roles <= old:
                exports[namespace][name] = old | roles
                changed = True

        for kind, node in records[namespace]:
            if kind == "class":
                bind(node.name, {_class_namespace(module, node)})
                continue
            for alias in node.names:
                role = _import_binding(node, alias)
                name = alias.asname or alias.name.partition(".")[0]
                roles = {role} if role != "other" else set()
                if isinstance(node, ast.Import):
                    target = (
                        alias.name if alias.asname else alias.name.partition(".")[0]
                    )
                    if target == "sys":
                        roles.add("namespace:sys")
                    elif target in exports:
                        roles.add("module:" + target)
                else:
                    targets = _from_targets(
                        node, module, is_package=mods[module].name == "__init__.py"
                    )
                    prefix = targets[-1]
                    if alias.name == "*":
                        for key, values in exports.get(prefix, {}).items():
                            bind(key, values)
                        continue
                    roles |= exports.get(prefix, {}).get(alias.name, set())
                    target = prefix + "." + alias.name
                    if target == "sys":
                        roles.add("namespace:sys")
                    elif target in exports:
                        roles.add("module:" + target)
                bind(name, roles)
        if changed:
            for dependent in sorted(dependencies[namespace]):
                if dependent not in queued:
                    queued.add(dependent)
                    queue.append(dependent)
    return exports


def _importer_capabilities(roles, owner):
    capabilities = roles & {
        *_DYNAMIC,
        *_IMPORTER_MODULES,
        "namespace:sys",
        "registry:sys.modules",
        *("namespace-factory:" + name for name in _NAMESPACE_FACTORIES),
    }
    return capabilities | {
        role
        for role in roles
        if (role.removeprefix("module:") if role.startswith("module:") else role)
        in owner.capability_namespaces
    }


def _known_capability_mutation(node, owner):
    if not isinstance(node, (ast.Attribute, ast.Subscript)) or not isinstance(
        node.ctx, (ast.Store, ast.Del)
    ):
        return False
    # Attribute dictionaries, callable attributes and nested machinery members
    # remain the same mutation class, even when the final projection is opaque.
    return any(
        _importer_capabilities(owner.roles(value), owner)
        for value in ast.walk(node.value)
        if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript, ast.Call))
    )


def _unresolvable_imports(tree, module, is_package, exports, *, importers=None):
    """Reject nonliteral calls and capability escapes, including uncalled ones.

    A direct imported alias can call a literal. Assignment, walrus, returning,
    passing or storing the importer/module itself is a capability escape and
    refuses even if a later call appears literal. This closes arbitrary alias
    chains without pretending to execute Python's object semantics.
    """
    owner = (
        importers
        if importers is not None
        else _strict_importers(tree, module, is_package, exports)
    )
    parents = {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    found = set()
    for node in ast.walk(tree):
        if id(node) in owner._unevaluated:
            continue
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            prefix = _from_targets(node, module, is_package=is_package)[-1]
            if prefix not in exports or prefix in owner.capability_namespaces:
                found.add((node.lineno, "importer star import"))
        if isinstance(node, ast.Call) and owner.called(node):
            # A nonliteral relative base or fromlist also hides dependency edges.
            targets = _dynamic_targets(node, module, owner, is_package=is_package)
            name = _str(_arg(node, 0, "name"))
            unresolved = (
                not name or not targets or any(k.arg is None for k in node.keywords)
            )
            if owner.called(node) == "__import__":
                fromlist = _arg(node, 3, "fromlist")
                level = _arg(node, 4, "level")
                # A literal None (also as a walrus RHS) cannot add submodules.
                # This preserves callee-before-argument binding controls; the
                # module target itself still must be a direct string literal.
                known_fromlist = (
                    fromlist.value if isinstance(fromlist, ast.NamedExpr) else fromlist
                )
                unresolved |= fromlist is not None and not (
                    isinstance(known_fromlist, ast.Constant)
                    and known_fromlist.value is None
                    or isinstance(fromlist, (ast.Tuple, ast.List))
                    and all(_str(item) is not None for item in fromlist.elts)
                )
                unresolved |= level is not None and not (
                    isinstance(level, ast.Constant)
                    and type(level.value) is int
                    and level.value == 0
                )
            if unresolved:
                found.add((node.lineno, "nonliteral or unresolved import target"))
        if (
            isinstance(node, ast.ClassDef)
            and _class_namespace(module, node) in owner.capability_namespaces
            and not _plain_class_namespace(node)
        ):
            found.add((node.lineno, "importer implicit class-namespace exposure"))
        if _known_capability_mutation(node, owner):
            found.add((node.lineno, "importer capability mutation"))
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)) or (
            hasattr(node, "ctx") and not isinstance(node.ctx, ast.Load)
        ):
            continue
        roles = owner.roles(node)
        capabilities = _importer_capabilities(roles, owner)
        if not capabilities:
            continue
        parent = parents.get(id(node))
        # The registry as a store target does not read a module capability.
        if (
            roles == {"registry:sys.modules"}
            and isinstance(parent, ast.Subscript)
            and isinstance(parent.ctx, ast.Store)
        ):
            continue
        # A callable's reflective attributes and registry lookups must not erase
        # capability provenance. Normal namespace member lookup is retained.
        if (
            not isinstance(node, (ast.Call, ast.Subscript))
            and isinstance(parent, ast.Attribute)
            and parent.value is node
            and isinstance(parent.ctx, ast.Load)
            and not roles
            & (
                _DYNAMIC
                | {
                    "registry:sys.modules",
                    *("namespace-factory:" + name for name in _NAMESPACE_FACTORIES),
                }
            )
            and not any(role.startswith("class:") for role in roles)
            and (not parent.attr.startswith("__") or owner.roles(parent) & _DYNAMIC)
        ):
            continue
        if (
            isinstance(parent, ast.Call)
            and parent.func is node
            and owner.called(parent)
        ):
            continue
        found.add((node.lineno, "importer value escape"))
    return [
        f"{module}:{line}: unresolvable dynamic import ({reason})"
        for line, reason in sorted(found)
    ]


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


def _lazy_exports(
    tree: ast.Module, module: str, *, exports: dict | None = None, importers=None
) -> dict[str, set[str]]:
    """Names a package ``__getattr__`` imports on demand, mapped to modules.

    Read from ``from m import n [as a]`` (the name it binds), ``import a.b``
    and literal ``import_module``/``__import__`` calls (the module's last
    name, any ``as`` name, and every name an enclosing ``if`` compares
    against), and the string tables, such as ``_LAZY_EXPORTS``, whose values
    ``__getattr__`` passes to an importing call.
    """

    out: dict[str, set[str]] = {}
    tables = _module_tables(tree)
    if importers is None:
        importers = (
            Importers(tree)
            if exports is None
            else _strict_importers(tree, module, True, exports)
        )
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
    source = {name: path.read_bytes() for name, path in mods.items()}
    trees = {name: ast.parse(raw.decode("utf-8")) for name, raw in source.items()}
    source_hashes = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in source.items()
    }
    closure_sha256 = _source_digest(source_hashes)
    exports = _importer_exports(trees, mods)
    capability_namespaces = _capability_namespaces(exports)
    # Exactly one immutable temporal analysis per module. Package owners live
    # only until their lazy summary and graph row are consumed; other owners
    # are built one at a time. Do not retain every module's journals at once.
    owners = {
        name: _strict_importers(
            tree, name, True, exports, capability_namespaces=capability_namespaces
        )
        for name, tree in trees.items()
        if mods[name].name == "__init__.py"
    }
    lazy = {
        name: _lazy_exports(trees[name], name, exports=exports, importers=owners[name])
        for name, path in mods.items()
        if path.name == "__init__.py"
    }

    def resolve(name: str) -> str | None:
        while name and name not in mods:
            name = name.rpartition(".")[0]
        return name or None

    graph = Graph()
    graph.package = package
    graph.source_closure_sha256 = closure_sha256
    for name, tree in trees.items():
        is_package = mods[name].name == "__init__.py"
        importers = (
            owners.pop(name)
            if name in owners
            else _strict_importers(
                tree,
                name,
                is_package,
                exports,
                capability_namespaces=capability_namespaces,
            )
        )
        graph.dynamic_problems[name] = _unresolvable_imports(
            tree, name, is_package, exports, importers=importers
        )
        graph.debt_sites[name] = _debt_site_metadata(
            tree,
            name,
            "packages/flow/" + mods[name].relative_to(root.parent).as_posix(),
            graph.dynamic_problems[name],
        )
        graph.admission_facts[name] = _admission_facts(
            tree,
            name,
            is_package,
            importers,
            mods[name].relative_to(root.parent).as_posix(),
            source_hashes[name],
            closure_sha256,
            trees,
        )
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


def _source_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _exact_import_module_callee(node, owner, machinery_mutated):
    """Exact raw binding with no source-known machinery mutation."""
    if machinery_mutated:
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return owner._resolve(func, func.id) == {"import_module"}
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and owner._resolve(func.value, func.value.id) == {"importlib"}
    )


def _admission_scope_declarations(nodes, parents):
    """Frozen equivalent of every enclosing-function Global/Nonlocal walk."""
    declarations = {}
    for node in nodes:
        if not isinstance(node, (ast.Global, ast.Nonlocal)):
            continue
        cursor = parents.get(id(node))
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declarations.setdefault(id(cursor), set()).update(node.names)
            cursor = parents.get(id(cursor))
    return {key: frozenset(names) for key, names in declarations.items()}


def _admission_statement_owners(nodes, lines):
    """One interval sweep, exactly matching closed statement source spans."""
    events = []
    for node in nodes:
        if isinstance(node, ast.stmt):
            events.append((node.lineno, 1, id(node)))
            events.append((node.end_lineno + 1, 0, id(node)))
    events.sort()
    active, result, index = set(), {}, 0
    for line in sorted(set(lines)):
        while index < len(events) and events[index][0] <= line:
            _, starts, identity = events[index]
            if starts:
                active.add(identity)
            else:
                active.discard(identity)
            index += 1
        result[line] = frozenset(active)
    return result


def _finite_guarded_call(
    node, tree, parents, owner, machinery_mutated, scope_declarations
):
    """An exact-string literal-membership guard dominates this loading call.

    A lookup table or configuration string by itself is not a runtime target
    bound. The immediately preceding guard must reject both non-native strings
    and names outside closed literal DATA before the importer is evaluated.
    """
    if not isinstance(node, ast.Call):
        return None
    if len(node.args) != 1 or node.keywords or not isinstance(node.args[0], ast.Name):
        return None
    if owner.called(node) != "import_module" or not _exact_import_module_callee(
        node, owner, machinery_mutated
    ):
        return None
    argument = node.args[0]
    # The guarded argument must reach the importer without executing a
    # compound statement, condition, loop, handler or other intervening code.
    # An ancestor guard does not bind a value reassigned inside that ancestor.
    containing = parents.get(id(node))
    if not (
        isinstance(containing, ast.Return)
        and containing.value is node
        or isinstance(containing, ast.Assign)
        and containing.value is node
        and len(containing.targets) == 1
        and isinstance(containing.targets[0], ast.Name)
    ):
        return None
    enclosing = parents.get(id(containing))
    if not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    index = enclosing.body.index(containing)
    if index == 0:
        return None
    guard = enclosing.body[index - 1]
    if not (
        isinstance(guard, ast.If)
        and not guard.orelse
        and len(guard.body) == 1
        and isinstance(guard.body[0], ast.Raise)
        and isinstance(guard.test, ast.BoolOp)
        and isinstance(guard.test.op, ast.Or)
        and len(guard.test.values) == 2
    ):
        return None
    if argument.id in scope_declarations.get(id(enclosing), ()):
        return None
    typed, bounded = guard.test.values
    if not (
        isinstance(typed, ast.Compare)
        and len(typed.ops) == 1
        and isinstance(typed.ops[0], ast.IsNot)
        and isinstance(typed.left, ast.Call)
        and isinstance(typed.left.func, ast.Name)
        and typed.left.func.id == "type"
        and not typed.left.keywords
        and len(typed.left.args) == 1
        and isinstance(typed.left.args[0], ast.Name)
        and typed.left.args[0].id == argument.id
        and len(typed.comparators) == 1
        and isinstance(typed.comparators[0], ast.Name)
        and typed.comparators[0].id == "str"
    ):
        return None
    if owner._resolve(typed.left.func, "type") or owner._resolve(
        typed.comparators[0], "str"
    ):
        return None
    if not (
        isinstance(bounded, ast.Compare)
        and len(bounded.ops) == 1
        and isinstance(bounded.ops[0], ast.NotIn)
        and isinstance(bounded.left, ast.Name)
        and bounded.left.id == argument.id
        and len(bounded.comparators) == 1
        and isinstance(bounded.comparators[0], ast.Tuple)
        and bounded.comparators[0].elts
        and all(_str(n) for n in bounded.comparators[0].elts)
    ):
        return None
    targets = sorted({_str(n) for n in bounded.comparators[0].elts})
    if not all(
        re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name) for name in targets
    ):
        return None
    if any(
        target in owner.capability_namespaces
        or any(
            target == root or target.startswith(root + ".")
            for root in (*_IMPORTER_MODULES, "sys")
        )
        for target in targets
    ):
        return None
    return {
        "proof_kind": "closed-runtime-target-guard",
        "targets": targets,
        "binding_sha256": _source_digest(ast.dump(guard, include_attributes=False)),
    }


def _literal_member_call(
    node, parents, owner, module, is_package, trees, machinery_mutated
):
    """A literal import followed by a fixed noncapability member projection."""
    if not isinstance(node, ast.Call):
        return None
    if len(node.args) != 1 or node.keywords or _str(node.args[0]) is None:
        return None
    parent = parents.get(id(node))
    if (
        not isinstance(parent, ast.Attribute)
        or parent.value is not node
        or parent.attr.startswith("__")
    ):
        return None
    # A projected importer, registry, class namespace or capable module is
    # never certified merely because its containing module name was literal.
    if owner.called(node) != "import_module" or not _exact_import_module_callee(
        node, owner, machinery_mutated
    ):
        return None
    roles = owner.roles(parent)
    direct = {
        *_DYNAMIC,
        *_IMPORTER_MODULES,
        "namespace:sys",
        "registry:sys.modules",
        *("namespace-factory:" + n for n in _NAMESPACE_FACTORIES),
    }
    if roles & direct or any(
        (r.removeprefix("module:") if r.startswith("module:") else r)
        in owner.capability_namespaces
        for r in roles
    ):
        return None
    targets = sorted(set(_dynamic_targets(node, module, owner, is_package=is_package)))
    if len(targets) != 1 or targets[0] not in trees:
        return None
    declarations = []
    for statement in trees[targets[0]].body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and statement.name == parent.attr
        ):
            declarations.append(statement)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                bound = alias.asname or (
                    alias.name.partition(".")[0]
                    if isinstance(statement, ast.Import)
                    else alias.name
                )
                if (
                    bound == parent.attr
                    and alias.name != "*"
                    and _import_binding(statement, alias) == "other"
                ):
                    declarations.append(statement)
        elif isinstance(statement, ast.Assign) and _literal_class_value(
            statement.value
        ):
            if any(
                isinstance(target, ast.Name) and target.id == parent.attr
                for target in statement.targets
            ):
                declarations.append(statement)
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and _literal_class_value(statement.value)
        ):
            if (
                isinstance(statement.target, ast.Name)
                and statement.target.id == parent.attr
            ):
                declarations.append(statement)
    if len(declarations) != 1:
        return None
    return {
        "proof_kind": "literal-member-projection",
        "targets": targets,
        "binding_sha256": _source_digest(
            {
                "attribute": parent.attr,
                "roles": sorted(roles),
                "declaration": ast.dump(declarations[0], include_attributes=False),
            }
        ),
    }


def _admission_facts(
    tree, module, is_package, owner, source_path, source_sha256, closure_sha256, trees
):
    nodes = tuple(ast.walk(tree))
    # The shared temporal owner already owns this immutable AST parent mapping.
    parents = owner._parents
    machinery_mutated = any(_known_capability_mutation(node, owner) for node in nodes)
    if machinery_mutated:
        return {}
    scope_declarations = _admission_scope_declarations(nodes, parents)
    provisional = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        value = _finite_guarded_call(
            node, tree, parents, owner, machinery_mutated, scope_declarations
        ) or _literal_member_call(
            node, parents, owner, module, is_package, trees, machinery_mutated
        )
        if value is None:
            continue
        ancestors = set()
        cursor = node
        statement = None
        while cursor is not None:
            ancestors.add(id(cursor))
            if statement is None and isinstance(cursor, ast.stmt):
                statement = cursor
            cursor = parents.get(id(cursor))
        terminal = (
            parents.get(id(node))
            if value["proof_kind"] == "literal-member-projection"
            else node
        )
        if not (
            isinstance(statement, ast.Return)
            and statement.value is terminal
            or isinstance(statement, ast.Assign)
            and statement.value is terminal
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        provisional.append((node, value, frozenset(ancestors)))
    if not provisional:
        return {}
    statement_owners = _admission_statement_owners(
        nodes, (node.lineno for node, _, _ in provisional)
    )
    candidates = {}
    for node, value, ancestors in provisional:
        # Exactly the old closed-span/nonancestor condition, without rescanning
        # every module statement for every otherwise proved candidate.
        if statement_owners[node.lineno] - ancestors:
            continue
        fact = {
            "path": source_path,
            "line": node.lineno,
            "source_sha256": source_sha256,
            "closure_sha256": closure_sha256,
            "span_ast_sha256": _source_digest(ast.dump(node, include_attributes=False)),
            **value,
        }
        candidates.setdefault(node.lineno, []).append(fact)
    return {line: values[0] for line, values in candidates.items() if len(values) == 1}


def _resolved_import_candidates(graph):
    """Read-only proposed DATA, not an admission or a safety verdict."""
    if not isinstance(graph, Graph) or set(graph.dynamic_problems) != set(graph):
        raise ValueError("missing dynamic import analysis")
    entries, blocked = [], []
    for module in sorted(graph.dynamic_problems):
        for problem in graph.dynamic_problems[module]:
            match = re.fullmatch(
                re.escape(module)
                + r":([0-9]+): unresolvable dynamic import \([^\n]+\)",
                problem,
            )
            fact = (
                graph.admission_facts.get(module, {}).get(int(match.group(1)))
                if match
                else None
            )
            if fact is None:
                blocked.append(problem)
            elif not any(
                entry["path"] == fact["path"] and entry["line"] == fact["line"]
                for entry in entries
            ):
                # A DATA consumer receives a detached copy, never the mutable
                # lists backing the independently produced source evidence.
                entries.append(
                    {
                        name: list(value) if type(value) is list else value
                        for name, value in fact.items()
                    }
                )
    return {
        "proposed_entries": entries,
        "blocked": blocked,
        "safety_verdict": False,
        "runtime_or_policy_changed": False,
    }


def _admission_schema_problems(entries):
    if type(entries) is not list:
        return ["resolved dynamic import DATA must be a list"]
    required = {
        "path",
        "line",
        "source_sha256",
        "closure_sha256",
        "span_ast_sha256",
        "proof_kind",
        "targets",
        "binding_sha256",
    }
    seen = set()
    for entry in entries:
        if type(entry) is not dict or set(entry) != required:
            return ["invalid resolved dynamic import DATA fields"]
        if (
            type(entry["path"]) is not str
            or not entry["path"]
            or entry["path"].startswith("/")
            or "\\" in entry["path"]
            or any(part in ("", ".", "..") for part in entry["path"].split("/"))
            or not entry["path"].endswith(".py")
            or type(entry["line"]) is not int
            or entry["line"] <= 0
        ):
            return ["invalid resolved dynamic import source location"]
        key = (entry["path"], entry["line"])
        if key in seen:
            return ["duplicate resolved dynamic import source location"]
        seen.add(key)
        if any(
            type(entry[name]) is not str
            or not re.fullmatch("[0-9a-f]{64}", entry[name])
            for name in (
                "source_sha256",
                "closure_sha256",
                "span_ast_sha256",
                "binding_sha256",
            )
        ):
            return ["invalid resolved dynamic import source binding"]
        if type(entry["proof_kind"]) is not str or entry["proof_kind"] not in (
            "closed-runtime-target-guard",
            "literal-member-projection",
        ):
            return ["unknown resolved dynamic import proof kind"]
        targets = entry["targets"]
        if (
            type(targets) is not list
            or not targets
            or any(
                type(target) is not str
                or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", target)
                for target in targets
            )
            or targets != sorted(set(targets))
        ):
            return ["invalid or noncanonical resolved dynamic import targets"]
    return []


def _admitted_graph(graph, contract):
    """Literal-equivalent target DATA cannot erase an unknown diagnostic."""
    entries = contract.get("resolved_dynamic_imports", [])
    if type(entries) is list and not entries:
        return graph
    if not isinstance(graph, Graph) or set(graph.dynamic_problems) != set(graph):
        return graph
    result = Graph()
    result.update(
        {
            name: {target: list(lines) for target, lines in edges.items()}
            for name, edges in graph.items()
        }
    )
    result.dynamic_problems = {
        name: list(values) for name, values in graph.dynamic_problems.items()
    }
    result.admission_facts = graph.admission_facts
    result.source_closure_sha256 = graph.source_closure_sha256
    result.package = graph.package
    result.admitted_dynamic_problems = list(graph.admitted_dynamic_problems)
    result.admission_problems = list(graph.admission_problems)
    invalid = _admission_schema_problems(entries)
    if invalid:
        result.admission_problems.extend(invalid)
        return result
    for entry in entries:
        matches = [
            (module, facts[entry["line"]])
            for module, facts in graph.admission_facts.items()
            if entry["line"] in facts and facts[entry["line"]]["path"] == entry["path"]
        ]
        if len(matches) != 1 or entry != matches[0][1]:
            result.admission_problems.append(
                "stale or unproved resolved dynamic import DATA"
            )
            continue
        module, fact = matches[0]
        prefix = f"{module}:{entry['line']}: unresolvable dynamic import ("
        problems = [v for v in graph.dynamic_problems[module] if v.startswith(prefix)]
        if not problems:
            # Applying the same immutable DATA twice must be harmless, while
            # a genuinely unused declaration remains stale and is refused.
            already = any(
                v.startswith(prefix)
                for v in getattr(graph, "admitted_dynamic_problems", [])
            )
            if not already:
                result.admission_problems.append("unused resolved dynamic import DATA")
                continue
        result.dynamic_problems[module] = [
            v for v in result.dynamic_problems[module] if not v.startswith(prefix)
        ]
        result.admitted_dynamic_problems.extend(problems)
        for target in fact["targets"]:
            if target == "benchmark" or target.startswith("benchmark."):
                resolved = "benchmark"
            elif graph.package and (
                target == graph.package or target.startswith(graph.package + ".")
            ):
                resolved = target
                while resolved and resolved not in graph:
                    resolved = resolved.rpartition(".")[0]
            else:
                # Same treatment as a literal external import: policy prefixes
                # still see the exact target, without inventing source modules.
                resolved = target
            while resolved:
                if resolved != module:
                    result[module].setdefault(resolved, [])
                    result[module][resolved] = sorted(
                        set(result[module][resolved]) | {entry["line"]}
                    )
                if not graph.package or not (
                    resolved == graph.package
                    or resolved.startswith(graph.package + ".")
                ):
                    break
                resolved = resolved.rpartition(".")[0]
    return result


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


def _dynamic_problems(graph: Graph, contract: dict, *, transitive: bool) -> list[str]:
    """A graph without its unresolved evidence can never certify safety."""
    if not isinstance(graph, Graph) or set(graph.dynamic_problems) != set(graph):
        return ["missing dynamic import analysis"]
    pending = [module for module in graph if _under(module, contract["core"])]
    reached = set()
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        if transitive:
            pending.extend(target for target in graph[module] if target in graph)
    return [
        *graph.admission_problems,
        *[
            problem
            for module in sorted(reached)
            for problem in graph.dynamic_problems[module]
        ],
    ]


DEBT_BASELINE_SHA256 = (
    "db114de9c693d96d08e6d32d1f23bc5df870f927703a12f2e19ba5f6abe84091"
)


# Repository migration ceiling only. Raw violation APIs remain strict.
_DEBT_FOLLOW_UP = "unresolved-import-provider-migration"
_DEBT_ROW_KEYS = {
    "module",
    "path",
    "line",
    "edge_kind",
    "diagnostic",
    "reason",
    "follow_up",
    "source_statement_sha256",
}


def _debt_message_parts(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([1-9][0-9]*): "
        r"unresolvable dynamic import \(([^()\n]+)\)",
        value,
    )
    return (match[1], int(match[2])) if match else None


def _debt_site_metadata(tree, module, path, problems):
    """Bind unresolved sites to existing parsed source, without admitting them."""
    if not problems:
        return {}
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)]
    found = {}
    for problem in problems:
        parts = _debt_message_parts(problem)
        if parts is None or parts[0] != module:
            continue
        line = parts[1]
        enclosing = [
            node for node in statements if node.lineno <= line <= node.end_lineno
        ]
        if not enclosing:
            continue
        span = min(node.end_lineno - node.lineno for node in enclosing)
        smallest = [node for node in enclosing if node.end_lineno - node.lineno == span]
        if len(smallest) != 1:
            # Multiple statements on the diagnosed line cannot identify one site.
            continue
        found[problem] = {
            "module": module,
            "path": path,
            "line": line,
            "diagnostic": problem,
            "source_statement_sha256": hashlib.sha256(
                ast.dump(smallest[0], include_attributes=False).encode("utf-8")
            ).hexdigest(),
        }
    return found


def _debt_rows_problems(rows):
    if type(rows) is not list:
        return ["unresolved debt entries must be a list"]
    problems = []
    encoded = []
    for row in rows:
        if type(row) is not dict or set(row) != _DEBT_ROW_KEYS:
            problems.append("unresolved debt row has an unknown or missing field")
            continue
        parts = _debt_message_parts(row["diagnostic"])
        path = row["path"]
        if (
            parts != (row["module"], row["line"])
            or type(row["line"]) is not int
            or row["line"] <= 0
            or type(path) is not str
            or not re.fullmatch(
                r"packages/flow/openadapt_flow/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py", path
            )
            or row["edge_kind"] not in ("direct", "transitive")
            or row["reason"]
            not in ("unbounded-config", "namespace-query", "vision-transfer")
            or row["follow_up"] != _DEBT_FOLLOW_UP
            or type(row["source_statement_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", row["source_statement_sha256"]) is None
        ):
            problems.append("unresolved debt row is malformed or unbound")
            continue
        encoded.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    if len(encoded) != len(set(encoded)):
        problems.append("duplicate unresolved debt membership")
    return problems


def _debt_row_differences(live_rows, baseline_rows):
    """Exact membership and multiplicity; stale debt must be removed explicitly."""
    problems = [*_debt_rows_problems(live_rows), *_debt_rows_problems(baseline_rows)]
    if problems:
        return problems
    # Lists retain multiplicity. No set projection can hide repeated results.
    live = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":")) for row in live_rows
    )
    baseline = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":")) for row in baseline_rows
    )
    if live != baseline:
        return [
            "new, changed or stale unresolved debt membership; review and shrink the committed baseline"
        ]
    return []


def repository_debt_violations(graph, raw_results, baseline_bytes, *, edge_kind):
    """Repository debt equality, not capability safety or dynamic-target admission.

    Every raw forbidden/unknown result refuses. Only an exact, source-bound,
    reviewed unresolved row can satisfy this explicit migration policy.
    """
    if (
        type(baseline_bytes) is not bytes
        or hashlib.sha256(baseline_bytes).hexdigest() != DEBT_BASELINE_SHA256
    ):
        return ["unreviewed unresolved debt baseline bytes"]
    try:
        baseline = json.loads(baseline_bytes)
    except (ValueError, UnicodeDecodeError):
        return ["unreadable unresolved debt baseline"]
    if (
        type(baseline) is not dict
        or set(baseline)
        != {"schema", "kind", "follow_up", "observed_receipt", "entries"}
        or type(baseline["schema"]) is not int
        or baseline["schema"] != 1
        or baseline["kind"] != "unresolved-import-debt-not-admission"
        or baseline["follow_up"] != _DEBT_FOLLOW_UP
        or edge_kind not in ("direct", "transitive")
    ):
        return ["invalid unresolved debt document"]
    receipt = baseline["observed_receipt"]
    if (
        type(receipt) is not dict
        or set(receipt) != {"source_head", "module_count", "edge_digest"}
        or type(receipt["module_count"]) is not int
        or receipt["module_count"] <= 0
        or type(receipt["source_head"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", receipt["source_head"]) is None
        or type(receipt["edge_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", receipt["edge_digest"]) is None
    ):
        return ["invalid unresolved debt historical receipt"]
    problems = _debt_rows_problems(baseline["entries"])
    if problems:
        return problems
    if (
        not isinstance(graph, Graph)
        or type(raw_results) is not list
        or set(graph.dynamic_problems) != set(graph)
        or set(graph.debt_sites) != set(graph)
    ):
        return ["missing strict unresolved debt analysis"]
    expected = [row for row in baseline["entries"] if row["edge_kind"] == edge_kind]
    live = []
    for problem in raw_results:
        parts = _debt_message_parts(problem)
        if parts is None:
            problems.append("strict import violation outside unresolved debt baseline")
            continue
        module, _line = parts
        metadata = graph.debt_sites.get(module, {}).get(problem)
        if not isinstance(metadata, dict):
            problems.append(
                "unresolved debt site has absent or ambiguous source binding"
            )
            continue
        matches = [
            row
            for row in expected
            if all(row.get(key) == value for key, value in metadata.items())
        ]
        if len(matches) != 1:
            problems.append("new or changed source-bound unresolved debt site")
            continue
        # Only the reviewed reason/follow-up annotations come from baseline.
        # Every executable-site field is independently supplied by parsed source.
        live.append(
            {
                **metadata,
                "edge_kind": edge_kind,
                "reason": matches[0]["reason"],
                "follow_up": matches[0]["follow_up"],
            }
        )
    return [*problems, *_debt_row_differences(live, expected)]


def _retain_repository_debt_observation(
    graph, raw_results, policy_results, baseline_bytes, *, edge_kind
):
    """Detach test-owned receipts; never rerun validators or certify safety."""
    rows = ()
    bound = False
    if (
        type(baseline_bytes) is bytes
        and hashlib.sha256(baseline_bytes).hexdigest() == DEBT_BASELINE_SHA256
    ):
        try:
            document = json.loads(baseline_bytes)
            rows = tuple(
                tuple(sorted(row.items()))
                for row in document["entries"]
                if row["edge_kind"] == edge_kind
            )
            bound = True
        except (ValueError, KeyError, TypeError):
            rows = ()
    graph.repository_debt_observations[edge_kind] = {
        "raw_validator_results": tuple(raw_results),
        "repository_policy_results": tuple(policy_results),
        "reviewed_debt_memberships": rows,
        "baseline_sha256": DEBT_BASELINE_SHA256,
        "baseline_bound": bound,
        "target_admission": False,
        "capability_safety_claim": False,
        "raw_validator_calls": 1,
        "repository_policy_calls": 1,
    }


@pytest.fixture(scope="module")
def unresolved_debt_baseline() -> bytes:
    return (ROOT.parent / "import_layers_debt.json").read_bytes()


def direct_violations(graph: Graph, contract: dict) -> list[str]:
    graph = _admitted_graph(graph, contract)
    found = _dynamic_problems(graph, contract, transitive=False)
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

    graph = _admitted_graph(graph, contract)
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
    graph = _admitted_graph(graph, contract)
    allowed = {(e["importer"], e["imported"]) for e in contract["exceptions"]}
    return [
        *_dynamic_problems(graph, contract, transitive=True),
        *[
            " > ".join(path)
            for edge, path in sorted(reached_edges(graph, contract).items())
            if edge not in allowed
        ],
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
    problems.extend(
        _admission_schema_problems(contract.get("resolved_dynamic_imports", []))
    )
    return problems


@pytest.fixture(scope="module")
def contract() -> dict:
    loaded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert policy_problems(loaded) == []
    return loaded


@pytest.fixture(scope="module")
def graph() -> Graph:
    return build_graph(ROOT, "openadapt_flow")


def test_core_imports_nothing_forbidden_directly(
    graph: Graph, contract: dict, unresolved_debt_baseline: bytes
) -> None:
    raw_results = direct_violations(graph, contract)
    policy_results = repository_debt_violations(
        graph, raw_results, unresolved_debt_baseline, edge_kind="direct"
    )
    _retain_repository_debt_observation(
        graph, raw_results, policy_results, unresolved_debt_baseline, edge_kind="direct"
    )
    assert policy_results == [], (
        "exact unresolved debt ceiling only; raw diagnostics remain unresolved"
    )


def test_core_reaches_nothing_forbidden(
    graph: Graph, contract: dict, unresolved_debt_baseline: bytes
) -> None:
    raw_results = transitive_violations(graph, contract)
    policy_results = repository_debt_violations(
        graph, raw_results, unresolved_debt_baseline, edge_kind="transitive"
    )
    _retain_repository_debt_observation(
        graph,
        raw_results,
        policy_results,
        unresolved_debt_baseline,
        edge_kind="transitive",
    )
    assert policy_results == [], (
        "exact unresolved debt ceiling only; raw diagnostics remain unresolved"
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


def test_checker_refuses_a_dynamic_import_it_cannot_read(tmp_path: Path) -> None:
    engine = (
        "import importlib\ndef go(name):\n    return importlib.import_module(name)\n"
    )
    graph = _plant(tmp_path, **{"core/engine.py": engine})
    assert direct_violations(graph, _PLANTED_CONTRACT) == [
        "planted.core.engine:3: unresolvable dynamic import (nonliteral or unresolved import target)"
    ]


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
    problems = transitive_violations(graph, _PLANTED_CONTRACT)
    # Lazy dependency projection still finds the same forbidden edge. A table
    # lookup is additionally refused by the strict literal-only import rule.
    unresolved = [
        problem for problem in problems if "unresolvable dynamic import" in problem
    ]
    assert [problem for problem in problems if problem not in unresolved] == [
        "planted.core.engine > planted.helper > planted.ui.window"
    ]
    assert bool(unresolved) is ("_LAZY_EXPORTS" in ui or "_T =" in ui)
    clean = _plant(
        tmp_path,
        **{"ui/__init__.py": ui, "helper.py": "from planted.ui import text\n"},
    )
    # Even selecting an otherwise allowed export does not authorize the
    # unresolved importer site in the same reachable package initializer.
    assert transitive_violations(clean, _PLANTED_CONTRACT) == unresolved


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

    def _branch_checkpoint(self):
        return self._checkpoint()

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


# Source-only proposed append to tests/test_import_layers.py. UNRUN.
# Reuses that module's normal imports, Graph, _plant and _PLANTED_CONTRACT.

_STRICT_BINDING_CAPABILITIES = (
    ("import importlib\n", "importlib.import_module"),
    ("import importlib as machinery\n", "machinery.import_module"),
    ("from importlib import import_module as load\n", "load"),
    ("import importlib\n", "importlib.__import__"),
    ("from importlib import __import__ as load\n", "load"),
    ("import builtins\n", "builtins.__import__"),
    ("import builtins as machinery\n", "machinery.__import__"),
    ("from builtins import __import__ as load\n", "load"),
    ("", "__import__"),
)

_STRICT_BINDING_ESCAPES = (
    "saved = {cap}\nsaved('planted.ui.text')\n",
    "(saved := {cap})('planted.ui.text')\n",
    "[{cap}][0]('planted.ui.text')\n",
    "{{'load': {cap}}}['load']('planted.ui.text')\n",
    "def apply(loader):\n    return loader('planted.ui.text')\napply({cap})\n",
    "def factory():\n    return {cap}\nfactory()('planted.ui.text')\n",
    "def deferred(loader={cap}):\n    return loader('planted.ui.text')\n",
    "def values():\n    yield {cap}\nnext(values())('planted.ui.text')\n",
    "class Holder:\n    pass\nHolder.loader = {cap}\n",
    "factory = lambda: {cap}\nfactory()('planted.ui.text')\n",
    "from functools import partial\npartial({cap}, 'planted.ui.text')()\n",
    "[loader('planted.ui.text') for loader in [{cap}]]\n",
)


def _strict_binding_graph(tmp_path, source, placement):
    changes = {"helper.py": "", "core/engine.py": source}
    if placement == "reachable_helper":
        changes = {
            "core/engine.py": "from planted import helper\n",
            "helper.py": source,
        }
    return _plant(tmp_path, **changes)


def _assert_strict_binding_result(graph, placement, refused):
    direct = direct_violations(graph, _PLANTED_CONTRACT)
    transitive = transitive_violations(graph, _PLANTED_CONTRACT)
    assert bool(transitive) is refused
    assert bool(direct) is (refused and placement == "core")


@pytest.mark.parametrize("capability", _STRICT_BINDING_CAPABILITIES)
@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
def test_strict_dynamic_literals_keep_supported_import_aliases(
    tmp_path, capability, placement
):
    setup, cap = capability
    source = setup + f"def go():\n    return {cap}('planted.ui.text')\n"
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, False
    )


@pytest.mark.parametrize("capability", _STRICT_BINDING_CAPABILITIES)
@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize("escape", _STRICT_BINDING_ESCAPES)
def test_importer_capabilities_cannot_escape_the_literal_call_grammar(
    tmp_path, capability, placement, escape
):
    setup, cap = capability
    source = setup + escape.format(cap=cap)
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, True
    )


@pytest.mark.parametrize("capability", _STRICT_BINDING_CAPABILITIES)
@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source_target",
    [
        "NAME = 'planted.ui.text'\n{cap}(NAME)\n",
        "def go():\n    name = 'planted.ui.text'\n    return {cap}(name)\n",
        "{cap}('planted.' + 'ui.text')\n",
        "{cap}(f'planted.ui.text')\n",
        "{cap}((name := 'planted.ui.text'))\n",
        "{cap}(name='planted.ui.text' if flag else 'planted.demo.app')\n",
        "{cap}(*['planted.ui.text'])\n",
        "{cap}(**{{'name': 'planted.ui.text'}})\n",
    ],
)
def test_nonliteral_targets_refuse_even_when_the_value_spells_an_allowed_module(
    tmp_path, capability, placement, source_target
):
    setup, cap = capability
    source = setup + source_target.format(cap=cap)
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, True
    )


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nm = importlib\nm.import_module('planted.ui.text')\n",
        "import builtins\nm = builtins\nm.__import__('planted.ui.text')\n",
        "import importlib\n(m := importlib).import_module('planted.ui.text')\n",
        "from importlib import *\nimport_module('planted.ui.text')\n",
        "from builtins import *\n__import__('planted.ui.text')\n",
        "import importlib\ngetattr(importlib, 'import_module')('planted.ui.text')\n",
        "import importlib\nvars(importlib)['import_module']('planted.ui.text')\n",
        "import importlib\nimportlib.__dict__['import_module']('planted.ui.text')\n",
        "__builtins__['__import__']('planted.ui.text')\n",
        "getattr(__builtins__, '__import__')('planted.ui.text')\n",
        "import sys\nsys.modules['importlib'].import_module('planted.ui.text')\n",
        "import sys\nsys.modules.get('builtins').__import__('planted.ui.text')\n",
        "__import__('importlib').import_module('planted.ui.text')\n",
        "getattr(__import__('builtins'), '__import__')('planted.ui.text')\n",
    ],
)
def test_known_importer_namespace_and_reflection_escapes_refuse(
    tmp_path, placement, source
):
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, True
    )


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nPACKAGE = 'planted.ui'\nimportlib.import_module('.window', PACKAGE)\n",
        "import importlib\nimportlib.import_module('.window', package=package)\n",
        "names = ['window']\n__import__('planted.ui', fromlist=names)\n",
        "__import__('planted.ui', fromlist=('window',) if flag else ())\n",
        "options = {'fromlist': ['window']}\n__import__('planted.ui', **options)\n",
        "__import__('window', level=level)\n",
        "__import__('window', level=1)\n",
    ],
)
def test_importer_ancillary_arguments_cannot_hide_an_unresolved_submodule(
    tmp_path, placement, source
):
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, True
    )


@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("levels", [1, 2])
@pytest.mark.parametrize("module_value", [False, True])
def test_importer_reexports_have_the_same_literal_boundary(
    tmp_path, literal, levels, module_value
):
    cap = "loader.import_module" if module_value else "loader"
    helper = (
        "import importlib as loader\n"
        if module_value
        else "from importlib import import_module as loader\n"
    )
    source_module = "bridge" if levels == 2 else "helper"
    source = f"from planted.{source_module} import loader\n"
    source += f"{cap}('planted.ui.text')\n" if literal else f"{cap}(name)\n"
    graph = _plant(
        tmp_path,
        **{
            "helper.py": helper,
            "bridge.py": "from planted.helper import loader\n",
            "core/engine.py": source,
        },
    )
    _assert_strict_binding_result(graph, "core", not literal)


@pytest.mark.parametrize(
    "export",
    [
        "from importlib import import_module as loader\n",
        "import importlib as loader\n",
        "from builtins import __import__ as loader\n",
    ],
)
def test_star_import_of_a_known_importer_reexport_refuses(tmp_path, export):
    graph = _plant(
        tmp_path,
        **{"helper.py": export, "core/engine.py": "from planted.helper import *\n"},
    )
    _assert_strict_binding_result(graph, "core", True)


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source",
    [
        "def import_module(name):\n    return name\nimport_module(name)\n",
        "def __import__(name):\n    return name\n__import__(name)\n",
        "import importlib\ndef go(importlib, name):\n    return importlib.import_module(name)\n",
        "from importlib import import_module as load\nload = str\nload(name)\n",
        "def go(registry, name):\n    return registry.import_module(name)\n",
        "from planted.ui.text import *\n",
        "class Registry:\n    def import_module(self, name):\n        return name\nRegistry().import_module(name)\n",
    ],
)
def test_strict_importer_model_does_not_invent_shadowed_capabilities(
    tmp_path, placement, source
):
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, False
    )


def test_unreachable_importer_escape_does_not_become_a_core_path(tmp_path):
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "",
            "helper.py": "import importlib\nvalue = importlib.import_module\n",
        },
    )
    _assert_strict_binding_result(graph, "core", False)


@pytest.mark.parametrize("copy_kind", ["dict", "copy"])
def test_copying_graph_cannot_strip_default_strict_refusal(tmp_path, copy_kind):
    graph = _strict_binding_graph(
        tmp_path,
        "import importlib\nimportlib.import_module(name)\n",
        "core",
    )
    copied = dict(graph) if copy_kind == "dict" else graph.copy()
    assert direct_violations(copied, _PLANTED_CONTRACT)
    assert transitive_violations(copied, _PLANTED_CONTRACT)


@given(
    commands=st.lists(
        st.sampled_from(
            [
                "shadow",
                "restore",
                "delete",
                "conditional_shadow",
                "literal",
                "dynamic",
                "copy",
            ]
        ),
        min_size=1,
        max_size=20,
    ),
    placement=st.sampled_from(["core", "reachable_helper"]),
)
def test_default_strict_validation_matches_a_finite_binding_escape_state_model(
    tmp_path_factory, commands, placement
):
    # Independent finite MAY-state oracle. No Python program or importer runs.
    possible_importer = True
    refused = False
    body = ["from importlib import import_module as loader"]
    for command in commands:
        if command == "shadow":
            body.append("loader = str")
            possible_importer = False
        elif command == "restore":
            body.append("from importlib import import_module as loader")
            possible_importer = True
        elif command == "delete":
            body.append("del loader")
            possible_importer = False
        elif command == "conditional_shadow":
            body.append("if flag:\n    loader = str")
            # An optional ordinary overwrite cannot erase an importing path.
        elif command == "literal":
            body.append("loader('planted.ui.text')")
        elif command == "dynamic":
            body.append("loader(name)")
            refused |= possible_importer
        else:
            body.append("saved = loader")
            refused |= possible_importer
    source = "def run(name, flag):\n" + indent("\n".join(body), "    ") + "\n"
    ast.parse(source)
    root = tmp_path_factory.mktemp("strict_binding_state")
    _assert_strict_binding_result(
        _strict_binding_graph(root, source, placement), placement, refused
    )


# Source-only additional append; uses helpers from PROPOSED_TEST_BLOCK.py.


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module.__call__(name)\n",
        "import builtins\nbuiltins.__import__.__call__(name)\n",
        "from importlib import import_module as load\nload.__call__(name)\n",
        "import importlib\nimportlib.__getattribute__('import_module')(name)\n",
        "import builtins\nbuiltins.__getattribute__('__import__')(name)\n",
        "import importlib\nimportlib.import_module.__get__(None)(name)\n",
        "import importlib\nimportlib.import_module.__getattribute__('__call__')(name)\n",
    ],
)
def test_attribute_traversal_cannot_erase_a_known_importer_capability(
    tmp_path, placement, source
):
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, True
    )


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "source",
    [
        "loader = str\nloader.__call__(name)\n",
        "from importlib import import_module as load\nload = str\nload.__call__(name)\n",
        "def go(registry, name):\n    return registry.__getattribute__('import_module')(name)\n",
    ],
)
def test_ordinary_shadowed_attribute_traversal_is_not_an_importer(
    tmp_path, placement, source
):
    ast.parse(source)
    _assert_strict_binding_result(
        _strict_binding_graph(tmp_path, source, placement), placement, False
    )


@pytest.mark.parametrize("placement", ["core", "reachable_helper"])
@pytest.mark.parametrize(
    "namespace,refused", [("opaque_dependency", True), ("planted.ui.text", False)]
)
def test_star_import_requires_a_known_capability_free_source(
    tmp_path, placement, namespace, refused
):
    graph = _strict_binding_graph(tmp_path, f"from {namespace} import *\n", placement)
    _assert_strict_binding_result(graph, placement, refused)


@pytest.mark.parametrize("prelude,cap", _STRICT_BINDING_CAPABILITIES[:-1])
@pytest.mark.parametrize("placement", ("core", "reachable"))
@pytest.mark.parametrize(
    "use",
    (
        "unused",
        "read",
        "argument",
        "stored",
        "reexport",
        "base",
        "descriptor",
        "decorator",
        "globals",
        "locals",
    ),
)
def test_class_owned_importer_namespace_follows_the_shared_binding_model(
    tmp_path: Path, prelude: str, cap: str, placement: str, use: str
) -> None:
    # Class bodies own their aliases. Unqualified names in methods still use
    # normal lexical lookup, not that class namespace. Reading the class value
    # or an implicit namespace consumer conservatively exposes its capability.
    class_source = (
        "class Holder:\n"
        + indent(prelude, "    ")
        + "    def method(self):\n        return 'ordinary'\n"
    )
    tail = {
        "unused": "",
        "read": "Holder.attribute\n",
        "argument": "consume(Holder)\n",
        "stored": "saved = Holder\n",
        "reexport": "",  # The core's normal import reads it below.
        "base": "",
        "descriptor": "",
        "decorator": "",
        "globals": "globals()['Holder']\n",
        "locals": "locals()['Holder']\n",
    }[use]
    if use == "base":
        class_source = class_source.replace("class Holder:", "class Holder(Base):")
    elif use == "descriptor":
        class_source += "    descriptor = make_descriptor()\n"
    elif use == "decorator":
        class_source = "@decorate\n" + class_source
    body = class_source + tail
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources = {target: body}
    if use == "reexport":
        # Import transfer preserves class ownership; the actual class read,
        # rather than absence of an API or a guessed target, is what refuses.
        sources[target] = (
            "from ..helper import Holder\nHolder.attribute\n"
            if placement == "core"
            else body
        )
        sources["helper.py"] = body if placement == "core" else body
        if placement == "reachable":
            sources["core/engine.py"] = (
                "from ..helper import Holder\nHolder.attribute\n"
            )
    elif placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reachable = transitive_violations(graph, _PLANTED_CONTRACT)
    if use == "unused":
        assert own == []
        assert reachable == []
    else:
        assert any("unresolvable dynamic import" in row for row in reachable)
        if placement == "core" or use == "reexport":
            assert any("unresolvable dynamic import" in row for row in own)


@pytest.mark.parametrize(
    "member",
    ("ordinary = 1", "def ordinary(self):\n    return 1", "class Nested:\n    pass"),
)
def test_internal_star_with_capability_free_class_namespace_is_not_an_escape(
    tmp_path: Path, member: str
) -> None:
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from ..helper import *\n",
            "helper.py": "class Ordinary:\n" + indent(member, "    ") + "\n",
        },
    )
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


def _strict_journal_evidence(owner):
    return (
        owner._position,
        tuple(
            (
                scope.kind,
                tuple(
                    (
                        channel,
                        tuple(
                            (name, tuple(events))
                            for name, events in getattr(scope, channel).items()
                        ),
                    )
                    for channel in ("bindings", "writes")
                ),
            )
            for scope in owner._scopes
        ),
    )


def test_graph_constructs_one_temporal_owner_per_module_and_all_consumers_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Count genuine normal construction, not an elapsed-time assertion. The
    # original uncached/journal equivalence families remain unchanged below.
    created, observed = {}, {"lazy": 0, "problems": 0, "targets": 0}
    construct = _strict_importers

    def once(tree, module, *args, **kwargs):
        assert module not in created
        owner = construct(tree, module, *args, **kwargs)
        created[module] = owner
        return owner

    monkeypatch.setitem(globals(), "_strict_importers", once)
    for label, name in (
        ("lazy", "_lazy_exports"),
        ("problems", "_unresolvable_imports"),
        ("targets", "_dynamic_targets"),
    ):
        original = globals()[name]

        def checked(*args, _label=label, _original=original, **kwargs):
            owner = kwargs.get("importers") if _label != "targets" else args[2]
            assert owner is not None and owner in created.values()
            before = _strict_journal_evidence(owner)
            value = _original(*args, **kwargs)
            assert _strict_journal_evidence(owner) == before
            observed[_label] += 1
            return value

        monkeypatch.setitem(globals(), name, checked)
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from ..helper import Value\n",
            "helper/__init__.py": "def __getattr__(name):\n    if name == 'Value':\n        from ..ui.text import Value\n        return Value\n    raise AttributeError(name)\n",
            "ui/text.py": "from .window import window\nValue = window\n",
        },
    )
    assert set(created) == set(graph)
    assert len({id(owner) for owner in created.values()}) == len(graph)
    assert (
        observed["lazy"] > 0
        and observed["problems"] == len(graph)
        and observed["targets"] > 0
    )
    assert any(
        "planted.ui.text" in row
        for row in transitive_violations(graph, _PLANTED_CONTRACT)
    )


@pytest.mark.parametrize("prelude,cap", _STRICT_BINDING_CAPABILITIES)
def test_shared_owner_query_order_preserves_full_journal_and_diagnostics(
    prelude: str, cap: str
) -> None:
    source = (
        prelude
        + "def __getattr__(name):\n    if name == 'Value':\n        return "
        + cap
        + "('planted.ui.text')\n    raise AttributeError(name)\n"
    )
    tree = ast.parse(source)
    module = "planted.helper"
    mods = {module: Path("__init__.py")}
    exports = _importer_exports({module: tree}, mods)
    owner = _strict_importers(tree, module, True, exports)
    before = _strict_journal_evidence(owner)
    first_lazy = _lazy_exports(tree, module, exports=exports, importers=owner)
    first_problems = _unresolvable_imports(tree, module, True, exports, importers=owner)
    second_problems = _unresolvable_imports(
        tree, module, True, exports, importers=owner
    )
    second_lazy = _lazy_exports(tree, module, exports=exports, importers=owner)
    assert first_lazy == second_lazy
    assert first_problems == second_problems
    assert _strict_journal_evidence(owner) == before


@pytest.mark.parametrize("prelude,cap", _STRICT_BINDING_CAPABILITIES[:-1])
@pytest.mark.parametrize(
    "use", ("unused", "read", "return", "locals", "method-shadow", "implicit-default")
)
def test_function_local_class_namespace_has_its_own_capability_boundary(
    tmp_path: Path, prelude: str, cap: str, use: str
) -> None:
    body = (
        "class Holder:\n"
        + indent(prelude, "    ")
        + "    def method(self):\n        return 'ordinary'\n"
    )
    if use == "implicit-default":
        body = (
            "class Holder:\n"
            + indent(prelude, "    ")
            + "    def method(self, captured=locals()):\n        return captured\n"
        )
    tail = {
        "unused": "return 'ordinary'\n",
        "read": "return Holder.attribute\n",
        "return": "return Holder\n",
        "locals": "return locals()['Holder']\n",
        "method-shadow": "return 'ordinary'\n",
        "implicit-default": "return 'ordinary'\n",
    }[use]
    if use == "method-shadow":
        root = cap.partition(".")[0]
        body = (
            "class Holder:\n"
            + indent(prelude, "    ")
            + "    def method(self):\n        return "
            + root
            + "('planted.demo.app')\n"
        )
    source = (
        "def "
        + (cap.partition(".")[0] if use == "method-shadow" else "ordinary")
        + "(name):\n    return name\n"
    )
    source += "def build():\n" + indent(body + tail, "    ")
    graph = _plant(tmp_path, **{"core/engine.py": source})
    direct = direct_violations(graph, _PLANTED_CONTRACT)
    transitive = transitive_violations(graph, _PLANTED_CONTRACT)
    if use in {"unused", "method-shadow"}:
        assert direct == []
        assert transitive == []
    else:
        assert any("unresolvable dynamic import" in row for row in direct)
        assert any("unresolvable dynamic import" in row for row in transitive)


@pytest.mark.parametrize("factory", ("globals", "locals", "vars"))
@pytest.mark.parametrize("placement", ("core", "reachable"))
@pytest.mark.parametrize(
    "form",
    (
        "bare",
        "from-alias",
        "module-attribute",
        "getattr",
        "star",
        "reexport",
        "reassigned",
        "deleted",
        "default",
        "class-default",
    ),
)
def test_namespace_factory_bindings_have_the_same_fail_closed_escape_model(
    tmp_path: Path, factory: str, placement: str, form: str
) -> None:
    if form == "bare":
        source = factory + "()\n"
    elif form in {"from-alias", "reexport"}:
        source = "from builtins import " + factory + " as read\nread()\n"
    elif form == "module-attribute":
        source = "import builtins as namespace\nnamespace." + factory + "()\n"
    elif form == "getattr":
        source = (
            "import builtins as namespace\ngetattr(namespace, '" + factory + "')()\n"
        )
    elif form == "star":
        source = "from builtins import *\n" + factory + "()\n"
    elif form == "reassigned":
        source = (
            "from builtins import "
            + factory
            + " as read\nread = str\nread('ordinary')\n"
        )
    elif form == "deleted":
        source = "from builtins import " + factory + " as read\ndel read\n"
    elif form == "default":
        source = (
            "from builtins import "
            + factory
            + " as read\ndef consume(captured=read):\n    return None\n"
        )
    else:
        source = (
            "class Holder:\n    from importlib import import_module as load\n    from builtins import "
            + factory
            + " as read\n    def method(self, captured=read()):\n        return captured\n"
        )
    sources = {}
    if form == "reexport":
        sources["factory.py"] = "from builtins import " + factory + " as read\n"
        source = (
            "from "
            + (".." if placement == "core" else ".")
            + "factory import read\nread()\n"
        )
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources[target] = source
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reachable = transitive_violations(graph, _PLANTED_CONTRACT)
    if form in {"reassigned", "deleted"}:
        assert own == []
        assert reachable == []
    else:
        assert any("unresolvable dynamic import" in row for row in reachable)
        if placement == "core":
            assert any("unresolvable dynamic import" in row for row in own)


@pytest.mark.parametrize("decorator", ("classmethod", "staticmethod"))
@pytest.mark.parametrize("capability", (False, True))
def test_method_descriptor_boundary_does_not_exempt_capability_namespaces(
    tmp_path: Path, decorator: str, capability: bool
) -> None:
    body = "    from importlib import import_module as load\n" if capability else ""
    source = (
        "class Holder:\n"
        + body
        + "    @"
        + decorator
        + "\n    def method(value):\n        return 'ordinary'\n"
    )
    graph = _plant(tmp_path, **{"core/engine.py": source})
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reachable = transitive_violations(graph, _PLANTED_CONTRACT)
    if capability:
        assert any("implicit class-namespace exposure" in row for row in own)
        assert any("implicit class-namespace exposure" in row for row in reachable)
    else:
        assert own == []
        assert reachable == []


@pytest.mark.parametrize("name", ("globals", "locals", "vars"))
@pytest.mark.parametrize("scope", ("function", "class-method", "module"))
def test_namespace_factory_same_spelled_ordinary_bindings_remain_shadows(
    tmp_path: Path, name: str, scope: str
) -> None:
    if scope == "function":
        source = "def ordinary(" + name + "):\n    return " + name + "('ordinary')\n"
    elif scope == "class-method":
        source = (
            "def "
            + name
            + "(value):\n    return value\nclass Ordinary:\n    from builtins import "
            + name
            + "\n    def method(self):\n        return "
            + name
            + "('ordinary')\n"
        )
    else:
        source = name + " = str\n" + name + "('ordinary')\n"
    graph = _plant(tmp_path, **{"core/engine.py": source})
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


@pytest.mark.parametrize(
    "prelude",
    (
        "positional",
        "keyword",
        "argument-annotation",
        "return-annotation",
        "vararg-annotation",
        "kwarg-annotation",
    ),
)
@pytest.mark.parametrize(
    "value", ("'ordinary'", "capture()", "probe.value", "probe['value']", "list[probe]")
)
@pytest.mark.parametrize("placement", ("core", "reachable"))
def test_capability_class_definition_preludes_must_be_literal_not_live_namespace_consumers(
    tmp_path: Path, prelude: str, value: str, placement: str
) -> None:
    # A real Python default/annotation executes while the class namespace is
    # live. These helpers use ordinary stdlib frame introspection instead of
    # spelling an importer alias at the caller; the static boundary must refuse
    # the executable prelude itself. No helper is invoked by this source test.
    helpers = (
        "import inspect\n"
        "def capture():\n"
        "    return inspect.currentframe().f_back.f_locals['load']('planted.ui.text')\n"
        "class Probe:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return inspect.currentframe().f_back.f_locals['load']('planted.ui.text')\n"
        "    def __getitem__(self, key):\n"
        "        return inspect.currentframe().f_back.f_locals['load']('planted.ui.text')\n"
        "probe = Probe()\n"
    )
    signature = {
        "positional": "self, captured=" + value,
        "keyword": "self, *, captured=" + value,
        "argument-annotation": "self, captured: " + value,
        "return-annotation": "self",
        "vararg-annotation": "self, *captured: " + value,
        "kwarg-annotation": "self, **captured: " + value,
    }[prelude]
    returns = " -> " + value if prelude == "return-annotation" else ""
    source = (
        helpers
        + "class Holder:\n    from importlib import import_module as load\n    def method("
        + signature
        + ")"
        + returns
        + ":\n        return 'ordinary'\n"
    )
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources = {target: source}
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reachable = transitive_violations(graph, _PLANTED_CONTRACT)
    if value == "'ordinary'":
        assert own == []
        assert reachable == []
    else:
        assert any("implicit class-namespace exposure" in row for row in reachable)
        if placement == "core":
            assert any("implicit class-namespace exposure" in row for row in own)


@pytest.mark.parametrize(
    "consumer",
    (
        "assignment",
        "positional-default",
        "keyword-default",
        "argument-annotation",
        "return-annotation",
    ),
)
@pytest.mark.parametrize(
    "value",
    (
        "None",
        "True",
        "1",
        "-1",
        "1+2j",
        "b'bytes'",
        "'text'",
        "[]",
        "()",
        "{}",
        "{1}",
        "[1, (2,)]",
        "{'x': 3}",
    ),
)
@pytest.mark.parametrize("placement", ("core", "reachable"))
def test_one_literal_syntax_certificate_covers_class_fields_and_definition_preludes(
    tmp_path: Path, consumer: str, value: str, placement: str
) -> None:
    field = {
        "assignment": "    value = " + value + "\n",
        "positional-default": "    def method(self, value="
        + value
        + "):\n        return value\n",
        "keyword-default": "    def method(self, *, value="
        + value
        + "):\n        return value\n",
        "argument-annotation": "    def method(self, value: "
        + value
        + "):\n        return value\n",
        "return-annotation": "    def method(self) -> "
        + value
        + ":\n        return None\n",
    }[consumer]
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources = {
        target: "class Holder:\n    from importlib import import_module as load\n"
        + field
    }
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    assert direct_violations(graph, _PLANTED_CONTRACT) == []
    assert transitive_violations(graph, _PLANTED_CONTRACT) == []


@pytest.mark.parametrize(
    "consumer",
    (
        "assignment",
        "positional-default",
        "keyword-default",
        "argument-annotation",
        "return-annotation",
    ),
)
@pytest.mark.parametrize("shadow", ("builtin", "global-callback", "class-callback"))
@pytest.mark.parametrize("placement", ("core", "reachable"))
def test_empty_set_call_is_executable_even_when_literal_eval_accepts_it(
    tmp_path: Path, consumer: str, shadow: str, placement: str
) -> None:
    # set() is a CALL, not an empty-set literal. In actual class construction a
    # same-spelled callback can read the live class namespace. Both consumers
    # must reject the syntax before literal_eval can bless its empty-set case.
    helpers = "import inspect\n"
    callback = "def set():\n    return inspect.currentframe().f_back.f_locals['load']('planted.ui.text')\n"
    if shadow == "global-callback":
        helpers += callback
    class_prefix = "class Holder:\n    from importlib import import_module as load\n"
    if shadow == "class-callback":
        class_prefix += indent(callback, "    ")
    field = {
        "assignment": "    value = set()\n",
        "positional-default": "    def method(self, value=set()):\n        return value\n",
        "keyword-default": "    def method(self, *, value=set()):\n        return value\n",
        "argument-annotation": "    def method(self, value: set()):\n        return value\n",
        "return-annotation": "    def method(self) -> set():\n        return None\n",
    }[consumer]
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources = {target: helpers + class_prefix + field}
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reachable = transitive_violations(graph, _PLANTED_CONTRACT)
    assert any("implicit class-namespace exposure" in row for row in reachable)
    if placement == "core":
        assert any("implicit class-namespace exposure" in row for row in own)


@pytest.mark.parametrize(
    "value", (None, "1", "set()", "capture()", "probe.value", "probe['value']")
)
@pytest.mark.parametrize(
    "annotation",
    (
        "'ordinary'",
        "capture()",
        "set()",
        "probe.value",
        "probe['value']",
        "list[probe]",
    ),
)
@pytest.mark.parametrize("placement", ("core", "reachable"))
def test_annotated_class_fields_use_the_same_literal_certificate_for_annotation_and_value(
    tmp_path: Path, value: str | None, annotation: str, placement: str
) -> None:
    # Annotation-only fields still evaluate their annotation in ordinary
    # Python class construction. No value does not authorize a callback.
    source = (
        "import inspect\n"
        "def capture():\n    return inspect.currentframe().f_back.f_locals['load']('planted.ui.text')\n"
        "class Holder:\n    from importlib import import_module as load\n"
        "    field: " + annotation + (" = " + value if value is not None else "") + "\n"
    )
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources = {target: source}
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reached = transitive_violations(graph, _PLANTED_CONTRACT)
    if annotation == "'ordinary'" and value in {None, "1"}:
        assert own == []
        assert reached == []
    else:
        assert any("implicit class-namespace exposure" in row for row in reached)
        if placement == "core":
            assert any("implicit class-namespace exposure" in row for row in own)


@pytest.mark.parametrize(
    "entry", ("import", "import-alias", "from", "relative", "star")
)
@pytest.mark.parametrize("depth", (1, 2, 3))
@pytest.mark.parametrize(
    "transfer", ("assignment", "argument", "walrus", "direct", "ordinary-shadow")
)
@pytest.mark.parametrize("placement", ("core", "reachable"))
def test_imported_child_capability_reaches_its_finite_ancestor_namespace(
    tmp_path: Path, entry: str, depth: int, transfer: str, placement: str
) -> None:
    parts = ["nest" + str(n) for n in range(depth)]
    package = ".".join(["planted", *parts])
    child = package + ".helper"
    sources = {
        "/".join([*parts, "helper.py"]): "from importlib import import_module as load\n"
    }
    # No __init__.py in the nested directories: normal namespace-package
    # ancestors must not lose a source-known imported child capability.
    if entry == "import":
        prelude, namespace, access = (
            "import " + child + "\n",
            "planted",
            ".".join([*parts, "helper", "load"]),
        )
    elif entry == "import-alias":
        prelude, namespace, access = (
            "import " + child + " as namespace\n",
            "namespace",
            "load",
        )
    elif entry == "from":
        prelude, namespace, access = (
            "from " + package + " import helper as namespace\n",
            "namespace",
            "load",
        )
    elif entry == "relative":
        prefix = ".." if placement == "core" else "."
        prelude, namespace, access = (
            "from " + prefix + ".".join(parts) + " import helper as namespace\n",
            "namespace",
            "load",
        )
    else:
        prelude, namespace, access = (
            "import " + child + "\nfrom " + package + " import *\n",
            "helper",
            "load",
        )
    if transfer == "assignment":
        action = "saved = " + namespace + "\nsaved." + access + "(name)\n"
    elif transfer == "argument":
        action = "consume(" + namespace + ")\n"
    elif transfer == "walrus":
        action = "(saved := " + namespace + ")." + access + "(name)\n"
    elif transfer == "ordinary-shadow":
        # A definite ordinary LOCAL binding is not an importer; this is not a
        # waiver for mutating a capability-bearing package attribute namespace.
        action = namespace + " = str\n" + namespace + "('ordinary')\n"
    else:
        action = namespace + "." + access + "(name)\n"
    target = "core/engine.py" if placement == "core" else "helper.py"
    sources[target] = (
        "name = 'planted.ui.text'\ndef consume(value):\n    return value\n"
        + prelude
        + action
    )
    if placement == "reachable":
        sources["core/engine.py"] = "from .. import helper\n"
    graph = _plant(tmp_path, **sources)
    own = direct_violations(graph, _PLANTED_CONTRACT)
    reached = transitive_violations(graph, _PLANTED_CONTRACT)
    # Star import itself transfers an importer-bearing namespace and refuses
    # before a later local overwrite can make that earlier transfer disappear.
    if transfer == "ordinary-shadow" and entry != "star":
        assert own == []
        assert reached == []
    else:
        assert any("unresolvable dynamic import" in row for row in reached)
        if placement == "core":
            assert any("unresolvable dynamic import" in row for row in own)


# Source-bound target DATA exercises the same mandatory direct/transitive APIs.


def _resolved_policy(graph, contract=_PLANTED_CONTRACT):
    proposal = _resolved_import_candidates(graph)
    assert proposal["safety_verdict"] is False
    assert proposal["runtime_or_policy_changed"] is False
    return {**contract, "resolved_dynamic_imports": proposal["proposed_entries"]}


@pytest.mark.parametrize("lookup", ("_TARGETS[key]", "_TARGETS.get(key)"))
@pytest.mark.parametrize("entrypoint", (direct_violations, transitive_violations))
def test_source_bound_finite_targets_keep_strict_entrypoints_and_graph_read_only(
    tmp_path, lookup, entrypoint
):
    guard = (
        "    if target is None:\n        raise LookupError('unknown target')\n"
        if ".get(" in lookup
        else ""
    )
    source = (
        "from importlib import import_module as load\n"
        "_TARGETS = {'left': 'planted.clean', 'right': 'planted.other'}\n"
        "def run(key):\n    target = "
        + lookup
        + "\n"
        + guard
        + '    if type(target) is not str or target not in ("planted.clean", "planted.other"):\n        raise ValueError("unknown target")\n    return load(target)\n'
    )
    graph = _plant(
        tmp_path,
        **{"core/engine.py": source, "helper.py": "", "clean.py": "", "other.py": ""},
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert policy["resolved_dynamic_imports"][0]["targets"] == [
        "planted.clean",
        "planted.other",
    ]
    before = json.dumps(
        {
            "edges": graph,
            "problems": graph.dynamic_problems,
            "facts": graph.admission_facts,
        },
        sort_keys=True,
    )
    assert entrypoint(graph, _PLANTED_CONTRACT)
    assert entrypoint(graph, policy) == []
    assert entrypoint(graph, policy) == []
    assert (
        json.dumps(
            {
                "edges": graph,
                "problems": graph.dynamic_problems,
                "facts": graph.admission_facts,
            },
            sort_keys=True,
        )
        == before
    )


@pytest.mark.parametrize(
    "field",
    (
        "path",
        "line",
        "source_sha256",
        "closure_sha256",
        "span_ast_sha256",
        "binding_sha256",
        "proof_kind",
        "targets",
    ),
)
@pytest.mark.parametrize("entrypoint", (direct_violations, transitive_violations))
def test_admission_binding_drift_never_certifies_safety(tmp_path, field, entrypoint):
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\ndef run(key):\n    target = _T[key]\n    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n    return load(target)\n",
            "helper.py": "",
            "clean.py": "",
        },
    )
    policy = _resolved_policy(graph)
    assert entrypoint(graph, policy) == []
    entry = policy["resolved_dynamic_imports"][0]
    entry[field] = (
        entry[field] + 1
        if field == "line"
        else ["planted.other"]
        if field == "targets"
        else "changed"
    )
    assert entrypoint(graph, policy)


@pytest.mark.parametrize("entrypoint", (direct_violations, transitive_violations))
def test_admitted_finite_targets_are_still_subject_to_forbidden_layer_policy(
    tmp_path, entrypoint
):
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from importlib import import_module as load\n_T = {'allowed': 'planted.clean', 'forbidden': 'planted.ui.window'}\ndef run(key):\n    target = _T[key]\n    if type(target) is not str or target not in ('planted.clean', 'planted.ui.window'):\n        raise ValueError('unknown target')\n    return load(target)\n",
            "helper.py": "",
            "clean.py": "",
        },
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert "planted.ui.window" in policy["resolved_dynamic_imports"][0]["targets"]
    assert any("planted.ui.window" in problem for problem in entrypoint(graph, policy))


@pytest.mark.parametrize(
    "source",
    (
        "from importlib import import_module as load\ndef run(name):\n    return load(name)\n",
        "from importlib import import_module as load\n_T = {'ok': 'planted.clean', 'bad': unknown}\ndef run(key):\n    target = _T[key]\n    return load(target)\n",
        "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\nother = _T\ndef run(key):\n    target = _T[key]\n    return load(target)\n",
        "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\ndef run(key):\n    target = _T.get(key)\n    return load(target)\n",
        "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\ndef run(key):\n    target = _T[key]\n    target = key\n    return load(target)\n",
        "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\ndef run(key):\n    target = _T[key]\n    return load(target, **kwargs)\n",
    ),
)
def test_unbounded_partial_escaped_reassigned_or_unpacking_targets_are_not_admissible(
    tmp_path, source
):
    graph = _plant(
        tmp_path, **{"core/engine.py": source, "helper.py": "", "clean.py": ""}
    )
    policy = _resolved_policy(graph)
    assert policy["resolved_dynamic_imports"] == []
    assert direct_violations(graph, policy)
    assert transitive_violations(graph, policy)


@pytest.mark.parametrize(
    "bad_targets", ([], ["planted.clean", "planted.clean"], [None], "planted.clean")
)
def test_empty_duplicate_or_untyped_targets_cannot_mean_skip(tmp_path, bad_targets):
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from importlib import import_module as load\n_T = {'ok': 'planted.clean'}\ndef run(key):\n    target = _T[key]\n    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n    return load(target)\n",
            "helper.py": "",
            "clean.py": "",
        },
    )
    policy = _resolved_policy(graph)
    policy["resolved_dynamic_imports"][0]["targets"] = bad_targets
    assert direct_violations(graph, policy)
    assert transitive_violations(graph, policy)


def test_literal_member_projection_requires_noncapability_result_and_exact_source(
    tmp_path,
):
    source = "from importlib import import_module as load\ndef run():\n    return load('planted.library').Value\n"
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert (
        policy["resolved_dynamic_imports"][0]["proof_kind"]
        == "literal-member-projection"
    )
    assert direct_violations(graph, policy) == []
    assert transitive_violations(graph, policy) == []
    changed = _plant(
        tmp_path,
        **{
            "core/engine.py": source.replace(".Value", ".importlib"),
            "helper.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    assert _resolved_policy(changed)["resolved_dynamic_imports"] == []
    assert direct_violations(changed, policy)
    assert transitive_violations(changed, policy)


@given(
    targets=st.lists(
        st.sampled_from(("planted.clean", "planted.other", "planted.ui.window")),
        min_size=1,
        max_size=8,
    )
)
def test_admitted_target_sequences_recheck_source_closure_and_every_layer(
    targets, tmp_path_factory
):
    root = tmp_path_factory.mktemp("admitted-target-sequence")
    old_policy = None
    old_source = None
    for target in targets:
        source = (
            "from importlib import import_module as load\n_T = {'selected': "
            + repr(target)
            + "}\ndef run(key):\n    bound = _T[key]\n    if type(bound) is not str or bound not in ("
            + repr(target)
            + ",):\n        raise ValueError('unknown target')\n    return load(bound)\n"
        )
        graph = _plant(
            root,
            **{
                "core/engine.py": source,
                "helper.py": "",
                "clean.py": "",
                "other.py": "",
            },
        )
        policy = _resolved_policy(graph)
        assert len(policy["resolved_dynamic_imports"]) == 1
        assert policy["resolved_dynamic_imports"][0]["targets"] == [target]
        for entrypoint in (direct_violations, transitive_violations):
            assert bool(entrypoint(graph, policy)) is (target == "planted.ui.window")
            if old_policy is not None and source != old_source:
                assert entrypoint(graph, old_policy)
        old_policy, old_source = policy, source


@pytest.mark.parametrize(
    "tail",
    (
        "return (load('planted.library').Value, __import__)\n",
        "value = load('planted.library').Value; escaped = __import__\n",
        "return load('planted.library').missing\n",
    ),
)
def test_projection_admission_never_hides_sibling_or_undeclared_escape(tmp_path, tail):
    source = "from importlib import import_module as load\ndef run():\n    " + tail
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    assert direct_violations(graph, _PLANTED_CONTRACT)
    assert transitive_violations(graph, _PLANTED_CONTRACT)


@pytest.mark.parametrize(
    "target", ("importlib", "importlib.metadata", "builtins", "sys")
)
def test_runtime_target_data_cannot_transfer_known_importer_namespace(tmp_path, target):
    source = (
        "from importlib import import_module as load\ndef run(name):\n    if type(name) is not str or name not in ("
        + repr(target)
        + ",):\n        raise ValueError('unknown target')\n    return load(name)\n"
    )
    graph = _plant(tmp_path, **{"core/engine.py": source, "helper.py": ""})
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    assert direct_violations(graph, _PLANTED_CONTRACT)
    assert transitive_violations(graph, _PLANTED_CONTRACT)


def test_extracted_target_data_is_detached_and_cannot_shrink_source_evidence(tmp_path):
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": "from importlib import import_module as load\ndef run(target):\n    if type(target) is not str or target not in ('planted.clean', 'planted.ui.window'):\n        raise ValueError('unknown target')\n    return load(target)\n",
            "helper.py": "",
            "clean.py": "",
        },
    )
    policy = _resolved_policy(graph)
    assert policy["resolved_dynamic_imports"][0]["targets"] == [
        "planted.clean",
        "planted.ui.window",
    ]
    policy["resolved_dynamic_imports"][0]["targets"].pop()
    assert _resolved_policy(graph)["resolved_dynamic_imports"][0]["targets"] == [
        "planted.clean",
        "planted.ui.window",
    ]
    assert direct_violations(graph, policy)
    assert transitive_violations(graph, policy)


@pytest.mark.parametrize("projection", (False, True))
@pytest.mark.parametrize("module_alias", (False, True))
def test_joined_callee_bindings_never_borrow_a_finite_target_certificate(
    tmp_path, projection, module_alias
):
    if module_alias:
        branches = "    if choose:\n        import importlib as machinery\n    else:\n        machinery = ordinary\n"
        callee = "machinery.import_module"
    else:
        branches = "    if choose:\n        from importlib import import_module as load\n    else:\n        load = ordinary\n"
        callee = "load"
    expression = (
        callee + "('planted.library').Value" if projection else callee + "(target)"
    )
    source = (
        "ordinary = None\ndef run(target, choose):\n"
        + branches
        + "    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
        + "    return "
        + expression
        + "\n"
    )
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "clean.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    assert direct_violations(graph, _PLANTED_CONTRACT)
    assert transitive_violations(graph, _PLANTED_CONTRACT)


@pytest.mark.parametrize("projection", (False, True))
@pytest.mark.parametrize("module_alias", (False, True))
def test_reexport_overwrite_is_not_an_exact_importer_certificate(
    tmp_path, projection, module_alias
):
    prefix = (
        "import planted.helper as machinery\n"
        if module_alias
        else "from planted.helper import load\n"
    )
    callee = "machinery.load" if module_alias else "load"
    expression = (
        callee + "('planted.library').Value" if projection else callee + "(target)"
    )
    source = (
        prefix
        + "def run(target):\n    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n    return "
        + expression
        + "\n"
    )
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "from importlib import import_module as load\nload = None\n",
            "clean.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    assert direct_violations(graph, _PLANTED_CONTRACT)
    assert transitive_violations(graph, _PLANTED_CONTRACT)


@pytest.mark.parametrize("projection", (False, True))
@pytest.mark.parametrize("module_alias", (False, True))
def test_exact_importer_bindings_share_one_positive_model(
    tmp_path, projection, module_alias
):
    prefix = (
        "import importlib as machinery\n"
        if module_alias
        else "from importlib import import_module as load\n"
    )
    callee = "machinery.import_module" if module_alias else "load"
    expression = (
        callee + "('planted.library').Value" if projection else callee + "(target)"
    )
    source = (
        prefix
        + "def run(target):\n    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n    return "
        + expression
        + "\n"
    )
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "clean.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert direct_violations(graph, policy) == []
    assert transitive_violations(graph, policy) == []


@pytest.mark.parametrize("module_alias", (False, True))
@pytest.mark.parametrize(
    "tail",
    (
        "    if True:\n        target = 'planted.ui.window'\n        return {callee}(target)\n",
        "    if (target := 'planted.ui.window'):\n        return {callee}(target)\n",
        "    for target in ('planted.ui.window',):\n        return {callee}(target)\n",
        "    try:\n        target = 'planted.ui.window'\n        result = {callee}(target)\n    finally:\n        pass\n",
        "    while True:\n        target = 'planted.ui.window'\n        result = {callee}(target)\n        break\n",
        "    if True:\n        return {callee}(target)\n",
    ),
)
def test_guard_certificate_requires_a_direct_adjacent_call_statement(
    tmp_path, module_alias, tail
):
    prefix = (
        "import importlib as machinery\n"
        if module_alias
        else "from importlib import import_module as load\n"
    )
    callee = "machinery.import_module" if module_alias else "load"
    source = (
        prefix
        + "def run(target):\n"
        + "    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
        + tail.format(callee=callee)
    )
    graph = _plant(
        tmp_path, **{"core/engine.py": source, "helper.py": "", "clean.py": ""}
    )
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    assert direct_violations(graph, _PLANTED_CONTRACT)
    assert transitive_violations(graph, _PLANTED_CONTRACT)


@pytest.mark.parametrize("module_alias", (False, True))
def test_guard_certificate_accepts_direct_adjacent_assignment(tmp_path, module_alias):
    prefix = (
        "import importlib as machinery\n"
        if module_alias
        else "from importlib import import_module as load\n"
    )
    callee = "machinery.import_module" if module_alias else "load"
    source = (
        prefix
        + "def run(target):\n"
        + "    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
        + "    result = "
        + callee
        + "(target)\n    return result\n"
    )
    graph = _plant(
        tmp_path, **{"core/engine.py": source, "helper.py": "", "clean.py": ""}
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert direct_violations(graph, policy) == []
    assert transitive_violations(graph, policy) == []


@pytest.mark.parametrize("projection", (False, True))
@pytest.mark.parametrize("source_kind", ("module", "function", "builtins"))
@pytest.mark.parametrize(
    "mutation", ("assign", "annotate", "augment", "delete", "dictionary")
)
def test_known_machinery_mutation_invalidates_both_target_proof_kinds(
    tmp_path, projection, source_kind, mutation
):
    prefix = "from importlib import import_module as load\n"
    if source_kind == "module":
        prefix = "import importlib as machinery\n"
        target, callee = "machinery.import_module", "machinery.import_module"
    elif source_kind == "function":
        target, callee = "load.__code__", "load"
    else:
        prefix += "import builtins as native\n"
        target, callee = "native.type", "load"
    writes = {
        "assign": target + " = replacement",
        "annotate": target + ": object = replacement",
        "augment": target + " += replacement",
        "delete": "del " + target,
        "dictionary": target.rpartition(".")[0] + ".__dict__['changed'] = replacement",
    }
    call = callee + "('planted.library').Value" if projection else callee + "(name)"
    source = (
        prefix
        + "def run(name, replacement):\n    "
        + writes[mutation]
        + "\n"
        + "    if type(name) is not str or name not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
        + "    return "
        + call
        + "\n"
    )
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "clean.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    assert _resolved_policy(graph)["resolved_dynamic_imports"] == []
    for entrypoint in (direct_violations, transitive_violations):
        assert any(
            "importer capability mutation" in problem
            for problem in entrypoint(graph, _PLANTED_CONTRACT)
        )


@pytest.mark.parametrize("projection", (False, True))
def test_ordinary_member_store_does_not_invalidate_exact_importer_binding(
    tmp_path, projection
):
    expression = "load('planted.library').Value" if projection else "load(name)"
    source = (
        "from importlib import import_module as load\n"
        + "def run(name, ordinary):\n    ordinary.count = 1\n"
        + "    if type(name) is not str or name not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
        + "    return "
        + expression
        + "\n"
    )
    graph = _plant(
        tmp_path,
        **{
            "core/engine.py": source,
            "helper.py": "",
            "clean.py": "",
            "library.py": "import importlib\nValue = 1\n",
        },
    )
    policy = _resolved_policy(graph)
    assert len(policy["resolved_dynamic_imports"]) == 1
    assert direct_violations(graph, policy) == []
    assert transitive_violations(graph, policy) == []


def _uncached_finite_guarded_call(node, tree, parents, owner, machinery_mutated):
    """An exact-string literal-membership guard dominates this loading call.

    A lookup table or configuration string by itself is not a runtime target
    bound. The immediately preceding guard must reject both non-native strings
    and names outside closed literal DATA before the importer is evaluated.
    """
    if not isinstance(node, ast.Call):
        return None
    if len(node.args) != 1 or node.keywords or not isinstance(node.args[0], ast.Name):
        return None
    argument = node.args[0]
    enclosing = parents.get(id(node))
    while enclosing is not None and not isinstance(
        enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        enclosing = parents.get(id(enclosing))
    if enclosing is None:
        return None
    if any(
        isinstance(n, (ast.Global, ast.Nonlocal)) and argument.id in n.names
        for n in ast.walk(enclosing)
    ):
        return None
    # The guarded argument must reach the importer without executing a
    # compound statement, condition, loop, handler or other intervening code.
    # An ancestor guard does not bind a value reassigned inside that ancestor.
    containing = parents.get(id(node))
    if not (
        isinstance(containing, ast.Return)
        and containing.value is node
        or isinstance(containing, ast.Assign)
        and containing.value is node
        and len(containing.targets) == 1
        and isinstance(containing.targets[0], ast.Name)
    ):
        return None
    if parents.get(id(containing)) is not enclosing:
        return None
    index = enclosing.body.index(containing)
    if index == 0:
        return None
    guard = enclosing.body[index - 1]
    if not (
        isinstance(guard, ast.If)
        and not guard.orelse
        and len(guard.body) == 1
        and isinstance(guard.body[0], ast.Raise)
        and isinstance(guard.test, ast.BoolOp)
        and isinstance(guard.test.op, ast.Or)
        and len(guard.test.values) == 2
    ):
        return None
    typed, bounded = guard.test.values
    if not (
        isinstance(typed, ast.Compare)
        and len(typed.ops) == 1
        and isinstance(typed.ops[0], ast.IsNot)
        and isinstance(typed.left, ast.Call)
        and isinstance(typed.left.func, ast.Name)
        and typed.left.func.id == "type"
        and not typed.left.keywords
        and len(typed.left.args) == 1
        and isinstance(typed.left.args[0], ast.Name)
        and typed.left.args[0].id == argument.id
        and len(typed.comparators) == 1
        and isinstance(typed.comparators[0], ast.Name)
        and typed.comparators[0].id == "str"
    ):
        return None
    if owner._resolve(typed.left.func, "type") or owner._resolve(
        typed.comparators[0], "str"
    ):
        return None
    if not (
        isinstance(bounded, ast.Compare)
        and len(bounded.ops) == 1
        and isinstance(bounded.ops[0], ast.NotIn)
        and isinstance(bounded.left, ast.Name)
        and bounded.left.id == argument.id
        and len(bounded.comparators) == 1
        and isinstance(bounded.comparators[0], ast.Tuple)
        and bounded.comparators[0].elts
        and all(_str(n) for n in bounded.comparators[0].elts)
    ):
        return None
    if owner.called(node) != "import_module" or not _exact_import_module_callee(
        node, owner, machinery_mutated
    ):
        return None
    targets = sorted({_str(n) for n in bounded.comparators[0].elts})
    if not all(
        re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name) for name in targets
    ):
        return None
    if any(
        target in owner.capability_namespaces
        or any(
            target == root or target.startswith(root + ".")
            for root in (*_IMPORTER_MODULES, "sys")
        )
        for target in targets
    ):
        return None
    return {
        "proof_kind": "closed-runtime-target-guard",
        "targets": targets,
        "binding_sha256": _source_digest(ast.dump(guard, include_attributes=False)),
    }


def _uncached_admission_facts(
    tree, module, is_package, owner, source_path, source_sha256, closure_sha256, trees
):
    parents = {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    candidates = {}
    # One read-only module summary, not a whole-tree scan for each candidate.
    machinery_mutated = any(
        _known_capability_mutation(node, owner) for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        value = _uncached_finite_guarded_call(
            node, tree, parents, owner, machinery_mutated
        ) or _literal_member_call(
            node, parents, owner, module, is_package, trees, machinery_mutated
        )
        if value is None:
            continue
        # A line-level diagnostic must not hide another escape sharing a
        # physical line or a sibling expression in this same statement.
        ancestors = set()
        cursor = node
        statement = None
        while cursor is not None:
            ancestors.add(id(cursor))
            if statement is None and isinstance(cursor, ast.stmt):
                statement = cursor
            cursor = parents.get(id(cursor))
        terminal = (
            parents.get(id(node))
            if value["proof_kind"] == "literal-member-projection"
            else node
        )
        if not (
            isinstance(statement, ast.Return)
            and statement.value is terminal
            or isinstance(statement, ast.Assign)
            and statement.value is terminal
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        if any(
            isinstance(other, ast.stmt)
            and id(other) not in ancestors
            and other.lineno <= node.lineno <= other.end_lineno
            for other in ast.walk(tree)
        ):
            continue
        fact = {
            "path": source_path,
            "line": node.lineno,
            "source_sha256": source_sha256,
            "closure_sha256": closure_sha256,
            "span_ast_sha256": _source_digest(ast.dump(node, include_attributes=False)),
            **value,
        }
        candidates.setdefault(node.lineno, []).append(fact)
    # A shared physical line with multiple loading operations is ambiguous.
    return {line: values[0] for line, values in candidates.items() if len(values) == 1}


def _admission_cost_fixture(source):
    module = "planted.core.engine"
    texts = {
        module: source,
        "planted.clean": "",
        "planted.library": "import importlib\nValue = 1\n",
    }
    trees = {name: ast.parse(text) for name, text in texts.items()}
    mods = {name: Path(name.replace(".", "/") + ".py") for name in texts}
    exports = _importer_exports(trees, mods)
    owner = _strict_importers(trees[module], module, False, exports)
    return (
        trees[module],
        module,
        False,
        owner,
        "planted/core/engine.py",
        "0" * 64,
        "1" * 64,
        trees,
    )


def _admission_cost_source(shape, noise_count):
    prefix = "from importlib import import_module as load\n"
    noise = "    ordinary(target)\n" * noise_count
    guard = "    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError('unknown target')\n"
    prelude, tail = "", "    return load(target)\n"
    if shape == "dotted":
        prefix = "import importlib as machinery\n"
        tail = "    return machinery.import_module(target)\n"
    elif shape == "nested-global":
        prelude = "    def child():\n        global target\n"
    elif shape == "nested-nonlocal":
        prelude = "    def child():\n        nonlocal target\n"
    elif shape == "ancestor-mutation":
        tail = "    if True:\n        target = 'planted.ui.window'\n        return load(target)\n"
    elif shape == "same-line-sibling":
        tail = "    value = load(target); escaped = __import__\n"
    elif shape == "literal-member":
        tail = "    return load('planted.library').Value\n"
    elif shape == "literal-sibling":
        tail = "    value = load('planted.library').Value; escaped = __import__\n"
    elif shape == "mutated-machinery":
        prefix += "import builtins as native\n"
        prelude = "    native.type = ordinary\n"
    elif shape == "joined-callee":
        prelude = "    if choose:\n        from importlib import import_module as load\n    else:\n        load = ordinary\n"
    elif shape == "multiple-guarded":
        tail = "    first = load(target)\n" + guard + "    return load(target)\n"
    elif shape == "multiple-literal":
        tail = "    first = load('planted.library').Value\n    return load('planted.library').Value\n"
    elif shape == "duplicate-line-literal":
        tail = "    first = load('planted.library').Value; second = load('planted.library').Value\n"
    elif shape == "nested-class-global":
        prelude = (
            "    class Inner:\n        def method(self):\n            global target\n"
        )
    elif shape == "nested-class-nonlocal":
        prelude = (
            "    class Inner:\n        def method(self):\n            nonlocal target\n"
        )
    elif shape == "async-nested-global":
        prelude = "    async def child():\n        global target\n"
    elif shape not in ("direct", "async-direct"):
        raise AssertionError("unknown finite source shape")
    definition = "async def run" if shape == "async-direct" else "def run"
    return (
        prefix
        + definition
        + "(target, ordinary, choose):\n"
        + prelude
        + noise
        + guard
        + tail
    )


@given(
    shape=st.sampled_from(
        (
            "direct",
            "dotted",
            "nested-global",
            "nested-nonlocal",
            "ancestor-mutation",
            "same-line-sibling",
            "literal-member",
            "literal-sibling",
            "mutated-machinery",
            "joined-callee",
            "multiple-guarded",
            "multiple-literal",
            "duplicate-line-literal",
            "async-direct",
            "nested-class-global",
            "nested-class-nonlocal",
            "async-nested-global",
        )
    ),
    noise_count=st.integers(min_value=0, max_value=12),
)
def test_admission_cost_summaries_match_uncached_facts_and_temporal_journal(
    shape, noise_count
):
    args = _admission_cost_fixture(_admission_cost_source(shape, noise_count))
    owner = args[3]
    snapshot = owner._snapshot()
    position = owner._position
    journals = [
        (
            dict((key, list(value)) for key, value in scope.bindings.items()),
            dict((key, list(value)) for key, value in scope.writes.items()),
        )
        for scope in owner._scopes
    ]
    expected = _uncached_admission_facts(*args)
    assert _admission_facts(*args) == expected
    assert list(_admission_facts(*args).items()) == list(expected.items())
    assert _uncached_admission_facts(*args) == expected
    assert owner._snapshot() == snapshot
    assert owner._position == position
    assert [
        (
            dict((key, list(value)) for key, value in scope.bindings.items()),
            dict((key, list(value)) for key, value in scope.writes.items()),
        )
        for scope in owner._scopes
    ] == journals


def test_irrelevant_calls_cannot_trigger_repeated_function_or_module_walks(monkeypatch):
    args = _admission_cost_fixture(_admission_cost_source("direct", 200))
    tree = args[0]
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    original = ast.walk
    counts = {}

    def counted(node):
        counts[id(node)] = counts.get(id(node), 0) + 1
        yield from original(node)

    monkeypatch.setattr(ast, "walk", counted)
    actual = _admission_facts(*args)
    optimized = dict(counts)
    counts.clear()
    assert _uncached_admission_facts(*args) == actual
    assert actual
    assert optimized.get(id(tree), 0) == 1
    assert optimized.get(id(function), 0) == 0
    assert counts.get(id(tree), 0) >= 4
    assert counts.get(id(function), 0) >= 202


@pytest.mark.parametrize(
    "source",
    (
        "x = first(); y = second()\n",
        "def outer():\n    if test:\n        value = (\n            call()\n        )\n    else:\n        another()\n",
        "class Outer:\n    def method(self):\n        try:\n            return call()\n        finally:\n            done()\n",
    ),
)
def test_statement_interval_summary_equals_uncached_closed_spans(source):
    nodes = tuple(ast.walk(ast.parse(source)))
    lines = range(1, len(source.splitlines()) + 2)
    expected = {
        line: frozenset(
            id(node)
            for node in nodes
            if isinstance(node, ast.stmt) and node.lineno <= line <= node.end_lineno
        )
        for line in lines
    }
    actual = _admission_statement_owners(nodes, lines)
    assert actual == expected
    assert all(type(value) is frozenset for value in actual.values())


@pytest.mark.parametrize(
    "source",
    (
        "def run(target, ordinary):\n    ordinary(target)\n",
        "from importlib import import_module as load\ndef run():\n    return load('planted.clean')\n",
        "from importlib import import_module as load\nimport builtins as native\ndef run(target, ordinary):\n    native.type = ordinary\n    if type(target) is not str or target not in ('planted.clean',):\n        raise ValueError()\n    return load(target)\n",
    ),
)
def test_no_admission_candidates_build_no_statement_span_summary(monkeypatch, source):
    args = _admission_cost_fixture(source)
    calls = []

    def unexpected(*values):
        calls.append(values)
        raise AssertionError("no candidate can require a span summary")

    monkeypatch.setitem(globals(), "_admission_statement_owners", unexpected)
    assert _admission_facts(*args) == {}
    assert _uncached_admission_facts(*args) == {}
    assert calls == []


@given(
    commands=st.lists(
        st.tuples(
            st.sampled_from(
                (
                    "bind",
                    "save_branch",
                    "save_dict",
                    "save_sets",
                    "mutate",
                    "restore",
                    "join",
                    "empty",
                    "new_scope",
                )
            ),
            st.integers(min_value=0, max_value=6),
            st.integers(min_value=0, max_value=3),
            st.integers(min_value=0, max_value=3),
            st.sampled_from(("alias", "global_name", "nonlocal_name", "__import__")),
            st.sampled_from(
                (
                    "other",
                    "unbound",
                    "not-written",
                    "import_module",
                    "__import__",
                    "importlib",
                )
            ),
            st.booleans(),
        ),
        max_size=45,
    )
)
@example(
    commands=[
        ("save_branch", 0, 0, 0, "alias", "other", False),
        ("new_scope", 0, 0, 0, "alias", "other", False),
        ("bind", 4, 0, 0, "alias", "import_module", False),
        ("restore", 4, 0, 0, "alias", "other", False),
    ]
)
@example(
    commands=[
        ("bind", 1, 0, 0, "global_name", "import_module", False),
        ("save_branch", 0, 0, 0, "alias", "other", False),
        ("empty", 1, 0, 0, "global_name", "other", True),
        ("join", 1, 0, 0, "alias", "other", True),
    ]
)
def test_scoped_branch_views_match_complete_flat_journals(commands):
    owner = Importers(ast.parse(""))
    root = owner._module
    function = owner._new_scope("function", root, ast.parse(""))
    nested = owner._new_scope("function", function, ast.parse(""))
    owner._new_scope("class", root, ast.parse(""))
    function.globals.add("global_name")
    nested.nonlocals.add("nonlocal_name")
    oracle = _JournalOracle(owner)
    saved = [{} for _ in range(4)]
    expected = [{} for _ in range(4)]
    for operation, scope_index, first, second, name, value, filtered in commands:
        scope = owner._scopes[scope_index % len(owner._scopes)]
        selected = {root, scope} if filtered else None
        if operation == "new_scope":
            if len(owner._scopes) < 7:
                scope = owner._new_scope("comprehension", function, ast.parse(""))
                oracle.histories[scope] = {"bindings": {}, "writes": {}}
        elif operation == "bind":
            owner._bind(scope, name, value)
            oracle.bind(scope, name, value)
        elif operation.startswith("save_"):
            if operation == "save_branch":
                saved[first] = owner._branch_checkpoint()
            elif operation == "save_dict":
                saved[first] = owner._checkpoint()
            else:
                saved[first] = owner._snapshot()
            expected[first] = oracle.snapshot()
        elif operation == "mutate":
            state = saved[first]
            if isinstance(state, _BranchState):
                # Private immutable branch values have no caller mutation API.
                # The ordinary public checkpoint still has every dict operation.
                state = dict(state.flat())
                saved[first] = state
            state[(scope, "bindings", name)] = {value}
            expected[first][(scope, "bindings", name)] = {value}
            copied = state.copy()
            assert copied == state
            copied.clear()
            assert copied == {}
        elif operation == "restore":
            owner._restore(saved[first], selected)
            oracle.restore(expected[first], selected)
        elif operation == "join":
            owner._join(saved[first], saved[second], scopes=selected)
            oracle.join(expected[first], expected[second], scopes=selected)
        else:
            state = {(scope, "bindings", name): set()}
            owner._restore(state, selected)
            oracle.restore(state, selected)
        assert owner._position == oracle.position
        assert list(owner._snapshot().items()) == list(oracle.snapshot().items())
        assert _journal_histories(owner) == {
            (item, channel, key): list(events)
            for item in oracle.scopes
            for channel in ("bindings", "writes")
            for key, events in oracle.histories[item][channel].items()
        }
        for actual, wanted in zip(saved, expected):
            assert (
                actual.flat() if isinstance(actual, _BranchState) else actual
            ) == wanted
        for history in _journal_histories(owner).values():
            for before in (
                None,
                -1,
                owner._position // 2,
                owner._position,
                owner._position + 1,
            ):
                assert owner._latest(history, before) == oracle.latest(history, before)
        assert len(owner._branch_head_cache) <= owner._BRANCH_CACHE_MAX_SCOPES
        assert owner._branch_head_cache_keys <= owner._BRANCH_CACHE_MAX_KEYS


@pytest.mark.parametrize("filtered", (False, True))
@pytest.mark.parametrize("operation", ("restore", "join"))
def test_scoped_branch_noop_avoids_global_reconstruction(
    monkeypatch, filtered, operation
):
    owner = Importers(ast.parse(""))
    for index in range(150):
        scope = owner._new_scope("function", owner._module, ast.parse(""))
        owner._bind(scope, "alias" + str(index), "other")
    state = owner._branch_checkpoint()
    position, history = owner._position, _journal_histories(owner)
    selected = {owner._module, scope} if filtered else None

    def unexpected(*args, **kwargs):
        raise AssertionError("A proved no-op must not enter flat global state work")

    monkeypatch.setattr(owner, "_flat_restore", unexpected)
    monkeypatch.setattr(owner, "_flat_join", unexpected)
    monkeypatch.setattr(owner, "_state_values", unexpected)
    if operation == "restore":
        owner._restore(state, selected)
    else:
        owner._join(state, state, scopes=selected)
    assert owner._position == position
    assert _journal_histories(owner) == history


@pytest.mark.parametrize("max_scopes,max_keys", ((0, 0), (1, 1), (2, 4), (512, 16384)))
def test_scoped_branch_budget_eviction_and_empty_heads_preserve_exact_events(
    monkeypatch, max_scopes, max_keys
):
    owner = Importers(ast.parse(""))
    monkeypatch.setattr(owner, "_BRANCH_CACHE_MAX_SCOPES", max_scopes)
    monkeypatch.setattr(owner, "_BRANCH_CACHE_MAX_KEYS", max_keys)
    root = owner._module
    other = owner._new_scope("function", root, ast.parse(""))
    oracle = _JournalOracle(owner)
    for scope in (root, other):
        owner._bind(scope, "alias", "import_module")
        oracle.bind(scope, "alias", "import_module")
    state, expected = owner._branch_checkpoint(), oracle.snapshot()
    for scope in (root, other):
        empty = {
            (scope, "bindings", "alias"): set(),
            (scope, "writes", "new_empty"): set(),
        }
        owner._restore(empty, {scope})
        oracle.restore(empty, {scope})
        owner._branch_checkpoint()
        assert owner._position == oracle.position
    owner._restore(state)
    oracle.restore(expected)
    owner._join(state, owner._branch_checkpoint())
    oracle.join(expected, oracle.snapshot())
    assert owner._position == oracle.position
    assert owner._snapshot() == oracle.snapshot()
    assert _journal_histories(owner) == {
        (item, channel, key): list(events)
        for item in oracle.scopes
        for channel in ("bindings", "writes")
        for key, events in oracle.histories[item][channel].items()
    }
    assert len(owner._branch_head_cache) <= max_scopes
    assert owner._branch_head_cache_keys <= max_keys


_SCOPED_BRANCH_SOURCES = (
    "import importlib as m\nif flag:\n m = other\nelse:\n pass\nm.import_module('planted.safe')\n",
    "from importlib import import_module as load\ndef f():\n global load\n if flag:\n  load = other\n return load(name)\nload('planted.safe')\n",
    "from importlib import import_module as load\ndef outer():\n x = load\n def inner():\n  nonlocal x\n  if flag:\n   del x\n  return x(name)\n return inner\n",
    "from importlib import import_module as load\ntry:\n load = other\nexcept Exception as load:\n load(name)\nfinally:\n load('planted.safe')\n",
    "from importlib import import_module as load\nfor x in items:\n if x:\n  load = other\nelse:\n load('planted.safe')\n",
    "from importlib import import_module as load\nclass C:\n x = load\n def method(self, arg=load):\n  return load(arg)\n",
    "from importlib import import_module as load\nitems = [(load := other) for x in [1]]\nload(name)\n",
    "from importlib import import_module as load\nitems = ((load := other) for x in source)\nload(name)\n",
)


@pytest.mark.parametrize("source", _SCOPED_BRANCH_SOURCES)
def test_scoped_branch_traversal_matches_independent_flat_reference_and_queries(source):
    tree = ast.parse(source)
    owner, reference = Importers(tree), _UncachedImporters(tree)
    assert owner._position == reference._position
    assert len(owner._scopes) == len(reference._scopes)

    def normalized_histories(item):
        indexes = {scope: index for index, scope in enumerate(item._scopes)}
        return {
            (indexes[scope], channel, name): list(events)
            for (scope, channel, name), events in _journal_histories(item).items()
        }

    # Scope objects belong to different owners and hashes may change set-union
    # order. Compare exact per-owner journal/cutpoint behavior through the shared
    # same-scope stateful oracle above; here verify semantic query equality.
    def roles(item):
        return [
            (node.lineno, node.col_offset, item.called(node))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]

    assert roles(owner) == roles(reference)
    forward = [
        (node, name)
        for node in ast.walk(tree)
        if id(node) in owner._position_of
        for name in ("load", "m", "x", "__import__")
    ]
    answers = [owner._resolve(node, name) for node, name in forward]
    assert answers == [reference._resolve(node, name) for node, name in forward]
    assert list(reversed(answers)) == [
        owner._resolve(node, name) for node, name in reversed(forward)
    ]
    actual, expected = normalized_histories(owner), normalized_histories(reference)
    assert set(actual) == set(expected)
    assert {key: [value for _, value in events] for key, events in actual.items()} == {
        key: [value for _, value in events] for key, events in expected.items()
    }


def test_public_checkpoint_keeps_normal_dict_operations_and_detachment():
    owner = Importers(ast.parse(""))
    root = owner._module
    owner._bind(root, "alias", "import_module")
    checkpoint = owner._checkpoint()
    assert type(checkpoint) is dict
    original = checkpoint.copy()
    key = (root, "bindings", "alias")
    assert checkpoint == original
    assert list(checkpoint.items()) == list(original.items())
    checkpoint.update({key: {"caller"}})
    checkpoint.setdefault((root, "writes", "new"), set()).add("caller-write")
    assert checkpoint.pop(key) == {"caller"}
    checkpoint.clear()
    assert checkpoint == {}
    assert owner._checkpoint() == original
    assert owner._snapshot()[key] == {"import_module"}
    frozen = owner._branch_checkpoint()
    owner._bind(root, "alias", "other")
    assert frozen.flat()[key] == {"import_module"}
    assert owner._checkpoint()[key] == {"other"}


@pytest.mark.parametrize("shape", ("scopes", "keys"))
def test_scoped_branch_above_budget_rebuilds_summaries_without_false_noop(
    monkeypatch, shape
):
    owner = Importers(ast.parse(""))
    root = owner._module
    if shape == "scopes":
        for _ in range(owner._BRANCH_CACHE_MAX_SCOPES + 1):
            owner._new_scope("function", root, ast.parse(""))
    oracle = _JournalOracle(owner)
    if shape == "scopes":
        for scope in owner._scopes:
            owner._bind(scope, "alias", "import_module")
            oracle.bind(scope, "alias", "import_module")
        assert len(owner._scopes) > owner._BRANCH_CACHE_MAX_SCOPES
    else:
        scope = root
        for index in range(owner._BRANCH_CACHE_MAX_KEYS // 2 + 1):
            owner._bind(scope, "alias" + str(index), "import_module")
            oracle.bind(scope, "alias" + str(index), "import_module")
        assert (
            sum(len(channel) for channel in owner._current[root].values())
            > owner._BRANCH_CACHE_MAX_KEYS
        )
    counts = {"items": 0, "flat_restore": 0, "flat_join": 0}

    class CountItems(dict):
        def items(self):
            counts["items"] += 1
            return super().items()

    # Install before any summary exists. Same dict contents/order, with only
    # aggregate enumeration counts; this is not a new source-analysis path.
    assert not owner._branch_head_cache
    for scope in owner._scopes:
        for channel in ("bindings", "writes"):
            owner._current[scope][channel] = CountItems(owner._current[scope][channel])
    old_restore, old_join = owner._flat_restore, owner._flat_join

    def flat_restore(*args, **kwargs):
        counts["flat_restore"] += 1
        return old_restore(*args, **kwargs)

    def flat_join(*args, **kwargs):
        counts["flat_join"] += 1
        return old_join(*args, **kwargs)

    monkeypatch.setattr(owner, "_flat_restore", flat_restore)
    monkeypatch.setattr(owner, "_flat_join", flat_join)
    saved, expected = owner._branch_checkpoint(), oracle.snapshot()
    initial_items = counts["items"]
    position, histories = owner._position, _journal_histories(owner)
    assert saved._flat is None
    owner._restore(saved)
    owner._join(saved, saved)
    assert saved._flat is None
    assert counts["flat_restore"] == counts["flat_join"] == 0
    assert owner._position == position
    assert _journal_histories(owner) == histories
    # Above either budget, current summaries really rebuild. No claim that
    # this private view makes the entire state operation constant/linear.
    assert counts["items"] > initial_items
    target = owner._scopes[-1]
    owner._bind(target, "new_after_checkpoint", "__import__")
    oracle.bind(target, "new_after_checkpoint", "__import__")
    fresh = owner._branch_checkpoint()
    key = (target, "bindings", "new_after_checkpoint")
    assert fresh.flat()[key] == {"__import__"}
    assert key not in saved.flat()
    owner._restore(saved)
    oracle.restore(expected)
    assert counts["flat_restore"] == 1
    assert owner._position == oracle.position
    assert list(owner._snapshot().items()) == list(oracle.snapshot().items())
    assert _journal_histories(owner) == {
        (scope, channel, name): list(events)
        for scope in oracle.scopes
        for channel in ("bindings", "writes")
        for name, events in oracle.histories[scope][channel].items()
    }
    assert len(owner._branch_head_cache) <= owner._BRANCH_CACHE_MAX_SCOPES
    assert owner._branch_head_cache_keys <= owner._BRANCH_CACHE_MAX_KEYS


@given(
    commands=st.lists(
        st.tuples(
            st.sampled_from(("bindings", "writes")),
            st.sampled_from(("restore", "join")),
            st.sampled_from(("dict", "subclass", "proxy")),
            st.sampled_from(("alias", "new_empty", "__import__")),
            st.sampled_from((None, "other", "unbound", "not-written", "import_module")),
            st.booleans(),
        ),
        max_size=30,
    )
)
@example(
    commands=[
        ("writes", "restore", "proxy", "new_empty", None, False),
        ("bindings", "join", "subclass", "alias", None, True),
    ]
)
def test_scoped_branch_mutable_unknown_inputs_both_channels_keep_flat_reference(
    commands,
):
    owner = Importers(ast.parse(""))
    root = owner._module
    function = owner._new_scope("function", root, ast.parse(""))
    function.globals.add("alias")
    oracle = _JournalOracle(owner)
    called = []
    old_restore, old_join = owner._flat_restore, owner._flat_join

    class OrdinaryState(dict):
        pass

    def flat_restore(state, scopes=None):
        called.append("restore")
        return old_restore(state, scopes)

    def flat_join(*states, scopes=None):
        called.append("join")
        return old_join(*states, scopes=scopes)

    owner._flat_restore = flat_restore
    owner._flat_join = flat_join
    for channel, operation, kind, name, value, filtered in commands:
        selected = {function} if filtered else None
        state = {(function, channel, name): set() if value is None else {value}}
        expected = {key: set(values) for key, values in state.items()}
        supplied = (
            OrdinaryState(state)
            if kind == "subclass"
            else MappingProxyType(state)
            if kind == "proxy"
            else state
        )
        count = len(called)
        if operation == "restore":
            owner._restore(supplied, selected)
            oracle.restore(expected, selected)
            assert called[count:] == ["restore"]
        else:
            owner._join(supplied, scopes=selected)
            oracle.join(expected, scopes=selected)
            assert called[count:] == ["join", "restore"]
        owner._branch_checkpoint()
        assert owner._position == oracle.position
        assert list(owner._snapshot().items()) == list(oracle.snapshot().items())
        assert _journal_histories(owner) == {
            (scope, item_channel, item_name): list(events)
            for scope in oracle.scopes
            for item_channel in ("bindings", "writes")
            for item_name, events in oracle.histories[scope][item_channel].items()
        }
    before = len(called)
    owner._join()
    oracle.join()
    assert called[before:] == ["join", "restore"]
    assert owner._position == oracle.position
    assert owner._snapshot() == oracle.snapshot()


def test_scoped_branch_unknown_unreadable_mapping_keeps_original_refusal(monkeypatch):
    owner = Importers(ast.parse(""))
    owner._bind(owner._module, "alias", "import_module")
    position, histories = owner._position, _journal_histories(owner)
    calls = []
    original_restore, original_join = owner._flat_restore, owner._flat_join

    class UnknownState:
        def keys(self):
            raise ValueError("synthetic unreadable mapping")

    def restore(state, scopes=None):
        calls.append("restore")
        return original_restore(state, scopes)

    def join(*states, scopes=None):
        calls.append("join")
        return original_join(*states, scopes=scopes)

    monkeypatch.setattr(owner, "_flat_restore", restore)
    monkeypatch.setattr(owner, "_flat_join", join)
    for operation in (owner._restore, owner._join):
        with pytest.raises(ValueError, match="synthetic unreadable mapping"):
            operation(UnknownState())
    assert calls == ["restore", "join"]
    assert owner._position == position
    assert _journal_histories(owner) == histories


def _debt_model_row(**changes):
    row = {
        "module": "openadapt_flow.runtime.planted",
        "path": "packages/flow/openadapt_flow/runtime/planted.py",
        "line": 3,
        "edge_kind": "direct",
        "diagnostic": "openadapt_flow.runtime.planted:3: unresolvable dynamic import (importer value escape)",
        "reason": "namespace-query",
        "follow_up": _DEBT_FOLLOW_UP,
        "source_statement_sha256": "a" * 64,
    }
    return {**row, **changes}


@given(field=st.sampled_from(sorted(_DEBT_ROW_KEYS)))
def test_debt_ceiling_detects_every_changed_row_field(field):
    row = _debt_model_row()
    changed = dict(row)
    changed[field] = 4 if field == "line" else str(row[field]) + "changed"
    assert _debt_row_differences([row], [row]) == []
    assert _debt_row_differences([changed], [row])
    assert _debt_row_differences([row], [changed])


@pytest.mark.parametrize("side", ["live", "baseline"])
def test_debt_ceiling_preserves_multiplicity_and_refuses_duplicates(side):
    row = _debt_model_row()
    left, right = [row], [dict(row)]
    (left if side == "live" else right).append(dict(row))
    assert _debt_row_differences(left, right)
    assert _debt_row_differences([row, row], [row, row])


@given(
    commands=st.lists(
        st.sampled_from(["add", "remove", "commit", "move", "duplicate"]), max_size=35
    )
)
def test_debt_ceiling_temporal_membership_model(commands):
    live = {}
    baseline = []
    for command in commands:
        if command == "add":
            live["site"] = _debt_model_row()
        elif command == "remove":
            live.pop("site", None)
        elif command == "commit":
            baseline = [dict(row) for row in live.values()]
        elif command == "move" and "site" in live:
            live["site"] = _debt_model_row(
                line=4,
                diagnostic="openadapt_flow.runtime.planted:4: unresolvable dynamic import (importer value escape)",
            )
        current = [dict(row) for row in live.values()]
        if command == "duplicate" and current:
            current.append(dict(current[0]))
        expected = (
            len(current) == len({json.dumps(row, sort_keys=True) for row in current})
            and current == baseline
        )
        before = json.dumps([current, baseline], sort_keys=True)
        assert (_debt_row_differences(current, baseline) == []) is expected
        assert json.dumps([current, baseline], sort_keys=True) == before


def test_debt_ceiling_removal_requires_same_commit_baseline_shrink():
    direct = _debt_model_row()
    transitive = _debt_model_row(edge_kind="transitive")
    assert _debt_row_differences([direct, transitive], [direct, transitive]) == []
    assert _debt_row_differences([transitive], [direct, transitive])
    assert _debt_row_differences([transitive], [transitive]) == []
    assert _debt_row_differences([], [transitive])
    assert _debt_row_differences([], []) == []


@pytest.mark.parametrize("code", ["load(name)\n", "if True:\n    load(name)\n"])
def test_debt_source_binding_is_structural_and_detects_same_line_change(code):
    line = 1 if code.startswith("load") else 2
    message = f"openadapt_flow.runtime.planted:{line}: unresolvable dynamic import (importer value escape)"
    args = (
        "openadapt_flow.runtime.planted",
        "packages/flow/openadapt_flow/runtime/planted.py",
        [message],
    )
    first = _debt_site_metadata(ast.parse(code), *args)
    second = _debt_site_metadata(ast.parse(code.replace("name", "other")), *args)
    assert set(first) == {message} and set(second) == {message}
    assert (
        first[message]["source_statement_sha256"]
        != second[message]["source_statement_sha256"]
    )


@pytest.mark.parametrize(
    "code,line",
    [("pass\n", 2), ("load(name); other()\n", 1), ("if True: load(name)\n", 1)],
)
def test_debt_source_binding_refuses_absent_or_ambiguous_statement(code, line):
    message = f"openadapt_flow.runtime.planted:{line}: unresolvable dynamic import (importer value escape)"
    assert (
        _debt_site_metadata(
            ast.parse(code),
            "openadapt_flow.runtime.planted",
            "packages/flow/openadapt_flow/runtime/planted.py",
            [message],
        )
        == {}
    )


def _debt_synthetic_observation(document):
    """Explicit policy-only graph double; real parsed-source binding tested above."""
    graph = Graph()
    for row in document["entries"]:
        module = row["module"]
        graph.setdefault(module, {})
        graph.dynamic_problems.setdefault(module, [])
        if row["diagnostic"] not in graph.dynamic_problems[module]:
            graph.dynamic_problems[module].append(row["diagnostic"])
        graph.debt_sites.setdefault(module, {})[row["diagnostic"]] = {
            key: row[key]
            for key in (
                "module",
                "path",
                "line",
                "diagnostic",
                "source_statement_sha256",
            )
        }
    return graph


@pytest.mark.parametrize("edge_kind", ["direct", "transitive"])
def test_repository_debt_boundary_preserves_raw_results_and_forbidden_refusal(
    unresolved_debt_baseline, edge_kind
):
    document = json.loads(unresolved_debt_baseline)
    graph = _debt_synthetic_observation(document)
    raw = [
        row["diagnostic"]
        for row in document["entries"]
        if row["edge_kind"] == edge_kind
    ]
    before = json.dumps(
        [graph, graph.dynamic_problems, graph.debt_sites, raw], sort_keys=True
    )
    assert (
        repository_debt_violations(
            graph, raw, unresolved_debt_baseline, edge_kind=edge_kind
        )
        == []
    )
    for forbidden in (
        "openadapt_flow.runtime.planted -> benchmark (benchmark, lines [3])",
        "openadapt_flow.runtime.planted > planted.ui.window",
        "missing dynamic import analysis",
        "stale or unproved resolved dynamic import DATA",
    ):
        assert repository_debt_violations(
            graph, [*raw, forbidden], unresolved_debt_baseline, edge_kind=edge_kind
        )
    assert repository_debt_violations(
        graph, raw + raw[:1], unresolved_debt_baseline, edge_kind=edge_kind
    )
    assert repository_debt_violations(
        graph, raw[1:], unresolved_debt_baseline, edge_kind=edge_kind
    )
    assert repository_debt_violations(
        graph, raw, unresolved_debt_baseline + b" ", edge_kind=edge_kind
    )
    assert (
        json.dumps(
            [graph, graph.dynamic_problems, graph.debt_sites, raw], sort_keys=True
        )
        == before
    )


def test_repository_debt_policy_does_not_filter_default_strict_validators(
    unresolved_debt_baseline,
):
    graph = Graph()
    module = "openadapt_flow.runtime.planted"
    graph[module] = {"benchmark": [3]}
    graph.dynamic_problems[module] = [
        "openadapt_flow.runtime.planted:3: unresolvable dynamic import (importer value escape)"
    ]
    graph.debt_sites[module] = {}
    contract = {
        "core": [module],
        "forbidden": {"benchmark": ["benchmark"]},
        "exceptions": [],
    }
    direct, transitive = (
        direct_violations(graph, contract),
        transitive_violations(graph, contract),
    )
    assert graph.dynamic_problems[module][0] in direct
    assert graph.dynamic_problems[module][0] in transitive
    assert len(direct) == 2 and len(transitive) == 2
    assert repository_debt_violations(
        graph, direct, unresolved_debt_baseline, edge_kind="direct"
    )
    assert repository_debt_violations(
        graph, transitive, unresolved_debt_baseline, edge_kind="transitive"
    )
    assert direct_violations(graph, contract) == direct
    assert transitive_violations(graph, contract) == transitive


def test_debt_binding_uses_normal_graph_public_repository_path(tmp_path: Path):
    root = tmp_path / "openadapt_flow"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "core.py").write_text(
        "import importlib\nimportlib.import_module(target)\n", encoding="utf-8"
    )
    observed = build_graph(root, "openadapt_flow")
    message = "openadapt_flow.core:2: unresolvable dynamic import (nonliteral or unresolved import target)"
    assert message in observed.dynamic_problems["openadapt_flow.core"]
    assert (
        observed.debt_sites["openadapt_flow.core"][message]["path"]
        == "packages/flow/openadapt_flow/core.py"
    )
    assert observed.debt_sites["openadapt_flow.core"][message]["line"] == 2


@pytest.mark.parametrize("edge_kind", ["direct", "transitive"])
def test_repository_debt_receipt_detaches_raw_and_policy_without_safety_claim(
    unresolved_debt_baseline, edge_kind
):
    document = json.loads(unresolved_debt_baseline)
    graph = _debt_synthetic_observation(document)
    raw = [
        row["diagnostic"]
        for row in document["entries"]
        if row["edge_kind"] == edge_kind
    ]
    policy = repository_debt_violations(
        graph, raw, unresolved_debt_baseline, edge_kind=edge_kind
    )
    assert policy == []
    _retain_repository_debt_observation(
        graph, raw, policy, unresolved_debt_baseline, edge_kind=edge_kind
    )
    receipt = graph.repository_debt_observations[edge_kind]
    assert receipt["raw_validator_results"] == tuple(raw)
    assert len(receipt["raw_validator_results"]) == (9 if edge_kind == "direct" else 21)
    assert len(receipt["reviewed_debt_memberships"]) == len(raw)
    assert receipt["repository_policy_results"] == ()
    assert receipt["baseline_bound"] is True
    assert (
        receipt["target_admission"] is False
        and receipt["capability_safety_claim"] is False
    )
    raw.append("changed after observation")
    policy.append("changed after observation")
    assert "changed after observation" not in receipt["raw_validator_results"]
    assert receipt["repository_policy_results"] == ()
