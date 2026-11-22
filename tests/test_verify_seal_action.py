"""Tests for the .github/actions/verify-seal composite GitHub Action."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / ".github" / "actions" / "verify-seal"
SCRIPT_PATH = ACTION_DIR / "verify_seal.py"
ACTION_YML = ACTION_DIR / "action.yml"
VECTORS = ROOT / "packages" / "types" / "tests" / "vectors" / "record-run-v1"


def _load_module() -> Any:
    types_dir = str(ROOT / "packages" / "types")
    if types_dir not in sys.path:
        sys.path.insert(0, types_dir)
    spec = importlib.util.spec_from_file_location("verify_seal_action", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _make_signed_receipt(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Sign a valid record/run/v1 statement with a fresh Ed25519 keypair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    types_dir = str(ROOT / "packages" / "types")
    if types_dir not in sys.path:
        sys.path.insert(0, types_dir)
    from openadapt_types import receipt as r

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw_pub = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pem_pub = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path = tmp_path / "public-key.pem"
    pub_path.write_bytes(pem_pub)

    # Reuse the canonical valid statement from the shared vector set.
    valid_vec = json.loads((VECTORS / "valid.json").read_text(encoding="utf-8"))
    import base64

    statement = json.loads(
        base64.b64decode(valid_vec["document"]["payload"]).decode("utf-8")
    )
    envelope = r.sign_statement(statement, [(r.key_id(raw_pub), priv.sign)])
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return receipt_path, pub_path, envelope


def test_valid_receipt_with_pem_key_passes_and_writes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    receipt_path, pub_path, _ = _make_signed_receipt(tmp_path)
    out_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "github_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    rc = mod.verify_seal_cli(
        ["--receipt", str(receipt_path), "--public-key", str(pub_path)]
    )
    assert rc == 0

    outputs = _parse_outputs(out_file)
    assert outputs["verified"] == "true"
    assert outputs["outcome"] != ""
    assert outputs["receipt-id"] != ""
    assert outputs["reason"] == ""

    summary = summary_file.read_text(encoding="utf-8")
    assert "✅ Verified by OpenAdapt Seal" in summary
    assert outputs["receipt-id"] in summary
    assert outputs["outcome"] in summary
    assert "Ed25519" in summary
    assert "openadapt verify" in summary


def test_valid_receipt_with_registry_json_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    valid_vec = json.loads((VECTORS / "valid.json").read_text(encoding="utf-8"))
    receipt_path = tmp_path / "vector_valid.json"
    receipt_path.write_text(json.dumps(valid_vec["document"]), encoding="utf-8")
    out_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "github_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    rc = mod.verify_seal_cli(
        [
            "--receipt",
            str(receipt_path),
            "--public-key",
            str(VECTORS / "registry.json"),
        ]
    )
    assert rc == 0
    assert _parse_outputs(out_file)["verified"] == "true"


def test_tampered_receipt_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    receipt_path, pub_path, envelope = _make_signed_receipt(tmp_path)
    # Flip a byte in the base64 payload so the Ed25519 signature no longer verifies.
    payload = envelope["payload"]
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    envelope["payload"] = flipped
    receipt_path.write_text(json.dumps(envelope), encoding="utf-8")

    out_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "github_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    rc = mod.verify_seal_cli(
        ["--receipt", str(receipt_path), "--public-key", str(pub_path)]
    )
    assert rc == 1
    outputs = _parse_outputs(out_file)
    assert outputs["verified"] == "false"
    assert outputs["reason"] != ""
    assert "❌ OpenAdapt Seal Verification Failed" in summary_file.read_text(
        encoding="utf-8"
    )


def test_missing_receipt_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    out_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "github_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    rc = mod.verify_seal_cli(["--receipt", str(tmp_path / "does-not-exist.json")])
    assert rc == 1
    outputs = _parse_outputs(out_file)
    assert outputs["verified"] == "false"
    assert outputs["reason"] == "missing_receipt"


def test_missing_trusted_keys_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    receipt_path, _, _ = _make_signed_receipt(tmp_path)
    out_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    rc = mod.verify_seal_cli(["--receipt", str(receipt_path)])
    assert rc == 1
    outputs = _parse_outputs(out_file)
    assert outputs["verified"] == "false"
    assert outputs["reason"] == "no_trusted_keys"


def test_action_yml_pins_setup_python_and_avoids_script_injection() -> None:
    text = ACTION_YML.read_text(encoding="utf-8")
    assert "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0" in text
    assert "verify_seal.py" in text
    # Inputs must flow through env vars, never directly interpolated inside run: blocks.
    in_run = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("run:"):
            in_run = True
            continue
        if in_run and line and not line.startswith("        "):
            in_run = False
        if in_run:
            assert "${{ inputs." not in line
