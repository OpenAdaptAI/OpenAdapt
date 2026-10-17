"""``openadapt-flow admit status`` reads the ledger. It never mints one."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from openadapt_flow.__main__ import main
from openadapt_flow.release_admission import (
    ADMITTED,
    EXPECTED_TARGETS,
    NOT_ADMITTED,
    classify_admission,
    load_ledger,
    status_report,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _ledger(admissions: dict[str, dict | None]) -> dict:
    targets = []
    for target_id in EXPECTED_TARGETS:
        targets.append(
            {
                "id": target_id,
                "display_name": target_id,
                "admission_history": [],
                "latest_admission": admissions.get(target_id),
            }
        )
    return {
        "schema_version": "openadapt.public-production-lifecycle/v1",
        "derivation": {
            "mode": "latest_signed_admission_at_read_time",
            "static_production_state": False,
        },
        "targets": targets,
    }


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "production-lifecycle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_empty_ledger_reports_flow_not_admitted(tmp_path: Path) -> None:
    path = _write(tmp_path, _ledger({}))
    report = status_report(load_ledger(path), now=NOW)
    assert report["flow"]["state"] == NOT_ADMITTED
    assert report["flow"]["latest_admission"] is None
    assert "no live, non-revoked admission" in report["flow"]["reason"]
    assert report["product_wide_production"] is False
    assert report["pack"]["complete"] is False
    assert "wheel_digest" in report["pack"]["missing"]
    for row in report["targets"]:
        assert row["state"] == NOT_ADMITTED


def test_expired_and_revoked_admissions_are_not_admitted() -> None:
    expired = {
        "admission_id": "production:flow:1",
        "expires_at": "2026-08-01T00:00:00Z",
        "revoked_at": None,
    }
    revoked = {
        "admission_id": "production:flow:2",
        "expires_at": "2026-12-01T00:00:00Z",
        "revoked_at": "2026-08-15T00:00:00Z",
    }
    state, reason = classify_admission(expired, now=NOW)
    assert state == NOT_ADMITTED
    assert "expired" in reason
    state, reason = classify_admission(revoked, now=NOW)
    assert state == NOT_ADMITTED
    assert "revoked" in reason
    state, reason = classify_admission(None, now=NOW)
    assert state == NOT_ADMITTED


def test_live_admission_is_reported_not_minted() -> None:
    live = {
        "admission_id": "production:flow:9",
        "expires_at": "2026-09-30T00:00:00Z",
        "revoked_at": None,
        "release": {
            "artifacts": [
                {"kind": "wheel", "sha256": "sha256:" + "ab" * 32},
            ]
        },
    }
    state, reason = classify_admission(live, now=NOW)
    assert state == ADMITTED
    assert "production:flow:9" in reason
    payload = _ledger({target: live for target in EXPECTED_TARGETS})
    report = status_report(payload, now=NOW)
    assert report["product_wide_production"] is True
    assert report["flow"]["state"] == ADMITTED
    assert "wheel_digest" in report["pack"]["present"]
    assert report["pack"]["note"].startswith("This CLI reports")


def test_cli_status_on_empty_ledger_exits_1_with_check(tmp_path: Path, capsys) -> None:
    path = _write(tmp_path, _ledger({}))
    code = main(["admit", "status", "--ledger", str(path), "--check"])
    captured = capsys.readouterr()
    assert code == 1
    assert "not_actively_admitted" in captured.out
    assert "product-wide Production: no" in captured.out
    assert "does not mint" in captured.out


def test_cli_status_json_does_not_invent_an_admission(tmp_path: Path, capsys) -> None:
    path = _write(tmp_path, _ledger({}))
    code = main(["admit", "status", "--ledger", str(path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["flow"]["state"] == NOT_ADMITTED
    assert payload["flow"]["latest_admission"] is None
    assert payload["product_wide_production"] is False


def test_missing_ledger_fails_closed(capsys) -> None:
    code = main(["admit", "status", "--ledger", "/no/such/production-lifecycle.json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "admit status:" in captured.err
    assert captured.out == ""
