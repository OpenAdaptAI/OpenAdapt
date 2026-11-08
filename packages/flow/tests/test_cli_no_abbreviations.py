"""Every CLI parser resolves only explicitly declared long option spellings."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest


def _parser_tree(root):
    """Visit dynamically registered children once, including nested aliases."""
    pending = [((), root)]
    seen = set()
    while pending:
        path, parser = pending.pop()
        if id(parser) in seen:
            continue
        seen.add(id(parser))
        yield path, parser
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(
                    (path + (name,), child) for name, child in action.choices.items()
                )


def _optional_resolutions(parser, spelling):
    """Inspect argparse matches across its supported internal result shapes."""
    resolved = parser._parse_optional(spelling)
    if resolved is None:
        return ()
    rows = resolved if isinstance(resolved, list) else [resolved]
    assert all(isinstance(row, tuple) and len(row) in (3, 4) for row in rows)
    assert all(row[0] is None or isinstance(row[0], argparse.Action) for row in rows)
    return rows


def test_every_cli_parser_rejects_all_undeclared_long_option_prefixes():
    from openadapt.cli import _seal_view_parser
    from openadapt_agent.cli import build_parser as compatibility_agent_parser

    from openadapt_flow.__main__ import build_parser as flow_parser
    from openadapt_flow.agent.cli import build_parser as agent_parser
    from openadapt_flow.wrap.cli import build_parser as wrap_parser

    assert compatibility_agent_parser is agent_parser
    roots = (flow_parser(), agent_parser(), _seal_view_parser(), wrap_parser())
    launcher = roots[2].parse_args(
        ["synthetic-run", "--keys", "keys.json", "--bundle", "bundle", "--no-open"]
    )
    assert launcher.run_dir == Path("synthetic-run")
    assert launcher.keys == Path("keys.json") and launcher.bundle == Path("bundle")
    assert launcher.no_open is True
    parser_count = prefix_count = option_count = 0
    for root in roots:
        for path, parser in _parser_tree(root):
            assert parser.allow_abbrev is False, (root.prog, path)
            parser_count += 1
            declared = parser._option_string_actions
            for option, action in declared.items():
                if not option.startswith("--"):
                    continue
                # Resolve the full spelling without invoking its action, type
                # converter, file handling, dispatcher, help or version output.
                resolved = _optional_resolutions(parser, option)
                assert len(resolved) == 1 and resolved[0][0] is action
                option_count += 1
                for length in range(2, len(option)):
                    prefix = option[:length]
                    if prefix in declared:
                        continue
                    for spelling in (prefix, prefix + "=synthetic"):
                        resolved = _optional_resolutions(parser, spelling)
                        assert all(row[0] is None for row in resolved), (
                            root.prog,
                            path,
                            option,
                            spelling,
                        )
                        prefix_count += 1
    # Guard against accidentally testing empty roots or dropping Flow's
    # dynamically registered modules from the complete parser construction.
    assert parser_count > 100 and option_count > 100 and prefix_count > option_count


def _parser_constructor_calls(tree):
    """Resolve static imports and parser bases without executing source code."""
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    comprehensions = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

    def scope_nodes(nodes):
        for node in nodes:
            if not isinstance(node, ast.AST):
                continue
            yield node
            if isinstance(node, comprehensions):
                # Only the first iterable is evaluated outside the implicit
                # comprehension scope; its targets must not shadow this scope.
                yield from scope_nodes([node.generators[0].iter])
            elif isinstance(node, scopes):
                # Defaults, decorators and bases belong to the enclosing scope.
                children = (
                    value
                    for field, value in ast.iter_fields(node)
                    if field != "body"
                )
                for value in children:
                    if isinstance(value, ast.AST):
                        yield from scope_nodes([value])
                    elif isinstance(value, list):
                        yield from scope_nodes(value)
            else:
                yield from scope_nodes(ast.iter_child_nodes(node))

    def provenance(node, bindings):
        if isinstance(node, ast.IfExp):
            return provenance(node.body, bindings) | provenance(node.orelse, bindings)
        if isinstance(node, ast.Name):
            return bindings.get(node.id, set())
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "ArgumentParser"
            and isinstance(node.value, ast.Name)
            and (
                node.value.id == "argparse"
                or "module" in bindings.get(node.value.id, set())
            )
        ):
            return {"parser"}
        return set()

    def comprehension_parts(node):
        parameters = [
            target.id
            for generator in node.generators
            for target in ast.walk(generator.target)
            if isinstance(target, ast.Name)
        ]
        body = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        body += [generator.iter for generator in node.generators[1:]]
        body += [condition for generator in node.generators for condition in generator.ifs]
        return body, parameters

    def enclosing_assignments(nodes):
        for node in scope_nodes(nodes):
            if isinstance(node, ast.NamedExpr):
                yield node.target.id
            elif isinstance(node, comprehensions):
                body, _ = comprehension_parts(node)
                yield from enclosing_assignments(body)

    def scope_bindings(body, inherited, parameters=(), class_scope=False):
        nodes = list(scope_nodes(body))
        local_names = set(parameters) | set(enclosing_assignments(body))
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local_names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local_names.update(
                    name.asname or name.name.split(".")[0] for name in node.names
                )
        for node in nodes:
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                local_names.difference_update(node.names)
        # Function locals shadow outer bindings throughout the function. Class
        # bodies can still read an outer name before assigning their own value.
        bindings = {
            name: set(kinds)
            for name, kinds in inherited.items()
            if class_scope or name not in local_names
        }
        for node in nodes:
            if isinstance(node, ast.Import):
                for name in node.names:
                    if name.name == "argparse":
                        bindings.setdefault(name.asname or name.name, set()).add("module")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "argparse"
                and node.level == 0
            ):
                for name in node.names:
                    if name.name == "ArgumentParser":
                        bindings.setdefault(name.asname or name.name, set()).add("parser")
        # A source census cannot decide which runtime branch rebinds a name.
        # Retain every possible static parser provenance instead of letting an
        # ambiguous assignment erase coverage. No application code is executed.
        changed = True
        while changed:
            changed = False
            for node in nodes:
                targets = []
                kinds = set()
                if isinstance(node, ast.ClassDef):
                    targets = [node.name]
                    if any(
                        "parser" in provenance(base, bindings) for base in node.bases
                    ):
                        kinds = {"parser"}
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                    assigned = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    targets = [
                        target.id for target in assigned if isinstance(target, ast.Name)
                    ]
                    kinds = provenance(node.value, bindings)
                for name in targets:
                    previous = bindings.setdefault(name, set())
                    if not kinds <= previous:
                        previous.update(kinds)
                        changed = True
            # Assignment expressions escape a comprehension into this scope.
            # Resolve their values inside the comprehension, where iteration
            # targets shadow outer aliases, then propagate only known kinds.
            for node in nodes:
                if not isinstance(node, comprehensions):
                    continue
                child_body, child_parameters = comprehension_parts(node)
                _, child_bindings = scope_bindings(child_body, bindings, child_parameters)
                for name in enclosing_assignments(child_body):
                    kinds = child_bindings.get(name, set())
                    previous = bindings.setdefault(name, set())
                    if not kinds <= previous:
                        previous.update(kinds)
                        changed = True
        return nodes, bindings

    def visit_scope(body, inherited, parameters=(), class_scope=False):
        nodes, bindings = scope_bindings(body, inherited, parameters, class_scope)
        for node in nodes:
            if isinstance(node, ast.Call) and (
                "parser" in provenance(node.func, bindings)
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser"
            ):
                yield node
            if isinstance(node, comprehensions):
                child_body, parameters = comprehension_parts(node)
                yield from visit_scope(child_body, bindings, parameters)
            elif isinstance(node, scopes):
                parameters = []
                if not isinstance(node, ast.ClassDef):
                    args = node.args
                    parameters = [
                        arg.arg
                        for arg in (
                            *args.posonlyargs,
                            *args.args,
                            *args.kwonlyargs,
                            *([args.vararg] if args.vararg else []),
                            *([args.kwarg] if args.kwarg else []),
                        )
                    ]
                child_body = [node.body] if isinstance(node, ast.Lambda) else node.body
                # A method's lexical parent is outside its class namespace.
                child_bindings = inherited if class_scope else bindings
                yield from visit_scope(
                    child_body,
                    child_bindings,
                    parameters,
                    class_scope=isinstance(node, ast.ClassDef),
                )

    return visit_scope(tree.body, {})


def test_standalone_cli_constructor_inventory_also_disables_abbreviations():
    repository = Path(__file__).resolve().parents[3]
    roots = (
        repository / "packages/flow",
        repository / "openadapt",
        repository / "packages/agent",
        repository / "scripts",
    )
    constructors = 0
    for root in roots:
        for source in root.rglob("*.py"):
            relative = source.relative_to(root)
            if (
                any(part in ("test", "tests") for part in relative.parts)
                or source.name.startswith("test_")
                or source.name == "conftest.py"
            ):
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in _parser_constructor_calls(tree):
                values = [kw.value for kw in node.keywords if kw.arg == "allow_abbrev"]
                assert (
                    len(values) == 1
                    and isinstance(values[0], ast.Constant)
                    and values[0].value is False
                ), source
                constructors += 1
    assert constructors > 500


@pytest.mark.parametrize("flag", ["--permission", "--perm"])
def test_run_does_not_route_a_permission_prefix_to_the_trust_registry(flag):
    from openadapt_flow.__main__ import build_parser

    with pytest.raises(SystemExit) as refused:
        build_parser().parse_args(["run", "bundle", flag, "permission.json"])
    assert refused.value.code == 2


@pytest.mark.parametrize(
    ("permissions", "message"),
    [
        ([], "process requires --permission NAME=ENVELOPE"),
        (
            ["--permission", "first=a.json", "--permission", "first=b.json"],
            "--permission name 'first' given more than once",
        ),
    ],
)
def test_process_permission_errors_use_the_full_canonical_flag(permissions, message):
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.cli_process import cmd_process

    args = build_parser().parse_args(
        [
            "process",
            "--child",
            "first=bundle-a",
            "--child",
            "second=bundle-b",
            "--out",
            "parent",
            *permissions,
        ]
    )
    # These refusals occur during argument validation, before any bundle read
    # or command execution. Existing valid old/new alias controls stay intact.
    with pytest.raises(SystemExit) as refused:
        cmd_process(args)
    assert str(refused.value).startswith(message)
