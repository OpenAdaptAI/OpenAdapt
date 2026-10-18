"""Bridge search on a staging copy, scored by read-back, then one approval.

MockMed ``?drift=menu2`` moves the save behind a menu with two save paths:
Actions > Billing > Submit encounter (a Triage encounter, as recorded) and
Actions > Clinical > Submit as Consult. Self-repair's probe stops before the
save, so both bridges pass it. Only the saved record tells them apart.

The search runs both on a separate staging server, writes through the save
there, and reads what landed. The Consult path is refuted; the Billing path
is verified and becomes the one proposal production self-repair sees. A
person approves it on the phone and the resumed production run verifies.
Production gets one record; staging writes never reach it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.staging_search import (
    ScriptedBridges,
    StagingBridgeSearch,
    StagingCopy,
    StagingSearchAgent,
)
from tests.test_self_repair import (  # noqa: F401 - the shared MockMed fixture
    _browser,
    _phone,
    _records,
    _reset,
    _self_repair,
    _target,
    mockmed,
)

pytest.importorskip("playwright.sync_api")

CONSULT = [
    {"kind": "click", "target": "#actions-menu"},
    {"kind": "click", "target": "#menu-clinical"},
    {"kind": "click", "target": "#menu-consult"},
]
BILLING = [
    {"kind": "click", "target": "#actions-menu"},
    {"kind": "click", "target": "#menu-billing"},
    {"kind": "click", "target": "#menu-submit"},
]


@pytest.fixture
def staging():
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.tutorial import TUTORIAL_NOTE

    url, _db, stop = serve()
    base = url.rstrip("/")
    try:
        yield (
            base,
            StagingCopy(
                target=_target(base, "menu2"),
                reset=lambda: _reset(base),
                oracle_recipe={
                    "kind": "rest",
                    "base_url": base,
                    "records_path": "/api/db",
                    "records_key": "records",
                },
                identity={"patient_id": "p1"},
                record={"type": "Triage", "note": TUTORIAL_NOTE},
                fault_probes=("missing",),
            ),
        )
    finally:
        stop()


def test_staging_search_picks_the_bridge_whose_record_verifies(
    mockmed,  # noqa: F811 - the shared fixture
    staging,
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    staging_url, copy = staging
    _reset(base_url)
    proposer = ScriptedBridges(CONSULT, BILLING)
    search = StagingBridgeSearch(proposer, copy, bundle, tmp_path / "search")
    agent = StagingSearchAgent(search)
    run_dir = tmp_path / "run"
    seen: list[int] = []
    with _browser():
        result = _self_repair(
            base_url,
            tmp_path,
            agent,
            drift="menu2",
            approval_wait_s=120,
            sleep=_phone(run_dir, approve=True, seen=seen, base_url=base_url),
        ).run(bundle, run_dir)

    found = agent.result
    assert found is not None
    consult, billing = found.candidates
    # Both reach the save on the right record; self-repair cannot tell them
    # apart before the save.
    assert consult.repair_status == billing.repair_status == "awaiting_approval"
    # The read-back can.
    # The runtime's record check halts the Consult run after its save, and
    # the read-back shows a Consult record on the right patient.
    assert consult.wrong_value and consult.reward == -0.5
    assert billing.outcome == "verified" and billing.reward == 1.0
    assert found.winner_index == 1
    assert (search.search_dir / "search.json").is_file()
    # Nothing on staging is left for a person to approve, and no secrets stay.
    for i in (0, 1):
        cdir = search.search_dir / f"candidate-{i}"
        assert not (cdir / "halt" / "item-approval").exists()
        [staged] = RepairStore(cdir / "store").list_candidates()
        assert staged.state == "rejected"
        assert not (cdir / "reward" / "worker" / "keys" / "ed25519.pem").exists()

    # Production saw one proposal, the winner, and a person approved it.
    assert result.status == "resumed", result.evidence.reason
    assert result.report.execution_outcome == "VERIFIED"
    assert seen == [0]
    records = _records(base_url)
    assert len(records) == 1 and records[0]["type"] == "Triage"
    candidate = RepairStore(tmp_path / "repair-store").load_candidate(
        result.evidence.candidate_id
    )
    assert candidate.state == "canary"
    # The staging copy is a different server: its writes never reached
    # production (the last staging run left its own record there).
    assert staging_url != base_url
    assert len(_records(staging_url)) == 1


def test_no_verified_candidate_means_no_proposal(
    mockmed,  # noqa: F811 - the shared fixture
    staging,
    tmp_path: Path,
) -> None:
    from openadapt_flow.repair.self_repair import RepairRequest

    _base_url, bundle = mockmed
    _staging_url, copy = staging
    search = StagingBridgeSearch(ScriptedBridges(CONSULT), copy, bundle, tmp_path / "s")
    agent = StagingSearchAgent(search)
    request = RepairRequest(
        step_id="step_005",
        intent="Save the encounter",
        action="click",
        halt_reason="Could not resolve target",
        live_png=b"",
        viewport=(1280, 800),
        recorded_label="Save Encounter",
        recorded_region=(0, 0, 10, 10),
        recorded_click_point=(5, 5),
        recorded_crop_png=None,
        dialect="generic",
        attempt=1,
        max_actions=3,
    )
    with _browser():
        assert agent.propose(request) is None
    assert agent.result is not None
    [only] = agent.result.candidates
    assert only.wrong_value and only.reward == -0.5
    # It proposes once; a second request gets nothing.
    assert agent.propose(request) is None
    # A second search under the same work_dir gets its own directory.
    first = search.search_dir
    with _browser():
        again = search.search(request)
    assert again.candidates[0].reward == -0.5
    assert search.search_dir != first


def test_model_bridges_map_labels_to_screen_points(monkeypatch) -> None:
    from dataclasses import dataclass

    from openadapt_flow.integrations.harness2.llm import Completion
    from openadapt_flow.repair.self_repair import RepairRequest
    from openadapt_flow.repair.staging_search import ModelBridges

    @dataclass
    class Line:
        text: str
        region: tuple[int, int, int, int]

    screen = [
        Line("Actions", (100, 500, 60, 20)),
        Line("Billing", (100, 530, 60, 20)),
        Line("Submit encounter", (100, 560, 120, 20)),
    ]
    import sys

    import openadapt_flow.vision.ocr  # noqa: F401 - the package re-exports a function by that name

    monkeypatch.setattr(
        sys.modules["openadapt_flow.vision.ocr"], "ocr", lambda png, **kw: screen
    )

    class Model:
        prompt = ""

        def complete(self, system, user, *, max_tokens=800):
            Model.prompt = user
            return Completion(
                '{"bridges": [["Actions", "Billing", "Submit encounter"],'
                ' ["Actions", "Nowhere"]]}',
                0.002,
            )

    request = RepairRequest(
        step_id="step_005",
        intent="Save the encounter",
        action="click",
        halt_reason="Could not resolve target",
        live_png=b"png",
        viewport=(1280, 800),
        recorded_label="Save Encounter",
        recorded_region=(0, 0, 10, 10),
        recorded_click_point=(5, 5),
        recorded_crop_png=None,
        dialect="generic",
        attempt=1,
        max_actions=3,
    )
    proposer = ModelBridges(Model())
    [bridge] = proposer.bridges(request, "# Trace halted-run\n")
    assert [dict(a) for a in bridge.bridge()] == [
        {"kind": "click", "x": 130, "y": 510},
        {"kind": "click", "x": 130, "y": 540},
        {"kind": "click", "x": 160, "y": 570},
    ]
    assert "# Trace halted-run" in Model.prompt and "Submit encounter" in Model.prompt
    assert proposer.model_calls == 1 and proposer.cost_usd == 0.002
