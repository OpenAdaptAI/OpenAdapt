"""Every file ``local-seal`` reads fails with one clear line and exit code 2.

A missing file, an empty one, a 404 web page saved in its place, text that
isn't JSON, and JSON with a repeated key: each input of ``verify``,
``timestamp`` and ``anchor`` is tried with each. Synthetic data only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from openadapt_flow import cli_local_seal
from openadapt_flow.offline_seal import public_key_entry, seal_run

WEB_PAGE = "<!DOCTYPE html>\n<html><head><title>404 Not Found</title></head></html>\n"
BAD = {
    "missing": None,
    "empty": "",
    "web_page": WEB_PAGE,
    "not_json": "id,value\n1,2\n",
    "repeated_key": '{"keys": [], "keys": []}',
}
EXPECT = {
    "missing": "no such file",
    "empty": "is empty",
    "web_page": "it looks like a web page, check the URL",
    "not_json": "is not JSON (it starts with 'id,value')",
    "repeated_key": "duplicate JSON key",
}


def _cli(tmp_path: Path, *argv: str) -> int:
    parser = argparse.ArgumentParser()
    cli_local_seal.register(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["local-seal", "--home", str(tmp_path / "home"), *argv])
    return args.func(args)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "OPENADAPT_SEAL_KEY",
        "OPENADAPT_SEAL_KEY_FILE",
        "OPENADAPT_NO_OUTBOUND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def setup(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text('{"run_id": "r", "execution_outcome": "VERIFIED"}')
    seal_run(run, home=tmp_path / "home", enqueue=False)
    keys = tmp_path / "site-keys.json"
    keys.write_text(json.dumps({"keys": [public_key_entry(tmp_path / "home")]}))
    return run, keys


def _put(path: Path, kind: str) -> None:
    if path.exists():
        path.unlink()
    if BAD[kind] is not None:
        path.write_text(BAD[kind])


def _one_clear_line(capsys, kind: str) -> str:
    err = capsys.readouterr().err.strip()
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1, err
    assert EXPECT[kind] in err, err
    return err


@pytest.mark.parametrize("kind", list(BAD))
def test_keys_file(tmp_path, setup, capsys, kind):
    run, keys = setup
    _put(keys, kind)
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys)) == 2
    err = _one_clear_line(capsys, kind)
    assert str(keys) in err


def test_keys_file_with_the_founders_example_message(tmp_path, setup, capsys):
    run, keys = setup
    keys.write_text(WEB_PAGE)
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys)) == 2
    err = capsys.readouterr().err.strip()
    assert err == (
        f"{keys} is not JSON (it starts with '<!DOCTYPE html>': it looks like a web "
        "page, check the URL)"
    )


def test_keys_json_that_is_not_a_registry(tmp_path, setup, capsys):
    run, keys = setup
    keys.write_text('{"keys": [{"key_id": "x"}]}')
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys)) == 2
    err = capsys.readouterr().err.strip()
    assert "is not a key registry" in err and "Traceback" not in err


@pytest.mark.parametrize("kind", list(BAD))
@pytest.mark.parametrize("name", ["report.json", "seal.json"])
def test_run_directory_files(tmp_path, setup, capsys, kind, name):
    run, keys = setup
    _put(run / name, kind)
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys)) == 2
    _one_clear_line(capsys, kind)


@pytest.mark.parametrize("kind", list(BAD))
def test_report_and_seal_given_as_files(tmp_path, setup, capsys, kind):
    run, keys = setup
    report, seal = tmp_path / "report.json", tmp_path / "seal.json"
    report.write_bytes((run / "report.json").read_bytes())
    seal.write_bytes((run / "seal.json").read_bytes())
    _put(report, kind)
    assert (
        _cli(tmp_path, "verify", str(report), "--seal", str(seal), "--keys", str(keys))
        == 2
    )
    _one_clear_line(capsys, kind)
    report.write_bytes((run / "report.json").read_bytes())
    _put(seal, kind)
    assert (
        _cli(tmp_path, "verify", str(report), "--seal", str(seal), "--keys", str(keys))
        == 2
    )
    _one_clear_line(capsys, kind)


@pytest.mark.parametrize("kind", ["empty", "web_page", "not_json", "repeated_key"])
@pytest.mark.parametrize("name", ["seal.anchor.json", "seal.tsa.json"])
def test_broken_anchor_or_timestamp_file(tmp_path, setup, capsys, kind, name):
    run, keys = setup
    _put(run / name, kind)
    flag = "--require-anchor" if "anchor" in name else "--require-timestamp"
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys), flag) == 2
    _one_clear_line(capsys, kind)


def test_missing_anchor_is_still_a_verdict(tmp_path, setup, capsys):
    run, keys = setup
    assert (
        _cli(tmp_path, "verify", str(run), "--keys", str(keys), "--require-anchor") == 1
    )
    assert "anchor_missing" in capsys.readouterr().err


@pytest.mark.parametrize("kind", list(BAD))
def test_authorization_statement(tmp_path, setup, capsys, kind):
    run, keys = setup
    auth = tmp_path / "auth"
    auth.mkdir()
    _put(auth / "run-authorization.json", kind)
    code = _cli(
        tmp_path, "verify", str(run), "--keys", str(keys), "--authorization", str(auth)
    )
    assert code == 2
    _one_clear_line(capsys, kind)


@pytest.mark.parametrize("flag", ["--log-key", "--tsa-cert"])
@pytest.mark.parametrize("kind", ["missing", "empty", "web_page"])
def test_pinned_key_and_certificate_files(tmp_path, setup, capsys, flag, kind):
    run, keys = setup
    pem = tmp_path / "pinned.pem"
    _put(pem, kind)
    assert _cli(tmp_path, "verify", str(run), "--keys", str(keys), flag, str(pem)) == 2
    _one_clear_line(capsys, kind)


@pytest.mark.parametrize("kind", list(BAD))
def test_timestamp_and_anchor_targets(tmp_path, setup, capsys, kind):
    run, _ = setup
    _put(run / "seal.json", kind)
    assert _cli(tmp_path, "timestamp", str(run)) == 2
    _one_clear_line(capsys, kind)
    assert _cli(tmp_path, "anchor", str(run)) == 2
    _one_clear_line(capsys, kind)


def test_unsigned_authorization_cannot_be_timestamped(tmp_path, capsys):
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "run-authorization.json").write_text("{}")
    assert _cli(tmp_path, "timestamp", str(auth)) == 2
    err = capsys.readouterr().err.strip()
    assert "run-authorization.json.sig: no such file" in err


@pytest.mark.parametrize("command", ["verify", "anchor"])
@pytest.mark.parametrize("kind", list(BAD))
def test_authorize_commands_read_the_statement_carefully(
    tmp_path, capsys, command, kind
):
    auth = tmp_path / "auth"
    auth.mkdir()
    _put(auth / "run-authorization.json", kind)
    assert _cli(tmp_path, "authorize", command, str(auth)) == 2
    _one_clear_line(capsys, kind)
