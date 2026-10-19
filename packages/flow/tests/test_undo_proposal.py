"""Proposed undo steps, verified on a test copy (docs/AFTER_A_STOP.md).

End to end against the REAL MockMed record store (the in-process
``fault_server``) and a small field-edit store, with the REAL Replayer, the
REST record read-back and the REST API actuator. The screen and keyboard are
scripted, as in ``test_after_stop_mockmed``: each Enter saves, each Delete
removes, so every verdict reads the actual records.

The story: ``undo propose`` finds an undo for each saving step, tries it on
the test copy, and writes only the ones that pass, as proposals. A proposal
is offered after a stop only once a person has approved it, and each run of
it still needs that person's approval then.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from openadapt_flow.after_stop import (
    UndoNotOffered,
    UndoRefused,
    approve_undo,
    build_change_ledger,
    keep_prior_state,
    offer_undos,
    prior_state_path,
    resolve_undo_params,
    run_undo,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    UndoStep,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.undo_proposal import (
    Sources,
    accept_proposal,
    app_ui_controls,
    compare_snapshots,
    find_candidates,
    network_candidates,
    proposal_problem,
    propose_undos,
    write_proposals,
)
from tests.test_replayer import FakeBackend, FakeVision, Match

# ---------------------------------------------------------------------------
# MockMed: a create (save encounter) and its inverse (delete it)
# ---------------------------------------------------------------------------


def _records(url: str) -> list[dict]:
    return requests.get(f"{url}/api/db", timeout=5).json()["records"]


class SavingBackend(FakeBackend):
    """Each Enter saves the next encounter kind; Delete removes p1's Triage
    rows (all of them, which is what makes it a bad undo)."""

    kinds = ("Triage", "Vitals")

    def __init__(self, url: str):
        super().__init__(viewport=(300, 200))
        self.url = url
        self.saves = 0

    def press(self, key):
        super().press(key)
        if key == "Enter":
            kind = self.kinds[self.saves % len(self.kinds)]
            self.saves += 1
            requests.post(
                f"{self.url}/api/encounter",
                json={"patient_id": "p1", "type": kind, "note": "n"},
                timeout=5,
            )
        elif key == "Delete":
            for row in _records(self.url):
                if row["patient_id"] == "p1" and row["type"] == "Triage":
                    requests.delete(f"{self.url}/api/encounter/{row['id']}", timeout=5)


def _vision() -> FakeVision:
    vision = FakeVision()
    hit = Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    vision.text_results = {"Saved": hit, "Deleted": hit}
    return vision


def _save(step_id: str, kind: str) -> Step:
    return Step(
        id=step_id,
        intent=f"save {kind} encounter",
        action=ActionKind.KEY,
        key="Enter",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
            )
        ],
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": "p1", "type": kind},
                expected_count=1,
                count_new_only=True,
                risk="irreversible",
                timeout_s=1.0,
            )
        ],
    )


def _workflow() -> Workflow:
    return Workflow(
        name="mockmed-two-saves",
        steps=[_save("save_triage", "Triage"), _save("save_vitals", "Vitals")],
    )


def _network(path: str = "/api/encounter") -> list[dict]:
    """What ``record --capture-network`` keeps for the two saves."""
    lines = []
    for n, kind in enumerate(("Triage", "Vitals"), start=1):
        lines.append(
            {
                "method": "POST",
                "url": f"http://127.0.0.1:1{path}",
                "request_type": "application/json",
                "body": json.dumps({"patient_id": "p1", "type": kind, "note": "n"}),
                "status": 200,
                "response_type": "application/json",
                "response_text": json.dumps({"ok": True, "id": n}),
            }
        )
    return lines


class MockMedCopy:
    """MockMed as a resettable test copy, driven like ``undo run`` drives it."""

    name = "mockmed-test-copy"

    def __init__(self, url: str, *, seed: list[dict] | None = None):
        self.url = url
        self.seed = seed or []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1
        requests.post(f"{self.url}/api/reset", json={}, timeout=5)
        for row in self.seed:
            requests.post(f"{self.url}/api/encounter", json=row, timeout=5)

    def snapshot(self):
        return _records(self.url)

    def read_records(self):
        return _records(self.url)

    def run(self, workflow, *, bundle_dir, run_dir, params):
        replayer = Replayer(
            SavingBackend(self.url),
            vision=_vision(),
            effect_verifier=RestRecordVerifier(self.url),
            api_actuator=ApiActuator(self.url),
            poll_interval_s=0.01,
        )
        return replayer.run(
            workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
        )


@pytest.fixture
def mockmed():
    url, _db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _bundle(tmp_path: Path, workflow: Workflow) -> Path:
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    return bundle


def test_network_undo_is_verified_written_as_a_proposal_and_waits_for_a_person(
    tmp_path, mockmed
):
    workflow = _workflow()
    bundle = _bundle(tmp_path, workflow)
    target = MockMedCopy(mockmed)

    report, verified = propose_undos(
        Workflow.load(bundle),
        bundle_dir=bundle,
        sources=Sources(network_lines=_network()),
        target=target,
        work_dir=tmp_path / "work",
    )

    assert report.summary() == {
        "saving_steps": 2,
        "verified_undo": 2,
        "no_verified_undo": 0,
    }
    triage = report.saving_steps[0]
    assert triage.verified_undo == "undo-save_triage-network-1"
    [result] = triage.candidates
    assert result.status == "verified", result.reason
    assert "REST convention gives DELETE /api/encounter/{undo_record_id}" in (
        result.basis
    )
    evidence = result.evidence
    assert evidence["before"]["sha256"] == evidence["after_undo"]["sha256"]
    assert evidence["after_save"]["sha256"] != evidence["before"]["sha256"]
    # The second save's "before" is the state the first save leaves.
    assert report.saving_steps[1].candidates[0].evidence["earlier_saves"] == 1
    # PHI-free: no record value in the evidence or the report.
    dumped = json.dumps(report.model_dump(mode="json"))
    assert "p1" not in dumped and "Triage" not in json.dumps(evidence["before"])
    # The copy is left at its baseline.
    assert _records(mockmed) == []

    written = write_proposals(bundle, verified)
    proposals = {u.undoes: u for u in written.undo_steps}
    undo = proposals["save_triage"]
    assert undo.source == "synthesized"
    assert undo.awaiting_approval
    assert undo.proposal.candidate_source == "network"
    assert (bundle / undo.proposal.evidence_path).is_file()
    assert proposal_problem(bundle, undo) is None

    # A stop now: the Triage row is saved, the run halts at step 2.
    run_wf = Workflow.load(bundle)
    run_dir = tmp_path / "run"
    stopping = Replayer(
        _RejectSecond(mockmed),
        vision=_vision(),
        effect_verifier=RestRecordVerifier(mockmed),
        poll_interval_s=0.01,
    )
    stopped = stopping.run(run_wf, bundle_dir=bundle, run_dir=run_dir)
    assert not stopped.success
    assert [r["type"] for r in _records(mockmed)] == ["Triage"]
    # The replayer kept the ids that matched before the save (none).
    assert prior_state_path(run_dir, "save_triage").is_file()

    # A proposal nobody approved is not offered, and the ledger names none.
    assert offer_undos(run_dir, run_wf, bundle_dir=bundle) == []
    assert build_change_ledger(stopped, run_wf)[0].undo_id is None
    with pytest.raises(UndoNotOffered):
        approve_undo(run_dir, run_wf, undo.id, approver="nurse-a", bundle_dir=bundle)

    # A person approves the proposal once; the bundle is re-sealed.
    accepted = accept_proposal(bundle, undo.id, approver="nurse-a")
    assert not next(u for u in accepted.undo_steps if u.id == undo.id).awaiting_approval
    offers = offer_undos(run_dir, accepted, bundle_dir=bundle)
    assert [o.undo_id for o in offers] == [undo.id]

    # Still no undo without the at-the-time approval.
    with pytest.raises(UndoRefused):
        run_undo(
            run_dir,
            accepted,
            undo.id,
            replayer=_undo_replayer(mockmed),
            bundle_dir=bundle,
        )
    approve_undo(run_dir, accepted, undo.id, approver="nurse-a", bundle_dir=bundle)
    result = run_undo(
        run_dir,
        accepted,
        undo.id,
        replayer=_undo_replayer(mockmed),
        bundle_dir=bundle,
    )
    assert result.status == "verified", result.reason
    assert _records(mockmed) == []


class _RejectSecond(SavingBackend):
    def press(self, key):
        if key == "Enter" and self.saves == 1:
            FakeBackend.press(self, key)
            self.saves += 1
            requests.post(
                f"{self.url}/api/encounter?fault=optimistic",
                json={"patient_id": "p1", "type": "Vitals", "note": "n"},
                timeout=5,
            )
            return
        super().press(key)


def _undo_replayer(url: str) -> Replayer:
    return Replayer(
        SavingBackend(url),
        vision=_vision(),
        effect_verifier=RestRecordVerifier(url),
        api_actuator=ApiActuator(url),
        poll_interval_s=0.01,
    )


def test_a_wrong_endpoint_fails_with_its_reason_and_is_never_written(
    tmp_path, mockmed
):
    workflow = Workflow(name="one-save", steps=[_save("save_triage", "Triage")])
    bundle = _bundle(tmp_path, workflow)
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(network_lines=_network("/api/encounter/new")),
        target=MockMedCopy(mockmed),
        work_dir=tmp_path / "work",
    )
    [row] = report.saving_steps
    assert row.verified_undo is None
    [candidate] = row.candidates
    assert candidate.status == "failed"
    assert "1 record(s) added since before the save remain" in candidate.reason
    assert verified == {}
    assert Workflow.load(bundle).undo_steps == []
    assert _records(mockmed) == []


def _delete_demo(tmp_path: Path) -> Path:
    demo = Workflow(
        name="delete triage encounter",
        steps=[
            Step(
                id="press_delete",
                intent="delete the Triage encounter",
                action=ActionKind.KEY,
                key="Delete",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Deleted",
                        timeout_s=0.2,
                    )
                ],
                risk="irreversible",
            )
        ],
    )
    path = tmp_path / "delete-demo"
    demo.save(path)
    return path


def test_recording_undo_that_also_deletes_another_record_fails_the_collateral_guard(
    tmp_path, mockmed
):
    workflow = Workflow(name="one-save", steps=[_save("save_triage", "Triage")])
    bundle = _bundle(tmp_path, workflow)
    # Another Triage row for the patient was already there. The recorded
    # delete removes every Triage row, so it takes that one too.
    seed = [{"patient_id": "p1", "type": "Triage", "note": "earlier"}]
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(recordings=[_delete_demo(tmp_path)]),
        target=MockMedCopy(mockmed, seed=seed),
        work_dir=tmp_path / "work",
    )
    [row] = report.saving_steps
    [candidate] = row.candidates
    assert candidate.source == "recording"
    assert candidate.status == "failed"
    assert "1 record(s) from before the save are gone" in candidate.reason
    assert "the save never touched (collateral)" in candidate.reason
    assert verified == {}


def test_recording_undo_passes_when_nothing_else_is_touched(tmp_path, mockmed):
    workflow = Workflow(name="one-save", steps=[_save("save_triage", "Triage")])
    bundle = _bundle(tmp_path, workflow)
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(recordings=[_delete_demo(tmp_path)]),
        target=MockMedCopy(mockmed),
        work_dir=tmp_path / "work",
    )
    [row] = report.saving_steps
    assert row.candidates[0].status == "verified", row.candidates[0].reason
    undo = verified["save_triage"][0].undo
    assert undo.source == "demonstration"
    # The recorded steps declared no effect, so the save's inverse was added.
    assert undo.steps[-1].effects[0].expected_count == 0
    assert undo.locate is not None


def test_without_a_test_copy_nothing_runs_and_nothing_verifies(tmp_path):
    workflow = _workflow()
    bundle = _bundle(tmp_path, workflow)
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(network_lines=_network()),
        target=None,
    )
    assert verified == {}
    assert all(
        c.status == "not_run" and "no test copy" in c.reason
        for row in report.saving_steps
        for c in row.candidates
    )


# ---------------------------------------------------------------------------
# A field edit and its inverse (set the old value back)
# ---------------------------------------------------------------------------


class _FieldStore:
    """A tiny record store with one editable field per patient."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.records = [
                {"id": 1, "patient_id": "p1", "allergy": "none recorded"},
                {"id": 2, "patient_id": "p2", "allergy": "latex"},
            ]

    def patch(self, record_id: int, body: dict):
        with self.lock:
            for row in self.records:
                if row["id"] == record_id:
                    row.update(body)
                    return True
        return False


@pytest.fixture
def field_store():
    store = _FieldStore()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            with store.lock:
                self._json(200, {"records": [dict(r) for r in store.records]})

        def do_POST(self):  # noqa: N802
            store.reset()
            self._json(200, {"ok": True})

        def do_PATCH(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            ok = store.patch(int(self.path.rstrip("/").rsplit("/", 1)[-1]), body)
            self._json(200 if ok else 404, {"ok": ok})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", store
    finally:
        server.shutdown()


class EditingBackend(FakeBackend):
    """Typing fills the allergy field; Enter saves it for patient p1."""

    def __init__(self, url: str, *, also_touch_p2: bool = False):
        super().__init__(viewport=(300, 200))
        self.url = url
        self.typed = ""
        self.also_touch_p2 = also_touch_p2

    def type_text(self, text):
        self.typed = text
        return super().type_text(text)

    def press(self, key):
        super().press(key)
        if key == "Enter":
            requests.patch(
                f"{self.url}/api/patient/1", json={"allergy": self.typed}, timeout=5
            )
            if self.also_touch_p2:
                requests.patch(
                    f"{self.url}/api/patient/2", json={"note": "seen"}, timeout=5
                )


def _edit_workflow() -> Workflow:
    return Workflow(
        name="record-allergy",
        params={"patient_id": "p1", "allergy": "penicillin"},
        steps=[
            Step(
                id="type_allergy",
                intent="type the allergy",
                action=ActionKind.TYPE,
                param="allergy",
            ),
            Step(
                id="save_allergy",
                intent="save the allergy",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
                    )
                ],
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"patient_id": ValueExpr(param="patient_id")},
                        field="allergy",
                        value=ValueExpr(param="allergy"),
                        risk="irreversible",
                        timeout_s=1.0,
                    )
                ],
            ),
        ],
    )


class FieldCopy:
    name = "field-test-copy"

    def __init__(self, url, store, *, also_touch_p2=False):
        self.url, self.store, self.also_touch_p2 = url, store, also_touch_p2

    def reset(self):
        self.store.reset()

    def snapshot(self):
        return requests.get(f"{self.url}/api/db", timeout=5).json()["records"]

    read_records = snapshot

    def run(self, workflow, *, bundle_dir, run_dir, params):
        replayer = Replayer(
            EditingBackend(self.url, also_touch_p2=self.also_touch_p2),
            vision=_vision(),
            effect_verifier=RestRecordVerifier(self.url),
            poll_interval_s=0.01,
        )
        return replayer.run(
            workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
        )


def test_field_edit_gets_a_verified_restore_of_the_old_value(tmp_path, field_store):
    url, store = field_store
    workflow = _edit_workflow()
    bundle = _bundle(tmp_path, workflow)
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(),
        target=FieldCopy(url, store),
        work_dir=tmp_path / "work",
    )
    [row] = report.saving_steps
    [candidate] = row.candidates
    assert candidate.source == "field_restore"
    assert candidate.status == "verified", candidate.reason
    undo = verified["save_allergy"][0].undo
    assert undo.restore is not None
    assert undo.restore.fields == {"allergy": "prior_allergy"}
    assert [s.param for s in undo.steps if s.param] == ["prior_allergy"]
    # The copy is back at its baseline.
    assert store.records[0]["allergy"] == "none recorded"


def test_field_restore_that_touches_another_record_fails(tmp_path, field_store):
    url, store = field_store
    workflow = _edit_workflow()
    bundle = _bundle(tmp_path, workflow)
    report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(),
        target=FieldCopy(url, store, also_touch_p2=True),
        work_dir=tmp_path / "work",
    )
    [candidate] = report.saving_steps[0].candidates
    assert candidate.status == "failed"
    assert "record(s) changed (fields: note)" in candidate.reason
    assert verified == {}


# ---------------------------------------------------------------------------
# Units: candidates, refusals, the kept values, the approval
# ---------------------------------------------------------------------------


def test_network_candidates_explain_why_there_is_none():
    step = _save("save_triage", "Triage")
    effect = step.effects[0]
    assert "no network capture" in network_candidates(step, effect, [], {})[1]
    form = [
        {
            "method": "POST",
            "url": "http://x/save.php",
            "form": [["patient_id", "p1"], ["type", "Triage"]],
            "status": 302,
        }
    ]
    assert "could not be found" in network_candidates(step, effect, form, {})[1]
    form[0]["status"] = 200
    assert "browser form post" in network_candidates(step, effect, form, {})[1]
    no_id = _network()[:1]
    no_id[0]["response_text"] = "{}"
    assert "no record id" in network_candidates(step, effect, no_id, {})[1]


def test_an_observed_delete_is_preferred_over_the_convention():
    step = _save("save_triage", "Triage")
    lines = _network()[:1] + [
        {"method": "DELETE", "url": "http://x/api/v2/encounters/1", "status": 204}
    ]
    [candidate], _ = network_candidates(step, step.effects[0], lines, {})
    assert "the recording sent DELETE /api/v2/encounters/{undo_record_id}" in (
        candidate.basis
    )
    assert candidate.undo.steps[0].api_binding.url_template == (
        "/api/v2/encounters/{undo_record_id}"
    )


def test_app_ui_control_is_reported_and_never_built():
    lines = _network()[:1] + [
        {
            "method": "GET",
            "url": "http://x/encounter/1",
            "status": 200,
            "response_type": "text/html",
            "response_text": "<button>Edit</button><button>Delete</button>"
            "<input type='submit' value='Void encounter'>",
        }
    ]
    assert app_ui_controls(lines, 0) == ["Delete", "Void encounter"]
    workflow = Workflow(name="one", steps=[_save("save_triage", "Triage")])
    candidates, _ = find_candidates(workflow, 0, Sources(network_lines=lines))
    ui = [c for c in candidates if c.source == "app_ui"]
    assert len(ui) == 2 and all(c.undo is None for c in ui)
    assert "recording of a person using it" in ui[0].problem


def test_a_save_without_a_bound_record_effect_gets_no_candidate():
    step = _save("save_triage", "Triage")
    step.effects = [
        step.effects[0].model_copy(update={"needs_operator_confirmation": True})
    ]
    workflow = Workflow(name="one", steps=[step])
    candidates, notes = find_candidates(workflow, 0, Sources(network_lines=_network()))
    assert candidates == []
    assert "declares no record effect" in notes[0]


def test_compare_snapshots_reports_counts_and_field_names_only():
    before = [{"id": 1, "patient_id": "p1", "note": "a"}]
    assert compare_snapshots(before, [dict(before[0])]) is None
    assert compare_snapshots(
        before, [{"id": 1, "patient_id": "p1", "note": "a", "at": 2}], ignore={"at"}
    ) is None
    why = compare_snapshots(before, [{"id": 1, "patient_id": "p1", "note": "b"}])
    assert "1 record(s) changed (fields: note)" in why
    assert "p1" not in why and "'b'" not in why


def _locating_workflow() -> Workflow:
    undo = UndoStep(
        id="u1",
        undoes="save_triage",
        intent="delete it",
        source="synthesized",
        steps=[
            Step(
                id="d",
                intent="delete",
                action=ActionKind.WAIT,
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": ValueExpr(param="undo_record_id")},
                        expected_count=0,
                    )
                ],
            )
        ],
        locate={"match": {"patient_id": {"literal": "p1"}}},
    )
    return Workflow(name="w", steps=[_save("save_triage", "Triage")], undo_steps=[undo])


def test_locate_needs_exactly_one_new_record(tmp_path):
    workflow = _locating_workflow()
    undo = workflow.undo_steps[0]
    before = [{"id": 1, "patient_id": "p1"}]
    with pytest.raises(UndoRefused, match="kept no values"):
        resolve_undo_params(undo, tmp_path, {}, lambda: before)
    keep_prior_state(workflow, workflow.steps[0], before, {}, tmp_path)
    assert (prior_state_path(tmp_path, "save_triage").stat().st_mode & 0o777) == 0o600
    with pytest.raises(UndoRefused, match="0 record"):
        resolve_undo_params(undo, tmp_path, {}, lambda: before)
    two = before + [{"id": 2, "patient_id": "p1"}, {"id": 3, "patient_id": "p1"}]
    with pytest.raises(UndoRefused, match="2 record"):
        resolve_undo_params(undo, tmp_path, {}, lambda: two)
    with pytest.raises(UndoRefused, match="could not be read"):
        resolve_undo_params(undo, tmp_path, {}, lambda: None)
    one = before + [{"id": 2, "patient_id": "p1"}]
    assert resolve_undo_params(undo, tmp_path, {"x": "y"}, lambda: one) == {
        "x": "y",
        "undo_record_id": "2",
    }


def test_a_declared_undo_serializes_exactly_as_before():
    undo = _locating_workflow().undo_steps[0].model_copy(update={"locate": None})
    dumped = undo.model_dump(mode="json")
    assert set(dumped) == {"id", "undoes", "intent", "source", "steps"}


def test_accept_refuses_changed_evidence_and_a_second_approval(tmp_path, mockmed):
    workflow = Workflow(name="one-save", steps=[_save("save_triage", "Triage")])
    bundle = _bundle(tmp_path, workflow)
    _report, verified = propose_undos(
        workflow,
        bundle_dir=bundle,
        sources=Sources(network_lines=_network()),
        target=MockMedCopy(mockmed),
        work_dir=tmp_path / "work",
    )
    written = write_proposals(bundle, verified)
    undo = written.undo_steps[0]
    evidence = bundle / undo.proposal.evidence_path
    original = evidence.read_bytes()
    evidence.write_bytes(original.replace(b'"verified"', b'"verified" '))
    with pytest.raises(ValueError, match="changed since the undo was verified"):
        accept_proposal(bundle, undo.id, approver="nurse-a")
    evidence.write_bytes(original)
    accept_proposal(bundle, undo.id, approver="nurse-a")
    with pytest.raises(ValueError, match="already approved"):
        accept_proposal(bundle, undo.id, approver="nurse-b")


def test_cli_propose_refuses_a_target_not_declared_a_test_copy(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    bundle = _bundle(tmp_path, _workflow())
    config = tmp_path / "deployment.yaml"
    config.write_text("name: prod\nbackend:\n  kind: web\n  url: http://127.0.0.1:9\n")
    code = main(["undo", "propose", str(bundle), "--config", str(config)])
    assert code == 3
    err = capsys.readouterr().err
    assert "does not declare its target a test copy" in err
    assert Workflow.load(bundle).undo_steps == []


def test_cli_propose_without_a_test_copy_lists_and_writes_nothing(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    bundle = _bundle(tmp_path, _workflow())
    out = tmp_path / "report.json"
    code = main(["undo", "propose", str(bundle), "--report", str(out)])
    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["summary"] == {
        "saving_steps": 2,
        "verified_undo": 0,
        "no_verified_undo": 2,
    }
    assert "0 with a verified undo" in capsys.readouterr().out


def test_cli_accept_needs_a_person_at_a_terminal(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    bundle = _bundle(tmp_path, _workflow())
    assert main(["undo", "accept", str(bundle), "--undo", "x"]) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_a_bundle_with_a_located_undo_round_trips(tmp_path):
    # A bundle holding a proposal loads and saves byte-identically.
    workflow = _locating_workflow()
    bundle = _bundle(tmp_path, workflow)
    first = (bundle / "workflow.json").read_bytes()
    Workflow.load(bundle).save(bundle)
    assert (bundle / "workflow.json").read_bytes() == first


def test_the_save_request_is_found_when_the_selector_has_a_field_it_lacks():
    # MockLoan-style: the record carries ``surface``, the request does not.
    step = _save("save_triage", "Triage")
    effect = step.effects[0].model_copy(
        update={"match": {**step.effects[0].match, "surface": ValueExpr(literal="x")}}
    )
    [candidate], note = network_candidates(step, effect, _network()[:1], {})
    assert note == ""
    assert candidate.undo.locate.match["surface"] == {"literal": "x", "param": None}
    # A request whose named field disagrees does not explain the save.
    wrong = _network()[:1]
    wrong[0]["body"] = json.dumps({"patient_id": "p9", "type": "Triage"})
    assert "could not be found" in network_candidates(step, effect, wrong, {})[1]
