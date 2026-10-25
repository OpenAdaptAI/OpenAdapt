"""scaffold-verifier builds a working record check and tries to break it.

The load-bearing contracts:

* the read is found in the recording's network capture (the
  ``openadapt.network-capture/1`` format), a database read path or a verifier
  section, by the recorded parameter values;
* the check binds those values to run parameters and covers every table the
  save touched; tables nothing reads are reported;
* the check is proved against injected bad writes, on the recorded records and
  end to end through the replayer (MockMed and the effect_e2e record service).
  One REST read misses the stray billing write (EFFECT_E2E.md's 9 of 72): that
  check is written as a draft with the miss listed, never as working;
* nothing is approved: verify-run refuses the proposed file until a person
  removes every ``needs_operator_confirmation`` line.

Synthetic data only, localhost only, no model calls.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from openadapt_flow.__main__ import main
from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.record_check_proof import prove_live
from openadapt_flow.record_check_synthesis import (
    PROOF_NAME,
    PROPOSED_NAME,
    SynthesisError,
    render,
    synthesize,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.effect import EffectState
from openadapt_flow.runtime.effects.surfaces import (
    SurfaceRoutedVerifier,
    surface_probe,
)
from openadapt_flow.verify_run import VerifyRunError, parse_verify_config
from tests._record_check_fixtures import (
    NOTE,
    Capture,
    effect_e2e_recording,
    mockmed_recording,
    write_recording,
)


@pytest.fixture
def e2e_service(tmp_path):
    from benchmark.effect_e2e.record_service import serve

    handle = serve(tmp_path / "record.db")
    yield handle
    handle.stop()


@pytest.fixture
def mockmed():
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    yield url, db
    stop()


def _results(proposal) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fault in proposal.faults:
        out.setdefault(fault.fault, []).append(fault.as_dict()["result"])
    return out


# ---------------------------------------------------------------------------
# effect_e2e: one REST read vs a database read path
# ---------------------------------------------------------------------------


def test_rest_read_found_by_recorded_values_and_values_bound(tmp_path, e2e_service):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    proposal = synthesize(rec)

    [surface] = proposal.search.surfaces
    assert surface.config["kind"] == "rest"
    assert surface.config["records_path"] == "/api/records"
    assert surface.config["records_key"] == "records"
    # The banner read also returns the saved values; the persisted row wins.
    assert any("/api/ui/last-save" in note for note in proposal.search.notes)
    effect, content = proposal.effects
    assert content.probe.startswith("surface=api_records.content")
    assert proposal.verifier["surfaces"]["api_records.content"][
        "records_drop_fields"
    ] == ["id"]
    assert effect.match["note"].param == "note"
    assert effect.match["patient_id"].param == "patient_id"
    # The save sent type=Triage as a fixed value: it tells this record apart.
    assert effect.match["type"].literal == "Triage"
    assert effect.count_new_only and effect.forbid_collateral_loss
    assert effect.needs_operator_confirmation


def test_one_rest_read_misses_the_billing_write_and_stays_a_draft(
    tmp_path, e2e_service
):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    proposal = synthesize(rec)

    results = _results(proposal)
    assert results["ok"] == ["clean pass"]
    for fault in ("no_persist", "partial", "duplicate", "wrong_record", "stale"):
        assert set(results[fault]) == {"caught"}, fault
    assert results["collateral_unaudited"] == ["MISSED"]

    proposal.live = prove_live(
        "effect-e2e",
        proposal.verifier,
        proposal.effects,
        proposal.params,
        write_requests=proposal.search.write_requests,
        work_dir=tmp_path / "live",
    )
    rows = {row["scenario"]: row["result"] for row in proposal.live["runs"]}
    assert rows["ok"] == "clean pass"
    assert rows["collateral_unaudited"] == "MISSED"
    assert all(
        rows[name].startswith("caught")
        for name in ("no_persist", "partial", "duplicate", "wrong_record", "stale")
    )
    assert not proposal.live["passed"]
    assert not proposal.working
    text = render(proposal)
    assert text.startswith("# DRAFT record check")
    assert "MISS: collateral_unaudited" in text


def test_database_read_path_covers_every_table_and_passes_the_suite(
    tmp_path, e2e_service
):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    proposal = synthesize(rec, db_path=e2e_service.db_path)

    roles = {s.name: s.is_target for s in proposal.search.surfaces}
    assert roles["encounters"] and roles["api_records"]
    assert roles["billing"] is False  # guarded: the save adds no row there
    assert proposal.search.closed_world_basis == "database"
    assert proposal.search.uncovered == []
    assert all(fault.ok for fault in proposal.faults)

    proposal.live = prove_live(
        "effect-e2e",
        proposal.verifier,
        proposal.effects,
        proposal.params,
        write_requests=proposal.search.write_requests,
        work_dir=tmp_path / "live",
    )
    rows = {row["scenario"]: row for row in proposal.live["runs"]}
    assert rows["ok"]["result"] == "clean pass"
    assert rows["collateral_unaudited"]["result"] == "caught by the check"
    # Every wrong save stopped; the timeout committed but the actuator could
    # not know, which is a safe stop the check is not blamed for.
    for name, row in rows.items():
        if row["ground_truth"] != "correct":
            assert not row["reported_done"], name
    assert rows["timeout"]["result"] == "stopped by the actuator (outcome unknown)"
    assert proposal.live["passed"], proposal.live["misses"]
    assert proposal.working


def test_frozen_demo_value_fails_the_live_clean_save(tmp_path, e2e_service):
    """The live proof uses new note values, so a literal demo note can't pass."""

    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    proposal = synthesize(rec, db_path=e2e_service.db_path)
    frozen = []
    for effect in proposal.effects:
        match = dict(effect.match)
        if "note" in match:
            from openadapt_flow.runtime.effects import ValueExpr

            match["note"] = ValueExpr(literal=NOTE)
        frozen.append(effect.model_copy(update={"match": match}))
    live = prove_live(
        "effect-e2e",
        proposal.verifier,
        frozen,
        proposal.params,
        write_requests=proposal.search.write_requests,
        work_dir=tmp_path / "live",
    )
    assert live["runs"][0]["result"] == "false stop"
    assert not live["passed"]


# ---------------------------------------------------------------------------
# MockMed (the --break-it fixture)
# ---------------------------------------------------------------------------


def test_mockmed_needs_a_declared_complete_read(tmp_path, mockmed):
    url, db = mockmed
    rec = mockmed_recording(tmp_path / "rec", url, db)

    plain = synthesize(rec)
    assert plain.search.surfaces[0].config["records_path"] == "/api/db"
    assert not plain.working
    assert any("collateral_unaudited" in miss for miss in plain.misses)

    declared = synthesize(rec, complete_read=True)
    assert declared.search.closed_world_basis == "declared"
    assert all(fault.ok for fault in declared.faults)
    declared.live = prove_live(
        "mockmed",
        declared.verifier,
        declared.effects,
        declared.params,
        write_requests=declared.search.write_requests,
        work_dir=tmp_path / "live",
    )
    rows = {row["scenario"]: row["result"] for row in declared.live["runs"]}
    assert rows["ok"] == "clean pass"
    for name in (
        "no_persist",
        "partial",
        "wrong_record",
        "stale",
        "optimistic",
        "session",
    ):
        assert rows[name].startswith("caught"), (name, rows[name])
    assert declared.live["passed"]
    assert declared.working
    assert declared.proof()["closed_world"] == "declared"


# ---------------------------------------------------------------------------
# OpenEMR shapes: FHIR R4 Observation search and the Standard API patient list
# ---------------------------------------------------------------------------


@pytest.fixture
def fhir():
    from tests._fhir_fake import serve

    base, store, stop = serve()
    yield base, store
    stop()


def _fhir_recording(tmp_path: Path, base: str, store) -> Path:
    cap = Capture()
    cap.get(f"{base}/Observation?patient=p1", 1.0)
    store.add_observation(patient="p1", note=NOTE)
    cap.get(f"{base}/Observation?patient=p1", 2.5)
    rec = write_recording(tmp_path / "rec", {"patient_id": "p1", "note": NOTE})
    cap.write(rec)
    return rec


def test_openemr_fhir_read_is_scoped_by_the_run_patient(tmp_path, fhir):
    base, store = fhir
    store.add_observation(patient="p1", note="An older synthetic note", source="other")
    rec = _fhir_recording(tmp_path, base, store)
    proposal = synthesize(rec)

    [surface] = proposal.search.surfaces
    assert surface.config["kind"] == "fhir"
    assert surface.config["base_url"] == base
    assert surface.config["resource_type"] == "Observation"
    assert surface.config["search_param_exprs"] == {"patient": {"param": "patient_id"}}
    assert surface.config["field_paths"]["valueString"] == "valueString"
    assert surface.before_recorded  # the chart was read before the save too
    results = _results(proposal)
    assert results["ok"] == ["clean pass"]
    assert "caught" in results["wrong_record"]  # the read is scoped by patient
    assert set(results["stale"]) == {"caught"}


def test_openemr_fhir_check_refutes_store_level_faults(tmp_path, fhir):
    """Each fault is injected in the FHIR store; the check built from the
    recording must refute it and confirm the clean save."""

    base, store = fhir
    rec = _fhir_recording(tmp_path, base, store)
    proposal = synthesize(rec)
    params = {"patient_id": "p1", "note": "Second synthetic note"}
    effects = [
        e.model_copy(update={"needs_operator_confirmation": False, "timeout_s": 0.2})
        for e in proposal.effects
    ]

    def verdicts(fault: str) -> set[Verdict]:
        store.resources.clear()
        store.add_observation(
            patient="p1", note="Concurrent synthetic note", source="other"
        )
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(proposal.verifier), params
        )
        before = verifier.capture_pre_state()
        if fault == "ok":
            store.add_observation(patient="p1", note=params["note"])
        elif fault == "partial":
            store.add_observation(patient="p1", note=None)
        elif fault == "duplicate":
            store.add_observation(patient="p1", note=params["note"])
            store.add_observation(patient="p1", note=params["note"])
        elif fault == "wrong_record":
            store.add_observation(patient="p2", note=params["note"])
        elif fault == "stale":
            store.delete_where(patient="p1", source="other")
            store.add_observation(patient="p1", note=params["note"])
        return {
            verifier.verify(effect.resolve(params), before).verdict
            for effect in effects
        }

    assert verdicts("ok") == {Verdict.CONFIRMED}
    for fault in ("no_persist", "partial", "duplicate", "wrong_record", "stale"):
        assert Verdict.REFUTED in verdicts(fault), fault


def test_openemr_patient_list_matches_the_hand_written_check(tmp_path):
    """The OpenEMR Standard API patient list (``{"data": [...]}``) yields the
    same read and fields the hand-written benchmark/openemr_local check uses."""

    params = {
        "fname": "Avery",
        "lname": "Quill",
        "DOB": "1984-03-09",
        "email": "avery.quill@example.test",
        "city": "Springfield",
    }
    before = {"data": [{"pid": 7, "uuid": "u-7", "fname": "Rowan", "lname": "Ash"}]}
    after = {
        "data": before["data"]
        + [{"pid": 8, "uuid": "u-8", **params, "sex": "Female", "title": ""}]
    }
    url = "https://127.0.0.1:9300/apis/default/api/patient?_limit=1000"
    lines = [
        {
            "method": "GET",
            "url": url,
            "status": 200,
            "t": 1.0,
            "response_type": "application/json",
            "response_text": json.dumps(before),
        },
        {
            "method": "POST",
            "url": "https://127.0.0.1:9300/interface/new/new_comprehensive_save.php",
            "status": 302,
            "t": 2.0,
            "form": [
                ["form_fname", "Avery"],
                ["csrf_token_form", "<session:csrf_token_form>"],
            ],
        },
        {
            "method": "GET",
            "url": url,
            "status": 200,
            "t": 3.0,
            "response_type": "application/json",
            "response_text": json.dumps(after),
        },
    ]
    rec = write_recording(tmp_path / "rec", params)
    with (rec / "network.jsonl").open("w") as fh:
        fh.write(
            json.dumps({"schema": "openadapt.network-capture/1", "exchanges": 3}) + "\n"
        )
        for line in lines:
            fh.write(json.dumps(line) + "\n")

    proposal = synthesize(rec)
    [surface] = proposal.search.surfaces
    assert surface.config["records_path"] == "/apis/default/api/patient?_limit=1000"
    assert surface.config["records_key"] == "data"
    assert surface.before_recorded
    effect = proposal.effects[0]
    assert {k: v.param for k, v in effect.match.items()} == {k: k for k in params}
    # The form save answered with a redirect; its sent value is read back.
    assert len(proposal.search.write_requests) == 1
    assert proposal.search.uncovered == []
    # Every checked field identifies the patient, so a lost update to another
    # chart of the same patient can't be told apart: reported, not credited.
    missed = {f.fault for f in proposal.faults if not f.ok}
    assert missed == {"stale", "collateral_unaudited"}
    results = _results(proposal)
    assert set(results["changed"]) == set(results["deleted"]) == {"caught"}


def test_session_token_reads_and_nested_fields_are_reported(tmp_path):
    params = {"note": NOTE}
    lines = [
        {
            "method": "POST",
            "url": "http://app.test/save",
            "status": 200,
            "t": 2.0,
            "body": json.dumps({"note": NOTE}),
        },
        {
            "method": "GET",
            "url": "http://app.test/api/x?token=<session:token>",
            "status": 200,
            "t": 3.0,
            "response_type": "application/json",
            "response_text": json.dumps({"records": [{"id": 1, "note": NOTE}]}),
        },
        {
            "method": "GET",
            "url": "http://app.test/api/nested",
            "status": 200,
            "t": 3.1,
            "response_type": "application/json",
            "response_text": json.dumps(
                {"records": [{"id": 1, "body": {"note": NOTE}}]}
            ),
        },
    ]
    rec = write_recording(tmp_path / "rec", params)
    with (rec / "network.jsonl").open("w") as fh:
        fh.write(json.dumps({"schema": "openadapt.network-capture/1"}) + "\n")
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    proposal = synthesize(rec)
    assert proposal.effects == []
    notes = " ".join(proposal.search.notes)
    assert "session token" in notes
    assert "nested fields" in notes


def test_no_params_is_refused(tmp_path):
    rec = write_recording(tmp_path / "rec", {})
    with pytest.raises(SynthesisError):
        synthesize(rec)


# ---------------------------------------------------------------------------
# Nothing is approved
# ---------------------------------------------------------------------------


def test_verify_run_refuses_until_a_person_removes_the_flags(tmp_path, e2e_service):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    proposal = synthesize(rec, db_path=e2e_service.db_path)
    doc = yaml.safe_load(render(proposal))
    with pytest.raises(VerifyRunError, match="unconfirmed placeholder"):
        parse_verify_config(doc)
    for effect in doc["effects"]:
        effect.pop("needs_operator_confirmation")
    cfg = parse_verify_config(doc)
    verifier = build_effect_verifier(cfg.verifier, proposal.params)
    assert isinstance(verifier, SurfaceRoutedVerifier)
    assert set(verifier.surfaces) == set(proposal.verifier["surfaces"])
    assert {s.name for s in proposal.search.surfaces} <= set(verifier.surfaces)

    from openadapt_flow.verify_run import _tag_reads

    _tag_reads(verifier, "verify-test-run-0001")
    rest = verifier.verifiers["api_records"]
    assert rest.headers["X-OpenAdapt-Execution-Id"] == "verify-test-run-0001"

    screen = dict(
        doc, verifier={"kind": "surfaces", "surfaces": {"a": {"kind": "onscreen"}}}
    )
    with pytest.raises(VerifyRunError, match="does not read independently"):
        parse_verify_config(screen)


# ---------------------------------------------------------------------------
# The routed verifier
# ---------------------------------------------------------------------------


class _Fixed:
    substrate = "fixed"
    verification_tier = 1

    def __init__(self, records):
        self.records = records

    def capture_pre_state(self, context=None):
        return EffectState(
            substrate="fixed", reachable=True, records=list(self.records)
        )

    def verify(self, expected, before, context=None):
        from openadapt_flow.runtime.effects._common import judge_records

        return judge_records(expected, before, self.records, substrate="fixed")


def test_surface_routed_verifier_judges_each_effect_on_its_own_read():
    a = _Fixed([{"id": 1, "note": "x"}])
    b = _Fixed([])
    verifier = SurfaceRoutedVerifier({"a": a, "b": b})
    before = verifier.capture_pre_state()
    a.records = a.records + [{"id": 2, "note": "y"}]
    b.records = [{"id": 9}]
    wrote = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"note": "y"},
        count_new_only=True,
        probe=surface_probe("a"),
    )
    guard = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={},
        expected_count=0,
        count_new_only=True,
        probe=surface_probe("b", "no new rows"),
    )
    assert verifier.verify(wrote, before).verdict is Verdict.CONFIRMED
    assert verifier.verify(guard, before).verdict is Verdict.REFUTED
    for probe in (None, "surface=c"):
        lost = wrote.model_copy(update={"probe": probe})
        assert verifier.verify(lost, before).verdict is Verdict.INDETERMINATE


def test_surfaces_config_validation_and_identity_unchanged():
    with pytest.raises(ValueError):
        EffectsConfig.model_validate({"kind": "surfaces"})
    with pytest.raises(ValueError):
        EffectsConfig.model_validate(
            {
                "kind": "rest",
                "base_url": "http://x",
                "surfaces": {"a": {"kind": "rest"}},
            }
        )
    with pytest.raises(ValueError):
        EffectsConfig.model_validate(
            {"kind": "surfaces", "surfaces": {"a": {"kind": "surfaces"}}}
        )
    # A config that sets no surfaces keeps the identity it had before.
    from openadapt_flow.deployment import _identity_config_dump

    dumped = _identity_config_dump(
        EffectsConfig.model_validate({"kind": "rest", "base_url": "http://x"})
    )
    assert "surfaces" not in dumped


# ---------------------------------------------------------------------------
# Identity coverage
# ---------------------------------------------------------------------------


def _identity_bundle(tmp_path: Path) -> tuple[Path, Path]:
    from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow

    def anchor(**extra):
        return Anchor(
            template="templates/x.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            **extra,
        )

    steps = [
        Step(
            id="step_000",
            intent="open row",
            action=ActionKind.CLICK,
            anchor=anchor(context_text="Quill Avery 1984-03-09"),
        ),
        Step(
            id="step_001",
            intent="open chart",
            action=ActionKind.CLICK,
            anchor=anchor(),
            identity_armed=False,
        ),
        Step(
            id="step_002",
            intent="pick patient",
            action=ActionKind.CLICK,
            anchor=anchor(),
            identity_armed=False,
        ),
        Step(
            id="step_003",
            intent="save",
            action=ActionKind.CLICK,
            anchor=anchor(),
            identity_armed=False,
            identity_unarmed_reason="no readable band text",
        ),
    ]
    bundle = tmp_path / "bundle"
    Workflow(name="identity demo", steps=steps).save(bundle)
    events = [
        {"kind": "click", "t": 0.1},
        {
            "kind": "click",
            "t": 0.2,
            "structured_identity": "Quill, Avery 1984-03-09 MRN 42",
        },
        {
            "kind": "click",
            "t": 0.3,
            "structural": {"role": "row", "name": "Quill Avery"},
        },
        {
            "kind": "click",
            "t": 0.4,
            "structural": {"role": "button", "name": "Save"},
            "title_before": "Quill Avery - Chart",
        },
    ]
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return bundle, rec


def test_identity_coverage_before_and_after(tmp_path):
    from openadapt_flow.record_check_identity import (
        identity_coverage,
        write_identity_proposals,
    )

    bundle, rec = _identity_bundle(tmp_path)
    report, proposals = identity_coverage(bundle, rec, {"lname": "Quill"})
    assert report["applicable"] == 4
    assert report["armed_before"] == 1
    assert report["armed_after"] == 3
    assert {p["step_id"] for p in proposals} == {"step_001", "step_002"}
    assert report["proposed"][0]["contains_params"] == ["lname"]
    assert "value" not in report["proposed"][0]  # record text stays private
    [seen] = report["seen_not_checkable"]
    assert seen["step_id"] == "step_003"
    path = write_identity_proposals(proposals, tmp_path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_identity_not_proposed_where_the_runtime_cannot_check_it(tmp_path):
    from openadapt_flow.ir import Workflow
    from openadapt_flow.record_check_identity import identity_coverage

    bundle, rec = _identity_bundle(tmp_path)
    workflow = Workflow.load(bundle)
    Workflow.model_validate(workflow.model_dump(mode="json") | {"surface": "rdp"}).save(
        bundle
    )
    report, proposals = identity_coverage(bundle, rec, {"lname": "Quill"})
    assert proposals == []
    assert report["armed_after"] == report["armed_before"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_a_working_check_and_its_proof(tmp_path, e2e_service, capsys):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    code = main(
        [
            "scaffold-verifier",
            str(rec),
            "--db",
            str(e2e_service.db_path),
            "--prove",
            "effect-e2e",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Record check: WORKING (not approved)" in out
    proof = json.loads((tmp_path / "out" / PROOF_NAME).read_text())
    assert proof["status"] == "working" and proof["approved"] is False
    assert proof["live"]["passed"]
    assert (tmp_path / "out" / PROPOSED_NAME).is_file()


def test_cli_exits_nonzero_for_a_draft(tmp_path, e2e_service, capsys):
    rec = effect_e2e_recording(tmp_path / "rec", e2e_service.base_url)
    code = main(["scaffold-verifier", str(rec), "--prove", "effect-e2e"])
    out = capsys.readouterr().out
    assert code == 1
    assert "DRAFT (not working)" in out
    assert "MISS: collateral_unaudited" in out


def test_cli_falls_back_to_the_todo_draft_without_reads(tmp_path, capsys):
    rec = write_recording(tmp_path / "rec", {"note": NOTE})
    code = main(["scaffold-verifier", str(rec)])
    out = capsys.readouterr().out
    assert code == 0
    assert "TODO draft" in out
    assert (rec / "effect_contract.yaml").is_file()


# ---------------------------------------------------------------------------
# Review findings: each would have let a broken check be called working
# ---------------------------------------------------------------------------


def _capture(rec: Path, lines: list[dict]) -> None:
    with (rec / "network.jsonl").open("w") as fh:
        fh.write(json.dumps({"schema": "openadapt.network-capture/1"}) + "\n")
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _db(path: Path, rows: dict[str, list[tuple]]) -> Path:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE encounters (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE billing (id INTEGER PRIMARY KEY, patient_id TEXT, amount TEXT)"
    )
    for table, values in rows.items():
        for value in values:
            marks = ",".join("?" * len(value))
            conn.execute(f"INSERT INTO {table} VALUES ({marks})", value)
    conn.commit()
    conn.close()
    return path


def test_a_table_too_big_to_scan_keeps_the_check_a_draft(tmp_path, monkeypatch):
    import openadapt_flow.record_check_synthesis as rcs

    db = _db(
        tmp_path / "app.db",
        {
            "encounters": [(1, "p1", NOTE)],
            "billing": [(i, "p9", "1.00") for i in range(1, 6)],
        },
    )
    monkeypatch.setattr(rcs, "MAX_SCAN_ROWS", 3)
    rec = write_recording(tmp_path / "rec", {"patient_id": "p1", "note": NOTE})
    proposal = synthesize(rec, db_path=db)
    assert proposal.search.closed_world_basis is None
    assert any("billing" in item for item in proposal.search.uncovered)
    assert not proposal.working


def test_stale_is_a_miss_when_no_field_tells_records_apart(tmp_path):
    db = _db(tmp_path / "app.db", {"encounters": [(1, "p1", None)]})
    rec = write_recording(tmp_path / "rec", {"patient_id": "p1"})
    proposal = synthesize(rec, db_path=db)
    stale = [f for f in proposal.faults if f.fault == "stale"]
    assert stale and all(f.as_dict()["result"] == "MISSED" for f in stale)
    assert not proposal.working


def test_guards_catch_a_changed_or_deleted_row_through_the_real_read(tmp_path):
    import sqlite3

    db = _db(
        tmp_path / "app.db",
        {"encounters": [(1, "p1", NOTE)], "billing": [(1, "p2", "10.00")]},
    )
    rec = write_recording(tmp_path / "rec", {"patient_id": "p1", "note": NOTE})
    proposal = synthesize(rec, db_path=db)
    results = _results(proposal)
    assert set(results["changed"]) == {"caught"}
    assert set(results["deleted"]) == {"caught"}
    assert proposal.working, proposal.misses

    effects = [
        e.model_copy(update={"needs_operator_confirmation": False, "timeout_s": 0.05})
        for e in proposal.effects
    ]

    def verdicts(mutate) -> set[Verdict]:
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(proposal.verifier), proposal.params
        )
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM encounters")
        conn.commit()
        before = verifier.capture_pre_state()
        conn.execute("INSERT INTO encounters VALUES (7, 'p1', ?)", (NOTE,))
        mutate(conn)
        conn.commit()
        conn.close()
        return {
            verifier.verify(e.resolve(proposal.params), before).verdict for e in effects
        }

    assert verdicts(lambda c: None) == {Verdict.CONFIRMED}
    changed = verdicts(lambda c: c.execute("UPDATE billing SET amount = '99.00'"))
    assert Verdict.REFUTED in changed
    deleted = verdicts(lambda c: c.execute("DELETE FROM billing"))
    assert Verdict.REFUTED in deleted


def test_names_are_unique_across_sources(tmp_path):
    db = _db(tmp_path / "app.db", {"encounters": [(1, "p1", NOTE)]})
    rec = write_recording(tmp_path / "rec", {"patient_id": "p1", "note": NOTE})
    _capture(
        rec,
        [
            {"method": "POST", "url": "http://app.test/save", "status": 200, "t": 2.1},
            {
                "method": "GET",
                "url": "http://app.test/encounters",
                "status": 200,
                "t": 2.5,
                "response_text": json.dumps(
                    {"records": [{"id": 1, "patient_id": "p1", "note": NOTE}]}
                ),
            },
        ],
    )
    proposal = synthesize(rec, db_path=db)
    names = [s.name for s in proposal.search.surfaces]
    assert len(names) == len(set(names))
    assert len(proposal.verifier["surfaces"]) == len(names) + sum(
        s.has_ids for s in proposal.search.surfaces
    )


def test_the_save_is_timed_from_its_write_request(tmp_path):
    """An event's t is stamped after the screen settles, so the save's POST
    and the app's read-back come before the Save step's t."""

    before = {"records": []}
    after = {"records": [{"id": 1, "patient_id": "p1", "note": NOTE}]}
    rec = write_recording(
        tmp_path / "rec", {"patient_id": "p1", "note": NOTE}, save_t=3.0
    )
    _capture(
        rec,
        [
            {
                "method": "GET",
                "url": "http://app.test/api/records",
                "status": 200,
                "t": 1.0,
                "response_text": json.dumps(before),
            },
            {
                "method": "POST",
                "url": "http://app.test/api/encounter",
                "status": 200,
                "t": 2.1,
                "body": json.dumps({"patient_id": "p1", "note": NOTE}),
            },
            {
                "method": "GET",
                "url": "http://app.test/api/records",
                "status": 200,
                "t": 2.5,
                "response_text": json.dumps(after),
            },
            {
                "method": "GET",
                "url": "http://app.test/api/records",
                "status": 200,
                "t": 6.0,
                "response_text": json.dumps(after),
            },
        ],
    )
    proposal = synthesize(rec, complete_read=True)
    [surface] = proposal.search.surfaces
    assert surface.before_recorded and surface.before == []
    assert proposal.search.save_t == 2.1
    assert _results(proposal)["ok"] == ["clean pass"]


def test_record_ids_in_a_url_stay_out_of_names_and_block_working(tmp_path):
    body = {"records": [{"id": 1, "patient_id": "p1", "note": NOTE}]}
    rec = write_recording(tmp_path / "rec", {"note": NOTE})
    _capture(
        rec,
        [
            {"method": "POST", "url": "http://app.test/save", "status": 200, "t": 2.1},
            {
                "method": "GET",
                "url": "http://app.test/api/patients/MRN4471/encounters",
                "status": 200,
                "t": 2.5,
                "response_text": json.dumps(body),
            },
        ],
    )
    proposal = synthesize(rec, complete_read=True)
    text = json.dumps(proposal.proof()) + render(proposal).split("schema_version")[0]
    assert "MRN4471" not in json.dumps(proposal.proof())
    assert all("MRN4471" not in s.name for s in proposal.search.surfaces)
    assert any("fixed record" in p for p in proposal.search.problems)
    assert not proposal.working
    assert "MRN4471" not in text


def test_generated_reads_are_built_and_run(tmp_path):
    import sqlite3

    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE visits (id INTEGER PRIMARY KEY, "patient_id" TEXT, note TEXT)'
    )
    conn.execute("INSERT INTO visits VALUES (1, 'p1', ?)", (NOTE,))
    conn.commit()
    conn.close()
    rec = write_recording(tmp_path / "rec", {"patient-id": "p1", "note": NOTE})
    proposal = synthesize(rec, db_path=path)
    config = proposal.verifier["surfaces"]["visits"]
    assert config["sql_query_params"] == {"p0": {"param": "patient-id"}}
    assert proposal.search.problems == []
    assert proposal.working, proposal.misses


def test_braces_in_a_literal_path_are_not_doubled(tmp_path):
    body = {"records": [{"id": 1, "note": NOTE}]}
    rec = write_recording(tmp_path / "rec", {"note": NOTE})
    _capture(
        rec,
        [
            {"method": "POST", "url": "http://app.test/save", "status": 200, "t": 2.1},
            {
                "method": "GET",
                "url": "http://app.test/api/{v}/records",
                "status": 200,
                "t": 2.5,
                "response_text": json.dumps(body),
            },
        ],
    )
    proposal = synthesize(rec)
    [surface] = proposal.search.surfaces
    assert surface.config["records_path"] == "/api/{v}/records"
    assert "path_params" not in surface.config


def test_a_truncated_capture_falls_back_to_the_todo_draft(tmp_path, capsys):
    rec = write_recording(tmp_path / "rec", {"note": NOTE})
    (rec / "network.jsonl").write_text(
        '{"schema": "openadapt.network-capture/1"}\n{"meth'
    )
    code = main(["scaffold-verifier", str(rec)])
    assert code == 0
    assert "TODO draft" in capsys.readouterr().out


def test_a_demonstration_with_no_write_is_refused(tmp_path, capsys):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text(
        json.dumps(
            {
                "kind": "click",
                "t": 1.0,
                "structural": {"role": "link", "name": "Open chart"},
            }
        )
        + "\n"
    )
    (rec / "meta.json").write_text(json.dumps({"params": {"note": NOTE}}))
    _capture(
        rec,
        [
            {
                "method": "GET",
                "url": "http://app.test/api/records",
                "status": 200,
                "t": 2.0,
                "response_text": json.dumps({"records": [{"id": 1, "note": NOTE}]}),
            },
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main(["scaffold-verifier", str(rec), "--complete-read"])
    assert "REFUSED" in str(exc.value.code if exc.value.code is not None else exc.value)


def test_a_binary_keyed_table_reads_the_same_in_the_scan_and_the_check(tmp_path):
    """OpenEMR keys patient_data by uuid binary(16). The scan must see the
    cells as the generated SQL read returns them, or every such read is
    reported as returning different rows."""

    import sqlite3

    path = tmp_path / "openemr.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE patient_data (id INTEGER PRIMARY KEY, uuid BLOB, "
        "fname TEXT, lname TEXT, email TEXT)"
    )
    conn.execute(
        "INSERT INTO patient_data VALUES (1, ?, 'Ada', 'Quill', 'ada.quill@example.test')",
        (bytes.fromhex("a2f1c3d40e5b4c7a9b1d2e3f40516273"),),
    )
    conn.commit()
    conn.close()
    proposal = synthesize(
        None,
        params={"fname": "Ada", "lname": "Quill", "email": "ada.quill@example.test"},
        db_path=path,
    )
    assert not [p for p in proposal.search.problems if "patient_data" in p]
    assert any(s.is_target for s in proposal.search.surfaces)
