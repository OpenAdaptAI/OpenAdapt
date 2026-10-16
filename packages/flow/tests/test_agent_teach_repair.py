"""A calling agent's demonstration becomes a governed repair candidate.

End to end on :mod:`openadapt_flow.repair.dialog_fixture`, a rendered page
where a survey dialog covers the confirm banner after Save. Flow's real
replayer, template matcher, OCR, teach loop, and replay and fault campaigns
run on it; nothing is stubbed. The run halts, the agent's one-click recording
is taught, the agent's own review and approval are refused, a person
approves, staging and canary activate it, and the activated bundle replays
without halting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.repair import Actor, CanaryRunObservation, RepairStore
from openadapt_flow.repair import dialog_fixture as fx
from openadapt_flow.repair.agent_teach import (
    AgentTeachRefused,
    teach_from_calling_agent,
)
from openadapt_flow.repair.campaign import _fault_verifier_failure
from openadapt_flow.repair.candidate import RepairProvenance, ScreenScrubEvidence
from openadapt_flow.repair.cli import _run_campaign
from openadapt_flow.repair.dialog_identity import enclosing_dialog_region
from openadapt_flow.repair.lifecycle import ModelActuationError, RepairLifecycleError
from openadapt_flow.runtime.replayer import Replayer

AGENT_ID = "mcp-client:test-agent"
PERSON = Actor(kind="human", id="reviewer@example.org")
AUTOMATION = Actor(kind="automation", id="ci")
TOKEN_SHA = hashlib.sha256(b"repair-token").hexdigest()


def _provenance(**overrides) -> RepairProvenance:
    values = dict(
        agent_id=AGENT_ID,
        halted_run_id="run-0001",
        halted_step_id="s_verify",
        repair_token_sha256=TOKEN_SHA,
        screen_access_enabled_by="admin@example.org",
        screens_shared=1,
        screens_scrubbed=True,
        scrub_evidence=ScreenScrubEvidence(
            provider="presidio", package_version="1.1.2", policy_sha256="ab" * 32
        ),
    )
    values.update(overrides)
    return RepairProvenance(**values)


def _replay(bundle: Path, run_dir: Path):
    app = fx.DialogApp()
    report = Replayer(app).run(
        Workflow.load(bundle), bundle_dir=bundle, run_dir=run_dir
    )
    return report, app


def _agent_recording(root: Path, *, dialog_region=None) -> Path:
    screen = fx.DialogApp(saved=True)
    before = screen.screenshot()
    screen.click(*fx.DISMISS_CLICK)
    return fx.record_click(
        root, before, screen.screenshot(), fx.DISMISS_CLICK, dialog_region=dialog_region
    )


@pytest.fixture()
def halted(tmp_path: Path):
    bundle = fx.base_bundle(tmp_path / "bundle")
    report, _ = _replay(bundle, tmp_path / "run")
    assert report.halt is not None and report.halt.state_id == "s_verify"
    return bundle, tmp_path / "run"


@pytest.fixture()
def taught(halted, tmp_path: Path):
    bundle, run_dir = halted
    result = teach_from_calling_agent(
        run_dir,
        _agent_recording(tmp_path / "agent_recording"),
        tmp_path / "proposed",
        bundle=bundle,
        provenance=_provenance(),
    )
    return bundle, run_dir, result


def test_halt_to_teach_to_approved_replay(taught, tmp_path: Path) -> None:
    _, _, result = taught
    candidate = result.candidate
    assert candidate.state == "candidate"
    assert candidate.provenance is not None
    assert candidate.provenance.repaired_by == "calling_agent"
    [taught_step] = candidate.taught_steps
    assert (
        result.evidence_dir / "heals" / taught_step.step_id / "screen.png"
    ).is_file()

    store = RepairStore(tmp_path / "store")
    candidate = store.add_candidate(candidate)
    assert store.active_pointer() is None

    agent_as_person = Actor(kind="human", id=AGENT_ID)
    with pytest.raises(RepairLifecycleError, match="may not perform repair review"):
        store.review(candidate.candidate_id, agent_as_person)
    with pytest.raises(ModelActuationError):
        store.review(candidate.candidate_id, Actor(kind="model", id=AGENT_ID))
    store.review(candidate.candidate_id, PERSON)

    # The production campaigns, with Flow's real resolver and OCR.
    for kind in ("replay", "fault"):
        campaign = _run_campaign(store.load_candidate(candidate.candidate_id), kind)
        assert campaign.passed, [c.detail for c in campaign.cases if not c.passed]
        store.record_campaign(candidate.candidate_id, campaign, AUTOMATION)

    with pytest.raises(RepairLifecycleError, match="may not perform repair approval"):
        store.approve(candidate.candidate_id, agent_as_person, non_interactive=True)
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, AUTOMATION)
    candidate = store.approve(candidate.candidate_id, PERSON, non_interactive=True)
    store.stage(candidate.candidate_id, AUTOMATION)
    store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=1)

    active = store.bundle_path(store.active_pointer().active_digest)
    report, app = _replay(active, tmp_path / "canary-run")
    assert report.success is True and report.halt is None
    assert ("press", "S") in app.actions and app.dialog is False
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id="canary-1", verified=True, silent_incorrect=False),
    )
    assert candidate.state == "active"
    assert "taught by calling agent" in candidate.diff_summary()


def test_dialog_found_from_pixels_or_given_by_dom(halted, tmp_path: Path) -> None:
    app = fx.DialogApp(saved=True)
    region = enclosing_dialog_region(app.screenshot(), fx.DISMISS_CLICK)
    assert region is not None
    x, y, w, h = region
    dx, dy, dw, dh = fx.DIALOG_REGION
    assert (
        abs(x - dx) <= 4 and abs(y - dy) <= 4 and abs(w - dw) <= 8 and abs(h - dh) <= 8
    )
    app.dialog = False
    assert enclosing_dialog_region(app.screenshot(), fx.DISMISS_CLICK) is None

    bundle, run_dir = halted
    result = teach_from_calling_agent(
        run_dir,
        _agent_recording(tmp_path / "dom_recording"),
        tmp_path / "dom_proposed",
        bundle=bundle,
        provenance=_provenance(),
        dialog_region=fx.DIALOG_REGION,
    )
    assert _run_campaign(result.candidate, "fault").passed


def test_dismiss_without_a_dialog_box_stays_unarmed(halted, tmp_path: Path) -> None:
    """With no dialog to read identity from, the fault campaign refuses."""
    bundle, run_dir = halted
    recording = _agent_recording(tmp_path / "bare_recording")
    # A frame with no box around the click: nothing to mark.
    blank = fx.DialogApp(saved=True)
    blank.dialog = False
    (recording / "frames" / "0000_before.png").write_bytes(
        _paint_button_only(blank.screenshot())
    )
    result = teach_from_calling_agent(
        run_dir, recording, tmp_path / "bare", bundle=bundle, provenance=_provenance()
    )
    assert _run_campaign(result.candidate, "fault").passed is False


def _paint_button_only(png: bytes) -> bytes:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    cv2.rectangle(image, (60, 116), (220, 164), (225, 225, 225), -1)
    cv2.putText(
        image, "Dismiss", (84, 148), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2
    )
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


def test_verifier_failure_fault_grays_the_whole_identity_region() -> None:
    from PIL import Image

    image = Image.new("RGB", (300, 200), (255, 255, 255))
    out = _fault_verifier_failure(image, (100, 150, 40, 20), (10, 10, 200, 100))
    assert out.getpixel((15, 15)) == (128, 128, 128)
    assert out.getpixel((120, 160)) == (255, 255, 255)  # target pasted back
    unmarked = _fault_verifier_failure(image, (100, 150, 40, 20))
    assert unmarked.getpixel((15, 15)) == (255, 255, 255)


def test_refused_teach_leaves_nothing(halted, tmp_path: Path) -> None:
    bundle, run_dir = halted
    empty = tmp_path / "empty_recording"
    (empty / "frames").mkdir(parents=True)
    (empty / "events.jsonl").write_text("")
    (empty / "meta.json").write_text(
        '{"id": "rec-empty", "created_at": "2026-07-14T00:00:00+00:00",'
        ' "viewport": [1280, 800], "app_url": "", "params": {}}'
    )
    out = tmp_path / "proposed"
    with pytest.raises(Exception):
        teach_from_calling_agent(
            run_dir, empty, out, bundle=bundle, provenance=_provenance()
        )
    assert not out.exists()
    assert not (tmp_path / "proposed.evidence").exists()


def test_existing_output_is_refused(taught, tmp_path: Path) -> None:
    bundle, run_dir, result = taught
    with pytest.raises(AgentTeachRefused):
        teach_from_calling_agent(
            run_dir,
            tmp_path / "agent_recording",
            result.proposed_bundle,
            bundle=bundle,
            provenance=_provenance(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        dict(screens_scrubbed=False, scrub_evidence=None),
        dict(
            screen_share_mode="ask_each_time",
            screens_scrubbed=False,
            scrub_evidence=None,
            screen_share_approved_by="reviewer@example.org",
        ),
        dict(scrub_evidence=None),
        dict(screen_share_mode="ask_each_time"),
        dict(screen_share_mode="ask_each_time", screen_share_approved_by=AGENT_ID),
    ],
)
def test_inconsistent_screen_provenance_is_rejected(overrides) -> None:
    with pytest.raises(ValueError):
        _provenance(**overrides)


def test_allowed_screen_provenance_modes() -> None:
    assert _provenance().screen_share_mode == "scrubbed_only"
    asked = _provenance(
        screen_share_mode="ask_each_time",
        screen_share_approved_by="reviewer@example.org",
    )
    assert asked.screen_share_approved_by == "reviewer@example.org"
    raw = _provenance(
        screen_share_mode="allow_unscrubbed",
        screens_scrubbed=False,
        scrub_evidence=None,
    )
    assert raw.screens_scrubbed is False
    assert _provenance(screens_shared=0, scrub_evidence=None).screens_shared == 0
