"""Turn a calling agent's demonstration into a governed repair candidate.

``openadapt-agent`` serves workflows to other agents over MCP. When a run
halts, it can hand the calling agent a one-time repair token. If an operator
turned on screen access for that workflow, the agent may demonstrate the
stuck step once while OpenAdapt records it. This module takes that recording
and:

1. marks the dialog around each click as that step's identity
   (:mod:`openadapt_flow.repair.dialog_identity`), so a dismiss step can
   pass the fault campaign the same way a record-row step does;
2. runs the ordinary :func:`openadapt_flow.learning.teach.teach` loop, which
   refuses an underdetermined or unsafe fix and writes nothing;
3. copies the frame the agent acted on into a per-candidate evidence
   directory, so ``openadapt-flow repair campaign`` has a frame to replay the
   taught step against;
4. writes the detached ``repair/candidate.json`` with
   :class:`~openadapt_flow.repair.candidate.RepairProvenance` attached.

The result is a candidate in state ``candidate``. It goes through the same
review, replay campaign, fault campaign, human approval, staging, and canary
as any other repair. The agent named in the provenance can never review or
approve it (:meth:`RepairStore.review`, :meth:`RepairStore.approve`).

Every failure removes the taught bundle, so no ungoverned bundle is left on
disk. The workflow keeps halting until a person approves a repair.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openadapt_flow.ir import Workflow
from openadapt_flow.learning.teach import teach
from openadapt_flow.repair.candidate import (
    RepairCandidate,
    RepairProvenance,
    TaughtStep,
    sha256_hex,
)
from openadapt_flow.repair.cli import all_steps
from openadapt_flow.repair.dialog_identity import mark_dialog_in_recording
from openadapt_flow.repair.registration import build_candidate, detached_candidate_path

__all__ = ["AgentTeachRefused", "AgentTeachResult", "teach_from_calling_agent"]

_STEP_TEMPLATE = re.compile(r"step_(\d+)\.png$")


class AgentTeachRefused(Exception):
    """The demonstration did not produce a repair candidate. Nothing was kept."""


@dataclass
class AgentTeachResult:
    candidate: RepairCandidate
    proposed_bundle: Path
    evidence_dir: Path


def _evidence_frame(recording: Path, template: str) -> Path:
    """The frame the agent clicked on, for a step compiled from ``recording``."""
    match = _STEP_TEMPLATE.search(template)
    if match is None:
        raise AgentTeachRefused(
            "a taught step has no recorded template, so there is no frame to test it on"
        )
    frame = recording / "frames" / f"{int(match.group(1)):04d}_before.png"
    if not frame.is_file():
        raise AgentTeachRefused(f"the recording has no frame for {template}")
    return frame


def teach_from_calling_agent(
    run_dir: Path | str,
    recording: Path | str,
    out: Path | str,
    *,
    bundle: Path | str,
    provenance: RepairProvenance,
    library_dir: Optional[Path | str] = None,
    dialog_region: Optional[tuple[int, int, int, int]] = None,
) -> AgentTeachResult:
    """Build a governed repair candidate from an agent's recorded demonstration.

    Raises :class:`AgentTeachRefused` when the teach loop refuses the fix or
    the candidate can't be recorded. In both cases nothing is left at ``out``.
    ``dialog_region`` is the dialog's box when the caller knows it from the
    DOM or accessibility tree; otherwise it is found in the recorded frame.
    """
    run_dir = Path(run_dir)
    recording = Path(recording)
    out = Path(out)
    if out.exists():
        raise AgentTeachRefused(f"{out} already exists; pick a new output path")
    evidence = out.parent / f"{out.name}.evidence"
    try:
        mark_dialog_in_recording(recording, dialog_region=dialog_region)
        result = teach(run_dir, recording, out, bundle=bundle, library_dir=library_dir)
        if not result.promoted:
            raise AgentTeachRefused(result.outcome.reason)
        base_steps = all_steps(Workflow.load(Path(bundle)))
        new_steps = {
            step_id: step
            for step_id, step in all_steps(Workflow.load(out)).items()
            if step_id not in base_steps and step.anchor is not None
        }
        if not new_steps:
            raise AgentTeachRefused(
                "the demonstration added no step with an on-screen target"
            )
        if evidence.exists():
            shutil.rmtree(evidence)
        (evidence / "heals").mkdir(parents=True)
        shutil.copy2(run_dir / "report.json", evidence / "report.json")
        taught: list[TaughtStep] = []
        for step_id, step in sorted(new_steps.items()):
            assert step.anchor is not None  # filtered above
            frame = _evidence_frame(recording, step.anchor.template)
            target = evidence / "heals" / step_id / "screen.png"
            target.parent.mkdir(parents=True)
            shutil.copy2(frame, target)
            taught.append(
                TaughtStep(
                    step_id=step_id, frame_sha256=sha256_hex(target.read_bytes())
                )
            )
        candidate = build_candidate(
            bundle, out, source="teach", evidence_run_dir=evidence
        )
        if candidate.state == "rejected":
            raise AgentTeachRefused(candidate.rejection_reason or "contract weakened")
        candidate.taught_steps = taught
        candidate.provenance = provenance
        path = detached_candidate_path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate.model_dump_json(indent=2))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(evidence, ignore_errors=True)
        raise
    return AgentTeachResult(
        candidate=candidate, proposed_bundle=out, evidence_dir=evidence
    )
