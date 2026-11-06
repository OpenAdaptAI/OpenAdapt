"""#1890 on the real MCP path: run_workflow and the legacy run_<id> tool.

These go through ``AgentBridge.dispatch`` with a real bundle that declares
``intake_field``. Only the Flow CLI process is stubbed, and the stub records
the params file it was given, so the tests see the values a run would use.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import openadapt_flow.agent.runner as runner_mod
from openadapt_flow.agent.bridge import AgentBridge, BridgeError
from openadapt_flow.agent.runner import RunnerConfig
from openadapt_flow.ir import ActionKind, ParamKind, ParamSpec, Step, Workflow
from openadapt_flow.provenance import (
    ParamProvenance,
    ProvenanceFile,
    SourceRef,
    sha256_bytes,
)

DOC_A = sha256_bytes(b"referral A")
DOC_B = sha256_bytes(b"referral B")

SUCCESS = {
    "workflow_name": "Intake Note",
    "success": True,
    "execution_outcome": "VERIFIED",
    "transaction_outcome": "VERIFIED",
    "execution_profile": "standard",
    "production_eligible": True,
    "results": [{"step_id": "s1", "intent": "Type the patient", "ok": True}],
    "model_calls": 0,
    "total_ms": 10.0,
}


class _Stub:
    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, **kw):
        if "--params-file" in cmd:
            path = Path(cmd[cmd.index("--params-file") + 1])
            self.params.append(json.loads(path.read_text()))
        if "--run-dir" in cmd:
            run_dir = Path(cmd[cmd.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "report.json").write_text(json.dumps(SUCCESS))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _save_bundle(bundle: Path, intent: str = "Type the patient") -> None:
    Workflow(
        name="Intake Note",
        params={"patient": "Recorded Person"},
        param_specs={
            "patient": ParamSpec(
                name="patient", type=ParamKind.STRING, intake_field="patient_name"
            )
        },
        steps=[
            Step(
                id="s1",
                intent=intent,
                action=ActionKind.TYPE,
                text="Recorded Person",
                param="patient",
            )
        ],
    ).save(bundle)


def _provenance(path: Path, value: str, doc: str = DOC_A) -> Path:
    path.write_text(
        ProvenanceFile(
            params={
                "patient_name": ParamProvenance(
                    value=value,
                    source=SourceRef(
                        kind="document", sha256=doc, line=2, label="Patient"
                    ),
                    extractor="intake:local",
                )
            }
        ).model_dump_json()
    )
    return path


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundle = tmp_path / "bundles" / "intake-note"
    _save_bundle(bundle)
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp_path / "runs", timeout_s=5
        ),
        allow_run=True,
    )
    return bridge, stub, bundle, tmp_path


def _name(bridge: AgentBridge) -> str:
    return next(iter(bridge.catalog))


def _run(bridge: AgentBridge, patient: str, request_id: str) -> dict[str, Any]:
    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": _name(bridge),
            "inputs": {"patient": patient},
            "request_id": request_id,
            "wait_seconds": 10,
        },
    )


def _refused(result: dict[str, Any]) -> bool:
    """Refused by the intake gate before anything ran."""

    return result.get("outcome") == "did_not_run" and any(
        item.get("input") == "(intake)" for item in result.get("invalid_inputs", [])
    )


def test_no_grant_refuses_an_intake_bound_run(setup) -> None:
    bridge, stub, _, _ = setup
    result = _run(bridge, "Jane Testcase", "req-0001")
    assert _refused(result), result
    assert stub.params == []
    assert "Jane" not in json.dumps(result)


def test_steered_value_is_refused_before_anything_runs(setup) -> None:
    bridge, stub, _, tmp = setup
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    result = _run(bridge, "Other Person", "req-0002")
    assert _refused(result), result
    assert stub.params == []
    assert "Other Person" not in json.dumps(result)


def test_run_uses_the_proven_value_not_the_callers_spelling(setup) -> None:
    bridge, stub, _, tmp = setup
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    result = _run(bridge, "JANE TESTCASE", "req-0003")
    assert result["outcome"] == "done", result
    assert stub.params == [{"patient": "Jane Testcase"}]


def test_a_grant_is_used_once_and_cannot_be_replayed_for_another_document(
    setup,
) -> None:
    # Referral A's grant must not let a later call, steered by referral B,
    # write to A's patient.
    bridge, stub, _, tmp = setup
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    first = _run(bridge, "Jane Testcase", "req-a-001")
    assert first["outcome"] == "done", first
    again = _run(bridge, "Jane Testcase", "req-a-001")
    assert again["run_id"] == first["run_id"] and again["replayed"] is True
    second = _run(bridge, "Jane Testcase", "req-b-001")
    assert _refused(second), second
    assert len(stub.params) == 1
    # A new document needs a new registration.
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "b.json", "John Sample", DOC_B)
    )
    assert _run(bridge, "John Sample", "req-b-002")["outcome"] == "done"
    assert stub.params[-1] == {"patient": "John Sample"}


def test_registration_can_require_the_named_document(setup) -> None:
    bridge, _, _, tmp = setup
    with pytest.raises(BridgeError):
        bridge.register_intake_provenance(
            _name(bridge),
            _provenance(tmp / "a.json", "Jane Testcase"),
            document_sha256=DOC_B,
        )


def test_an_expired_grant_is_refused(setup) -> None:
    bridge, stub, _, tmp = setup
    grant = bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    grant.expires_at = 0.0
    assert _refused(_run(bridge, "Jane Testcase", "req-0004"))
    assert stub.params == []


def test_a_bundle_changed_after_registration_is_refused(setup) -> None:
    bridge, stub, bundle, tmp = setup
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    _save_bundle(bundle, intent="Type the patient into another field")
    assert _refused(_run(bridge, "Jane Testcase", "req-0005"))
    assert stub.params == []


def test_unloadable_specs_with_a_grant_fail_closed(setup) -> None:
    bridge, stub, bundle, tmp = setup
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    (bundle / "workflow.json").write_text("{not json")
    assert _refused(_run(bridge, "Jane Testcase", "req-0006"))
    assert stub.params == []


def test_legacy_run_tool_is_gated_and_single_use(setup) -> None:
    bridge, stub, _, tmp = setup
    legacy = next(iter(bridge.workflows))
    with pytest.raises(BridgeError):
        bridge.dispatch(f"run_{legacy}", {"patient": "Jane Testcase"})
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp / "a.json", "Jane Testcase")
    )
    with pytest.raises(BridgeError):
        bridge.dispatch(f"run_{legacy}", {"patient": "Other Person"})
    bridge.dispatch(f"run_{legacy}", {"patient": "jane testcase"})
    assert stub.params == [{"patient": "Jane Testcase"}]
    with pytest.raises(BridgeError):
        bridge.dispatch(f"run_{legacy}", {"patient": "Jane Testcase"})
    assert len(stub.params) == 1


def test_registration_for_a_taught_over_mcp_entry_is_refused(setup) -> None:
    bridge, _, _, tmp = setup
    entry = bridge.catalog[_name(bridge)]
    bridge.catalog["taught_over_mcp"] = dataclasses.replace(entry, info=None)
    with pytest.raises(BridgeError, match="taught over MCP"):
        bridge.register_intake_provenance(
            "taught_over_mcp", _provenance(tmp / "a.json", "Jane Testcase")
        )


# -- admitted bundle identity through dispatch (#1978 Codex BLOCK) ------------


class _FlipDesk:
    """Return one bundle for register/gate, another for execute."""

    def __init__(self, admitted: Path, switched: Path, *, admit_calls: int = 2) -> None:
        self.admitted = Path(admitted)
        self.switched = Path(switched)
        self.admit_calls = admit_calls
        self.calls = 0

    def active_bundle(self, workflow: str, default: Path) -> Path:
        self.calls += 1
        if self.calls <= self.admit_calls:
            return self.admitted
        return self.switched


def _bridge_with_desk(tmp: Path, desk: Any, monkeypatch: pytest.MonkeyPatch):
    bundle = tmp / "bundles" / "intake-note"
    _save_bundle(bundle)
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp / "runs", timeout_s=5
        ),
        allow_run=True,
        repair_desk=desk,
    )
    return bridge, stub, bundle


def test_repair_switched_after_gate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair (or other active bundle) switched after the gate must not run.

    Fails on 63783926f: execute re-resolves the active repair and reaches the
    CLI with bytes the grant never admitted.
    """

    admitted = tmp_path / "admitted"
    switched = tmp_path / "switched"
    _save_bundle(admitted)
    _save_bundle(switched, intent="Type the patient into another field")
    # Put the catalog bundle aside; the desk supplies the real paths.
    catalog = tmp_path / "bundles" / "intake-note"
    catalog.parent.mkdir(parents=True)
    shutil.copytree(admitted, catalog)
    desk = _FlipDesk(admitted, switched, admit_calls=2)
    bridge, stub, _ = _bridge_with_desk(tmp_path, desk, monkeypatch)
    # Force the catalog entry's bundle_dir to admitted so register/gate hash it
    # when the desk returns admitted.
    entry = bridge.catalog[_name(bridge)]
    entry.info.bundle_dir = admitted
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp_path / "a.json", "Jane Testcase")
    )
    result = _run(bridge, "Jane Testcase", "req-flip-001")
    assert result.get("outcome") == "did_not_run", result
    assert stub.params == []
    assert desk.calls >= 3


def test_bundle_bytes_changed_after_gate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing the active bundle after the gate must not reach the CLI."""

    bundle = tmp_path / "bundles" / "intake-note"
    _save_bundle(bundle)
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp_path / "runs", timeout_s=5
        ),
        allow_run=True,
    )
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp_path / "a.json", "Jane Testcase")
    )
    original_start = bridge.service._start

    def mutate_then_start(*args, **kwargs):
        _save_bundle(bundle, intent="Type the patient into another field")
        return original_start(*args, **kwargs)

    monkeypatch.setattr(bridge.service, "_start", mutate_then_start)
    result = _run(bridge, "Jane Testcase", "req-mutate-001")
    assert result.get("outcome") == "did_not_run", result
    assert stub.params == []


def test_stable_repaired_bundle_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repaired bundle that stays active through dispatch still runs."""

    repaired = tmp_path / "repaired"
    _save_bundle(repaired)
    catalog = tmp_path / "bundles" / "intake-note"
    catalog.parent.mkdir(parents=True)
    shutil.copytree(repaired, catalog)

    class _StableDesk:
        def active_bundle(self, workflow: str, default: Path) -> Path:
            return repaired

    bridge, stub, _ = _bridge_with_desk(tmp_path, _StableDesk(), monkeypatch)
    entry = bridge.catalog[_name(bridge)]
    entry.info.bundle_dir = repaired
    bridge.register_intake_provenance(
        _name(bridge), _provenance(tmp_path / "a.json", "Jane Testcase")
    )
    result = _run(bridge, "Jane Testcase", "req-stable-001")
    assert result["outcome"] == "done", result
    assert stub.params == [{"patient": "Jane Testcase"}]


def _save_typed_bundle(
    bundle: Path, *, kind: ParamKind, param: str, intake: str
) -> None:
    # IR params are strings; ParamSpec carries the declared card type.
    default = (
        "0"
        if kind is ParamKind.NUMBER
        else ("false" if kind is ParamKind.BOOLEAN else "x")
    )
    Workflow(
        name="Intake Typed",
        params={param: default},
        param_specs={param: ParamSpec(name=param, type=kind, intake_field=intake)},
        steps=[
            Step(
                id="s1",
                intent="Fill the field",
                action=ActionKind.TYPE,
                text=default,
                param=param,
            )
        ],
    ).save(bundle)


def _provenance_field(path: Path, field: str, value: str, doc: str = DOC_A) -> Path:
    path.write_text(
        ProvenanceFile(
            params={
                field: ParamProvenance(
                    value=value,
                    source=SourceRef(kind="document", sha256=doc, line=2, label=field),
                    extractor="intake:local",
                )
            }
        ).model_dump_json()
    )
    return path


def test_numeric_proven_value_keeps_declared_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance strings must not demote a valid number input to text.

    Fails on 63783926f: applying the proven string makes the card schema
    refuse a run whose caller already matched the document as a number.
    """

    bundle = tmp_path / "bundles" / "amount-note"
    _save_typed_bundle(
        bundle, kind=ParamKind.NUMBER, param="amount", intake="amount_due"
    )
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp_path / "runs", timeout_s=5
        ),
        allow_run=True,
    )
    name = _name(bridge)
    bridge.register_intake_provenance(
        name, _provenance_field(tmp_path / "a.json", "amount_due", "42")
    )
    result = bridge.dispatch(
        "run_workflow",
        {
            "workflow": name,
            "inputs": {"amount": 42},
            "request_id": "req-num-001",
            "wait_seconds": 10,
        },
    )
    assert result["outcome"] == "done", result
    assert stub.params == [{"amount": "42"}]


def test_boolean_proven_value_keeps_declared_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance strings must not demote a valid boolean input to text."""

    bundle = tmp_path / "bundles" / "flag-note"
    _save_typed_bundle(
        bundle, kind=ParamKind.BOOLEAN, param="urgent", intake="is_urgent"
    )
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp_path / "runs", timeout_s=5
        ),
        allow_run=True,
    )
    name = _name(bridge)
    bridge.register_intake_provenance(
        name, _provenance_field(tmp_path / "a.json", "is_urgent", "true")
    )
    result = bridge.dispatch(
        "run_workflow",
        {
            "workflow": name,
            "inputs": {"urgent": True},
            "request_id": "req-bool-001",
            "wait_seconds": 10,
        },
    )
    assert result["outcome"] == "done", result
    assert stub.params == [{"urgent": "true"}]


def test_mismatched_numeric_caller_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller number that disagrees with provenance is still refused."""

    bundle = tmp_path / "bundles" / "amount-note"
    _save_typed_bundle(
        bundle, kind=ParamKind.NUMBER, param="amount", intake="amount_due"
    )
    stub = _Stub()
    monkeypatch.setattr(runner_mod, "_run_flow_process", stub)
    bridge = AgentBridge(
        bundle.parent,
        RunnerConfig(
            flow_cli=("openadapt-flow-stub",), runs_dir=tmp_path / "runs", timeout_s=5
        ),
        allow_run=True,
    )
    name = _name(bridge)
    bridge.register_intake_provenance(
        name, _provenance_field(tmp_path / "a.json", "amount_due", "42")
    )
    result = bridge.dispatch(
        "run_workflow",
        {
            "workflow": name,
            "inputs": {"amount": 99},
            "request_id": "req-num-bad",
            "wait_seconds": 10,
        },
    )
    assert _refused(result), result
    assert stub.params == []
