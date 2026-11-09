"""Known-ID admission: a confirmed export, re-checked for age and hash."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.runtime.known_ids import (
    KnownIdAdmission,
    KnownIdAdmissionHalt,
    KnownIdListError,
    confirm_export,
    default_confirmation_path,
    file_sha256,
    load_export,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _export(tmp_path, rows=("AC50061", "ZZ1000", "ZZ1001")):
    path = tmp_path / "accounts.csv"
    path.write_text("account_id,status\n" + "".join(f"{r},open\n" for r in rows))
    return path


def test_formats(tmp_path):
    csv_path = _export(tmp_path)
    assert load_export(csv_path, "account_id") == ["AC50061", "ZZ1000", "ZZ1001"]
    with pytest.raises(KnownIdListError, match="column"):
        load_export(csv_path, None)
    with pytest.raises(KnownIdListError, match="no column"):
        load_export(csv_path, "id")
    j = tmp_path / "ids.json"
    j.write_text(json.dumps({"ids": ["A1", "B2"]}))
    assert load_export(j) == ["A1", "B2"]
    t = tmp_path / "ids.txt"
    t.write_text("A1\n\nB2\n")
    assert load_export(t) == ["A1", "B2"]


def test_confirmation_records_who_when_source_rows_hash(tmp_path):
    export = _export(tmp_path)
    rec = confirm_export(
        export,
        source="ledger system, accounts export",
        confirmed_by="operator-1",
        column="account_id",
        exported_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    assert rec.confirmed_by == "operator-1"
    assert rec.confirmed_at == "2026-10-17T12:00:00Z"
    assert rec.source == "ledger system, accounts export"
    assert rec.row_count == 3
    assert rec.sha256 == file_sha256(export)
    assert rec.exported_at == "2026-10-17T11:00:00Z"
    stored = json.loads(default_confirmation_path(export).read_text())
    assert stored["sha256"] == rec.sha256 and stored["complete"] is True
    log = default_confirmation_path(export).with_name(
        "accounts.csv.confirmation.json.log.jsonl"
    )
    assert len(log.read_text().splitlines()) == 1


def test_confirmation_needs_a_source_and_a_person(tmp_path):
    export = _export(tmp_path)
    with pytest.raises(KnownIdListError, match="source"):
        confirm_export(export, source=" ", confirmed_by="x", column="account_id")
    with pytest.raises(KnownIdListError, match="who"):
        confirm_export(export, source="s", confirmed_by="", column="account_id")


def test_unconfirmed_list_halts(tmp_path):
    with pytest.raises(KnownIdAdmissionHalt) as exc:
        KnownIdAdmission.load(_export(tmp_path), column="account_id")
    assert exc.value.reason == "known_ids_unconfirmed"


def test_confirmed_fresh_list_is_usable(tmp_path):
    export = _export(tmp_path)
    confirm_export(
        export, source="s", confirmed_by="op", column="account_id", exported_at=NOW
    )
    adm = KnownIdAdmission.load(export, now=NOW + timedelta(hours=2))
    assert adm.known_ids.complete and adm.known_ids.size == 3
    assert adm.check(NOW + timedelta(hours=2)) is None


def test_stale_list_halts(tmp_path):
    export = _export(tmp_path)
    confirm_export(
        export, source="s", confirmed_by="op", column="account_id", exported_at=NOW
    )
    with pytest.raises(KnownIdAdmissionHalt) as exc:
        KnownIdAdmission.load(export, max_age_s=3600, now=NOW + timedelta(hours=2))
    assert exc.value.reason == "known_ids_stale"
    adm = KnownIdAdmission.load(
        export, max_age_s=3 * 3600, now=NOW + timedelta(hours=2)
    )
    assert adm.check(NOW + timedelta(hours=4)).reason == "known_ids_stale"


def test_changed_export_halts_until_confirmed_again(tmp_path):
    export = _export(tmp_path)
    confirm_export(
        export, source="s", confirmed_by="op", column="account_id", exported_at=NOW
    )
    adm = KnownIdAdmission.load(export, now=NOW)
    _export(tmp_path, rows=("AC50061", "ZZ1000", "ZZ1001", "AC5OO61"))
    assert adm.check(NOW).reason == "known_ids_changed"
    with pytest.raises(KnownIdAdmissionHalt) as exc:
        KnownIdAdmission.load(export, now=NOW)
    assert exc.value.reason == "known_ids_changed"
    confirm_export(
        export, source="s", confirmed_by="op", column="account_id", exported_at=NOW
    )
    assert KnownIdAdmission.load(export, now=NOW).known_ids.size == 4


def test_cli_confirm_and_status(tmp_path, capsys):
    export = _export(tmp_path)
    args = ["known-ids", "status", str(export), "--column", "account_id"]
    assert main(args) == 1
    assert "known_ids_unconfirmed" in capsys.readouterr().out
    assert (
        main(
            [
                "known-ids",
                "confirm",
                str(export),
                "--column",
                "account_id",
                "--source",
                "ledger accounts export",
                "--by",
                "operator-1",
                "--yes",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "rows:        3" in out and '"confirmed_by": "operator-1"' in out
    assert main(args) == 0


def test_cli_confirm_requires_typed_yes(tmp_path, monkeypatch, capsys):
    export = _export(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    rc = main(
        [
            "known-ids",
            "confirm",
            str(export),
            "--column",
            "account_id",
            "--source",
            "s",
        ]
    )
    assert rc == 1
    assert not default_confirmation_path(export).exists()
