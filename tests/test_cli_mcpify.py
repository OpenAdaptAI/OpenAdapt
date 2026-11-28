"""Tests for the top-level ``openadapt mcpify`` launcher command."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli


def test_mcpify_forwards_arguments_and_exit_code(monkeypatch):
    seen: list[list[str]] = []

    def fake_main(argv):
        seen.append(list(argv))
        sys.stdout.write("mcpify stdout\n")
        sys.stderr.write("mcpify stderr\n")
        return 7

    monkeypatch.setitem(
        sys.modules,
        "openadapt_verify.mcpify",
        types.SimpleNamespace(main=fake_main),
    )
    monkeypatch.setattr(
        cli,
        "_invoke_flow",
        lambda _argv: (_ for _ in ()).throw(
            AssertionError("mcpify must not dispatch to openadapt-flow")
        ),
    )

    argv = ["mcpify", "--from-compiled", ".openadapt/compiled", "--server-name", "demo"]
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 7
    assert seen == [["--from-compiled", ".openadapt/compiled", "--server-name", "demo"]]
    assert result.stdout == "mcpify stdout\n"
    assert result.stderr == "mcpify stderr\n"


def test_mcpify_help_forwards_to_verify_entrypoint_when_installed(monkeypatch):
    seen: list[list[str]] = []

    def fake_main(argv):
        seen.append(list(argv))
        sys.stdout.write("usage: openadapt mcpify\n")
        return 0

    monkeypatch.setitem(
        sys.modules,
        "openadapt_verify.mcpify",
        types.SimpleNamespace(main=fake_main),
    )

    result = CliRunner().invoke(cli.main, ["mcpify", "--help"])
    assert result.exit_code == 0
    assert seen == [["--help"]]
    assert "usage: openadapt mcpify" in result.stdout


def test_mcpify_missing_package_shows_help_or_install_error(monkeypatch):
    def fake_import(name: str):
        if name == "openadapt_verify.mcpify":
            err = ModuleNotFoundError("No module named 'openadapt_verify'")
            err.name = "openadapt_verify"
            raise err
        return __import__(name)

    monkeypatch.setattr(cli, "import_module", fake_import)

    help_res = CliRunner().invoke(cli.main, ["mcpify", "--help"])
    assert help_res.exit_code == 0
    assert (
        "Serve compiled workflow packs or JIT plans as a local MCP server."
        in help_res.stdout
    )

    err_res = CliRunner().invoke(cli.main, ["mcpify", "--from-compiled"])
    assert err_res.exit_code == 2
    assert "openadapt-verify is not installed" in err_res.stderr


def test_mcpify_real_verify_help_and_from_compiled_shorthand(monkeypatch, tmp_path):
    verify_src = Path(__file__).resolve().parents[1] / "packages" / "verify" / "src"
    if not verify_src.is_dir():
        pytest.skip("packages/verify/src not present")
    monkeypatch.syspath_prepend(str(verify_src))

    # Real --help via launcher
    help_res = CliRunner().invoke(cli.main, ["mcpify", "--help"])
    assert help_res.exit_code == 0
    assert "serve" in help_res.stdout
    assert "config" in help_res.stdout
    assert "pubkey" in help_res.stdout

    # Real serve --help shows --from-compiled
    serve_help = CliRunner().invoke(cli.main, ["mcpify", "serve", "--help"])
    assert serve_help.exit_code == 0
    assert "--from-compiled" in serve_help.stdout

    # Bare `openadapt mcpify --from-compiled <dir>` defaults to `serve`
    from openadapt_verify import mcpify as verify_mcpify

    assert verify_mcpify._normalize_argv(["--from-compiled"]) == [
        "serve",
        "--from-compiled",
    ]
    assert verify_mcpify._normalize_argv(["--from-compiled", str(tmp_path)]) == [
        "serve",
        "--from-compiled",
        str(tmp_path),
    ]
    assert verify_mcpify._normalize_argv(["--spec", "pack.json"]) == [
        "serve",
        "--spec",
        "pack.json",
    ]
    assert verify_mcpify._normalize_argv(["config", "--from-compiled"]) == [
        "config",
        "--from-compiled",
    ]

    # Bare --from-compiled parses with default `.openadapt/compiled`
    parser = verify_mcpify._build_parser()
    parsed = parser.parse_args(verify_mcpify._normalize_argv(["--from-compiled"]))
    assert parsed.command == "serve"
    assert parsed.from_compiled == ".openadapt/compiled"

    # End-to-end `openadapt mcpify --from-compiled <dir>` serves stdio JSON-RPC initialize
    spec_file = tmp_path / "demo.oapack.json"
    spec_file.write_text(
        json.dumps(
            {
                "name": "demo_tool",
                "description": "Demo workflow tool",
                "input_schema": {"type": "object", "properties": {}, "required": []},
                "steps": [
                    {
                        "op": "json_write",
                        "path": str(tmp_path / "out.json"),
                        "data": {"ok": True},
                    }
                ],
                "readback_contract": {
                    "kind": "json_file",
                    "path": str(tmp_path / "out.json"),
                    "expected": {"ok": True},
                },
            }
        ),
        encoding="utf-8",
    )
    init_req = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        )
        + "\n"
    )
    serve_res = CliRunner().invoke(
        cli.main,
        ["mcpify", "--spec", str(spec_file)],
        input=init_req,
    )
    assert serve_res.exit_code == 0, serve_res.stderr
    reply = json.loads(serve_res.stdout.strip())
    assert reply["id"] == 1
    assert reply["result"]["serverInfo"]["name"] == "demo_tool"
