"""Qualify a bundle's rollback on a test copy: a stop at every halt point.

``openadapt-flow rollback qualify <bundle> --config test-copy.yaml`` answers,
before production, the question a stop asks: if the run stops here, can Flow
put the system of record back? For each saving step it tries two halt points
on a declared test copy:

- **before the save**: the steps that lead to it ran (a form is filled, maybe
  a draft exists); the save didn't. The whole store must match the read from
  before the run once the earlier saves are compensated. An app that keeps a
  draft on its own shows up here;
- **after the save**: the compensations for this save and every earlier one
  run, latest first, and the store must match the read from before the run.

Each halt point is a reset, a read of the whole store, the steps up to the
halt point, and then :func:`openadapt_flow.rollback.roll_back`: the same
procedure a production stop runs. A saving step with no approved
compensation gets candidates from ``undo propose`` (another recording,
captured traffic, the field's old value); the first that passes its
after-save halt point is written into the bundle as a proposal with this
evidence, and a person approves it once (``undo accept``). A candidate that
fails is reported with the reason and never written.

Test copy only: the same gate as ``induce --probe-on`` and ``undo propose``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import RollbackRecord, RunReport, UndoStep, Workflow
from openadapt_flow.rollback import approved_undo, roll_back
from openadapt_flow.rollback_state import StateReader, StateUnreadable
from openadapt_flow.undo_proposal import (
    Candidate,
    CandidateResult,
    Sources,
    find_candidates,
    saving_steps,
)

REPORT_SCHEMA = "openadapt.rollback-qualification/v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HaltPoint(BaseModel):
    """One simulated stop and what the rollback did about it."""

    step_id: str
    #: ``before_save``: the steps leading to the save ran, the save didn't.
    #: ``after_save``: the save ran too.
    when: Literal["before_save", "after_save"]
    #: The compensation tried for this save (after_save only).
    compensation: Optional[str] = None
    compensation_source: Optional[str] = None
    passed: bool
    reason: str
    rollback: Optional[dict[str, Any]] = None


class QualifyReport(BaseModel):
    schema_: str = Field(default=REPORT_SCHEMA, alias="schema")
    workflow_name: str
    test_copy: str
    scope: str
    halt_points: list[HaltPoint] = Field(default_factory=list)
    #: Saving step -> the compensation that passed (written as a proposal
    #: when it wasn't approved already). Missing: none passed.
    qualified: dict[str, str] = Field(default_factory=dict)
    written: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def summary(self) -> dict[str, int]:
        saves = {p.step_id for p in self.halt_points}
        before = [p for p in self.halt_points if p.when == "before_save"]
        return {
            "saving_steps": len(saves),
            "halt_points": len(self.halt_points),
            "halt_points_rolled_back": sum(1 for p in self.halt_points if p.passed),
            "before_save_clean": sum(1 for p in before if p.passed),
            "saves_with_qualified_compensation": len(self.qualified),
        }


@dataclass
class CopyRunner:
    """Adapts an ``UndoTestCopy`` (fresh session per run) to a runner."""

    target: Any

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, Any],
    ) -> Optional[RunReport]:
        report: Optional[RunReport] = self.target.run(
            workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
        )
        return report


def _prefix(workflow: Workflow, end: int, undos: list[UndoStep]) -> Workflow:
    return workflow.model_copy(
        update={
            "steps": [s.model_copy(deep=True) for s in workflow.steps[:end]],
            "program": None,
            "subflows": {},
            "undo_steps": list(undos),
        }
    )


def _kind_rank(undo: UndoStep) -> int:
    from openadapt_flow.rollback import KIND_ORDER

    return KIND_ORDER.index(undo.kind or "delete")


def _as_approved(undo: UndoStep) -> UndoStep:
    """A candidate counted as approved inside the test copy only."""
    return undo.model_copy(update={"proposal": None})


def halt_point(
    workflow: Workflow,
    *,
    end: int,
    undos: list[UndoStep],
    target: Any,
    reader: StateReader,
    bundle_dir: Path,
    run_dir: Path,
    allow_delete: bool = False,
) -> tuple[bool, str, Optional[RollbackRecord]]:
    """Reset, read, run steps ``[:end]``, roll back; did the store come back?"""
    try:
        target.reset()
        before = reader.read()
    except Exception as exc:  # noqa: BLE001 - nothing ran yet
        return False, f"couldn't reset or read the test copy ({exc})", None
    prefix = _prefix(workflow, end, undos)
    params = dict(workflow.params or {})
    try:
        report = target.run(
            prefix, bundle_dir=bundle_dir, run_dir=run_dir / "run", params=params
        )
    except Exception as exc:  # noqa: BLE001 - recorded as a failed halt point
        report = None
        crash = type(exc).__name__
    else:
        crash = ""
    if report is None:
        return False, f"the steps up to the halt point didn't run ({crash})", None
    if not report.success:
        failed = next((r.step_id for r in report.results if not r.ok), "?")
        return (
            False,
            f"the steps up to the halt point stopped early, at step {failed}",
            None,
        )
    record = roll_back(
        report,
        prefix,
        run_dir=run_dir / "run",
        bundle_dir=bundle_dir,
        reader=reader,
        before=before,
        runner=CopyRunner(target),
        read_records=getattr(target, "read_records", None),
        assume_stopped=True,
        allow_delete=allow_delete,
    )
    assert record is not None
    try:
        target.reset()
    except Exception:  # noqa: BLE001 - every halt point resets first
        pass
    return record.store_proven_unchanged, record.reason, record


def _evidence(
    candidate: Candidate, point: HaltPoint, target_name: str
) -> dict[str, Any]:
    from openadapt_flow.undo_proposal import _sha256

    assert candidate.undo is not None
    return {
        "schema": REPORT_SCHEMA,
        "candidate_id": candidate.candidate_id,
        "undoes": candidate.undoes,
        "source": candidate.source,
        "basis": candidate.basis,
        "test_copy": target_name,
        "undo_digest": _sha256(candidate.undo.model_dump(mode="json")),
        "halt_point": point.model_dump(mode="json"),
        "verdict": "verified",
        "reason": point.reason,
        "finished_at": _now(),
    }


def qualify_rollback(
    workflow: Workflow,
    *,
    bundle_dir: Path,
    target: Any,
    reader: StateReader,
    sources: Sources,
    work_dir: Optional[Path] = None,
    allow_delete: bool = False,
) -> tuple[QualifyReport, dict[str, tuple[Candidate, CandidateResult]]]:
    """Run the halt matrix; return the report and the new compensations that
    passed (for :func:`openadapt_flow.undo_proposal.write_proposals`).

    Candidates are tried least destructive first (discard a draft, restore
    old values, void, delete). A delete is tried only with ``allow_delete``.
    """
    from openadapt_flow.after_stop import _has_loop

    if workflow.program is not None or _has_loop(workflow):
        raise ValueError(
            "rollback qualify works on straight-line bundles without a loop"
        )
    report = QualifyReport(
        workflow_name=workflow.name,
        test_copy=str(getattr(target, "name", "test copy")),
        scope=str(getattr(reader, "scope", "collection")),
    )
    new: dict[str, tuple[Candidate, CandidateResult]] = {}
    own_tmp = work_dir is None
    work = Path(tempfile.mkdtemp(prefix="rollback-qualify-")) if own_tmp else work_dir
    assert work is not None
    # Compensations that pass become usable for the later saves' halt points.
    qualified: list[UndoStep] = [
        u for u in workflow.undo_steps if not u.awaiting_approval
    ]
    try:
        for index, step in saving_steps(workflow):
            slug = f"{index:03d}-{step.id}"
            ok, why, record = halt_point(
                workflow,
                end=index,
                undos=qualified,
                target=target,
                reader=reader,
                bundle_dir=bundle_dir,
                run_dir=work / slug / "before",
                allow_delete=allow_delete,
            )
            report.halt_points.append(
                HaltPoint(
                    step_id=step.id,
                    when="before_save",
                    passed=ok,
                    reason=why,
                    rollback=record.model_dump(mode="json") if record else None,
                )
            )
            approved = approved_undo(workflow, step.id)
            if approved is not None:
                tries: list[tuple[UndoStep, str, Optional[Candidate]]] = [
                    (approved, "approved", None)
                ]
            else:
                candidates, notes = find_candidates(workflow, index, sources)
                built = [c for c in candidates if c.undo is not None]
                built.sort(key=lambda c: _kind_rank(c.undo))  # type: ignore[arg-type]
                if not allow_delete:
                    skipped = [c for c in built if _kind_rank(c.undo) >= 3]  # type: ignore[arg-type]
                    built = [c for c in built if _kind_rank(c.undo) < 3]  # type: ignore[arg-type]
                    if skipped:
                        notes = [
                            *notes,
                            f"{len(skipped)} delete candidate(s) not tried: this "
                            "deployment doesn't allow deletes (rollback.allow_delete)",
                        ]
                tries = [(c.undo, c.source, c) for c in built]  # type: ignore[misc]
                if not tries:
                    report.halt_points.append(
                        HaltPoint(
                            step_id=step.id,
                            when="after_save",
                            passed=False,
                            reason="no compensation to try: "
                            + "; ".join(
                                [c.problem or "" for c in candidates if c.problem]
                                + notes
                            ),
                        )
                    )
                    continue
            for n, (undo, source, candidate) in enumerate(tries):
                stage = work / slug / f"bundle-{n}"
                if stage.exists():
                    shutil.rmtree(stage)
                shutil.copytree(bundle_dir, stage)
                for rel, src in (candidate.assets if candidate else {}).items():
                    (stage / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, stage / rel)
                ok, why, record = halt_point(
                    workflow,
                    end=index + 1,
                    undos=[*qualified, _as_approved(undo)],
                    target=target,
                    reader=reader,
                    bundle_dir=stage,
                    run_dir=work / slug / f"after-{n}",
                    allow_delete=allow_delete,
                )
                point = HaltPoint(
                    step_id=step.id,
                    when="after_save",
                    compensation=undo.id,
                    compensation_source=source,
                    passed=ok,
                    reason=why,
                    rollback=record.model_dump(mode="json") if record else None,
                )
                report.halt_points.append(point)
                if not ok:
                    continue
                report.qualified[step.id] = undo.id
                qualified.append(_as_approved(undo))
                if candidate is not None:
                    new[step.id] = (
                        candidate,
                        CandidateResult(
                            candidate_id=candidate.candidate_id,
                            source=candidate.source,
                            basis=candidate.basis,
                            status="verified",
                            reason=why,
                            evidence=_evidence(candidate, point, report.test_copy),
                        ),
                    )
                break
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
    return report, new


class CopyReader:
    """The test copy's own whole-collection read (its reward contract)."""

    scope = "collection"

    def __init__(self, target: Any) -> None:
        from openadapt_flow.rollback_state import _new_key

        self.target = target
        self.key = _new_key()

    def read(self, before: Any = None) -> Any:
        from openadapt_flow.rollback_state import records_snapshot

        records = self.target.snapshot()
        if records is None:
            raise StateUnreadable("the test copy could not be read")
        return records_snapshot(records, key=self.key)


def reader_for(cfg: Any, target: Any) -> StateReader:
    """The whole-database reader when ``rollback.database`` is set, else the
    test copy's own collection read."""
    from openadapt_flow.rollback_state import DatabaseReader

    section = getattr(cfg, "rollback", None)
    if section is not None and section.database is not None:
        return DatabaseReader.from_frame_database(
            section.database,
            profile=section.profile,
            actor_names=section.actor_names,
        )
    return CopyReader(target)


def report_digest(report: QualifyReport) -> str:
    payload = json.dumps(report.model_dump(mode="json", by_alias=True), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "HaltPoint",
    "QualifyReport",
    "CopyReader",
    "CopyRunner",
    "halt_point",
    "qualify_rollback",
    "reader_for",
]
