"""Fast (<3s) verification tests for the tx ACID benchmark servers, faults, and arms."""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from pathlib import Path

import pytest

import benchmark as _bm_pkg

_FLOW_ROOT = Path(__file__).resolve().parents[1]
_FLOW_BM_DIR = str(_FLOW_ROOT / "benchmark")
if hasattr(_bm_pkg, "__path__") and _FLOW_BM_DIR not in _bm_pkg.__path__:
    _bm_pkg.__path__.insert(0, _FLOW_BM_DIR)

from benchmark.tx_acid import (  # noqa: E402
    ALL_BENCHMARK_FAULTS,
    ALL_WRITE_FAULTS,
    BackgroundTrafficGenerator,
    ClaimsLedgerServer,
    ClinicalChartServer,
    FaultConfig,
    FaultInjectingProxy,
    LegacyWholeTableContractArm,
    NaiveAgent2xxArm,
    ReferenceTxArm,
    ReplaceTaskServer,
    render_markdown_report,
    run_tx_acid_benchmark,
)
from benchmark.tx_acid.baselines import _http_json_request  # noqa: E402
from openadapt_flow.tx.types import TxTask  # noqa: E402


def test_import_boundary_zero_vision_or_ml_imports() -> None:
    """Ensure benchmark/tx_acid contains zero vision/ML imports."""
    forbidden = {"numpy", "cv2", "PIL", "pillow", "rapidocr", "rapidocr_onnxruntime"}
    tx_acid_dir = _FLOW_ROOT / "benchmark" / "tx_acid"
    py_files = sorted(tx_acid_dir.glob("*.py"))
    assert len(py_files) == 4

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in forbidden, (
                        f"{py_file.name} imports {root_mod}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                assert root_mod not in forbidden, f"{py_file.name} imports {root_mod}"


def test_clinical_chart_server_wal_pagination_and_normalization() -> None:
    """ClinicalChartServer seeds 1,000+ rows in WAL mode, paginates at 200, and normalizes."""
    with ClinicalChartServer(seed_rows=1_000) as srv:
        assert srv.journal_mode().lower() == "wal"
        assert srv.count_rows() == 1_000
        assert srv.seed_duration_ms < 400.0

        # Collection GET caps page size at 200 and exposes links.next without snapshot token
        status, _, page = _http_json_request(
            srv.base_url, "GET", "/api/patients?_limit=500"
        )
        assert status == 200
        assert len(page["items"]) == 200
        assert "next" in page["links"]

        # Confusable ID PT-0O1l8 vs PT-0O118 + O(1) indexed GET by id and email
        st_id, hdrs_id, pt = _http_json_request(
            srv.base_url, "GET", "/api/patients/PT-0O1l8"
        )
        assert st_id == 200
        assert pt["fname"] == "Bo"
        assert pt["lname"] == "Li"
        assert pt["version"] == 1
        assert pt["last_author"] == "seed"
        assert hdrs_id.get("etag") == '"PT-0O1l8:v1"'

        st_em, _, by_email = _http_json_request(
            srv.base_url, "GET", "/api/patients?email=bo.li@clinic.example"
        )
        assert st_em == 200
        assert len(by_email["items"]) == 1
        assert "next" not in by_email["links"]
        assert by_email["items"][0]["id"] == "PT-0O1l8"

        # PATCH normalizes Title Case names, UPPER street/city, and state bijections
        st_patch, hdrs_patch, updated = _http_json_request(
            srv.base_url,
            "PATCH",
            "/api/patients/PT-0O1l8",
            body={
                "fname": "amy",
                "lname": "wu",
                "street": "7 mill rd",
                "city": "cambridge",
                "state": "Massachusetts",
            },
            headers={"X-Actor-Id": "nurse-1"},
        )
        assert st_patch == 200
        assert updated["fname"] == "Amy"
        assert updated["lname"] == "Wu"
        assert updated["street"] == "7 MILL RD"
        assert updated["city"] == "CAMBRIDGE"
        assert updated["state"] == "MA"
        assert updated["version"] == 2
        assert updated["last_author"] == "nurse-1"
        assert hdrs_patch.get("etag") == '"PT-0O1l8:v2"'

        # POST normalizes California -> CA and Ontario -> ON
        st_ca, _, created_ca = _http_json_request(
            srv.base_url,
            "POST",
            "/api/patients",
            body={
                "fname": "bo",
                "lname": "li",
                "email": "bo.ca@clinic.example",
                "street": "101 pine st",
                "city": "san francisco",
                "state": "California",
            },
        )
        assert st_ca == 201
        assert created_ca["state"] == "CA"
        assert created_ca["fname"] == "Bo"

        st_on, _, created_on = _http_json_request(
            srv.base_url,
            "POST",
            "/api/patients",
            body={
                "fname": "al",
                "lname": "ng",
                "email": "al.on@clinic.example",
                "street": "88 king st",
                "city": "toronto",
                "state": "Ontario",
            },
        )
        assert st_on == 201
        assert created_on["state"] == "ON"


def test_replace_task_server_put_clobber_and_non_idempotent_post() -> None:
    """ReplaceTaskServer overwrites omitted fields on PUT and duplicates rows on POST retry."""
    with ReplaceTaskServer(seed_rows=1_000) as srv:
        assert srv.journal_mode().lower() == "wal"
        assert srv.count_rows() == 1_000

        before = srv.get_record_direct("TSK-0O1l8")
        assert before is not None
        assert before["description"] != ""
        assert before["assignee"] == "Bo Li"
        assert before["due_date"] == "2026-12-15"
        assert before["labels"] != ""
        assert before["budget_cents"] > 0
        assert before["department"] == "PHARMACY_OPS"

        # Partial PUT returns 200 OK while clobbering all omitted fields to "" or 0!
        st_put, _, after = _http_json_request(
            srv.base_url,
            "PUT",
            "/api/tasks/TSK-0O1l8",
            body={"title": before["title"], "status": "completed"},
        )
        assert st_put == 200
        assert after["status"] == "completed"
        assert after["description"] == ""
        assert after["assignee"] == ""
        assert after["due_date"] == ""
        assert after["labels"] == ""
        assert after["budget_cents"] == 0
        assert after["department"] == ""

        # Non-idempotent POST creates duplicate rows when retried
        create_body = {
            "external_ref": "DUP-TEST-REF-99",
            "title": "Duplicate Check Task",
            "status": "open",
            "description": "Non-idempotent create test",
            "assignee": "Bo Li",
            "due_date": "2026-12-30",
            "labels": "erp",
            "budget_cents": 50000,
            "department": "FINANCE_ERP",
        }
        s1, _, r1 = _http_json_request(
            srv.base_url, "POST", "/api/tasks", body=create_body
        )
        s2, _, r2 = _http_json_request(
            srv.base_url, "POST", "/api/tasks", body=create_body
        )
        assert s1 == 201 and s2 == 201
        assert r1["id"] != r2["id"]
        assert len(srv.find_records_direct("external_ref", "DUP-TEST-REF-99")) == 2


def test_claims_ledger_server_occ_phone_and_iso_date_normalization() -> None:
    """ClaimsLedgerServer normalizes E.164 phones, UTC ISO dates, audit_seq, and If-Match OCC."""
    with ClaimsLedgerServer(seed_rows=1_000) as srv:
        assert srv.journal_mode().lower() == "wal"
        assert srv.count_rows() == 1_000

        before = srv.get_record_direct("CLM-0O1l8")
        assert before is not None
        old_etag = before["etag"]
        old_audit_seq = before["audit_seq"]

        st_patch, hdrs, after = _http_json_request(
            srv.base_url,
            "PATCH",
            "/api/claims/CLM-0O1l8",
            body={
                "member_phone": "(416) 555-0199",
                "service_date": "2026-11-19",
                "status": "approved",
            },
            headers={"If-Match": old_etag, "X-Actor-Id": "adjuster-7"},
        )
        assert st_patch == 200
        assert after["member_phone"] == "+14165550199"
        assert after["service_date"] == "2026-11-19T00:00:00Z"
        assert after["version"] == before["version"] + 1
        assert after["audit_seq"] > old_audit_seq
        assert after["last_author"] == "adjuster-7"
        assert hdrs.get("etag") == '"CLM-0O1l8:v2"'

        # Stale If-Match precondition must return 412 Precondition Failed
        st_stale, _, _ = _http_json_request(
            srv.base_url,
            "PATCH",
            "/api/claims/CLM-0O1l8",
            body={"status": "paid"},
            headers={"If-Match": old_etag},
        )
        assert st_stale == 412
        assert srv.get_record_direct("CLM-0O1l8")["status"] == "approved"  # type: ignore[index]


def test_50k_seed_speed_and_8_worker_background_concurrency() -> None:
    """Verify <400ms 50k-row bulk seed and lock-free 8-worker concurrency across all 3 servers."""
    with ClinicalChartServer(seed_rows=50_000) as srv_50k:
        assert srv_50k.count_rows() == 50_000
        assert srv_50k.seed_duration_ms < 400.0

    for cls in (ClinicalChartServer, ReplaceTaskServer, ClaimsLedgerServer):
        with cls(seed_rows=1_000) as srv:
            with BackgroundTrafficGenerator(srv, num_workers=8, interval_s=0.001) as bg:
                deadline = time.perf_counter() + 0.5
                while bg.ops_completed < 24 and time.perf_counter() < deadline:
                    time.sleep(0.005)
                # Perform concurrent foreground O(1) read while workers hammer SQLite WAL
                st, _, _ = _http_json_request(
                    srv.base_url,
                    "GET",
                    f"{srv.collection_path}/{srv.confusable_primary_id}",
                )
                assert st == 200
            assert bg.ops_completed >= 24
            assert bg.creates_completed + bg.updates_completed == bg.ops_completed
            assert bg.errors == [], (
                f"Unexpected SQLite/HTTP concurrency errors: {bg.errors}"
            )


def test_fault_injecting_proxy_all_6_write_faults_plus_occ_race() -> None:
    """Verify FaultInjectingProxy produces all 6 write-path faults + OCC race accurately."""
    assert len(ALL_WRITE_FAULTS) == 6
    assert len(ALL_BENCHMARK_FAULTS) == 7

    naive = NaiveAgent2xxArm()
    tx_arm = ReferenceTxArm()
    try:
        with (
            ClinicalChartServer(seed_rows=1_000) as srv,
            FaultInjectingProxy(srv.base_url, server=srv) as proxy,
        ):
            for fk in ALL_BENCHMARK_FAULTS:
                proxy.set_fault(
                    FaultConfig(
                        fault_kind=fk,
                        collateral_id="PT-000050",
                        wrong_target_id="PT-0O118",
                    )
                )
                snap = srv.snapshot_records(["PT-0O1l8", "PT-0O118", "PT-000050"])
                if fk == "duplicate_post_on_retry":
                    dup_email = "dup.fault.unit@clinic.example"
                    srv.delete_where_field("email", dup_email)
                    task = TxTask(
                        task_id=f"fault-{fk}",
                        operation="create",
                        intended_delta={
                            "fname": "Elena",
                            "lname": "Patel",
                            "email": dup_email,
                            "street": "12 oak st",
                            "city": "boston",
                            "state": "Massachusetts",
                        },
                    )
                    n_res = naive.execute(srv, task, fault_proxy=proxy)
                    assert n_res.accepted is True
                    # Fault injector actually created 2 duplicate rows in SQLite!
                    assert len(srv.find_records_direct("email", dup_email)) == 2
                    srv.delete_where_field("email", dup_email)

                    tx_res = tx_arm.execute(srv, task, fault_proxy=proxy)
                    assert tx_res.accepted is False
                    srv.delete_where_field("email", dup_email)
                else:
                    task = TxTask(
                        task_id=f"fault-{fk}",
                        operation="update",
                        entity_id="PT-0O1l8",
                        intended_delta={
                            "street": "55 walnut st",
                            "state": "Massachusetts",
                        },
                    )
                    n_res = naive.execute(srv, task, fault_proxy=proxy)
                    assert n_res.accepted is True

                    after_pt = srv.get_record_direct("PT-0O1l8")
                    assert after_pt is not None
                    if fk == "partial_put_clobber":
                        assert after_pt["allergies"] == "" and after_pt["notes"] == ""
                    elif fk == "silent_drop_200_ok":
                        assert after_pt["street"] == snap["PT-0O1l8"]["street"]  # type: ignore[index]
                    elif fk == "wrong_value_mutation":
                        assert after_pt["street"] == "55 WALNUT ST_CORRUPTED"
                    elif fk == "collateral_extra_write":
                        col_after = srv.get_record_direct("PT-000050")
                        assert col_after is not None
                        assert col_after["notes"] == "UNAUTHORIZED_COLLATERAL_EDIT"
                    elif fk == "wrong_target_record":
                        assert after_pt["street"] == snap["PT-0O1l8"]["street"]  # type: ignore[index]
                        sib_after = srv.get_record_direct("PT-0O118")
                        assert sib_after is not None
                        assert sib_after["street"] == "55 WALNUT ST"
                    elif fk == "same_record_concurrent_race":
                        # Naive arm overwrote / committed on top of concurrent clinician write (v1 -> v3)
                        assert after_pt["version"] == 3

                    srv.restore_records(snap)
                    tx_res = tx_arm.execute(srv, task, fault_proxy=proxy)
                    assert tx_res.accepted is False
                    if fk == "same_record_concurrent_race":
                        assert tx_res.occ_status == "CONCURRENT_MODIFICATION"
                    srv.restore_records(snap)
    finally:
        tx_arm.close()


def test_legacy_whole_table_contract_arm_honest_modes() -> None:
    """Verify LegacyWholeTableContractArm passes clean <200-row unnormalized, and halts on real triggers."""
    legacy = LegacyWholeTableContractArm()

    # 1. >200 rows -> INDETERMINATE_PAGINATION_CAP
    with ClinicalChartServer(seed_rows=1_000) as srv_big:
        res_page = legacy.execute(
            srv_big,
            TxTask(
                task_id="legacy-gt-200",
                operation="update",
                entity_id="PT-000010",
                intended_delta={"street": "500 MAIN ST"},
            ),
        )
        assert res_page.accepted is False
        assert res_page.outcome_label == "INDETERMINATE_PAGINATION_CAP"
        assert res_page.rows_read == 200

        # 2. <4 char identity param -> SHORT_PARAM_HALT
        res_short = legacy.execute(
            srv_big,
            TxTask(
                task_id="legacy-short",
                operation="update",
                entity_id="PT-0O1l8",
                expected_identity={"fname": "Bo", "lname": "Li"},
                intended_delta={"street": "500 MAIN ST"},
            ),
        )
        assert res_short.accepted is False
        assert res_short.outcome_label == "SHORT_PARAM_HALT"

    # 3. <200 rows, no concurrency: literal normalized string passes, unnormalized fails!
    with ClinicalChartServer(seed_rows=50) as srv_small:
        res_ok = legacy.execute(
            srv_small,
            TxTask(
                task_id="legacy-small-exact",
                operation="update",
                entity_id="PT-000007",
                intended_delta={"street": "900 BROADWAY", "state": "MA"},
            ),
        )
        assert res_ok.accepted is True
        assert res_ok.outcome_label == "CONFIRMED"

        res_norm_fail = legacy.execute(
            srv_small,
            TxTask(
                task_id="legacy-small-norm-fail",
                operation="update",
                entity_id="PT-000007",
                intended_delta={"street": "7 mill rd", "state": "Massachusetts"},
            ),
        )
        assert res_norm_fail.accepted is False
        assert res_norm_fail.outcome_label == "LITERAL_MISMATCH"

        # 4. <200 rows WITH active background concurrency -> INDETERMINATE_CONCURRENT_DIFF
        with BackgroundTrafficGenerator(srv_small, num_workers=8, interval_s=0.0) as bg:
            deadline = time.perf_counter() + 0.5
            saw_concurrent_diff = False
            i = 0
            while time.perf_counter() < deadline and not saw_concurrent_diff:
                i += 1
                res_conc = legacy.execute(
                    srv_small,
                    TxTask(
                        task_id=f"legacy-conc-{i}",
                        operation="update",
                        entity_id="PT-000008",
                        intended_delta={"street": f"{100 + i} BROADWAY"},
                    ),
                )
                if res_conc.outcome_label in (
                    "INDETERMINATE_CONCURRENT_DIFF",
                    "INDETERMINATE_PAGINATION_CAP",
                ):
                    saw_concurrent_diff = True
            assert bg.ops_completed > 0
            assert saw_concurrent_diff is True


def test_runner_metrics_pluggable_factory_and_report_generation() -> None:
    """Verify run_tx_acid_benchmark records accurate per-app/per-arm metrics and renders reports."""
    factory_calls = 0

    def _custom_factory(server: object) -> ReferenceTxArm:
        nonlocal factory_calls
        factory_calls += 1
        return ReferenceTxArm()

    report = run_tx_acid_benchmark(
        seed_rows=1_000,
        num_workers=8,
        clean_tasks_per_app=4,
        fault_repeats=1,
        bg_interval_s=0.001,
        tx_arm_factory=_custom_factory,
    )
    assert factory_calls > 0
    assert report.tx_backend == "custom_tx_arm_factory"
    assert report.total_background_writes_landed > 0
    assert set(report.apps.keys()) == {
        "clinical_chart_ehr",
        "replace_task_erp",
        "claims_ledger",
    }

    for app_rep in report.apps.values():
        assert app_rep.background_errors == 0
        assert app_rep.background_concurrent_writes_landed > 0

    agg_tx = report.aggregate_by_arm["tx_acid"]
    agg_naive = report.aggregate_by_arm["naive_2xx"]
    agg_legacy = report.aggregate_by_arm["legacy_whole_table"]

    assert agg_tx.clean_pass_rate == 1.0
    assert agg_tx.false_stop_rate == 0.0
    assert agg_tx.fault_catch_rate == 1.0
    assert agg_tx.false_accept_rate == 0.0
    assert agg_tx.mean_rows_read_per_tx <= 3.0

    assert agg_naive.fault_catch_rate == 0.0
    assert agg_naive.false_accept_rate == 1.0
    assert (
        report.apps["replace_task_erp"].arms["naive_2xx"].clean_silent_corruptions > 0
    )

    assert agg_legacy.clean_pass_rate == 0.0
    assert agg_legacy.false_stop_rate == 1.0

    as_json = json.dumps(report.to_dict(), sort_keys=True)
    assert "clinical_chart_ehr" in as_json
    md = render_markdown_report(report)
    assert "## Aggregate Comparison Across All 3 Systems of Record" in md
    assert "`partial_put_clobber`" in md


@pytest.mark.parametrize(
    "server_type", [ClinicalChartServer, ReplaceTaskServer, ClaimsLedgerServer]
)
def test_bulk_seed_preserves_every_row_and_builds_indexes_in_one_transaction(
    server_type, monkeypatch
):
    """All fixture schemas share a bounded seed, not one statement per row."""
    connect = sqlite3.connect
    inserts = []
    index_transactions = []

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(statement):
            # Record counts/state only, never expanded parameter values.
            if statement.startswith("INSERT INTO "):
                inserts.append(True)
            elif statement.startswith("CREATE INDEX "):
                index_transactions.append(connection.in_transaction)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    server = server_type(seed_rows=137)
    try:
        connection = server._conn()
        actual = [
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {server.table_name} ORDER BY id"
            )
        ]
        expected = sorted(server._generate_seed_rows(137), key=lambda row: row[0])
        assert actual == expected
        assert len(inserts) < 137 // 2
        assert index_transactions == [True, True]
        assert server.count_rows() == 137
        assert server.journal_mode().lower() == "wal"
    finally:
        server.close()


@pytest.mark.parametrize(
    "server_type", [ClinicalChartServer, ReplaceTaskServer, ClaimsLedgerServer]
)
def test_index_failure_rolls_back_the_seed_for_every_fixture(server_type, tmp_path):
    """Failed initialization must not leave committed rows or partial indexes."""

    class FailedIndexes(server_type):
        def _create_indexes(self, connection):
            super()._create_indexes(connection)
            raise RuntimeError("synthetic index failure")

    database = tmp_path / "seed.sqlite3"
    server = FailedIndexes.__new__(FailedIndexes)
    try:
        with pytest.raises(RuntimeError, match="synthetic index failure"):
            server.__init__(seed_rows=137, db_path=str(database))
    finally:
        server.close()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM {server_type.table_name}"
            ).fetchone()[0]
            == 0
        )
        indexes = connection.execute(
            f"PRAGMA index_list({server_type.table_name})"
        ).fetchall()
        assert all(row[3] != "c" for row in indexes)
