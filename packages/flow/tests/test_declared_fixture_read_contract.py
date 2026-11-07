"""Source-only #2030 controls; copy into Flow tests for an allocated run.

Actual loopback HTTP verifies fixture contracts against independently seeded
stores. No source admission, write qualification, app or native credit.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.mockmed.fault_server import MOCKMED_RECORDS_READ_CONTRACT
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.qualify.config import SystemOfRecordConfig
from openadapt_flow.qualify.session import verifier_factory
from openadapt_flow.record_sor import load_observer
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.verify_run import (
    VerifyConfig,
    _audit_verifier,
    _build_verifier,
    _read_versions,
)
from tests.e2e.rest_page_cap_server import CappedRecordServer

COUNTS = (0, 1, 2, 200, 201)
ROUTES = (
    "direct",
    "deployment",
    "qualification",
    "qualification_session",
    "verify_run",
    "verify_audit",
    "verify_versions",
    "record_observer",
    "surface",
    "candidate",
    "reward",
    "admission_read",
    "synthesis_read",
)
FIELDS = ("records_next_path", "records_total_path", "records_complete")


def _verify_config(section):
    return VerifyConfig.model_validate(
        {
            "verifier": section,
            "effects": [
                Effect.model_validate(
                    {"kind": EffectKind.RECORD_WRITTEN, "match": {"patient_id": "p1"}}
                )
            ],
            "frame": {
                "identity_field": "id",
                "attribution": {
                    "kind": "audit_read",
                    "actor_field": "source",
                    "self": ["replay"],
                    "record_field": "id",
                    "read": section,
                },
                "versions": {
                    "read": section,
                    "record_field": "id",
                    "version_field": "id",
                },
            },
        }
    )


def _read(route, section: dict[str, Any], monkeypatch, tmp_path):
    """Existing constructors/read projections only; never dispatch a write."""
    cfg = EffectsConfig.model_validate(section)
    assert cfg.base_url is not None and cfg.records_path is not None
    verifier: Any
    if route == "direct":
        verifier = RestRecordVerifier(
            cfg.base_url,
            records_path=cfg.records_path,
            records_key=cfg.records_key,
            next_path=cfg.records_next_path,
            total_path=cfg.records_total_path,
            records_complete=cfg.records_complete,
        )
    elif route in {"admission_read", "synthesis_read"}:
        # Intercept only the existing constructor boundary and execute its real
        # reader. Stop before an admission actuator can even be constructed.
        import openadapt_flow.deployment as deployment

        class ReadCaptured(Exception):
            pass

        def capture(actual_cfg, params=None, *, session=None):
            verifier = build_effect_verifier(actual_cfg, params, session=session)
            assert verifier is not None
            assert verifier.next_path == cfg.records_next_path
            assert verifier.total_path == cfg.records_total_path
            assert verifier.records_complete is cfg.records_complete
            try:
                state = verifier.capture_pre_state()
                result = (
                    state.reachable,
                    list(state.records) if state.reachable else None,
                )
            finally:
                client = getattr(verifier, "_session", None)
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            raise ReadCaptured(result)

        with monkeypatch.context() as patch:
            patch.setattr(deployment, "build_effect_verifier", capture)
            if route == "admission_read":
                from openadapt_flow.compiler import binding_admission as admission
                from openadapt_flow.ir import ApiBinding

                def no_actuator(*_args, **_kwargs):
                    raise AssertionError("admission test reached an actuator")

                patch.setattr(admission, "ApiActuator", no_actuator)
                fixture = admission.AdmissionFixture(
                    cfg.base_url,
                    cfg.records_path,
                    cfg.records_key,
                    records_next_path=cfg.records_next_path,
                    records_total_path=cfg.records_total_path,
                    records_complete=cfg.records_complete,
                )
                proposal = admission.RestWriteProposal(
                    "synthetic-step",
                    ApiBinding(method="POST", url_template="/never-called"),
                    [],
                    "constructor propagation only",
                )
                with pytest.raises(ReadCaptured) as captured:
                    admission.admit_rest_binding(proposal, fixture=fixture)
            else:
                from openadapt_flow.record_check_synthesis import (
                    ReadSearch,
                    reads_from_verifier,
                )

                with pytest.raises(ReadCaptured) as captured:
                    reads_from_verifier(section, {}, ReadSearch())
        return captured.value.args[0]
    elif route == "deployment":
        verifier = build_effect_verifier(cfg)
    elif route in {"qualification", "qualification_session"}:
        client = None
        if route == "qualification_session":
            # Hook lookup only is substituted; the client performs actual HTTP.
            client = httpx.Client(trust_env=False)
            monkeypatch.setattr(
                "openadapt_flow.qualify.session.load_hook",
                lambda _reference: lambda _config: client,
            )
        sor = SystemOfRecordConfig(
            verifier=cfg,
            session="synthetic_contract:session" if client is not None else None,
        )
        verifier = verifier_factory(SimpleNamespace(system_of_record=sor))({})
    elif route == "verify_run":
        verifier = _build_verifier(_verify_config(section), {})
    elif route == "verify_audit":
        verifier = _audit_verifier(_verify_config(section), {})
    elif route == "verify_versions":
        rows = _read_versions(_verify_config(section), {})
        return rows is not None, rows
    elif route == "record_observer":
        path = tmp_path / "fixture-verify.json"
        path.write_text(json.dumps({"verifier": section}), encoding="utf-8")
        observer, retained = load_observer(path)
        for key in FIELDS:
            assert retained.get(key) == section.get(key)
        verifier = observer.verifier
    elif route == "surface":
        routed = build_effect_verifier(
            EffectsConfig(kind="surfaces", surfaces={"rows": cfg})
        )
        assert routed is not None
        verifier = routed.verifiers["rows"]
    elif route == "candidate":
        candidates = build_effect_verifier(EffectsConfig(candidates=[cfg]))
        # Constructor propagation only; no invented admitted candidate execution.
        assert candidates is not None
        verifier = candidates._candidates[0]
    elif route == "reward":
        from openadapt_flow.reward.models import OracleRecipeV1
        from openadapt_flow.reward.oracles import VerifierOracle, _adapter_for

        recipe = OracleRecipeV1.model_validate(section)
        adapter = _adapter_for(recipe, Path(tmp_path))
        assert isinstance(adapter, VerifierOracle)
        verifier = adapter.verifier
    else:
        raise AssertionError(route)
    assert verifier is not None
    assert verifier.next_path == cfg.records_next_path
    assert verifier.total_path == cfg.records_total_path
    assert verifier.records_complete is cfg.records_complete
    try:
        state = verifier.capture_pre_state()
        return state.reachable, list(state.records) if state.reachable else None
    finally:
        client = getattr(verifier, "_session", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _whole_set(returned, committed):
    assert returned is not None
    assert len(returned) == len(committed)
    assert [row["id"] for row in returned] == [row["id"] for row in committed]
    assert returned == committed


@pytest.mark.parametrize("count", COUNTS)
def test_mockmed_declared_fullset_matches_actual_store(count, monkeypatch, tmp_path):
    base, db, stop = fault_serve()
    try:
        for index in range(count):
            db.add("p1", "Triage", f"synthetic note {index}")
        committed = db.snapshot()["records"]
        assert len(committed) == count
        with httpx.Client(trust_env=False) as client:
            response = client.get(base + "api/db", timeout=2.0)
        assert response.status_code == 200
        _whole_set(response.json()["records"], committed)
        assert MOCKMED_RECORDS_READ_CONTRACT == {
            "kind": "rest",
            "records_path": "/api/db",
            "records_key": "records",
            "records_complete": True,
        }
        # Public293 accepted this fixture implicitly. The new positive requires
        # an explicit contract, never endpoint spelling or a short array.
        for complete in (None, False, True):
            section = {
                **MOCKMED_RECORDS_READ_CONTRACT,
                "base_url": base.rstrip("/"),
                "records_complete": complete,
            }
            for route in ROUTES:
                reachable, returned = _read(route, section, monkeypatch, tmp_path)
                assert reachable is (complete is True), (count, complete, route)
                if complete is True:
                    _whole_set(returned, committed)
                else:
                    assert returned is None
        assert db.snapshot()["records"] == committed
    finally:
        stop()


@pytest.mark.parametrize("count", COUNTS)
@pytest.mark.parametrize("shape", ("next", "total", "both"))
def test_http_metadata_propagates_across_readers(count, shape, monkeypatch, tmp_path):
    # Only total-only needs a server whose one page really covers every row.
    cap = 512 if shape == "total" else 200
    with CappedRecordServer(page_size=cap) as server:
        for index in range(count):
            server.append({"id": str(index), "patient_id": "p1"})
        committed = server.snapshot()
        for complete in (None, False, True):
            section = {
                "kind": "rest",
                "base_url": server.url,
                "records_path": "/records?limit=1000",
                "records_key": "records",
                "records_complete": complete,
                "records_next_path": "page.next" if shape != "total" else None,
                "records_total_path": "page.total" if shape != "next" else None,
            }
            for route in ROUTES:
                reachable, returned = _read(route, section, monkeypatch, tmp_path)
                assert reachable is True, (count, shape, complete, route)
                _whole_set(returned, committed)
        assert server.snapshot() == committed


def test_independent_contract_oracle_rejects_silent_200_row_cap():
    # Mutation control: the independent whole-store oracle must fail. A reader
    # cannot discover a hidden cap from a falsely supplied True declaration.
    with CappedRecordServer(page_size=200, metadata=False) as server:
        for index in range(201):
            server.append({"id": str(index), "patient_id": "p1"})
        with httpx.Client(trust_env=False) as client:
            response = client.get(server.url + "/records?limit=1000", timeout=2.0)
        assert response.status_code == 200
        committed, returned = server.snapshot(), response.json()["records"]
        assert len(committed) == 201 and len(returned) == 200
        assert committed[-1]["id"] not in {row["id"] for row in returned}
        with pytest.raises(AssertionError):
            _whole_set(returned, committed)


@pytest.mark.parametrize("complete", (None, False, True))
def test_total_mismatch_cannot_be_overridden(complete, monkeypatch, tmp_path):
    with CappedRecordServer(page_size=200) as server:
        for index in range(201):
            server.append({"id": str(index), "patient_id": "p1"})
        section = {
            "kind": "rest",
            "base_url": server.url,
            "records_path": "/records?limit=1000",
            "records_key": "records",
            "records_total_path": "page.total",
            "records_complete": complete,
        }
        for route in ROUTES:
            reachable, returned = _read(route, section, monkeypatch, tmp_path)
            assert reachable is False, route
            assert returned is None


def test_owned_session_contracts_are_source_scoped():
    from openadapt_flow.fleet.simulator import (
        SIMULATED_RECORDS_READ_CONTRACT,
        SystemOfRecord,
        _RecordSession,
    )
    from openadapt_flow.qualification_oracle_gate import (
        UNCHANGED_RECORDS_READ_CONTRACT,
        _UnchangedStore,
    )

    assert UNCHANGED_RECORDS_READ_CONTRACT == {
        "kind": "rest",
        "records_path": "/records",
        "records_key": "records",
        "records_complete": True,
    }
    assert SIMULATED_RECORDS_READ_CONTRACT == UNCHANGED_RECORDS_READ_CONTRACT
    # Permanently empty by source definition; not an arbitrary-size HTTP fake.
    assert _UnchangedStore().get("http://synthetic.invalid/records").json() == {
        "records": []
    }
    empty_verifier = build_effect_verifier(
        EffectsConfig.model_validate(
            {**UNCHANGED_RECORDS_READ_CONTRACT, "base_url": "http://synthetic.invalid"}
        ),
        session=_UnchangedStore(),
    )
    assert empty_verifier is not None
    empty_state = empty_verifier.capture_pre_state()
    assert empty_state.reachable is True and empty_state.readback_complete is True
    assert empty_state.records == []
    for count in COUNTS:
        sor = SystemOfRecord()
        sor.records.extend({"id": str(index)} for index in range(count))
        _whole_set(
            _RecordSession(sor)
            .get("http://synthetic.invalid/records")
            .json()["records"],
            sor.records,
        )
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(
                {
                    **SIMULATED_RECORDS_READ_CONTRACT,
                    "base_url": "http://synthetic.invalid",
                }
            ),
            session=_RecordSession(sor),
        )
        assert verifier is not None
        state = verifier.capture_pre_state()
        assert state.reachable is True and state.readback_complete is True
        _whole_set(state.records, sor.records)


def test_carrier_cartesian_roundtrip_and_legacy_positional_order():
    from openadapt_flow.compiler.binding_admission import AdmissionFixture
    from openadapt_flow.reward.models import OracleRecipeV1

    session = object()
    fixture = AdmissionFixture(
        "http://synthetic.invalid",
        "/records",
        "records",
        "/write",
        "POST",
        frozenset({"id"}),
        "key",
        {"x": "y"},
        3.0,
        session,
    )
    assert fixture.write_path == "/write" and fixture.write_method == "POST"
    assert fixture.server_fields == frozenset({"id"})
    assert fixture.idempotency_key_field == "key" and fixture.params == {"x": "y"}
    assert fixture.timeout_s == 3.0 and fixture.session is session
    assert all(getattr(fixture, key) is None for key in FIELDS)
    for complete, following, total in itertools.product(
        (None, False, True), (None, "page.next"), (None, "page.total")
    ):
        values: dict[str, Any] = dict(zip(FIELDS, (following, total, complete)))
        section: dict[str, Any] = {
            "kind": "rest",
            "base_url": "http://synthetic.invalid",
            "records_path": "/records",
            **values,
        }
        cfg = EffectsConfig.model_validate_json(json.dumps(section))
        recipe = OracleRecipeV1.model_validate_json(json.dumps(section))
        fixture = AdmissionFixture(section["base_url"], "/records", "records", **values)
        dumped = recipe.model_dump(mode="json")
        for key, value in values.items():
            assert getattr(cfg, key) == value
            assert getattr(recipe, key) == value
            assert getattr(fixture, key) == value
            if value is None:
                assert key not in dumped
            else:
                assert dumped[key] == value and type(dumped[key]) is type(value)
        assert OracleRecipeV1.model_validate(dumped) == recipe


@pytest.mark.parametrize("bad", ("true", "false", 0, 1, 0.0, 1.0, [], {}))
def test_completeness_carriers_refuse_non_boolean_values(bad):
    from openadapt_flow.compiler.binding_admission import AdmissionFixture
    from openadapt_flow.reward.models import OracleRecipeV1

    with pytest.raises(ValueError):
        EffectsConfig(kind="rest", records_complete=bad)
    with pytest.raises(ValueError):
        OracleRecipeV1(
            kind="rest",
            base_url="http://synthetic.invalid",
            records_path="/records",
            records_complete=bad,
        )
    with pytest.raises(ValueError):
        AdmissionFixture(
            "http://synthetic.invalid", "/records", "records", records_complete=bad
        )


def test_non_rest_false_is_explicit_and_refused():
    from openadapt_flow.reward.models import OracleRecipeV1

    with pytest.raises(ValueError):
        EffectsConfig(kind="file", records_complete=False)
    with pytest.raises(ValueError):
        OracleRecipeV1(kind="json_file", path="synthetic.json", records_complete=False)


def test_legacy_recipe_json_dump_and_digest_remain_exact():
    from openadapt_flow.reward.models import OracleRecipeV1

    # Exact complete public293 model-dump shape, not a candidate-generated
    # baseline. Adding unset fields must not alter existing recipe bytes.
    legacy: dict[str, Any] = {
        "kind": "rest",
        "path": None,
        "records_key": "records",
        "base_url": "http://synthetic.invalid",
        "records_path": "/records",
        "headers_env": None,
        "query": None,
        "resource_type": None,
        "search_params": {},
        "field_paths": None,
        "token_env": None,
        "pattern": "*",
        "timeout_s": 5.0,
    }

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    raw = canonical(legacy)
    actual = OracleRecipeV1.model_validate_json(raw).model_dump(mode="json")
    assert actual == legacy
    assert canonical(actual) == raw
    assert hashlib.sha256(canonical(actual)).digest() == hashlib.sha256(raw).digest()
    assert (
        OracleRecipeV1.model_validate_json(raw).digest
        == "sha256:" + hashlib.sha256(raw).hexdigest()
    )


def test_reward_synthetic_header_reference_reaches_actual_loopback_http(
    monkeypatch, tmp_path
):
    # Diagnostic bytes only. This does not establish an authenticated principal
    # or qualify the reader/source; the fixture itself requires no credential.
    import os
    import uuid

    from openadapt_flow.reward.models import OracleRecipeV1
    from openadapt_flow.reward.oracles import VerifierOracle, _adapter_for

    name = "OPENADAPT_TEST_2030_HEADERS_" + uuid.uuid4().hex.upper()
    while name in os.environ:
        name = "OPENADAPT_TEST_2030_HEADERS_" + uuid.uuid4().hex.upper()
    headers = {"X-Synthetic-Read": "diagnostic-only"}
    monkeypatch.setenv(name, json.dumps(headers))
    base, db, stop = fault_serve()
    try:
        db.add("p1", "Triage", "synthetic diagnostic")
        recipe = OracleRecipeV1(
            kind="rest",
            base_url=base.rstrip("/"),
            records_path="/api/db",
            records_key="records",
            records_complete=True,
            headers_env=name,
        )
        assert recipe.model_dump(mode="json")["headers_env"] == name
        adapter = _adapter_for(recipe, Path(tmp_path))
        assert isinstance(adapter, VerifierOracle)
        verifier = adapter.verifier
        assert verifier.headers == headers
        seen: list[dict[str, str]] = []
        with httpx.Client(
            trust_env=False,
            event_hooks={
                "request": [lambda request: seen.append(dict(request.headers))]
            },
        ) as client:
            # Existing read-client seam, without altering parsing or headers.
            verifier._session = client
            state = verifier.capture_pre_state()
        assert state.reachable is True and state.readback_complete is True
        _whole_set(state.records, db.snapshot()["records"])
        assert len(seen) == 1
        assert seen[0]["x-synthetic-read"] == "diagnostic-only"
        assert "authorization" not in seen[0]
    finally:
        stop()
