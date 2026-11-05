"""Real CLI exit codes, canonical signed verdicts and bound store rechecks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_verification import VALUES


def _cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "openadapt_verify", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
    )


def _files(store, tmp_path, recheck_after=None):
    doc = store.contract()
    if recheck_after is not None:
        doc["frame"]["database"] = {
            "dialect": "sqlite",
            "read": {"sqlite_database": str(store.path)},
            "views": {"item": ""},
            "new_row_fields": {"item": ["id"]},
            "settle_s": 0,
            "recheck_after_s": recheck_after,
        }
    contract = tmp_path / "verify.yaml"
    contract.write_text(json.dumps(doc))
    params = tmp_path / "values.json"
    params.write_text(json.dumps(VALUES))
    return contract, params


def _run(store, tmp_path, contract, params, writer_status=0):
    writer = (
        "import sys,urllib.request; "
        "r=urllib.request.Request(sys.argv[1],data=sys.argv[2].encode(),"
        "headers={'Content-Type':'application/json'}); "
        "urllib.request.urlopen(r).read(); sys.exit(int(sys.argv[3]))"
    )
    return _cli(
        "run",
        "--contract",
        contract,
        "--params",
        params,
        "--state",
        tmp_path / "writes",
        "--",
        sys.executable,
        "-c",
        writer,
        store.base + "/items",
        json.dumps(VALUES),
        writer_status,
    )


def _pins(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from openadapt_flow.receipt_log import LocalLog
    from openadapt_flow.write_receipt import default_signer

    paths = []
    for name, raw in (
        ("issuer", default_signer().public_raw()),
        ("log", LocalLog().public_raw()),
    ):
        path = tmp_path / (name + ".pub.pem")
        path.write_bytes(
            Ed25519PublicKey.from_public_bytes(raw).public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        paths.append(path)
    return paths


def _verify(path, issuer, log):
    return _cli("verify", path, "--issuer-key", issuer, "--log-key", log)


@pytest.mark.parametrize(
    "case,mode,outcome,code,provisional",
    [
        ("final", "correct", "CONFIRMED", 0, False),
        ("uncertain", "skip", "UNCERTAIN", 3, False),
        ("refuted", "duplicate", "REFUTED", 4, False),
        ("provisional", "correct", "CONFIRMED", 5, True),
    ],
)
def test_cli_run_and_signed_verify_require_final_confirmation(
    store, tmp_path, case, mode, outcome, code, provisional
):
    store.mode = mode
    contract, params = _files(store, tmp_path, 3600 if provisional else None)
    run = _run(store, tmp_path, contract, params)
    result = json.loads(run.stdout)
    assert result["outcome"] == outcome, run.stderr + run.stdout
    assert result["provisional"] is provisional
    assert result["write_receipt"] == "issued"
    assert store.posts == 1
    assert len(store.records()) == {"skip": 0, "duplicate": 2}.get(mode, 1)
    # Check both codes before asserting, so a failing baseline retains both.
    checked = _verify(result["write_receipt_path"], *_pins(tmp_path))
    receipt = json.loads(checked.stdout)
    assert receipt["valid"] is True and receipt["payload"]["verdict"] == outcome
    assert receipt["payload"]["provisional"] is provisional
    assert (run.returncode, checked.returncode) == (code, code), (
        run.stdout,
        checked.stdout,
        run.returncode,
        checked.returncode,
    )
    if provisional:
        state = Path(result["recheck_state_path"])
        assert state.is_file() and state.name == "recheck.json"


@pytest.mark.parametrize("fault", ["tampered", "unknown_issuer"])
def test_cli_signature_failure_never_confirms(store, tmp_path, fault):
    contract, params = _files(store, tmp_path)
    run = _run(store, tmp_path, contract, params)
    assert run.returncode == 0, run.stderr + run.stdout
    path = Path(json.loads(run.stdout)["write_receipt_path"])
    issuer, log = _pins(tmp_path)
    if fault == "tampered":
        data = path.read_bytes()
        path = tmp_path / "tampered.receipt.cbor"
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    else:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        issuer = tmp_path / "unknown-issuer.pub.pem"
        issuer.write_bytes(
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    checked = _verify(path, issuer, log)
    assert checked.returncode == 1, checked.stderr + checked.stdout
    assert json.loads(checked.stdout)["valid"] is False


@pytest.mark.parametrize(
    "case,expected,code",
    [
        ("held", "CONFIRMED", 0),
        ("late_collateral", "REFUTED", 4),
        ("unavailable_database", "UNCERTAIN", 3),
        ("wrong_actor", "UNCERTAIN", 3),
        ("wrong_params", "UNCERTAIN", 3),
        ("wrong_contract", "UNCERTAIN", 3),
        ("tampered_state", "UNCERTAIN", 3),
        ("unissued_state", "UNCERTAIN", 3),
        ("not_due", None, 5),
    ],
)
def test_cli_recheck_uses_canonical_binding_and_refusal(
    store, tmp_path, case, expected, code
):
    contract, params = _files(store, tmp_path, 3600 if case == "not_due" else 1)
    run = _run(store, tmp_path, contract, params)
    first = json.loads(run.stdout)
    assert first["outcome"] == "CONFIRMED" and first["provisional"] is True
    assert first["write_receipt"] == "issued"
    # Locate canonical saved state independently of the new CLI field, so
    # the reviewed baseline reaches the missing-recheck-command control.
    state = Path(first["receipt_path"]).parent / "recheck.json"
    assert state.is_file()
    actor = "external-agent"
    env = None
    if case == "late_collateral":
        with sqlite3.connect(store.path) as db:
            db.execute(
                "INSERT INTO item(code, quantity) VALUES ('late-unexplained', '9')"
            )
    elif case == "unavailable_database":
        store.path.rename(tmp_path / "preserved-unavailable-store.sqlite")
    elif case == "wrong_actor":
        actor = "different-agent"
    elif case == "wrong_params":
        params.write_text(json.dumps({**VALUES, "quantity": "4"}))
    elif case == "wrong_contract":
        doc = json.loads(contract.read_text())
        doc["effects"][0]["expected_count"] = 2
        contract.write_text(json.dumps(doc))
    elif case == "tampered_state":
        payload = json.loads(state.read_text())
        payload["post_state"]["read_at"] = "2001-01-01T00:00:00Z"
        state = tmp_path / "tampered-state.json"
        state.write_text(json.dumps(payload))
    elif case == "unissued_state":
        env = {**os.environ, "OPENADAPT_VERIFIER_STATE_DIR": str(tmp_path / "unissued")}
    if case != "not_due":
        due = datetime.fromisoformat(first["recheck_due_at"].replace("Z", "+00:00"))
        time.sleep(max(0.0, (due - datetime.now(timezone.utc)).total_seconds()) + 0.02)
    checked = _cli(
        "recheck",
        "--contract",
        contract,
        "--params",
        params,
        "--recheck-state",
        state,
        "--agent-id",
        actor,
        env=env,
    )
    assert checked.returncode == code, checked.stderr + checked.stdout
    reply = json.loads(checked.stdout)
    assert store.posts == 1
    assert reply["recheck_due_at"] == first["recheck_due_at"]
    if case == "not_due":
        assert reply["due"] is False and reply["wait_s"] > 0
    else:
        assert reply["due"] is True and reply["outcome"] == expected
        assert reply["verified"] is (expected == "CONFIRMED")
        assert reply["receipt_sha256"] == first["receipt_sha256"]
        if case in ("held", "late_collateral"):
            assert reply["post_save_read_at"] and reply["read_at"]
        else:
            assert reply["reasons"]
    # A new store read does not silently upgrade the original signed payload.
    signed = _verify(first["write_receipt_path"], *_pins(tmp_path))
    assert signed.returncode == 5
    assert json.loads(signed.stdout)["payload"]["provisional"] is True


def test_cli_committed_write_with_command_failure_is_nonzero(store, tmp_path):
    contract, params = _files(store, tmp_path)
    run = _run(store, tmp_path, contract, params, writer_status=3)
    result = json.loads(run.stdout)
    assert result["outcome"] == "CONFIRMED" and result["write_receipt"] == "issued"
    assert result["command_returncode"] == 3 and store.posts == 1
    assert run.returncode == 1


def test_plain_import_does_not_trigger_deprecated_guard_or_optional_sdks():
    code = (
        "import sys; import openadapt_verify; "
        "assert 'openadapt_flow.verify' not in sys.modules; "
        "assert not {'mcp','agents','openai','claude_agent_sdk','browser_use',"
        "'playwright'}.intersection(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_serve_missing_mcp_reports_extra_without_traceback(tmp_path):
    code = """
import importlib.abc
import sys
class MissingMCP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.'):
            raise ModuleNotFoundError("No module named 'mcp'", name=fullname)
sys.meta_path.insert(0, MissingMCP())
from openadapt_verify.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "serve",
            "--contracts",
            str(tmp_path),
            "--state",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "pip install 'openadapt-verify[mcp]'" in result.stderr
    assert "Traceback" not in result.stderr
