"""Self-repair on MockMed: a fake agent, a real browser, a real record check.

The bundle is the tutorial's triage workflow, recorded against MockMed's
persistence boundary and compiled with its mined record check. The
``redesign`` drift replaces the save control: new label, new DOM id, new
place. The recorded run halts before the save, and these tests cover what
self-repair does next:

* a correct proposal is probed up to the save, approved with one tap on the
  phone portal, and the resumed run is VERIFIED by the record check;
* a wrong proposal fails the probe (or is refused before it) and nothing is
  written;
* nothing is written without a person: no answer, a decline, and the agent
  itself approving are all refused;
* a healthy run never calls the agent and records zero model calls.

The agent is scripted (it reads no model), so CI makes no paid calls.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Iterator, Optional

import pytest

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.repair.candidate import Actor
from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore
from openadapt_flow.repair.self_repair import (
    AppSession,
    RepairProposal,
    RepairRequest,
    ReplayTarget,
    ScreenAccess,
    SelfRepair,
    SelfRepairRefused,
)

pytest.importorskip("playwright.sync_api")

AGENT_ID = "agent:scripted-repair"
QUERY = "?fault=ok&idempotency=demo"


@pytest.fixture(scope="module")
def mockmed(tmp_path_factory) -> Iterator[tuple[str, Path]]:
    """MockMed's persistence boundary and the compiled triage bundle."""

    from openadapt_flow.backends._browser_process import shared_browser
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.tutorial import TutorialError, certify_tutorial, record_tutorial

    root = tmp_path_factory.mktemp("self-repair")
    base_url, _db, stop = serve()
    try:
        with shared_browser(headless=True):
            # On a loaded machine the recorder now and then catches the URL
            # change of the first click, and the compiled bundle then fails
            # the tutorial's certification. Record again, at most three times,
            # until it certifies, as the tutorial would require.
            for attempt in range(3):
                take = root / f"take-{attempt}"
                record_tutorial(base_url, take / "recording")
                workflow = compile_recording(
                    take / "recording",
                    take / "bundle",
                    name="triage",
                    mine_effects=True,
                )
                try:
                    certify_tutorial(workflow)
                    break
                except TutorialError:
                    if attempt == 2:
                        raise
            yield base_url, take / "bundle"
    finally:
        stop()


def _records(base_url: str) -> list[dict]:
    from openadapt_flow.tutorial import _records

    return _records(base_url)


def _reset(base_url: str) -> None:
    from openadapt_flow.tutorial import _http_json

    _http_json(f"{base_url}/api/reset", method="POST", body={})


def _target(base_url: str, drift: Optional[str]) -> ReplayTarget:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from openadapt_flow.tutorial import TUTORIAL_NOTE

    def url(fault: Optional[str]) -> str:
        drifts = ",".join(d for d in (drift, fault) if d)
        return f"{base_url}/{QUERY}" + (f"&drift={drifts}" if drifts else "") + "#tasks"

    def open_session(fault: Optional[str]) -> AppSession:
        backend, close = PlaywrightBackend.launch(url(fault), headless=True)
        return AppSession(backend=backend, close=close)

    return ReplayTarget(
        open_session,
        effect_verifier=RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        ),
        policy="clinical-write",
        params={"note": TUTORIAL_NOTE},
        entry_url=url(None),
        execution_origin=base_url,
    )


def _center_of(base_url: str, selector: str) -> tuple[int, int]:
    """Where ``selector`` sits on the redesigned encounter screen."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    backend, close = PlaywrightBackend.launch(
        f"{base_url}/{QUERY}&drift=redesign#encounter", headless=True
    )
    try:
        locator = backend.page.locator(selector)
        locator.wait_for(state="visible")
        box = locator.bounding_box()
        assert box is not None
        return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    finally:
        close()


class ScriptedAgent:
    """Proposes the given points in order, as Anthropic ``left_click`` actions.

    Stands in for a computer-use agent: it reports one model call and a cost
    per proposal so the ledger can be checked, and calls no model.
    """

    dialect = "anthropic"
    agent_id = AGENT_ID
    requires_model_key = False

    def __init__(self, *points: tuple[int, int]) -> None:
        self.points = list(points)
        self.requests: list[RepairRequest] = []

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        self.requests.append(request)
        if not self.points:
            return None
        x, y = self.points.pop(0)
        return RepairProposal(
            action={"action": "left_click", "coordinate": [x, y]},
            model_calls=1,
            cost_usd=0.0125,
        )


def _phone(root: Path, *, approve: bool, seen: list[int], base_url: str):
    """A ``sleep`` that answers the waiting request through the phone portal.

    This is the decision portal's own code path (``decide_save_approval``),
    so the answer is a person's, on the ``decision_portal`` route. It records
    how many records the system of record held when the person was asked.
    """

    from openadapt_flow.console.save_approvals import (
        SaveApprovalActionRequest,
        decide_save_approval,
        list_save_approvals,
        save_approval_detail,
    )

    def sleep(_seconds: float) -> None:
        for row in list_save_approvals(root):
            seen.append(len(_records(base_url)))
            detail = save_approval_detail(root, row.request_id)
            decide_save_approval(
                root,
                row.request_id,
                SaveApprovalActionRequest(
                    action="verify_and_resume" if approve else "reject",
                    task_digest=detail["task_digest"],
                    task_signature=detail["task"]["signature"],
                    decline_reason=None if approve else "screen_problem",
                ),
            )

    return sleep


def _self_repair(
    base_url: str,
    tmp_path: Path,
    agent: ScriptedAgent,
    *,
    drift: Optional[str] = "redesign",
    **kwargs,
) -> SelfRepair:
    kwargs.setdefault("fault_probes", ("missing",))
    return SelfRepair(
        agent,
        _target(base_url, drift),
        store=tmp_path / "repair-store",
        screen_access=ScreenAccess(enabled_by="ops-lead", mode="allow_unscrubbed"),
        **kwargs,
    )


@contextlib.contextmanager
def _browser() -> Iterator[None]:
    from openadapt_flow.backends._browser_process import shared_browser

    with shared_browser(headless=True):
        yield


def test_healthy_run_never_calls_the_agent(mockmed, tmp_path) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    agent = ScriptedAgent((1, 1))
    with _browser():
        result = _self_repair(base_url, tmp_path, agent, drift=None).run(
            bundle, tmp_path / "run"
        )
    assert result.status == "not_needed"
    assert result.report.execution_outcome == "VERIFIED"
    assert agent.requests == []
    assert result.report.model_calls == 0
    on_disk = RunReport.model_validate_json(
        (tmp_path / "run" / "report.json").read_text()
    )
    assert on_disk.self_repair is not None
    assert on_disk.self_repair.status == "not_needed"
    assert on_disk.self_repair.model_calls == 0
    assert on_disk.self_repair.est_model_cost_usd == 0.0
    assert len(_records(base_url)) == 1


def test_renamed_button_is_repaired_approved_on_the_phone_and_verified(
    mockmed, tmp_path
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    with _browser():
        submit = _center_of(base_url, "#submit-encounter")
        agent = ScriptedAgent(submit)
        seen: list[int] = []
        run_dir = tmp_path / "run"
        repair = _self_repair(
            base_url,
            tmp_path,
            agent,
            approval_wait_s=60,
            sleep=_phone(run_dir, approve=True, seen=seen, base_url=base_url),
        )
        result = repair.run(bundle, run_dir)

    # The recorded run halted before the save, and nothing was written then.
    assert result.halted_report is not None
    assert result.halted_report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert result.halted_report.halt.state_id == "step_005"
    # The agent saw the step, the recorded evidence and the live screen.
    request = agent.requests[0]
    assert request.step_id == "step_005"
    assert request.recorded_label == "Save Encounter"
    assert request.recorded_crop_png and request.live_png
    # The person was asked while the system of record was still empty.
    assert seen == [0]
    # The resumed run is verified by the independent record check.
    assert result.status == "resumed", result.evidence.reason
    assert result.report.execution_outcome == "VERIFIED"
    assert result.report.transaction_outcome == "VERIFIED"
    assert result.report.model_calls == 0
    assert len(_records(base_url)) == 1
    # Every stage, and the agent's model calls and cost, are on the report.
    evidence = result.report.self_repair
    assert evidence is not None
    assert evidence.status == "resumed"
    assert evidence.model_calls == 1
    assert evidence.est_model_cost_usd == pytest.approx(0.0125)
    assert [s.stage for s in evidence.steps] == [
        "halt",
        "propose",
        "patch",
        "probe",
        "campaign",
        "campaign",
        "approval",
        "resume",
    ]
    assert all(s.passed for s in evidence.steps)
    assert evidence.approved_by == "decision_portal:person"
    on_disk = RunReport.model_validate_json(
        (result.run_dir / "report.json").read_text()
    )
    assert on_disk.self_repair == evidence
    ledger = json.loads((run_dir / "self-repair" / "self-repair.json").read_text())
    assert ledger["status"] == "resumed"
    # The candidate went through the governed lifecycle under the person.
    candidate = RepairStore(tmp_path / "repair-store").load_candidate(
        evidence.candidate_id
    )
    assert candidate.source == "model_suggestion"
    assert candidate.provenance is not None
    assert candidate.provenance.repaired_by == "self_repair"
    assert candidate.provenance.agent_id == AGENT_ID
    assert candidate.state == "canary"
    assert candidate.reviewed_by == "decision_portal:person"
    assert candidate.approval.approved_by == "decision_portal:person"
    assert candidate.replay_campaign.passed and candidate.fault_campaign.passed
    assert [t.to_state for t in candidate.history] == [
        "reviewed",
        "replay_passed",
        "fault_passed",
        "approved",
        "staged",
        "canary",
    ]
    assert candidate.canary_metrics.runs_completed == 1
    patched = Workflow.load(result.patched_bundle)
    step = next(s for s in patched.steps if s.id == "step_005")
    assert step.anchor.structural.selector == "#submit-encounter"
    assert (
        step.anchor.identity_template
        == next(
            s for s in Workflow.load(bundle).steps if s.id == "step_005"
        ).anchor.identity_template
    )


def test_wrong_proposals_are_refused_and_nothing_is_written(mockmed, tmp_path) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    with _browser():
        consult = _center_of(base_url, "#type-consult")
        feedback = _center_of(base_url, "#submit-feedback")
        agent = ScriptedAgent(consult, feedback)
        result = _self_repair(base_url, tmp_path, agent, max_attempts=2).run(
            bundle, tmp_path / "run"
        )

    assert result.status == "no_fix"
    stages = [(s.stage, s.passed, s.attempt) for s in result.evidence.steps]
    assert stages == [
        ("halt", True, None),
        ("propose", True, 1),
        ("patch", False, 1),
        ("propose", True, 2),
        ("patch", True, 2),
        ("probe", False, 2),
    ]
    # "Consult" does not read as a save: refused before any probe.
    assert "does not read as a save" in result.evidence.steps[2].detail
    # "Submit feedback" reads as a save but belongs to no patient: the
    # probe's record check stops it before the save.
    assert "save" in result.evidence.steps[5].detail
    # The second request told the agent why the first was refused.
    assert agent.requests[1].previous_refusals
    assert result.evidence.model_calls == 2
    assert len(_records(base_url)) == 0
    assert RepairStore(tmp_path / "repair-store").list_candidates() == []
    assert not (tmp_path / "run" / "item-approval").exists()


def test_nothing_is_written_without_a_person(mockmed, tmp_path) -> None:
    base_url, bundle = mockmed
    submit_store = tmp_path / "repair-store"
    with _browser():
        submit = _center_of(base_url, "#submit-encounter")

        # Nobody answers: the candidate waits, the request stays open.
        _reset(base_url)
        waiting = _self_repair(
            base_url, tmp_path, ScriptedAgent(submit), approval_wait_s=0
        ).run(bundle, tmp_path / "run-wait")
        assert waiting.status == "awaiting_approval"
        assert len(_records(base_url)) == 0
        assert waiting.candidate is not None
        assert waiting.candidate.state == "candidate"
        assert waiting.approval_request_id is not None
        assert (tmp_path / "run-wait" / "item-approval").is_dir()

        # The agent can never review or approve its own patch.
        store = RepairStore(submit_store)
        with pytest.raises(RepairLifecycleError):
            store.review(
                waiting.candidate.candidate_id, Actor(kind="human", id=AGENT_ID)
            )
        with pytest.raises(RepairLifecycleError):
            store.review(
                waiting.candidate.candidate_id, Actor(kind="model", id="any-model")
            )

        # A person declines on the phone: nothing is written, the candidate
        # is rejected.
        _reset(base_url)
        seen: list[int] = []
        declined = _self_repair(
            base_url,
            tmp_path,
            ScriptedAgent(submit),
            approval_wait_s=60,
            sleep=_phone(
                tmp_path / "run-decline", approve=False, seen=seen, base_url=base_url
            ),
        ).run(bundle, tmp_path / "run-decline")
    assert declined.status == "declined"
    assert seen == [0]
    assert len(_records(base_url)) == 0
    assert declined.candidate is not None
    assert declined.candidate.state == "rejected"


def test_a_model_calling_agent_needs_the_customers_key(tmp_path) -> None:
    class ModelAgent:
        dialect = "anthropic"
        agent_id = "agent:model"

        def propose(self, request):  # pragma: no cover - never reached
            raise AssertionError

    with pytest.raises(SelfRepairRefused, match="customer's own"):
        SelfRepair(
            ModelAgent(),
            _target("http://127.0.0.1:9", None),
            store=tmp_path / "store",
            screen_access=ScreenAccess(enabled_by="ops-lead"),
        )
    with pytest.raises(SelfRepairRefused, match="ask_each_time"):
        SelfRepair(
            ScriptedAgent(),
            _target("http://127.0.0.1:9", None),
            store=tmp_path / "store",
            screen_access=ScreenAccess(enabled_by="ops-lead", mode="ask_each_time"),
        )


def test_only_the_ladders_own_refusals_are_repaired(mockmed) -> None:
    """A decline, a record mismatch or a found target is never repaired."""

    from openadapt_flow.ir import HaltObservation, IdentityCheck, Resolution, StepResult
    from openadapt_flow.repair.self_repair import _repairable

    _base_url, bundle = mockmed
    workflow = Workflow.load(bundle)

    def report(**halted) -> RunReport:
        done = [
            StepResult(step_id=s.id, intent=s.intent, ok=True, delivery_attempted=True)
            for s in workflow.steps[:5]
        ]
        last = StepResult(
            step_id="step_005",
            intent="click 'Save Encounter'",
            ok=False,
            delivery_attempted=False,
            **halted,
        )
        return RunReport(
            workflow_name="triage",
            started_at="2026-10-18T00:00:00Z",
            results=[*done, last],
            halt=HaltObservation(state_id="step_005", reason=last.error or ""),
        )

    def geometry() -> Resolution:
        return Resolution(rung="geometry", point=(1, 1), confidence=0.5, elapsed_ms=0)

    not_found = "Could not resolve target for step 'step_005' (x): all rungs failed"
    below = "Step 'step_005' is irreversible but only resolved via the 'geometry' rung"
    assert not isinstance(_repairable(report(error=not_found), workflow), str)
    assert not isinstance(
        _repairable(report(error=below, resolution=geometry()), workflow), str
    )
    declined = report(
        error="Approval mode: a person declined this save (wrong_values). "
        "This save was not made. Nothing was retried."
    )
    assert "another reason" in _repairable(declined, workflow)
    found = report(
        error="postcondition failed",
        resolution=Resolution(
            rung="structural", point=(1, 1), confidence=1.0, elapsed_ms=0
        ),
    )
    assert "found its target" in _repairable(found, workflow)
    mismatch = report(
        error=not_found,
        identity=IdentityCheck(status="mismatch", mode="structured"),
    )
    assert "wrong-record" in _repairable(mismatch, workflow)
    delivered = report(error=not_found)
    delivered.results[-1].delivery_attempted = None
    assert "may have reached" in _repairable(delivered, workflow)


class GivesUpWithCost(ScriptedAgent):
    """Gives up on every attempt, reporting the model call each decision took."""

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        self.requests.append(request)
        return RepairProposal(model_calls=1, cost_usd=0.02)


def test_a_give_up_still_counts_its_model_calls(mockmed, tmp_path) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    agent = GivesUpWithCost()
    with _browser():
        result = _self_repair(base_url, tmp_path, agent).run(bundle, tmp_path / "run")
    assert result.status == "no_fix"
    assert result.evidence.reason == "the agent gave up"
    # The first give-up ends the search, and its call is in the ledger.
    assert len(agent.requests) == 1
    assert result.evidence.model_calls == 1
    assert result.evidence.est_model_cost_usd == 0.02
    assert _records(base_url) == []
