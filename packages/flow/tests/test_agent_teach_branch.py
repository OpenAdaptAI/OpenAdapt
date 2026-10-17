"""A calling agent's demonstrations on several halts become one branch candidate.

Same rendered dialog fixture as ``test_agent_teach_repair`` (real replayer,
template matcher, OCR, campaigns), but each halt's dialog carries a different
reference number. The branch keys on what the demonstrations share, so after
a person approves it the workflow gets past a dialog with a reference it
never saw.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.repair import Actor, RepairStore
from openadapt_flow.repair import dialog_fixture as fx
from openadapt_flow.repair.agent_teach import (
    AgentTeachRefused,
    teach_branch_from_calling_agent,
)
from openadapt_flow.repair.cli import _run_campaign
from openadapt_flow.repair.lifecycle import RepairLifecycleError
from openadapt_flow.runtime.replayer import Replayer
from tests.test_agent_teach_repair import AGENT_ID, AUTOMATION, PERSON, _provenance

TITLES = ["Survey Required for Aetna", "Survey Required for Cigna"]
UNSEEN = "Survey Required for Humana"


def _replay(bundle: Path, run_dir: Path, title: str):
    app = fx.DialogApp(title=title)
    report = Replayer(app).run(
        Workflow.load(bundle), bundle_dir=bundle, run_dir=run_dir
    )
    return report, app


def _demonstration(root: Path, title: str) -> Path:
    screen = fx.DialogApp(title=title, saved=True)
    before = screen.screenshot()
    screen.click(*fx.DISMISS_CLICK)
    return fx.record_click(root, before, screen.screenshot(), fx.DISMISS_CLICK)


def _examples(tmp_path: Path, titles):
    bundle = fx.base_bundle(tmp_path / "bundle")
    pairs = []
    for i, title in enumerate(titles):
        run = tmp_path / f"run{i}"
        report, _ = _replay(bundle, run, title)
        assert report.halt is not None
        pairs.append((run, _demonstration(tmp_path / f"rec{i}", title)))
    return bundle, pairs


def test_two_demonstrations_become_one_approved_branch(tmp_path):
    bundle, pairs = _examples(tmp_path, TITLES)
    result = teach_branch_from_calling_agent(
        pairs, tmp_path / "proposed", bundle=bundle, provenance=_provenance()
    )
    candidate = result.candidate
    assert candidate.state == "candidate"
    assert candidate.provenance is not None
    assert candidate.provenance.agent_id == AGENT_ID

    store = RepairStore(tmp_path / "store")
    candidate = store.add_candidate(candidate)
    with pytest.raises(RepairLifecycleError):
        store.review(candidate.candidate_id, Actor(kind="human", id=AGENT_ID))
    store.review(candidate.candidate_id, PERSON)
    for kind in ("replay", "fault"):
        campaign = _run_campaign(store.load_candidate(candidate.candidate_id), kind)
        assert campaign.passed, [c.detail for c in campaign.cases if not c.passed]
        store.record_campaign(candidate.candidate_id, campaign, AUTOMATION)
    store.approve(candidate.candidate_id, PERSON, non_interactive=True)
    store.stage(candidate.candidate_id, AUTOMATION)
    store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=1)

    active = store.bundle_path(store.active_pointer().active_digest)
    report, app = _replay(active, tmp_path / "unseen", UNSEEN)
    assert report.success and app.dialog is False, (
        report.halt,
        [(r.step_id, r.ok, r.error) for r in report.results],
    )


def test_one_demonstration_is_not_a_branch(tmp_path):
    bundle, pairs = _examples(tmp_path, TITLES[:1])
    with pytest.raises(AgentTeachRefused, match="at least two"):
        teach_branch_from_calling_agent(
            pairs, tmp_path / "proposed", bundle=bundle, provenance=_provenance()
        )
    assert not (tmp_path / "proposed").exists()


def test_identity_is_the_band_every_dialog_shares(tmp_path):
    from openadapt_flow.repair.agent_teach import shared_dialog_band

    _, pairs = _examples(tmp_path, TITLES)
    band = shared_dialog_band([rec for _, rec in pairs])
    assert band is not None
    x, y, w, h = band
    # The band holds the click and leaves out the title line that varies.
    assert y <= fx.DISMISS_CLICK[1] < y + h
    assert y > 95
