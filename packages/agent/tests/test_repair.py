"""needs_repair, screen access modes, and teach_repair, without a real app.

The calling agent can never change the access setting, approve screen
sharing, or approve its own repair. Each test drives the MCP tool surface
through :class:`AgentBridge.dispatch`.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_agent import repair as repair_mod
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.cards import WorkflowCard
from openadapt_agent.cli import main
from openadapt_agent.repair import RepairAccessPolicy, RepairDesk, RepairError, set_access
from openadapt_agent.runner import RunnerConfig
from openadapt_agent.service import CatalogEntry, EngineResult

WORKFLOW = "save_encounter"
AGENT = "calling-agent"


def _png(color=(255, 255, 255), size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


RAW = _png((200, 30, 30))


class FakeScreen:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def screenshot(self) -> bytes:
        return RAW

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def viewport(self) -> tuple[int, int]:
        return (64, 48)


class FakeScrubber:
    """Stands in for openadapt-privacy: paints the whole image black."""

    def validate_ready(self, modalities):
        assert modalities == ["PIL_IMAGE"]

    def scrub_image(self, image):
        return Image.new("RGB", image.size, (0, 0, 0))

    def evidence(self, modalities):
        return {
            "provider": "fake",
            "package_version": "0.0.0",
            "policy_sha256": "ab" * 32,
            "status": "completed",
        }


class BrokenScrubber(FakeScrubber):
    def scrub_image(self, image):
        raise RuntimeError("model missing")


class HaltEngine:
    def __init__(self, tmp: Path, reason: str = "unexpected_screen") -> None:
        self.tmp = tmp
        self.reason = reason

    def execute(self, entry, run_id, inputs, raw) -> EngineResult:
        run_dir = self.tmp / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return EngineResult(
            reason=self.reason,
            halt_step_id="s_verify",
            run_dir=str(run_dir),
            bundle_dir=str(self.tmp / "bundle"),
        )

    def refresh(self, entry, run_id):
        return None


def _bridge(
    tmp: Path,
    mode: Optional[str],
    *,
    engine: Any = None,
    scrubber=FakeScrubber,
    screen: Any = "default",
) -> AgentBridge:
    access = tmp / "access.json"
    if mode is not None:
        set_access(access, workflow=WORKFLOW, mode=mode, enabled_by="admin")
    desk = RepairDesk(
        tmp / ".repair",
        RepairAccessPolicy(access if mode is not None else None),
        demonstrator=FakeScreen() if screen == "default" else screen,
        scrubber_factory=scrubber,
        agent_id=AGENT,
    )
    bridge = AgentBridge(
        None, RunnerConfig(runs_dir=tmp / "runs"), allow_run=True, repair_desk=desk
    )
    card = WorkflowCard(name=WORKFLOW, purpose="Save an encounter.", done_means="Saved.")
    bridge.catalog[WORKFLOW] = CatalogEntry(card=card, engine=engine or HaltEngine(tmp))
    return bridge


def _run(bridge: AgentBridge, request_id: str = "req-0001") -> dict:
    return bridge.dispatch(
        "run_workflow", {"workflow": WORKFLOW, "inputs": {}, "request_id": request_id}
    )


def _tools(bridge: AgentBridge) -> set[str]:
    return {spec.name for spec in bridge.list_tool_specs()}


@pytest.fixture(autouse=True)
def _flow_supports_teach(monkeypatch):
    # Unit tests don't depend on the installed Flow having agent_teach.
    monkeypatch.setattr(repair_mod, "teach_supported", lambda: True)


# -- the setting ---------------------------------------------------------------


def test_access_is_off_by_default_and_when_unreadable(tmp_path: Path) -> None:
    assert RepairAccessPolicy(None).for_workflow(WORKFLOW).mode == "off"
    bad = tmp_path / "access.json"
    bad.write_text("{not json")
    assert RepairAccessPolicy(bad).for_workflow(WORKFLOW).mode == "off"
    bad.write_text(json.dumps({"schema": "wrong", "org": {"screens": "allow_unscrubbed"}}))
    assert RepairAccessPolicy(bad).for_workflow(WORKFLOW).mode == "off"


def test_setting_without_a_person_is_off(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    path.write_text(
        json.dumps(
            {
                "schema": repair_mod.ACCESS_SCHEMA,
                "workflows": {WORKFLOW: {"screens": "scrubbed_only"}},
            }
        )
    )
    assert RepairAccessPolicy(path).for_workflow(WORKFLOW).mode == "off"


def test_workflow_setting_overrides_org(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    set_access(path, workflow=None, mode="scrubbed_only", enabled_by="admin")
    set_access(path, workflow=WORKFLOW, mode="off", enabled_by="admin")
    policy = RepairAccessPolicy(path)
    assert policy.for_workflow("other").mode == "scrubbed_only"
    assert policy.for_workflow(WORKFLOW).mode == "off"


def test_mcp_surface_cannot_change_access_or_approve(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "ask_each_time")
    names = _tools(bridge)
    assert {"get_repair_screen", "teach_repair"} <= names
    assert not any("access" in name or "approve" in name for name in names)
    for name in ("repair_access", "approve_screen", "approve_repair"):
        with pytest.raises(BridgeError):
            bridge.dispatch(name, {})


def test_cli_refuses_allow_unscrubbed_without_a_terminal(tmp_path: Path, capsys) -> None:
    path = tmp_path / "access.json"
    rc = main(
        [
            "repair",
            "access",
            "--workflow",
            WORKFLOW,
            "--screens",
            "allow_unscrubbed",
            "--by",
            "admin",
            "--file",
            str(path),
        ]
    )
    assert rc == 1
    assert not path.exists()


def test_cli_sets_scrubbed_only(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    rc = main(
        [
            "repair",
            "access",
            "--org",
            "--screens",
            "scrubbed_only",
            "--by",
            "admin",
            "--file",
            str(path),
        ]
    )
    assert rc == 0
    assert RepairAccessPolicy(path).for_workflow(WORKFLOW).enabled_by == "admin"


# -- needs_repair ----------------------------------------------------------------


def test_off_returns_needs_repair_without_screens_or_tools(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, None)
    result = _run(bridge)
    block = result["needs_repair"]
    assert result["outcome"] == "needs_review"
    assert block["step_id"] == "s_verify"
    assert block["halt_reason"] == "unexpected_screen"
    assert block["screen_access"] == "off"
    assert block["teach_repair_available"] is False
    assert not any("png" in key or "image" in key for key in block)
    assert not {"get_repair_screen", "teach_repair"} & _tools(bridge)
    with pytest.raises(BridgeError):
        bridge.dispatch("teach_repair", {"repair_token": block["repair_token"]})


def test_uncertain_outcomes_get_no_repair(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "scrubbed_only", engine=HaltEngine(tmp_path, "save_not_confirmed"))
    result = _run(bridge)
    assert result["outcome"] == "not_sure_if_saved"
    assert "needs_repair" not in result


def test_unknown_token_is_refused(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "scrubbed_only")
    with pytest.raises(BridgeError, match="not a token"):
        bridge.dispatch("get_repair_screen", {"repair_token": "x" * 40})


# -- scrubbed_only -------------------------------------------------------------


def test_scrubbed_only_shares_a_scrubbed_screen(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "scrubbed_only")
    block = _run(bridge)["needs_repair"]
    assert block["teach_repair_available"] is True
    assert block["human_approval_required"] is False
    shown = bridge.dispatch("get_repair_screen", {"repair_token": block["repair_token"]})
    image = base64.b64decode(shown["screen_png_base64"])
    assert shown["scrubbed"] is True
    assert image != RAW
    assert Image.open(io.BytesIO(image)).getpixel((5, 5)) == (0, 0, 0)


def test_scrub_failure_shares_nothing(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "scrubbed_only", scrubber=BrokenScrubber)
    token = _run(bridge)["needs_repair"]["repair_token"]
    with pytest.raises(BridgeError, match="no screen was shared"):
        bridge.dispatch("get_repair_screen", {"repair_token": token})


def test_missing_privacy_package_shares_nothing(tmp_path: Path, monkeypatch) -> None:
    def unavailable():
        raise ImportError("openadapt-privacy is not installed")

    bridge = _bridge(tmp_path, "scrubbed_only", scrubber=unavailable)
    token = _run(bridge)["needs_repair"]["repair_token"]
    with pytest.raises(BridgeError, match="no screen was shared"):
        bridge.dispatch("get_repair_screen", {"repair_token": token})


# -- ask_each_time -------------------------------------------------------------


def _desk(bridge: AgentBridge) -> RepairDesk:
    return bridge.repair_desk


def test_ask_each_time_needs_a_person(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "ask_each_time")
    block = _run(bridge)["needs_repair"]
    token = block["repair_token"]
    assert block["human_approval_required"] is True
    assert block["teach_repair_available"] is False
    assert "approve-screen" in block["next_action"]
    with pytest.raises(BridgeError, match="approve"):
        bridge.dispatch("get_repair_screen", {"repair_token": token})
    with pytest.raises(BridgeError, match="approve"):
        bridge.dispatch(
            "teach_repair",
            {"repair_token": token, "demonstration": {"action": "click", "x": 1, "y": 1}},
        )


def test_agent_cannot_approve_its_own_screen_sharing(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "ask_each_time")
    block = _run(bridge)["needs_repair"]
    desk = _desk(bridge)
    sha, record = desk.find(block["token_id"])
    image, evidence = desk.prepare_preview(sha, record)
    with pytest.raises(RepairError, match="other than the calling agent"):
        desk.approve_screen(sha, approved_by=AGENT, image=image, evidence=evidence)


def test_cli_approval_refuses_without_a_terminal(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "ask_each_time")
    block = _run(bridge)["needs_repair"]
    rc = main(
        [
            "repair",
            "approve-screen",
            block["token_id"],
            "--by",
            "reviewer",
            "--repair-access",
            str(tmp_path / "access.json"),
            "--repair-dir",
            str(tmp_path / ".repair"),
        ]
    )
    assert rc == 1
    with pytest.raises(BridgeError):
        bridge.dispatch("get_repair_screen", {"repair_token": block["repair_token"]})


def test_approval_is_bound_to_one_token_and_the_exact_image(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "ask_each_time")
    first = _run(bridge, "req-0001")["needs_repair"]
    second = _run(bridge, "req-0002")["needs_repair"]
    desk = _desk(bridge)
    sha, record = desk.find(first["token_id"])
    image, evidence = desk.prepare_preview(sha, record)
    desk.approve_screen(sha, approved_by="reviewer", image=image, evidence=evidence)

    shown = bridge.dispatch("get_repair_screen", {"repair_token": first["repair_token"]})
    assert base64.b64decode(shown["screen_png_base64"]) == image
    with pytest.raises(BridgeError, match="approve"):
        bridge.dispatch("get_repair_screen", {"repair_token": second["repair_token"]})

    # A changed preview no longer matches what the person approved.
    (tmp_path / ".repair" / "approvals" / f"{sha}.png").write_bytes(RAW)
    with pytest.raises(BridgeError, match="approve"):
        bridge.dispatch("get_repair_screen", {"repair_token": first["repair_token"]})


# -- allow_unscrubbed ------------------------------------------------------------


def test_allow_unscrubbed_shares_raw_and_records_it(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "allow_unscrubbed", scrubber=BrokenScrubber)
    block = _run(bridge)["needs_repair"]
    shown = bridge.dispatch("get_repair_screen", {"repair_token": block["repair_token"]})
    assert shown["scrubbed"] is False
    assert base64.b64decode(shown["screen_png_base64"]) == RAW
    sha, record = _desk(bridge).find(block["token_id"])
    assert record["screens_shared"] == 1
    assert record["screens_scrubbed"] is False


# -- teach_repair input checks -------------------------------------------------


def test_teach_refuses_bad_demonstrations_without_clicking(tmp_path: Path) -> None:
    screen = FakeScreen()
    bridge = _bridge(tmp_path, "scrubbed_only", screen=screen)
    token = _run(bridge)["needs_repair"]["repair_token"]
    for demo in ({"action": "click", "x": 999, "y": 1}, {"action": "type"}):
        with pytest.raises(BridgeError):
            bridge.dispatch("teach_repair", {"repair_token": token, "demonstration": demo})
    assert screen.clicks == []


def test_no_screen_configured_hides_tools(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "scrubbed_only", screen=None)
    assert not {"get_repair_screen", "teach_repair"} & _tools(bridge)
    block = _run(bridge)["needs_repair"]
    assert block["teach_repair_available"] is False


def test_image_modality_matches_openadapt_privacy() -> None:
    privacy = pytest.importorskip("openadapt_privacy.base")
    from openadapt_agent.repair import IMAGE_MODALITY

    assert IMAGE_MODALITY == privacy.Modality.PIL_IMAGE
    provider = pytest.importorskip("openadapt_privacy").PresidioScrubbingProvider
    assert IMAGE_MODALITY in provider.capabilities
