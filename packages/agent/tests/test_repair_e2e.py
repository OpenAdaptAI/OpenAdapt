"""End to end: halt -> needs_repair -> teach_repair -> person approves -> replay passes.

Runs on ``openadapt_flow.repair.dialog_fixture``: a rendered app where a
survey dialog covers the confirm banner after Save. Flow's real replayer,
OCR, teach loop, and replay and fault campaigns run; nothing is stubbed
except the scrubber. The halted app instance stays open and is the screen the
agent's click lands on, as a held browser page would be. Skipped when the
installed openadapt-flow has no dialog fixture.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest

pytest.importorskip("openadapt_flow.repair.dialog_fixture")

from openadapt_flow.ir import Workflow  # noqa: E402
from openadapt_flow.repair import Actor, CanaryRunObservation, RepairStore  # noqa: E402
from openadapt_flow.repair import dialog_fixture as fx  # noqa: E402
from openadapt_flow.repair.cli import _run_campaign  # noqa: E402
from openadapt_flow.repair.lifecycle import RepairLifecycleError  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from PIL import Image  # noqa: E402

from openadapt_agent.bridge import AgentBridge  # noqa: E402
from openadapt_agent.cards import WorkflowCard  # noqa: E402
from openadapt_agent.repair import RepairAccessPolicy, RepairDesk, set_access  # noqa: E402
from openadapt_agent.runner import RunnerConfig  # noqa: E402
from openadapt_agent.service import CatalogEntry, EngineResult  # noqa: E402

WORKFLOW = "save_encounter"
AGENT = "calling-agent"
PERSON = Actor(kind="human", id="reviewer")
AUTOMATION = Actor(kind="automation", id="ci")


class HeldPage:
    """The halted app, kept open for the repair, as Flow's hold would."""

    def __init__(self) -> None:
        self.app: fx.DialogApp | None = None
        self.clicks: list[tuple[int, int]] = []

    def screenshot(self) -> bytes:
        assert self.app is not None
        return self.app.screenshot()

    def click(self, x: int, y: int) -> None:
        assert self.app is not None
        self.clicks.append((x, y))
        self.app.click(x, y)

    def viewport(self) -> tuple[int, int]:
        return fx.VIEWPORT


class Scrubber:
    def validate_ready(self, modalities):
        pass

    def scrub_image(self, image):
        return image.convert("L").convert("RGB")

    def evidence(self, modalities):
        return {"provider": "test", "package_version": "0", "status": "completed"}


class ReplayEngine:
    """Runs the active bundle through Flow's real replayer on the fixture."""

    def __init__(self, tmp: Path, desk: RepairDesk, bundle: Path, held: HeldPage) -> None:
        self.tmp, self.desk, self.bundle, self.held = tmp, desk, bundle, held

    def execute(self, entry, run_id, inputs, raw) -> EngineResult:
        bundle = self.desk.active_bundle(entry.name, self.bundle)
        run_dir = self.tmp / "runs" / run_id
        app = fx.DialogApp()
        report = Replayer(app).run(Workflow.load(bundle), bundle_dir=bundle, run_dir=run_dir)
        if report.success:
            return EngineResult(reason="saved_and_checked", model_calls=0)
        self.held.app = app
        return EngineResult(
            reason="unexpected_screen",
            halt_step_id=report.halt.state_id,
            run_dir=str(run_dir),
            bundle_dir=str(bundle),
            model_calls=0,
        )

    def refresh(self, entry, run_id):
        return None


def test_halt_teach_approve_replay(tmp_path: Path) -> None:
    bundle = fx.base_bundle(tmp_path / "bundle")
    access = tmp_path / "access.json"
    set_access(access, workflow=WORKFLOW, mode="scrubbed_only", enabled_by="admin")
    screen = HeldPage()
    desk = RepairDesk(
        tmp_path / ".repair",
        RepairAccessPolicy(access),
        demonstrator=screen,
        scrubber_factory=Scrubber,
        agent_id=AGENT,
    )
    bridge = AgentBridge(
        None, RunnerConfig(runs_dir=tmp_path / "runs"), allow_run=True, repair_desk=desk
    )
    card = WorkflowCard(name=WORKFLOW, purpose="Save an encounter.", done_means="Saved.")
    bridge.catalog[WORKFLOW] = CatalogEntry(
        card=card, engine=ReplayEngine(tmp_path, desk, bundle, screen)
    )

    def run(request_id: str) -> dict:
        return bridge.dispatch(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": {}, "request_id": request_id},
        )

    # 1. The run halts and returns needs_repair, with no screen in it.
    halted = run("req-0001")
    assert halted["outcome"] == "needs_review"
    block = halted["needs_repair"]
    assert block["step_id"] == "s_verify"
    assert block["teach_repair_available"] is True

    # 2. The agent looks at the scrubbed screen and teaches one click.
    shown = bridge.dispatch("get_repair_screen", {"repair_token": block["repair_token"]})
    assert shown["scrubbed"] is True
    Image.open(io.BytesIO(base64.b64decode(shown["screen_png_base64"])))
    taught = bridge.dispatch(
        "teach_repair",
        {
            "repair_token": block["repair_token"],
            "demonstration": {
                "action": "click",
                "x": fx.DISMISS_CLICK[0],
                "y": fx.DISMISS_CLICK[1],
            },
        },
    )
    assert taught["status"] == "pending_approval"
    assert screen.clicks == [fx.DISMISS_CLICK]

    # The token works once.
    with pytest.raises(Exception, match="already used"):
        bridge.dispatch(
            "teach_repair",
            {
                "repair_token": block["repair_token"],
                "demonstration": {
                    "action": "click",
                    "x": fx.DISMISS_CLICK[0],
                    "y": fx.DISMISS_CLICK[1],
                },
            },
        )

    # 3. Until a person approves, the workflow still halts.
    assert run("req-0002")["outcome"] == "needs_review"

    store = RepairStore(desk.store_for(WORKFLOW))
    candidate = store.load_candidate(taught["candidate_id"])
    assert candidate.state == "candidate"
    provenance = candidate.provenance
    assert provenance.repaired_by == "calling_agent"
    assert provenance.agent_id == AGENT
    assert provenance.screen_share_mode == "scrubbed_only"
    assert provenance.screens_shared == 1
    assert provenance.screens_scrubbed is True
    assert provenance.scrub_evidence.provider == "test"

    # 4. The agent can't review or approve its own repair, even as a "human".
    with pytest.raises(RepairLifecycleError):
        store.review(candidate.candidate_id, Actor(kind="human", id=AGENT))
    store.review(candidate.candidate_id, PERSON)
    # Flow's production campaigns, with its real resolver and OCR.
    for kind in ("replay", "fault"):
        campaign = _run_campaign(store.load_candidate(candidate.candidate_id), kind)
        assert campaign.passed, [c.detail for c in campaign.cases if not c.passed]
        store.record_campaign(candidate.candidate_id, campaign, AUTOMATION)
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, Actor(kind="human", id=AGENT))
    store.approve(candidate.candidate_id, PERSON, non_interactive=True)
    store.stage(candidate.candidate_id, AUTOMATION)
    store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=1)

    # 5. The approved repair replays without halting, with no model calls.
    passed = run("req-0003")
    assert passed["outcome"] == "done"
    assert passed["model_calls"] == 0
    assert "needs_repair" not in passed
    store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id=passed["run_id"], verified=True, silent_incorrect=False),
    )
    assert store.load_candidate(candidate.candidate_id).state == "active"
