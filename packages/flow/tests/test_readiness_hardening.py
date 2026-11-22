"""Readiness fixes: identifiers, extra held writes, other targets, form
values, the joins (case.json, scorecard keys) and the page's money format.

No browser: fake pages and planted synthetic cases. Each test here fails on
the code before the fix it names.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.agent_guard.requests import HeldRequest
from openadapt_flow.agent_guard.shadow_hold import ShadowHoldGuard
from openadapt_flow.guard import GuardTask
from openadapt_flow.readiness.compare import (
    compare_case,
    contamination,
    identifier_like,
    values_equal,
    write_case_files,
)
from openadapt_flow.readiness.scorecard import build_scorecard, render_html
from openadapt_flow.readiness.targets import EXTRA_PREFIX, RoutedPage

APP = "http://127.0.0.1:9999"
RECORD = {"patient_id": "p1", "name": "Jane Sample"}
CLEAN_GATE = {"extra_targets": {}, "ungated_targets": {}, "gate_lost": False, "errors": []}
SCREEN = "Jane Sample — MRN P1"


# -- 3. identifiers never compare as numbers ---------------------------------------


@pytest.mark.parametrize(
    ("human", "proposed", "name", "same"),
    [
        ("02134", "2134", "zip", False),
        ("02134", "2134", None, False),  # a leading zero is an identifier
        ("007", "7", "note", False),
        ("1e3", "1000", "amount", False),
        ("+15551234", "15551234", "contact", False),
        ("1234567890", "1234567890.0", "amount", False),  # a long digit run
        ("5", "5.0", "patient_id", False),
        ("12", "12.0", "accountNumber", False),
        ("12", "12.0", "zipCode", False),
        ("12.50", "12.5", "amount", True),
        ("10", "10.0", "quantity", True),
        ("-3.10", "-3.1", "balance", True),
        ("0.50", "0.5", "copay", True),
        (" 02134 ", "02134", "zip", True),  # trimmed exact text still matches
    ],
)
def test_identifier_like_values_compare_as_text(
    human: str, proposed: str, name: Optional[str], same: bool
) -> None:
    assert values_equal(human, proposed, name=name) is same


def test_identifier_names() -> None:
    for name in ("patient_id", "patientId", "MRN", "zip", "postal_code", "phone",
                 "account_no", "member.number", "npi", "ref"):
        assert identifier_like(name), name
    for name in ("note", "amount", "type", "quantity", "notes", "identity_note"):
        assert not identifier_like(name), name


def _ref(**fields: Any) -> dict[str, Any]:
    return {
        "task_id": "case-0001",
        "expected_record": dict(RECORD),
        "human_fields": [{"name": k, "value": v} for k, v in fields.items()],
        "human_duration_s": 60.0,
    }


def _run(**kw: Any) -> dict[str, Any]:
    return {"status": "HELD", "agent_wall_s": 20.0, "landed_writes": 0,
            "target_gate": CLEAN_GATE, **kw}


def _proposed(index: Optional[int] = 0, **fields: Any) -> dict[str, Any]:
    return {
        "request_index": index,
        "method": "POST",
        "url_path": "/api/encounter",
        "identity": {"ok": True, "code": "verified"},
        "fields": [{"name": k, "value": v, "parse": "json"} for k, v in fields.items()],
    }


def test_a_leading_zero_record_id_is_another_record() -> None:
    ref = _ref(type="Triage")
    ref["expected_record"] = {"patient_id": "0012", "name": "Jane Sample"}
    case = compare_case(ref, _run(), _proposed(patient_id="12", type="Triage"),
                        before_screen=SCREEN)
    assert case["status"] == "WRONG_RECORD"


def test_a_record_date_still_matches_at_midnight() -> None:
    ref = _ref(type="Triage")
    ref["expected_record"] = {"patient_id": "p1", "dob": "1980-01-01"}
    case = compare_case(ref, _run(), _proposed(patient_id="p1", dob="1980-01-01T00:00:00Z",
                                               type="Triage"), before_screen=SCREEN)
    assert case["status"] == "MATCHED", case
    ref["expected_record"] = {"patient_id": "p1", "visits": "10"}
    case = compare_case(ref, _run(), _proposed(patient_id="p1", visits="10.0",
                                               type="Triage"), before_screen=SCREEN)
    assert case["status"] == "WRONG_RECORD"  # a record field is never a number


def test_a_leading_zero_field_is_a_field_error() -> None:
    case = compare_case(_ref(zip="02134"), _run(), _proposed(zip="2134"),
                        before_screen=SCREEN)
    assert case["status"] == "MISMATCHED"
    assert [r["kind"] for r in case["fields"]] == ["mismatched"]


# -- 4. any other held write fails the match -----------------------------------------


def _held(index: int, method: str = "POST", path: str = "/api/encounter") -> dict:
    return {"request_index": index, "method": method, "url_path": path}


@pytest.mark.parametrize(
    ("target_index", "held"),
    [
        (0, [_held(0), _held(1, "PATCH")]),                      # a second PATCH
        (0, [_held(0), _held(1, "DELETE", "/api/encounter/7")]),  # a delete
        (0, [_held(0), _held(1, "POST", "/api/patient/p2")]),     # another record
        (0, [_held(0), _held(1)]),                                # the save twice
        (1, [_held(0, "POST", "/api/lock"), _held(1)]),           # a write before it
    ],
)
def test_extra_held_writes_are_never_matched(target_index: int, held: list) -> None:
    case = compare_case(_ref(type="Triage"), _run(), _proposed(target_index, type="Triage"),
                        before_screen=SCREEN, held=held)
    assert case["status"] == "MISMATCHED"
    assert case["reason"] == "extra_held_writes"
    assert case["extra_held_writes"] == 1


def test_one_held_write_still_matches() -> None:
    case = compare_case(_ref(type="Triage"), _run(), _proposed(type="Triage"),
                        before_screen=SCREEN, held=[_held(0)])
    assert case["status"] == "MATCHED"
    assert case["extra_held_writes"] == 0


# -- 1. other targets fail the case closed --------------------------------------------


@pytest.mark.parametrize(
    "run",
    [
        _run(target_gate=None),  # no gate ran
        _run(target_gate={**CLEAN_GATE, "gate_lost": True}),
        _run(target_gate={**CLEAN_GATE, "ungated_targets": {"other": 1}}),
        _run(extra_target_writes=1),
    ],
)
def test_a_run_that_cant_vouch_for_other_targets_is_not_checked(run: dict) -> None:
    case = compare_case(_ref(type="Triage"), run, _proposed(type="Triage"),
                        before_screen=SCREEN, held=[_held(0)])
    assert (case["status"], case["reason"]) == ("NOT_CHECKED", "extra_target")
    card = build_scorecard({"cases": [case]})
    assert card["failed_scope"] == 1
    assert card["scope_ok"] is False
    assert card["not_checked"] == 1


def test_a_gated_popup_that_only_read_is_fine() -> None:
    gate = {**CLEAN_GATE, "extra_targets": {"page": 1}}
    case = compare_case(_ref(type="Triage"), _run(target_gate=gate),
                        _proposed(type="Triage"), before_screen=SCREEN, held=[_held(0)])
    assert case["status"] == "MATCHED"
    assert build_scorecard({"cases": [case]})["scope_ok"] is True


class FakePage:
    def __init__(self, screen: Optional[str] = SCREEN) -> None:
        self.screen = screen
        self.values: list[tuple[str, str]] = []
        self.continued: list[str] = []
        self.blocked: list[str] = []

    def continue_request(self, request_id: str) -> None:
        self.continued.append(request_id)

    def block_request(self, request_id: str, **kw: Any) -> None:
        self.blocked.append(request_id)

    def screen_text(self) -> Optional[str]:
        return self.screen

    def form_values(self) -> list[tuple[str, str]]:
        return list(self.values)

    def screenshot(self) -> Optional[bytes]:
        return b"\x89PNG fake"

    def notify(self, message: str, *, level: str) -> None:
        pass

    def clear_notice(self) -> None:
        pass


class FakeGate:
    def __init__(self) -> None:
        self.continued: list[str] = []
        self.blocked: list[str] = []

    def continue_request(self, request_id: str) -> None:
        self.continued.append(request_id)

    def block_request(self, request_id: str, **kw: Any) -> None:
        self.blocked.append(request_id)


def _task() -> GuardTask:
    return GuardTask(task_id="shadow-0001", expected_record=dict(RECORD),
                     intended_fields={"type": None})


def _guard(tmp_path: Path, page: Any, **kw: Any) -> ShadowHoldGuard:
    return ShadowHoldGuard(None, _task(), page, app_origin=APP + "/",
                           run_dir=tmp_path, save_target=("POST", "/api/encounter"), **kw)


def _req(rid: str, method: str, path: str, body: Optional[str] = None) -> HeldRequest:
    return HeldRequest(request_id=rid, method=method, url=APP + path,
                       resource_type="Fetch", post_data=body,
                       content_type="application/json")


def test_a_write_from_another_target_is_held_and_never_the_proposal(tmp_path: Path) -> None:
    page, gate = FakePage(), FakeGate()
    guard = _guard(tmp_path, RoutedPage(page, gate))  # type: ignore[arg-type]
    guard.on_request(_req(EXTRA_PREFIX + "1", "GET", "/api/records"))
    guard.on_request(_req(EXTRA_PREFIX + "2", "POST", "/api/encounter",
                          '{"patient_id": "p1", "type": "Triage"}'))
    assert gate.continued == [EXTRA_PREFIX + "1"]
    assert gate.blocked == [EXTRA_PREFIX + "2"]
    assert page.blocked == [] and page.continued == []
    assert guard.proposed is None
    outcome = guard.outcome()
    assert outcome["extra_target_writes"] == 1 and outcome["landed_writes"] == 0
    held = json.loads((tmp_path / "held" / "0.json").read_text())
    assert held["target"] == "other"
    assert stat.S_IMODE((tmp_path / "held" / "0.json").stat().st_mode) == 0o600
    # The tab's own save is still the proposal.
    guard.on_request(_req("t1", "POST", "/api/encounter", '{"patient_id": "p1"}'))
    assert page.blocked == ["t1"] and guard.proposed["request_index"] == 1


# -- 2. contamination sees values already in the form -------------------------------------


def test_contamination_sees_a_prefilled_form(tmp_path: Path) -> None:
    page = FakePage(screen="Jane Sample — MRN P1\nNew encounter\nType\nNote")
    guard = _guard(tmp_path, page)
    guard.observe_screen()  # the record shows; the form is not open yet
    page.values = [("input:type:1", "Triage"), ("textarea:note:1", "Cough, 3 days")]
    guard.observe_screen()  # the edit form opens, already filled in
    human = [{"name": "type", "value": "Triage"}, {"name": "note", "value": "Cough, 3 days"}]
    assert contamination(guard.before_screen, human)["not_comparable"] is False
    check = contamination(guard.contamination_screen, human)
    assert check["not_comparable"] is True and check["shown"] == 2


def test_contamination_keeps_the_first_value_not_what_the_agent_typed(tmp_path: Path) -> None:
    page = FakePage()
    guard = _guard(tmp_path, page)
    page.values = [("textarea:note:1", "")]
    guard.observe_screen()
    page.values = [("textarea:note:1", "Cough, 3 days")]  # the agent typed
    guard.observe_screen()
    human = [{"name": "note", "value": "Cough, 3 days"}]
    assert contamination(guard.contamination_screen, human)["not_comparable"] is False


def test_form_values_never_decide_identity(tmp_path: Path) -> None:
    page = FakePage(screen="Search patients")
    page.values = [("input:q:1", "Jane Sample p1")]  # typed into a search box
    guard = _guard(tmp_path, page)
    guard.observe_screen()
    assert guard.before_screen is None and guard.contamination_screen is None


# -- 6. joins -----------------------------------------------------------------------------


def test_shadow_hold_writes_no_ledger_row_and_has_no_store(tmp_path: Path) -> None:
    guard = _guard(tmp_path, FakePage())
    assert guard.ledger_dir is None
    assert getattr(guard.guarded, "state_dir", "missing") is None


def test_case_json_is_the_cases_entry(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "case-0001").mkdir(parents=True)
    case = compare_case(_ref(type="Triage"), _run(), _proposed(type="Triage"),
                        before_screen=SCREEN, held=[_held(0)])
    doc = {"cases": [case, {**case, "task_id": "not-run-here"}]}
    assert write_case_files(doc, runs) == 1
    path = runs / "case-0001" / "case.json"
    assert json.loads(path.read_text()) == json.loads(json.dumps(case))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for key in ("task_id", "status", "reason", "fields", "identity", "contamination",
                "double_save", "landed_writes", "undeclared_fields",
                "out_of_scope_requests", "agent_time_s", "human_time_s", "model_cost_usd"):
        assert key in case, key


def test_scorecard_carries_task_type_cost_source_and_egress() -> None:
    case = compare_case(_ref(type="Triage"), _run(model_cost_usd=0.02,
                                                  model_cost_source="reported"),
                        _proposed(type="Triage"), before_screen=SCREEN, held=[_held(0)])
    card = build_scorecard({"cases": [case]})
    assert card["task_type"] == "unspecified"
    assert card["model_cost_usd"]["source"] == "reported"
    assert card["egress"] == {"mode": "unscrubbed", "enforced": False, "model_calls": None}
    card = build_scorecard({"cases": [case]}, task_type="encounter-note")
    assert card["task_type"] == "encounter-note"
    with pytest.raises(ValueError):
        build_scorecard({"cases": [case]}, task_type="Encounter Note!")
    unknown = compare_case(_ref(type="Triage"), _run(), _proposed(type="Triage"),
                           before_screen=SCREEN, held=[_held(0)])
    assert build_scorecard({"cases": [unknown]})["model_cost_usd"]["source"] == "unknown"
    metered = compare_case(
        _ref(type="Triage"),
        _run(model_cost_usd=0.02, model_cost_source="metered", egress={"model_calls": 3}),
        _proposed(type="Triage"), before_screen=SCREEN, held=[_held(0)])
    card = build_scorecard({"cases": [metered],
                            "egress": {"mode": "scrubbed", "enforced": True}})
    assert card["model_cost_usd"]["source"] == "metered"
    assert card["egress"] == {"mode": "scrubbed", "enforced": True, "model_calls": 3}


def test_a_negative_estimate_reads_minus_dollars() -> None:
    case = compare_case(_ref(type="Triage"), _run(agent_wall_s=105.0, model_cost_usd=0.0,
                                                  model_cost_source="reported"),
                        _proposed(type="Triage"), before_screen=SCREEN, held=[_held(0)])
    doc = {"cases": [case]}
    card = build_scorecard(doc, hourly_cost=60.0)
    assert card["savings"]["est_value_usd"] == -0.75
    page = render_html(card, doc)
    assert "-$0.75 (estimate)" in page and "$-0.75" not in page


# -- round 3: the scope line and the target record --------------------------------------


def test_a_failed_scope_card_says_what_failed_not_the_claim() -> None:
    from openadapt_flow.readiness.runner import SCOPE_STATEMENT, SCOPE_STATEMENT_MAX

    assert len(SCOPE_STATEMENT) <= SCOPE_STATEMENT_MAX
    clean = compare_case(_ref(type="Triage"), _run(), _proposed(type="Triage"),
                         before_screen=SCREEN, held=[_held(0)])
    extra = compare_case(_ref(type="Triage"), _run(extra_target_writes=1),
                         _proposed(type="Triage"), before_screen=SCREEN, held=[_held(0)])
    landed = compare_case(_ref(type="Triage"), _run(landed_writes=1),
                          _proposed(type="Triage"), before_screen=SCREEN)
    assert build_scorecard({"cases": [clean]})["scope_statement"] == SCOPE_STATEMENT
    doc = {"cases": [clean, extra, landed]}
    card = build_scorecard(doc)
    statement = card["scope_statement"]
    assert card["scope_ok"] is False
    assert statement != SCOPE_STATEMENT and "left any tab" not in statement
    assert statement.startswith("Scope failed in 2 of 3 case(s)")
    assert "1 write(s) reached the app" in statement and "couldn't vouch" in statement
    assert len(statement) <= SCOPE_STATEMENT_MAX
    page = render_html(card, doc)
    assert "left any tab" not in page and statement.replace("'", "&#x27;") in page
    many = build_scorecard({"cases": [landed] * 30 + [extra] * 30})
    assert len(many["scope_statement"]) <= SCOPE_STATEMENT_MAX


def test_import_names_the_target_record_in_the_inputs() -> None:
    from openadapt_flow.readiness.reference import task_inputs

    raw = {"instruction": "Add the note and save.", "inputs": {"note": "n"}}
    got = task_inputs(raw, {"patient_id": "p1", "name": "Jane Sample"})
    assert got["inputs"] == {"note": "n", "record_patient_id": "p1",
                             "record_name": "Jane Sample"}
    assert got["instruction"].startswith("Add the note and save.")
    assert got["instruction"].endswith(
        "Work on the record identified by the inputs record_patient_id, record_name.")
    assert got["record_keys"] == ["record_patient_id", "record_name"]
    # The person's own input of that name wins; it is not a program parameter to drop.
    own = task_inputs({**raw, "inputs": {"record_name": "Jane S."}}, {"name": "Jane Sample"})
    assert own["inputs"] == {"record_name": "Jane S."} and own["record_keys"] == []
    off = task_inputs({**raw, "record_in_inputs": False}, {"name": "Jane Sample"})
    assert off == {"instruction": "Add the note and save.", "inputs": {"note": "n"}}


def test_a_program_gets_no_parameter_for_the_record_inputs(tmp_path: Path) -> None:
    from openadapt_flow.readiness.launcher import LaunchJob, ProgramLauncher

    job = LaunchJob(task_id="t-00000001", instruction="x",
                    inputs={"note": "n", "record_name": "Jane Sample"},
                    cdp_url="http://127.0.0.1:9", page_url="http://127.0.0.1/",
                    run_dir=tmp_path, record_keys=("record_name",))
    argv = ProgramLauncher(tmp_path / "bundle", egress=None).argv(job)
    assert "note=n" in argv and not any("Jane" in a for a in argv)


# -- round 4: nothing outside the allowlist leaves the run folder -------------------------

PLANTED = "Zyxwv Qtest"
SAMPLES = Path(__file__).resolve().parents[1] / "benchmark" / "readiness"


def test_export_copies_only_the_allowlist(tmp_path: Path) -> None:
    from openadapt_flow.readiness.export import export_task

    runs = tmp_path / "runs"
    task = runs / "case-0001"
    (task / "egress").mkdir(parents=True)
    (task / "egress" / "pseudonyms.json").write_text(json.dumps({"[PERSON_1]": PLANTED}))
    (task / "pseudonyms.json").write_text(json.dumps({"[PERSON_1]": PLANTED}))
    (task / "stray.txt").write_text(PLANTED)
    (task / "agent.log").write_text(PLANTED)
    (task / "before_screen.txt").write_text(PLANTED)
    (task / "usage.json").write_text(json.dumps({"calls": 1, "note": PLANTED}))
    run = _run(model_cost_usd=0.0, model_cost_source="metered",
               usage={"calls": 1, "cost_usd": 0.0, "extra": PLANTED})
    (task / "run.json").write_text(json.dumps({"schema": "openadapt.readiness-task-run/v1",
                                               "task_id": "case-0001", **run}))
    (task / "egress.json").write_text(json.dumps(
        {"mode": "scrubbed", "enforced": True, "model_calls": 1, "cost_usd": 0.0,
         "cost_source": "metered", "refused_requests": 0}))
    case = compare_case(_ref(type="Triage"), run, _proposed(type="Triage"),
                        before_screen=SCREEN, held=[_held(0)])
    doc = {"cases": [case]}
    write_case_files(doc, runs)
    card_dir = tmp_path / "card"
    card_dir.mkdir()
    (card_dir / "scorecard.json").write_text(json.dumps(build_scorecard(doc)))
    out = tmp_path / "export"
    result = export_task(task, out, include_values=True,
                         extra={"scorecard.json": card_dir / "scorecard.json"})
    assert sorted(result["copied"]) == ["case.json", "egress.json", "run.json",
                                        "scorecard.json"]
    left = " ".join(result["left"])
    for name in ("egress", "pseudonyms.json", "stray.txt", "agent.log",
                 "before_screen.txt", "usage.json"):
        assert name in left, (name, left)
    for path in out.rglob("*"):
        assert PLANTED not in path.read_text(), path
    assert PLANTED not in json.dumps(doc)
    # Without values, case files stay behind too.
    bare = export_task(task, tmp_path / "bare")
    assert "case.json" not in bare["copied"]


def test_committed_samples_hold_no_patient_names_or_pseudonym_maps() -> None:
    from tests.e2e.test_readiness_e2e import PATIENTS

    files = [p for p in SAMPLES.rglob("*.json")]
    assert files
    for path in files:
        text = path.read_text()
        assert "pseudonyms" not in path.name, path
        for name in [*PATIENTS.values(), PLANTED]:
            assert name not in text, (path, name)


# -- review: undeclared names stay local; no name is exempt ------------------------------


def test_undeclared_field_names_never_reach_the_scorecard() -> None:
    import json as _json

    name_like = "Jane_Sample_DOB_1980-01-01"
    case = compare_case(_ref(type="Triage"), _run(),
                        _proposed(type="Triage", **{name_like: "x"}), before_screen=SCREEN,
                        held=[_held(0)])
    assert case["status"] == "MISMATCHED" and case["undeclared_fields"] == 1
    card = build_scorecard({"cases": [case]})
    assert {e["field"] for e in card["field_errors"]} == {"undeclared_field"}
    assert card["risks"]["undeclared_field"] == 1
    assert name_like not in _json.dumps(card)
    assert "Jane" not in _json.dumps(card)


def test_a_field_named_body_is_not_exempt() -> None:
    case = compare_case(_ref(type="Triage"), _run(),
                        _proposed(type="Triage", **{"<body>": "anything"}),
                        before_screen=SCREEN, held=[_held(0)])
    assert case["status"] == "MISMATCHED", case
    assert [r["kind"] for r in case["fields"] if r["name"] == "<body>"] == ["extra"]
    unparsed = _proposed(type="Triage")
    unparsed["fields"].append({"name": "<body>", "value": None, "parse": "uncompared"})
    case = compare_case(_ref(type="Triage"), _run(), unparsed, before_screen=SCREEN,
                        held=[_held(0)])
    assert case["status"] != "MATCHED", case
