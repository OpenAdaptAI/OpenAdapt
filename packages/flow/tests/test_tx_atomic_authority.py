"""Storage-enforced row controls and unqualified HTTP boundary regressions."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from openadapt_flow.tx import (
    EndpointTemplate,
    EntityLensClient,
    EntityLensSpec,
    LensProbeReport,
    SQLiteAtomicRowAuthority,
    TransactionalCommitEngine,
    TxTask,
)
from openadapt_flow.tx.judge import SafeNormalizedEntailmentOracle
from openadapt_flow.tx.receipt import task_key_for
from openadapt_flow.tx.sqlite_authority import AtomicRowError
from openadapt_flow.tx.types import CertificationProbeResult
from openadapt_flow.tx.wire_tap import SessionWireTap, is_mutation_request


def _lens(url="http://127.0.0.1:1", *, identity="id"):
    return EntityLensSpec(
        entity_name="row",
        base_url=url,
        id_field=identity,
        write_mode="patch",
        get_one=EndpointTemplate(
            method="GET", path_template="/rows/{id}", id_field=identity
        ),
        write_endpoint=EndpointTemplate(
            method="PATCH", path_template="/rows/{id}", id_field=identity
        ),
        volatile_fields=(),
    )


def _db(tmp_path, *, table="records", identity="id", revision="version"):
    db = tmp_path / (table + ".sqlite")
    with sqlite3.connect(db) as c:
        c.execute(
            f'CREATE TABLE "{table}" ("{identity}" TEXT PRIMARY KEY, note TEXT, other TEXT, "{revision}" INTEGER NOT NULL)'
        )
        c.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?)', ("r1", "old", "preserve", 1)
        )
    return SQLiteAtomicRowAuthority(
        db, table=table, id_column=identity, revision_column=revision
    )


@pytest.mark.parametrize(
    "table,identity,revision",
    [("records", "id", "version"), ("ledger_items", "item_key", "revision_no")],
)
def test_real_sqlite_transaction_updates_and_undoes_unchanged_row(
    tmp_path, table, identity, revision
):
    authority = _db(tmp_path, table=table, identity=identity, revision=revision)
    engine = TransactionalCommitEngine(
        _lens(identity=identity), atomic_authority=authority, workdir=tmp_path / "state"
    )
    result = engine.execute(
        TxTask(
            task_id="change",
            operation="update",
            entity_id="r1",
            intended_delta={"note": "new"},
        )
    )
    assert result.outcome == "CONFIRMED", result
    assert result.occ_status == "UNCONTESTED" and result.can_compensate
    assert result.qualification_state == "atomic_row_enforced"
    assert authority.read("r1") == {
        identity: "r1",
        "note": "new",
        "other": "preserve",
        revision: 2,
    }
    assert engine.compensate("change") is True
    assert authority.read("r1") == {
        identity: "r1",
        "note": "old",
        "other": "preserve",
        revision: 3,
    }
    assert (
        engine.compensate("change") is False
    )  # stale postimage cannot authorize replay
    with pytest.raises(AttributeError, match="immutable"):
        authority.table = "other"


def test_undo_compares_at_real_transaction_boundary_and_preserves_other_writer(
    tmp_path,
):
    authority = _db(tmp_path)
    engine = TransactionalCommitEngine(
        _lens(), atomic_authority=authority, workdir=tmp_path / "state"
    )
    assert (
        engine.execute(
            TxTask(
                task_id="change",
                operation="update",
                entity_id="r1",
                intended_delta={"note": "new"},
            )
        ).outcome
        == "CONFIRMED"
    )
    with sqlite3.connect(authority.database) as c:
        c.execute(
            "UPDATE records SET other='human edit', version=version+1 WHERE id='r1'"
        )
    assert engine.compensate("change") is False
    assert authority.read("r1")["other"] == "human edit"
    assert authority.read("r1")["note"] == "new"


@pytest.mark.parametrize("delete", [False, True])
def test_independent_sqlite_connections_cannot_both_consume_same_revision(
    tmp_path, delete
):
    authority = _db(tmp_path)
    expected = authority.read("r1")
    barrier = threading.Barrier(2)

    def mutate():
        barrier.wait(timeout=5)
        if delete:
            return authority.compare_delete("r1", expected=expected)
        return authority.compare_update(
            "r1", expected=expected, values={"note": "candidate"}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: mutate(), range(2)))
    assert sum(result.applied for result in (first, second)) == 1
    assert sum(result.conflict for result in (first, second)) == 1


def test_atomic_backend_refuses_trigger_corruption_schema_drift_and_revision_intent(
    tmp_path,
):
    authority = _db(tmp_path)
    engine = TransactionalCommitEngine(
        _lens(), atomic_authority=authority, workdir=tmp_path / "state"
    )
    result = engine.execute(
        TxTask(
            task_id="revision",
            operation="update",
            entity_id="r1",
            intended_delta={"version": 100},
        )
    )
    assert (
        result.outcome == "BLOCKED_PRECOMMIT"
        and "reserved_atomic_revision_field" in result.reasons
    )
    with sqlite3.connect(authority.database) as c:
        c.execute(
            "CREATE TRIGGER corrupt AFTER UPDATE ON records BEGIN UPDATE records SET other='corrupt' WHERE id=NEW.id; END"
        )
    with pytest.raises(AtomicRowError, match="schema changed"):
        authority.read("r1")
    # Rebinding an operator-approved profile still cannot excuse a changed row.
    authority = SQLiteAtomicRowAuthority(authority.database, table="records")
    expected = authority.read("r1")
    with pytest.raises(AtomicRowError, match="differs"):
        authority.compare_update("r1", expected=expected, values={"note": "new"})
    assert authority.read("r1") == expected  # trigger and mutation rolled back together


def test_candidate_probe_rows_never_grant_execution_authority(tmp_path):
    names = [
        ("clean_roundtrip", "benign"),
        ("benign_concurrent_other_record", "benign"),
        ("fault_partial_clobber", "harmful"),
        ("fault_wrong_value", "harmful"),
        ("fault_wrong_record", "harmful"),
        ("fault_no_persist", "harmful"),
    ]
    forged = LensProbeReport(
        lens=_lens(),
        probes_passed=True,
        sensitivity=1,
        specificity=1,
        probes=tuple(
            CertificationProbeResult(
                probe_name=n,
                polarity=p,
                expected_reject=p == "harmful",
                observed_reject=p == "harmful",
                passed=True,
            )
            for n, p in names
        ),
    ).with_computed_digest()
    assert forged.probe_integrity_errors() == ()
    assert forged.admission_errors() == ("unqualified_probe_report",)
    result = TransactionalCommitEngine(forged, workdir=tmp_path).execute(
        TxTask(
            task_id="candidate",
            operation="update",
            entity_id="r1",
            intended_delta={"note": "new"},
        )
    )
    assert (
        result.outcome == "BLOCKED_PRECOMMIT"
        and result.qualification_state == "unqualified"
    )
    assert not result.can_compensate and result.rows_read == 0
    with pytest.raises(ValidationError):
        TransactionalCommitEngine(
            forged.model_copy(update={"certified": True}), workdir=tmp_path / "copy"
        )


def test_http_redirects_cannot_change_endpoint_method_or_forward_credentials(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            self.send_response(302 if self.path.startswith("/rows") else 200)
            self.send_header(
                "Location", f"http://127.0.0.1:{self.server.server_port}/shadow/r1"
            )
            self.end_headers()
            if self.path.startswith("/shadow"):
                self.wfile.write(b'{"id":"r1","note":"new"}')

        do_POST = do_GET
        do_PATCH = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        client = EntityLensClient(
            _lens(url), headers={"Authorization": "Bearer synthetic"}
        )
        assert client.get_one("r1")[1] is None
        assert (
            client.write("update", {"note": "new"}, entity_id="r1").status_code == 302
        )
        with SessionWireTap(url) as tap:
            tapped = EntityLensClient(_lens(tap.base_url))
            relayed = tapped.write("update", {"note": "new"}, entity_id="r1")
            # The tap relays a same-origin redirect to the client, rewritten
            # onto its own origin, and never follows it itself.
            assert relayed.status_code == 302 and not relayed.ok
        assert all(path.startswith("/rows") for _, path, _ in seen)
        assert [method for method, _, _ in seen] == ["GET", "PATCH", "PATCH"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_low_entropy_task_id_is_absent_from_paths_and_public_commitment(tmp_path):
    authority = _db(tmp_path)
    engine = TransactionalCommitEngine(
        _lens(), atomic_authority=authority, workdir=tmp_path / "state"
    )
    result = engine.execute(
        TxTask(
            task_id="PT-100",
            operation="update",
            entity_id="r1",
            intended_delta={"note": "new"},
        )
    )
    receipt = Path(result.receipt_path).read_bytes().decode("latin-1")
    assert "PT-100" not in receipt and "task_id_sha256" not in receipt
    assert "task_id_hmac_sha256" in receipt
    assert task_key_for(
        "PT-100", workdir=engine.workdir
    ) != "PT-100" and "PT-100" not in str(Path(result.receipt_path))
    assert not any(
        "PT-100" in str(p.relative_to(engine.workdir))
        for p in engine.workdir.rglob("*")
    )


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_graphql_write_classification_uses_operation_not_http_read_method(method):
    doc = 'mutation Change { changeNote(id: 1, note: "new") { id } }'
    if method == "POST":
        args = (
            "/graphql",
            json.dumps({"query": doc}).encode(),
            {"Content-Type": "application/json"},
        )
    else:
        args = ("/graphql?" + urlencode({"query": doc}),)
    assert is_mutation_request(method, *args) == (True, True)
    assert is_mutation_request(
        "GET", "/graphql?" + urlencode({"query": "query Read { item { id } }"})
    ) == (False, False)


def test_structured_sibling_metadata_cannot_hide_business_mutation():
    oracle = SafeNormalizedEntailmentOracle()
    for key in ("meta", "extension", "period", "version"):
        assert not oracle.entails(
            "object",
            {"name": "new"},
            {"name": "new", key: {"privilege": "admin"}},
            {"name": "old", key: {"privilege": "reader"}},
        )[0]


def test_default_wrapped_callable_is_not_run_without_enforcing_authority(tmp_path):
    engine = TransactionalCommitEngine(_lens(), workdir=tmp_path)
    called = []
    result = engine.wrap_action(
        TxTask(
            task_id="external",
            operation="update",
            entity_id="r1",
            intended_delta={"note": "new"},
        ),
        lambda: called.append(True),
    )
    assert result.outcome == "BLOCKED_PRECOMMIT" and called == []
    assert "enforcing_mutation_authority_required" in result.reasons


def test_unqualified_http_undo_never_relies_on_ignored_if_match(tmp_path):
    state = {"id": "r1", "note": "old", "other": "keep", "version": 1}
    writes = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", '"stable"')
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())

        def do_PATCH(self):
            values = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            writes.append(self.headers.get("If-Match"))
            if len(writes) > 1:
                state["other"] = "intervening edit at write boundary"
            state.update(values)
            state["version"] += 1  # deliberately ignores If-Match
            self.do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        http_lens = _lens(f"http://127.0.0.1:{server.server_port}").model_copy(
            update={"volatile_fields": ("version",)}
        )
        engine = TransactionalCommitEngine(
            http_lens, workdir=tmp_path, allow_unqualified_prototype=True
        )
        result = engine.execute(
            TxTask(
                task_id="http-undo",
                operation="update",
                entity_id="r1",
                intended_delta={"note": "new"},
            )
        )
        assert (
            result.outcome == "UNCERTAIN"
            and result.qualification_state == "unqualified"
        )
        assert not result.can_compensate and len(writes) == 1
        assert engine.compensate("http-undo") is False and len(writes) == 1
        assert state["note"] == "new" and state["other"] == "keep"
        proposal = engine.compensation_proposal("http-undo")
        from openadapt_flow import crypto

        assert proposal is not None and crypto.is_encrypted(proposal.read_bytes())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_redirect_to_distinct_origin_never_receives_authorization(tmp_path):
    foreign_requests = []

    class Foreign(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            foreign_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"r1","note":"new"}')

    foreign = ThreadingHTTPServer(("127.0.0.1", 0), Foreign)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{foreign.server_port}/foreign/r1"
            )
            self.end_headers()

        do_PATCH = do_GET

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True) for s in (origin, foreign)
    ]
    for thread in threads:
        thread.start()
    try:
        client = EntityLensClient(
            _lens(f"http://127.0.0.1:{origin.server_port}"),
            headers={"Authorization": "Bearer synthetic-token"},
        )
        assert client.get_one("r1")[1] is None
        assert (
            client.write("update", {"note": "new"}, entity_id="r1").status_code == 302
        )
        assert foreign_requests == []
    finally:
        for server in (origin, foreign):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_browser_route_without_redirect_control_never_continues_network():
    class Page:
        def route(self, pattern, handler):
            self.handler = handler

    class Request:
        url = "http://127.0.0.1:1/rows/r1"
        method = "PATCH"
        headers = {}
        post_data_buffer = b'{"note":"new"}'

    class Route:
        request = Request()
        continued = False
        aborted = False

        def continue_(self):
            self.continued = True

        def abort(self):
            self.aborted = True

    page = Page()
    tap = SessionWireTap("http://127.0.0.1:1")
    tap.attach_playwright_route(page)
    route = Route()
    page.handler(route)
    assert route.aborted and not route.continued
    assert tap.summary().mutations[0].response_status is None


@pytest.mark.parametrize("missing", ["cbor2", "cwt"])
def test_missing_receipt_dependency_refuses_before_network_or_local_state(
    tmp_path, missing
):
    state = tmp_path / "must-not-create"
    script = """
import sys
from pathlib import Path
class MissingReceiptDependency:
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] == sys.argv[1]:
            raise ModuleNotFoundError("receipts dependency deliberately unavailable")
sys.meta_path.insert(0, MissingReceiptDependency())
from openadapt_flow.tx import EntityLensClient, EntityLensSpec, EndpointTemplate, TransactionalCommitEngine, TxTask
def forbid_network(*args, **kwargs):
    raise AssertionError("network reached without receipt integrity")
EntityLensClient._request = forbid_network
lens = EntityLensSpec(entity_name="row", base_url="http://127.0.0.1:1",
    get_one=EndpointTemplate(method="GET",path_template="/rows/{id}"),
    write_endpoint=EndpointTemplate(method="PATCH",path_template="/rows/{id}"))
try:
    engine = TransactionalCommitEngine(lens,workdir=sys.argv[2],allow_unqualified_prototype=True)
    engine.execute(TxTask(task_id="missing-receipt",operation="update",entity_id="r1",intended_delta={"note":"new"}))
except RuntimeError as exc:
    assert "openadapt-flow[receipts]" in str(exc)
else:
    raise AssertionError("missing receipt dependency was admitted")
assert not Path(sys.argv[2]).exists()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, missing, str(state)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_candidate_author_hints_cannot_expand_the_enforced_task_delta(tmp_path):
    from openadapt_flow.tx.types import JudgeVerdict

    authority = _db(tmp_path)
    base_lens = _lens()
    candidate = base_lens.model_copy(
        update={
            "volatile_fields": ("note", "other"),
            "write_endpoint": base_lens.write_endpoint.model_copy(
                update={"author_field": "other"}
            ),
            "expected_author": "candidate-injected",
        }
    )

    class AcceptEverything:
        def evaluate(self, *args, **kwargs):
            return JudgeVerdict(
                passed=True, frame_preserved=True, intent_satisfied=True
            )

    engine = TransactionalCommitEngine(
        candidate,
        atomic_authority=authority,
        judge=AcceptEverything(),
        workdir=tmp_path / "state",
    )
    result = engine.execute(
        TxTask(
            task_id="candidate-policy",
            operation="update",
            entity_id="r1",
            intended_delta={"note": "requested"},
        )
    )
    assert result.outcome == "UNCERTAIN"
    assert "concurrent_author_mismatch" in result.reasons
    assert authority.read("r1")["note"] == "requested"
    assert authority.read("r1")["other"] == "preserve"


def test_atomic_profile_refuses_unsupported_column_identifiers(tmp_path):
    database = tmp_path / "unsupported.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE entries (id TEXT PRIMARY KEY, version INTEGER, "unusual column" TEXT)'
        )
    with pytest.raises(AtomicRowError, match="column identifier"):
        SQLiteAtomicRowAuthority(database, table="entries")
