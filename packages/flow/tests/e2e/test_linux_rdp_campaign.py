"""Opt-in real lossless loopback qualification; software collection never launches it."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.rdp_campaign_fixture import LoopbackSession
from tests.fixtures import rdp_write_app as app

NOTE = "Synthetic account note"
_SESSIONS: dict[str, Any] = {}


class CampaignOwner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "accounts.db"
        self.sessions: list[LoopbackSession] = []
        self.oracles: list[dict[str, Any]] = []
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY, holder TEXT, note TEXT)")
            db.executemany("INSERT INTO accounts VALUES(?,?,?)", [(app.TARGET, "Alex Archer", ""), ("AC200002", "Brook Brooks", "")])

    def rows(self) -> dict[str, dict[str, str]]:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("SELECT id,holder,note FROM accounts ORDER BY id").fetchall()
        return {row[0]: dict(zip(("id", "holder", "note"), row)) for row in rows}

    def reset(self) -> None:
        if any(not session.closed for session in self.sessions):
            raise RuntimeError("previous owned session is still active")
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE accounts SET note='' ")
        assert all(row["note"] == "" for row in self.rows().values())

    def open(self):
        if any(not session.closed for session in self.sessions):
            raise RuntimeError("session overlap refused")
        session = LoopbackSession(self.root / f"session-{len(self.sessions):02d}", self.database)
        self.sessions.append(session)
        return session.start()

    def before_trial(self, context):
        context.state["sqlite_before"] = self.rows()
        assert all(row["note"] == "" for row in context.state["sqlite_before"].values())

    def oracle(self, context, report):
        from openadapt_flow.qualification_faults import fault_detector_contract_error

        before = context.state["sqlite_before"]
        after = self.rows()
        expected = {key: dict(value) for key, value in before.items()}
        expected[app.TARGET]["note"] = NOTE
        exact, untouched = after == expected, after == before
        mutations = report.qualification_fault_mutations
        result = {
            "schema_version": "openadapt.qualify-readback/v1", "oracle_kind": context.oracle_kind,
            "read_path": "independent SQLite connection, complete row comparison", "independent": True,
            "passed": exact if context.oracle_kind == "exact_record" else untouched,
            "observed": ("exact_record" if exact else "record_mismatch") if context.oracle_kind == "exact_record" else ("no_mutation" if untouched else "unexpected_mutation"),
            "effect_verdict": "CONFIRMED" if exact else "REFUTED", "landed": "exact" if exact else ("none" if untouched else "some"),
            "silent_incorrect_success": bool(report.success and not exact),
            "records_before": len(before), "records_after": len(after),
            "fault_receipt_count": len(mutations),
            "fault_detector_error": fault_detector_contract_error(report, mutations[0]) if len(mutations) == 1 else None,
            "execution_outcome": report.execution_outcome, "model_calls": report.model_calls,
        }
        self.oracles.append({"case_id": context.case_id, "trial": context.trial, **result})
        return result


def open_loopback_session(config):
    return _SESSIONS[config.app.name].open()


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("OAFLOW_RDP_CAMPAIGN_E2E") != "1", reason="requires an explicitly owned Linux loopback RDP campaign")
@pytest.mark.timeout(900)
def test_loopback_six_primary_pixel_cases(tmp_path):
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.qualify.campaign import QualifyHooks, qualify
    from openadapt_flow.qualify.config import CASE_IDS, parse_verify_config
    from tests.e2e.hosted_policy_observer import observe_policy

    root = tmp_path / "rdp-campaign"
    root.mkdir()
    owner = CampaignOwner(root)
    name = "loopback-" + uuid.uuid4().hex
    evidence: dict[str, Any] = {"status": "UNRUN", "primary_planned": 6, "informational_planned": 0, "certification_requested": False, "lossless": True}
    try:
        backend, close = owner.open()
        try:
            observed = backend.qualification_environment_identity()
            assert observed is not None and observed[0] == app.APP
            before = owner.rows()

            def driver(rec):
                rec.click(*app.POINTS[0])
                rec.type_text(app.TARGET, param="account")
                rec.click(*app.POINTS[1])
                rec.click(*app.POINTS[2])
                rec.type_text(NOTE, param="note")
                rec.click(*app.POINTS[3])

            recording = record_desktop_demo(backend, root / "recording", driver, identifier_region=app.IDENTIFIER)
            expected = {key: dict(value) for key, value in before.items()}
            expected[app.TARGET]["note"] = NOTE
            assert owner.rows() == expected
        finally:
            close()
        bundle = root / "bundle"
        workflow = compile_recording(recording, bundle, name="loopback-account-note")
        clicks = [step for step in workflow.steps if step.anchor is not None]
        assert len(clicks) == 4
        assert all(step.anchor.structural is None and step.identity_armed and step.anchor.identifier_crop for step in clicks)
        save = clicks[-1]
        _SESSIONS[name] = owner
        config = parse_verify_config({
            "schema_version": "openadapt.verify/v1",
            "app": {"name": name, "target": "rdp", "surface": "remote_display", "application_identity": app.APP,
                    "version": observed[1], "session": f"{__name__}:open_loopback_session",
                    "required_capabilities": ["pixel_frame_capture", "pixel_input"]},
            "minimum_readback": "system_of_record", "save_step": save.id,
            "verifier": {"kind": "sql", "sqlite_database": str(owner.database), "sql_query": "SELECT id,holder,note FROM accounts ORDER BY id"},
            "record": {"name": "account note", "match": ["id"], "duplicate_key": "id", "fields": {"id": {"literal": app.TARGET}, "note": "note"}},
            "inputs": {"params": {"account": app.TARGET, "note": NOTE}},
            "campaign": {"trials": 1, "cases": list(CASE_IDS)},
        }, env={})
        owner.reset()
        evidence["status"] = "RUNNING"
        with observe_policy(evidence):
            result = qualify(bundle, config, out=root / "campaign", trials=1, certify=None,
                             hooks=QualifyHooks(reset=owner.reset, before_trial=owner.before_trial, oracle=owner.oracle), log=lambda _: None)
        evidence.update(status="RETURNED", qualification_passed=result.passed, certified=result.certified)
        assert not result.certified
        assert len(result.rows) == 6 and {row["case_id"] for row in result.rows} == set(CASE_IDS)
        assert all(row["model_calls"] == 0 and not row["fault_declined"] for row in result.rows)
        assert len(owner.oracles) == 6 and all(row["passed"] and row["fault_detector_error"] is None for row in owner.oracles)
        for case in CASE_IDS[1:]:
            report = result.reports[case][0]
            assert report.execution_outcome == "HALTED" and len(report.qualification_fault_mutations) == 1
        assert all(session.closed and not session.cleanup_failed for session in owner.sessions)
        assert result.passed, "six primary pixel cases did not qualify"
        evidence["status"] = "PASSED"
    except BaseException as exc:
        evidence.update(status="FAILED", exception_type=type(exc).__name__)
        raise
    finally:
        _SESSIONS.pop(name, None)
        try:
            for session in reversed(owner.sessions):
                session.close()
        finally:
            evidence["oracles"] = owner.oracles
            evidence["primary_completed"] = len(owner.oracles)
            evidence["informational_completed"] = 0
            evidence["sessions"] = [{"phase": s.phase, "closed": s.closed, "cleanup_failed": s.cleanup_failed,
                                    "child_exits": [p.poll() for p in s.processes],
                                    "task_count": sum(len(o._handles) for o in s.owners)} for s in owner.sessions]
            (root / "campaign-evidence.json").write_text(json.dumps(evidence, indent=2))
