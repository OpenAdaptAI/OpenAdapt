"""After a stop: the change ledger, write-ordering checks, and declared undo.

A stop isn't a rollback. A run can confirm a write at step 3 and then stop at
step 7, leaving the app half-changed. This module gives a person three things
for that case (``docs/AFTER_A_STOP.md``):

1. **Change ledger.** :func:`build_change_ledger` lists, in run order, each
   step that could have changed the app, its declared effects, and whether
   each was confirmed, refuted or unknown in the system of record. The halt
   message, ``explain`` and ``REPORT.md`` read it to say what may need
   undoing.
2. **Write-ordering check.** :func:`write_ordering_findings` finds a write
   that comes before a right-record check or a halting precondition the
   program could have done first. ``lint`` reports it with a suggested
   order. Nothing is reordered.
3. **Declared undo.** A bundle can declare a compensating step for an effect
   (:class:`~openadapt_flow.ir.UndoStep`), written by hand or compiled from a
   demonstration. After a stop, :func:`offer_undos` offers it to a person.
   It runs only after :func:`approve_undo` records that person's approval,
   at most once, and :func:`finish_undo_run` checks the undo's own declared
   effects in the system of record before calling it done.

Flow never runs an undo on its own: the replayer has no path to this module.
PHI: the ledger and the undo records carry step ids, author text, effect
kinds and contract hashes, never selector or read-back values.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    ChangeLedgerEffect,
    ChangeLedgerEntry,
    RunReport,
    Step,
    StepResult,
    UndoStep,
    Workflow,
)

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.runtime.effects.effect import Effect

UNDO_DIRNAME = "undo"
APPROVAL_FILENAME = "approval.json"
CLAIM_FILENAME = "claim.json"
RESULT_FILENAME = "result.json"
UNDO_RUN_DIRNAME = "run"

LedgerStatus = Literal["confirmed", "refuted", "unknown"]


# ---------------------------------------------------------------------------
# 1. Change ledger
# ---------------------------------------------------------------------------


def _effect_status(final_verdict: str) -> LedgerStatus:
    if final_verdict == "confirmed":
        return "confirmed"
    if final_verdict == "refuted":
        return "refuted"
    return "unknown"


def _declared_effects(step: Optional[Step]) -> list["Effect"]:
    if step is None:
        return []
    if step.effects:
        return list(step.effects)
    if step.api_binding is not None:
        return list(step.api_binding.effects)
    return []


def _effect_description(effect: "Effect") -> str:
    kind = effect.kind.value
    return f"{kind}: {effect.probe}" if effect.probe else kind


def _ledger_effects(
    result: StepResult, declared: list["Effect"]
) -> list[ChangeLedgerEffect]:
    """Pair each declared effect with the verifier evidence for it.

    The runtime verifies a step's effects in declaration order and records one
    evidence item per verified effect, so the n-th evidence item belongs to
    the n-th declared effect. An effect with no evidence (no verifier, or the
    run stopped before checking it) is ``unknown``.
    """
    evidence = list(result.effect_evidence)
    out: list[ChangeLedgerEffect] = []
    for index in range(max(len(declared), len(evidence))):
        effect = declared[index] if index < len(declared) else None
        item = evidence[index] if index < len(evidence) else None
        kind = effect.kind.value if effect is not None else "effect"
        description = (
            _effect_description(effect)
            if effect is not None
            else f"{item.substrate} effect"
            if item is not None
            else kind
        )
        out.append(
            ChangeLedgerEffect(
                kind=kind,
                description=description,
                contract_hash=(
                    item.effect_contract_hash
                    if item is not None
                    else (
                        result.effect_contract_hashes[index]
                        if index < len(result.effect_contract_hashes)
                        else ""
                    )
                ),
                status=_effect_status(item.final_verdict) if item else "unknown",
                observed_effect=item.observed_effect if item else "unknown",
            )
        )
    return out


def _step_status(effects: list[ChangeLedgerEffect]) -> LedgerStatus:
    if not effects or any(e.status == "unknown" for e in effects):
        return "unknown"
    if any(e.status == "refuted" for e in effects):
        return "refuted"
    return "confirmed"


def build_change_ledger(
    report: RunReport, workflow: Workflow
) -> list[ChangeLedgerEntry]:
    """List, in run order, each step that could have changed the app.

    A step is listed when it is consequential (or declares an effect) and the
    run did not positively record that it stopped before delivering the
    action. ``may_need_undo`` is False only when the system of record proved
    that nothing the step declared landed (every effect read ``absent``).
    """
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.transaction import _attempt_state, _is_consequential_result
    from openadapt_flow.traversal import iter_workflow_steps

    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    undo_by_target: dict[str, str] = {}
    for undo in workflow.undo_steps:
        undo_by_target.setdefault(undo.undoes, undo.id)

    ledger: list[ChangeLedgerEntry] = []
    position = 0
    for result in report.results:
        if result.skipped:
            continue
        position += 1
        step = steps_by_id.get(result.step_id)
        declared = _declared_effects(step)
        could_write = (
            bool(declared)
            or _is_consequential_result(result)
            or (step is not None and is_consequential(step, workflow))
        )
        if not could_write:
            continue
        attempt = _attempt_state(result)
        if attempt == "not_actuated":
            continue
        effects = _ledger_effects(result, declared)
        may_need_undo = not effects or any(
            e.observed_effect != "absent" for e in effects
        )
        ledger.append(
            ChangeLedgerEntry(
                position=position,
                step_id=result.step_id,
                intent=result.intent,
                attempt_state=attempt,
                status=_step_status(effects),
                effects=effects,
                may_need_undo=may_need_undo,
                undo_id=undo_by_target.get(result.step_id),
            )
        )
    return ledger


_STATUS_WORDS = {
    "confirmed": "confirmed in the system of record",
    "refuted": "refuted by the system of record",
    "unknown": "not confirmed (unknown)",
}


def change_ledger_lines(
    report: RunReport,
    *,
    undo_hint: Optional[str] = None,
) -> list[str]:
    """Plain-language lines naming what may need undoing after a stop.

    Empty when the run finished or nothing could have changed the app.
    ``undo_hint`` is the command a person runs to see the offered undos.
    """
    if report.success or not report.change_ledger:
        return []
    lines = ["Changes before the stop (in order):"]
    repeated: dict[str, int] = {}
    for entry in report.change_ledger:
        repeated[entry.step_id] = repeated.get(entry.step_id, 0) + 1
    needs = [entry for entry in report.change_ledger if entry.may_need_undo]
    for entry in report.change_ledger:
        marker = "may need undoing" if entry.may_need_undo else "nothing landed"
        lines.append(
            f"  {entry.position}. step `{entry.step_id}` ({entry.intent}): "
            f"{_STATUS_WORDS[entry.status]}; {marker}"
        )
        for effect in entry.effects:
            lines.append(f"       - {effect.description}: {effect.status}")
        if entry.may_need_undo and entry.undo_id:
            if repeated[entry.step_id] > 1:
                lines.append(
                    f"       declared undo `{entry.undo_id}` is not offered: "
                    "the step ran more than once"
                )
            else:
                lines.append(
                    f"       declared undo: `{entry.undo_id}` (needs a person)"
                )
    if not needs:
        lines.append("The system of record shows none of these changes landed.")
        return lines
    with_undo = [entry.step_id for entry in needs if entry.undo_id]
    without_undo = [entry.step_id for entry in needs if not entry.undo_id]
    if with_undo:
        lines.append(
            "Flow never runs an undo on its own. A person must review and "
            "approve each one" + (f": {undo_hint}" if undo_hint else ".")
        )
    if without_undo:
        lines.append(
            f"No undo is declared for step(s) {', '.join(without_undo)}. Check "
            "and repair them in the application by hand before anyone runs "
            "this again."
        )
    return lines


# ---------------------------------------------------------------------------
# 2. Write-ordering check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderingFinding:
    """One write that comes before a check the program could have done first."""

    code: Literal["write_before_identity_check", "write_before_check"]
    step_id: str
    check_step_id: str
    message: str
    suggested_order: list[str] = field(default_factory=list)


def _is_halting_check(step: Step) -> bool:
    return step.guard is not None and step.guard.on_unmet == "halt"


def _suggest(
    ids: list[str], first_write: int, block_start: int, check: int
) -> list[str]:
    """Move ``ids[block_start..check]`` ahead of ``ids[first_write]``."""
    return (
        ids[:first_write]
        + ids[block_start : check + 1]
        + ids[first_write:block_start]
        + ids[check + 1 :]
    )


def write_ordering_findings(
    workflow: Workflow, consequential_ids: set[str]
) -> list[OrderingFinding]:
    """Find writes that run before a check the program could have done first.

    Straight-line bundles only (``workflow.program is None``); a program graph
    has no single order to suggest. Two rules:

    - ``write_before_identity_check``: a write runs before ANY right-record
      check, while a later read-only step does check the record. The record
      is first confirmed after the app has already changed.
    - ``write_before_check``: a later read-only step can stop the run (a
      halting ``guard``, or a right-record check that protects a later
      write) after an earlier write already landed.

    Each finding suggests moving the check, with the navigation steps that
    lead to it since the last write, ahead of the first write. It is advice:
    the check may depend on the write, and moving it may need extra
    navigation. Nothing is reordered.
    """
    from openadapt_flow.policy import is_identity_armed

    if workflow.program is not None:
        return []
    steps = list(workflow.steps)
    ids = [step.id for step in steps]
    writes = [i for i, step in enumerate(steps) if step.id in consequential_ids]
    if not writes:
        return []
    armed = [is_identity_armed(step) for step in steps]
    findings: list[OrderingFinding] = []
    flagged_checks: set[int] = set()

    first_write = writes[0]
    first_check = next(
        (
            i
            for i, step in enumerate(steps)
            if armed[i] and step.id not in consequential_ids
        ),
        None,
    )
    if (
        first_check is not None
        and first_check > first_write
        and not any(armed[: first_write + 1])
    ):
        early = [ids[i] for i in writes if i < first_check and not armed[i]]
        block_start = max(i for i in writes if i < first_check) + 1
        order = _suggest(ids, first_write, block_start, first_check)
        findings.append(
            OrderingFinding(
                code="write_before_identity_check",
                step_id=ids[first_write],
                check_step_id=ids[first_check],
                message=(
                    f"step(s) {', '.join(early)} write before the first "
                    f"right-record check (step {ids[first_check]}), so a wrong "
                    "record is caught only after the app has changed. If the "
                    f"check does not depend on those writes, move steps "
                    f"{ids[block_start]}..{ids[first_check]} ahead of step "
                    f"{ids[first_write]}. Suggested order: {' -> '.join(order)}"
                ),
                suggested_order=order,
            )
        )
        flagged_checks.add(first_check)

    for j, step in enumerate(steps):
        if j in flagged_checks or step.id in consequential_ids:
            continue
        earlier = [i for i in writes if i < j]
        if not earlier:
            continue
        guards_later_write = armed[j] and any(i > j for i in writes)
        if not (_is_halting_check(step) or guards_later_write):
            continue
        what = (
            "a precondition that halts the run"
            if _is_halting_check(step)
            else "a right-record check before a later write"
        )
        block_start = max(earlier) + 1
        order = _suggest(ids, earlier[0], block_start, j)
        findings.append(
            OrderingFinding(
                code="write_before_check",
                step_id=ids[earlier[0]],
                check_step_id=ids[j],
                message=(
                    f"step {ids[j]} is {what}, and it runs after step(s) "
                    f"{', '.join(ids[i] for i in earlier)} already wrote. If it "
                    "fails, the run stops with the app half-changed. If the "
                    "check does not depend on those writes, move steps "
                    f"{ids[block_start]}..{ids[j]} ahead of step "
                    f"{ids[earlier[0]]} (add any navigation needed to get "
                    f"back). Suggested order: {' -> '.join(order)}"
                ),
                suggested_order=order,
            )
        )
    return findings


def undo_declaration_problems(workflow: Workflow) -> list[tuple[str, str, str]]:
    """``(undo_id, code, problem)`` for each undo that can't be offered.

    ``undo_target_missing``: it names no step of the bundle.
    ``undo_duplicate_target``: an earlier undo already covers that step.
    """
    from openadapt_flow.traversal import iter_workflow_steps

    step_ids = {step.id for step in iter_workflow_steps(workflow)}
    problems: list[tuple[str, str, str]] = []
    seen_targets: dict[str, str] = {}
    for undo in workflow.undo_steps:
        if undo.undoes not in step_ids:
            problems.append(
                (
                    undo.id,
                    "undo_target_missing",
                    f"undoes step {undo.undoes!r}, which is not in this bundle",
                )
            )
        elif undo.undoes in seen_targets:
            problems.append(
                (
                    undo.id,
                    "undo_duplicate_target",
                    f"is a second undo for step {undo.undoes!r} (first: "
                    f"{seen_targets[undo.undoes]!r}); only the first is offered",
                )
            )
        else:
            seen_targets[undo.undoes] = undo.id
    return problems


# ---------------------------------------------------------------------------
# 3. Declared undo: offer, approve, run, verify
# ---------------------------------------------------------------------------


class UndoRefused(RuntimeError):
    """An undo was not offered, approved or run. Nothing was executed."""


class UndoNotOffered(UndoRefused):
    """This run does not offer that undo (it finished, or the step's change
    was proven absent, or the bundle declares no such undo)."""


class UndoNotApproved(UndoRefused):
    """No approval by a person matches this exact offer."""


class UndoAlreadyRun(UndoRefused):
    """The approval was already used. An undo runs at most once."""


class UndoOffer(BaseModel):
    """An undo Flow offers a person after a stop. Offering runs nothing."""

    undo_id: str
    undoes: str
    intent: str
    source: Literal["demonstration", "hand_written"]
    #: The ledger entry that makes this undo relevant.
    ledger_entry: ChangeLedgerEntry
    #: SHA-256 of the run's report.json bytes.
    report_sha256: str
    bundle_content_digest: Optional[str] = None
    #: SHA-256 over the undo declaration, the ledger entry, the report and the
    #: bundle. An approval binds to this digest, so it can't be replayed onto
    #: another run, another bundle or an edited undo.
    offer_digest: str


class UndoApproval(BaseModel):
    """A person's approval to run one offered undo, once."""

    undo_id: str
    offer_digest: str
    approver: str = Field(min_length=1)
    approved_at: str
    note: str = ""


class UndoResult(BaseModel):
    """What happened when an approved undo ran."""

    undo_id: str
    undoes: str
    #: ``verified``: every step ran and every declared undo effect was
    #: confirmed in the system of record. ``not_verified``: anything else; the
    #: app may still need a person's repair.
    status: Literal["verified", "not_verified"]
    reason: str
    approver: str
    offer_digest: str
    started_at: str
    finished_at: str
    undo_run_dir: str
    execution_outcome: Optional[str] = None
    transaction_outcome: Optional[str] = None
    effect_results: list[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_report(run_dir: Path) -> tuple[RunReport, str]:
    path = run_dir / "report.json"
    if not path.is_file():
        raise UndoNotOffered(f"{run_dir} holds no report.json")
    raw = path.read_bytes()
    return RunReport.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def _undo_dir(run_dir: Path, undo_id: str) -> Path:
    return run_dir / UNDO_DIRNAME / undo_id


def offer_undos(run_dir: Path | str, workflow: Workflow) -> list[UndoOffer]:
    """The undos Flow offers a person for this stopped run, latest step first.

    An undo is offered when the run stopped (``success`` is False), its
    ledger entry may need undoing, the step ran once (not a loop body), and
    the bundle declares an undo for that step. Undo the latest change first: the offers come in that order.
    Offering runs nothing.
    """
    run_dir = Path(run_dir)
    report, report_sha = _load_report(run_dir)
    if report.success:
        return []
    bundle_digest = (
        workflow.manifest.content_digest if workflow.manifest is not None else None
    )
    # A step that ran more than once (a loop body) is not offered: the undo
    # runs with the run's parameters and can't be bound to one iteration.
    runs_per_step: dict[str, int] = {}
    for entry in report.change_ledger:
        runs_per_step[entry.step_id] = runs_per_step.get(entry.step_id, 0) + 1
    offers: list[UndoOffer] = []
    for entry in sorted(report.change_ledger, key=lambda e: -e.position):
        if not entry.may_need_undo or runs_per_step[entry.step_id] > 1:
            continue
        undo = next((u for u in workflow.undo_steps if u.undoes == entry.step_id), None)
        if undo is None:
            continue
        digest = hashlib.sha256(
            _canonical(
                {
                    "undo": undo.model_dump(mode="json"),
                    "ledger_entry": entry.model_dump(mode="json"),
                    "report_sha256": report_sha,
                    "bundle_content_digest": bundle_digest,
                }
            )
        ).hexdigest()
        offers.append(
            UndoOffer(
                undo_id=undo.id,
                undoes=undo.undoes,
                intent=undo.intent,
                source=undo.source,
                ledger_entry=entry,
                report_sha256=report_sha,
                bundle_content_digest=bundle_digest,
                offer_digest=digest,
            )
        )
    return offers


def _find_offer(run_dir: Path, workflow: Workflow, undo_id: str) -> UndoOffer:
    for offer in offer_undos(run_dir, workflow):
        if offer.undo_id == undo_id:
            return offer
    raise UndoNotOffered(
        f"undo {undo_id!r} is not offered for {run_dir}: the run finished, the "
        "system of record proved the step's change absent, or the bundle "
        "declares no such undo"
    )


def _write_once(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def approve_undo(
    run_dir: Path | str,
    workflow: Workflow,
    undo_id: str,
    *,
    approver: Optional[str] = None,
    note: str = "",
) -> UndoApproval:
    """Record a person's approval to run one offered undo.

    Binds to the exact offer (report, bundle, undo, ledger entry). Written
    once: a second approval for the same undo is refused. The CLI calls this
    only from an interactive terminal after the person types the undo id.
    """
    run_dir = Path(run_dir)
    offer = _find_offer(run_dir, workflow, undo_id)
    approval = UndoApproval(
        undo_id=undo_id,
        offer_digest=offer.offer_digest,
        approver=approver or getpass.getuser(),
        approved_at=_now(),
        note=note,
    )
    try:
        _write_once(
            _undo_dir(run_dir, undo_id) / APPROVAL_FILENAME,
            approval.model_dump_json(indent=2),
        )
    except FileExistsError as exc:
        raise UndoRefused(f"undo {undo_id!r} already has an approval") from exc
    return approval


def read_undo_result(run_dir: Path | str, undo_id: str) -> Optional[UndoResult]:
    path = _undo_dir(Path(run_dir), undo_id) / RESULT_FILENAME
    if not path.is_file():
        return None
    return UndoResult.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class PreparedUndo:
    """An approved, claimed undo, ready for the normal runtime."""

    offer: UndoOffer
    approval: UndoApproval
    undo: UndoStep
    workflow: Workflow
    params: dict[str, Any]
    undo_run_dir: Path
    started_at: str


def undo_workflow(workflow: Workflow, undo: UndoStep) -> Workflow:
    """The undo as a straight-line workflow the normal runtime can run."""
    return workflow.model_copy(
        update={
            "name": f"{workflow.name} undo {undo.id}",
            "steps": [step.model_copy(deep=True) for step in undo.steps],
            "program": None,
            "subflows": {},
            "data_sources": {},
            "undo_steps": [],
            "qualification": None,
            "manifest": None,
        }
    )


def prepare_undo_run(
    run_dir: Path | str, workflow: Workflow, undo_id: str
) -> PreparedUndo:
    """Check the approval and claim it. Raises :class:`UndoRefused` otherwise.

    The claim is written with exclusive create, so an approval is used at
    most once even when two processes race.
    """
    run_dir = Path(run_dir)
    offer = _find_offer(run_dir, workflow, undo_id)
    approval_path = _undo_dir(run_dir, undo_id) / APPROVAL_FILENAME
    if not approval_path.is_file():
        raise UndoNotApproved(
            f"undo {undo_id!r} has no approval. A person must review and "
            "approve it first; Flow never runs an undo on its own"
        )
    try:
        approval = UndoApproval.model_validate_json(
            approval_path.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise UndoNotApproved(f"undo {undo_id!r} approval is unreadable") from exc
    if approval.undo_id != undo_id or approval.offer_digest != offer.offer_digest:
        raise UndoNotApproved(
            f"the approval for undo {undo_id!r} was given for a different "
            "offer (the report, bundle or undo changed since); a person must "
            "approve it again"
        )
    started = _now()
    try:
        _write_once(
            _undo_dir(run_dir, undo_id) / CLAIM_FILENAME,
            json.dumps({"offer_digest": offer.offer_digest, "claimed_at": started}),
        )
    except FileExistsError as exc:
        raise UndoAlreadyRun(
            f"undo {undo_id!r} was already run with this approval; check "
            f"{_undo_dir(run_dir, undo_id) / RESULT_FILENAME}"
        ) from exc
    undo = next(u for u in workflow.undo_steps if u.id == undo_id)
    report, _ = _load_report(run_dir)
    return PreparedUndo(
        offer=offer,
        approval=approval,
        undo=undo,
        workflow=undo_workflow(workflow, undo),
        params=dict(report.params),
        undo_run_dir=_undo_dir(run_dir, undo_id) / UNDO_RUN_DIRNAME,
        started_at=started,
    )


def _judge_undo(
    prepared: PreparedUndo, report: Optional[RunReport]
) -> tuple[bool, str]:
    if report is None:
        return False, "the undo run left no report"
    by_id = {result.step_id: result for result in report.results}
    for step in prepared.undo.steps:
        result = by_id.get(step.id)
        if result is None:
            return False, f"undo step {step.id!r} did not run"
        if not result.ok:
            return False, f"undo step {step.id!r} failed: {result.error or 'halted'}"
        if step.effects and result.effect_verified is not True:
            return False, (
                f"undo step {step.id!r} was not confirmed in the system of record"
            )
    return True, "every undo step ran and its declared effects were confirmed"


def finish_undo_run(
    run_dir: Path | str, prepared: PreparedUndo, report: Optional[RunReport]
) -> UndoResult:
    """Judge the undo run against its declared effects and record the result."""
    verified, reason = _judge_undo(prepared, report)
    result = UndoResult(
        undo_id=prepared.undo.id,
        undoes=prepared.undo.undoes,
        status="verified" if verified else "not_verified",
        reason=reason,
        approver=prepared.approval.approver,
        offer_digest=prepared.offer.offer_digest,
        started_at=prepared.started_at,
        finished_at=_now(),
        undo_run_dir=str(prepared.undo_run_dir),
        execution_outcome=report.execution_outcome if report else None,
        transaction_outcome=report.transaction_outcome if report else None,
        effect_results=[
            line
            for r in (report.results if report else [])
            for line in r.effect_results
        ],
    )
    (_undo_dir(Path(run_dir), prepared.undo.id) / RESULT_FILENAME).write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    return result


def run_undo(
    run_dir: Path | str,
    workflow: Workflow,
    undo_id: str,
    *,
    replayer: Any,
    bundle_dir: Path | str,
) -> UndoResult:
    """Run one APPROVED undo through ``replayer`` and verify it landed.

    Refuses (and runs nothing) without a matching approval, or when the
    approval was already used. The undo runs through the normal runtime, so
    identity checks, the risk gate and effect verification all apply.
    """
    prepared = prepare_undo_run(run_dir, workflow, undo_id)
    report: Optional[RunReport] = None
    try:
        report = replayer.run(
            prepared.workflow,
            params=prepared.params,
            bundle_dir=Path(bundle_dir),
            run_dir=prepared.undo_run_dir,
        )
    finally:
        result = finish_undo_run(run_dir, prepared, report)
    return result


# ---------------------------------------------------------------------------
# Declaring an undo
# ---------------------------------------------------------------------------


def _rewrite_paths(node: Any, mapping: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _rewrite_paths(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite_paths(value, mapping) for value in node]
    if isinstance(node, str) and node in mapping:
        return mapping[node]
    return node


def steps_from_demonstration(
    demo_bundle: Path | str, target_bundle: Path | str, undo_id: str
) -> list[Step]:
    """Take the compiled steps of a recorded undo and move their crops over.

    ``demo_bundle`` is the bundle compiled from a recording of a person doing
    the undo. Its referenced crops are copied into
    ``<target_bundle>/templates/undo-<undo_id>/`` and the steps are rewritten
    to point there. Encrypted demonstration bundles are refused.
    """
    from openadapt_flow.bundle_validation import _referenced_asset_paths

    demo_bundle = Path(demo_bundle)
    target_bundle = Path(target_bundle)
    demo = Workflow.load(demo_bundle)
    if demo.encrypted:
        raise ValueError("an encrypted demonstration bundle is not supported here")
    if demo.program is not None:
        raise ValueError("the undo demonstration must compile to straight-line steps")
    mapping: dict[str, str] = {}
    for rel in sorted(_referenced_asset_paths(demo)):
        source = demo_bundle / rel
        if not source.is_file():
            raise ValueError(f"the demonstration bundle is missing {rel}")
        new_rel = f"templates/undo-{undo_id}/{Path(rel).name}"
        (target_bundle / new_rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_bundle / new_rel)
        mapping[rel] = new_rel
    payload = [
        _rewrite_paths(step.model_dump(mode="json"), mapping) for step in demo.steps
    ]
    return [Step.model_validate(item) for item in payload]


def declare_undo(bundle_dir: Path | str, undo: UndoStep) -> Workflow:
    """Add ``undo`` to the bundle and re-seal it.

    Re-sealing changes the bundle digest, so a certified bundle needs
    re-certifying, and approvals bound to the old digest no longer match.
    """
    bundle_dir = Path(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    if any(existing.id == undo.id for existing in workflow.undo_steps):
        raise ValueError(f"the bundle already declares undo {undo.id!r}")
    candidate = workflow.model_copy(update={"undo_steps": [*workflow.undo_steps, undo]})
    problems = [
        msg
        for uid, _code, msg in undo_declaration_problems(candidate)
        if uid == undo.id
    ]
    if problems:
        raise ValueError(f"undo {undo.id!r} {problems[0]}")
    workflow.undo_steps = [*workflow.undo_steps, undo]
    workflow.save(bundle_dir, encrypt=workflow.encrypted)
    return workflow


__all__ = [
    "ChangeLedgerEntry",
    "OrderingFinding",
    "PreparedUndo",
    "UndoAlreadyRun",
    "UndoApproval",
    "UndoNotApproved",
    "UndoNotOffered",
    "UndoOffer",
    "UndoRefused",
    "UndoResult",
    "approve_undo",
    "build_change_ledger",
    "change_ledger_lines",
    "declare_undo",
    "finish_undo_run",
    "offer_undos",
    "prepare_undo_run",
    "read_undo_result",
    "run_undo",
    "steps_from_demonstration",
    "undo_declaration_problems",
    "undo_workflow",
    "write_ordering_findings",
]
