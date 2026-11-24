"""Encryption at rest by default, round 3 of the #2423 review: the paths
around a default encrypted run directory (export, verify, the Python API,
Cloud reporting, retention, ``--no-encrypt`` and the library recorders).

Synthetic data only; no browser, keychain or network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow import key_hierarchy as kh

TYPED = b"a synthetic typed value"


@pytest.fixture
def default_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        at_rest.OPT_OUT_ENV,
        at_rest.MODE_ENV,
        at_rest.ENCRYPT_ENV,
        crypto.ENV_KEY,
        sealed_set.RECORDING_ENV_KEY,
        kh.POLICY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kh, "MACHINE_POLICY_SOURCES", [])
    home = tmp_path / "at-rest-home"
    monkeypatch.setenv(at_rest.HOME_ENV, str(home))
    at_rest._reset_for_tests()
    yield home
    at_rest._reset_for_tests()


def _run(root: Path, files: dict[str, bytes]) -> Path:
    with sealed_set.writing(root, "run", sealed_set.passphrase_for("run")) as writer:
        for rel, data in files.items():
            writer.write(rel, data)
    assert sealed_set.read_header(root)["version"] == sealed_set.ENVELOPE_VERSION
    return root


def test_no_encrypt_lasts_for_one_command(default_mode: Path, tmp_path: Path) -> None:
    from openadapt_flow.__main__ import main

    before = {n: os.environ.get(n) for n in (at_rest.ENCRYPT_ENV, at_rest.OPT_OUT_ENV)}
    with pytest.raises(FileNotFoundError):  # the command fails ...
        main(
            [
                "compile",
                str(tmp_path / "missing"),
                "--out",
                str(tmp_path / "b"),
                "--name",
                "x",
                "--no-encrypt",
            ]
        )
    after = {n: os.environ.get(n) for n in (at_rest.ENCRYPT_ENV, at_rest.OPT_OUT_ENV)}
    assert after == before  # ... and the opt-out did not outlive it
    assert at_rest.seals_by_default() is True


def test_opted_out_restores_the_environment_on_error(default_mode: Path) -> None:
    with pytest.raises(RuntimeError):
        with at_rest.opted_out("--no-encrypt"):
            assert at_rest.opt_out_reason() is not None
            raise RuntimeError("the command failed")
    assert at_rest.opt_out_reason() is None


def test_records_json_outside_an_encrypted_run_is_refused(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow.records import run as records_run

    read = SimpleNamespace(model_dump=lambda mode="json": {"value": TYPED.decode()})
    monkeypatch.setattr(records_run, "field_reads_from_report", lambda report: [read])
    monkeypatch.setattr(records_run, "seal_field_reads", lambda reads, receipt: reads)
    receipt = tmp_path / "new-receipt-dir" / "receipt.json"
    receipt.parent.mkdir()
    with pytest.raises(at_rest.AtRestRefused, match="field values"):
        records_run.write_sealed_reads(object(), receipt)
    assert not (receipt.parent / "records.json").exists()
    # Inside the encrypted run directory, the values are sealed.
    run = _run(tmp_path / "run", {"report.json": b"{}"})
    out = records_run.write_sealed_reads(object(), run / "receipt.json")
    assert out is not None and not out.exists()  # only ciphertext on disk
    assert TYPED in sealed_set.read_bytes(out)
    # The recorded opt-out still allows plaintext, with a ledger line.
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "synthetic test data")
    assert records_run.write_sealed_reads(object(), receipt) == (
        receipt.parent / "records.json"
    )


def test_local_seal_verify_reads_an_encrypted_runs_report(
    default_mode: Path, tmp_path: Path
) -> None:
    from openadapt_flow import cli_at_rest, cli_local_seal

    run = _run(tmp_path / "run", {"report.json": b'{"success": true}'})
    assert not (run / "report.json").exists()
    assert cli_local_seal._input_report(run / "report.json") == {"success": True}
    assert "openadapt_flow.cli_local_seal:_cmd_verify" in cli_at_rest.SEALED_READY
    args = SimpleNamespace(
        seal=None,
        require_anchor=False,
        require_timestamp=False,
        authorization=None,
    )
    # The report reads; what is missing is the Seal itself.
    with pytest.raises(cli_local_seal.InputError, match="seal.json"):
        cli_local_seal._check_verify_inputs(args, run)


def test_the_python_replay_api_reads_an_encrypted_report(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    import openadapt_flow.__main__ as cli
    from openadapt_flow import api

    run_dir = tmp_path / "run"

    def fake_main(argv):
        _run(run_dir, {"report.json": b'{"replayed": true}'})
        return 0

    class Report:
        @staticmethod
        def model_validate_json(text: str) -> dict:
            return json.loads(text)

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setattr(api, "RunReport", Report)
    assert api.replay("bundle", url="http://127.0.0.1:9", run_dir=run_dir) == {
        "replayed": True
    }


def test_cloud_report_ids_live_in_the_sealed_index(
    default_mode: Path, tmp_path: Path
) -> None:
    from openadapt_flow import hosted

    run = _run(tmp_path / "run", {"report.json": b"{}"})
    first = hosted._client_run_id(run)
    assert hosted._client_run_id(run) == first  # a retry reports the same run
    opened = sealed_set.SealedSet.open(run)
    indexed = sealed_set._open_index(run, opened._header, opened._key)
    assert ".report_run_id" in indexed  # indexed, not merely tolerated
    opened.verify_all()


def test_retention_refuses_a_passphrase_check_on_a_version_3_run(
    default_mode: Path, tmp_path: Path
) -> None:
    from openadapt_flow.retention import RetentionError, plan_retention
    from tests.test_retention import POLICY

    run = _run(tmp_path / "run", {"report.json": b"{}"})
    with pytest.raises(RetentionError, match="does_not_open_version_3"):
        plan_retention(
            run,
            POLICY,
            b"{}",
            [],
            key="any passphrase at all",
            now=datetime.now(timezone.utc) + timedelta(days=1),
        )


class _Backend:
    """Enough of a backend for constructing a recorder."""

    page = None

    def screenshot(self):  # pragma: no cover - not reached
        raise AssertionError


def test_the_library_recorder_seals_by_default(
    default_mode: Path, tmp_path: Path
) -> None:
    from openadapt_flow.recorder import Recorder

    out = tmp_path / "rec"
    recorder = Recorder(_Backend(), out)
    assert recorder._sealed_writer is not None and sealed_set.is_sealed(out)


def test_an_explicit_none_recorder_is_refused_in_strict_and_ledgered_otherwise(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow.recorder import Recorder

    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused):
        Recorder(_Backend(), tmp_path / "a", encryption_key=None)
    assert not (tmp_path / "a" / "events.jsonl").exists()
    monkeypatch.delenv(at_rest.MODE_ENV)
    Recorder(_Backend(), tmp_path / "b", encryption_key=None)
    ledger = at_rest.ledger_path().read_text()
    assert str(tmp_path / "b") in ledger and "encryption_key=None" in ledger


def test_a_rollback_in_an_encrypted_run_keeps_its_marker_sealed(
    default_mode: Path, tmp_path: Path
) -> None:
    """Rollback proof in the default (encrypted) mode: the compensated marker
    and the remaining-difference file go through the run's sealed set, the
    run still opens, and resume still sees that the run was undone."""
    from openadapt_flow import rollback
    from openadapt_flow.runtime.durable.resume import was_compensated

    run = tmp_path / "run"
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    sealed_set.register_writer(writer)
    try:
        writer.write("report.json", b"{}")
        rollback._mark_compensated(run)
        rollback._write_private(
            run / rollback.ROLLBACK_DIRNAME / rollback.DIFF_FILENAME,
            {"row": TYPED.decode()},
        )
    finally:
        sealed_set.unregister_writer(writer)
    writer.finalize(adopt_external=True)
    assert not (run / rollback.COMPENSATED_MARKER).exists()  # no stray plaintext
    for path in run.rglob("*"):
        if path.is_file():
            assert TYPED not in path.read_bytes(), path
    sealed_set.SealedSet.open(run).verify_all()
    assert was_compensated(run) is True
    # A shredded run still answers (nothing needs opening).
    sealed_set.shred(run)
    assert was_compensated(run) is True
    # A run that was never compensated is not.
    other = _run(tmp_path / "other", {"report.json": b"{}"})
    assert was_compensated(other) is False


def test_a_run_cut_off_before_finalizing_resumes_into_the_same_set(
    default_mode: Path, tmp_path: Path
) -> None:
    """A durable leg killed mid-run leaves an unfinalized encrypted run. The
    next leg carries it on: files the earlier leg sealed are adopted only
    when they authenticate under the set's key at their own path."""
    run = tmp_path / "run"
    first = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    first.write("steps/a.png", TYPED)
    # The process dies here: no finalize, no index.
    with pytest.raises(sealed_set.SealedSetError, match="never finalized"):
        sealed_set.SealedSet.open(run)
    second = sealed_set.SealedSetWriter(
        run, "run", sealed_set.passphrase_for("run"), resume=True
    )
    second.write("report.json", b"{}")
    second.finalize()
    assert sealed_set.read_bytes(run / "steps" / "a.png") == TYPED
    # A file that doesn't authenticate at its path is never carried on.
    cut = tmp_path / "cut"
    w = sealed_set.SealedSetWriter(cut, "run", sealed_set.passphrase_for("run"))
    w.write("steps/a.png", TYPED)
    w.write("steps/b.png", b"other")
    (cut / "steps" / "b.png.enc").write_bytes(
        (cut / "steps" / "a.png.enc").read_bytes()
    )
    with pytest.raises(sealed_set.SealedSetError, match="does not authenticate"):
        sealed_set.SealedSetWriter(cut, "run", None, resume=True)
