"""Credential-free HTTP controls for receipt-bound late rechecks.

This is a synthetic protocol server, not a real-app qualification. It retains
all intermediate versions so an immediately restored wrong write remains
observable. No evaluation delay chooses the configured quiet window.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow import verifier as api
from openadapt_flow.fhir_reference import definition_digest
from openadapt_flow.runtime.effects.targeted import OUTSIDE_INTERMEDIATE_HISTORY

PARAMS = {"name": "private-business-value", "actor": "actor/run"}
ACTOR = api.Actor("agent", "actor/run")
RECORD_ID = "private-record-id"


class Store:
    def __init__(self) -> None:
        self.current: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []
        self.history_mode = "normal"
        self.history_reads = 0
        self.omit_identifying_search = False
        self.fail_identifying_search = False
        self.change_during_identifying_search = False
        self.bridge_mode = False
        self.metadata_delay_s = 0.0
        self.broad_identifying_search = False
        self.extra_resources: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.reply_id_override: str | None = None

    def save(
        self,
        identity: str = RECORD_ID,
        *,
        name: str = PARAMS["name"],
        author: str | None = "actor/run",
        timestamp: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        versions = self.history.setdefault(identity, [])
        if self.bridge_mode and timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        resource = {
            "resourceType": "Basic",
            "id": identity,
            "name": name,
            "meta": {
                "versionId": f"v{len(versions) + 1}",
                "lastUpdated": timestamp or f"2026-01-01T00:00:{len(versions):02d}Z",
            },
        }
        resource.update(copy.deepcopy(fields or {}))
        if author is not None:
            resource["meta"]["author"] = {"reference": author}
        self.current[identity] = copy.deepcopy(resource)
        versions.append(copy.deepcopy(resource))
        if self.bridge_mode:
            self.audit_events.append(
                {
                    "resourceType": "AuditEvent",
                    "id": f"audit-{len(self.audit_events)}",
                    "action": "C" if len(versions) == 1 else "U",
                    "recorded": resource["meta"]["lastUpdated"],
                    "agent": [{"who": {"reference": author}}] if author else [],
                    "entity": [{"what": {"reference": f"Basic/{identity}"}}],
                }
            )


@pytest.fixture
def server():
    store = Store()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            store.calls.append(self.path)
            route = urlsplit(self.path)
            parts = route.path.strip("/").split("/")
            status = 200
            if store.bridge_mode and parts == ["metadata"]:
                time.sleep(store.metadata_delay_s)
                body = {"resourceType": "CapabilityStatement"}
            elif store.bridge_mode and parts == ["AuditEvent"]:
                params = parse_qs(route.query)
                events = copy.deepcopy(store.audit_events)
                for bound in params.get("date", []):
                    instant = datetime.fromisoformat(bound[2:].replace("Z", "+00:00"))
                    if bound.startswith("ge"):
                        events = [
                            e
                            for e in events
                            if datetime.fromisoformat(
                                e["recorded"].replace("Z", "+00:00")
                            )
                            >= instant
                        ]
                    elif bound.startswith("le"):
                        events = [
                            e
                            for e in events
                            if datetime.fromisoformat(
                                e["recorded"].replace("Z", "+00:00")
                            )
                            <= instant
                        ]
                if "action" in params:
                    events = [e for e in events if e["action"] == params["action"][0]]
                if "entity" in params:
                    events = [
                        e
                        for e in events
                        if e["entity"][0]["what"]["reference"] == params["entity"][0]
                    ]
                body = {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(events),
                    "entry": [{"resource": e} for e in events],
                }
            elif parts == ["Basic"]:
                params = parse_qs(route.query)
                records = list(store.current.values())
                if "name" in params:
                    if not store.broad_identifying_search:
                        records = [r for r in records if r["name"] == params["name"][0]]
                    if store.omit_identifying_search:
                        records = []
                body = {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(records),
                    "entry": [{"resource": copy.deepcopy(r)} for r in records],
                }
                if "name" in params and store.fail_identifying_search:
                    status = 503
                    body = {
                        "resourceType": "OperationOutcome",
                        "issue": [{"severity": "error", "code": "transient"}],
                    }
                if "name" in params and store.change_during_identifying_search:
                    store.change_during_identifying_search = False
                    store.save(name="changed-during-identifying-search")
            elif len(parts) == 3 and parts[0] == "Basic" and parts[2] == "_history":
                store.history_reads += 1
                versions = copy.deepcopy(store.history.get(parts[1], []))
                if store.history_mode == "gap":
                    versions = versions[1:]
                if store.history_mode == "empty":
                    versions = []
                if store.history_mode == "unstable" and store.history_reads % 2:
                    versions = versions[:-1]
                body = {
                    "resourceType": "Bundle",
                    "type": "history",
                    "total": len(versions),
                    "entry": [
                        {
                            "resource": r,
                            "request": {"method": "PUT", "url": f"Basic/{parts[1]}"},
                        }
                        for r in reversed(versions)
                    ],
                }
                if store.history_mode == "paged":
                    body["link"] = [{"relation": "next", "url": self.path + "&page=2"}]
            elif len(parts) == 2 and parts[0] == "Basic" and parts[1] in store.current:
                body = store.current[parts[1]]
                if store.bridge_mode:
                    store.audit_events.append(
                        {
                            "resourceType": "AuditEvent",
                            "id": f"audit-{len(store.audit_events)}",
                            "action": "R",
                            "recorded": datetime.now(timezone.utc).isoformat(),
                            "agent": [{"who": {"reference": "actor/run"}}],
                            "entity": [{"what": {"reference": f"Basic/{parts[1]}"}}],
                        }
                    )
            elif "/".join(parts) in store.extra_resources:
                body = store.extra_resources["/".join(parts)]
            else:
                status = 404
                body = {
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found"}],
                }
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            store.calls.append("POST " + self.path)
            length = int(self.headers.get("Content-Length") or 0)
            document = json.loads(self.rfile.read(length))
            if not store.bridge_mode or urlsplit(self.path).path != "/Basic":
                self.send_error(405)
                return
            identity = RECORD_ID
            fields = {
                k: v
                for k, v in document.items()
                if k not in {"resourceType", "id", "meta", "name"}
            }
            store.save(identity, name=document["name"], fields=fields)
            body = copy.deepcopy(store.current[identity])
            if store.reply_id_override is not None:
                body["id"] = store.reply_id_override
            encoded = json.dumps(body).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def contract(base: str, **options: Any) -> api.Contract:
    definition = {
        "resourceType": "StructureDefinition",
        "kind": "resource",
        "type": "Basic",
        "url": "http://hl7.org/fhir/StructureDefinition/Basic",
        "snapshot": {
            "element": [
                {"path": "Basic"},
                {"path": "Basic.name", "type": [{"code": "string"}]},
            ]
        },
    }
    return api.Contract.from_dict(
        {
            "verifier": {
                "kind": "fhir",
                "base_url": base,
                "resource_type": "Basic",
                "fhir_cache_bound_s": 0,
                "field_paths": {"id": "id", "name": "name"},
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {"name": {"param": "name"}},
                    "expected_count": 1,
                }
            ],
            "frame": {
                "identity_field": "id",
                "targeted": {
                    "quiet_s": 0.01,
                    "setup_evidence_sha256": "sha256:" + "a" * 64,
                    "search": {
                        "resource_type": "Basic",
                        "base_url": base,
                        "self": ["actor/run"],
                        "fields": {"name": {"path": "name"}},
                        "identity": [["name"]],
                        "search": {"name": "name"},
                        "reference_schema": {
                            "definition": definition,
                            "sha256": definition_digest(definition),
                        },
                    },
                    "history_retention_qualified": True,
                    "author_path": "meta.author.reference",
                    "author_server_set": True,
                    "self": [{"param": "actor"}],
                    "not_found_qualified": True,
                    **options,
                },
            },
        }
    )


@pytest.fixture
def issued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any):
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    # Deterministic due-time control, separate from automatic scheduler proof.
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    cfg = contract(base)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    store.save()
    write = api.verify_write(pending, write_attempted=True)
    return store, cfg, write, ledger


def late(
    cfg: api.Contract, write: Any, ledger: Any, *, wait: bool = True
) -> api.RecheckResult:
    return api.recheck(cfg, write, params=PARAMS, actor=ACTOR, ledger=ledger, wait=wait)


def test_clean_targeted_recheck_is_final_and_store_unchanged(issued: Any) -> None:
    store, cfg, write, ledger = issued
    assert write.outcome is api.Outcome.CONFIRMED, write.reasons
    assert write.provisional and not write.final
    assert write.evidence["frame"]["targeted"]["phase"] == "write"
    assert write.evidence["frame"]["targeted"]["future_late_search"] == "owed"
    assert "late_search_count" not in write.evidence["frame"]["targeted"]
    before = copy.deepcopy((store.current, store.history))
    prior_history_reads = store.history_reads
    result = late(cfg, write, ledger)
    assert result.outcome == "CONFIRMED", result.reasons
    assert result.final
    assert result.evidence["targeted"]["phase"] == "late"
    assert before == (store.current, store.history)
    assert (
        result.evidence["targeted"]["outside_intermediate_history"]
        == OUTSIDE_INTERMEDIATE_HISTORY
    )
    assert store.history_reads == prior_history_reads + 2
    assert not any(
        "/_history" in p and p.startswith("/Basic/_history") for p in store.calls
    )


@pytest.mark.parametrize(
    "author,expected",
    [("actor/run", "REFUTED"), ("actor/colleague", "CONFIRMED"), (None, "UNCERTAIN")],
)
def test_same_record_later_version_requires_trusted_actor(
    issued: Any, author: str | None, expected: str
) -> None:
    store, cfg, write, ledger = issued
    store.save(name="later-private-value", author=author)
    result = late(cfg, write, ledger)
    assert result.outcome == expected, result.reasons
    if author == "actor/colleague":
        assert result.evidence["targeted"]["legitimate_later_user_edits"] == 1


def test_changed_then_restored_run_version_still_refutes(issued: Any) -> None:
    store, cfg, write, ledger = issued
    store.save(name="wrong-intermediate-value")
    store.save()
    result = late(cfg, write, ledger)
    assert result.outcome == "REFUTED", result.reasons


@pytest.mark.parametrize("mode", ["gap", "empty", "unstable", "paged"])
def test_incomplete_history_never_confirms(issued: Any, mode: str) -> None:
    store, cfg, write, ledger = issued
    store.history_mode = mode
    if mode == "unstable":
        store.save()
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN", result.reasons
    assert not result.final


def test_equal_version_times_are_unknown(issued: Any) -> None:
    store, cfg, write, ledger = issued
    store.save(timestamp="2026-01-01T00:00:00Z")
    assert late(cfg, write, ledger).outcome == "UNCERTAIN"


def test_identifying_search_detects_late_duplicate(issued: Any) -> None:
    store, cfg, write, ledger = issued
    store.save("second-private-record")
    assert late(cfg, write, ledger).outcome == "REFUTED"


def test_missing_record_without_deleted_actor_proof_never_confirms(issued: Any) -> None:
    store, cfg, write, ledger = issued
    del store.current[RECORD_ID]
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN"
    assert not result.final


def test_persisted_state_and_receipt_never_contain_locators_or_values(
    issued: Any, tmp_path: Path
) -> None:
    _, cfg, write, ledger = issued
    assert write.recheck is not None
    state = write.recheck
    path = state.save(tmp_path / "state.json")
    public = json.dumps(write.to_dict()) + json.dumps(state.payload) + repr(state)
    assert RECORD_ID not in public
    assert PARAMS["name"] not in public
    assert path.stat().st_mode & 0o777 == 0o600
    missing = api.RecheckState.load(path, params=PARAMS)
    assert late(cfg, missing, ledger).outcome == "UNCERTAIN"
    correct = api.RecheckState.load(path, params=PARAMS)
    correct.target_ids = (f"Basic/{RECORD_ID}",)
    loaded_result = late(cfg, correct, ledger)
    assert loaded_result.outcome == "UNCERTAIN"
    assert not loaded_result.verified and not loaded_result.final
    assert (
        "late_search_live_read_clock_binding_unknown"
        in loaded_result.evidence["targeted"]["problems"]
    )


def test_foreign_locator_cannot_replace_issued_identity(issued: Any) -> None:
    store, cfg, write, ledger = issued
    store.save("foreign-private-record", name="unrelated")
    assert write.recheck is not None
    write.recheck.target_ids = ("Basic/foreign-private-record",)
    assert late(cfg, write, ledger).outcome == "UNCERTAIN"


def test_params_used_for_actor_binding_cannot_change(issued: Any) -> None:
    _, cfg, write, ledger = issued
    result = api.recheck(
        cfg,
        write,
        params={**PARAMS, "actor": "actor/colleague"},
        actor=ACTOR,
        ledger=ledger,
    )
    assert result.outcome == "UNCERTAIN"


def test_initial_unknown_history_has_distinct_read_only_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    cfg = contract(base, history_available=False, history_retention_qualified=False)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    store.save()
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.UNCERTAIN
    assert not write.provisional and not write.final
    assert write.recheck_purpose == "read_only_reconciliation"
    assert write.recheck_due_at
    assert "recheck_due_at" not in write.receipt
    assert ledger.latest(write.write_key).recheck_owed == write.recheck_due_at
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN"
    assert ledger.latest(write.write_key).recheck_owed == write.recheck_due_at


def test_final_state_only_contract_names_unavailable_intermediate_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    cfg = contract(
        base,
        history_available=False,
        history_retention_qualified=False,
        history_required=False,
    )
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    store.save()
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.CONFIRMED
    assert OUTSIDE_INTERMEDIATE_HISTORY in write.scope.not_covered
    assert any(
        "per-record intermediate history: UNKNOWN/BLOCKED" in line
        for line in write.scope.not_covered
    )
    result = late(cfg, write, None)
    assert result.outcome == "CONFIRMED", result.reasons
    assert result.evidence["targeted"]["history_status"] == "UNKNOWN/BLOCKED"


@pytest.mark.parametrize("ledger_mode", ["original", "none"])
def test_refuted_targeted_recheck_cannot_be_cleared_by_cleaning_duplicate(
    issued: Any, ledger_mode: str
) -> None:
    store, cfg, write, ledger = issued
    store.save("duplicate-before-cleanup")
    selected = ledger if ledger_mode == "original" else None
    first = late(cfg, write, selected)
    assert first.outcome == "REFUTED"
    del store.current["duplicate-before-cleanup"]
    again = late(cfg, write, selected)
    assert again.outcome == "REFUTED", again.reasons
    if selected is not None:
        assert ledger.latest(write.write_key).resolved["outcome"] == "REFUTED"


def test_due_time_blocks_all_late_reads_before_the_frozen_window(
    issued: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cfg, write, ledger = issued
    before = list(store.calls)
    monkeypatch.setattr(api, "seconds_until", lambda due: 10)
    with pytest.raises(api.RecheckNotDue):
        late(cfg, write, ledger, wait=False)
    assert store.calls == before


def test_unqualified_sql_identifying_search_cannot_claim_targeted_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    import sqlite3

    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    _, base = server
    db = tmp_path / "source.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE records (id TEXT PRIMARY KEY, name TEXT)")
    doc = contract(base).config.model_dump(mode="json", by_alias=True)
    doc["verifier"] = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT * FROM records",
    }
    cfg = api.Contract.from_dict(doc)
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO records VALUES (?, ?)", (RECORD_ID, PARAMS["name"]))
    result = api.verify_write(pending, write_attempted=True)
    assert result.outcome is api.Outcome.UNCERTAIN
    assert not result.final
    assert result.evidence["frame"]["targeted"]["problem"] == "TargetedUnreadable"
    assert (
        result.evidence["frame"]["targeted"]["reason"]
        == "independent_target_identity_unknown"
    )
    assert result.recheck is not None
    assert (
        result.recheck_due_at and result.recheck_purpose == "read_only_reconciliation"
    )
    unresolved = result.recheck.payload["post_state"]["targeted"]
    assert unresolved["status"] == "unresolved" and unresolved["targets"] == []
    again = late(cfg, result, None)
    assert again.outcome == "UNCERTAIN" and not again.final
    assert any(
        "independent_target_identity_unknown" in reason for reason in again.reasons
    )


def test_complete_http_body_does_not_replace_retention_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    cfg = contract(base, history_retention_qualified=False)
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    store.save()
    result = api.verify_write(pending, write_attempted=True)
    assert result.outcome is api.Outcome.UNCERTAIN
    assert late(cfg, result, None).outcome == "UNCERTAIN"
    assert not result.final


def test_late_empty_search_cannot_contradict_current_matching_target(
    issued: Any,
) -> None:
    store, cfg, write, ledger = issued
    store.omit_identifying_search = True
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN"
    assert not result.final


def test_bookkeeping_ignore_cannot_erase_a_declared_effect_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    doc = contract(base).config.model_dump(mode="json", by_alias=True)
    doc["verifier"]["field_paths"]["phone"] = "phone"
    doc["effects"][0]["match"]["phone"] = {"literal": "declared-phone"}
    doc["frame"]["ignore_fields"] = ["phone"]
    cfg = api.Contract.from_dict(doc)
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    store.save()
    store.current[RECORD_ID]["phone"] = "declared-phone"
    store.history[RECORD_ID][-1]["phone"] = "declared-phone"
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.CONFIRMED, write.reasons
    store.save()
    store.current[RECORD_ID]["phone"] = "wrong-phone"
    store.history[RECORD_ID][-1]["phone"] = "wrong-phone"
    assert late(cfg, write, None).outcome == "REFUTED"


@pytest.mark.parametrize(
    "change", ["none", "params", "foreign", "missing", "duplicate", "versioned", "post"]
)
def test_locator_preparation_is_pure_and_exact(
    issued: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from openadapt_flow.runtime.effects import targeted

    _, cfg, write, _ = issued
    assert write.recheck is not None
    state = write.recheck
    spec = cfg.config.frame.targeted
    post = copy.deepcopy(state.payload["post_state"]["targeted"])
    locators = state.target_ids
    params = dict(PARAMS)
    if change == "params":
        params["actor"] = "actor/colleague"
    elif change == "foreign":
        locators = ("Basic/foreign-private-record",)
    elif change == "missing":
        locators = ()
    elif change == "duplicate":
        locators = (*locators, *locators)
    elif change == "versioned":
        locators = (locators[0] + "/_history/1",)
    elif change == "post":
        post["unexpected"] = True

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pure preparation performed reader work")

    monkeypatch.setattr(targeted, "_reader", forbidden)
    monkeypatch.setattr(targeted, "_transport", forbidden)
    problem = targeted.targeted_locators_problem(
        spec, post, locators, params, bytes.fromhex(state.payload["salt"])
    )
    assert (problem is None) == (change == "none")


@pytest.mark.parametrize("locators", [(), ("Basic/reply-is-not-authority",)])
def test_unresolved_identity_never_becomes_observable_from_caller_ids(
    issued: Any, monkeypatch: pytest.MonkeyPatch, locators: tuple[str, ...]
) -> None:
    from openadapt_flow.runtime.effects import targeted

    _, cfg, write, _ = issued
    assert write.recheck is not None
    salt = bytes.fromhex(write.recheck.payload["salt"])
    post = targeted.unresolved_targeted(PARAMS, salt, "held")
    assert targeted.targeted_state_problem(post) is None
    assert post["targets"] == [] and "search" not in post

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unresolved target performed reader work")

    monkeypatch.setattr(targeted, "_reader", forbidden)
    monkeypatch.setattr(targeted, "_transport", forbidden)
    assert (
        targeted.targeted_locators_problem(
            cfg.config.frame.targeted, post, locators, PARAMS, salt
        )
        == targeted.UNRESOLVED_TARGET_IDENTITY
    )
    with pytest.raises(
        targeted.TargetedUnreadable, match=targeted.UNRESOLVED_TARGET_IDENTITY
    ):
        targeted.observe_targeted(
            cfg.config.frame.targeted, None, post, locators, PARAMS, salt, "", "", ""
        )
    post["search"] = write.recheck.payload["post_state"]["targeted"]["search"]
    assert targeted.targeted_state_problem(post) is not None


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_initial_unknown_late_observation_does_not_promote_overall_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any, ledger_mode: str
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, base = server
    cfg = contract(base)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    store.save()
    store.history_mode = "empty"
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.UNCERTAIN and not write.final
    assert write.recheck is not None and write.recheck_due_at
    assert write.recheck_purpose == "read_only_reconciliation"
    assert ledger.latest(write.write_key).resolved["outcome"] == "UNCERTAIN"
    store.history_mode = "normal"
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    result = late(cfg, write, selected)
    assert result.evidence["targeted"]["phase"] == "late"
    assert result.evidence["targeted"]["status"] == "held"
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert result.reasons
    assert write.outcome is api.Outcome.UNCERTAIN and not write.final
    attempt = ledger.latest(write.write_key)
    assert (
        attempt.resolved["outcome"] == "UNCERTAIN"
        and attempt.resolved["review"] is True
    )
    assert attempt.recheck_owed == write.recheck_due_at


@pytest.mark.parametrize(
    "author,expected", [("actor/run", "REFUTED"), (None, "UNCERTAIN")]
)
def test_failed_search_preserves_authenticated_wrong_version_only(
    issued: Any, author: str | None, expected: str
) -> None:
    store, cfg, write, ledger = issued
    store.save(name="wrong-own-field", author=author)
    store.fail_identifying_search = True
    before = len(store.calls)
    result = late(cfg, write, ledger)
    assert result.outcome == expected
    assert result.evidence["targeted"]["late_search_status"] == "UNKNOWN"
    assert "identifying_search_unknown" in result.evidence["targeted"]["problems"]
    assert "late_search_count" not in result.evidence["targeted"]
    calls = store.calls[before:]
    current_index = next(
        i
        for i, path in enumerate(calls)
        if urlsplit(path).path == f"/Basic/{RECORD_ID}"
    )
    history_index = next(
        i
        for i, path in enumerate(calls)
        if urlsplit(path).path == f"/Basic/{RECORD_ID}/_history"
    )
    search_index = next(
        i
        for i, path in enumerate(calls)
        if urlsplit(path).path == "/Basic" and "name" in parse_qs(urlsplit(path).query)
    )
    assert current_index < search_index and history_index < search_index
    if expected == "UNCERTAIN":
        assert not result.verified and not result.final


def test_current_changed_during_search_cannot_confirm_stale_target(issued: Any) -> None:
    store, cfg, write, ledger = issued
    store.change_during_identifying_search = True
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert result.evidence["targeted"]["history_status"] == "UNKNOWN/BLOCKED"


@pytest.mark.parametrize("binding", ["locator", "params"])
def test_bad_pure_binding_reads_nothing_and_creates_no_fence(
    issued: Any, tmp_path: Path, binding: str
) -> None:
    store, cfg, write, ledger = issued
    assert write.recheck is not None
    before = list(store.calls)
    params = dict(PARAMS)
    if binding == "locator":
        write.recheck.target_ids = ("Basic/reply-is-not-authority",)
    else:
        params["actor"] = "actor/colleague"
    result = api.recheck(cfg, write, params=params, actor=ACTOR, ledger=ledger)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert store.calls == before
    assert not list((tmp_path / "authority").rglob("*.inflight"))


@pytest.mark.parametrize("caller_ids", [(), ("Basic/reply-only-id",)])
def test_unresolved_owed_deadline_survives_pure_refusal_without_landed_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    caller_ids: tuple[str, ...],
) -> None:
    import sqlite3

    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    _, base = server
    db = tmp_path / "unresolved.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, name TEXT)")
    document = contract(base).config.model_dump(mode="json", by_alias=True)
    document["verifier"] = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT * FROM records",
    }
    cfg = api.Contract.from_dict(document)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO records VALUES (?, ?)", (RECORD_ID, PARAMS["name"])
        )
    write = api.verify_write(pending, write_attempted=True)
    assert (
        write.outcome is api.Outcome.UNCERTAIN
        and not write.verified
        and not write.final
    )
    assert not write.landed and write.review and not write.provisional
    assert write.recheck is not None and write.recheck_due_at
    assert "recheck_due_at" not in write.receipt
    assert write.evidence["reconciliation"]["executable"] is False
    assert ledger.latest(write.write_key).recheck_owed == write.recheck_due_at
    write.recheck.target_ids = caller_ids

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unresolved identity must not read the native store")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = late(cfg, write, ledger)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert any(
        "independent_target_identity_unknown" in reason for reason in result.reasons
    )
    attempt = ledger.latest(write.write_key)
    assert attempt.resolved["landed"] is False and attempt.review
    assert attempt.resolved["outcome"] == "UNCERTAIN"
    assert attempt.resolved["result"]["reconciliation"]["executable"] is False
    assert attempt.recheck_owed == write.recheck_due_at
    assert not list((tmp_path / "authority").rglob("*.inflight"))
    with pytest.raises(api.WriteHeld):
        api.begin_write(cfg, PARAMS, actor=ACTOR, ledger=ledger)


@pytest.mark.parametrize(
    "omit_search,expected", [(False, "CONFIRMED"), (True, "UNCERTAIN")]
)
def test_colleague_nonidentity_edit_still_requires_complete_identifying_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    omit_search: bool,
    expected: str,
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    store, base = server
    doc = contract(base).config.model_dump(mode="json", by_alias=True)
    doc["verifier"]["field_paths"]["note"] = "note"
    cfg = api.Contract.from_dict(doc)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    store.save()
    store.current[RECORD_ID]["note"] = "original-note"
    store.history[RECORD_ID][-1]["note"] = "original-note"
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.CONFIRMED, write.reasons
    store.save(author="actor/colleague")
    store.current[RECORD_ID]["note"] = "colleague-note"
    store.history[RECORD_ID][-1]["note"] = "colleague-note"
    store.omit_identifying_search = omit_search
    result = late(cfg, write, ledger)
    assert result.evidence["targeted"]["legitimate_later_user_edits"] == 1
    assert result.outcome == expected, result.reasons
    assert result.evidence["targeted"]["late_search_status"] == (
        "UNKNOWN" if omit_search else "complete"
    )
    assert result.final is (not omit_search)


@pytest.mark.parametrize("reference_kind", ["absolute", "versioned", "missing"])
def test_known_reference_identity_cannot_be_hidden_by_an_empty_search(
    server: Any,
    reference_kind: str,
) -> None:
    from openadapt_flow.ensure_record import Unreadable
    from openadapt_flow.fhir_reference import ReferenceUnreadable
    from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
    from openadapt_flow.runtime.effects.targeted import FrameTargeted, _search
    from tests.test_fhir_reference import schema

    store, base = server
    data = contract(base).config.frame.targeted.model_dump(mode="json", by_alias=True)
    data["search"]["fields"] = {"name": {"path": "subject.reference"}}
    data["search"]["reference_schema"] = schema("Basic", "subject")
    spec = FrameTargeted.model_validate(data)
    patient = {"resourceType": "Patient", "id": "person", "meta": {"versionId": "2"}}
    store.extra_resources["Patient/person"] = patient
    store.extra_resources["Patient/person/_history/1"] = {
        **patient,
        "meta": {"versionId": "1"},
    }
    wanted = f"{base}/Patient/person"
    if reference_kind == "versioned":
        wanted += "/_history/1"
    elif reference_kind == "missing":
        wanted = "Patient/missing"
    store.save(
        name="Patient/person", fields={"subject": {"reference": "Patient/person"}}
    )
    current = copy.deepcopy(store.current[RECORD_ID])
    store.omit_identifying_search = True
    reader = FhirEffectVerifier(base, resource_type="Basic", field_paths={"id": "id"})
    try:
        with pytest.raises((Unreadable, ReferenceUnreadable)):
            _search(
                spec,
                reader,
                {**PARAMS, "name": wanted},
                "2026-11-05T00:00:00Z",
                current_records=(current,),
            )
    finally:
        reader._get_session().close()
