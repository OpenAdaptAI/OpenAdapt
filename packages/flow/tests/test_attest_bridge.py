"""Unit tests for the opt-in openadapt-attest bridge.

``openadapt_attest`` is a SEPARATE, privately distributed package that flow's
public CI can never install. Every test here injects a FAKE
``openadapt_attest`` (and its ``flow_run`` / ``check`` submodules) into
``sys.modules`` — or actively blocks the import — so the suite exercises the
whole coupling surface without the real sidecar:

* no configured contract -> every hook is a silent no-op;
* configured contract + installed sidecar -> ``check_flow_run`` is called
  with the exact frozen-interface arguments and a compact summary prints;
* sidecar not installed -> exactly one notice line, no exception;
* sidecar raising -> a warning prints and the exit-code mapping of
  ``_finish_replay`` is untouched (WRAP-not-rewrite);
* environment fallbacks work and an explicit CLI flag wins;
* the pre-actuation snapshot writes ``attest_pre_state.json`` and an explicit
  ``--attest-pre-state`` suppresses the capture.
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.attest_bridge import (
    AUDIT_LOG_ENV,
    CONTRACT_ENV,
    PRE_STATE_ENV,
    PRE_STATE_FILENAME,
    RECEIPT_FILENAME,
    SIGN_KEY_ENV,
    maybe_attest_run,
    maybe_capture_pre_state,
    resolve_attest_config,
)

_ATTEST_ENVS = (CONTRACT_ENV, SIGN_KEY_ENV, AUDIT_LOG_ENV, PRE_STATE_ENV)


@pytest.fixture(autouse=True)
def _clear_attest_env(monkeypatch):
    """No ambient attest (or hosted-reporting) opt-in can leak into any test."""
    for name in (
        *_ATTEST_ENVS,
        "OPENADAPT_FLOW_HOSTED_WORKFLOW_ID",
        "OPENADAPT_FLOW_REPORT_RUN",
    ):
        monkeypatch.delenv(name, raising=False)


def _inject_fake_attest(monkeypatch, *, check_flow_run=None, capture_snapshot=None):
    """Install a fake ``openadapt_attest`` package into ``sys.modules``."""
    pkg = types.ModuleType("openadapt_attest")
    pkg.__path__ = []  # mark it as a package
    flow_run = types.ModuleType("openadapt_attest.flow_run")
    check = types.ModuleType("openadapt_attest.check")
    if check_flow_run is not None:
        flow_run.check_flow_run = check_flow_run
    if capture_snapshot is not None:
        check.capture_snapshot = capture_snapshot
    pkg.flow_run = flow_run
    pkg.check = check
    monkeypatch.setitem(sys.modules, "openadapt_attest", pkg)
    monkeypatch.setitem(sys.modules, "openadapt_attest.flow_run", flow_run)
    monkeypatch.setitem(sys.modules, "openadapt_attest.check", check)


def _block_attest_import(monkeypatch):
    """Make ``openadapt_attest`` deterministically unimportable."""
    for name in list(sys.modules):
        if name == "openadapt_attest" or name.startswith("openadapt_attest."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class _Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "openadapt_attest" or fullname.startswith(
                "openadapt_attest."
            ):
                raise ModuleNotFoundError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])


def _confirmed_result():
    return SimpleNamespace(
        verdict=SimpleNamespace(value="confirmed"),
        tier=2,
        receipt={"verdict": "confirmed"},
    )


def _successful_report(run_dir: Path):
    """A minimal SUCCESSFUL run report saved into ``run_dir``."""
    from openadapt_flow.ir import RunReport

    report = RunReport(
        workflow_name="attest bridge fixture",
        started_at="2026-08-13T00:00:00Z",
        success=True,
    )
    report.save(run_dir)
    return report


# --- (a) no contract configured -> silent no-op ------------------------------


def test_no_contract_is_a_silent_noop(tmp_path, monkeypatch, capsys):
    _inject_fake_attest(
        monkeypatch,
        check_flow_run=lambda *a, **k: pytest.fail(
            "must never attest without a contract"
        ),
        capture_snapshot=lambda *a, **k: pytest.fail(
            "must never snapshot without a contract"
        ),
    )
    run_dir = tmp_path / "run"
    maybe_attest_run(run_dir, report=None, args=argparse.Namespace())
    maybe_attest_run(run_dir, report=None, args=None)
    maybe_capture_pre_state(run_dir, args=None)
    assert capsys.readouterr().out == ""
    assert not (run_dir / PRE_STATE_FILENAME).exists()


# --- (b) contract + fake sidecar -> summary + exact frozen-interface call ----


def test_attest_run_calls_sidecar_and_prints_summary(tmp_path, monkeypatch, capsys):
    calls: list = []

    def fake_check_flow_run(run_dir, contract_path, **kw):
        calls.append((run_dir, contract_path, kw))
        return _confirmed_result()

    _inject_fake_attest(monkeypatch, check_flow_run=fake_check_flow_run)
    run_dir = tmp_path / "run"
    args = argparse.Namespace(
        attest_contract=str(tmp_path / "contract.yaml"),
        attest_sign_key=str(tmp_path / "key.pem"),
        attest_audit_log=str(tmp_path / "audit.log"),
        attest_pre_state=None,
    )
    maybe_attest_run(run_dir, report=None, args=args)

    assert calls == [
        (
            run_dir,
            tmp_path / "contract.yaml",
            {
                "sign_key": tmp_path / "key.pem",
                "audit_log": tmp_path / "audit.log",
                "pre_state_path": None,
            },
        )
    ]
    out = capsys.readouterr().out
    assert "attest: verdict confirmed (evidence tier 2)" in out
    assert str(run_dir / RECEIPT_FILENAME) in out


def test_attest_run_passes_explicit_pre_state_path(tmp_path, monkeypatch):
    calls: list = []

    def fake_check_flow_run(run_dir, contract_path, **kw):
        calls.append(kw)
        return _confirmed_result()

    _inject_fake_attest(monkeypatch, check_flow_run=fake_check_flow_run)
    args = argparse.Namespace(
        attest_contract=str(tmp_path / "contract.yaml"),
        attest_pre_state=str(tmp_path / "before.json"),
    )
    maybe_attest_run(tmp_path / "run", report=None, args=args)
    assert calls[0]["pre_state_path"] == tmp_path / "before.json"


# --- (c) sidecar not installed -> one notice line, no exception --------------


def test_attest_run_degrades_to_one_line_when_not_installed(
    tmp_path, monkeypatch, capsys
):
    _block_attest_import(monkeypatch)
    args = argparse.Namespace(attest_contract=str(tmp_path / "contract.yaml"))
    maybe_attest_run(tmp_path / "run", report=None, args=args)
    out = capsys.readouterr().out
    assert out == "attest: openadapt-attest is not installed; skipping receipt\n"


def test_pre_state_capture_degrades_to_one_line_when_not_installed(
    tmp_path, monkeypatch, capsys
):
    _block_attest_import(monkeypatch)
    run_dir = tmp_path / "run"
    args = argparse.Namespace(attest_contract=str(tmp_path / "contract.yaml"))
    maybe_capture_pre_state(run_dir, args=args)
    out = capsys.readouterr().out
    assert out == (
        "attest: openadapt-attest is not installed; skipping pre-state snapshot\n"
    )
    assert not (run_dir / PRE_STATE_FILENAME).exists()


# --- (d) sidecar raising -> warning only, exit-code mapping untouched --------


def test_attest_failure_is_swallowed_and_exit_code_unchanged(
    tmp_path, monkeypatch, capsys
):
    from openadapt_flow.__main__ import _finish_replay

    def exploding_check_flow_run(run_dir, contract_path, **kw):
        raise RuntimeError("system of record unreachable")

    _inject_fake_attest(monkeypatch, check_flow_run=exploding_check_flow_run)
    run_dir = tmp_path / "run"
    report = _successful_report(run_dir)
    args = argparse.Namespace(attest_contract=str(tmp_path / "contract.yaml"))

    # Must never raise: the hook cannot change the run's outcome.
    maybe_attest_run(run_dir, report, args)
    assert "attest receipt skipped: system of record unreachable" in (
        capsys.readouterr().out
    )

    # The full replay tail still maps the SUCCESSFUL report to exit code 0.
    assert _finish_replay(run_dir, report, args) == 0
    out = capsys.readouterr().out
    assert "Replay success" in out
    assert "attest receipt skipped: system of record unreachable" in out


def test_finish_replay_without_attest_config_never_touches_the_sidecar(
    tmp_path, monkeypatch, capsys
):
    from openadapt_flow.__main__ import _finish_replay

    _inject_fake_attest(
        monkeypatch,
        check_flow_run=lambda *a, **k: pytest.fail(
            "must never attest without a contract"
        ),
    )
    run_dir = tmp_path / "run"
    report = _successful_report(run_dir)
    assert _finish_replay(run_dir, report, argparse.Namespace()) == 0
    assert "attest" not in capsys.readouterr().out


# --- (e) env fallback works and the CLI flag wins -----------------------------


def test_env_fallback_and_cli_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv(CONTRACT_ENV, str(tmp_path / "env-contract.yaml"))
    monkeypatch.setenv(SIGN_KEY_ENV, str(tmp_path / "env-key.pem"))
    monkeypatch.setenv(AUDIT_LOG_ENV, str(tmp_path / "env-audit.log"))
    monkeypatch.setenv(PRE_STATE_ENV, str(tmp_path / "env-before.json"))

    from_env = resolve_attest_config(argparse.Namespace())
    assert from_env.contract == tmp_path / "env-contract.yaml"
    assert from_env.sign_key == tmp_path / "env-key.pem"
    assert from_env.audit_log == tmp_path / "env-audit.log"
    assert from_env.pre_state == tmp_path / "env-before.json"

    cli_wins = resolve_attest_config(
        argparse.Namespace(
            attest_contract=str(tmp_path / "cli-contract.yaml"),
            attest_sign_key=str(tmp_path / "cli-key.pem"),
        )
    )
    assert cli_wins.contract == tmp_path / "cli-contract.yaml"
    assert cli_wins.sign_key == tmp_path / "cli-key.pem"
    # Flags left unset still fall back to the environment.
    assert cli_wins.audit_log == tmp_path / "env-audit.log"
    assert cli_wins.pre_state == tmp_path / "env-before.json"


def test_env_only_opt_in_fires_the_hook(tmp_path, monkeypatch, capsys):
    calls: list = []

    def fake_check_flow_run(run_dir, contract_path, **kw):
        calls.append(contract_path)
        return _confirmed_result()

    _inject_fake_attest(monkeypatch, check_flow_run=fake_check_flow_run)
    monkeypatch.setenv(CONTRACT_ENV, str(tmp_path / "env-contract.yaml"))
    maybe_attest_run(tmp_path / "run", report=None, args=None)
    assert calls == [tmp_path / "env-contract.yaml"]
    assert "attest: verdict confirmed" in capsys.readouterr().out


def test_replay_run_and_resume_parsers_accept_the_attest_flags():
    from openadapt_flow.__main__ import build_parser

    parser = build_parser()
    for argv in (
        ["replay", "bundle"],
        ["run", "bundle"],
        ["resume", "run-dir"],
    ):
        args = parser.parse_args(
            [
                *argv,
                "--attest-contract",
                "contract.yaml",
                "--attest-sign-key",
                "key.pem",
                "--attest-audit-log",
                "audit.log",
                "--attest-pre-state",
                "before.json",
            ]
        )
        assert args.attest_contract == "contract.yaml"
        assert args.attest_sign_key == "key.pem"
        assert args.attest_audit_log == "audit.log"
        assert args.attest_pre_state == "before.json"


# --- (f) pre-run capture -------------------------------------------------------


def test_pre_state_capture_writes_snapshot(tmp_path, monkeypatch, capsys):
    calls: list = []

    def fake_capture_snapshot(contract_path):
        calls.append(contract_path)
        return SimpleNamespace(
            model_dump_json=lambda: '{"records": 3}',
            reachable=True,
        )

    _inject_fake_attest(monkeypatch, capture_snapshot=fake_capture_snapshot)
    run_dir = tmp_path / "runs" / "r1"  # does not exist yet: capture creates it
    args = argparse.Namespace(attest_contract=str(tmp_path / "contract.yaml"))
    maybe_capture_pre_state(run_dir, args=args)

    assert calls == [tmp_path / "contract.yaml"]
    pre_state = run_dir / PRE_STATE_FILENAME
    assert pre_state.read_text(encoding="utf-8") == '{"records": 3}\n'
    out = capsys.readouterr().out
    assert f"attest: pre-state snapshot written to {pre_state}" in out
    assert "UNREACHABLE" not in out


def test_explicit_pre_state_flag_suppresses_capture(tmp_path, monkeypatch, capsys):
    _inject_fake_attest(
        monkeypatch,
        capture_snapshot=lambda *a, **k: pytest.fail(
            "an explicit --attest-pre-state must suppress capture"
        ),
    )
    run_dir = tmp_path / "run"
    args = argparse.Namespace(
        attest_contract=str(tmp_path / "contract.yaml"),
        attest_pre_state=str(tmp_path / "before.json"),
    )
    maybe_capture_pre_state(run_dir, args=args)
    assert capsys.readouterr().out == ""
    assert not (run_dir / PRE_STATE_FILENAME).exists()


def test_pre_state_capture_failure_is_swallowed(tmp_path, monkeypatch, capsys):
    def exploding_capture_snapshot(contract_path):
        raise RuntimeError("no route to the system of record")

    _inject_fake_attest(monkeypatch, capture_snapshot=exploding_capture_snapshot)
    run_dir = tmp_path / "run"
    args = argparse.Namespace(attest_contract=str(tmp_path / "contract.yaml"))
    # Must never raise: the run proceeds without a baseline.
    maybe_capture_pre_state(run_dir, args=args)
    out = capsys.readouterr().out
    assert "attest pre-state capture skipped: no route to the system of record" in out
    assert not (run_dir / PRE_STATE_FILENAME).exists()
