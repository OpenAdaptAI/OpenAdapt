"""Offline Seals: signed locally, verified against pinned keys, fail closed."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_flow import offline_seal
from openadapt_types.seal import parse_key_registry
from openadapt_flow.offline_seal import (
    LocalSealError,
    public_key_entry,
    seal_after_run,
    seal_run,
    verify_run,
)

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


def _run(tmp_path: Path, outcome: str = "VERIFIED", run_id: str = "run-1") -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"run_id": run_id, "execution_outcome": outcome, "steps": []}),
        encoding="utf-8",
    )
    return run


def _pinned(home: Path) -> list:
    return parse_key_registry([public_key_entry(home)])


def test_seal_run_writes_a_verifiable_seal(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal = seal_run(run, home=home, now=NOW)
    on_disk = json.loads((run / "seal.json").read_text())
    assert on_disk["schema"] == "openadapt.local-seal/v1"
    assert on_disk["issuer"] == "local"
    assert on_disk["outcome"] == "VERIFIED"
    assert on_disk["run_id"] == "run-1"
    assert on_disk["issued_at"] == "2026-10-17T12:00:00Z"
    assert seal.subject_kind == "run-report"
    result = verify_run(run, _pinned(home))
    assert result.ok and result.subject_file == "report.json"
    # The signing key is private to the operator.
    assert (home / "keys" / "ed25519.pem").stat().st_mode & 0o777 == 0o600
    # Queued for an optional batch.
    assert len(list((home / "batch" / "pending").glob("*.json"))) == 1


def test_outcome_is_copied_never_upgraded(tmp_path):
    run = _run(tmp_path, outcome="HALTED")
    seal = seal_run(run, home=tmp_path / "home", now=NOW)
    assert seal.outcome == "HALTED"


def test_tampered_report_is_refused(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path, outcome="HALTED")
    seal_run(run, home=home, now=NOW)
    (run / "report.json").write_text(
        json.dumps({"run_id": "run-1", "execution_outcome": "VERIFIED", "steps": []})
    )
    assert verify_run(run, _pinned(home)).reason == "subject_mismatch"


def test_resealing_changed_bytes_is_refused_not_overwritten(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    first = seal_run(run, home=home, now=NOW)
    assert seal_run(run, home=home, now=NOW) == first  # idempotent
    (run / "report.json").write_text("{}")
    with pytest.raises(LocalSealError) as exc:
        seal_run(run, home=home, now=NOW)
    assert exc.value.code == "already_sealed"


def test_unknown_key_is_refused(tmp_path):
    run = _run(tmp_path)
    seal_run(run, home=tmp_path / "a", now=NOW)
    other = _pinned(tmp_path / "b")
    assert verify_run(run, other).reason == "unknown_key"


def test_embedded_public_key_is_not_trusted(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal_run(run, home=home, now=NOW)
    seal = json.loads((run / "seal.json").read_text())
    seal["public_key"] = base64.b64encode(b"\x01" * 32).decode()
    (run / "seal.json").write_text(json.dumps(seal))
    assert verify_run(run, _pinned(home)).reason == "key_mismatch"


def test_bad_signature_is_refused(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal_run(run, home=home, now=NOW)
    seal = json.loads((run / "seal.json").read_text())
    seal["run_id"] = "run-2"
    (run / "seal.json").write_text(json.dumps(seal))
    assert verify_run(run, _pinned(home)).reason == "bad_signature"


def test_revoked_key_refuses_seals_issued_after_revocation(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal_run(run, home=home, now=NOW)
    entry = dict(
        public_key_entry(home), status="revoked", revoked_at="2026-10-01T00:00:00Z"
    )
    assert verify_run(run, parse_key_registry([entry])).reason == "revoked_key"
    entry["revoked_at"] = "2026-11-01T00:00:00Z"
    assert verify_run(run, parse_key_registry([entry])).ok


def test_malformed_and_missing_seal(tmp_path):
    home = tmp_path / "home"
    run = _run(tmp_path)
    assert verify_run(run, _pinned(home)).reason == "unsigned"
    (run / "seal.json").write_text(json.dumps({"schema": "openadapt.local-seal/v1"}))
    assert verify_run(run, _pinned(home)).reason == "malformed"


def test_cloud_receipt_attached_later_still_verifies(tmp_path):
    """attach-seal keeps report.pre-seal.json; the offline Seal covers that."""
    from openadapt_flow.cloud_seal import attach_receipt

    home = tmp_path / "home"
    run = _run(tmp_path)
    seal_run(run, home=home, now=NOW)
    attach_receipt(run, {"execution_id": "exe_1", "outcome": "verified"}, "exe_1")
    result = verify_run(run, _pinned(home))
    assert result.ok and result.subject_file == "report.pre-seal.json"


def test_cli_hook_is_default_on_and_can_be_turned_off(tmp_path):
    run = _run(tmp_path)
    env = {"OPENADAPT_SEAL_HOME": str(tmp_path / "home"), "OPENADAPT_LOCAL_SEAL": "off"}
    assert seal_after_run(run, env) is None
    assert not (run / "seal.json").exists()
    env.pop("OPENADAPT_LOCAL_SEAL")
    assert "offline seal:" in (seal_after_run(run, env) or "")
    assert (run / "seal.json").exists()
    # A run that wrote no report gets no Seal and no error.
    assert seal_after_run(tmp_path / "missing", env) is None


def test_cli_hook_never_raises(tmp_path):
    run = _run(tmp_path)
    (run / "report.json").write_text("not json")
    msg = seal_after_run(run, {"OPENADAPT_SEAL_HOME": str(tmp_path / "home")})
    assert msg == "offline seal not written: report_unreadable"


def test_sealing_makes_no_network_calls(tmp_path, monkeypatch):
    import socket

    def _boom(*a, **k):  # pragma: no cover - only hit on a regression
        raise AssertionError("offline sealing touched the network")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal_run(run, home=home, now=NOW)
    assert verify_run(run, _pinned(home)).ok


def test_local_seal_cli_round_trip(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    home = tmp_path / "home"
    run = _run(tmp_path)
    assert main(["local-seal", "--home", str(home), "run", str(run)]) == 0
    capsys.readouterr()
    assert main(["local-seal", "--home", str(home), "key"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["keys"][0]["status"] == "active"
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(printed))
    assert (
        main(
            ["local-seal", "--home", str(home), "verify", str(run), "--keys", str(keys)]
        )
        == 0
    )
    record = tmp_path / "record.csv"
    record.write_text("id,value\nR-1,42\n")
    assert (
        main(
            ["local-seal", "--home", str(home), "file", str(record), "--kind", "record"]
        )
        == 0
    )
    sealfile = Path(str(record) + ".seal.json")
    assert (
        main(
            [
                "local-seal",
                "--home",
                str(home),
                "verify",
                str(record),
                "--seal",
                str(sealfile),
                "--keys",
                str(keys),
            ]
        )
        == 0
    )
    record.write_text("id,value\nR-1,43\n")
    assert (
        main(
            [
                "local-seal",
                "--home",
                str(home),
                "verify",
                str(record),
                "--seal",
                str(sealfile),
                "--keys",
                str(keys),
            ]
        )
        == 1
    )


def test_offline_seal_module_has_no_network_imports():
    src = Path(offline_seal.__file__).read_text()
    for name in ("httpx", "requests", "urllib", "socket"):
        assert f"import {name}" not in src


def test_replay_and_run_seal_by_default_without_changing_the_exit_code(
    tmp_path, monkeypatch
):
    import openadapt_flow.__main__ as cli

    def fake(args):
        Path(args.run_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.run_dir) / "report.json").write_text(
            json.dumps({"run_id": "r", "execution_outcome": "HALTED"})
        )
        return 2

    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENADAPT_LOCAL_SEAL", raising=False)
    for name, command in (("_cmd_replay", "replay"), ("_cmd_run", "run")):
        monkeypatch.setattr(cli, name, fake)
        run = tmp_path / command
        assert cli.main([command, str(tmp_path / "bundle"), "--run-dir", str(run)]) == 2
        seal = json.loads((run / "seal.json").read_text())
        assert seal["outcome"] == "HALTED"
