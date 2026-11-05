"""Native synthetic HTTP projections through the shared receipt producer.

The loopback stores are component fixtures, not application qualification.
No caller-created completion Mapping can replace an actual adapter capture.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.agent.verify_tools import VerifyBridge
from openadapt_flow.runtime.effects import rest
from openadapt_flow.runtime.effects.rest import PROJECTION_DETAIL, RestRecordVerifier
from openadapt_flow.verifier import Actor, Contract, Outcome, begin_write, verify_write
from tests import test_verify_run as old_rest
from tests.test_effect_paging import serve as page_server
from tests.test_native_receipt_scope import assert_undeclared_search


@pytest.fixture(autouse=True)
def isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = tmp_path / "issued"
    monkeypatch.setattr(verifier, "USED_DIR", authority)
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(authority))
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


@pytest.fixture
def source():
    from openadapt_flow.mockmed.fault_server import serve
    base, _store, stop = serve()
    try:
        yield base.rstrip("/")
    finally:
        stop()


def test_original_rest_configuration_confirms_only_its_actual_response_projection(source: str) -> None:
    contract = Contract.from_dict(old_rest._verify_yaml(source))
    pending = begin_write(contract, old_rest.PARAMS, actor=Actor("agent", "synthetic"))
    old_rest._actor_writes(source)
    result = verify_write(pending)
    assert result.outcome is Outcome.CONFIRMED and result.verified
    hard = result.evidence["frame"]["hard_invariants"]
    assert_undeclared_search(hard)
    domain = hard["guarantee"]
    assert domain["scope"] == "declared_projection"
    assert domain["identity_binding"] == "projected_tuple"
    assert "target_id_macs" not in domain and domain["projection_tuple_macs"]
    assert domain["schema_sha256"] is None
    assert any("native_schema_and_whole_database: UNKNOWN" in note for note in result.scope.not_covered)
    assert old_rest.PARAMS["note"] not in json.dumps(result.evidence)


def test_actual_rest_capture_binds_values_source_and_adapter_instance(source: str) -> None:
    old_rest._actor_writes(source)
    reader = RestRecordVerifier(source, records_path="/api/db", records_key="records")
    state = reader.capture_pre_state()
    facts = state.detail[PROJECTION_DETAIL]
    assert reader._projection_matches_capture(state.records, facts)
    assert facts["pages_read"] == 1 and facts["termination"] == "single_response"
    assert facts["records_read"] == len(state.records)
    other = RestRecordVerifier(source, records_path="/api/db", records_key="records")
    assert not other._projection_matches_capture(state.records, facts)
    changed = [dict(row) for row in state.records]
    changed[0]["note"] = "a different projected value"
    assert not reader._projection_matches_capture(changed, facts)
    assert not reader._projection_matches_capture(state.records, {**facts, "pages_read": 2})
    reader.records_path = "/api/db?tenant=another"
    assert not reader._projection_matches_capture(state.records, facts)
    assert old_rest.PARAMS["note"] not in json.dumps(facts)


def test_actual_httpx_url_object_attests_the_same_native_projection(source: str) -> None:
    import httpx

    old_rest._actor_writes(source)
    with httpx.Client() as session:
        reader = RestRecordVerifier(source, records_path="/api/db", records_key="records", session=session)
        state = reader.capture_pre_state()
        assert state.reachable and PROJECTION_DETAIL in state.detail
        assert reader._projection_matches_capture(state.records, state.detail[PROJECTION_DETAIL])


def test_missing_observed_response_source_has_no_completion_authority() -> None:
    """Structural unknown-provenance control, not a native HTTP trial."""
    class Response:
        status_code = 200
        headers: dict[str, str] = {}
        def json(self) -> dict[str, Any]:
            return {"records": [{"name": "a tuple"}]}
    class Session:
        def get(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()
    reader = RestRecordVerifier("http://127.0.0.1:1", records_path="/records", records_key="records", session=Session())
    state = reader.capture_pre_state()
    assert state.reachable
    assert PROJECTION_DETAIL not in state.detail
    assert not reader._projection_matches_capture(state.records, {})


def test_routed_actual_rest_capture_reaches_the_canonical_mcp_path(source: str, tmp_path: Path) -> None:
    doc = old_rest._verify_yaml(source)
    native = doc["verifier"]
    doc["verifier"] = {"kind": "surfaces", "surfaces": {"records": native}}
    doc["effects"][0]["probe"] = "surface=records|the declared write exists"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    # VerifyBridge loads governed YAML files; JSON is valid YAML syntax.
    (contracts / "synthetic.yaml").write_text(json.dumps(doc))
    bridge = VerifyBridge(contracts, tmp_path / "runs")
    begun = bridge.dispatch("verify_begin", {"contract": "synthetic", "values": old_rest.PARAMS})
    assert begun["ok_to_write"]
    old_rest._actor_writes(source)
    checked = bridge.dispatch("verify_check", {"write_id": begun["write_id"]})
    assert checked["outcome"] == "CONFIRMED" and checked["verified"]


def _contract(base: str, *, paged: bool) -> Contract:
    read: dict[str, Any] = {"kind": "rest", "base_url": base, "records_path": "/records", "records_key": "records"}
    if paged:
        read.update({"records_next_path": "next", "records_total_path": "total"})
    return Contract.from_dict({
        "schema_version": "openadapt.verify/v1", "verifier": read,
        "effects": [{"kind": "record_written", "match": {"name": {"param": "name"}}, "expected_count": 1, "count_new_only": True, "timeout_s": 0.1}],
    })


@pytest.mark.parametrize("fault", ["none", "later_page", "total", "missing_next", "mixed", "partial", "range", "redirect_path", "redirect_query", "cap"])
def test_actual_http_projection_completion_and_refusals(page_server: Any, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    # The owned fixture changes its native in-memory records to model a write;
    # all observations and the public verification path use actual HTTP.
    rows = [{"id": "prior1", "name": "old1"}, {"id": "prior2", "name": "old2"}]
    phase = {"written": False}
    paged = fault in {"none", "later_page", "total", "missing_next", "cap"}
    def page(path: str, number: int) -> tuple:
        active = phase["written"]
        if active and fault == "later_page" and number == 1:
            return 503, {"error": "synthetic unavailable page"}
        if active and fault == "redirect_path" and path == "/records":
            return 302, {}, {"Location": "/different"}
        if active and fault == "redirect_query" and path == "/records" and number == 0:
            return 302, {}, {"Location": "/records?page=1&tenant=another"}
        selected = rows[number * 2:(number + 1) * 2] if paged else rows
        body: dict[str, Any] = {"records": list(selected)}
        if paged:
            more = (number + 1) * 2 < len(rows)
            body["next"] = f"/records?page={number + 1}" if more or active and fault == "cap" else None
            body["total"] = len(rows) + (1 if active and fault == "total" else 0)
        if active and fault == "missing_next":
            body.pop("next")
        if active and fault == "mixed":
            body["records"].append("not a native record")
        if active and fault == "partial":
            return 206, body
        if active and fault == "range":
            return 200, body, {"Content-Range": "items 0-1/99"}
        return 200, body
    base = page_server(page)
    if fault == "cap":
        monkeypatch.setattr(rest, "MAX_PAGES", 2)
    params = {"name": "new-native-projection"}
    pending = begin_write(_contract(base, paged=paged), params, actor=Actor("agent", "synthetic"))
    rows.append({"id": "target", "name": params["name"]})
    phase["written"] = True
    result = verify_write(pending)
    if fault == "none":
        assert result.outcome is Outcome.CONFIRMED and result.verified
        assert_undeclared_search(result.evidence["frame"]["hard_invariants"])
    else:
        assert result.outcome is Outcome.UNCERTAIN and not result.verified
        assert result.evidence["frame"]["hard_invariants"]["status"] == "not_checked"


def test_rest_wrong_record_stays_refuted_despite_undeclared_search(source: str) -> None:
    pending = begin_write(Contract.from_dict(old_rest._verify_yaml(source)), old_rest.PARAMS, actor=Actor("agent", "synthetic"))
    old_rest._actor_writes(source, patient="p2")
    result = verify_write(pending)
    assert result.outcome is Outcome.REFUTED and not result.verified
