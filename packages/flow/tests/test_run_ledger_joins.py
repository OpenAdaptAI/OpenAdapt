"""The joins with the agent-control-plane pieces, on copies of their real files.

Every fixture here is a copy of a synthetic artifact the producer wrote in the
end-to-end run of 2026-11-21 (only local paths were shortened): P1 readiness
run directories (J1), P5 guarded saves (J3), P6 exception folders (J4), P7
fallback results and the dispatcher envelope built with P7's own ``finish``
(J5). ``shadow_egress/egress.json`` is the one exception: P3 has not written a
real egress summary yet, so it follows J7's keys.
"""

from __future__ import annotations

import json
import shutil
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
    # P5's old shadow-hold row says CAUGHT_ERROR; it isn't read at all.
    assert "guard_ledger_row.json" not in row.sources
    assert "guard_events.jsonl" not in row.sources
    assert set(row.sources) == {
        "case.json",
        "proposed_write.json",
        "run.json",
        "usage.json",
    }
    assert [(f.name, f.check) for f in row.fields_written] == [
        ("note", "matched"),
        ("patient_id", "not_checked"),
        ("type", "matched"),
    ]


def test_shadow_time_and_cost_come_from_the_run():
    row, _, _ = build_row(FIXTURES / "shadow_matched")
    assert row.duration_s == 2.001  # agent_wall_s
    assert row.model.calls == 6
    assert (row.model.input_tokens, row.model.output_tokens) == (12000, 600)
    assert (row.model.cost_usd, row.model.cost_source) == (0.03, "reported")
    assert row.executor_kind == "agent"
    assert row.executor_label == "claude-sonnet-5-5"


def test_metered_egress_cost_beats_the_agents_report():
    row, _, _ = build_row(FIXTURES / "shadow_egress")
    assert (row.model.cost_usd, row.model.cost_source) == (0.0412, "metered")
    assert row.model.calls == 6 and row.model.input_tokens == 12000


def test_a_wrong_record_shadow_is_the_error_the_shadow_caught():
    row, private, _ = build_row(FIXTURES / "shadow_wrong_record")
    assert _outcome(row) == ("CAUGHT_ERROR", "shadow_wrong_record")
    checks = {f.name: f.check for f in row.fields_written}
    assert checks == {"note": "matched", "patient_id": "differs", "type": "matched"}
    assert row.fields_differing == 1
    assert '"p3"' not in json.dumps(row.to_json())
    assert private.fields["patient_id"] == {
        "intended": "p3",
        "saved": "p2",
        "diff": "--- intended\n+++ saved\n@@ -1 +1 @@\n-p3\n+p2",
    }


def test_a_mismatched_shadow_is_caught():
    row, _, _ = build_row(FIXTURES / "shadow_mismatched")
    assert _outcome(row) == ("CAUGHT_ERROR", "shadow_field_mismatch")
    assert row.fields_differing >= 1


def test_a_shadow_run_without_its_case_is_not_a_match():
    row, _, _ = build_row(FIXTURES / "shadow_no_case")
    assert _outcome(row) == ("UNKNOWN", "shadow_not_compared")
    assert {f.check for f in row.fields_written} == {"not_checked"}


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
        ("TIMEOUT", ("FAILED", "timed_out_no_write")),
        ("NO_WRITE_HELD", ("FAILED", "agent_gave_up")),
        # P1 sets AGENT_ERROR before its guard counted anything: no evidence.
        ("AGENT_ERROR", ("UNKNOWN", "write_unconfirmed")),
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


def test_the_folder_named_like_the_run_is_used_without_a_run_ref(tmp_path):
    home = _home_with_exceptions(tmp_path)
    run = tmp_path / "e2e-answered"
    run.mkdir()
    row = LedgerStore(home).add(run).row
    assert row.outcome_reason == "second_factor"


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
    row, _, _ = build_row(FIXTURES / "fallback_verified")
    assert _outcome(row) == ("VERIFIED", "readback_verified")
    assert row.readback_tier == "independent_readback"
    assert row.executor_kind == "agent"
    assert (row.model.calls, row.model.cost_usd) == (0, 0.0)
    # A person approved at the gate; the gate records one time, so the
    # person's time is unknown, not zero.
    assert row.human.intervention_s is None
    assert row.sources == ("result.json",)


def test_a_refused_gate_is_a_decline_not_schema_unrecognised():
    row, _, _ = build_row(FIXTURES / "fallback_refused")
    assert _outcome(row) == ("ESCALATED", "person_declined")
    # The agent attempt's own run1.json (agent: null) isn't read again.
    assert row.sources == ("result.json",)


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


def test_a_refused_gate_with_a_write_on_record_is_not_a_clean_decline(tmp_path):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "fallback_refused", run)
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
    for name in ("shadow_matched", "shadow_wrong_record", "shadow_mismatched"):
        store.add(FIXTURES / name)
    lines = outbound_lines(store, summary=False, scrubber=_Presidio())
    assert len(lines) == 3
    assert any('"patient_id"' in line for line in lines)


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
    with pytest.raises(LedgerRefused, match="fields_written.name"):
        outbound_lines(store, summary=False, scrubber=_Presidio("ssn"))


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
    store.add(FIXTURES / "shadow_matched")
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
    except PrivacyNotAvailable:
        scrubber = None
    if scrubber is None:
        pytest.skip("openadapt-privacy's text scrubber isn't installed")
    store = _store(tmp_path)
    for path in sorted(FIXTURES.iterdir()):
        if path.name == "shadow_no_case":
            continue  # nothing declares its fields: see the test below
        if path.name.startswith(("shadow_", "fallback_")):
            store.add(path)
        elif path.name.startswith("guard_") and not path.name.endswith("_config"):
            config = FIXTURES / f"{path.name}_config"
            store.add(path, config=tuple(sorted(config.iterdir())))
    lines = outbound_lines(store, summary=True, scrubber=scrubber)
    assert len(lines) == len(store.latest())
    # With no case.json, nothing declares patient_id: the export refuses.
    store.add(FIXTURES / "shadow_no_case")
    with pytest.raises(LedgerRefused, match="fields_written.name"):
        outbound_lines(store, summary=True, scrubber=scrubber)


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
    assert halted["started_at"] is None and halted["duration_s"] is None
    assert halted["model"]["calls"] is None and halted["model"]["cost_usd"] is None


def test_the_committed_j6_examples_are_what_push_sends():
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
