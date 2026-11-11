"""Headless SQLite inputs shared by the reward and task validation controls."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from openadapt_flow.verify_run import VerifyConfig, parse_verify_config

PARAMS = {"record_key": "synthetic-target", "label": "desired-value"}


def setup_store(path: Path) -> VerifyConfig:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE entries (id INTEGER PRIMARY KEY, record_key TEXT, label TEXT)"
        )
    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(path),
                "sql_query": "SELECT id, record_key, label FROM entries",
            },
            "record": {
                "name": "entry",
                "match": ["record_key"],
                "timeout_s": 0.05,
                "fields": {"record_key": "record_key", "label": "label"},
            },
            "frame": {"identity_field": "id"},
        },
        source="synthetic owned-attempt consumer control",
    )


def exercise_external_write(path: Path, *, label: str = PARAMS["label"]) -> None:
    """A separate connection commits before an actuator that changes nothing."""
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone() == (0,)
        connection.execute(
            "INSERT INTO entries (record_key, label) VALUES (?, ?)",
            (PARAMS["record_key"], label),
        )
        assert connection.total_changes == 1
    with closing(sqlite3.connect(path)) as actuator:
        assert actuator.execute("SELECT COUNT(*) FROM entries").fetchone() == (1,)
        assert actuator.total_changes == 0


def observe_receipts(
    monkeypatch: pytest.MonkeyPatch,
    consumer_module: ModuleType,
    verifier_module: ModuleType,
    *,
    owned: bool,
) -> list[dict[str, Any]]:
    """Capture a real baseline and observe the unmodified consumer verification."""
    capture_baseline = verifier_module.capture_baseline
    verify_run = verifier_module.verify_run
    captured: list[Path] = []
    receipts: list[dict[str, Any]] = []

    def capture(cfg: VerifyConfig, params: dict[str, str], out: Path) -> Any:
        if owned:
            result = capture_baseline(cfg, params, out, purpose="owned_attempt")
        else:
            result = capture_baseline(cfg, params, out)
        captured.append(out)
        return result

    def observe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert "purpose" not in kwargs
        assert captured == [kwargs["baseline_path"]]
        result = verify_run(*args, **kwargs)
        receipts.append(result)
        return result

    monkeypatch.setattr(consumer_module, "capture_baseline", capture)
    monkeypatch.setattr(consumer_module, "verify_run", observe)
    return receipts


def check_matching_read(result: dict[str, Any], *, owned: bool) -> None:
    receipt, evidence = result["receipt"], result["evidence"]
    assert receipt["outcome"] == ("reconciliation_required" if owned else "verified")
    assert receipt["effect_verdict"] == ("INDETERMINATE" if owned else "CONFIRMED")
    assert receipt["contracts"]["effect_passed"] is (not owned)
    assert receipt["delivery_uncertain"] is owned
    reads = evidence["readback"]["effects"]
    assert [read["kind"] for read in reads] == ["record_written", "field_equals"]
    for read in reads:
        assert read["verdict"] == "CONFIRMED"
        assert read["observed_effect"] == "present"
        assert read["observed_count"] == 1
    assert evidence["frame"]["status"] == "held"
