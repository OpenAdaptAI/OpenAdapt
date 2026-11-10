"""Owned fixture read declarations cover their actual, unpaged HTTP scope."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from benchmark.harness.guard_demo import verify_yaml
from benchmark.lending_fault_model.read_contracts import (
    MOCKLOAN_ALL_READ_CONTRACT,
    MOCKLOAN_DISBURSEMENTS_READ_CONTRACT,
)
from openadapt_flow.agent.example_app import (
    DEMO_PASSWORD,
    DEMO_USER,
    ORDERS_READ_CONTRACT,
)
from openadapt_flow.agent.example_app import serve as orders_serve
from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.mockloan.fault_server import serve as loan_serve
from openadapt_flow.mockmed.fault_server import serve as mockmed_serve
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from tests._rest_read_contract_fixtures import mockmed_effects_yaml
from tests.e2e.rest_page_cap_server import CappedRecordServer

COUNTS = (0, 1, 2, 200, 201)


@pytest.mark.parametrize("count", COUNTS)
def test_orders_declaration_covers_every_saved_order(count: int) -> None:
    base, stop = orders_serve()
    expected = [
        {
            "order_id": f"WO-{1001 + i}",
            "customer": f"synthetic-{i}",
            "product": "Desk lamp",
            "units": "1",
        }
        for i in range(count)
    ]
    try:
        with httpx.Client(base_url=base, follow_redirects=False) as client:
            assert (
                client.post(
                    "/login", data={"username": DEMO_USER, "password": DEMO_PASSWORD}
                ).status_code
                == 303
            )
            for row in expected:
                assert (
                    client.post(
                        "/orders",
                        data={k: row[k] for k in ("customer", "product", "units")},
                    ).status_code
                    == 303
                )
            assert client.get("/api/orders").json()["records"] == expected
        verifier = build_effect_verifier(
            EffectsConfig.model_validate({**ORDERS_READ_CONTRACT, "base_url": base})
        )
        assert isinstance(verifier, RestRecordVerifier)
        state = verifier.capture_pre_state()
        assert state.reachable is True
        assert state.readback_complete is True
        assert state.records == expected
    finally:
        stop()


@pytest.mark.parametrize("count", COUNTS)
def test_mockloan_declarations_match_complete_native_scopes(count: int) -> None:
    base, db, stop = loan_serve()
    try:
        for i in range(count):
            db.add(
                "L1001", "Personal", "18500", f"synthetic-{i}", surface="disbursements"
            )
            db.add("L1001", "Personal", "18500", f"fee-{i}", surface="fees")
        # Independent SQL row IDs establish the exact population, including
        # rows beyond common page limits and the deliberately excluded surface.
        with sqlite3.connect(db.database_path) as conn:
            all_ids = [
                row[0] for row in conn.execute("SELECT id FROM records ORDER BY id")
            ]
            selected_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM records WHERE surface = 'disbursements' ORDER BY id"
                )
            ]
        assert len(all_ids) == count * 2
        assert len(selected_ids) == count
        for declaration, ids in (
            (MOCKLOAN_ALL_READ_CONTRACT, all_ids),
            (MOCKLOAN_DISBURSEMENTS_READ_CONTRACT, selected_ids),
        ):
            verifier = RestRecordVerifier(base, **declaration)
            state = verifier.capture_pre_state()
            assert state.reachable is True
            assert state.readback_complete is True
            assert [row["id"] for row in state.records] == ids
    finally:
        stop()


@pytest.mark.parametrize("count", COUNTS)
def test_shared_mockmed_demo_and_yaml_preserve_actual_store_contract(
    count: int,
) -> None:
    import yaml

    base, db, stop = mockmed_serve()
    try:
        for i in range(count):
            db.add("p1", "Triage", f"synthetic-{i}")
        expected = db.snapshot()["records"]
        assert len(expected) == count
        declarations = (
            verify_yaml(base)["verifier"],
            yaml.safe_load(mockmed_effects_yaml(base)),
        )
        for declaration in declarations:
            verifier = build_effect_verifier(EffectsConfig.model_validate(declaration))
            assert isinstance(verifier, RestRecordVerifier)
            state = verifier.capture_pre_state()
            assert state.reachable is True
            assert state.readback_complete is True
            assert state.records == expected
    finally:
        stop()


def test_an_unknown_or_capped_endpoint_does_not_inherit_a_fixture_declaration() -> None:
    rows = [{"id": str(i)} for i in range(201)]
    with CappedRecordServer(rows=rows, page_size=200) as server:
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(
                {
                    "kind": "rest",
                    "base_url": server.url,
                    "records_path": "/records",
                    "records_key": "records",
                }
            )
        )
        assert isinstance(verifier, RestRecordVerifier)
        state = verifier.capture_pre_state()
        assert state.reachable is False
        assert state.readback_complete is None
        assert state.records == []
        assert len(server.snapshot()) == 201


@pytest.mark.parametrize("count", COUNTS)
def test_record_service_complete_scopes_match_actual_sql_tables(
    count: int, tmp_path
) -> None:
    from benchmark.effect_e2e.record_service import (
        BANNER_READ_CONTRACT,
        ENCOUNTERS_READ_CONTRACT,
        serve,
    )

    service = serve(tmp_path / "records.sqlite")
    try:
        # Seed both native tables independently. Normal writes intentionally
        # replace the last-save banner, so issuing N writes does not seed N
        # banner rows. These controls test complete HTTP scope, not write history.
        with sqlite3.connect(service.db_path) as conn:
            rows = [("p1", "Triage", f"synthetic-{i}") for i in range(count)]
            conn.executemany(
                "INSERT INTO encounters (patient_id, type, note, source, key) VALUES (?, ?, ?, 'fixture', NULL)",
                rows,
            )
            conn.executemany(
                "INSERT INTO banner (patient_id, type, note) VALUES (?, ?, ?)", rows
            )
            conn.commit()
        with sqlite3.connect(service.db_path) as conn:
            conn.row_factory = sqlite3.Row
            expected_encounters = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, patient_id, type, note, source, key FROM encounters ORDER BY id"
                )
            ]
            expected_banner = [
                dict(row)
                for row in conn.execute(
                    "SELECT patient_id, type, note FROM banner ORDER BY id"
                )
            ]
        assert len(expected_encounters) == len(expected_banner) == count
        for declaration, expected in (
            (ENCOUNTERS_READ_CONTRACT, expected_encounters),
            (BANNER_READ_CONTRACT, expected_banner),
        ):
            state = RestRecordVerifier(
                service.base_url, **declaration
            ).capture_pre_state()
            assert state.reachable is True
            assert state.readback_complete is True
            assert state.records == expected
    finally:
        service.stop()


@pytest.mark.parametrize("count", COUNTS)
def test_agent_guard_setup_declares_actual_complete_encounters(
    count: int, tmp_path
) -> None:
    import yaml

    from benchmark.agent_guard.app import serve
    from benchmark.agent_guard.run import write_setup
    from benchmark.effect_e2e.record_service import ENCOUNTERS_READ_CONTRACT

    app = serve(tmp_path / "agent-guard.sqlite")
    try:
        with sqlite3.connect(app.db_path) as conn:
            conn.executemany(
                "INSERT INTO encounters (patient_id, type, note, source, key) VALUES (?, ?, ?, 'fixture', NULL)",
                [("p1", "Triage", f"synthetic-{i}") for i in range(count)],
            )
            conn.commit()
        with sqlite3.connect(app.db_path) as conn:
            conn.row_factory = sqlite3.Row
            expected = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, patient_id, type, note, source, key FROM encounters ORDER BY id"
                )
            ]
        files = write_setup(tmp_path / "setup", app.base_url, teach_dir=None)
        config = yaml.safe_load(files["verify"].read_text())["verifier"]
        assert config == {
            "kind": "rest",
            "base_url": app.base_url,
            **ENCOUNTERS_READ_CONTRACT,
        }
        assert len(expected) == count
        state = RestRecordVerifier(
            app.base_url, **ENCOUNTERS_READ_CONTRACT
        ).capture_pre_state()
        assert state.reachable is True
        assert state.readback_complete is True
        assert state.records == expected
    finally:
        app.stop()
