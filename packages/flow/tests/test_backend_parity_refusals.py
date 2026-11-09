"""Commands that used to assume a browser now follow the bundle's surface.

``emit-mcp`` and ``emit-skill`` name the app with a deployment file for a
bundle recorded off the browser. ``serve-execute``, ``bench`` and the attended
console refuse what they can't drive, with the reason, before any action. See
docs/BACKEND_PARITY.md.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.emit import emit_mcp_server, emit_skill
from openadapt_flow.emit.skill import bundle_surface
from openadapt_flow.ir import ActionKind, BackendHints, Step, Workflow


def _bundle(tmp_path: Path, surface: str | None) -> Path:
    bundle = tmp_path / "bundle"
    Workflow(
        name="Triage Note",
        surface=surface,
        params={"note": "x"},
        steps=[Step(id="s0", intent="type note", action=ActionKind.TYPE, param="note")],
    ).save(bundle)
    return bundle


def test_the_bundle_surface_is_web_unless_it_names_another() -> None:
    assert bundle_surface(Workflow(name="w", steps=[])) == "web"
    assert bundle_surface(Workflow(name="w", steps=[], surface="linux")) == "linux"
    hinted = Workflow(name="w", steps=[], backend_hints=BackendHints(backend="citrix"))
    assert bundle_surface(hinted) == "citrix"


# -- emit-skill -----------------------------------------------------------------


@pytest.mark.parametrize("surface", ["windows", "macos", "linux", "rdp", "citrix"])
def test_a_desktop_skill_names_a_deployment_file_not_a_url(
    tmp_path: Path, surface: str
) -> None:
    from openadapt_flow.__main__ import build_parser

    out = emit_skill(_bundle(tmp_path, surface), tmp_path / "skill")
    text = (out / "SKILL.md").read_text()
    assert "--url" not in text.replace("Don't pass `--url`", "")
    assert f"recorded on the `{surface}` surface" in text
    line = next(
        line for line in text.splitlines() if line.startswith("openadapt-flow run")
    )
    args = build_parser().parse_args(shlex.split(line)[1:])
    assert args.command == "run" and args.config == "<DEPLOYMENT_FILE>"


def test_a_web_skill_still_takes_the_url(tmp_path: Path) -> None:
    out = emit_skill(_bundle(tmp_path, None), tmp_path / "skill")
    assert "--url <APP_URL>" in (out / "SKILL.md").read_text()


# -- emit-mcp -------------------------------------------------------------------


def _load_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> types.ModuleType:
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FakeFastMCP:
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self):  # noqa: ANN202 - FastMCP's decorator factory
            return lambda func: func

    fastmcp.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    for name in ("mcp", "mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    monkeypatch.delenv("OPENADAPT_DEPLOYMENT", raising=False)
    out = emit_mcp_server(_bundle(tmp_path, surface), tmp_path / "mcp" / "server.py")
    spec = importlib.util.spec_from_file_location(f"emitted_{surface}", out)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_desktop_mcp_tool_takes_no_url(tmp_path: Path, monkeypatch) -> None:
    import inspect

    server = _load_server(tmp_path, monkeypatch, "macos")
    assert list(inspect.signature(server.run_triage_note).parameters) == ["note"]
    assert "playwright" not in (tmp_path / "mcp" / "server.py").read_text()


def test_a_desktop_mcp_tool_refuses_without_a_deployment(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("nothing may run")
    )
    server = _load_server(tmp_path, monkeypatch, "macos")
    out = server.run_triage_note(note="synthetic")
    assert out["success"] is False and out["outcome"] == "REFUSED"
    assert "--config deploy.yaml" in out["detail"]


def test_a_desktop_mcp_tool_runs_the_governed_cli_with_every_param_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """A parameter named like a local (``kind``) reaches the run unchanged."""

    import json
    import subprocess

    bundle = tmp_path / "bundle"
    Workflow(
        name="Triage Note",
        surface="citrix",
        params={"kind": "x", "section": "y", "workflow": "z"},
        steps=[
            Step(id="s0", intent="type", action=ActionKind.TYPE, param="kind"),
            Step(id="s1", intent="type", action=ActionKind.TYPE, param="section"),
            Step(id="s2", intent="type", action=ActionKind.TYPE, param="workflow"),
        ],
    ).save(bundle)
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> Any:
        args = dict(zip(argv[::1], argv[1:]))
        seen["argv"] = argv
        seen["params"] = json.loads(Path(args["--params-file"]).read_text())
        run_dir = Path(args["--run-dir"])
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(
            json.dumps(
                {
                    "execution_outcome": "VERIFIED",
                    "transaction_outcome": "VERIFIED",
                    "production_eligible": True,
                    "model_calls": 0,
                }
            )
        )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = lambda name: types.SimpleNamespace(  # type: ignore[attr-defined]
        tool=lambda: lambda func: func
    )
    for name in ("mcp", "mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    out_path = emit_mcp_server(bundle, tmp_path / "mcp" / "server.py")
    spec = importlib.util.spec_from_file_location("emitted_params", out_path)
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(subprocess, "run", run)
    server.DEPLOYMENT = str(tmp_path / "deploy.yaml")

    out = server.run_triage_note(kind="REAL", section="S", workflow="W")
    assert out["success"] is True and out["outcome"] == "VERIFIED"
    assert seen["params"] == {"kind": "REAL", "section": "S", "workflow": "W"}
    assert seen["argv"][1:5] == [
        "-m",
        "openadapt_flow",
        "run",
        str(out_path.parent / "bundle"),
    ]
    assert seen["argv"][seen["argv"].index("--config") + 1] == server.DEPLOYMENT
    # The params file holds entered values; it is gone after the run.
    assert not Path(dict(zip(seen["argv"], seen["argv"][1:]))["--params-file"]).exists()


def test_a_desktop_mcp_tool_reports_the_run_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end: the real ``openadapt-flow run`` refuses a deployment that
    names another surface, and the tool says so. Nothing is built."""

    import os

    server = _load_server(tmp_path, monkeypatch, "macos")
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text(
        "backend:\n  kind: linux\n  linux_app: a\n  linux_window_title: b\n"
    )
    server.DEPLOYMENT = str(deploy)
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH")) if p),
    )
    out = server.run_triage_note(note="synthetic")
    assert out["success"] is False and out["outcome"] == "REFUSED"
    assert "REFUSED" in out["detail"] and "'macos'" in out["detail"]


def test_a_multi_surface_bundle_is_named_by_its_primary_surface() -> None:
    from openadapt_flow.ir import SurfaceSpec

    workflow = Workflow(
        name="w", steps=[], surfaces=[SurfaceSpec(id="primary", kind="windows")]
    )
    assert bundle_surface(workflow) == "windows"


# -- serve-execute, bench, attended console --------------------------------------


@pytest.mark.parametrize("surface", ["windows", "macos", "linux", "rdp", "citrix"])
def test_the_execute_worker_refuses_a_desktop_bundle_before_any_effect(
    tmp_path: Path, surface: str
) -> None:
    from openadapt_flow.execute.dispatch import DispatchError, live_replay
    from openadapt_flow.execute.models import AdmittedBundle

    admission = AdmittedBundle(
        qualification_id="q",
        workflow_version="1",
        workflow_digest="sha256:" + "0" * 64,
        environment_id="env",
        minimum_effect_strength="independent_system_of_record",
        bundle_dir=str(_bundle(tmp_path, surface)),
    )
    with pytest.raises(DispatchError, match=f"recorded on '{surface}'") as caught:
        live_replay(admission, None, tmp_path / "run")  # type: ignore[arg-type]
    assert caught.value.pre_effect is True


def test_bench_refuses_a_desktop_bundle(tmp_path: Path, capsys) -> None:
    from openadapt_flow.__main__ import main

    rc = main(["bench", str(_bundle(tmp_path, "linux")), "--run-root", str(tmp_path)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "BENCH REFUSED" in out and "recorded on 'linux'" in out


def test_attended_citrix_actions_need_a_readiness_marker() -> None:
    from openadapt_flow.deployment import DeploymentConfig
    from openadapt_flow.runtime.durable.attended_service import (
        AttendedActionService,
    )

    with pytest.raises(ValueError, match="rdp_readiness_text"):
        AttendedActionService(
            DeploymentConfig.model_validate(
                {"backend": {"kind": "citrix", "rdp_window": "wfica32"}}
            )
        )
