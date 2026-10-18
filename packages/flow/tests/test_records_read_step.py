"""READ as a workflow step: format, replay, halts, write gate, zero model calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openadapt_flow.ir import ActionKind, ReadStepSpec, Step, Workflow
from openadapt_flow.records.schema import FieldSpec, GradeGate, ReadProof
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision

CODE = FieldSpec(name="account_number", kind="code")
REGION = (100, 100, 200, 30)


def _read_step(proof: ReadProof = ReadProof(), on_review: str = "halt") -> Step:
    return Step(
        id="step_000",
        intent="read account number",
        action=ActionKind.READ,
        read=ReadStepSpec(
            schema_name="ledger_account",
            field=CODE,
            region=REGION,
            proof=proof,
            on_review=on_review,
        ),
    )


def _vision(text: str) -> FakeVision:
    vision = FakeVision()
    vision.ocr_lines = [SimpleNamespace(text=text, confidence=0.97, region=REGION)]
    return vision


class _Clipboard:
    def __init__(self, backend: FakeBackend, value: str) -> None:
        self.backend, self.value, self.text = backend, value, None

    def write_text(self, text: str) -> None:
        self.text = text

    def read_text(self):
        pressed = [a for a in self.backend.actions if a[0] == "press"]
        return self.value if pressed else self.text


def _run(tmp_path, steps, vision, *, clipboard=None, backend=None):
    backend = backend or FakeBackend()
    if clipboard is not None:
        clipboard = clipboard(backend)
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True, exist_ok=True)
    workflow = Workflow(name="read-wf", steps=steps)
    report = Replayer(backend, vision=vision, read_clipboard=clipboard).run(
        workflow, params={}, bundle_dir=bundle, run_dir=tmp_path / "run"
    )
    return report, backend


def test_read_step_requires_spec_and_is_reversible() -> None:
    with pytest.raises(ValidationError, match="READ step needs"):
        Step(id="s", intent="read", action=ActionKind.READ)
    with pytest.raises(ValidationError, match="only READ steps"):
        Step(id="s", intent="k", action=ActionKind.KEY, key="a", read=_read_step().read)
    with pytest.raises(ValidationError, match="reversible"):
        Step(**{**_read_step().model_dump(), "risk": "irreversible"})


def test_old_bundles_load_unchanged_and_read_steps_round_trip(tmp_path) -> None:
    old = Workflow(
        name="old", steps=[Step(id="s", intent="press", action=ActionKind.KEY, key="a")]
    )
    raw = json.loads(old.model_dump_json())
    for step in raw["steps"]:
        step.pop("read")
        step.pop("read_gate")
    assert Workflow.model_validate(raw).steps[0].read is None
    new = Workflow(name="new", steps=[_read_step()])
    again = Workflow.model_validate_json(new.model_dump_json())
    assert again.steps[0].read == new.steps[0].read


def test_default_read_is_one_channel_unchecked_no_model_calls(tmp_path) -> None:
    report, backend = _run(tmp_path, [_read_step()], _vision("AC-1042"))
    assert report.success, [r.error for r in report.results]
    read = report.results[0].field_read
    assert read is not None
    assert read.value == "AC-1042"
    assert read.grade == "unchecked"
    assert [r.channel for r in read.reads] == ["ocr"]
    assert read.seal is None
    assert read.region is not None and read.region.bbox == REGION
    assert backend.actions == []  # an OCR-only read sends no input
    assert report.model_calls == 0


def test_double_read_with_seal_reaches_same_screen(tmp_path) -> None:
    proof = ReadProof(double_read=True, seal=True)
    report, backend = _run(
        tmp_path,
        [_read_step(proof)],
        _vision("AC-1042"),
        clipboard=lambda b: _Clipboard(b, "AC-1042"),
    )
    assert report.success, [r.error for r in report.results]
    read = report.results[0].field_read
    assert read.grade == "same_screen"
    assert [r.channel for r in read.reads] == ["clipboard", "ocr"]
    assert read.seal is not None and read.seal.run_id
    assert ("click", 200, 115, False) in backend.actions
    assert report.model_calls == 0


def test_disagreement_halts_by_default(tmp_path) -> None:
    proof = ReadProof(double_read=True)
    report, _ = _run(
        tmp_path,
        [_read_step(proof)],
        _vision("AC-1043"),
        clipboard=lambda b: _Clipboard(b, "AC-1042"),
    )
    assert not report.success
    (result,) = report.results
    assert "needs review" in (result.error or "")
    assert result.field_read is not None and result.field_read.needs_review


def test_on_review_continue_records_and_goes_on(tmp_path) -> None:
    proof = ReadProof(double_read=True)
    report, _ = _run(
        tmp_path,
        [_read_step(proof, on_review="continue")],
        _vision("AC-1043"),
        clipboard=lambda b: _Clipboard(b, "AC-1042"),
    )
    assert report.success
    assert report.results[0].field_read.needs_review


def test_double_read_without_clipboard_halts(tmp_path) -> None:
    report, _ = _run(tmp_path, [_read_step(ReadProof(double_read=True))], _vision("x"))
    assert not report.success
    assert "no clipboard channel" in (report.results[0].error or "")


def _write_step(floor: str) -> Step:
    return Step(
        id="step_001",
        intent="press Enter to save",
        action=ActionKind.KEY,
        key="Enter",
        read_gate=GradeGate(min_grade=floor, fields=("account_number",)),
    )


def test_write_gate_halts_below_floor_before_acting(tmp_path) -> None:
    report, backend = _run(
        tmp_path, [_read_step(), _write_step("same_screen")], _vision("AC-1042")
    )
    assert not report.success
    assert "below it: account_number (unchecked)" in (report.results[-1].error or "")
    assert ("press", "Enter") not in backend.actions


def test_write_gate_passes_at_floor(tmp_path) -> None:
    report, backend = _run(
        tmp_path,
        [_read_step(ReadProof(double_read=True)), _write_step("same_screen")],
        _vision("AC-1042"),
        clipboard=lambda b: _Clipboard(b, "AC-1042"),
    )
    assert report.success, [r.error for r in report.results]
    assert ("press", "Enter") in backend.actions


def test_write_gate_halts_when_field_never_read(tmp_path) -> None:
    report, _ = _run(tmp_path, [_write_step("unchecked")], _vision("x"))
    assert not report.success
    assert "account_number" in (report.results[-1].error or "")


def test_recorded_read_compiles_to_read_step_and_replays(tmp_path) -> None:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler.codegen import render_workflow_py
    from openadapt_flow.recorder import Recorder

    backend = FakeBackend()
    recorder = Recorder(backend, tmp_path / "rec")
    recorder.mark_read(
        CODE,
        REGION,
        schema_name="ledger_account",
        proof=ReadProof(double_read=True),
        label_region=(20, 100, 70, 30),
        label_text="Account",
    )
    rec_dir = recorder.finish()
    workflow = compile_recording(rec_dir, tmp_path / "bundle", name="read-wf")
    (step,) = workflow.steps
    assert step.action is ActionKind.READ
    assert step.read is not None and step.read.field == CODE
    assert step.read.proof.double_read
    assert step.anchor is not None and step.anchor.ocr_text == "Account"
    assert step.expect == []
    assert "flow.read('code'" in render_workflow_py(workflow)
    again = Workflow.load(tmp_path / "bundle")
    assert again.steps[0].read == step.read
    # Replay: the label moved 10 px left and 20 px down; the read follows it.
    from tests.test_replayer import Match

    vision = _vision("AC-1042")
    vision.template_results = [
        Match(point=(45, 135), region=(10, 120, 70, 30), confidence=0.95)
    ]
    clip_backend = FakeBackend()
    report = Replayer(
        clip_backend, vision=vision, read_clipboard=_Clipboard(clip_backend, "AC-1042")
    ).run(again, params={}, bundle_dir=tmp_path / "bundle", run_dir=tmp_path / "run")
    assert report.success, [r.error for r in report.results]
    read = report.results[0].field_read
    assert read.region.bbox == (90, 120, 200, 30)
    assert read.grade == "same_screen"
    assert report.model_calls == 0


def test_seal_pointers_come_from_the_run_receipt(tmp_path) -> None:
    import hashlib

    from openadapt_flow.records.run import (
        field_reads_from_report,
        record_from_report,
        seal_record,
        write_sealed_reads,
    )
    from openadapt_flow.records.schema import RecordSchema

    proof = ReadProof(double_read=True, seal=True)
    report, _ = _run(
        tmp_path,
        [_read_step(proof)],
        _vision("AC-1042"),
        clipboard=lambda b: _Clipboard(b, "AC-1042"),
    )
    receipt = tmp_path / "receipt" / "receipt.json"
    receipt.parent.mkdir()
    receipt.write_bytes(b'{"schema_version": "openadapt.run-receipt/v2"}\n')
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()

    schema = RecordSchema(
        name="ledger_account", fields=(CODE,), identity_fields=("account_number",)
    )
    record = seal_record(record_from_report(report, schema), receipt)
    assert record.record_key == "AC-1042"
    seal = record.fields[0].seal
    assert seal is not None and seal.sealed and seal.receipt_sha256 == digest
    assert seal.field_commitment is None  # the run receipt does not commit per value

    out = write_sealed_reads(report, receipt)
    assert (
        out is not None
        and json.loads(out.read_text())[0]["seal"]["receipt_sha256"] == digest
    )
    # Unsealed reads stay unsealed.
    plain, _ = _run(tmp_path / "plain", [_read_step()], _vision("AC-1042"))
    assert field_reads_from_report(plain)[0].seal is None
