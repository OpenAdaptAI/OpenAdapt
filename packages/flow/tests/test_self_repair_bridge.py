"""Self-repair across a short bridge of new steps, on MockMed.

Two vendor changes need more than one corrected click:

* ``?drift=menu``: the save moved behind a menu (Actions > Billing > Submit
  encounter). The bridge opens the menu and re-targets the save.
* ``?drift=provider``: a new required "Ordering provider" screen appears
  after New Encounter. The bridge chooses a provider and continues; the
  encounter form is then the screen New Encounter expected.

Each bridge is probed up to the save in a fresh session, approved by one
person on the phone portal, and the resumed run is VERIFIED by the record
check. The refusals cover a bridge that would save, one that types an
invented value, and one longer than three actions. The agent is scripted and
uses CSS selectors (the ``generic`` dialect), so CI makes no model calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.self_repair import RepairProposal, RepairRequest
from tests.test_self_repair import (  # noqa: F401 - the shared MockMed fixture
    _browser,
    _phone,
    _records,
    _reset,
    _self_repair,
    mockmed,
)

pytest.importorskip("playwright.sync_api")


def click(selector: str) -> dict:
    return {"kind": "click", "target": selector}


class BridgeAgent:
    """Proposes the given bridges in order, one per attempt."""

    dialect = "generic"
    agent_id = "agent:scripted-bridge"
    requires_model_key = False

    def __init__(self, *bridges: Sequence[dict]) -> None:
        self.bridges = [list(b) for b in bridges]
        self.requests: list[RepairRequest] = []

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        self.requests.append(request)
        if not self.bridges:
            return None
        return RepairProposal(actions=self.bridges.pop(0), model_calls=1)


def test_a_save_moved_into_a_menu_is_bridged_and_verified(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    agent = BridgeAgent(
        [click("#actions-menu"), click("#menu-billing"), click("#menu-submit")]
    )
    run_dir = tmp_path / "run"
    seen: list[int] = []
    with _browser():
        result = _self_repair(
            base_url,
            tmp_path,
            agent,
            drift="menu",
            approval_wait_s=60,
            sleep=_phone(run_dir, approve=True, seen=seen, base_url=base_url),
        ).run(bundle, run_dir)

    assert agent.requests[0].mode == "replace"
    assert agent.requests[0].max_actions == 3
    assert result.status == "resumed", result.evidence.reason
    assert result.report.execution_outcome == "VERIFIED"
    assert seen == [0]
    assert len(_records(base_url)) == 1
    patched = Workflow.load(result.patched_bundle)
    ids = [s.id for s in patched.steps]
    assert ids[-3:] == ["step_005_bridge_1", "step_005_bridge_2", "step_005"]
    by_id = {s.id: s for s in patched.steps}
    assert by_id["step_005_bridge_1"].anchor.structural.selector == "#actions-menu"
    assert by_id["step_005_bridge_2"].anchor.structural.selector == "#menu-billing"
    assert by_id["step_005"].anchor.structural.selector == "#menu-submit"
    assert all(by_id[i].risk == "reversible" for i in ids[-3:-1])
    probe = next(s for s in result.evidence.steps if s.stage == "probe")
    assert "target_found" in probe.detail
    candidate = RepairStore(tmp_path / "repair-store").load_candidate(
        result.evidence.candidate_id
    )
    assert {t.step_id for t in candidate.taught_steps} == {
        "step_005_bridge_1",
        "step_005_bridge_2",
    }
    assert candidate.state == "canary"


def test_a_new_required_dialog_is_bridged_and_verified(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    agent = BridgeAgent([click("#provider-1"), click("#provider-use")])
    run_dir = tmp_path / "run"
    seen: list[int] = []
    with _browser():
        result = _self_repair(
            base_url,
            tmp_path,
            agent,
            drift="provider",
            approval_wait_s=60,
            sleep=_phone(run_dir, approve=True, seen=seen, base_url=base_url),
        ).run(bundle, run_dir)

    request = agent.requests[0]
    assert request.step_id == "step_001"
    assert request.mode == "after"
    assert request.expected_texts
    assert result.status == "resumed", result.evidence.reason
    assert result.report.execution_outcome == "VERIFIED"
    assert seen == [0]
    assert len(_records(base_url)) == 1
    patched = Workflow.load(result.patched_bundle)
    ids = [s.id for s in patched.steps]
    at = ids.index("step_001")
    assert ids[at + 1 : at + 3] == ["step_001_bridge_1", "step_001_bridge_2"]
    by_id = {s.id: s for s in patched.steps}
    original = {s.id: s for s in Workflow.load(bundle).steps}
    # The screen New Encounter expected now comes after the bridge, and New
    # Encounter itself now expects the new dialog.
    assert by_id["step_001_bridge_2"].expect == original["step_001"].expect
    assert by_id["step_001"].expect
    assert by_id["step_001"].expect != original["step_001"].expect
    # Each new click checks the recorded record's identity.
    assert by_id["step_001_bridge_1"].anchor.identity_template is not None
    assert by_id["step_001_bridge_1"].expect
    assert by_id["step_001"].anchor == original["step_001"].anchor


def test_bridges_that_save_invent_values_or_run_long_are_refused(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    agent = BridgeAgent(
        # Longer than three actions: refused before anything is performed.
        [
            click("#provider-1"),
            click("#provider-note"),
            click("#provider-1"),
            click("#provider-use"),
        ],
        # Would click a control that reads as a save.
        [click("#provider-1"), click("#provider-draft")],
        # Types text that is neither a parameter's value nor on the screen.
        [
            click("#provider-note"),
            {"kind": "type", "text": "Please expedite"},
            click("#provider-use"),
        ],
    )
    with _browser():
        result = _self_repair(
            base_url, tmp_path, agent, drift="provider", max_attempts=3
        ).run(bundle, tmp_path / "run")

    assert result.status == "no_fix"
    refused = [s.detail for s in result.evidence.steps if s.stage == "patch"]
    assert len(refused) == 3
    assert "at most 3 action(s)" in refused[0]
    assert "reads as a save" in refused[1]
    assert "invented value" in refused[2]
    # Each attempt after one that acted started from the real halt again.
    assert (tmp_path / "run" / "self-repair" / "attempt-3" / "halt").is_dir()
    assert agent.requests[2].previous_refusals == tuple(refused[:2])
    assert len(_records(base_url)) == 0
    assert RepairStore(tmp_path / "repair-store").list_candidates() == []
    assert not (tmp_path / "run" / "item-approval").exists()
