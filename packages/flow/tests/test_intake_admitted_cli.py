"""#1978 Codex BLOCK: the intake-admitted bundle identity reaches the CLI.

The intake grant admits one bundle digest. These drive the real
AgentBridge -> RunService -> FlowCliEngine -> FlowRunner path into the real
``openadapt-flow run`` entry point (``main``) in-process, through the run
gate, up to the replay handoff. Only the replay itself is captured, so
nothing actuates. A valid bundle edit made after the service's digest check
must never be what the CLI admits or authorizes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

import openadapt_flow.__main__ as cli
import openadapt_flow.agent.runner as runner_mod
from openadapt_flow.agent.bridge import AgentBridge
from openadapt_flow.agent.runner import FlowRunner, RunnerConfig
from openadapt_flow.intake.param_gate import bundle_sha256
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ParamKind,
    ParamSpec,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.provenance import (
    ParamProvenance,
    ProvenanceFile,
    SourceRef,
    sha256_bytes,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

DOC = sha256_bytes(b"referral A")
SUCCESS = {
    "workflow_name": "intake-write",
    "success": True,
    "execution_outcome": "VERIFIED",
    "transaction_outcome": "VERIFIED",
    "execution_profile": "standard",
    "production_eligible": True,
    "results": [{"step_id": "s1", "intent": "click 'Save note'", "ok": True}],
    "model_calls": 0,
    "total_ms": 10.0,
}
_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]


def _click(step_id: str, intent: str, ocr: str, **kw: Any) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text=ocr,
            context_text="Row 42 Jane Doe",
        ),
        identity_armed=True,
        expect=list(_PC),
        **kw,
    )


def _save(bundle: Path, open_intent: str = "click 'Open chart'") -> None:
    Workflow(
        name="intake-write",
        params={"patient": "Recorded Person"},
        param_specs={
            "patient": ParamSpec(
                name="patient", type=ParamKind.STRING, intake_field="patient_name"
            )
        },
        steps=[
            _click("s0", open_intent, "Open", risk="reversible"),
            _click(
                "s1",
                "click 'Save note'",
                "Save",
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": "p1"},
                        idempotency_key="run-1",
                        risk="irreversible",
                    )
                ],
            ),
        ],
    ).save(bundle)


def _provenance(path: Path) -> Path:
    path.write_text(
        ProvenanceFile(
            params={
                "patient_name": ParamProvenance(
                    value="Jane Testcase",
                    source=SourceRef(kind="document", sha256=DOC, line=2, label="P"),
                    extractor="intake:local",
                )
            }
        ).model_dump_json()
    )
    return path


class _RealCli:
    """Run the real ``openadapt-flow`` CLI ``main`` in-process.

    ``before`` runs first (an edit racing the child's start). ``_cmd_replay``
    is wrapped: the real one still runs when ``real_replay`` is set (its
    reload check), otherwise the handoff is captured and nothing actuates.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, before=None) -> None:
        self.before = before
        self.calls: list[list[str]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.rcs: list[int] = []

        def capture(args: Any) -> int:
            authorization = args._governed_run_authorization
            self.handoffs.append(
                {
                    "bundle": Path(args.bundle),
                    "bundle_sha256": bundle_sha256(Path(args.bundle)),
                    "authorized_digest": authorization.bundle_content_digest,
                }
            )
            run_dir = Path(args.run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "report.json").write_text(json.dumps(SUCCESS))
            return 0

        monkeypatch.setattr(cli, "_cmd_replay", capture)

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, **kw):
        if self.before is not None:
            self.before()
        argv = list(cmd[1:])
        self.calls.append(argv)
        rc = cli.main(argv)
        self.rcs.append(rc)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")


def _bridge(tmp: Path, monkeypatch: pytest.MonkeyPatch, before=None):
    bundle = tmp / "bundles" / "intake-write"
    _save(bundle)
    fake = _RealCli(monkeypatch, before)
    monkeypatch.setattr(runner_mod, "_run_flow_process", fake)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow",),
            runs_dir=tmp / "runs",
            timeout_s=30,
            policy="clinical-write",
            extra_run_args=("--approve-unverified-writes", "--allow-unencrypted"),
        ),
        allow_run=True,
    )
    name = next(iter(bridge.catalog))
    grant = bridge.register_intake_provenance(name, _provenance(tmp / "a.json"))
    return bridge, fake, bundle, name, grant


def _run(bridge: AgentBridge, name: str, request_id: str) -> dict[str, Any]:
    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": name,
            "inputs": {"patient": "Jane Testcase"},
            "request_id": request_id,
            "wait_seconds": 30,
        },
    )


def test_stable_admitted_bundle_reaches_consumer_with_the_grant_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, fake, _, name, grant = _bridge(tmp_path, monkeypatch)
    result = _run(bridge, name, "req-stable-cli")
    assert result["outcome"] == "done", (result, fake.rcs)
    assert len(fake.handoffs) == 1
    handoff = fake.handoffs[0]
    assert handoff["bundle_sha256"] == grant.bundle_sha256
    assert handoff["authorized_digest"] == grant.bundle_sha256
    argv = fake.calls[0]
    assert argv[argv.index("--admitted-bundle-sha256") + 1] == grant.bundle_sha256


def test_bundle_edit_after_service_check_never_reaches_cli_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on 900f52669: the CLI admits and authorizes the edited bundle.

    The edit lands after RunService/FlowCliEngine checked the digest, just
    as the CLI child starts. The CLI must consume only the admitted bytes.
    """

    edited: dict[str, Optional[str]] = {"digest": None}

    def edit() -> None:
        _save(live, open_intent="click 'Open another chart'")
        edited["digest"] = bundle_sha256(live)

    live = tmp_path / "bundles" / "intake-write"
    bridge, fake, _, name, grant = _bridge(tmp_path, monkeypatch, before=edit)
    result = _run(bridge, name, "req-edit-cli")
    assert edited["digest"] is not None and edited["digest"] != grant.bundle_sha256
    # The run still happens, on the admitted snapshot, not the edited bytes.
    assert result["outcome"] == "done", (result, fake.rcs)
    assert len(fake.handoffs) == 1
    for handoff in fake.handoffs:
        assert handoff["bundle_sha256"] == grant.bundle_sha256, result
        assert handoff["authorized_digest"] == grant.bundle_sha256, result
        assert handoff["authorized_digest"] != edited["digest"]


def test_bundle_edit_before_snapshot_is_refused_before_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on 900f52669: an edit after the service's check reaches the CLI."""

    live = tmp_path / "bundles" / "intake-write"
    original = FlowRunner.run

    def edit_then_run(self, **kwargs):
        _save(live, open_intent="click 'Open another chart'")
        return original(self, **kwargs)

    monkeypatch.setattr(FlowRunner, "run", edit_then_run)
    bridge, fake, _, name, _ = _bridge(tmp_path, monkeypatch)
    result = _run(bridge, name, "req-edit-pre")
    assert result.get("outcome") == "did_not_run", result
    assert fake.calls == [] and fake.handoffs == []


def _cli_args(bundle: Path, tmp: Path, admitted: str) -> list[str]:
    params = tmp / "params.json"
    params.write_text(json.dumps({"patient": "Jane Testcase"}))
    return [
        "run",
        str(bundle),
        "--run-dir",
        str(tmp / "run"),
        "--params-file",
        str(params),
        "--policy",
        "clinical-write",
        "--approve-unverified-writes",
        "--allow-unencrypted",
        "--admitted-bundle-sha256",
        admitted,
    ]


def test_cli_refuses_a_bundle_other_than_the_admitted_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bundle = tmp_path / "intake-write"
    _save(bundle)
    fake = _RealCli(monkeypatch)
    assert cli.main(_cli_args(bundle, tmp_path, "0" * 64)) == 2
    assert fake.handoffs == []
    assert "intake grant admitted" in capsys.readouterr().out
    # The matching digest goes through the same gate to the handoff.
    assert cli.main(_cli_args(bundle, tmp_path, bundle_sha256(bundle))) == 0
    assert fake.handoffs[0]["authorized_digest"] == bundle_sha256(bundle)


def test_cli_replay_reload_refuses_bytes_changed_after_the_run_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The real replay reload must not run bytes the gate never admitted."""

    bundle = tmp_path / "intake-write"
    _save(bundle)
    admitted = bundle_sha256(bundle)
    original_bind = cli._bind_approval_mode

    def edit_after_gate(args: Any) -> Any:
        _save(bundle, open_intent="click 'Open another chart'")
        return original_bind(args)

    monkeypatch.setattr(cli, "_bind_approval_mode", edit_after_gate)
    built: list[Any] = []
    import openadapt_flow.backends.factory as factory

    monkeypatch.setattr(
        factory, "build_backend", lambda *a, **k: built.append(a) or None
    )
    assert cli.main(_cli_args(bundle, tmp_path, admitted)) == 2
    assert built == []
    assert "intake grant admitted" in capsys.readouterr().out
