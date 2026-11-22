"""Unit tests for zero-PHI pilot capture bundles and Model C local sandbox runner."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.pilot_bundle import (
    PILOT_BUNDLE_SCHEMA,
    PilotExecutionResult,
    PilotIntegrityError,
    PilotRedactionError,
    PilotVerificationResult,
    create_pilot_bundle,
    inspect_pilot_bundle,
    main,
    run_pilot_pack,
)


def _dirty_actions() -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "kind": "type",
            "target": "input[name=email]",
            "value": "alice.smith@example.org",
        },
        {
            "step": 2,
            "kind": "type",
            "target": "input[name=ssn]",
            "value": "123-45-6789",
        },
        {
            "step": 3,
            "kind": "type",
            "target": "input[name=phone]",
            "value": "(415) 555-0199",
        },
        {
            "step": 4,
            "kind": "type",
            "target": "input[name=password]",
            "password": "synthetic-test-password-value",
        },
        {
            "step": 5,
            "kind": "http_header",
            "header": "Bearer "
            + ".".join(
                [
                    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                    "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                    "abc123def456",
                ]
            ),
        },
    ]


def test_create_pilot_bundle_redacts_pii_and_writes_manifest(tmp_path: Path) -> None:
    out_zip = tmp_path / "demo.oapilot.zip"
    bundle = create_pilot_bundle(
        out_zip,
        app_name="sample-portal",
        workflow_name="submit-order",
        actions=_dirty_actions(),
        dom_snapshots={
            "step1": {
                "title": "Order Form",
                "text": "Contact bob@example.com or 987-65-4321",
                "api_secret": "synthetic-secret-field",
            }
        },
        screenshots={"step1.png": b"\x89PNG\r\n\x1a\nsynthetic"},
    )

    assert bundle.path == out_zip
    assert bundle.schema == PILOT_BUNDLE_SCHEMA
    assert bundle.step_count == 5
    assert bundle.snapshot_count == 1
    assert bundle.screenshot_count == 1
    assert bundle.redaction_report.safe_to_share is True
    assert bundle.redaction_report.secrets_remaining == 0
    assert bundle.redaction_report.fields_scrubbed >= 2
    assert bundle.redaction_report.patterns_matched["ssn"] >= 2
    assert bundle.redaction_report.patterns_matched["email"] >= 2
    assert bundle.redaction_report.patterns_matched["bearer_token"] >= 1

    # Verify zero raw PII/secrets appear anywhere in the ZIP text entries.
    with zipfile.ZipFile(out_zip, "r") as zf:
        for member in ("trace.json", "dom_snapshots/step1.json", "metadata.json"):
            raw_text = zf.read(member).decode("utf-8")
            assert "alice.smith@example.org" not in raw_text
            assert "bob@example.com" not in raw_text
            assert "123-45-6789" not in raw_text
            assert "987-65-4321" not in raw_text
            assert "synthetic-test-password-value" not in raw_text
            assert "synthetic-secret-field" not in raw_text
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in raw_text

    inspection = inspect_pilot_bundle(out_zip)
    assert inspection.verified_files == len(bundle.manifest_sha256)
    summary = inspection.format_summary()
    assert "SAFE TO SHARE" in summary
    assert "5 steps" in summary


def test_create_pilot_bundle_fails_closed_when_redaction_disabled_on_dirty_data(
    tmp_path: Path,
) -> None:
    out_zip = tmp_path / "should-not-exist.oapilot.zip"
    with pytest.raises(PilotRedactionError, match="unredacted PII/secret"):
        create_pilot_bundle(
            out_zip,
            app_name="sample-portal",
            workflow_name="submit-order",
            actions=_dirty_actions(),
            redact=False,
        )
    assert not out_zip.exists()


def test_inspect_pilot_bundle_detects_tampered_file_hash(tmp_path: Path) -> None:
    out_zip = tmp_path / "tampered.oapilot.zip"
    create_pilot_bundle(
        out_zip,
        app_name="sample-portal",
        workflow_name="submit-order",
        actions=[{"step": 1, "kind": "click", "target": "button#ok"}],
    )

    # Rewrite trace.json inside the archive without updating manifest.json.
    tampered_zip = tmp_path / "tampered_copy.oapilot.zip"
    with zipfile.ZipFile(out_zip, "r") as src, zipfile.ZipFile(tampered_zip, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "trace.json":
                data = data + b"\n"
            dst.writestr(item, data)

    with pytest.raises(PilotIntegrityError, match="SHA-256 mismatch"):
        inspect_pilot_bundle(tampered_zip)


def test_inspect_pilot_bundle_rejects_zip_path_traversal(tmp_path: Path) -> None:
    evil_zip = tmp_path / "evil.oapilot.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.txt", b"bad")
        zf.writestr("manifest.json", b"{}")
        zf.writestr("metadata.json", b"{}")

    with pytest.raises(PilotIntegrityError, match="Path traversal"):
        inspect_pilot_bundle(evil_zip)


def test_run_pilot_pack_emits_verifiable_value_free_ed25519_receipt(
    tmp_path: Path,
) -> None:
    receipt_mod = pytest.importorskip("openadapt_types.receipt")
    pack_path = tmp_path / "sample.oapack.json"
    pack_path.write_text(
        json.dumps(
            {
                "workflow_name": "submit-order-v1",
                "steps": [{"id": "s1", "kind": "click", "target": "button#save"}],
            }
        ),
        encoding="utf-8",
    )
    receipt_out = tmp_path / "receipt.json"

    def _executor(pack: dict[str, Any]) -> PilotExecutionResult:
        assert pack["workflow_name"] == "submit-order-v1"
        return PilotExecutionResult(
            steps_total=1,
            steps_ok=1,
            writes_attempted=1,
            succeeded=True,
            model_calls=0,
        )

    def _verifier(
        _pack: dict[str, Any], _exec: PilotExecutionResult
    ) -> PilotVerificationResult:
        return PilotVerificationResult(
            readback_confirmed=True,
            checks_required=1,
            checks_confirmed=1,
        )

    outcome = run_pilot_pack(
        pack_path,
        executor=_executor,
        verifier=_verifier,
        receipt_out=receipt_out,
    )

    assert outcome.outcome == "VERIFIED"
    assert receipt_out.is_file()
    loaded = json.loads(receipt_out.read_text(encoding="utf-8"))
    check = receipt_mod.verify_receipt(loaded, [outcome.signer_key])
    assert check.ok is True, f"verify_receipt refused: {check.reason} ({check.detail})"
    assert check.outcome == "VERIFIED"
    # Raw workflow name must never appear in the value-free receipt payload.
    assert "submit-order-v1" not in json.dumps(check.statement)


def test_run_pilot_pack_stops_fail_closed_on_readback_mismatch(tmp_path: Path) -> None:
    receipt_mod = pytest.importorskip("openadapt_types.receipt")
    pack_path = tmp_path / "sample.oapack.json"
    pack_path.write_text(
        json.dumps({"workflow_name": "submit-order-v1", "steps": []}),
        encoding="utf-8",
    )

    outcome = run_pilot_pack(
        pack_path,
        executor=lambda _p: PilotExecutionResult(
            steps_total=2, steps_ok=2, writes_attempted=1, succeeded=True
        ),
        verifier=lambda _p, _e: PilotVerificationResult(
            readback_confirmed=False,
            checks_required=1,
            checks_confirmed=0,
            reason_code="readback_mismatch",
        ),
    )

    assert outcome.outcome == "STOPPED"
    check = receipt_mod.verify_receipt(outcome.receipt_envelope, [outcome.signer_key])
    assert check.ok is True, f"verify_receipt refused: {check.reason} ({check.detail})"
    assert check.outcome == "STOPPED"


def test_pilot_bundle_cli_capture_and_inspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace_file = tmp_path / "trace.json"
    trace_file.write_text(
        json.dumps({"actions": _dirty_actions(), "dom_snapshots": {"s1": {"note": "a@b.com"}}}),
        encoding="utf-8",
    )
    out_zip = tmp_path / "cli.oapilot.zip"

    rc_cap = main(
        [
            "capture",
            "--trace",
            str(trace_file),
            "--out",
            str(out_zip),
            "--app",
            "sample-app",
            "--workflow",
            "sample-flow",
        ]
    )
    assert rc_cap == 0
    cap_json = json.loads(capsys.readouterr().out)
    assert cap_json["redaction"]["safe_to_share"] is True
    assert cap_json["redaction"]["secrets_remaining"] == 0

    rc_ins = main(["inspect", str(out_zip)])
    assert rc_ins == 0
    ins_out = capsys.readouterr().out
    assert "SAFE TO SHARE" in ins_out
    assert "Integrity:      OK" in ins_out
