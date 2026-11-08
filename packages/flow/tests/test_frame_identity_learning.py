"""Learn identity from actual HTTP reads and the frame's own SQLite snapshot."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from openadapt_flow.complete_read import CompleteReadProvider
from openadapt_flow.deployment import build_effect_verifier
from openadapt_flow.runtime.effects.frame import FrameSpec
from openadapt_flow.runtime.effects.frame_identity import (
    FrameIdentityError, canonical_identity, learn_identity,
)
from openadapt_flow.verify_run import (
    VerifyRunError, capture_baseline, learn_frame_identities, parse_verify_config, verify_run,
)
from tests.test_quiescent_read_setup import LockedAuthority

IDS = ["12345678-1234-4234-8234-00000000000" + str(i) for i in range(1, 5)]
PARAMS = {"value": "created"}


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "store.sqlite"
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript("""
        CREATE TABLE objects(row_id INTEGER PRIMARY KEY, external_key, value TEXT,
                             untouched TEXT DEFAULT '');
        CREATE TABLE children(child_id INTEGER PRIMARY KEY, parent_row INTEGER);
    """)
    state = SimpleNamespace(
        path=path, db=db, lock=threading.RLock(), active=False, owner=None,
        pending=False, lost=False, checks=0, calls=0, protocol="fhir",
        transform=lambda rows: rows,
    )

    def write(sql, args=()):
        with state.lock:
            db.execute(sql, args)
            db.commit()

    def seed(binary=False, count=2):
        with state.lock:
            db.execute("DELETE FROM objects")
            db.execute("DELETE FROM children")
            db.executemany("INSERT INTO objects(row_id,external_key,value) VALUES (?,?,?)", [
                (i+10, UUID(IDS[i]).bytes if binary else IDS[i], "old"+str(i))
                for i in range(count)
            ])
            db.commit()

    def snapshot():
        result = []
        for identity, value in db.execute("SELECT external_key,value FROM objects ORDER BY row_id"):
            if isinstance(identity, bytes):
                identity = str(UUID(bytes=identity))
            result.append({"id": identity, "value": value})
        return state.transform(result)

    state.write, state.seed = write, seed
    seed()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            rows = snapshot()
            body = rows if state.protocol == "rest" else {
                "resourceType": "Bundle", "type": "searchset", "total": len(rows),
                "entry": [{"resource": {"resourceType": "Observation", "id": row["id"], "valueString": row["value"]}} for row in rows],
            }
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        db.close()


def document(service, kind="fhir"):
    if kind == "fhir":
        read = {"kind": "fhir", "base_url": service.url, "resource_type": "Observation",
                "field_paths": {"id": "id", "value": "valueString"},
                "records_consistency": "quiescent-two-pass", "timeout_s": 2, "poll_interval_s": 0}
    elif kind == "rest":
        service.protocol = "rest"
        read = {"kind": "rest", "records_url": service.url + "/objects", "records_complete": True}
    else:
        read = {"kind": "sql", "sqlite_database": str(service.path),
                "sql_query": "SELECT external_key AS id,value FROM objects"}
    return {
        "schema_version": "openadapt.verify/v1", "verifier": read,
        "record": {"name": "object", "match": ["value"], "fields": {"value": "value"}},
        "frame": {"identity_field": "row_id", "database": {
            "dialect": "sqlite", "read": {"sqlite_database": str(service.path)},
            "views": {"objects": ""}, "settle_s": 0,
            "new_row_fields": {"objects": ["external_key"]},
        }, "side_records": [{"surface": "children", "link_field": "parent_row",
                              "of_surface": "objects", "of_field": "row_id"}]},
    }


def setup(service, client, *, kind="fhir", doc=None):
    cfg = parse_verify_config(doc or document(service, kind))
    reader = build_effect_verifier(cfg.verifier, PARAMS, session=None if kind == "sql" else client,
                                  quiescence=LockedAuthority(service) if kind == "fhir" else None)
    provider = CompleteReadProvider(cfg.verifier, PARAMS, reader)
    learned = learn_frame_identities(cfg, PARAMS, read_provider=provider)
    return cfg, learned, provider


@pytest.mark.parametrize("binary", [False, True])
def test_learn_then_verify_new_record_and_side_row(service, tmp_path, binary):
    service.seed(binary=binary)
    with httpx.Client() as client:
        original, cfg, provider = setup(service, client)
        binding = cfg.frame.identities[""]
        assert not original.frame.identities
        assert binding.column == "external_key" and binding.surface_field == "id"
        assert binding.canonicaliser == "uuid" and binding.witnesses == 2
        assert binding.diagnostics == []
        assert cfg.frame.identity_field == "row_id"  # audit/side row key is unchanged
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        assert IDS[0] not in baseline.read_text() and "old0" not in baseline.read_text()
        service.write("INSERT INTO objects(row_id,external_key,value) VALUES (30,?,?)",
                      (UUID(IDS[2]).bytes if binary else IDS[2], "created"))
        service.write("INSERT INTO children VALUES (1,30)")
        result = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["checked"]["frame"] == "held"
        assert result["receipt"]["outcome"] == "verified"
        assert service.calls >= 6


@pytest.mark.parametrize("mutation", ["stray-column", "extra-record", "wrong-child", "duplicate-key", "null-key", "reader-mismatch"])
def test_current_snapshot_and_existing_frame_guards_refuse(service, tmp_path, mutation):
    with httpx.Client() as client:
        _, cfg, provider = setup(service, client)
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        service.write("INSERT INTO objects(row_id,external_key,value) VALUES (30,?,?)", (IDS[2], "created"))
        if mutation == "stray-column":
            service.write("UPDATE objects SET untouched='unexpected' WHERE row_id=30")
        elif mutation == "extra-record":
            service.write("INSERT INTO objects(row_id,external_key,value) VALUES (31,?,?)", (IDS[3], "stray"))
        elif mutation == "wrong-child":
            service.write("INSERT INTO children VALUES (1,10)")
        elif mutation == "duplicate-key":
            service.write("UPDATE objects SET external_key=? WHERE row_id=30", (IDS[0],))
        elif mutation == "null-key":
            service.write("UPDATE objects SET external_key=NULL WHERE row_id=30")
        else:
            service.transform = lambda rows: [dict(row, id=IDS[3]) if row["value"] == "created" else row for row in rows]
        result = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["outcome"] != "verified"
        expected = "not_checked" if mutation in {"duplicate-key", "null-key", "reader-mismatch"} else "refuted"
        assert result["receipt"]["checked"]["frame"] == expected


def test_inherited_unavailable_identity_refuses_before_baseline(service, tmp_path):
    cfg = parse_verify_config(document(service))
    with httpx.Client() as client:
        reader = build_effect_verifier(cfg.verifier, PARAMS, session=client, quiescence=LockedAuthority(service))
        provider = CompleteReadProvider(cfg.verifier, PARAMS, reader)
        with pytest.raises(VerifyRunError, match="identity is unavailable"):
            capture_baseline(cfg, PARAMS, tmp_path / "absent.json", read_provider=provider)
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize("change", ["url", "field", "table", "column"])
def test_binding_drift_refuses_before_baseline(service, tmp_path, change):
    with httpx.Client() as client:
        _, cfg, provider = setup(service, client)
        changed = cfg.model_copy(deep=True)
        if change == "url":
            changed.verifier.base_url += "/other"
        elif change == "field":
            changed.verifier.field_paths["value"] = "status"
        elif change == "table":
            changed.frame.identities[""] = changed.frame.identities[""].model_copy(update={"table": "children"})
        else:
            changed.frame.identities[""] = changed.frame.identities[""].model_copy(update={"column": "missing"})
        with pytest.raises(VerifyRunError):
            capture_baseline(changed, PARAMS, tmp_path / "absent.json", read_provider=provider)
    assert not (tmp_path / "absent.json").exists()


def test_single_witness_is_explicit(service):
    service.seed(count=1)
    with httpx.Client() as client:
        _, cfg, _ = setup(service, client)
    assert cfg.frame.identities[""].witnesses == 1
    assert cfg.frame.identities[""].diagnostics == ["single_witness"]


@pytest.mark.parametrize("kind", ["rest", "sql"])
def test_existing_shared_identity_needs_no_new_binding(service, tmp_path, kind):
    doc = document(service, kind)
    doc["frame"]["identity_field"] = "id"
    # Existing shared-field behavior, with no alternate mapping requested.
    cfg = parse_verify_config(doc)
    assert "identities" not in cfg.frame.model_dump(mode="json")
    capture_baseline(cfg, PARAMS, tmp_path / "baseline.json")
    assert (tmp_path / "baseline.json").exists()


def test_frame_legacy_serialization_keeps_optional_binding_absent():
    frame = FrameSpec(identity_field="id")
    assert "identities" not in frame.model_dump(mode="json")
    assert FrameSpec.model_validate(frame.model_dump(mode="json")) == frame


@pytest.mark.parametrize("value", [IDS[0], IDS[0].replace("-", ""), IDS[0].upper(), UUID(IDS[0]).bytes])
def test_uuid_forms(value):
    assert canonical_identity(value, "uuid") == UUID(IDS[0]).hex


@pytest.mark.parametrize("value", [None, True, False, 1.0, b"short", "", "not-a-uuid", IDS[0]+"\n"])
def test_uuid_has_no_application_specific_coercion(value):
    with pytest.raises(FrameIdentityError):
        canonical_identity(value, "uuid")


@pytest.mark.parametrize("case", ["empty-read", "empty-store", "missing", "ambiguous", "null", "duplicate-store", "duplicate-read", "different-column", "nonbijective", "malformed-rows", "bool"])
def test_correspondence_refusal_siblings(case):
    records = [{"id": IDS[0]}, {"id": IDS[1]}]
    rows = [{"key": IDS[0], "number": 10}, {"key": IDS[1], "number": 11}]
    if case == "empty-read": records = []
    elif case == "empty-store": rows = []
    elif case == "missing": records[0]["id"] = IDS[3]
    elif case == "ambiguous":
        for row in rows: row["alias"] = row["key"]
    elif case == "null": rows.append({"key": None, "number": 12})
    elif case == "duplicate-store": rows.append({"key": IDS[0], "number": 12})
    elif case == "duplicate-read": records[1]["id"] = IDS[0]
    elif case == "different-column": rows[1] = {"key": "other", "number": IDS[1]}
    elif case == "nonbijective": records[1]["id"] = IDS[0].replace("-", "")
    elif case == "malformed-rows": rows[1]["extra"] = "x"
    else: records[0]["id"] = True
    with pytest.raises(FrameIdentityError):
        learn_identity(records, rows, surface_field="id", table="objects", read_config={"kind": "synthetic"})


@pytest.mark.parametrize("values", [["one", "two"], [1, 2]])
def test_exact_identity_domains_are_not_coerced(values):
    binding = learn_identity([{"id": v} for v in values], [{"key": v} for v in values],
                             surface_field="id", table="objects", read_config={"kind": "synthetic"})
    assert binding.canonicaliser == "exact"
    assert binding.column == "key" and binding.witnesses == 2
    assert canonical_identity("1", "exact") != canonical_identity(1, "exact")


@pytest.mark.parametrize("mutation", ["null", "duplicate", "ambiguous", "missing", "empty", "changed-snapshot"])
def test_setup_refusals_use_actual_http_and_frame_database(service, mutation):
    if mutation == "null":
        service.write("INSERT INTO objects(row_id,external_key,value) VALUES(12,NULL,'hidden')")
        service.transform = lambda rows: [r for r in rows if r["id"] is not None]
    elif mutation == "duplicate":
        service.write("INSERT INTO objects(row_id,external_key,value) VALUES(12,?,'hidden')", (IDS[0],))
        service.transform = lambda rows: rows[:2]
    elif mutation == "ambiguous":
        service.write("ALTER TABLE objects ADD COLUMN alias TEXT")
        service.write("UPDATE objects SET alias=external_key")
    elif mutation == "missing":
        service.transform = lambda rows: [dict(r, id=IDS[3]) if i == 0 else r for i, r in enumerate(rows)]
    elif mutation == "empty":
        service.seed(count=0)
    else:
        def change(rows):
            # Adversarial writer outside the declared exclusion protocol:
            # the independent before/after frame snapshots must catch it.
            service.db.execute("UPDATE objects SET untouched='moved' WHERE row_id=10")
            service.db.commit()
            return rows
        service.transform = change
    with httpx.Client() as client, pytest.raises(VerifyRunError, match="identity|snapshot"):
        setup(service, client)
    assert service.calls > 0


@pytest.mark.parametrize("kind", ["rest", "sql"])
def test_same_generic_learning_works_for_rest_and_sql(service, kind):
    doc = document(service, kind)
    doc["frame"]["identity_field"] = "id"
    with httpx.Client() as client:
        _, cfg, _ = setup(service, client, kind=kind, doc=doc)
    assert cfg.frame.identities[""].column == "external_key"
    assert cfg.frame.identities[""].surface_field == "id"


def test_unknown_read_completeness_cannot_learn_identity(service):
    cfg = parse_verify_config(document(service))
    cfg.verifier.records_consistency = None
    with httpx.Client() as client:
        reader = build_effect_verifier(cfg.verifier, PARAMS, session=client)
        provider = CompleteReadProvider(cfg.verifier, PARAMS, reader)
        with pytest.raises(VerifyRunError, match="complete reachable"):
            learn_frame_identities(cfg, PARAMS, read_provider=provider)


def test_snapshot_retention_is_opt_in_and_never_serialized(service):
    from openadapt_flow.runtime.effects.db_snapshot import take_snapshot

    def connect():
        return sqlite3.connect(service.path)

    ordinary = take_snapshot(connect, "sqlite", key=b"synthetic-key")
    retained = take_snapshot(connect, "sqlite", key=b"synthetic-key", retain_values=("objects",))
    assert ordinary.tables["objects"].values == {}
    assert len(retained.tables["objects"].values) == 2
    assert ordinary.tables["objects"].rows == retained.tables["objects"].rows
    assert IDS[0] not in json.dumps(retained.to_json())
    assert "old0" not in json.dumps(retained.to_json())


def deletion_document(service):
    doc = document(service)
    del doc["record"]
    doc["effects"] = [{"kind": "record_written", "match": {"value": "old0"},
                       "expected_count": 0, "timeout_s": 0}]
    return doc


@pytest.mark.parametrize("also_delete_other", [False, True])
def test_authenticated_old_selection_handles_deleted_records(service, tmp_path, also_delete_other):
    with httpx.Client() as client:
        _, cfg, provider = setup(service, client, doc=deletion_document(service))
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        assert IDS[0] not in baseline.read_text() and "old0" not in baseline.read_text()
        service.write("DELETE FROM objects WHERE row_id=10")
        if also_delete_other:
            service.write("DELETE FROM objects WHERE row_id=11")
        result = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["checked"]["frame"] == ("refuted" if also_delete_other else "held")
        if not also_delete_other:
            assert result["receipt"]["outcome"] == "verified"
        else:
            assert result["receipt"]["outcome"] != "verified"


def test_declared_identity_change_uses_real_current_row_and_keeps_column_guard(service, tmp_path):
    doc = document(service)
    del doc["record"]
    doc["effects"] = [{"kind": "field_equals", "match": {"value": "old0"}, "field": "id", "value": IDS[2], "timeout_s": 0}]
    doc["frame"]["target_fields"] = ["external_key"]
    with httpx.Client() as client:
        _, cfg, provider = setup(service, client, doc=doc)
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        service.write("UPDATE objects SET external_key=? WHERE row_id=10", (IDS[2],))
        result = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["checked"]["frame"] == "held"
        assert result["receipt"]["outcome"] == "verified"


@pytest.mark.parametrize("drift", ["selected-row", "index", "parameters", "effects"])
def test_old_selection_is_authenticated_and_bound_to_this_contract(service, tmp_path, drift):
    from openadapt_flow.verify_run import FRAME_IDENTITY_DETAIL

    with httpx.Client() as client:
        _, cfg, provider = setup(service, client, doc=deletion_document(service))
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        payload = json.loads(baseline.read_text())
        data = payload["state"]["detail"][FRAME_IDENTITY_DETAIL][""]["data"]
        if drift == "selected-row":
            data["declared"] = list(data["index"].values())
        elif drift == "index":
            data["index"] = {}
        baseline.write_text(json.dumps(payload))
        params = {"value": "different"} if drift == "parameters" else PARAMS
        if drift == "effects":
            cfg.effects[0] = cfg.effects[0].model_copy(update={"expected_count": 1})
        if drift == "parameters":
            provider = CompleteReadProvider(cfg.verifier, params, provider.resolve(cfg.verifier, PARAMS))
        service.write("DELETE FROM objects WHERE row_id=10")
        result = verify_run(cfg, params=params, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["checked"]["frame"] == "not_checked"
        assert result["receipt"]["outcome"] != "verified"
        assert "baseline identity evidence" in result["evidence"]["frame"]["reason"]


@pytest.mark.parametrize("kind", ["rest", "sql"])
@pytest.mark.parametrize("empty", [False, True])
def test_legacy_selected_read_requires_identity_only_when_nonempty(service, tmp_path, kind, empty):
    if empty:
        service.write("DELETE FROM objects")
    cfg = parse_verify_config(document(service, kind))
    baseline = tmp_path / "baseline.json"
    if empty:
        capture_baseline(cfg, PARAMS, baseline)
        assert baseline.exists()
    else:
        with pytest.raises(VerifyRunError, match="identity is unavailable"):
            capture_baseline(cfg, PARAMS, baseline)
        assert not baseline.exists()


@pytest.mark.parametrize("drift", ["permuted-column", "canonicaliser", "surface-field", "removed-mapping", "removed-frame", "removed-database"])
def test_baseline_authenticates_exact_learned_mapping(service, tmp_path, drift):
    service.protocol = "rest"
    service.transform = lambda rows: [dict(row, alias=row["id"]) for row in rows]
    with httpx.Client() as client:
        _, cfg, provider = setup(service, client, kind="rest")
        # This is an existing, unique alternative column at baseline time.
        # Its values map the same reader identities to the opposite rows.
        service.write("ALTER TABLE objects ADD COLUMN alternate_key TEXT")
        service.write("UPDATE objects SET alternate_key=? WHERE row_id=10", (IDS[1],))
        service.write("UPDATE objects SET alternate_key=? WHERE row_id=11", (IDS[0],))
        baseline = tmp_path / "baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        before = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert before["receipt"]["checked"]["frame"] == "held"
        baseline = tmp_path / "drift-baseline.json"
        capture_baseline(cfg, PARAMS, baseline, read_provider=provider)
        changes = {"permuted-column": {"column": "alternate_key"},
                   "canonicaliser": {"canonicaliser": "exact"},
                   "surface-field": {"surface_field": "alias"}}
        binding = cfg.frame.identities[""]
        if drift == "removed-frame":
            cfg.frame = None
        elif drift == "removed-database":
            cfg.frame.database = None
        elif drift == "removed-mapping":
            cfg.frame.identities.clear()
        else:
            cfg.frame.identities[""] = binding.model_copy(update=changes[drift])
        result = verify_run(cfg, params=PARAMS, run_record={"executor_kind": "agent", "executor_id": "synthetic"},
                            authorization_ref="test", baseline_path=baseline, read_provider=provider)
        assert result["receipt"]["checked"]["frame"] == "not_checked"
        assert result["receipt"]["outcome"] != "verified"
        assert "baseline identity evidence belongs to another" in result["evidence"]["frame"]["reason"]
