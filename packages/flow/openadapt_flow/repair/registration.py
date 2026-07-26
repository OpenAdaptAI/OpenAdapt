"""Building and registering repair candidates from bundle pairs.

This is the seam that replaces immediate repair promotion: wherever the
engine used to write a proposed bundle that an operator could simply start
using (the replayer's ``save_healed_to`` healed bundle, the teach loop's
promoted bundle), it now ALSO writes a detached
:class:`~openadapt_flow.repair.candidate.RepairCandidate` record inside the
proposed bundle (``repair/candidate.json``). The record never activates
anything; it is the entry ticket into the governed lifecycle
(:class:`~openadapt_flow.repair.lifecycle.RepairStore`), which the ``repair``
CLI drives.

The candidate record is privacy-safe by construction: binding values are
stored as digests, failure evidence as path + hash references, and failure
fingerprints as structured labels plus digests. No raw observation (frame,
OCR text, identity band) enters the record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from openadapt_flow.ir import Workflow
from openadapt_flow.qualification import EvidenceRef
from openadapt_flow.repair.candidate import (
    BindingChange,
    FailureFingerprint,
    RepairCandidate,
    RepairSource,
    candidate_id_for,
    sha256_hex,
    value_digest,
)
from openadapt_flow.repair.invariants import (
    RepairInvariantError,
    enforce_contract_invariants,
)
from openadapt_flow.runtime.healing.patch import IDENTITY_FIELDS, LOCATOR_FIELDS

#: Where the detached candidate record lives inside a proposed bundle.
DETACHED_CANDIDATE_RELPATH = "repair/candidate.json"


def detached_candidate_path(proposed_bundle: Path | str) -> Path:
    return Path(proposed_bundle) / DETACHED_CANDIDATE_RELPATH


def _content_digest(workflow: Workflow, label: str) -> str:
    digest = workflow.manifest.content_digest if workflow.manifest else ""
    if not digest:
        raise ValueError(
            f"the {label} bundle carries no content digest; cannot build a "
            "hash-identified repair candidate"
        )
    return digest


def _binding_changes(prior: Workflow, proposed: Workflow) -> list[BindingChange]:
    """Per-step anchor field diffs, values digested (never stored raw)."""
    changes: list[BindingChange] = []
    prior_steps = {step.id: step for step in prior.steps}
    for step in proposed.steps:
        old_step = prior_steps.get(step.id)
        if old_step is None:
            continue
        old_anchor = old_step.anchor
        new_anchor = step.anchor
        if old_anchor is None and new_anchor is None:
            continue
        for field in IDENTITY_FIELDS + LOCATOR_FIELDS + ("structural",):
            old_value = getattr(old_anchor, field, None) if old_anchor else None
            new_value = getattr(new_anchor, field, None) if new_anchor else None
            if old_value == new_value:
                continue
            old_dump = (
                old_value.model_dump(mode="json")
                if isinstance(old_value, BaseModel)
                else old_value
            )
            new_dump = (
                new_value.model_dump(mode="json")
                if isinstance(new_value, BaseModel)
                else new_value
            )
            changes.append(
                BindingChange(
                    step_id=step.id,
                    field=field,
                    identity=field in IDENTITY_FIELDS,
                    old_sha256=value_digest(old_dump),
                    new_sha256=value_digest(new_dump),
                )
            )
    return changes


def _evidence_from_run_dir(
    run_dir: Path,
) -> tuple[list[EvidenceRef], list[FailureFingerprint]]:
    """Collect heal-patch evidence refs + privacy-safe failure fingerprints."""
    evidence: list[EvidenceRef] = []
    fingerprints: list[FailureFingerprint] = []
    heals_dir = run_dir / "heals"
    if heals_dir.is_dir():
        for patch_path in sorted(heals_dir.glob("*/patch.json")):
            raw = patch_path.read_bytes()
            rel = patch_path.relative_to(run_dir).as_posix()
            evidence.append(
                EvidenceRef(kind="other", sha256=sha256_hex(raw), relative_path=rel)
            )
            step_id = patch_path.parent.name
            rung = "unknown"
            try:
                rung = str(json.loads(raw).get("rung_used", "unknown"))
            except (ValueError, AttributeError):
                pass
            fingerprints.append(
                FailureFingerprint.from_evidence(
                    step_id=step_id,
                    failure_class=f"anchor_drift:{rung}",
                    evidence=sha256_hex(raw),
                )
            )
    report_path = run_dir / "report.json"
    if report_path.is_file():
        evidence.append(
            EvidenceRef(
                kind="run_report",
                sha256=sha256_hex(report_path.read_bytes()),
                relative_path="report.json",
            )
        )
    return evidence, fingerprints


def build_candidate(
    prior_bundle: Path | str,
    proposed_bundle: Path | str,
    *,
    source: RepairSource,
    evidence_run_dir: Optional[Path | str] = None,
) -> RepairCandidate:
    """Build a repair candidate from a (prior, proposed) bundle pair.

    Runs the contract-invariant enforcement (fail closed): a proposed bundle
    that weakens the identity, effect, risk, environment, or policy contract
    without an explicit new qualification revision yields a candidate that is
    ALREADY ``rejected`` (with the refusal recorded); it can never advance.
    The candidate never activates anything by being built.
    """
    prior_path = Path(prior_bundle)
    proposed_path = Path(proposed_bundle)
    prior = Workflow.load(prior_path)
    proposed = Workflow.load(proposed_path)
    prior_digest = _content_digest(prior, "prior")
    proposed_digest = _content_digest(proposed, "proposed")

    evidence: list[EvidenceRef] = []
    fingerprints: list[FailureFingerprint] = []
    if evidence_run_dir is not None:
        evidence, fingerprints = _evidence_from_run_dir(Path(evidence_run_dir))

    environment_sha: Optional[str] = None
    if proposed.qualification is not None:
        environment_sha = proposed.qualification.environment.contract_sha256()

    candidate = RepairCandidate(
        candidate_id=candidate_id_for(prior_digest, proposed_digest),
        source=source,
        workflow_name=proposed.name,
        prior_content_digest=prior_digest,
        proposed_content_digest=proposed_digest,
        prior_bundle_path=str(prior_path.resolve()),
        proposed_bundle_path=str(proposed_path.resolve()),
        environment_contract_sha256=environment_sha,
        qualification_revision_prior=(
            prior.qualification.revision if prior.qualification else None
        ),
        qualification_revision_proposed=(
            proposed.qualification.revision if proposed.qualification else None
        ),
        binding_changes=_binding_changes(prior, proposed),
        failure_evidence=evidence,
        failure_fingerprints=fingerprints,
        evidence_run_dir=(
            str(Path(evidence_run_dir).resolve())
            if evidence_run_dir is not None
            else None
        ),
    )

    try:
        candidate.contract_weakenings = enforce_contract_invariants(prior, proposed)
    except RepairInvariantError as exc:
        candidate.state = "rejected"
        candidate.rejection_reason = str(exc)
    return candidate


def register_bundle_candidate(
    prior_bundle: Path | str,
    proposed_bundle: Path | str,
    *,
    source: RepairSource,
    evidence_run_dir: Optional[Path | str] = None,
) -> Path:
    """Write the detached candidate record inside the proposed bundle.

    Called by the replayer (healed bundle) and the teach loop (taught
    bundle). The record makes the proposed bundle a lifecycle CANDIDATE and
    nothing more: it does not stage, approve, or activate anything.

    Returns the path of the written ``repair/candidate.json``.
    """
    candidate = build_candidate(
        prior_bundle,
        proposed_bundle,
        source=source,
        evidence_run_dir=evidence_run_dir,
    )
    path = detached_candidate_path(proposed_bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate.model_dump_json(indent=2))
    return path


def load_detached_candidate(proposed_bundle: Path | str) -> RepairCandidate:
    """Load the detached candidate record from a proposed bundle."""
    path = detached_candidate_path(proposed_bundle)
    if not path.is_file():
        raise FileNotFoundError(
            f"no detached repair candidate at {path}; register the bundle "
            "pair first (openadapt-flow repair register)"
        )
    return RepairCandidate.model_validate_json(path.read_text())
