"""Connector startup, ledger location, and the shipped container configuration."""

import argparse
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from openadapt_flow import at_rest
from openadapt_flow.connector import cli, windows_service
from openadapt_flow.connector.config import ConnectorConfigError, ConnectorSettings
from openadapt_flow.connector.secrets import LocalSecretStore, SecretStoreError


@pytest.mark.parametrize("profile", ["standard", "regulated"])
def test_managed_profiles_pass_preflight(tmp_path, profile):
    path = tmp_path / "deployment.yaml"
    path.write_text(f"runtime: {{profile: {profile}}}", encoding="utf-8")
    ConnectorSettings(profile=str(path)).validate_run_config()


@pytest.mark.parametrize(
    "contents", [None, "{}", "runtime: {profile: demo}", "runtime: {profile: invalid}"]
)
def test_run_and_service_refuse_invalid_profile_before_any_request(
    tmp_path, monkeypatch, capsys, caplog, contents
):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    monkeypatch.setenv("BYOC_CONNECTOR_TOKEN", "synthetic-test-value")
    monkeypatch.delenv("BYOC_PROFILE", raising=False)
    if contents is not None:
        path = tmp_path / "deployment.yaml"
        path.write_text(contents, encoding="utf-8")
        monkeypatch.setenv("BYOC_PROFILE", str(path))

    def no_client(*args, **kwargs):
        pytest.fail("startup connected before checking its deployment configuration")

    monkeypatch.setattr("openadapt_flow.connector.ConnectorClient", no_client)
    monkeypatch.setattr(windows_service, "_stoppable_client", no_client)
    assert cli._cmd_connector(argparse.Namespace(connector_cmd="run")) == 2
    assert "runtime.profile" in capsys.readouterr().out
    assert (
        windows_service.serve(tmp_path, threading.Event(), logging.getLogger(__name__))
        == 2
    )
    assert "runtime.profile" in caplog.text
    assert "synthetic-test-value" not in caplog.text


def test_container_uses_private_ledger_outside_read_only_config(tmp_path, monkeypatch):
    deploy = Path(__file__).resolve().parents[1] / "deploy" / "connector"
    image = (deploy / "Dockerfile").read_text()
    harness = (deploy / "e2e" / "run_e2e.sh").read_text()
    mount = "/var/lib/openadapt/ledger"
    assert "PYTHONUNBUFFERED=1" in image
    assert f"{at_rest.LEDGER_DIR_ENV}={mount}" in image
    assert f'source=$STATE/runner-ledger,target={mount}"' in harness

    key_home = tmp_path / "config"
    ledger_dir = tmp_path / "private-state"
    ledger_dir.mkdir(mode=0o700)
    monkeypatch.setenv(at_rest.HOME_ENV, str(key_home))
    monkeypatch.setenv(at_rest.LEDGER_DIR_ENV, str(ledger_dir))
    at_rest.unsealed_write("synthetic run", tmp_path / "report")
    assert at_rest.ledger_path() == ledger_dir / at_rest.LEDGER_NAME
    entry = json.loads(at_rest.ledger_path().read_text())
    assert entry["path"] == str(tmp_path / "report")
    assert entry["purpose"].startswith("synthetic run")
    assert at_rest.local_key_path() == key_home / "keys" / at_rest.KEY_NAME
    assert not key_home.exists()

    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    with pytest.raises(at_rest.AtRestRefused):
        at_rest.unsealed_write("synthetic strict run", tmp_path / "other")


def test_unwritable_ledger_still_refuses_the_write(tmp_path, monkeypatch):
    blocked = tmp_path / "file"
    blocked.write_text("existing file", encoding="utf-8")
    monkeypatch.setenv(at_rest.LEDGER_DIR_ENV, str(blocked))
    with pytest.raises(at_rest.AtRestRefused, match="plaintext ledger"):
        at_rest.unsealed_write("synthetic blocked run", tmp_path / "report")


def test_job_storage_cannot_contain_ledger(tmp_path, monkeypatch):
    config = tmp_path / "deployment.yaml"
    config.write_text("runtime: {profile: standard}", encoding="utf-8")
    storage = tmp_path / "store"
    monkeypatch.setenv(at_rest.LEDGER_DIR_ENV, str(storage / "ledger"))
    with pytest.raises(ConnectorConfigError, match="outside"):
        ConnectorSettings(
            profile=str(config), storage_root=str(storage)
        ).validate_run_config()


@pytest.mark.parametrize(
    "name", [at_rest.LEDGER_DIR_ENV, at_rest.HOME_ENV, "OPENADAPT_HOME"]
)
def test_secret_reference_cannot_select_ledger(tmp_path, name):
    secret_file = tmp_path / "synthetic.env"
    secret_file.write_text(f"{name}=/wrong", encoding="utf-8")
    if os.name == "posix":
        secret_file.chmod(0o600)
    with pytest.raises(SecretStoreError, match="invalid"):
        LocalSecretStore(str(tmp_path)).resolve("synthetic")
