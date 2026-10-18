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
from openadapt_flow.ir import ActionKind, BackendHints, RunReport, Step, Workflow


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


def test_a_desktop_mcp_tool_refuses_without_or_with_the_wrong_deployment(
    tmp_path: Path, monkeypatch
) -> None:
    import openadapt_flow.backends.factory as factory

    monkeypatch.setattr(
        factory, "build_backend", lambda *a, **k: pytest.fail("must not build")
    )
    server = _load_server(tmp_path, monkeypatch, "macos")
    out = server.run_triage_note(note="synthetic")
    assert out["success"] is False and out["outcome"] == "REFUSED"
    assert "--config deploy.yaml" in out["detail"]

    deploy = tmp_path / "deploy.yaml"
    deploy.write_text(
        "backend:\n  kind: linux\n  linux_app: a\n  linux_window_title: b\n"
    )
    server.DEPLOYMENT = str(deploy)
    out = server.run_triage_note(note="synthetic")
    assert out["outcome"] == "REFUSED"
    assert "recorded on macos" in out["detail"] and "'linux'" in out["detail"]


def test_a_desktop_mcp_tool_drives_the_app_its_deployment_names(
    tmp_path: Path, monkeypatch
) -> None:
    import openadapt_flow.backends.factory as factory
    from openadapt_flow.runtime import Replayer

    seen: dict[str, Any] = {}

    class Backend:
        def close(self) -> None:
            seen["closed"] = True

    def build_backend(cfg: Any, **_: Any) -> Backend:
        seen["cfg"] = cfg
        return Backend()

    def run(self: Any, workflow: Workflow, **kwargs: Any) -> RunReport:
        seen["kind"] = kwargs["execution_target_kind"]
        seen["params"] = kwargs["params"]
        return RunReport(
            workflow_name=workflow.name,
            started_at="2026-10-18T00:00:00Z",
            success=True,
            execution_profile="standard",
            execution_outcome="VERIFIED",
            transaction_outcome="VERIFIED",
        )

    monkeypatch.setattr(factory, "build_backend", build_backend)
    monkeypatch.setattr(Replayer, "run", run)
    server = _load_server(tmp_path, monkeypatch, "citrix")
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text(
        "backend:\n  kind: citrix\n  rdp_window: wfica32\n"
        "  rdp_readiness_text: Patient Notes\n"
    )
    server.DEPLOYMENT = str(deploy)
    out = server.run_triage_note(note="synthetic")
    assert out["success"] is True and out["outcome"] == "VERIFIED"
    assert seen["cfg"].kind == "citrix" and seen["cfg"].rdp_window == "wfica32"
    assert seen["kind"] == "citrix" and seen["params"] == {"note": "synthetic"}
    assert seen["closed"] is True


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
