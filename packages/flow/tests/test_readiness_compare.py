"""The readiness comparator and scorecard, on planted synthetic cases."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from openadapt_flow.readiness.compare import compare_case, contamination, values_equal
from openadapt_flow.readiness.runner import SCOPE_STATEMENT
from openadapt_flow.readiness.scorecard import build_scorecard, render_html

RECORD = {"patient_id": "p1", "name": "Jane Sample"}


def _ref(**fields: Any) -> dict[str, Any]:
    return {
        "schema": "openadapt.reference-episode/v1",
        "task_id": "case-000001",
        "expected_record": RECORD,
        "human_fields": [{"name": "patient_id", "value": "p1"}]
        + [{"name": k, "value": v} for k, v in fields.items()],
        "human_duration_s": 60.0,
    }


#: A run whose target gate saw nothing outside the guarded tab.
CLEAN_GATE = {"extra_targets": {}, "ungated_targets": {}, "gate_lost": False, "errors": []}


def _run(**kw: Any) -> dict[str, Any]:
    return {
        "status": "HELD",
        "agent_wall_s": 20.0,
        "landed_writes": 0,
        "target_gate": CLEAN_GATE,
        **kw,
    }


def _proposed(ok: bool = True, parse: str = "json", **fields: Any) -> dict[str, Any]:
    return {
        "identity": {"ok": ok, "code": "verified" if ok else "identity_mismatch"},
        "fields": [{"name": "patient_id", "value": "p1", "parse": parse}]
        + [{"name": k, "value": v, "parse": parse} for k, v in fields.items()],
    }


SCREEN = "Jane Sample — MRN P1 — DOB 1980-01-01"


def _case(ref: dict, proposed: Optional[dict], run: Optional[dict] = None, **kw: Any):
    return compare_case(ref, run if run is not None else _run(), proposed,
                        before_screen=kw.pop("before_screen", SCREEN), **kw)


@pytest.mark.parametrize(
    ("human", "proposed", "same"),
    [
        (None, "", True),
        ("", None, True),
        ("  a  b ", "a b", True),
        ("1.50", "1.5", True),
        ("10", "10.0", True),
        ("10", "11", False),
        ("2026-01-05", "2026-01-05T00:00:00Z", True),
        ("2026-01-05", "2026-01-06", False),
        ("Jane", "jane", False),
        ("x", None, False),
    ],
)
def test_values_equal(human: Any, proposed: Any, same: bool) -> None:
    assert values_equal(human, proposed) is same


def test_codec_applies() -> None:
    from openadapt_flow.tx.types import FieldCodec

    assert values_equal("Jane", "jane", FieldCodec(kind="casefold"))


def test_matched() -> None:
    case = _case(_ref(type="Triage", note="Cough, 3 days"),
                 _proposed(type="Triage", note=" Cough,  3 days"))
    assert case["status"] == "MATCHED"
    assert {r["kind"] for r in case["fields"]} == {"matched"}


def test_planted_errors() -> None:
    ref = _ref(type="Triage", note="n", dose="5")
    mism = _case(ref, _proposed(type="Consult", note="n", dose="5"))
    assert mism["status"] == "MISMATCHED"
    assert [(r["name"], r["kind"], r["human"], r["proposed"]) for r in mism["fields"]
            if r["kind"] != "matched"] == [("type", "mismatched", "Triage", "Consult")]
    missing = _case(ref, _proposed(type="Triage", dose="5"))
    assert [(r["name"], r["kind"]) for r in missing["fields"] if r["kind"] != "matched"] == [
        ("note", "missing")]
    extra = _case(ref, _proposed(type="Triage", note="n", dose="5", billing="999"))
    assert extra["status"] == "MISMATCHED"
    assert extra["undeclared_fields"] == 1
    ignored = _case(ref, _proposed(type="Triage", note="n", dose="5", billing="999"),
                    ignore_fields=["billing"])
    assert ignored["status"] == "MATCHED"


def test_wrong_record() -> None:
    ref = _ref(type="Triage")
    by_screen = _case(ref, _proposed(ok=False, type="Triage"))
    assert by_screen["status"] == "WRONG_RECORD"
    body = _proposed(type="Triage")
    body["fields"][0]["value"] = "p2"
    by_body = _case(ref, body)
    assert by_body["status"] == "WRONG_RECORD"
    assert [r["kind"] for r in by_body["fields"]][0] == "wrong_record"


def test_uncompared_is_never_matched() -> None:
    ref = _ref(type="Triage", note="n")
    unreadable = {"identity": {"ok": True, "code": "verified"},
                  "fields": [{"name": "<body>", "value": None, "parse": "uncompared"}]}
    case = _case(ref, unreadable)
    assert case["status"] == "NOT_CHECKED"
    assert {r["kind"] for r in case["fields"]} == {"uncompared"}
    one = _proposed(type="Triage", note="n")
    one["fields"][2] = {"name": "note", "value": None, "parse": "uncompared"}
    assert _case(ref, one)["status"] == "NOT_CHECKED"
    human_unreadable = _ref(type="Triage")
    human_unreadable["human_fields"].append({"name": "file", "value": None, "parse": "uncompared"})
    assert _case(human_unreadable, _proposed(type="Triage", file="x"))["status"] == "NOT_CHECKED"


def test_contamination() -> None:
    ref = _ref(type="Triage", note="Cough for three days")
    shown = SCREEN + "\nPrevious: Triage — Cough for three days"
    case = _case(ref, _proposed(type="Triage", note="Cough for three days"),
                 before_screen=shown)
    assert case["status"] == "NOT_COMPARABLE"
    half = _case(ref, _proposed(type="Triage", note="Cough for three days"),
                 before_screen=SCREEN + " Triage")
    assert half["status"] == "NOT_COMPARABLE"  # one of two is at least half
    assert contamination(SCREEN, [{"name": "note", "value": "Cough"}])["not_comparable"] is False
    no_screen = _case(ref, _proposed(type="Triage", note="Cough for three days"),
                      before_screen=None)
    assert no_screen["status"] == "NOT_COMPARABLE"


def test_not_run_and_failed_scope() -> None:
    ref = _ref(type="Triage")
    assert compare_case(ref, None, None)["status"] == "NOT_CHECKED"
    assert _case(ref, None, run=_run(status="TIMEOUT"))["reason"] == "timeout"
    assert _case(ref, _proposed(type="Triage"), run=_run(landed_writes=1))["status"] == "FAILED_SCOPE"


def _cases() -> dict[str, Any]:
    ref = _ref(type="Triage", note="secret-ish value 123")
    cases = [
        _case(ref, _proposed(type="Triage", note="secret-ish value 123"),
              run=_run(model_cost_usd=0.02)),
        _case(ref, _proposed(type="Consult", note="secret-ish value 123"),
              run=_run(model_cost_usd=0.04, agent_wall_s=30.0)),
        _case(ref, _proposed(ok=False, type="Triage", note="x"), run=_run(model_cost_usd=0.0)),
    ]
    for i, c in enumerate(cases):
        c["task_id"] = f"case-00000{i}"
    return {"agent_label": "scripted", "mode": "sandbox", "cases": cases}


def test_scorecard_json_has_no_values() -> None:
    doc = _cases()
    card = build_scorecard(doc, hourly_cost=60.0)
    assert card["schema"] == "openadapt.readiness-scorecard/v1"
    assert card["scope_statement"] == SCOPE_STATEMENT
    assert (card["cases_total"], card["matched"], card["mismatched"]) == (3, 1, 2)
    assert card["risks"]["wrong_record"] == 1
    assert {(e["task_id"], e["field"], e["kind"]) for e in card["field_errors"]} == {
        ("case-000001", "type", "mismatched"),
        ("case-000002", "<record>", "wrong_record"),
    }
    assert card["model_cost_usd"] == {
        "total": 0.06, "per_task": 0.02, "unknown_count": 0, "source": "reported"
    }
    assert card["agent_time_s"]["median"] == 20.0
    assert card["savings"]["minutes_saved_per_task"] == round(40 / 60, 3)
    assert card["savings"]["est_value_usd"] == round(40 / 60 * 1 / 60 * 60 - 0.06, 2)
    text = json.dumps(card)
    for value in ("secret-ish", "Triage", "Consult", "Jane", "p1"):
        assert value not in text
    page = render_html(card, doc)
    assert "Consult" in page and "zero write risk" not in page.lower()


def test_savings_unknown_cost_is_not_estimated() -> None:
    doc = _cases()
    doc["cases"][0]["model_cost_usd"] = None
    card = build_scorecard(doc, hourly_cost=60.0)
    assert card["savings"]["est_value_usd"] is None
    assert card["model_cost_usd"]["unknown_count"] == 1
    assert build_scorecard(_cases())["savings"]["est_value_usd"] is None


def test_usage_pricing_and_program_launcher(tmp_path) -> None:
    from pathlib import Path

    from openadapt_flow.readiness.launcher import (
        LaunchJob,
        ProgramLauncher,
        approval_fields,
        price_usage,
        read_usage,
    )

    assert price_usage({"cost_usd": 0.5}) == 0.5
    assert price_usage({"model": "claude-sonnet-5-5", "input_tokens": 1_000_000,
                        "output_tokens": 100_000}) == 3.0
    assert price_usage({"model": "unknown-model", "input_tokens": 1, "output_tokens": 1}) is None
    assert price_usage({"calls": 3}) is None
    assert read_usage(Path(tmp_path) / "none.json") is None

    job = LaunchJob(task_id="t-00000001", instruction="do it", inputs={"note": "n"},
                    cdp_url="http://127.0.0.1:9", page_url="http://127.0.0.1:1/",
                    run_dir=Path(tmp_path))
    argv = ProgramLauncher(Path(tmp_path) / "bundle").flow_args(job)
    assert argv[:2] == ["run", str(Path(tmp_path) / "bundle")]
    assert argv[argv.index("--approval-mode") + 1] == "each_item"
    assert "note=n" in argv
    folder = Path(tmp_path) / "program-run" / "item-approval"
    folder.mkdir(parents=True)
    (folder / "r.json").write_text(json.dumps({
        "schema_version": "openadapt.item-approval-request/v1",
        "fields": [{"label": "note", "before": None, "after": "n"}],
    }))
    assert approval_fields(Path(tmp_path) / "program-run") == [
        {"name": "note", "value": "n", "parse": "form"}]
