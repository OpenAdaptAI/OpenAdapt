"""The run-ledger row is closed, value-free, and self-consistent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from openadapt_types.run_ledger import (
    AGENT_RUN_SUMMARY_SCHEMA,
    RUN_LEDGER_OUTCOME_PRECEDENCE,
    RUN_LEDGER_REASON_OUTCOME,
    RUN_LEDGER_SCHEMA,
    AgentRunSummaryV1,
    RunLedgerHumanV1,
    RunLedgerIntervalV1,
    RunLedgerModelUsageV1,
    RunLedgerOutcome,
    RunLedgerOutcomeReason,
    RunLedgerRowV1,
)

ROW_ID = "0b6f3f9e-6a43-4c63-9f6e-1d2a3b4c5d6e"


def _row(**overrides: Any) -> RunLedgerRowV1:
    body: dict[str, Any] = {
        "row_id": ROW_ID,
        "task_id": "task-0001",
        "task_type": "intake",
        "executor_kind": "agent",
        "executor_label": "browser-use",
        "outcome": "VERIFIED",
        "outcome_reason": "readback_verified",
        "readback_tier": "independent_readback",
        "sources": ["summary.json"],
    }
    body.update(overrides)
    return RunLedgerRowV1.model_validate(body)


def _strings(schema: dict[str, Any], defs: dict[str, Any], path: str = "") -> list[str]:
    """Every string-typed node with no pattern, const or enum."""

    open_: list[str] = []
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return _strings(defs[name], defs, path + "->" + name)
    if schema.get("type") == "string" and not (
        "pattern" in schema or "const" in schema or "enum" in schema
    ):
        open_.append(path or "<root>")
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        for i, sub in enumerate(schema.get(key, [])):
            open_ += _strings(sub, defs, f"{path}[{key}{i}]")
    if isinstance(schema.get("items"), dict):
        open_ += _strings(schema["items"], defs, path + "[]")
    for name, sub in (schema.get("properties") or {}).items():
        open_ += _strings(sub, defs, f"{path}.{name}")
    return open_


@pytest.mark.parametrize("model", [RunLedgerRowV1, AgentRunSummaryV1])
def test_no_string_field_accepts_free_text(model: Any) -> None:
    schema = model.model_json_schema(by_alias=True)
    assert _strings(schema, schema.get("$defs", {})) == []


def test_every_reason_names_exactly_one_bucket() -> None:
    assert set(get_args(RunLedgerOutcomeReason)) == set(RUN_LEDGER_REASON_OUTCOME)
    for outcome in RunLedgerOutcome:
        assert outcome in RUN_LEDGER_REASON_OUTCOME.values(), outcome
    assert RUN_LEDGER_OUTCOME_PRECEDENCE == (
        RunLedgerOutcome.CAUGHT_ERROR,
        RunLedgerOutcome.UNKNOWN,
        RunLedgerOutcome.ESCALATED,
        RunLedgerOutcome.FAILED,
        RunLedgerOutcome.VERIFIED,
    )


def test_a_reason_from_another_bucket_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a VERIFIED reason"):
        _row(outcome_reason="readback_refuted")


def test_verified_needs_an_independent_readback() -> None:
    with pytest.raises(ValidationError, match="independent"):
        _row(readback_tier="screen_readback")
    with pytest.raises(ValidationError, match="independent"):
        _row(readback_tier=None)


def test_unknown_time_is_null_never_zero() -> None:
    open_interval = RunLedgerIntervalV1(
        kind="approval", opened_at="2026-11-20T10:00:00Z"
    )
    with pytest.raises(ValidationError, match="null"):
        RunLedgerHumanV1(intervention_s=0.0, intervals=(open_interval,))
    human = RunLedgerHumanV1(intervention_s=None, intervals=(open_interval,))
    assert human.intervention_s is None
    closed = RunLedgerIntervalV1(
        kind="question",
        opened_at="2026-11-20T10:00:00Z",
        decided_at="2026-11-20T10:01:30Z",
    )
    assert (
        RunLedgerHumanV1(intervention_s=90.0, intervals=(closed,)).intervention_s
        == 90.0
    )
    with pytest.raises(ValidationError, match="sum"):
        RunLedgerHumanV1(intervention_s=10.0, intervals=(closed,))
    with pytest.raises(ValidationError, match="before"):
        RunLedgerIntervalV1(
            kind="question",
            opened_at="2026-11-20T10:01:00Z",
            decided_at="2026-11-20T10:00:00Z",
        )


def test_cost_source_matches_cost() -> None:
    assert RunLedgerModelUsageV1().cost_source == "unknown"
    with pytest.raises(ValidationError):
        RunLedgerModelUsageV1(cost_usd=0.1)
    with pytest.raises(ValidationError):
        RunLedgerModelUsageV1(cost_source="metered")
    usage = RunLedgerModelUsageV1(calls=3, cost_usd=0.12, cost_source="metered")
    assert usage.input_tokens is None  # unknown, not zero


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "Patient Jane Doe"),
        ("executor_label", "agent with spaces"),
        ("task_id", "has space"),
        ("sources", ["/etc/passwd"]),
        ("sources", ["../outside.json"]),
        ("fields_written", [{"name": "note value!", "check": "matched"}]),
        ("unknown_key", "x"),
    ],
)
def test_values_have_no_field_to_travel_in(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        _row(**{field: value})


def test_summary_is_the_row_minus_sources() -> None:
    row = _row()
    assert row.to_json()["schema"] == RUN_LEDGER_SCHEMA
    summary = row.summary().to_json()
    assert summary["schema"] == AGENT_RUN_SUMMARY_SCHEMA
    assert "sources" not in summary
    assert {
        k: v for k, v in row.to_json().items() if k not in ("schema", "sources")
    } == {k: v for k, v in summary.items() if k != "schema"}
    with pytest.raises(ValidationError):
        AgentRunSummaryV1.model_validate(dict(summary, sources=["a.json"]))


def test_fields_differing_counts_every_differing_field() -> None:
    with pytest.raises(ValidationError, match="fields_differing"):
        _row(
            outcome="CAUGHT_ERROR",
            outcome_reason="shadow_field_mismatch",
            fields_written=[{"name": "note", "check": "differs"}],
            fields_differing=0,
        )


# -- J6: the push payload Cloud ingests ------------------------------------------------

#: Real ``openadapt-flow ledger push --dry-run`` output over synthetic runs
#: (shadow, guarded, declined, not-saved, refused fallback, halted program).
#: The Cloud dashboard (openadapt-cloud#551) copies this file as its fixture.
J6_EXAMPLES = Path(__file__).parent / "fixtures" / "agent_run_summary_v1_examples.jsonl"


def test_the_j6_examples_validate_against_the_model_and_its_json_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = AgentRunSummaryV1.model_json_schema(by_alias=True)
    validator = jsonschema.Draft202012Validator(schema)
    lines = J6_EXAMPLES.read_text(encoding="utf-8").splitlines()
    assert lines
    nulls = 0
    for line in lines:
        payload = json.loads(line)
        validator.validate(payload)
        AgentRunSummaryV1.model_validate(payload)
        assert payload["schema"] == "openadapt.agent-run-summary/v1"
        assert "sources" not in payload
        nulls += payload["model"]["calls"] is None
    # Unknown values travel as null.
    assert nulls >= 1


def test_no_intervals_is_zero_or_unknown_never_another_number() -> None:
    from openadapt_types.run_ledger import RunLedgerHumanV1

    assert RunLedgerHumanV1().intervention_s is None
    assert RunLedgerHumanV1(intervention_s=0.0).intervention_s == 0.0
    with pytest.raises(ValidationError):
        RunLedgerHumanV1(intervention_s=5.0)


def test_verified_with_a_differing_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="differs"):
        _row(fields_differing=1)
