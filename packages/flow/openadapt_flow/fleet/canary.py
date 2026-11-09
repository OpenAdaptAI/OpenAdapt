"""Run an imported patch against this tenant's own qualification cases.

The canary is the replay and fault campaigns of the repair lifecycle, run on
whole workflow cases instead of single evidence frames:

* a **healthy case** passes only when the run is ``VERIFIED`` (independent
  effect verification under a Standard or Regulated profile), shows no
  silent-incorrect signal, healed nothing, and made **zero model calls**. A
  run that needed a model is not a healthy replay, whatever it ended with;
* a **fault case** (wrong entity, duplicated target, missing control, ...)
  passes only when the run refused: it did not succeed, did not end
  ``VERIFIED``, confirmed no effect, and made zero model calls.

Cases, their parameters, and their run directories stay on this tenant. The
campaign records carry only a digest label per case.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.repair.candidate import (
    Actor,
    CampaignCaseResult,
    CampaignResult,
    RepairCandidate,
)
from openadapt_flow.repair.lifecycle import (
    QUALIFICATION_CASE_KIND,
    RepairLifecycleError,
    RepairStore,
    observation_from_report,
)

CANARY_ACTOR = Actor(kind="automation", id="fleet-canary")


class QualificationCase(BaseModel):
    """One of this tenant's qualification cases. Never leaves the tenant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=256)
    kind: Literal["healthy", "fault"]
    #: For fault cases, the fault the runner presents (``wrong_entity``,
    #: ``duplicate_target``, ``missing_control``, ...).
    fault: Optional[str] = Field(default=None, max_length=64)
    params: dict[str, str] = Field(default_factory=dict)


class TenantCanaryRunner(Protocol):
    """Runs one case of a bundle on this tenant and returns the run report.

    In production this is the tenant's qualification run path. The fleet
    simulator (:mod:`openadapt_flow.fleet.simulator`) drives the real
    :class:`~openadapt_flow.runtime.replayer.Replayer` against a simulated
    application.
    """

    def run_case(
        self, bundle_dir: Path, case: QualificationCase, run_dir: Path
    ) -> RunReport: ...


def _label(case: QualificationCase) -> str:
    return "case-" + hashlib.sha256(case.case_id.encode()).hexdigest()[:12]


def judge_healthy(report: RunReport) -> tuple[bool, str]:
    observation = observation_from_report(report)
    if report.model_calls:
        return False, f"made {report.model_calls} model call(s)"
    if report.heal_count:
        return False, f"healed {report.heal_count} step(s)"
    if observation.silent_incorrect:
        return False, "silent-incorrect result"
    if report.execution_outcome != "VERIFIED" or not observation.verified:
        return False, f"outcome {report.execution_outcome or 'unknown'}, not VERIFIED"
    return True, "VERIFIED, zero model calls"


def judge_fault(report: RunReport) -> tuple[bool, str]:
    if report.model_calls:
        return False, f"made {report.model_calls} model call(s)"
    if report.success or report.execution_outcome == "VERIFIED":
        return False, "acted on a fault case"
    for result in report.results:
        for evidence in result.effect_evidence:
            if evidence.observed_effect == "present":
                return False, "a write landed on a fault case"
    return True, f"refused ({report.execution_outcome or 'halted'})"


def run_qualification_canary(
    runner: TenantCanaryRunner,
    bundle_dir: Path,
    cases: Sequence[QualificationCase],
    work_dir: Path,
) -> tuple[CampaignResult, CampaignResult]:
    """Run every case; return the (replay, fault) campaign results."""
    healthy: list[CampaignCaseResult] = []
    faults: list[CampaignCaseResult] = []
    for index, case in enumerate(cases):
        run_dir = work_dir / f"{index:03d}-{case.kind}"
        try:
            report = runner.run_case(bundle_dir, case, run_dir)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed case
            passed, detail = False, f"runner raised {type(exc).__name__}"
        else:
            passed, detail = (
                judge_healthy(report) if case.kind == "healthy" else judge_fault(report)
            )
        entry = CampaignCaseResult(
            label=_label(case),
            kind=QUALIFICATION_CASE_KIND,
            passed=passed,
            detail=detail,
        )
        (healthy if case.kind == "healthy" else faults).append(entry)

    def campaign(
        kind: Literal["replay", "fault"], items: list[CampaignCaseResult]
    ) -> CampaignResult:
        failed = sum(not item.passed for item in items)
        return CampaignResult(
            campaign=kind,
            passed=bool(items) and failed == 0,
            total=len(items),
            failed=failed,
            cases=items,
        )

    return campaign("replay", healthy), campaign("fault", faults)


def canary_patch(
    store: RepairStore,
    candidate_id: str,
    runner: TenantCanaryRunner,
    cases: Sequence[QualificationCase],
    work_dir: Path,
) -> RepairCandidate:
    """Run the canary on a candidate's proposed bundle and record it.

    The results are always recorded. The state advances only from
    ``reviewed`` (replay) and ``replay_passed`` (fault), as for any campaign.
    """
    candidate = store.load_candidate(candidate_id)
    if not candidate.proposed_bundle_path:
        raise RepairLifecycleError("candidate has no proposed bundle to run")
    bundle = Path(candidate.proposed_bundle_path)
    workflow = Workflow.load(bundle)
    digest = workflow.manifest.content_digest if workflow.manifest else ""
    if digest != candidate.proposed_content_digest:
        raise RepairLifecycleError(
            "refused: the proposed bundle changed since it was registered"
        )
    replay, fault = run_qualification_canary(runner, bundle, cases, work_dir)
    candidate = store.record_campaign(candidate_id, replay, CANARY_ACTOR)
    return store.record_campaign(candidate.candidate_id, fault, CANARY_ACTOR)


__all__ = [
    "CANARY_ACTOR",
    "QualificationCase",
    "TenantCanaryRunner",
    "canary_patch",
    "judge_fault",
    "judge_healthy",
    "run_qualification_canary",
]
