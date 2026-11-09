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
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from openadapt_flow import sealed_set as _sealed
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


def _declared_effects(step: Optional[Step], result: StepResult) -> list["Effect"]:
    """The effect set the step ran against: the API binding's when it was
    actuated through the API, else the step's own."""
    if step is None:
        return []
    if result.actuation == "api" and step.api_binding is not None:
        return list(step.api_binding.effects)
    if step.effects:
        return list(step.effects)
    if step.api_binding is not None:
        return list(step.api_binding.effects)
    return []


def _ledger_effects(
    result: StepResult, declared: list["Effect"]
) -> list[ChangeLedgerEffect]:
    """One entry per declared effect, matched to evidence by contract hash.

    ``result.effect_contract_hashes`` holds the run-bound hash of each
    declared effect, in declaration order; evidence is matched to it by hash,
    never by position, and each evidence item settles one contract at most.
    An effect with no matching evidence (no verifier, or the run stopped
    before checking it) is ``unknown``. The description is the effect kind
    only: a compiler-written ``probe`` can quote record values.
    """
    hashes = list(result.effect_contract_hashes)
    unclaimed = list(result.effect_evidence)
    out: list[ChangeLedgerEffect] = []
    for index in range(max(len(declared), len(hashes))):
        kind = declared[index].kind.value if index < len(declared) else "effect"
        contract_hash = hashes[index] if index < len(hashes) else ""
        item = next(
            (
                e
                for e in unclaimed
                if contract_hash and e.effect_contract_hash == contract_hash
            ),
            None,
        )
        if item is not None:
            unclaimed.remove(item)
        out.append(
            ChangeLedgerEffect(
                kind=kind,
                description=kind,
                contract_hash=contract_hash,
                status=_effect_status(item.final_verdict) if item else "unknown",
                observed_effect=item.observed_effect if item else "unknown",
            )
        )
    # Evidence for a contract the step didn't declare is still a reading of
    # the system of record; keep it visible rather than drop it.
    for item in unclaimed:
        out.append(
            ChangeLedgerEffect(
                kind="effect",
                description=f"{item.substrate} effect",
                contract_hash=item.effect_contract_hash,
                status=_effect_status(item.final_verdict),
                observed_effect=item.observed_effect,
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
    that nothing the step declared landed: every declared contract read
    ``absent``, with exact hash coverage (``transaction._effect_absence_proven``).
    """
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.transaction import (
        _attempt_state,
        _effect_absence_proven,
        _is_consequential_result,
    )
    from openadapt_flow.traversal import iter_workflow_steps

    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    undo_by_target: dict[str, str] = {}
    for undo in workflow.undo_steps:
        # A proposal no person has approved is not an undo yet.
        if not undo.awaiting_approval:
            undo_by_target.setdefault(undo.undoes, undo.id)

    ledger: list[ChangeLedgerEntry] = []
    position = 0
    for result in report.results:
        if result.skipped:
            continue
        position += 1
        step = steps_by_id.get(result.step_id)
        declared = _declared_effects(step, result)
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
        # The transaction outcome's own absence rule: every declared contract
        # read ``absent``, with exact hash coverage.
        may_need_undo = not _effect_absence_proven(result)
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
    source: Literal["demonstration", "hand_written", "synthesized"]
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
    if not _sealed.is_file(path):
        raise UndoNotOffered(f"{run_dir} holds no report.json")
    raw = _sealed.read_bytes(path)
    return RunReport.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def _undo_dir(run_dir: Path, undo_id: str) -> Path:
    return run_dir / UNDO_DIRNAME / undo_id


def _has_loop(workflow: Workflow) -> bool:
    from openadapt_flow.ir import StateKind

    graphs = [workflow.program, *workflow.subflows.values()]
    return any(
        state.kind is StateKind.LOOP
        for graph in graphs
        if graph is not None
        for state in graph.states.values()
    )


LINEAGE_FILENAME = "undo-lineage.json"
_LINEAGE_SCHEMA = "openadapt.undo-lineage/v1"


def _read_lineage(bundle_dir: Path) -> list[str]:
    path = bundle_dir / LINEAGE_FILENAME
    if not _sealed.is_file(path):
        return []
    try:
        raw = json.loads(_sealed.read_text(path))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("schema") != _LINEAGE_SCHEMA:
        return []
    digests = raw.get("prior_content_digests")
    return (
        [d for d in digests if isinstance(d, str)] if isinstance(digests, list) else []
    )


def _record_lineage(bundle_dir: Path, prior_digest: str) -> None:
    digests = _read_lineage(bundle_dir)
    if prior_digest not in digests:
        digests.append(prior_digest)
    _sealed.write_text(
        bundle_dir / LINEAGE_FILENAME,
        json.dumps(
            {"schema": _LINEAGE_SCHEMA, "prior_content_digests": digests}, indent=2
        ),
    )


def bundle_mismatch(
    report: RunReport, workflow: Workflow, bundle_dir: Optional[Path | str] = None
) -> Optional[str]:
    """Why ``workflow`` is not the bundle this run used, or None if it is.

    The run's sealed digest must equal the bundle's current digest, or a
    digest the bundle had before an undo was declared on it (recorded by
    :func:`declare_undo` in ``undo-lineage.json`` in ``bundle_dir``). That
    keeps an undo declared after the stop, and any re-certification that
    follows, matched to the run. Without a sealed digest on either side, the
    name and every ledger step (id and intent) must match.

    This guards against passing the wrong bundle by accident. It is not a
    defense against someone who can edit the bundle directory: the approval
    is what binds the undo a person approved.
    """
    from openadapt_flow.traversal import iter_workflow_steps

    if report.workflow_name != workflow.name:
        return f"the run used workflow {report.workflow_name!r}, not {workflow.name!r}"
    run_digest = report.bundle_content_digest
    if run_digest is not None and workflow.manifest is not None:
        known = {workflow.manifest.content_digest}
        if bundle_dir is not None:
            known.update(_read_lineage(Path(bundle_dir)))
        if run_digest in known:
            return None
        return (
            "the bundle's content digest differs from the one the run used; "
            "pass the run's own bundle"
        )
    steps = {step.id: step for step in iter_workflow_steps(workflow)}
    for entry in report.change_ledger:
        step = steps.get(entry.step_id)
        if step is None or step.intent != entry.intent:
            return f"step {entry.step_id!r} differs from the one the run used"
    return None


def offer_undos(
    run_dir: Path | str,
    workflow: Workflow,
    *,
    bundle_dir: Optional[Path | str] = None,
) -> list[UndoOffer]:
    """The undos Flow offers a person for this stopped run, latest step first.

    An undo is offered when the run stopped (``success`` is False), the
    bundle is the one the run used (:func:`bundle_mismatch`), the workflow
    has no loop, the step's ledger entry may need undoing, and the bundle
    declares an undo for that step. Undo the latest change first: the offers
    come in that order. Offering runs nothing.

    Loops are refused because an undo runs with the run's base parameters,
    while a loop iteration wrote with its row's values.
    """
    run_dir = Path(run_dir)
    report, report_sha = _load_report(run_dir)
    if report.success or report.loop_iterations or _has_loop(workflow):
        return []
    if bundle_mismatch(report, workflow, bundle_dir) is not None:
        return []
    bundle_digest = (
        workflow.manifest.content_digest if workflow.manifest is not None else None
    )
    runs_per_step: dict[str, int] = {}
    for entry in report.change_ledger:
        runs_per_step[entry.step_id] = runs_per_step.get(entry.step_id, 0) + 1
    offers: list[UndoOffer] = []
    for entry in sorted(report.change_ledger, key=lambda e: -e.position):
        if not entry.may_need_undo or runs_per_step[entry.step_id] > 1:
            continue
        undo = next(
            (
                u
                for u in workflow.undo_steps
                if u.undoes == entry.step_id and not u.awaiting_approval
            ),
            None,
        )
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


def _find_offer(
    run_dir: Path,
    workflow: Workflow,
    undo_id: str,
    bundle_dir: Optional[Path | str] = None,
) -> UndoOffer:
    for offer in offer_undos(run_dir, workflow, bundle_dir=bundle_dir):
        if offer.undo_id == undo_id:
            return offer
    report, _ = _load_report(run_dir)
    why = bundle_mismatch(report, workflow, bundle_dir)
    raise UndoNotOffered(
        f"undo {undo_id!r} is not offered for {run_dir}: "
        + (
            why
            if why
            else "the run finished, the workflow has a loop, the system of "
            "record proved the step's change absent, the bundle declares no "
            "such undo, or it is a proposal no person has approved yet "
            "(openadapt-flow undo accept)"
        )
    )


def _write_once(path: Path, payload: str) -> None:
    if _sealed.is_sealed_path(path):
        # The run of a sealed bundle: the record is encrypted in memory. The
        # existence check and the write are not one atomic step here.
        if _sealed.exists(path):
            raise FileExistsError(str(path))
        _sealed.write_text(path, payload)
        return
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
    bundle_dir: Optional[Path | str] = None,
) -> UndoApproval:
    """Record a person's approval to run one offered undo.

    Binds to the exact offer (report, bundle, undo, ledger entry). A second
    approval for the same offer is refused, and so is any approval once the
    undo was claimed to run. An unused approval for an offer that has since
    changed (a stale approval) is replaced. The CLI calls this only from an
    interactive terminal after the person types the undo id.
    """
    run_dir = Path(run_dir)
    offer = _find_offer(run_dir, workflow, undo_id, bundle_dir)
    undo_dir = _undo_dir(run_dir, undo_id)
    if _sealed.exists(undo_dir / CLAIM_FILENAME):
        raise UndoAlreadyRun(f"undo {undo_id!r} was already run; it runs once")
    approval = UndoApproval(
        undo_id=undo_id,
        offer_digest=offer.offer_digest,
        approver=approver or getpass.getuser(),
        approved_at=_now(),
        note=note,
    )
    path = undo_dir / APPROVAL_FILENAME
    existing = read_undo_approval(run_dir, undo_id)
    if existing is not None and existing.offer_digest == offer.offer_digest:
        raise UndoRefused(f"undo {undo_id!r} is already approved for this offer")
    if existing is None and not _sealed.exists(path):
        try:
            _write_once(path, approval.model_dump_json(indent=2))
        except FileExistsError as exc:
            raise UndoRefused(f"undo {undo_id!r} already has an approval") from exc
        return approval
    if _sealed.is_sealed_path(path):
        _sealed.write_text(path, approval.model_dump_json(indent=2))
        return approval
    # A stale or unreadable approval that was never used: replace it whole.
    staging = undo_dir / f".{APPROVAL_FILENAME}.{os.getpid()}"
    _write_once(staging, approval.model_dump_json(indent=2))
    os.replace(staging, path)
    return approval


def read_undo_approval(run_dir: Path | str, undo_id: str) -> Optional[UndoApproval]:
    path = _undo_dir(Path(run_dir), undo_id) / APPROVAL_FILENAME
    if not _sealed.is_file(path):
        return None
    try:
        return UndoApproval.model_validate_json(_sealed.read_text(path))
    except ValueError:
        return None


def read_undo_result(run_dir: Path | str, undo_id: str) -> Optional[UndoResult]:
    path = _undo_dir(Path(run_dir), undo_id) / RESULT_FILENAME
    if not _sealed.is_file(path):
        return None
    return UndoResult.model_validate_json(_sealed.read_text(path))


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


# ---------------------------------------------------------------------------
# Values kept before the save, for an undo that locates or restores
# ---------------------------------------------------------------------------

PRIOR_DIRNAME = "prior"
_PRIOR_SCHEMA = "openadapt.undo-prior/v1"

RecordsReader = Callable[[], Optional[list[dict[str, Any]]]]


def _resolved_selector(
    match: dict[str, Any], params: Mapping[str, Any]
) -> Optional[dict[str, Any]]:
    """``match`` bound to ``params``; None when a value is missing.

    Values are bound expressions (:func:`selector_value`), so a number
    compares by value as it does when the record check selects the record.
    """
    from openadapt_flow.runtime.effects.effect import (
        Effect,
        EffectKind,
        selector_value,
    )

    effect = Effect(kind=EffectKind.RECORD_WRITTEN, match=match)
    out: dict[str, Any] = {}
    for key, expr in effect.match.items():
        if expr.resolve(params) is None:
            return None
        out[key] = selector_value(expr.resolved(params))
    return out


def _matching(
    records: list[dict[str, Any]], selector: dict[str, Any]
) -> list[dict[str, Any]]:
    from openadapt_flow.runtime.effects.effect import record_matches

    return [r for r in records if record_matches(r, selector)]


def prior_state_path(run_dir: Path | str, step_id: str) -> Path:
    digest = hashlib.sha256(step_id.encode("utf-8")).hexdigest()[:16]
    return Path(run_dir) / UNDO_DIRNAME / PRIOR_DIRNAME / f"{digest}.json"


def keep_prior_state(
    workflow: Workflow,
    step: Step,
    records: Optional[list[dict[str, Any]]],
    params: Mapping[str, Any],
    run_dir: Path | str,
) -> Optional[Path]:
    """Keep what an undo of ``step`` needs, read just before the save.

    Called by the replayer with the system-of-record records it read for the
    step's pre-action baseline. Writes only when the bundle has an undo for
    ``step`` that locates or restores. For ``locate``: the ids of records
    that already matched, so the undo can tell the new one. For
    ``restore``: the old values of the named fields, from the one matching
    record. The file holds record values, like ``params.json``: it is
    created owner-only and never copied into the report.

    Never raises: a run is not stopped because an undo can't be prepared.
    The undo then refuses, and a person repairs the record by hand.
    """
    undos = [
        u
        for u in workflow.undo_steps
        if u.undoes == step.id and (u.locate is not None or u.restore is not None)
    ]
    if not undos or records is None:
        return None
    try:
        payload: dict[str, Any] = {"schema": _PRIOR_SCHEMA, "step_id": step.id}
        existing: dict[str, list[str]] = {}
        restored: dict[str, dict[str, Any]] = {}
        for undo in undos:
            if undo.locate is not None:
                selector = _resolved_selector(undo.locate.match, params)
                if selector is not None:
                    existing[undo.id] = sorted(
                        str(r.get(undo.locate.field))
                        for r in _matching(records, selector)
                    )
            if undo.restore is not None:
                selector = _resolved_selector(undo.restore.match, params)
                rows = _matching(records, selector) if selector is not None else []
                if len(rows) == 1:
                    restored[undo.id] = {
                        name: rows[0].get(name) for name in undo.restore.fields
                    }
        payload["existing_ids"] = existing
        payload["restore"] = restored
        path = prior_state_path(run_dir, step.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{path.name}.{os.getpid()}")
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(staging, path)
        return path
    except Exception:  # noqa: BLE001 - never stop a run over undo bookkeeping
        return None


def read_prior_state(run_dir: Path | str, step_id: str) -> Optional[dict[str, Any]]:
    path = prior_state_path(run_dir, step_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _PRIOR_SCHEMA:
        return None
    return raw


def needs_records(undo: UndoStep) -> bool:
    """Whether running ``undo`` needs a fresh read of the system of record."""
    return undo.locate is not None


def resolve_undo_params(
    undo: UndoStep,
    run_dir: Path | str,
    params: Mapping[str, Any],
    read_records: Optional[RecordsReader] = None,
) -> dict[str, Any]:
    """The run's params plus what ``undo.locate`` and ``undo.restore`` bind.

    Raises :class:`UndoRefused` when a value can't be found: no values were
    kept before the save, the record the save created can't be told apart,
    or the system of record can't be read. Nothing has run at that point.
    """
    out = dict(params)
    if undo.locate is None and undo.restore is None:
        return out
    prior = read_prior_state(run_dir, undo.undoes)
    if prior is None:
        raise UndoRefused(
            f"the run kept no values before step {undo.undoes!r} for undo "
            f"{undo.id!r} (the step ran without a record read-back, or before "
            "this undo was in the bundle); repair the record by hand"
        )
    if undo.restore is not None:
        values = (prior.get("restore") or {}).get(undo.id)
        if not isinstance(values, dict) or set(values) != set(undo.restore.fields):
            raise UndoRefused(
                f"the run could not read one record's old values before step "
                f"{undo.undoes!r}; undo {undo.id!r} has nothing to restore"
            )
        for field_name, param in undo.restore.fields.items():
            value = values[field_name]
            out[param] = "" if value is None else str(value)
    if undo.locate is not None:
        existing = (prior.get("existing_ids") or {}).get(undo.id)
        selector = _resolved_selector(undo.locate.match, params)
        if not isinstance(existing, list) or selector is None:
            raise UndoRefused(
                f"the run kept no record list before step {undo.undoes!r}; "
                f"undo {undo.id!r} can't tell which record the step created"
            )
        records = read_records() if read_records is not None else None
        if records is None:
            raise UndoRefused(
                "the system of record could not be read, so the record the "
                f"step {undo.undoes!r} created can't be found; nothing ran"
            )
        new = [
            r
            for r in _matching(records, selector)
            if str(r.get(undo.locate.field)) not in set(existing)
        ]
        if len(new) != 1:
            raise UndoRefused(
                f"{len(new)} record(s) match step {undo.undoes!r}'s record and "
                "were not there before it ran; undo "
                f"{undo.id!r} needs exactly one. Repair the record by hand"
            )
        out[undo.locate.param] = str(new[0].get(undo.locate.field))
    return out


def prepare_undo_run(
    run_dir: Path | str,
    workflow: Workflow,
    undo_id: str,
    *,
    bundle_dir: Optional[Path | str] = None,
    read_records: Optional[RecordsReader] = None,
) -> PreparedUndo:
    """Check the approval and claim it. Raises :class:`UndoRefused` otherwise.

    The claim is written with exclusive create, so an approval is used at
    most once even when two processes race. An undo that locates or restores
    resolves its values first (``read_records`` reads the system of record),
    so a refusal there leaves the approval unused.
    """
    run_dir = Path(run_dir)
    offer = _find_offer(run_dir, workflow, undo_id, bundle_dir)
    approval_path = _undo_dir(run_dir, undo_id) / APPROVAL_FILENAME
    if not _sealed.is_file(approval_path):
        raise UndoNotApproved(
            f"undo {undo_id!r} has no approval. A person must review and "
            "approve it first; Flow never runs an undo on its own"
        )
    try:
        approval = UndoApproval.model_validate_json(_sealed.read_text(approval_path))
    except ValueError as exc:
        raise UndoNotApproved(f"undo {undo_id!r} approval is unreadable") from exc
    if approval.undo_id != undo_id or approval.offer_digest != offer.offer_digest:
        raise UndoNotApproved(
            f"the approval for undo {undo_id!r} was given for a different "
            "offer (the report, bundle or undo changed since); a person must "
            "approve it again"
        )
    undo = next(u for u in workflow.undo_steps if u.id == undo_id)
    report, _ = _load_report(run_dir)
    params = resolve_undo_params(undo, run_dir, report.params, read_records)
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
    return PreparedUndo(
        offer=offer,
        approval=approval,
        undo=undo,
        workflow=undo_workflow(workflow, undo),
        params=params,
        undo_run_dir=_undo_dir(run_dir, undo_id) / UNDO_RUN_DIRNAME,
        started_at=started,
    )


def _judge_undo(
    prepared: PreparedUndo, report: Optional[RunReport]
) -> tuple[bool, str]:
    return judge_undo_report(prepared.undo, report)


def judge_undo_report(undo: UndoStep, report: Optional[RunReport]) -> tuple[bool, str]:
    """Whether ``report`` shows ``undo`` ran in full and was confirmed.

    Every undo step must have run and succeeded, and every step that
    declares an effect must be confirmed by an independent read of the
    system of record. ``undo propose`` judges its test-copy runs with this
    same function.
    """
    from openadapt_flow.transaction import _effect_evidence_has_exact_coverage

    if report is None:
        return False, "the undo run left no report"
    by_id = {result.step_id: result for result in report.results}
    for step in undo.steps:
        result = by_id.get(step.id)
        if result is None:
            return False, f"undo step {step.id!r} did not run"
        if not result.ok:
            return False, f"undo step {step.id!r} failed: {result.error or 'halted'}"
        if not step.effects:
            continue
        if (
            result.effect_verified is not True
            or not _effect_evidence_has_exact_coverage(result)
        ):
            return False, (
                f"undo step {step.id!r} was not confirmed in the system of record"
            )
        weak = [
            e.assurance
            for e in result.effect_evidence
            if e.final_verdict != "confirmed" or e.assurance != "independent_readback"
        ]
        if weak:
            return False, (
                f"undo step {step.id!r} was confirmed only by "
                f"{', '.join(sorted(set(weak)))}, not an independent read of the "
                "system of record"
            )
    return True, (
        "every undo step ran and an independent read of the system of record "
        "confirmed its declared effects"
    )


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
    _sealed.write_text(
        _undo_dir(Path(run_dir), prepared.undo.id) / RESULT_FILENAME,
        result.model_dump_json(indent=2),
    )
    return result


def records_reader(verifier: Any) -> Optional[RecordsReader]:
    """Read every record through an effect verifier's own read path."""
    if verifier is None or not callable(getattr(verifier, "capture_pre_state", None)):
        return None

    def read() -> Optional[list[dict[str, Any]]]:
        try:
            state = verifier.capture_pre_state()
        except Exception:  # noqa: BLE001 - an unreadable store is not a guess
            return None
        if getattr(state, "reachable", False) is not True:
            return None
        return [dict(r) for r in state.records]

    return read


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
    prepared = prepare_undo_run(
        run_dir,
        workflow,
        undo_id,
        bundle_dir=bundle_dir,
        read_records=records_reader(getattr(replayer, "effect_verifier", None)),
    )
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


@dataclass(frozen=True)
class DemonstratedUndo:
    """Compiled undo steps and the crops they need, not yet copied."""

    steps: list[Step]
    #: Target bundle-relative path -> source file in the demonstration bundle.
    assets: dict[str, Path]


def steps_from_demonstration(demo_bundle: Path | str, undo_id: str) -> DemonstratedUndo:
    """Read the compiled steps of a recorded undo and plan their crops.

    ``demo_bundle`` is the bundle compiled from a recording of a person doing
    the undo. Each referenced crop gets a path under
    ``templates/undo-<undo_id>/`` that keeps its sub-path (an anchor crop and
    an identifier crop can share a file name), and the steps are rewritten to
    point there. Nothing is copied until :func:`declare_undo` has validated
    the undo. Encrypted demonstration bundles are refused.
    """
    from openadapt_flow.bundle_validation import _referenced_asset_paths

    demo_bundle = Path(demo_bundle)
    demo = Workflow.load(demo_bundle)
    if demo.encrypted:
        raise ValueError("an encrypted demonstration bundle is not supported here")
    if demo.program is not None:
        raise ValueError("the undo demonstration must compile to straight-line steps")
    mapping: dict[str, str] = {}
    assets: dict[str, Path] = {}
    for rel in sorted(_referenced_asset_paths(demo)):
        source = demo_bundle / rel
        if not source.is_file():
            raise ValueError(f"the demonstration bundle is missing {rel}")
        inner = rel[len("templates/") :] if rel.startswith("templates/") else rel
        new_rel = f"templates/undo-{undo_id}/{inner}"
        mapping[rel] = new_rel
        assets[new_rel] = source
    payload = [
        _rewrite_paths(step.model_dump(mode="json"), mapping) for step in demo.steps
    ]
    return DemonstratedUndo(
        steps=[Step.model_validate(item) for item in payload], assets=assets
    )


def declare_undo(
    bundle_dir: Path | str,
    undo: UndoStep,
    *,
    assets: Optional[dict[str, Path]] = None,
) -> Workflow:
    """Validate ``undo``, copy its crops, add it to the bundle and re-seal it.

    Nothing is written when the undo is refused. Re-sealing changes the
    bundle digest, so a prior certification is marked expired (the bundle
    needs certifying again), and approvals bound to the old digest no longer
    match.
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
    for rel in assets or {}:
        if _sealed.exists(bundle_dir / rel):
            raise ValueError(f"the bundle already has {rel}")
    for rel, source in (assets or {}).items():
        if _sealed.is_sealed(bundle_dir):
            # The crop joins the in-memory map; save() seals it below.
            workflow._decrypted_templates[rel] = Path(source).read_bytes()
            continue
        (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, bundle_dir / rel)
    return save_undo_steps(
        bundle_dir,
        workflow,
        [*workflow.undo_steps, undo],
        reason="an undo step was declared after certification",
    )


def save_undo_steps(
    bundle_dir: Path | str,
    workflow: Workflow,
    undo_steps: list[UndoStep],
    *,
    reason: str,
) -> Workflow:
    """Replace the bundle's undo steps and re-seal it.

    Runs made with the bundle as it was still match it afterwards (the old
    digest goes into ``undo-lineage.json``). A prior certification is marked
    expired with ``reason``.
    """
    from openadapt_flow.bundle_sealing import _invalidate_prior_certification

    bundle_dir = Path(bundle_dir)
    if workflow.manifest is not None and workflow.manifest.content_digest:
        _record_lineage(bundle_dir, workflow.manifest.content_digest)
    workflow.undo_steps = list(undo_steps)
    if workflow.manifest is not None:
        _invalidate_prior_certification(workflow, reason=reason)
        # The template is rebuilt from the certification on every load; with
        # the certification gone it can't be, so it goes too.
        workflow.manifest.provenance.governed_authorization_template = None
    workflow.save(bundle_dir, encrypt=workflow.encrypted)
    return Workflow.load(bundle_dir)


__all__ = [
    "ChangeLedgerEntry",
    "DemonstratedUndo",
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
    "judge_undo_report",
    "keep_prior_state",
    "needs_records",
    "prior_state_path",
    "read_prior_state",
    "records_reader",
    "resolve_undo_params",
    "offer_undos",
    "prepare_undo_run",
    "LINEAGE_FILENAME",
    "bundle_mismatch",
    "read_undo_approval",
    "read_undo_result",
    "run_undo",
    "save_undo_steps",
    "steps_from_demonstration",
    "undo_declaration_problems",
    "undo_workflow",
    "write_ordering_findings",
]
