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
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                constructor = (
                    node.func.attr == "ArgumentParser"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "argparse"
                )
                if not constructor and node.func.attr != "add_parser":
                    continue
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
