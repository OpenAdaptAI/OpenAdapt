"""Learned field correspondence over real HTTP reads and SQLite snapshots."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from hypothesis import given, strategies as st

from openadapt_flow.complete_read import CompleteReadProvider
from openadapt_flow.deployment import build_effect_verifier
from openadapt_flow.runtime.effects.frame_identity import (
    canonical_field_value,
    learn_field_bindings,
    learn_identity,
)
from openadapt_flow.verify_run import (
    capture_baseline,
    learn_frame_correspondence,
    parse_verify_config,
    verify_run,
)
from tests.test_frame_identity_learning import IDS, document
from tests.test_frame_identity_learning import service  # noqa: F401
from tests.test_quiescent_read_setup import LockedAuthority


def configured(service, client, operation, route="views", kind="fhir"):
    doc = document(service, kind)
    doc["frame"]["side_records"] = []
    doc["frame"]["database"][route] = {"objects": ""}
    if route != "views":
        del doc["frame"]["database"]["views"]
    if kind == "fhir":
        doc["verifier"]["field_paths"] = {"id": "id", "reading": "valueString"}
    elif kind == "sql":
        doc["verifier"]["sql_query"] = (
            "SELECT external_key AS id,value AS reading FROM objects"
        )
    else:
        service.transform = lambda rows: [
            {"id": row["id"], "reading": row["value"]} for row in rows
        ]
    params = {"reading": "old0" if operation == "noop" else "created"}
    if operation == "create":
        doc["record"] = {
            "name": "object", "match": ["reading"],
            "fields": {"reading": "reading"},
        }
    else:
        del doc["record"]
        doc["effects"] = [{
            "kind": "field_equals", "match": {"id": IDS[0]},
            "field": "reading", "value": {"param": "reading"}, "timeout_s": 0,
        }]
    cfg = parse_verify_config(doc)
    reader = build_effect_verifier(
        cfg.verifier, params, session=None if kind == "sql" else client,
        quiescence=LockedAuthority(service) if kind == "fhir" else None,
    )
    provider = CompleteReadProvider(cfg.verifier, params, reader)
    learned = learn_frame_correspondence(cfg, params, read_provider=provider)
    return learned, params, provider


def mutate(service, operation):
    if operation == "create":
        service.write(
            "INSERT INTO objects(row_id,external_key,value,untouched) VALUES (30,?,?,?)",
            (IDS[2], "created", "server-filled"),
        )
        return 30
    if operation == "update":
        service.write(
            "UPDATE objects SET value='created',untouched='server-filled' WHERE row_id=10"
        )
    return 10


def checked(cfg, params, provider, baseline):
    return verify_run(
        cfg, params=params,
        run_record={"executor_kind": "agent", "executor_id": "synthetic"},
        authorization_ref="test", baseline_path=baseline, read_provider=provider,
    )


@pytest.mark.parametrize("operation", ["create", "update", "noop"])
@pytest.mark.parametrize("route", ["views", "judged_by"])
@pytest.mark.parametrize("kind", ["fhir", "rest", "sql"])
def test_correspondence_checks_target_and_reports_unverified_columns(
    service, tmp_path, operation, route, kind
):
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, operation, route, kind)
        binding = cfg.frame.identities[""]
        assert binding.field_bindings["reading"].column == "value"
        assert binding.field_bindings["reading"].witnesses == 2
        assert binding.field_columns == ["external_key", "row_id", "untouched", "value"]
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        assert "old0" not in baseline.read_text()
        mutate(service, operation)
        result = checked(cfg, params, provider, baseline)
    assert result["receipt"]["checked"]["delta"] == "held"
    assert result["receipt"]["checked"]["frame"] == "held"
    assert result["receipt"]["outcome"] == "verified"
    frame = result["evidence"]["frame"]
    assert frame["collateral_count"] == 0
    assert frame["coverage"]["unverified_columns"] == {"objects": ["row_id", "untouched"]}
    assert "server-filled" not in json.dumps(frame)


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("route", ["views", "judged_by"])
def test_wrong_bound_storage_refutes_even_when_reader_claims_correct(
    service, tmp_path, operation, route
):
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, operation, route)
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        row = mutate(service, operation)
        service.write("UPDATE objects SET value='wrong' WHERE row_id=?", (row,))
        target_id = IDS[2] if operation == "create" else IDS[0]
        service.transform = lambda rows: [
            dict(record, value="created") if record["id"] == target_id else record
            for record in rows
        ]
        result = checked(cfg, params, provider, baseline)
    assert result["receipt"]["checked"]["delta"] == "held"
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["collateral_fields"] == ["value"]


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("fault", ["extra-row", "other-row", "other-table"])
@pytest.mark.parametrize("route", ["views", "judged_by"])
def test_correspondence_never_exempts_another_row_or_table(
    service, tmp_path, operation, fault, route
):
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, operation, route)
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        mutate(service, operation)
        if fault == "extra-row":
            service.write(
                "INSERT INTO objects(row_id,external_key,value) VALUES (31,?,?)",
                (IDS[3], "unrelated"),
            )
        elif fault == "other-row":
            service.write("UPDATE objects SET untouched='unrelated' WHERE row_id=11")
        else:
            service.write("INSERT INTO children VALUES (1,11)")
        result = checked(cfg, params, provider, baseline)
    assert result["receipt"]["checked"]["delta"] == "held"
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["collateral_count"] > 0


@pytest.mark.parametrize("operation", ["create", "update"])
def test_ambiguous_field_remains_unbound_and_visible_in_target_coverage(
    service, tmp_path, operation
):
    service.write("ALTER TABLE objects ADD COLUMN twin TEXT")
    service.write("UPDATE objects SET twin=value")
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, operation)
        assert "reading" not in cfg.frame.identities[""].field_bindings
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        mutate(service, operation)
        result = checked(cfg, params, provider, baseline)
    assert result["receipt"]["checked"]["frame"] == "held"
    assert result["receipt"]["outcome"] == "verified"
    assert result["evidence"]["frame"]["coverage"]["unverified_columns"] == {
        "objects": ["row_id", "twin", "untouched", "value"]
    }


@pytest.mark.parametrize("drift", ["bindings", "inventory"])
def test_field_coverage_is_authenticated_against_the_baseline(service, tmp_path, drift):
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, "create")
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        binding = cfg.frame.identities[""]
        changes = {"field_bindings": {}} if drift == "bindings" else {"field_columns": ["row_id"]}
        changed = cfg.model_copy(deep=True)
        changed.frame.identities[""] = binding.model_copy(update=changes)
        mutate(service, "create")
        result = checked(changed, params, provider, baseline)
    assert result["receipt"]["checked"]["frame"] == "not_checked"
    assert result["receipt"]["outcome"] != "verified"


@given(st.lists(st.integers(min_value=1, max_value=10000), min_size=2, max_size=6, unique=True))
def test_field_correspondence_is_row_order_invariant_and_refuses_ambiguity(values):
    records = [{"id": str(i), "surface": value} for i, value in enumerate(values)]
    rows = [{"key": str(i), "stored": value, "other": -value} for i, value in enumerate(values)]
    identity = learn_identity(records, rows, surface_field="id", table="items", read_config={})
    learned = learn_field_bindings(identity, records, rows)
    assert learned["surface"].column == "stored"
    assert learned == learn_field_bindings(identity, records[::-1], rows[::-1])
    ambiguous = [dict(row, duplicate=row["stored"]) for row in rows]
    assert "surface" not in learn_field_bindings(identity, records, ambiguous)
    inconsistent = [dict(row, stored=-row["stored"]) if i == 0 else row for i, row in enumerate(rows)]
    assert "surface" not in learn_field_bindings(identity, records, inconsistent)


@pytest.mark.parametrize("left,right", [(False, 0), (True, 1), (1, "1"), (1, 1.0), (None, "null"), (b"", "")])
def test_field_values_remain_typed(left, right):
    assert canonical_field_value(left, "exact") != canonical_field_value(right, "exact")


def test_missing_or_null_witness_does_not_invent_a_field_binding():
    records = [{"id": "one", "surface": None}, {"id": "two", "surface": None}]
    rows = [{"key": "one", "stored": None}, {"key": "two", "stored": None}]
    identity = learn_identity(records, rows, surface_field="id", table="items", read_config={})
    assert "surface" not in learn_field_bindings(identity, records, rows)
    records[0]["surface"] = "value"
    del records[1]["surface"]
    assert "surface" not in learn_field_bindings(identity, records, rows)


def test_field_uuid_canonicalisation_uses_the_same_generic_identity_model():
    records = [{"id": "one", "surface": IDS[0]}, {"id": "two", "surface": IDS[1]}]
    rows = [{"key": record["id"], "stored": UUID(record["surface"]).bytes} for record in records]
    identity = learn_identity(records, rows, surface_field="id", table="items", read_config={})
    cell = learn_field_bindings(identity, records, rows)["surface"]
    assert cell.column == "stored" and cell.canonicaliser == "uuid"
    assert canonical_field_value(IDS[0], cell.canonicaliser) == canonical_field_value(UUID(IDS[0]).bytes, cell.canonicaliser)


@pytest.mark.parametrize("kind", ["fhir", "rest"])
@pytest.mark.parametrize("route", ["views", "judged_by"])
def test_unchanged_storage_cannot_confirm_a_misleading_update_read(
    service, tmp_path, kind, route
):
    with httpx.Client() as client:
        cfg, params, provider = configured(service, client, "update", route, kind)
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, params, baseline, read_provider=provider)
        # No database write at all. The reader alone claims the requested value.
        if kind == "fhir":
            service.transform = lambda rows: [
                dict(record, value="created") if record["id"] == IDS[0] else record
                for record in rows
            ]
        else:
            service.transform = lambda rows: [
                {"id": record["id"], "reading": "created" if record["id"] == IDS[0] else record["value"]}
                for record in rows
            ]
        result = checked(cfg, params, provider, baseline)
    assert service.db.execute("SELECT value FROM objects WHERE row_id=10").fetchone() == ("old0",)
    assert result["receipt"]["checked"]["delta"] == "held"
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["collateral_fields"] == ["value"]
