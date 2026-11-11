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
from typing import Optional, Sequence

from openadapt_flow.ir import Workflow
from openadapt_flow.learning.teach import teach
from openadapt_flow.repair.candidate import (
    RepairCandidate,
    RepairProvenance,
    TaughtStep,
    sha256_hex,
)
from openadapt_flow.repair.cli import all_steps
from openadapt_flow.repair.dialog_identity import (
    enclosing_dialog_region,
    mark_dialog_in_recording,
)
from openadapt_flow.repair.registration import build_candidate, detached_candidate_path

__all__ = [
    "AgentTeachRefused",
    "AgentTeachResult",
    "teach_branch_from_calling_agent",
    "teach_from_calling_agent",
]

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


def _finish_candidate(
    bundle: Path,
    out: Path,
    evidence: Path,
    run_dir: Path,
    recording: Path,
    provenance: RepairProvenance,
) -> RepairCandidate:
    """Copy the evidence frames and write the candidate with its provenance."""
    base_steps = all_steps(Workflow.load(bundle))
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
            TaughtStep(step_id=step_id, frame_sha256=sha256_hex(target.read_bytes()))
        )
    candidate = build_candidate(bundle, out, source="teach", evidence_run_dir=evidence)
    if candidate.state == "rejected":
        raise AgentTeachRefused(candidate.rejection_reason or "contract weakened")
    candidate.taught_steps = taught
    candidate.provenance = provenance
    path = detached_candidate_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
    return candidate


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
        candidate = _finish_candidate(
            Path(bundle), out, evidence, run_dir, recording, provenance
        )
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(evidence, ignore_errors=True)
        raise
    return AgentTeachResult(
        candidate=candidate, proposed_bundle=out, evidence_dir=evidence
    )


#: A shared band shorter than this can't hold a readable line of text.
_MIN_BAND_HEIGHT = 20


def shared_dialog_band(
    recordings: Sequence[Path | str],
) -> Optional[tuple[int, int, int, int]]:
    """The part of the dialog that every demonstration shows identically.

    Each demonstration's dialog can carry details of its own run (a payer,
    a reference). Using the whole dialog as the dismiss step's identity would
    then refuse the next run's dialog. This returns the horizontal band of the
    dialog, around the click, whose pixels are the same in every recording's
    frame, or None when the dialogs differ in place or nothing around the
    click is shared.
    """
    import json

    import cv2
    import numpy as np

    regions = []
    frames = []
    clicks = []
    for rec in recordings:
        rec = Path(rec)
        lines = (rec / "events.jsonl").read_text(encoding="utf-8").splitlines()
        event = json.loads(next(line for line in lines if line.strip()))
        frame = (rec / "frames" / f"{int(event['i']):04d}_before.png").read_bytes()
        click = (int(event["x"]), int(event["y"]))
        region = enclosing_dialog_region(frame, click)
        if region is None:
            return None
        regions.append(region)
        clicks.append(click)
        image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return None
        frames.append(image)
    if len(set(regions)) != 1 or len(set(clicks)) != 1:
        return None
    x, y, w, h = regions[0]
    cy = clicks[0][1]
    stack = np.stack([f[y : y + h, x : x + w].astype(np.int16) for f in frames])
    same = (stack.max(axis=0) - stack.min(axis=0)).max(axis=1) <= 8
    row = cy - y
    if not 0 <= row < h or not same[row]:
        return None
    top = row
    while top > 0 and same[top - 1]:
        top -= 1
    bottom = row
    while bottom + 1 < h and same[bottom + 1]:
        bottom += 1
    if bottom - top + 1 < _MIN_BAND_HEIGHT:
        return None
    return (x, y + top, w, bottom - top + 1)


def teach_branch_from_calling_agent(
    examples: Sequence[tuple[Path | str, Path | str]],
    out: Path | str,
    *,
    bundle: Path | str,
    provenance: RepairProvenance,
    normal_runs: Sequence[Path | str] = (),
    library_dir: Optional[Path | str] = None,
) -> AgentTeachResult:
    """Build one branch candidate from several of the agent's demonstrations.

    ``examples`` pairs each halted run with the agent's recording for it. The
    branch is learned by :func:`openadapt_flow.learning.branch_teach.teach_branch`
    (shared screen text, optional parameter narrowing, the governed learn
    loop). The dismiss step's identity is the part of the dialog every
    demonstration shows the same way (:func:`shared_dialog_band`), so a dialog
    with another run's details still passes it. ``provenance`` describes the
    latest demonstration. The candidate
    goes through the same campaigns and human approval as any repair, and the
    agent can't review or approve it.
    """
    from openadapt_flow.learning.branch_teach import BranchExample, teach_branch
    from openadapt_flow.learning.teach import TeachError

    if len(examples) < 2:
        raise AgentTeachRefused("a branch needs at least two demonstrations")
    out = Path(out)
    if out.exists():
        raise AgentTeachRefused(f"{out} already exists; pick a new output path")
    evidence = out.parent / f"{out.name}.evidence"
    first_run, first_recording = (Path(p) for p in examples[0])
    try:
        band = shared_dialog_band([rec for _, rec in examples])
        for _, recording in examples:
            mark_dialog_in_recording(Path(recording), dialog_region=band)
        try:
            result = teach_branch(
                [BranchExample(run_dir=Path(r), fix=Path(f)) for r, f in examples],
                out,
                bundle=bundle,
                negatives=list(normal_runs),
                library_dir=library_dir,
            )
        except TeachError as exc:
            raise AgentTeachRefused(str(exc)) from exc
        if not result.promoted:
            raise AgentTeachRefused(result.reason or "the branch was not learned")
        candidate = _finish_candidate(
            Path(bundle), out, evidence, first_run, first_recording, provenance
        )
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(evidence, ignore_errors=True)
        raise
    return AgentTeachResult(
        candidate=candidate, proposed_bundle=out, evidence_dir=evidence
    )
