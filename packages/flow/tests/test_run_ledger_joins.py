"""The joins with the agent-control-plane pieces, on copies of their real files.

Every fixture here is a copy of a synthetic artifact a producer wrote (only
local paths were shortened):

* ``shadow_matched``, ``shadow_mismatched``, ``shadow_extra_target``: P1's
  committed samples (``benchmark/readiness/sample/``, branch
  claude/acp-P1-shadow-scorecard at d5339c054). ``shadow_egress`` is the
  matched sample with P3's real scrubbed-mode ``egress.json``
  (``tests/fixtures/readiness/p3_egress.json`` on that branch).
* ``shadow_wrong_record``, ``shadow_no_case``: the 2026-11-21 end-to-end run;
  the wrong-record ``case.json`` is that run's ``card/cases.json`` entry.
* P5 guarded saves (J3), P6 exception folders (J4) and P7 fallback results
  from the same run, and the dispatcher envelope built with P7's own
  ``WrappedTask._result`` and ``finish`` (J5).
"""

from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path

import pytest

from openadapt_flow.run_ledger.cli import LedgerRefused, outbound_lines, push
from openadapt_flow.run_ledger.collect import collect
from openadapt_flow.run_ledger.store import LedgerStore, build_row

FIXTURES = Path(__file__).parent / "fixtures" / "run_ledger"
TYPES_FIXTURES = Path(__file__).resolve().parents[2] / "types" / "tests" / "fixtures"
J6_EXAMPLES = TYPES_FIXTURES / "agent_run_summary_v1_examples.jsonl"


class _Presidio:
    """What openadapt-privacy's scrubber does to the labels in the demo run:
    it rewrites the field name ``patient_id`` to ``<ORGANIZATION>``."""

    def __init__(self, *rewrites: str) -> None:
        self.rewrites = set(rewrites) | {"patient_id"}

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return "<ORGANIZATION>" if text in self.rewrites else text


def _outcome(row) -> tuple[str, str]:
    return row.outcome.value, row.outcome_reason


# -- J1: readiness shadow runs ----------------------------------------------------------


def test_a_matched_shadow_run_is_escalated_never_verified_or_caught():
    row, _, _ = build_row(FIXTURES / "shadow_matched")
    assert _outcome(row) == ("ESCALATED", "shadow_matched")
    assert row.readback_tier is None
    assert set(row.sources) == {
        "case.json",
        "egress.json",
        "proposed_write.json",
        "run.json",
        "usage.json",
    }
    # The shadow hold asks nobody: 0, not unknown.
    assert row.human.intervention_s == 0.0
    # A shadow run writes nothing: no fields_written (round 3 finding 2).
    assert row.fields_written == () and row.fields_differing == 0


def test_an_old_shadow_hold_guard_row_is_ignored():
    # The demo run's shadow dirs carry P5's shadow-hold row (CAUGHT_ERROR).
    row, _, _ = build_row(FIXTURES / "shadow_wrong_record")
    assert "guard_ledger_row.json" not in row.sources
    assert "guard_events.jsonl" not in row.sources


def test_shadow_time_and_cost_come_from_the_run():
    row, _, _ = build_row(FIXTURES / "shadow_matched")
    assert row.duration_s == 2.123  # agent_wall_s
    # A healthy ``--egress blocked`` run: the proxy metered 0 calls, $0.
    assert (row.model.calls, row.model.input_tokens) == (0, 0)
    assert (row.model.cost_usd, row.model.cost_source) == (0.0, "metered")
    assert row.executor_kind == "agent"
    assert row.executor_label == "readiness-shadow"  # usage names no model
    row, _, _ = build_row(FIXTURES / "shadow_wrong_record")
    assert row.duration_s == 2.042 and row.model.calls == 6
    assert (row.model.cost_usd, row.model.cost_source) == (0.03, "reported")
    assert row.executor_label == "claude-sonnet-5-5"


def test_metered_egress_cost_beats_the_agents_report():
    # P1's matched sample with P3's real scrubbed-mode egress.json: the
    # proxy saw one call the agent's usage didn't report.
    row, _, _ = build_row(FIXTURES / "shadow_egress")
    assert (row.model.cost_usd, row.model.cost_source) == (0.008, "metered")
    assert row.model.calls == 1


def test_a_wrong_record_shadow_is_the_error_the_shadow_caught():
    row, private, _ = build_row(FIXTURES / "shadow_wrong_record")
    assert _outcome(row) == ("CAUGHT_ERROR", "shadow_wrong_record")
    assert row.fields_written == () and row.fields_differing == 0
    assert '"p3"' not in json.dumps(row.to_json())
    assert private.fields["patient_id"] == {
        "intended": "p3",
        "saved": "p2",
        "diff": "--- intended\n+++ saved\n@@ -1 +1 @@\n-p3\n+p2",
    }


def test_a_mismatched_shadow_is_caught():
    row, _, _ = build_row(FIXTURES / "shadow_mismatched")
    assert _outcome(row) == ("CAUGHT_ERROR", "shadow_field_mismatch")
    assert row.fields_written == ()


def test_an_extra_target_run_is_unknown_never_a_match():
    row, _, _ = build_row(FIXTURES / "shadow_extra_target")
    assert _outcome(row) == ("UNKNOWN", "shadow_extra_target")


def test_a_refused_egress_is_told_apart_from_a_healthy_blocked_run(tmp_path):
    # Both say mode "blocked"; only run.json egress.refused differs.
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "shadow_matched", run)
    doc = json.loads((run / "run.json").read_text())
    doc["egress"] = {"refused": True, "reason": "the OS egress block is unavailable"}
    (run / "run.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "egress_refused")
    doc["status"] = "EGRESS_REFUSED"
    (run / "run.json").write_text(json.dumps(doc))
    (run / "egress.json").unlink()
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "egress_refused")
    # A refused session metered nothing: the agent's own report stands.
    assert row.model.cost_source == "reported"
    healthy, _, _ = build_row(FIXTURES / "shadow_matched")
    assert _outcome(healthy) == ("ESCALATED", "shadow_matched")


def test_a_shadow_run_without_its_case_is_not_a_match():
    row, _, _ = build_row(FIXTURES / "shadow_no_case")
    assert _outcome(row) == ("UNKNOWN", "shadow_not_compared")
    assert row.fields_written == ()


def test_a_write_that_reached_the_app_in_shadow_mode_is_unknown(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "shadow_matched", run)
    doc = json.loads((run / "run.json").read_text())
    doc["landed_writes"] = 1
    (run / "run.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "shadow_write_landed")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("TIMEOUT", ("FAILED", "shadow_timed_out")),
        ("NO_WRITE_HELD", ("FAILED", "shadow_no_proposal")),
        # P1 sets AGENT_ERROR before its guard counted anything: no evidence.
        ("AGENT_ERROR", ("UNKNOWN", "shadow_agent_error")),
    ],
)
def test_a_shadow_run_that_proposed_nothing(tmp_path, status, expected):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "shadow_no_case", run)
    (run / "proposed_write.json").unlink()
    doc = json.loads((run / "run.json").read_text())
    doc.update(status=status, proposed=False)
    (run / "run.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == expected


# -- J3: guarded saves ----------------------------------------------------------------


def test_a_declined_approval_is_escalated_not_unknown_or_caught():
    row, _, _ = build_row(FIXTURES / "guard_declined")
    assert _outcome(row) == ("ESCALATED", "person_declined")
    # The row's approval times and the item-approval files describe one wait.
    assert len(row.human.intervals) == 1
    assert row.human.intervention_s == 3.181


def test_the_guard_rows_verdict_is_never_a_bucket_source(tmp_path):
    for fixture, expected in (
        ("guard_declined", ("ESCALATED", "person_declined")),
        ("guard_no_persist", ("UNKNOWN", "reconciliation_required")),
        ("guard_approved_ok", ("VERIFIED", "readback_verified")),
    ):
        run = tmp_path / fixture
        shutil.copytree(FIXTURES / fixture, run)
        for verdict in ("CAUGHT_ERROR", "VERIFIED", "ESCALATED", "FAILED"):
            doc = json.loads((run / "guard_ledger_row.json").read_text())
            doc["verdict"] = verdict
            (run / "guard_ledger_row.json").write_text(json.dumps(doc))
            row, _, _ = build_row(run)
            assert _outcome(row) == expected, (fixture, verdict)


def test_one_guarded_run_is_one_row_with_one_bucket():
    # The not-saved run: the guard says RECONCILIATION_REQUIRED, P5's row
    # said ESCALATED and the effect receipt says REFUTED with the effect
    # absent. The guard's rule (REFUTED only when the store shows a write)
    # gives one answer: UNKNOWN.
    row, _, _ = build_row(FIXTURES / "guard_no_persist")
    assert _outcome(row) == ("UNKNOWN", "reconciliation_required")
    assert row.readback_tier == "independent_readback"


def test_a_field_diff_keeps_caught_error_even_with_a_person_in_the_loop():
    row, _, _ = build_row(FIXTURES / "guard_wrong_value")
    assert _outcome(row) == ("CAUGHT_ERROR", "readback_refuted")
    assert {f.name: f.check for f in row.fields_written} == {
        "note": "differs",
        "type": "matched",
    }
    assert row.fields_differing == 1


def test_an_approved_guarded_save_is_verified_with_the_persons_time():
    row, _, _ = build_row(FIXTURES / "guard_approved_ok")
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert len(row.human.intervals) == 1
    assert row.human.intervention_s is not None and row.human.intervention_s > 0


# -- J4: P6 exception answers ----------------------------------------------------------


def _home_with_exceptions(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    shutil.copytree(FIXTURES / "exceptions_home", home)
    return home


def test_exception_answers_are_read_from_the_registry(tmp_path):
    home = _home_with_exceptions(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    result = LedgerStore(home).add(run, run_ref="e2e-answered")
    row = result.row
    assert _outcome(row) == ("ESCALATED", "second_factor")
    assert [i.kind for i in row.human.intervals] == ["second_factor"]
    assert row.human.intervention_s == 2.822
    assert row.task_id == "note-intake"  # the card's task id


def test_an_expired_exception_is_unknown_time_not_zero(tmp_path):
    home = _home_with_exceptions(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    row = LedgerStore(home).add(run, run_ref="e2e-expired").row
    assert row.outcome.value == "ESCALATED"
    assert row.human.intervention_s is None


def test_without_a_run_ref_no_exception_folder_is_read(tmp_path):
    # P6's run reference is the operator's --run-ref; nothing derives it
    # from a run directory, so a folder named like the run isn't guessed.
    home = _home_with_exceptions(tmp_path)
    run = tmp_path / "e2e-answered"
    run.mkdir()
    row = LedgerStore(home).add(run).row
    assert row.outcome_reason == "no_sources"


def test_a_card_from_another_task_is_a_conflict(tmp_path):
    home = _home_with_exceptions(tmp_path)
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "guard_declined", run)
    row = LedgerStore(home).add(run, run_ref="e2e-answered").row
    assert _outcome(row) == ("UNKNOWN", "task_id_conflict")


def test_run_dir_exceptions_folder_is_not_p6s_location(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(
        FIXTURES / "exceptions_home" / "exceptions" / "e2e-answered", run / "exceptions"
    )
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "no_sources")


# -- J5: P7 fallback --------------------------------------------------------------------


def test_an_approved_verified_fallback_is_verified():
    row, _, _ = build_row(FIXTURES / "fallback_verified_readable")
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert row.readback_tier == "independent_readback"
    assert row.executor_kind == "agent"
    assert (row.model.calls, row.model.cost_usd) == (0, 0.0)
    # A person approved at the gate; the gate records one time, so the
    # person's time is unknown, not zero.
    assert row.human.intervention_s is None
    assert row.sources == ("result.json",)


def test_a_refused_gate_is_a_decline_not_schema_unrecognised():
    row, _, _ = build_row(FIXTURES / "fallback_refused_readable")
    assert _outcome(row) == ("ESCALATED", "person_declined")
    # The agent attempt's own run1.json (agent: null) isn't read again.
    row, _, _ = build_row(FIXTURES / "fallback_refused")
    assert row.outcome_reason != "schema_unrecognised"
    assert row.sources == ("result.json",)


def test_a_record_check_without_readable_true_proves_nothing():
    # P7's TaskRecordCheck says readable: false when a read failed; the zero
    # counts then prove nothing. An older check has no flag at all.
    for fixture, verified_or_not in (
        ("fallback_refused_unreadable", ("UNKNOWN", "write_unconfirmed")),
        ("fallback_refused", ("UNKNOWN", "write_unconfirmed")),
        ("fallback_verified", ("UNKNOWN", "reconciliation_required")),
    ):
        row, _, _ = build_row(FIXTURES / fixture)
        assert _outcome(row) == verified_or_not, fixture


def test_the_dispatcher_envelope_is_wrapped_tasks_result_plus_dispatch_fields():
    # Built with P7's own WrappedTask._result and dispatch_cli.finish over
    # the real result.json: no replay or compile, a top-level fallback.
    doc = json.loads((FIXTURES / "dispatch_envelope_refused.json").read_text())
    assert "replay" not in doc and "compile" not in doc
    assert doc["fallback"]["task_record_check"]["created"] == 0
    row, _, _ = build_row(FIXTURES / "dispatch_envelope_refused.json")
    assert row.task_id == "r-119"


def test_the_dispatcher_envelopes_outcome_is_not_read(tmp_path):
    for name, expected in (
        ("dispatch_envelope_verified.json", ("VERIFIED", "readback_verified")),
        ("dispatch_envelope_refused.json", ("ESCALATED", "person_declined")),
    ):
        doc = json.loads((FIXTURES / name).read_text())
        doc["outcome"] = "CAUGHT_ERROR"
        path = tmp_path / name
        path.write_text(json.dumps(doc))
        row, _, _ = build_row(path)
        assert _outcome(row) == expected


def test_a_wrap_record_of_a_verified_fallback_needs_its_record_check(tmp_path):
    env = json.loads((FIXTURES / "dispatch_envelope_verified.json").read_text())
    result = {k: v for k, v in env.items() if k not in ("outcome", "fallback")}
    record = {
        "schema": "openadapt-agent.run/v1",
        "request_id": "r-118",
        "state": "finished",
        "result": result,
    }
    path = tmp_path / "run-0001.json"
    path.write_text(json.dumps(record))
    row, _, _ = build_row(path)
    # verified: true with no readable record check proves nothing.
    assert _outcome(row) == ("UNKNOWN", "reconciliation_required")
    result["record_check"] = {"readable": True, "passed": True, "created": 1}
    path.write_text(json.dumps(record))
    row, _, _ = build_row(path)
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert row.executor_kind == "agent"


def test_a_refused_gate_with_a_write_on_record_is_not_a_clean_decline(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "fallback_refused_readable", run)
    doc = json.loads((run / "result.json").read_text())
    doc["replay"]["fallback"]["task_record_check"]["created"] = 1
    (run / "result.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "write_unconfirmed")


# -- skeptic #13: crash and timeout without evidence -----------------------------------


def test_a_failed_compiled_run_with_no_transaction_outcome_is_unknown(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "compiled_halted", run)
    doc = json.loads((run / "report.json").read_text())
    doc.pop("transaction_outcome", None)
    doc["execution_outcome"] = "FAILED"
    (run / "report.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == ("UNKNOWN", "write_unconfirmed")


# -- (a): declared field names may leave ----------------------------------------------


def _store(tmp_path: Path) -> LedgerStore:
    return LedgerStore(tmp_path / "home")


def test_declared_patient_id_passes_the_scrubber_check(tmp_path):
    store = _store(tmp_path)
    store.add(
        FIXTURES / "run2_fallback_approved",
        config=(RUN2_VERIFY,),
        approvals=RUN2_APPROVALS,
    )
    (line,) = outbound_lines(store, summary=False, scrubber=_Presidio())
    assert '"patient_id"' in line


def test_an_undeclared_rewritable_name_still_refuses(tmp_path):
    store = _store(tmp_path)
    # A lone proposed write: no task, verify or reference config declares
    # its fields.
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(FIXTURES / "shadow_matched" / "proposed_write.json", run)
    store.add(run)
    with pytest.raises(LedgerRefused, match="fields_written.name"):
        outbound_lines(store, summary=False, scrubber=_Presidio())


def test_an_extra_field_the_reference_does_not_declare_refuses(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "shadow_matched", run)
    case = json.loads((run / "case.json").read_text())
    case["fields"].append(
        {"name": "ssn", "kind": "extra", "human": None, "proposed": "x"}
    )
    (run / "case.json").write_text(json.dumps(case))
    store = _store(tmp_path)
    store.add(run)
    # A shadow row carries no field names at all, so none can leave.
    (line,) = outbound_lines(store, summary=False, scrubber=_Presidio("ssn"))
    assert "ssn" not in line


def test_a_failed_scope_case_declares_none_of_the_proposed_names(tmp_path):
    # compare.py returns before it builds the rows for FAILED_SCOPE, so the
    # empty field list says nothing about the agent's own names.
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "shadow_matched", run)
    case = json.loads((run / "case.json").read_text())
    case.update(status="FAILED_SCOPE", reason="a write reached the app", fields=[])
    (run / "case.json").write_text(json.dumps(case))
    store = _store(tmp_path)
    assert store.add(run).row.outcome_reason == "shadow_write_landed"
    assert store.entries()[0]["declared_fields"] == []


def test_another_tasks_guard_task_declares_nothing(tmp_path):
    task = json.loads((FIXTURES / "guard_approved_ok_config" / "task.json").read_text())
    task["task_id"] = "another-task"
    other = tmp_path / "task.json"
    other.write_text(json.dumps(task))
    store = _store(tmp_path)
    store.add(FIXTURES / "guard_approved_ok", config=(other,))
    assert store.entries()[0]["declared_fields"] == []


def test_guard_task_fields_are_declared_through_config(tmp_path):
    config = sorted((FIXTURES / "guard_approved_ok_config").iterdir())
    plain = LedgerStore(tmp_path / "a")
    plain.add(FIXTURES / "guard_approved_ok")
    with pytest.raises(LedgerRefused, match="fields_written.name"):
        outbound_lines(plain, summary=False, scrubber=_Presidio("note"))
    declared = LedgerStore(tmp_path / "b")
    declared.add(FIXTURES / "guard_approved_ok", config=tuple(config))
    (entry,) = declared.entries()
    assert entry["declared_fields"] == ["name", "note", "patient_id", "type"]
    assert outbound_lines(declared, summary=False, scrubber=_Presidio("note"))


def test_an_entry_from_before_declared_fields_exempts_nothing(tmp_path):
    store = _store(tmp_path)
    store.add(
        FIXTURES / "run2_fallback_approved",
        config=(RUN2_VERIFY,),
        approvals=RUN2_APPROVALS,
    )
    body = "".join(
        json.dumps({k: v for k, v in e.items() if k != "declared_fields"}) + "\n"
        for e in store.entries()
    )
    store.rows_path.write_text(body)
    with pytest.raises(LedgerRefused):
        outbound_lines(store, summary=False, scrubber=_Presidio())


def test_the_real_scrubber_lets_every_demo_run_out(tmp_path):
    """openadapt-privacy's own scrubber over the shadow and guard copies."""

    from openadapt_flow.privacy import PrivacyNotAvailable, get_text_scrubber

    try:
        scrubber = get_text_scrubber()
        if scrubber is not None:
            scrubber.scrub_text("probe")  # the spaCy model may be missing
    except (PrivacyNotAvailable, Exception):  # noqa: BLE001
        scrubber = None
    if scrubber is None:
        pytest.skip("openadapt-privacy's text scrubber can't run here")
    store = _store(tmp_path)
    for path in sorted(FIXTURES.iterdir()):
        if path.name == "shadow_no_case":
            continue  # added at the end
        if path.name.startswith("fallback_"):
            continue  # no config declares their read-back field names
        if path.name.startswith(("shadow_", "run2_claude")):
            store.add(path)
        elif path.name == "run2_fallback_approved":
            config = FIXTURES / "run2_fallback_approved_config" / "verify.yaml"
            store.add(path, config=(config,), approvals=FIXTURES / "run2_approvals")
        elif path.name.startswith("guard_") and not path.name.endswith("_config"):
            config = FIXTURES / f"{path.name}_config"
            files = tuple(sorted(config.iterdir())) if config.is_dir() else ()
            store.add(path, config=files)
    lines = outbound_lines(store, summary=True, scrubber=scrubber)
    assert len(lines) == len(store.latest())
    # A shadow run with no case.json: no field names, so it goes too.
    store.add(FIXTURES / "shadow_no_case")
    assert len(outbound_lines(store, summary=True, scrubber=scrubber)) == len(
        store.latest()
    )


# -- J6: the push payload -------------------------------------------------------------


def _schema() -> dict:
    from openadapt_types.run_ledger import AgentRunSummaryV1

    return AgentRunSummaryV1.model_json_schema(by_alias=True)


def test_push_payloads_validate_against_the_json_schema(tmp_path):
    import jsonschema

    store = _store(tmp_path)
    for name in (
        "shadow_matched",
        "guard_declined",
        "guard_no_persist",
        "fallback_refused",
        "compiled_halted_receipt",
    ):
        store.add(FIXTURES / name)
    payloads = push(store, dry_run=True, environ={}, scrubber=_Presidio())
    validator = jsonschema.Draft202012Validator(_schema())
    for payload in payloads:
        validator.validate(payload)
    # Unknown values stay null, never 0.
    halted = next(p for p in payloads if p["outcome_reason"] == "halted_before_effect")
    # The receipt's own read-back times bound the run; no duration is kept.
    assert halted["started_at"] is not None and halted["duration_s"] is None
    assert halted["model"]["calls"] is None and halted["model"]["cost_usd"] is None


def test_the_committed_j6_examples_validate_against_the_json_schema():
    import jsonschema

    validator = jsonschema.Draft202012Validator(_schema())
    lines = J6_EXAMPLES.read_text().splitlines()
    assert len(lines) >= 4
    seen_null = False
    for line in lines:
        payload = json.loads(line)
        validator.validate(payload)
        seen_null |= payload["model"]["calls"] is None
    assert seen_null
    reasons = {json.loads(line)["outcome_reason"] for line in lines}
    assert {"shadow_wrong_record", "person_declined", "readback_verified"} <= reasons


def test_collect_reads_a_shadow_run_as_one_source():
    collected = collect(FIXTURES / "shadow_wrong_record")
    assert [c.collector for c in collected.contributions] == ["readiness_shadow"]
    assert collected.declared_fields == {"note", "patient_id", "type"}


def test_a_field_that_differs_from_any_source_is_a_caught_error(tmp_path):
    # Review on b4e63aa2d: a row could be VERIFIED while a field differed.
    # The guard row's differing field now makes the run CAUGHT_ERROR even
    # when summary.json says VERIFIED.
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "guard_approved_ok", run)
    doc = json.loads((run / "guard_ledger_row.json").read_text())
    doc.update(fields_differing=["note"])
    (run / "guard_ledger_row.json").write_text(json.dumps(doc))
    row, _, _ = build_row(run)
    assert _outcome(row) == ("CAUGHT_ERROR", "readback_field_differs")
    assert ("note", "differs") in [(f.name, f.check) for f in row.fields_written]


def test_the_schema_refuses_verified_with_a_differing_field():
    from openadapt_types.run_ledger import RunLedgerRowV1

    row, _, _ = build_row(FIXTURES / "guard_approved_ok")
    doc = row.to_json()
    doc["fields_differing"] = 1
    with pytest.raises(ValueError, match="differs"):
        RunLedgerRowV1.model_validate(doc)


# -- a person's time: unknown unless a source shows nobody was asked ----------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("shadow_matched", 0.0),  # the shadow hold asks nobody
        ("guard_no_persist", 0.0),  # approval not_gated
        ("compiled_verified", None),  # a compiled run's report doesn't say
        ("compiled_halted_receipt", None),
        ("fallback_verified", None),  # a person approved at a gate
        ("dispatch_envelope_refused.json", None),  # no gate times at all
    ],
)
def test_no_intervals_is_unknown_unless_nobody_was_asked(fixture, expected):
    row, _, _ = build_row(FIXTURES / fixture)
    assert row.human.intervention_s == expected


# -- task ids: a rewritten one leaves as an opaque id, never as text --------------


class _ReadsTaskIdAsOrg(_Presidio):
    """What Presidio does to P1's extra-target sample's task id."""

    def __init__(self, *more: str) -> None:
        super().__init__("readiness-mockmed-0002", *more)


def test_a_task_id_the_scrubber_rewrites_leaves_as_an_opaque_id(tmp_path):
    store = _store(tmp_path)
    store.add(FIXTURES / "shadow_extra_target")
    (line,) = outbound_lines(store, summary=True, scrubber=_ReadsTaskIdAsOrg())
    sent = json.loads(line)["task_id"]
    assert re.fullmatch(r"tid-[0-9a-f]{16}", sent)
    assert "readiness-mockmed-0002" not in line
    # Stable per task, and the key stays here, owner-only.
    assert store.opaque_task_id("readiness-mockmed-0002") == sent
    key = store.root / "task-id.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # Kept locally as it was.
    assert store.rows()[0].task_id == "readiness-mockmed-0002"


def test_a_caller_chosen_request_id_never_leaves_as_text(tmp_path):
    # A wrap-dispatch request id is chosen by the agent that calls it.
    store = _store(tmp_path)
    store.add(FIXTURES / "run2_envelope_happy.json")
    store.add(FIXTURES / "run2_envelope_declined.json")
    lines = outbound_lines(
        store,
        summary=True,
        scrubber=_ReadsTaskIdAsOrg("dispatch-happy", "dispatch-drift-declined"),
    )
    ids = {json.loads(line)["task_id"] for line in lines}
    assert len(ids) == 2 and all(i.startswith("tid-") for i in ids)
    assert not any("dispatch-" in line for line in lines)


def test_another_install_gets_another_opaque_id(tmp_path):
    a, b = LedgerStore(tmp_path / "a"), LedgerStore(tmp_path / "b")
    assert a.opaque_task_id("x") != b.opaque_task_id("x")


# -- P5's current row (guard_verdict, verdict_reason) ---------------------------------


@pytest.mark.parametrize("fixture", ["guard_row_p5_verified", "guard_row_p5_declined"])
def test_p5s_current_row_alone_decides_no_bucket(fixture):
    # Real rows from P5's d0a1e92f3 (its own tests against MockMed's fault
    # server): verdict VERIFIED / ESCALATED, guard_verdict, verdict_reason.
    doc = json.loads((FIXTURES / fixture / "guard_ledger_row.json").read_text())
    assert {"guard_verdict", "verdict_reason"} <= set(doc)
    row, _, _ = build_row(FIXTURES / fixture)
    assert _outcome(row) == ("UNKNOWN", "completed_unverified")
    assert row.readback_tier == doc["readback_tier"]


def test_p5s_row_times_still_count():
    row, _, _ = build_row(FIXTURES / "guard_row_p5_declined")
    assert len(row.human.intervals) == 1
    assert row.human.intervention_s is not None


# -- round 3: the integrated run2 (2026-11-22) ---------------------------------------
#
# run2_* are copies of /tmp/acp-demo-out/run2: 5-claude's real Claude readiness
# run, 4-fallback's results, envelopes and item-approval folders (request and
# answer JSON only), the compiled verify.yaml a person confirmed, and a
# 2-gated approved save.

RUN2_APPROVALS = FIXTURES / "run2_approvals"
RUN2_VERIFY = FIXTURES / "run2_fallback_approved_config" / "verify.yaml"


def test_a_real_claude_readiness_run_with_no_proposal_is_a_shadow_reason():
    # NO_WRITE_HELD: the agent said it wasn't done and proposed nothing.
    row, _, _ = build_row(FIXTURES / "run2_claude_no_write")
    assert _outcome(row) == ("FAILED", "shadow_no_proposal")
    assert row.outcome_reason.startswith("shadow_")
    assert (row.model.calls, row.model.cost_source) == (2, "metered")
    # The proxy's timeline names the run's first and last model traffic.
    assert row.started_at == "2026-11-22T05:10:55.470000Z"
    assert row.ended_at is not None and row.duration_s == 7.603


@pytest.mark.parametrize(
    "fixture",
    [
        "shadow_matched",
        "shadow_mismatched",
        "shadow_extra_target",
        "shadow_egress",
        "shadow_wrong_record",
        "shadow_no_case",
        "run2_claude_no_write",
    ],
)
def test_every_shadow_row_has_a_shadow_reason(fixture):
    row, _, _ = build_row(FIXTURES / fixture)
    assert row.outcome_reason.startswith("shadow_") or (
        row.outcome_reason == "egress_refused"
    )


def test_the_approved_fallback_has_the_persons_time_and_its_fields(tmp_path):
    row = (
        _store(tmp_path)
        .add(
            FIXTURES / "run2_fallback_approved",
            config=(RUN2_VERIFY,),
            approvals=RUN2_APPROVALS,
        )
        .row
    )
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    # The signed approval: request created_at to answer answered_at.
    assert [i.kind for i in row.human.intervals] == ["approval"]
    assert row.human.intervention_s == 0.062
    # The record check's names that the confirmed verify.yaml declares.
    assert [(f.name, f.check) for f in row.fields_written] == [
        ("note", "matched"),
        ("patient_id", "matched"),
        ("type", "matched"),
    ]
    assert row.started_at is not None and row.ended_at is not None


def test_without_the_approvals_folder_the_persons_time_is_unknown(tmp_path):
    alone = tmp_path / "result-drift-approved.json"
    shutil.copy(FIXTURES / "run2_envelope_approved.json", alone)
    row, _, _ = build_row(alone)
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert row.human.intervention_s is None


def test_the_envelope_finds_its_own_approvals_folder_by_request_id():
    # The fixtures folder holds run2_approvals/<fallback request id>/: the
    # gate_answers' request id and sha256 bind it to this envelope.
    row, _, _ = build_row(FIXTURES / "run2_envelope_approved.json")
    assert row.human.intervention_s == 0.062


def test_the_envelope_with_its_approvals_folder(tmp_path):
    row = (
        _store(tmp_path)
        .add(FIXTURES / "run2_envelope_declined.json", approvals=RUN2_APPROVALS)
        .row
    )
    assert _outcome(row) == ("ESCALATED", "person_declined")
    assert row.human.intervention_s == 0.02
    assert row.task_id == "dispatch-drift-declined"


def test_times_come_from_the_runs_own_files():
    row, _, _ = build_row(FIXTURES / "run2_guard_approved")
    # The guard held the save at t=2.829 s; the approval request was created
    # then (05:02:08.735233), so the run started 2.829 s before that.
    assert row.started_at == "2026-11-22T05:02:05.906233Z"
    assert row.ended_at == "2026-11-22T05:02:13.621233Z"
    assert row.duration_s == 7.715
    row, _, _ = build_row(FIXTURES / "run2_envelope_happy.json")
    assert row.started_at is None and row.ended_at is None  # no source has one


# -- round 3: one bad row refuses itself, not the ledger ----------------------------


def _two_rows(tmp_path: Path) -> LedgerStore:
    store = _store(tmp_path)
    store.add(FIXTURES / "shadow_matched")
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(FIXTURES / "shadow_matched" / "proposed_write.json", run)
    store.add(run)  # a lone proposed write: patient_id undeclared
    return store


def test_screen_rows_refuses_only_the_bad_row(tmp_path):
    from openadapt_flow.run_ledger.cli import screen_rows

    store = _two_rows(tmp_path)
    screened = screen_rows(store, summary=True, scrubber=_Presidio())
    assert len(screened.lines) == 1
    ((row_id, reason),) = screened.refused
    assert reason == "the scrubber would rewrite its fields_written.name"
    assert row_id not in screened.lines[0]


def test_push_sends_the_good_rows_and_lists_the_refused_ones(tmp_path):
    store = _two_rows(tmp_path)
    sent: list[dict] = []
    refused: list[tuple[str, str]] = []
    results = push(
        store,
        host="https://app.openadapt.ai",
        poster=lambda url, headers, payload: sent.append(payload) or 202,
        environ={"OPENADAPT_API_KEY": "test-key-not-real"},
        scrubber=_Presidio(),
        refused=refused,
    )
    assert len(sent) == len(results) == 1 and len(refused) == 1


def test_export_writes_the_good_rows_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    import openadapt_flow.privacy as privacy
    from openadapt_flow.run_ledger.cli import main

    home = tmp_path / "home"
    monkeypatch.setenv("OPENADAPT_HOME", str(home))
    monkeypatch.setattr(privacy, "get_text_scrubber", lambda: _Presidio())
    _two_rows(tmp_path)
    out = tmp_path / "export.jsonl"
    assert main(["ledger", "export", "--out", str(out)]) == 2
    assert len(out.read_text().splitlines()) == 1
    assert "refused row" in capsys.readouterr().err


# -- round 4: an allowlist of files, nothing else is read ---------------------------

PLANTED = ("Zelda Quintanilla-Hart", "MRN-PLANTED-7731")


def _planted_run(tmp_path: Path) -> Path:
    """A real readiness run plus P3's pseudonym map (real values, 0600) and a
    stray file, both unreadable so any attempt to open them would show."""

    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "run2_claude_no_write", run)
    pseudonyms = run / "egress" / "pseudonyms.json"
    pseudonyms.write_text(
        json.dumps({"<PERSON_1>": PLANTED[0], "<ID_1>": PLANTED[1]}), encoding="utf-8"
    )
    stray = run / "notes.json"
    stray.write_text(
        json.dumps(
            {
                "schema": "openadapt.flight-run/v1",
                "task_id": PLANTED[1],
                "note": PLANTED[0],
            }
        )
    )
    for path in (pseudonyms, stray):
        path.chmod(0o000)
    return run


def test_files_off_the_allowlist_never_leave(tmp_path):
    run = _planted_run(tmp_path)
    clean, _, _ = build_row(FIXTURES / "run2_claude_no_write")
    try:
        store = _store(tmp_path)
        row = store.add(run, task_type="intake").row
        assert _outcome(row) == _outcome(clean)
        assert "egress/pseudonyms.json" not in row.sources
        assert "notes.json" not in row.sources
        payloads = push(store, dry_run=True, environ={}, scrubber=_Presidio())
        exported = outbound_lines(store, summary=False, scrubber=_Presidio())
        stored = store.rows_path.read_text()
        sidecars = "".join(p.read_text() for p in store.private_root.rglob("*.json"))
        for text in (json.dumps(payloads), "".join(exported), stored, sidecars):
            for value in PLANTED:
                assert value not in text
    finally:
        for path in (run / "egress" / "pseudonyms.json", run / "notes.json"):
            path.chmod(0o600)


def test_a_file_off_the_allowlist_named_directly_or_as_config_is_ignored(tmp_path):
    run = _planted_run(tmp_path)
    pseudonyms = run / "egress" / "pseudonyms.json"
    try:
        row, _, _ = build_row(pseudonyms)
        assert _outcome(row) == ("UNKNOWN", "no_sources") and row.sources == ()
        collected = collect(run, config=(pseudonyms, run / "notes.json"))
        assert collected.config_paths == []
        assert all(
            p.name not in ("pseudonyms.json", "notes.json") for p in collected.files
        )
    finally:
        for path in (pseudonyms, run / "notes.json"):
            path.chmod(0o600)


@pytest.mark.parametrize(
    ("name", "direct", "allowed"),
    [
        ("egress/pseudonyms.json", False, False),
        ("egress/pseudonyms.json", True, False),
        ("egress/timeline.jsonl", False, True),
        ("timeline.jsonl", False, False),
        ("notes.json", False, False),
        ("run.json", False, True),
        ("item-approval/request-abc.json", False, True),
        ("item-approval/consumed-abc", False, False),
        ("runs/run-0001.json", False, True),
        ("runs/pseudonyms.json", False, False),
        ("result-drift-approved.json", True, True),
        ("verify.proposed.yaml", False, False),
    ],
)
def test_the_allowlist(name, direct, allowed):
    from openadapt_flow.run_ledger.collect import allowed_file

    assert allowed_file(Path("/x") / name, direct=direct) is allowed


# -- round 5: run3 (2026-11-22) accuracy findings -----------------------------------
#
# run3_* are copies of /tmp/acp-demo-out/run3 (allowlisted files only).

RUN3_FALLBACK = FIXTURES / "run3_fallback"


def test_a_cleared_exception_then_a_verified_save_is_verified():
    # P6: the person typed the code (done_by_person); the guard then read the
    # save back independently. VERIFIED, with the person's time recorded.
    exceptions = FIXTURES / "run3_exceptions_home" / "exceptions" / "watch-2fa-run3"
    row, _, _ = build_row(FIXTURES / "run3_watch", exceptions=exceptions)
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert [i.kind for i in row.human.intervals] == ["second_factor"]
    assert row.human.intervention_s == 0.059


def test_a_stopped_exception_stays_escalated_even_after_a_verified_save(tmp_path):
    folder = tmp_path / "watch-2fa-run3"
    shutil.copytree(
        FIXTURES / "run3_exceptions_home" / "exceptions" / "watch-2fa-run3", folder
    )
    (answer,) = folder.glob("answer-*.json")
    doc = json.loads(answer.read_text())
    doc["answer"] = "abort"  # the person stopped the run
    answer.write_text(json.dumps(doc))
    row, _, _ = build_row(FIXTURES / "run3_watch", exceptions=folder)
    assert _outcome(row) == ("ESCALATED", "second_factor")


def test_run3_shadow_rows_write_no_fields():
    row, _, _ = build_row(FIXTURES / "run3_shadow_matched")
    assert _outcome(row) == ("ESCALATED", "shadow_matched")
    assert row.fields_written == ()


@pytest.mark.parametrize(
    "fixture",
    [
        "run3_adversarial",  # the hold's held/0.json held_at
        "run3_guard_wrong_patient",  # recording/meta.json created_at
        "run3_fallback/result-happy.json",  # wrap's run store record
        "run3_fallback/result-drift-approved.json",
        "run3_fallback/result-drift-declined.json",
    ],
)
def test_run3_rows_have_start_times(fixture):
    row, _, _ = build_row(FIXTURES / fixture)
    assert row.started_at is not None and row.ended_at is not None


def test_the_approved_fallback_envelope_finds_its_approval_and_fields():
    row, _, _ = build_row(RUN3_FALLBACK / "result-drift-approved.json")
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert [i.kind for i in row.human.intervals] == ["approval"]
    assert row.human.intervention_s == 0.043
    # From the fallback's own result.json record check, kept to the names
    # the task's confirmed compiled/verify.yaml declares.
    assert [(f.name, f.check) for f in row.fields_written] == [
        ("note", "matched"),
        ("patient_id", "matched"),
        ("type", "matched"),
    ]


def test_a_gate_answer_for_another_request_is_not_read(tmp_path):
    copy = tmp_path / "fb"
    shutil.copytree(RUN3_FALLBACK, copy)
    envelope = copy / "result-drift-approved.json"
    doc = json.loads(envelope.read_text())
    doc["fallback"]["gate_answers"][0]["request_sha256"] = "sha256:" + "0" * 64
    envelope.write_text(json.dumps(doc))
    row, _, _ = build_row(envelope)
    # The folder's answer belongs to another request: fail closed.
    assert _outcome(row) == ("UNKNOWN", "source_unreadable")


def test_the_decline_rows_duration_is_the_runs_own_span():
    # The guard idled 54 s after the decline until its timeout; the run ends
    # at its last save decision (t=5.893). The person answered 3.191 s after
    # the card opened.
    row, _, _ = build_row(FIXTURES / "run3_guard_decline")
    assert _outcome(row) == ("ESCALATED", "person_declined")
    assert row.duration_s == 5.893
    assert row.human.intervention_s == 3.191
    assert row.started_at == "2026-11-22T05:56:43.053948Z"


# -- review fixes ---------------------------------------------------------------------


def test_p5s_values_unread_block_is_escalated_values_unread():
    # P5 6fa9c3a36: the card couldn't show the values, so the guard asked no
    # one and blocked the save. summary.json and the row are P5's own output
    # (its unit test; summary.json is CommitGuard.summary.to_dict() as
    # agent_guard/session.py writes it).
    row, _, _ = build_row(FIXTURES / "guard_values_unread")
    assert _outcome(row) == ("ESCALATED", "values_unread")
    assert row.human.intervention_s == 0.0  # nobody was asked


def test_an_answered_question_stays_escalated():
    # The ledger never reads a question's answer text, so it can't tell
    # whether the answer let the run go on: an answered question is still
    # a person's step (ESCALATED question_asked), unlike a cleared exception.
    row, _, _ = build_row(FIXTURES / "task_ledger_question")
    assert _outcome(row) == ("ESCALATED", "question_asked")
    assert row.human.intervention_s == 120.0
