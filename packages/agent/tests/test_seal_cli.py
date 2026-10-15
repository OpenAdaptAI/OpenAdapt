"""seal keys pin (TOFU + rotation) and seal verify (offline, pinned keys only)."""

from __future__ import annotations

import argparse
import base64
import io
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_agent.cli import build_parser
from openadapt_agent.seal import _canonical, _key_id, receipt_sha256
from openadapt_agent.seal_cli import cmd_keys_pin, cmd_verify, plan_update

EXEC = "11111111-2222-4333-8444-555555555555"


def _key():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, {"key_id": _key_id(raw), "public_key": base64.b64encode(raw).decode(), "status": "active"}


def _sealed(private, entry, outcome="verified", issued_at="2026-10-14T12:00:00Z"):
    receipt = {"schema_version": "openadapt.execute-evidence-receipt/v1", "execution_id": EXEC, "outcome": outcome}
    sig = {
        "schema": "openadapt.seal-signature/v1", "issuer": "openadapt-cloud", "algorithm": "ed25519",
        "key_id": entry["key_id"], "receipt_sha256": receipt_sha256(receipt), "run_id": EXEC, "issued_at": issued_at,
    }
    sig["signature"] = base64.b64encode(private.sign(b"openadapt.seal-signature/v1\0" + _canonical(sig).encode())).decode()
    return {**receipt, "seal_signature": sig}


def _pin_args(tmp_path, *confirm):
    return argparse.Namespace(url="https://cloud.test/api/public/seal-keys", path=str(tmp_path / "keys.json"), confirm=list(confirm))


def _fetch(*entries):
    return lambda url: json.dumps({"schema": "openadapt.seal-signature/v1", "keys": list(entries)}).encode()


def test_first_use_needs_a_person(tmp_path):
    _, entry = _key()
    out = io.StringIO()
    # No terminal and no --confirm: nothing is trusted.
    assert cmd_keys_pin(_pin_args(tmp_path), fetch=_fetch(entry), is_tty=lambda: False, out=out) == 1
    assert not (tmp_path / "keys.json").exists()
    # A person types the wrong id: skipped.
    assert cmd_keys_pin(_pin_args(tmp_path), fetch=_fetch(entry), is_tty=lambda: True, ask=lambda _: "y", out=out) == 1
    assert not (tmp_path / "keys.json").exists()
    # A person types the key id back: pinned.
    assert cmd_keys_pin(_pin_args(tmp_path), fetch=_fetch(entry), is_tty=lambda: True, ask=lambda _: entry["key_id"], out=out) == 0
    pinned = json.loads((tmp_path / "keys.json").read_text())
    assert [k["key_id"] for k in pinned["keys"]] == [entry["key_id"]]
    assert "fingerprint" in out.getvalue()


def test_rotation_adds_only_confirmed_keys_and_applies_revocation(tmp_path):
    _, old = _key()
    _, new = _key()
    assert cmd_keys_pin(_pin_args(tmp_path, old["key_id"]), fetch=_fetch(old), is_tty=lambda: False, out=io.StringIO()) == 0
    revoked_old = dict(old, status="revoked", revoked_at="2026-11-01T00:00:00Z")
    # Unconfirmed new key: revocation applied, new key not trusted.
    cmd_keys_pin(_pin_args(tmp_path), fetch=_fetch(revoked_old, new), is_tty=lambda: False, out=io.StringIO())
    keys = {k["key_id"]: k for k in json.loads((tmp_path / "keys.json").read_text())["keys"]}
    assert keys[old["key_id"]]["status"] == "revoked" and new["key_id"] not in keys
    # Confirmed: added. A later "un-revoke" from Cloud is refused.
    cmd_keys_pin(_pin_args(tmp_path, new["key_id"]), fetch=_fetch(old, new), is_tty=lambda: False, out=io.StringIO())
    keys = {k["key_id"]: k for k in json.loads((tmp_path / "keys.json").read_text())["keys"]}
    assert keys[old["key_id"]]["status"] == "revoked" and keys[new["key_id"]]["status"] == "active"


def test_plan_keeps_keys_cloud_stopped_listing():
    _, a = _key()
    assert plan_update([a], [])["missing"] == [a]


def test_tampered_document_is_refused(tmp_path):
    private, entry = _key()
    rogue, _ = _key()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [entry]}))
    good = tmp_path / "report.json"
    good.write_text(json.dumps({"execution_outcome": "VERIFIED", "execute_receipt": _sealed(private, entry)}))

    def verify(path):
        out = io.StringIO()
        code = cmd_verify(argparse.Namespace(target=str(path), keys=str(keys), json=True), out=out)
        return code, json.loads(out.getvalue())

    assert verify(good)[0] == 0
    tampered = json.loads(good.read_text())
    tampered["execute_receipt"]["execution_id"] = "22222222-2222-4333-8444-555555555555"
    (tmp_path / "t.json").write_text(json.dumps(tampered))
    assert verify(tmp_path / "t.json") == (1, verify(tmp_path / "t.json")[1])
    assert verify(tmp_path / "t.json")[1]["reason"] == "receipt_mismatch"
    (tmp_path / "f.json").write_text(json.dumps(_sealed(rogue, entry)))
    assert verify(tmp_path / "f.json")[1]["reason"] == "bad_signature"
    (tmp_path / "h.json").write_text(json.dumps(_sealed(private, entry, outcome="halted")))
    code, verdict = verify(tmp_path / "h.json")
    assert code == 1 and verdict["verified"] and verdict["verified_outcome"] is False
    revoked = tmp_path / "revoked.json"
    revoked.write_text(json.dumps({"keys": [dict(entry, status="revoked", revoked_at="2026-10-01T00:00:00Z")]}))
    out = io.StringIO()
    assert cmd_verify(argparse.Namespace(target=str(good), keys=str(revoked), json=True), out=out) == 1
    assert json.loads(out.getvalue())["reason"] == "revoked_key"


def test_verify_url_and_no_keys(tmp_path, monkeypatch):
    private, entry = _key()
    monkeypatch.setattr("openadapt_agent.seal.default_keys_path", lambda: tmp_path / ".openadapt" / "seal-keys.json")
    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_JSON", raising=False)
    body = json.dumps(_sealed(private, entry)).encode()
    out = io.StringIO()
    args = argparse.Namespace(target="https://example.test/seal.json", keys=None, json=False)
    assert cmd_verify(args, fetch=lambda url: body, out=out) == 1
    assert "No Seal keys are pinned" in out.getvalue()
    (tmp_path / ".openadapt").mkdir()
    (tmp_path / ".openadapt" / "seal-keys.json").write_text(json.dumps({"keys": [entry]}))
    out = io.StringIO()
    assert cmd_verify(args, fetch=lambda url: body, out=out) == 0
    assert out.getvalue().startswith("SEAL VERIFIED")


def test_cli_wires_seal_commands():
    parser = build_parser()
    assert parser.parse_args(["seal", "verify", "x.json"]).func is cmd_verify
    assert parser.parse_args(["seal", "keys", "pin", "--confirm", "seal-ed25519-0"]).confirm == ["seal-ed25519-0"]


def test_seal_signed_by_cloud_typescript_signer_verifies():
    """Fixture produced by Cloud's src/lib/sealSignature.ts signSeal (throwaway key)."""
    from pathlib import Path

    from openadapt_agent.seal import parse_registry, verify_document

    fixture = json.loads((Path(__file__).parent / "fixtures" / "cloud-ts-signed-seal.json").read_text())
    registry = parse_registry({"keys": fixture["keys"]})
    assert verify_document(fixture["receipt"], registry)["reason"] == "verified"
    changed = dict(fixture["receipt"], note="changed")
    assert verify_document(changed, registry)["reason"] == "receipt_mismatch"
