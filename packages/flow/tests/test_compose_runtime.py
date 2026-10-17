"""Fail-closed sequencing of composed child bundles."""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import Composition, HandoffBinding
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.composition import (
    ChildRunResult,
    execute_composition,
)
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests.test_loop_authoring import (
    _encounter_body,
    _RowWritingBackend,
    _vision_confirms_saved,
)
from tests.test_replayer import FakeBackend, FakeVision


def _writer() -> Workflow:
    return _encounter_body()


def _local_reader() -> Workflow:
    """Second child: a local/mock backend that consumes patient_id."""

    return Workflow(
        name="post-note",
        surface="linux",
        steps=[
            Step(
                id="type_patient",
                intent="type handed-off patient_id",
                action=ActionKind.TYPE,
                param="patient_id",
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="unset")},
    )


def _save(tmp_path: Path, workflow: Workflow, folder: str) -> Path:
    path = tmp_path / folder
    path.mkdir()
    workflow.save(path)
    return path


def _compose_two(tmp_path: Path) -> tuple[Composition, Path]:
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _local_reader(), "posting")
    out = tmp_path / "composed"
    composition = author_composition(
        [("intake", intake), ("posting", posting)],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        name="two-child",
        out=out,
    )
    return composition, out


def test_missing_handoff_evidence_halts(tmp_path: Path):
    composition, parent = _compose_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            # VERIFIED but the effect fact is missing -> successor HALTs.
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={},
                effect_facts={},
                success=True,
            )
        raise AssertionError("posting must not start without handoff evidence")

    report = execute_composition(
        composition,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "posting"
    assert "missing handoff evidence" in report.reason
    assert [item.child for item in report.children] == ["intake"]


def test_unverified_predecessor_halts_before_next_child(tmp_path: Path):
    composition, parent = _compose_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                success=False,
            )
        raise AssertionError("posting must not start after an unverified predecessor")

    report = execute_composition(
        composition,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "posting"
    assert "ended HALTED" in report.reason


def test_allowed_halt_class_may_start_successor_without_handoff(tmp_path: Path):
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _local_reader(), "posting")
    out = tmp_path / "composed"
    composition = author_composition(
        [("intake", intake), ("posting", posting)],
        allowed_halt_classes={"posting": ["HALTED"]},
        out=out,
    )
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={},
                effect_facts={},
                success=False,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_composition(
        composition,
        parent_dir=out,
        run_dir=tmp_path / "run",
        child_run=child_run,
    )
    assert seen == ["intake", "posting"]
    # No handoff was required, so the allowed halt lets posting run. The
    # parent is not VERIFIED because intake halted.
    assert report.outcome != "VERIFIED"
    assert [item.child for item in report.children] == ["intake", "posting"]


def test_scripted_verified_handoff_runs_second_child(tmp_path: Path):
    composition, parent = _compose_two(tmp_path)
    received: dict[str, str] = {}

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice", "note": "triage"},
                effect_facts={"patient_id": "alice"},
                success=True,
            )
        received.update(inputs)
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_composition(
        composition,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        inputs={"note": "triage"},
    )
    assert report.outcome == "VERIFIED"
    assert report.model_calls == 0
    assert received["patient_id"] == "alice"
    assert received["note"] == "triage"
    assert (tmp_path / "run" / "composition-report.json").is_file()


def test_two_child_fixture_second_is_local_backend(tmp_path: Path):
    """Intake writes through MockMed; posting is a local FakeBackend.

    The parent copies the verified patient_id from intake's confirmed
    record_written contract into posting. Nothing guesses a window or URL.
    """

    composition, parent = _compose_two(tmp_path)
    url, db, stop = fault_serve()
    typed_on_posting: list[str] = []
    try:

        def child_run(
            capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
        ):
            params = {str(k): str(v) for k, v in inputs.items()}
            if child == "intake":
                backend = _RowWritingBackend(url)
                report = Replayer(
                    backend,
                    vision=_vision_confirms_saved(),
                    effect_verifier=RestRecordVerifier(url),
                    poll_interval_s=0.01,
                ).run(
                    workflow,
                    params=params,
                    bundle_dir=bundle_dir,
                    run_dir=run_dir,
                )
                outcome = (
                    "VERIFIED"
                    if report.success
                    and all(
                        result.effect_verified is True
                        for result in report.results
                        if result.step_id == "save"
                    )
                    else "HALTED"
                )
                return ChildRunResult(
                    child=child,
                    outcome=outcome,
                    bound_params=params,
                    effect_facts=params,
                    model_calls=report.model_calls,
                    success=report.success,
                )

            backend = FakeBackend(viewport=(300, 200))

            def type_text(text: str) -> None:
                FakeBackend.type_text(backend, text)
                typed_on_posting.append(text)

            backend.type_text = type_text  # type: ignore[method-assign]
            report = Replayer(
                backend,
                vision=FakeVision(),
                poll_interval_s=0.0,
            ).run(
                workflow,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
            )
            return ChildRunResult(
                child=child,
                outcome="VERIFIED" if report.success else "HALTED",
                bound_params=params,
                effect_facts={},
                model_calls=report.model_calls,
                success=report.success,
            )

        report = execute_composition(
            composition,
            parent_dir=parent,
            run_dir=tmp_path / "run",
            child_run=child_run,
            inputs={"patient_id": "alice", "note": "alice triage"},
        )
        assert report.outcome == "VERIFIED"
        assert report.model_calls == 0
        assert typed_on_posting == ["alice"]
        written = {rec["patient_id"] for rec in db.snapshot()["records"]}
        assert written == {"alice"}
    finally:
        stop()
