"""Encryption at rest by default, the recorded opt-out, and strict mode.

The rest of the suite runs under the plaintext opt-out (tests/conftest.py).
These tests remove it to check the default and ``OPENADAPT_FLOW_AT_REST=strict``.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow.__main__ import _recording_encryption_key, main
from openadapt_flow.ir import Workflow
from tests.test_sealed_at_rest import (
    PNG_SIGNATURE,
    _assert_ciphertext_only,
    _recording,
    watching_disk,
)


@pytest.fixture
def default_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        at_rest.OPT_OUT_ENV,
        at_rest.MODE_ENV,
        crypto.ENV_KEY,
        sealed_set.RECORDING_ENV_KEY,
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "at-rest-home"
    monkeypatch.setenv(at_rest.HOME_ENV, str(home))
    at_rest._reset_for_tests()
    yield home
    at_rest._reset_for_tests()


def _ledger(home: Path) -> list[dict]:
    path = home / at_rest.LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# -- the local key -------------------------------------------------------------


def test_local_key_is_created_private_and_reused(default_mode: Path) -> None:
    first = crypto.resolve_key(None)
    path = default_mode / "keys" / at_rest.KEY_NAME
    assert first and path.read_text().strip() == first
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert crypto.resolve_key(None) == first
    assert not [p for p in path.parent.iterdir() if p.name != at_rest.KEY_NAME]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_key_others_can_read_is_refused(default_mode: Path) -> None:
    crypto.resolve_key(None)
    path = default_mode / "keys" / at_rest.KEY_NAME
    path.chmod(0o644)
    with pytest.raises(at_rest.LocalKeyError, match="chmod 600"):
        crypto.resolve_key(None)


def test_a_malformed_key_is_refused(default_mode: Path) -> None:
    path = default_mode / "keys" / at_rest.KEY_NAME
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("short\n")
    path.chmod(0o600)
    with pytest.raises(at_rest.LocalKeyError):
        crypto.resolve_key(None)


def test_a_configured_key_wins(default_mode: Path, monkeypatch) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, "deployment-key")
    assert crypto.resolve_key(None) == "deployment-key"
    assert not (default_mode / "keys").exists()


# -- the opt-out and strict mode -----------------------------------------------


def test_opt_out_restores_plaintext_loudly_and_records_it(
    default_mode: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "de-identified fixtures")
    assert crypto.resolve_key(None) is None
    assert at_rest.plaintext_allowed("a compiled bundle")
    assert at_rest.plaintext_allowed("a compiled bundle")  # warns once
    err = capsys.readouterr().err
    assert err.count("WARNING: writing a compiled bundle in plaintext") == 1
    [line] = [e for e in _ledger(default_mode) if e["purpose"] == "a compiled bundle"]
    assert line["reason"] == "de-identified fixtures"


def test_strict_mode_ignores_the_opt_out(
    default_mode: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "dev")
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    assert crypto.resolve_key(None)
    assert not at_rest.plaintext_allowed("a compiled bundle")
    assert "ignored" in capsys.readouterr().err


def test_an_unknown_mode_stops_the_cli(
    default_mode: Path, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(at_rest.MODE_ENV, "stricter")
    with pytest.raises(ValueError):
        at_rest.strict()
    assert main(["explain", str(tmp_path / "missing")]) == 2


def test_unsealed_write_names_the_path_or_refuses(
    default_mode: Path, monkeypatch, capsys
) -> None:
    at_rest.unsealed_write("a run directory", "/data/run-7")
    assert "/data/run-7" in capsys.readouterr().err
    assert _ledger(default_mode)[0]["path"] == "/data/run-7"
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused, match="strict"):
        at_rest.unsealed_write("a run directory", "/data/run-8")
    assert len(_ledger(default_mode)) == 1


# -- recordings and bundles -------------------------------------------------------


def test_a_plaintext_bundle_is_named_by_default(
    default_mode: Path, tmp_path: Path, capsys
) -> None:
    rec = _recording(tmp_path / "rec", key=None)
    assert main(["compile", str(rec), "--out", str(tmp_path / "b"), "--name", "x"]) == 0
    assert (tmp_path / "b" / "workflow.json").is_file()
    assert str(tmp_path / "b") in capsys.readouterr().err
    assert _ledger(default_mode)[0]["path"] == str(tmp_path / "b")


def test_strict_mode_compiles_a_plaintext_recording_to_a_sealed_bundle(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    rec = _recording(tmp_path / "rec", key=None)  # made before strict mode
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    assert main(["compile", str(rec), "--out", str(tmp_path / "b"), "--name", "x"]) == 0
    _assert_ciphertext_only(tmp_path / "b")
    assert Workflow.load(tmp_path / "b").encrypted
    assert sealed_set.SealedSet.open(tmp_path / "b", crypto.resolve_key(None))
    assert _ledger(default_mode) == []


def test_compile_under_the_opt_out_stays_plaintext(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "fixtures")
    rec = _recording(tmp_path / "rec", key=None)
    assert main(["compile", str(rec), "--out", str(tmp_path / "b"), "--name", "x"]) == 0
    assert (tmp_path / "b" / "workflow.json").is_file()
    assert any(e["purpose"] == "a compiled bundle" for e in _ledger(default_mode))


def test_a_recording_only_key_seals_bundles_under_that_key(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(sealed_set.RECORDING_ENV_KEY, "recording-only-key")
    rec = _recording(tmp_path / "rec", key="recording-only-key")
    assert main(["compile", str(rec), "--out", str(tmp_path / "b"), "--name", "x"]) == 0
    assert sealed_set.SealedSet.open(tmp_path / "b", "recording-only-key")


def test_strict_induce_seals_and_refuses_a_plaintext_report(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    first = _recording(tmp_path / "rec1", key=None)
    second = _recording(tmp_path / "rec2", key=None)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    report = tmp_path / "induce.json"
    refused = main(
        [
            "induce",
            str(first),
            str(second),
            "--out",
            str(tmp_path / "x"),
            "--report",
            str(report),
        ]
    )
    assert refused == 2 and not report.exists()
    assert main(["induce", str(first), str(second), "--out", str(tmp_path / "i")]) == 0
    _assert_ciphertext_only(tmp_path / "i")


def test_a_key_from_another_deployment_gets_a_clear_message(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    rec = _recording(tmp_path / "rec", key="deployment-key")
    with pytest.raises(sealed_set.SealedSetKeyError, match="OPENADAPT_RECORDING_KEY"):
        sealed_set.open_set(rec)


def _record_args(out: Path, *, no_encrypt: bool = False) -> argparse.Namespace:
    return argparse.Namespace(out=str(out), no_encrypt=no_encrypt)


def test_web_recordings_are_named_by_default_and_sealed_in_strict_mode(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    assert _recording_encryption_key(_record_args(tmp_path / "r"), "web") is None
    assert _ledger(default_mode)[0]["path"] == str(tmp_path / "r")
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    assert _recording_encryption_key(_record_args(tmp_path / "r2"), "web") == (
        crypto.resolve_key(None)
    )
    monkeypatch.delenv(at_rest.MODE_ENV)
    monkeypatch.setenv(crypto.ENV_KEY, "deployment-key")
    assert _recording_encryption_key(_record_args(tmp_path / "r3"), "web") == (
        "deployment-key"
    )


def test_no_encrypt_is_recorded_and_strict_refuses_it(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    assert (
        _recording_encryption_key(_record_args(tmp_path / "r", no_encrypt=True), "web")
        is None
    )
    assert "record --no-encrypt" in _ledger(default_mode)[0]["reason"]
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(SystemExit, match="strict"):
        _recording_encryption_key(_record_args(tmp_path / "r2", no_encrypt=True), "web")


def test_desktop_recorders_work_by_default_and_refuse_in_strict_mode(
    default_mode: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    out = tmp_path / "desk"
    assert _recording_encryption_key(_record_args(out), "windows") is None
    assert str(out) in capsys.readouterr().err
    assert _ledger(default_mode)[0]["path"] == str(out)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(SystemExit, match="strict"):
        _recording_encryption_key(_record_args(tmp_path / "desk2"), "windows")


def test_commands_that_cannot_seal_their_output_refuse_in_strict_mode(
    default_mode: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    assert main(["explain", str(tmp_path / "missing")]) == 2
    assert "strict" in capsys.readouterr().err


# -- run directories -----------------------------------------------------------


def _mockmed_run(tmp_path: Path, bundle: Path, run_dir: Path, **kw):
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_after_stop_mockmed import MockMedBackend, _vision

    url, _db, stop = serve()
    url = url.rstrip("/")
    try:
        return Replayer(
            MockMedBackend(url),
            vision=_vision(),
            effect_verifier=RestRecordVerifier(
                url, records_path="/api/db", records_key="records"
            ),
            poll_interval_s=0.01,
        ).run(Workflow.load(bundle), bundle_dir=bundle, run_dir=run_dir, **kw)
    finally:
        stop()


def test_a_run_directory_is_plaintext_by_default_and_named(
    default_mode: Path, tmp_path: Path, capsys
) -> None:
    from tests.test_after_stop_mockmed import _workflow

    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    run_dir = tmp_path / "run"
    _mockmed_run(tmp_path, bundle, run_dir)
    assert (run_dir / "report.json").is_file()
    assert str(run_dir) in capsys.readouterr().err
    assert any(e.get("path") == str(run_dir) for e in _ledger(default_mode))


def test_strict_mode_seals_the_run_directory_of_a_plaintext_bundle(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from tests.test_after_stop_mockmed import _workflow

    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    bundle = tmp_path / "bundle"
    with monkeypatch.context() as m:
        m.setenv(at_rest.OPT_OUT_ENV, "")
        _workflow().save(bundle)
    run_dir = tmp_path / "run"
    with watching_disk() as watch:
        _mockmed_run(tmp_path, bundle, run_dir)
    assert sealed_set.is_sealed(run_dir)
    _assert_ciphertext_only(run_dir)
    assert watch.leaks([b'"execution_outcome"', PNG_SIGNATURE]) == []
    assert _ledger(default_mode) == []


# -- review fixes (#1755) --------------------------------------------------------


def test_a_sealed_bundle_runs_sealed_in_every_mode(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    """Encrypted input never yields a plaintext run directory, even under the
    opt-out Desktop uses."""
    from tests.test_after_stop_mockmed import _workflow

    bundle = tmp_path / "bundle"
    with sealed_set.writing(bundle, "bundle", crypto.resolve_key(None)):
        _workflow().save(bundle)
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "desktop")
    monkeypatch.setenv(at_rest.HOME_ENV, str(default_mode))
    # The bundle opens with the local key, which the opt-out leaves unread.
    monkeypatch.setenv(crypto.ENV_KEY, at_rest.local_key())
    run_dir = tmp_path / "run"
    with watching_disk() as watch:
        _mockmed_run(tmp_path, bundle, run_dir)
    assert sealed_set.is_sealed(run_dir)
    _assert_ciphertext_only(run_dir)
    assert watch.leaks([b'"execution_outcome"', PNG_SIGNATURE]) == []


def test_a_healed_copy_of_a_sealed_run_is_held_back(
    default_mode: Path, tmp_path: Path
) -> None:
    from tests.test_after_stop_mockmed import _workflow

    bundle = tmp_path / "bundle"
    with sealed_set.writing(bundle, "bundle", crypto.resolve_key(None)):
        _workflow().save(bundle)
    with pytest.raises(at_rest.AtRestRefused, match="healed bundle"):
        _mockmed_run(
            tmp_path, bundle, tmp_path / "run", save_healed_to=tmp_path / "healed"
        )
    assert not (tmp_path / "healed").exists()
    assert not (tmp_path / "run").exists()


def test_default_mode_checkpoints_are_encrypted_on_disk(
    default_mode: Path, tmp_path: Path
) -> None:
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_after_stop_mockmed import MockMedBackend, _vision, _workflow

    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    run_dir = tmp_path / "run"
    replayer = Replayer(MockMedBackend("http://127.0.0.1:9"), vision=_vision())
    assert replayer.checkpoint_key is None
    try:
        replayer.run(Workflow.load(bundle), bundle_dir=bundle, run_dir=run_dir)
    except Exception:  # noqa: BLE001 - no app is listening; the key is what counts
        pass
    assert replayer.checkpoint_key == at_rest.local_key()
    store = CheckpointStore(run_dir, key=replayer.checkpoint_key)
    store._write_model(run_dir / "checkpoints" / "probe.json", Workflow(name="x"))
    on_disk = sorted(p.name for p in (run_dir / "checkpoints").iterdir())
    assert on_disk == ["probe.json.enc"]
    assert b"probe" not in (run_dir / "checkpoints" / "probe.json.enc").read_bytes()


def test_a_recording_only_key_reads_back_from_the_environment(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(sealed_set.RECORDING_ENV_KEY, "recording-only-key")
    rec = _recording(tmp_path / "rec", key="recording-only-key")
    assert main(["compile", str(rec), "--out", str(tmp_path / "b"), "--name", "x"]) == 0
    assert Workflow.load(tmp_path / "b").name == "x"


def test_strict_mode_runs_only_listed_commands(default_mode: Path, monkeypatch) -> None:
    from openadapt_flow import __main__ as cli
    from openadapt_flow.cli_service import _cmd_health
    from openadapt_flow.cli_shadow import cmd_shadow

    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused, match="strict"):
        cli._unsealed_output_check(argparse.Namespace(func=cmd_shadow, out="o"))
    cli._unsealed_output_check(argparse.Namespace(func=_cmd_health))
    with pytest.raises(at_rest.AtRestRefused, match="record-video"):
        cli._unsealed_output_check(
            argparse.Namespace(func=cli._cmd_replay, record_video="video")
        )


def test_default_mode_logs_the_newly_listed_writers(default_mode: Path, capsys) -> None:
    from openadapt_flow import __main__ as cli
    from openadapt_flow.cli_shadow import cmd_shadow

    cli._unsealed_output_check(argparse.Namespace(func=cmd_shadow, out="shadow-out"))
    cli._unsealed_output_check(
        argparse.Namespace(func=cli._cmd_replay, record_video="video-out")
    )
    paths = {e["path"] for e in _ledger(default_mode)}
    assert {"shadow-out", "video-out"} <= paths


def test_strict_holds_back_library_reports_before_they_start(
    default_mode: Path, monkeypatch
) -> None:
    from openadapt_flow.runtime.composition import execute_composition

    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused, match="Nothing was executed"):
        execute_composition(
            None,  # type: ignore[arg-type]
            parent_dir="p",
            run_dir="r",
            child_run=None,  # type: ignore[arg-type]
        )


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX perms")
def test_an_unwritable_ledger_stops_the_write(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / at_rest.LEDGER_NAME).mkdir()  # a directory where the file goes
    monkeypatch.setenv(at_rest.HOME_ENV, str(locked))
    with pytest.raises(at_rest.AtRestRefused, match="ledger"):
        at_rest.unsealed_write("a run directory", "/data/run-9")


def test_seal_requires_the_deployment_key(default_mode: Path, tmp_path: Path) -> None:
    from tests.test_sealed_at_rest import _plain_bundle

    code = main(["seal", str(_plain_bundle(tmp_path)), "--out", str(tmp_path / "s")])
    assert code != 0 and not (tmp_path / "s").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_an_open_keys_directory_is_refused(default_mode: Path) -> None:
    crypto.resolve_key(None)
    (default_mode / "keys").chmod(0o777)
    try:
        with pytest.raises(at_rest.LocalKeyError, match="chmod 700"):
            crypto.resolve_key(None)
    finally:
        (default_mode / "keys").chmod(0o700)


# -- review round 2 (#1755) -----------------------------------------------------


def test_strict_runs_other_sealed_ready_commands_on_encrypted_input_only(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli
    from tests.test_after_stop_mockmed import _workflow

    plain = tmp_path / "plain-bundle"
    _workflow().save(plain)
    sealed = tmp_path / "sealed-bundle"
    with sealed_set.writing(sealed, "bundle", crypto.resolve_key(None)):
        _workflow().save(sealed)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    for func in (cli._cmd_scaffold_verifier, cli._cmd_certify):
        with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
            cli._unsealed_output_check(argparse.Namespace(func=func, bundle=str(plain)))
        cli._unsealed_output_check(argparse.Namespace(func=func, bundle=str(sealed)))
    # The commands that seal plaintext input themselves still take it.
    cli._unsealed_output_check(
        argparse.Namespace(func=cli._cmd_compile, recording=str(plain))
    )


def test_strict_holds_back_qualification_trials(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli

    sealed = tmp_path / "sealed-bundle"
    from tests.test_after_stop_mockmed import _workflow

    with sealed_set.writing(sealed, "bundle", crypto.resolve_key(None)):
        _workflow().save(sealed)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    for verb in ("run-case", "drills"):
        with pytest.raises(at_rest.AtRestRefused, match="qualification trials"):
            cli._unsealed_output_check(
                argparse.Namespace(
                    func=cli._cmd_qualify, qualify_cmd=verb, bundle=str(sealed)
                )
            )
    with pytest.raises(at_rest.AtRestRefused, match="strict"):
        cli._unsealed_output_check(
            argparse.Namespace(func=cli._cmd_qualify_campaign, bundle=str(sealed))
        )


def test_a_local_key_bundle_under_the_opt_out_refuses_to_run(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from tests.test_after_stop_mockmed import _workflow

    bundle = tmp_path / "bundle"
    with sealed_set.writing(bundle, "bundle", crypto.resolve_key(None)):
        _workflow().save(bundle)
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "desktop")
    with pytest.raises(sealed_set.SealedSetError):
        _mockmed_run(tmp_path, bundle, tmp_path / "run")
    assert not (tmp_path / "run").exists()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX perms")
def test_a_report_after_records_changed_survives_a_ledger_failure(
    default_mode: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / at_rest.LEDGER_NAME).mkdir()
    monkeypatch.setenv(at_rest.HOME_ENV, str(locked))
    at_rest.unsealed_write("a worklist report", "/data/pool", refuse=False)
    assert "ERROR" in capsys.readouterr().err


def test_read_paths_create_no_key_and_no_ledger_line(
    default_mode: Path, monkeypatch
) -> None:
    assert crypto.resolve_read_key(None) is None
    assert not (default_mode / "keys").exists()
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "desktop")
    assert crypto.resolve_read_key(None) is None
    assert _ledger(default_mode) == []
    monkeypatch.delenv(at_rest.OPT_OUT_ENV)
    key = crypto.resolve_key(None)
    assert crypto.resolve_read_key(None) == key


# -- review round 3 follow-ups --------------------------------------------------


def _sealed_and_plain(tmp_path: Path) -> tuple[Path, Path]:
    from tests.test_after_stop_mockmed import _workflow

    plain = tmp_path / "plain-bundle"
    _workflow().save(plain)
    sealed = tmp_path / "sealed-bundle"
    with sealed_set.writing(sealed, "bundle", crypto.resolve_key(None)):
        _workflow().save(sealed)
    return sealed, plain


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_strict_judges_a_symlink_inside_a_set_by_its_target(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli

    sealed, plain = _sealed_and_plain(tmp_path)
    link = sealed / "evil"
    link.symlink_to(plain, target_is_directory=True)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
        cli._unsealed_output_check(
            argparse.Namespace(func=cli._cmd_certify, bundle=str(link))
        )


def test_strict_treats_a_plaintext_copy_inside_a_set_as_plaintext(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    import shutil

    from openadapt_flow import __main__ as cli

    sealed, plain = _sealed_and_plain(tmp_path)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    cli._unsealed_output_check(
        argparse.Namespace(func=cli._cmd_certify, bundle=str(sealed))
    )
    sealed_set._OPENED.clear()  # a new process: the set is opened afresh
    nested = sealed / "nested"
    shutil.copytree(plain, nested)
    for target in (nested, sealed):  # planted plaintext taints the whole set
        with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
            cli._unsealed_output_check(
                argparse.Namespace(func=cli._cmd_certify, bundle=str(target))
            )


def test_strict_refuses_plaintext_crops_and_broken_links_inside_a_set(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli

    sealed, _plain = _sealed_and_plain(tmp_path)
    crops = sealed / "t" / "templates"
    crops.mkdir(parents=True)
    (crops / "x.png").write_bytes(b"plaintext crop")
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    monkeypatch.setattr(crypto, "resolve_read_key", lambda key: None)  # no key
    with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
        cli._unsealed_output_check(
            argparse.Namespace(func=cli._cmd_certify, bundle=str(sealed / "t"))
        )
    if os.name == "posix":
        broken = tmp_path / "broken"
        broken.symlink_to(tmp_path / "missing", target_is_directory=True)
        with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
            cli._unsealed_output_check(
                argparse.Namespace(func=cli._cmd_certify, bundle=str(broken))
            )


def test_strict_seals_plaintext_input_with_seal(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli

    _sealed, plain = _sealed_and_plain(tmp_path)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    cli._unsealed_output_check(
        argparse.Namespace(func=cli._cmd_seal, source=str(plain), out="out")
    )


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX perms")
def test_the_kept_report_message_says_what_happened(
    default_mode: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / at_rest.LEDGER_NAME).mkdir()
    monkeypatch.setenv(at_rest.HOME_ENV, str(locked))
    at_rest.unsealed_write("a worklist report", "/data/pool", refuse=False)
    err = capsys.readouterr().err
    assert "is kept in plaintext" in err and "Nothing was written" not in err


# -- review round 3 (#1815): no string skipped by its spelling --------------------


@pytest.mark.skipif(os.name != "posix", reason="newline and ':' in file names")
@pytest.mark.parametrize("spelling", ["new\nline", "url://shaped"])
def test_strict_refuses_plaintext_at_a_newline_or_url_shaped_path(
    default_mode: Path, tmp_path: Path, monkeypatch, spelling: str
) -> None:
    from openadapt_flow import __main__ as cli
    from tests.test_after_stop_mockmed import _workflow

    plain = tmp_path / spelling
    _workflow().save(plain)
    text = f"{tmp_path}/{spelling}"
    assert Path(text).is_dir() and (Path(text) / "workflow.json").is_file()
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    for func in (cli._cmd_scaffold_verifier, cli._cmd_certify):
        with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
            cli._unsealed_output_check(argparse.Namespace(func=func, bundle=text))


def test_strict_takes_text_that_names_no_path(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    # Controls: a URL, multi-line text and an over-long string name nothing
    # on disk, so they never refuse a run; a sealed bundle is still taken.
    from openadapt_flow import __main__ as cli

    sealed, _plain = _sealed_and_plain(tmp_path)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    for text in ("http://127.0.0.1:8080", "a\nb", "x" * 5000, str(sealed)):
        cli._unsealed_output_check(
            argparse.Namespace(func=cli._cmd_certify, bundle=str(sealed), note=text)
        )


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX permissions")
def test_strict_refuses_a_path_it_cannot_check(
    default_mode: Path, tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow import __main__ as cli

    sealed, _plain = _sealed_and_plain(tmp_path)
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    try:
        with pytest.raises(at_rest.AtRestRefused, match="not encrypted"):
            cli._unsealed_output_check(
                argparse.Namespace(
                    func=cli._cmd_certify,
                    bundle=str(sealed),
                    other=str(locked / "inner"),
                )
            )
    finally:
        locked.chmod(0o700)
