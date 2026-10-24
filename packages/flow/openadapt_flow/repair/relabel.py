"""Repair a control the vendor relabeled, with proof that only its label changed.

When an app release renames a button (for example "Confirm Create New Patient"
becomes "Save new patient"), the run stops before the save: the ladder finds
the same control, but its structured identity no longer matches the recorded
one. Self-repair never repairs a failed identity check, because in general it
can't tell a renamed control from a different control. This module handles
the one case where it can.

The recorded structured identity is a salted hash, so the recorded label is
not readable from it. But the bundle keeps the recorded label in plaintext
(the step's intent and its anchor's OCR text). :func:`propose` rebuilds the
recorded identity from the observed one with only the label swapped back, and
checks it against the recorded hash. A match proves that the control id, the
form and the control kind are all unchanged, and that only the label differs.

Everything else is refused:

- the halted step acted, or any step before it delivered input to a save or
  showed an effect (a step that may have written is never retried);
- the identity check failed on anything but the structured tier, or names a
  record field (a parameter) rather than a control;
- the ladder found the target below the structural rung (a structural
  handle always names exactly one control);
- the rebuilt identity doesn't match the recorded hash under any recorded
  label.

A proposal is never applied on its own. :func:`build` writes a candidate
bundle with the new label in the step's identity template, intent and OCR
text, and nothing else changed. :func:`readmit` then re-qualifies the
candidate on a test copy with the parent's own campaign, asks a person once
(the review shows the old and new label and the re-qualification), and signs
the lineage that lets a pin to the parent accept the certified copy
(:mod:`openadapt_flow.repair.readmission`). Any failure leaves the parent
pinned and saves nothing.

A production qualification admission and a managed dispatch are signed
outside this machine, so their runs stay refused (docs/COMPATIBILITY.md).
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.ir import RunReport, Step, Workflow

#: Only a control: an identity naming a record field is never relabeled.
_CONTROL_KINDS = frozenset({"action"})
#: The identity key that holds the visible label.
_LABEL_KEY = "label"


class RelabelRefused(RuntimeError):
    """The relabel repair can't go ahead; nothing was changed or saved."""


@dataclass(frozen=True)
class RelabelProposal:
    """A control whose label changed, and the proof that nothing else did."""

    step_id: str
    old_label: str
    new_label: str
    #: The observed structured identity, as the run read it (no record values:
    #: only the control's id, kind, form path and label).
    observed_identity: str
    #: Every key of the identity except the label, unchanged by proof.
    unchanged: dict[str, str]

    def summary(self) -> str:
        return (
            f"{self.step_id}: the control now reads {self.new_label!r} instead of "
            f"{self.old_label!r}; its id, kind and form are unchanged (the "
            "recorded identity hash matches with only the label swapped back)"
        )


@dataclass
class RelabelReview:
    """What the person sees before approving a relabel repair."""

    proposal: RelabelProposal
    parent_digest: str
    certified_digest: str
    requalification: dict[str, Any]
    #: Set when the relabeled control is part of the record check (a save
    #: button's identity is): no lineage hop can carry it, so approving it is
    #: a fresh admission of the certified copy. Says why.
    fresh_admission: Optional[str] = None


@dataclass
class RelabelResult:
    """The outcome of :func:`readmit`."""

    #: "readmitted" (lineage hop), "fresh_admission" (a person approved the
    #: certified copy, which runs once the deployment pins its digest),
    #: "declined" or "refused".
    status: str
    reason: str = ""
    bundle_dir: Optional[Path] = None
    digest: Optional[str] = None
    lineage: Optional[dict[str, Any]] = None
    approved_by: Optional[str] = None
    #: Machine and person seconds, by stage, for the operator-time ledger.
    timings: dict[str, float] = field(default_factory=dict)


# -- propose ---------------------------------------------------------------------------


def _quoted(text: str) -> Optional[str]:
    start, end = text.find("'"), text.rfind("'")
    if 0 <= start < end:
        return text[start + 1 : end]
    return None


def _recorded_labels(step: Step) -> list[str]:
    """Every plaintext label the bundle recorded for this step, in order."""

    anchor = step.anchor
    found: list[str] = []
    for label in (
        anchor.ocr_text if anchor is not None else None,
        _quoted(step.intent or ""),
        (
            anchor.structural.name
            if anchor is not None and anchor.structural is not None
            else None
        ),
    ):
        if label and label not in found:
            found.append(label)
    return found


def _certifying_policy(workflow: Workflow) -> Any:
    """The policy the bundle is certified under, as the approval gate reads it.

    A step the certified qualification classified as not saving (a button
    that opens a duplicate check, say) counts as a save without it.
    """

    provenance = workflow.manifest.provenance if workflow.manifest else None
    if provenance is None or not provenance.certified or not provenance.policy_name:
        return None
    from openadapt_flow.policy import load_policy

    try:
        return load_policy(provenance.policy_name)
    except Exception:  # noqa: BLE001 - an unknown policy proves nothing
        return None


def propose(report: RunReport, workflow: Workflow) -> "RelabelProposal | str":
    """A relabel proposal for this halt, or why it isn't one."""

    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.runtime.identity_template import verify_structured_template

    steps = {step.id: step for step in workflow.steps}
    policy = _certifying_policy(workflow)
    for result in report.results:
        step = steps.get(result.step_id)
        if step is None:
            continue
        if result.delivery_attempted and is_consequential(
            step, workflow, certifying_policy=policy
        ):
            return f"input reached {step.id}, a step that saves; never repaired"
        for item in result.effect_evidence:
            if item.observed_effect not in (None, "absent"):
                return f"{step.id} has an observed effect; never repaired"
    if report.halt is None:
        return "the run did not halt at a step"
    halted = next(
        (r for r in reversed(report.results) if not r.ok and not r.skipped), None
    )
    if halted is None or halted.step_id != report.halt.state_id:
        return "the report's halt does not name a failed step"
    step = steps.get(halted.step_id)
    if step is None or step.anchor is None:
        return "the halted step has no on-screen target"
    if halted.delivery_attempted is not False:
        return f"input may have reached {step.id}; only a step that never acted is repaired"
    identity = halted.identity
    if identity is None or identity.status != "mismatch":
        return f"{step.id} did not stop on its identity check"
    if identity.mode != "structured" or identity.param is not None:
        return f"{step.id}'s identity check failed outside the structured tier"
    resolution = halted.resolution
    handle = resolution.structural_handle if resolution is not None else None
    if resolution is None or resolution.rung != "structural" or handle is None:
        return f"{step.id}'s target was not found on the structural rung"
    template = step.anchor.identity_template
    if template is None or not template.structured:
        return f"{step.id} has no recorded structured identity to compare"
    if template.structured_params:
        return f"{step.id}'s identity carries a record value; never relabeled"
    observed = identity.observed or ""
    try:
        live = json.loads(observed)
    except ValueError:
        return f"{step.id}'s observed identity is not a structured control"
    if (
        not isinstance(live, dict)
        or not all(isinstance(v, str) for v in live.values())
        or str(live.get("target_kind", "")) not in _CONTROL_KINDS
        or not live.get(_LABEL_KEY)
    ):
        return f"{step.id}'s observed identity is not a labeled control"
    new_label = live[_LABEL_KEY]
    encoded = json.dumps(new_label)
    if observed.count(encoded) != 1:
        return f"{step.id}'s observed label can't be isolated"
    for old_label in _recorded_labels(step):
        if old_label == new_label:
            continue
        rebuilt = observed.replace(encoded, json.dumps(old_label))
        check = verify_structured_template(template, rebuilt)
        if check is not None and check.status == "verified":
            return RelabelProposal(
                step_id=step.id,
                old_label=old_label,
                new_label=new_label,
                observed_identity=observed,
                unchanged={k: v for k, v in live.items() if k != _LABEL_KEY},
            )
    return (
        f"{step.id}'s control changed more than its label (no recorded label "
        "matches the recorded identity)"
    )


# -- build -----------------------------------------------------------------------------


def _relabeled_template(template: Any, observed: str) -> Any:
    from openadapt_flow.runtime.identity_template import build_identity_template

    fresh = build_identity_template(
        None, structured_identity=observed, salt_hex=template.salt
    )
    assert fresh is not None and fresh.structured
    signals = {
        k: v
        for k, v in (template.signal_hashes or {}).items()
        if not k.startswith("structured|")
    }
    signals.update(
        {
            k: v
            for k, v in (fresh.signal_hashes or {}).items()
            if k.startswith("structured|")
        }
    )
    return template.model_copy(
        update={"structured": fresh.structured, "signal_hashes": signals}
    )


def build(
    bundle_dir: Path | str,
    proposal: RelabelProposal,
    out_dir: Path | str,
    *,
    key: Optional[str] = None,
) -> Path:
    """Write the candidate bundle: the new label, and nothing else changed.

    The candidate is uncertified; only :func:`readmit` can certify and admit
    it.
    """

    from openadapt_flow.repair.self_repair import _uncertify_patch

    bundle_dir, out_dir = Path(bundle_dir), Path(out_dir)
    workflow = Workflow.load(bundle_dir, key=key) if key else Workflow.load(bundle_dir)
    patched = workflow.model_copy(deep=True)
    steps = list(patched.steps)
    index = next((i for i, s in enumerate(steps) if s.id == proposal.step_id), None)
    if index is None:
        raise RelabelRefused(f"the bundle has no step {proposal.step_id}")
    step = steps[index]
    anchor = step.anchor
    if anchor is None or anchor.identity_template is None:
        raise RelabelRefused(f"{step.id} has no identity template")
    new_anchor = anchor.model_copy(
        update={
            "identity_template": _relabeled_template(
                anchor.identity_template, proposal.observed_identity
            ),
            "ocr_text": (
                proposal.new_label
                if anchor.ocr_text == proposal.old_label
                else anchor.ocr_text
            ),
        }
    )
    steps[index] = step.model_copy(
        update={
            "anchor": new_anchor,
            "intent": (step.intent or "").replace(
                f"'{proposal.old_label}'", f"'{proposal.new_label}'"
            ),
        }
    )
    patched.steps = steps
    _uncertify_patch(patched)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(
        bundle_dir, out_dir, ignore=shutil.ignore_patterns("repair", "manifest.json")
    )
    patched.save(out_dir, encrypt=bool(workflow.encrypted), key=key)
    return out_dir


# -- re-qualify, a person's one approval, signed lineage ------------------------------


def _rows(result: Any) -> list[dict[str, Any]]:
    return [
        {k: row.get(k) for k in ("case_id", "trial", "expected", "transaction_outcome")}
        for row in (getattr(result, "rows", None) or [])
        if isinstance(row, dict)
    ]


def _campaigns(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
    """The re-qualification's cases as the store's replay and fault campaigns."""

    from openadapt_flow.repair.candidate import CampaignCaseResult, CampaignResult

    def campaign(kind: str, picked: list[dict[str, Any]]) -> Any:
        cases = [
            CampaignCaseResult(
                label=f"{r.get('case_id')}#{r.get('trial')}"[:128],
                kind=str(r.get("case_id") or "case")[:64],
                passed=True,
                detail=f"{r.get('transaction_outcome')} as expected"[:512],
            )
            for r in picked
        ]
        return CampaignResult(
            campaign=kind,  # type: ignore[arg-type]
            passed=bool(cases),
            total=len(cases),
            failed=0,
            cases=cases,
        )

    replay = [r for r in rows if r.get("case_id") == "representative"]
    fault = [r for r in rows if r.get("case_id") != "representative"]
    return campaign("replay", replay), campaign("fault", fault)


def readmit(
    *,
    parent_bundle: Path | str,
    candidate_bundle: Path | str,
    proposal: RelabelProposal,
    store: Any,
    requalification: Any,
    ask_person: Callable[[RelabelReview], tuple[str, str]],
    work_dir: Path | str,
    evidence_run_dir: Optional[Path | str] = None,
    key_dir: Optional[Path] = None,
    clock: Callable[[], float] = time.monotonic,
) -> RelabelResult:
    """Re-qualify the candidate, ask a person once, and sign the lineage.

    ``ask_person(review)`` returns ``(decision, approver_id)``, where decision
    is ``"approve"`` or ``"decline"``. It is asked only after the candidate
    passed its parent's whole campaign on the test copy. ``store`` is the
    :class:`~openadapt_flow.repair.lifecycle.RepairStore` that holds both
    bundles and the lineage record.
    """

    import dataclasses

    from openadapt_flow.repair.candidate import Actor
    from openadapt_flow.repair.readmission import (
        ReadmissionRefused,
        Requalification,
        admit,
        requalify,
    )
    from openadapt_flow.repair.registration import build_candidate

    automation = Actor(kind="automation", id="openadapt-relabel-repair")
    work = Path(work_dir)
    timings: dict[str, float] = {}
    captured: dict[str, Any] = {}
    inner = requalification.run
    if inner is None:
        from openadapt_flow.qualify.campaign import qualify as inner

    def run(*args: Any, **kwargs: Any) -> Any:
        captured["result"] = inner(*args, **kwargs)
        return captured["result"]

    candidate_digest = Workflow.load(candidate_bundle).manifest.content_digest  # type: ignore[union-attr]
    parent_digest = Workflow.load(parent_bundle).manifest.content_digest  # type: ignore[union-attr]
    t0 = clock()
    try:
        requalified = dataclasses.replace(
            requalify(
                parent_bundle,
                candidate_bundle,
                work / "requalify",
                dataclasses.replace(requalification, run=run)
                if isinstance(requalification, Requalification)
                else requalification,
            ),
            candidate_digest=candidate_digest,
        )
    except ReadmissionRefused as exc:
        timings["requalify_s"] = round(clock() - t0, 1)
        result: Any = captured.get("result")
        if (
            "fresh admission" not in str(exc)
            or not getattr(result, "passed", False)
            or not getattr(result, "certified", False)
        ):
            return RelabelResult(status="refused", reason=str(exc), timings=timings)
        # The campaign passed and certified a copy, but the relabeled control
        # is part of the fixed contract, so no lineage hop may carry it. A
        # person may still admit the certified copy afresh.
        return _fresh_admission(
            proposal,
            parent_digest,
            Path(result.bundle_dir),
            _rows(result),
            str(exc),
            ask_person,
            timings,
            clock,
        )
    timings["requalify_s"] = round(clock() - t0, 1)
    rows = _rows(captured.get("result"))
    candidate = build_candidate(
        parent_bundle,
        requalified.bundle_dir,
        source="manual",
        evidence_run_dir=evidence_run_dir,
    )
    if candidate.state == "rejected":
        return RelabelResult(
            status="refused",
            reason=candidate.rejection_reason
            or "the certified copy weakens the contract",
            timings=timings,
        )
    candidate = store.add_candidate(candidate)
    review = RelabelReview(
        proposal=proposal,
        parent_digest=parent_digest,
        certified_digest=requalified.digest,
        requalification={
            "policy": requalified.policy_name,
            "cases": list(requalified.case_ids),
            "rows": rows,
        },
    )
    t1 = clock()
    decision, approver = ask_person(review)
    timings["person_wait_s"] = round(clock() - t1, 1)
    if decision != "approve":
        store.reject(
            candidate.candidate_id,
            Actor(kind="human", id=approver),
            "declined by a person (relabel repair)",
        )
        return RelabelResult(
            status="declined", reason=f"declined by {approver}", timings=timings
        )
    person = Actor(kind="human", id=approver)
    t2 = clock()
    store.review(candidate.candidate_id, person)
    replay, fault = _campaigns(rows)
    for result in (replay, fault):
        store.record_campaign(candidate.candidate_id, result, automation)
    store.approve(candidate.candidate_id, person)
    store.stage(candidate.candidate_id, automation)
    try:
        record = admit(
            store.root,
            parent_bundle=parent_bundle,
            candidate_digest=candidate_digest,
            candidate_id=candidate.candidate_id,
            requalified=requalified,
            approved_by=approver,
            route="person",
            key_dir=key_dir,
        )
    except ReadmissionRefused as exc:
        timings["readmit_s"] = round(clock() - t2, 1)
        return RelabelResult(status="refused", reason=str(exc), timings=timings)
    timings["readmit_s"] = round(clock() - t2, 1)
    return RelabelResult(
        status="readmitted",
        bundle_dir=store.bundle_path(requalified.digest),
        digest=requalified.digest,
        lineage=record,
        approved_by=approver,
        timings=timings,
    )


def _fresh_admission(
    proposal: RelabelProposal,
    parent_digest: str,
    certified_dir: Path,
    rows: list[dict[str, Any]],
    why: str,
    ask_person: Callable[[RelabelReview], tuple[str, str]],
    timings: dict[str, float],
    clock: Callable[[], float],
) -> RelabelResult:
    """Ask a person to admit the certified copy afresh (no lineage)."""

    certified = Workflow.load(certified_dir)
    provenance = certified.manifest.provenance if certified.manifest else None
    if provenance is None or not provenance.certified:
        return RelabelResult(
            status="refused",
            reason="the re-qualified copy is not certified",
            timings=timings,
        )
    digest = certified.manifest.content_digest  # type: ignore[union-attr]
    review = RelabelReview(
        proposal=proposal,
        parent_digest=parent_digest,
        certified_digest=digest,
        requalification={
            "policy": provenance.policy_name,
            "cases": sorted({str(r.get("case_id")) for r in rows}),
            "rows": rows,
        },
        fresh_admission=why,
    )
    t1 = clock()
    decision, approver = ask_person(review)
    timings["person_wait_s"] = round(clock() - t1, 1)
    if decision != "approve":
        return RelabelResult(
            status="declined", reason=f"declined by {approver}", timings=timings
        )
    return RelabelResult(
        status="fresh_admission",
        reason=why,
        bundle_dir=certified_dir,
        digest=digest,
        approved_by=approver,
        timings=timings,
    )
