"""`openadapt deploy` must not call a host ready when it checked less than that.

Ledger B021. The preflight printed `[OK] env:FOO` for any reference that
matched the env:NAME or keychain:service/item syntax, even when FOO wasn't set.
It also printed "Preflight passed" after a `[SETUP]` line that says the guide
does not claim the target is ready. An operator read both as a ready host.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main

READY_WEB = {"openadapt_flow", "playwright"}


@pytest.fixture(autouse=True)
def supported_python(monkeypatch):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: cli._SUPPORTED_PYTHON[1])


def _deploy(monkeypatch, args, installed=READY_WEB, host=None):
    if host is not None:
        monkeypatch.setattr("openadapt.cli._host_platform", lambda: host, raising=False)
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in installed else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.35.0")
    return CliRunner().invoke(cli_main, ["deploy", *args])


def test_unset_env_reference_fails_the_preflight(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)

    result = _deploy(monkeypatch, ["--backend", "web", "--secret-ref", "env:FOO"])

    assert result.exit_code != 0, result.output
    assert "[OK] env:FOO" not in result.output
    assert "[MISSING] env:FOO is not set" in result.output
    assert "Preflight failed" in result.output
    assert "Preflight passed" not in result.output


def test_empty_env_reference_fails_the_preflight(monkeypatch):
    monkeypatch.setenv("FOO", "")

    result = _deploy(monkeypatch, ["--backend", "web", "--secret-ref", "env:FOO"])

    assert result.exit_code != 0, result.output
    assert "[OK] env:FOO" not in result.output
    assert "[MISSING] env:FOO" in result.output


def test_set_env_reference_is_ok_and_its_value_stays_hidden(monkeypatch):
    monkeypatch.setenv("FOO", "s3cr3t-token-value")

    result = _deploy(monkeypatch, ["--backend", "web", "--secret-ref", "env:FOO"])

    assert result.exit_code == 0, result.output
    assert "[OK] env:FOO is set" in result.output
    assert "s3cr3t-token-value" not in result.output
    assert "Preflight passed" in result.output


def test_keychain_reference_is_not_reported_as_ok(monkeypatch):
    result = _deploy(
        monkeypatch, ["--backend", "web", "--secret-ref", "keychain:svc/item"]
    )

    assert result.exit_code == 0, result.output
    assert "[OK] keychain:svc/item" not in result.output
    assert "[SETUP] keychain:svc/item" in result.output
    assert "Preflight passed" not in result.output
    assert "Preflight incomplete" in result.output
    assert "1 [SETUP] item above" in result.output


def test_native_backend_setup_item_is_not_a_pass(monkeypatch):
    result = _deploy(
        monkeypatch,
        ["--backend", "windows"],
        installed={"openadapt_flow", "requests"},
        host="linux",
    )

    assert result.exit_code == 0, result.output
    assert "[SETUP] windows target readiness" in result.output
    assert "Preflight passed" not in result.output
    assert "Preflight incomplete" in result.output
    assert "No service was started" in result.output


def test_setup_items_are_counted(monkeypatch):
    result = _deploy(
        monkeypatch,
        ["--backend", "windows", "--secret-ref", "keychain:svc/item"],
        installed={"openadapt_flow", "requests"},
        host="linux",
    )

    assert result.exit_code == 0, result.output
    assert "2 [SETUP] items above" in result.output


def test_fully_checked_web_host_still_passes(monkeypatch):
    result = _deploy(monkeypatch, ["--backend", "web"])

    assert result.exit_code == 0, result.output
    assert "[SETUP]" not in result.output
    assert "Preflight passed for the installed components" in result.output
