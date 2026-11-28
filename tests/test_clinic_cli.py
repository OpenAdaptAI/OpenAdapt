"""Unit tests for clinic CLI commands: trust init, qualify --local,
cohort-receipt, pool, and support-bundle.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types
import zipfile
from pathlib import Path
from typing import Optional, Sequence

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.audit_sample import (
    AUDIT_SAMPLE_SCHEMA,
    sample_runs,
    write_sample_manifest,
)
from openadapt.cohort_receipt import (
    COHORT_RECEIPT_SCHEMA,
    build_cohort_receipt,
    compute_merkle_root,
    verify_cohort_receipt,
)
from openadapt.pool import (
    CommandResult,
    PoolError,
    pool_down,
    pool_status,
    pool_up,
    resolve_container_platform,
)
from openadapt.qualify_local import LOCAL_QUALIFY_SCHEMA, qualify_local
from openadapt.support_bundle import (
    MANIFEST_FILENAME,
    SUPPORT_BUNDLE_SCHEMA,
    create_support_bundle,
    verify_support_bundle,
)
from openadapt.trust_init import (
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
    TRUST_METADATA_SCHEMA,
    TrustInitError,
    init_trust_key,
    load_private_key,
    load_public_key,
)


class _InMemoryKeyStore:
    name = "mock-keychain"

    def __init__(self, available: bool = True) -> None:
        self._available = available
        self._data: dict[str, str] = {}

    def is_available(self) -> bool:
        return self._available

    def store(self, account: str, secret: str) -> None:
        self._data[account] = secret

    def load(self, account: str) -> Optional[str]:
        return self._data.get(account)


# =============================================================================
# 1. openadapt trust init
# =============================================================================


def test_trust_init_file_storage_permissions_and_no_overwrite(tmp_path: Path) -> None:
    key_dir = tmp_path / "user-trust"
    result = init_trust_key(key_dir=key_dir, storage="file", owner="clinic-ops")

    assert result.algorithm == "Ed25519"
    assert result.storage_backend == "file"
    assert result.key_id.startswith("ed25519:")
    assert len(result.fingerprint_sha256) == 64

    priv_path = key_dir / PRIVATE_KEY_FILENAME
    pub_path = key_dir / PUBLIC_KEY_FILENAME
    assert priv_path.is_file()
    assert pub_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(priv_path.stat().st_mode) == 0o600

    # Must refuse overwrite without --force.
    with pytest.raises(TrustInitError, match="--force"):
        init_trust_key(key_dir=key_dir, storage="file")

    # With force=True, rotates key cleanly.
    rotated = init_trust_key(key_dir=key_dir, storage="file", force=True)
    assert rotated.key_id != result.key_id

    # Private and public keys load and match.
    priv = load_private_key(key_dir)
    pub = load_public_key(key_dir)
    sig = priv.sign(b"test-message")
    pub.verify(sig, b"test-message")


def test_trust_init_refuses_bundle_directory(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "intake-workflow"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.json").write_text('{"name": "intake"}\n', encoding="utf-8")

    with pytest.raises(TrustInitError, match="mutable bundle directory"):
        init_trust_key(key_dir=bundle_dir / "trust", storage="file")

    dot_bundle = tmp_path / "my-app.bundle" / "keys"
    with pytest.raises(TrustInitError, match="mutable bundle directory"):
        init_trust_key(key_dir=dot_bundle, storage="file")


def test_trust_init_keychain_storage_and_cli_json(tmp_path: Path) -> None:
    store = _InMemoryKeyStore(available=True)
    key_dir = tmp_path / "trust-kc"
    result = init_trust_key(key_dir=key_dir, storage="keychain", keystore=store)

    assert result.storage_backend == "mock-keychain"
    assert result.private_key_path is None
    assert not (key_dir / PRIVATE_KEY_FILENAME).exists()
    assert (key_dir / PUBLIC_KEY_FILENAME).is_file()

    priv = load_private_key(key_dir, keystore=store)
    pub = load_public_key(key_dir)
    pub.verify(priv.sign(b"kc-payload"), b"kc-payload")

    # Unavailable keychain fails when storage="keychain".
    with pytest.raises(TrustInitError, match="unavailable"):
        init_trust_key(
            key_dir=tmp_path / "trust-fail",
            storage="keychain",
            keystore=_InMemoryKeyStore(available=False),
        )

    # CLI --json output works end-to-end.
    cli_dir = tmp_path / "cli-trust"
    runner = CliRunner()
    res = runner.invoke(
        cli.main,
        ["trust", "init", "--key-dir", str(cli_dir), "--storage", "file", "--json"],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["schema"] == TRUST_METADATA_SCHEMA
    assert doc["algorithm"] == "Ed25519"
    assert doc["storage_backend"] == "file"


# =============================================================================
# 2. openadapt qualify --local
# =============================================================================


def test_qualify_local_autosave_and_explicit_save(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "inline-app-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.json").write_text(
        json.dumps(
            {
                "name": "inline-form",
                "steps": [{"kind": "fill", "field": "status_code"}],
                "fields": ["status_code", "effective_date"],
                "readback": {
                    "fields": {
                        "status_code": {"persisted": True},
                        "effective_date": {"persisted": True},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # Explicit save mode fails because there is no Save button click step.
    explicit = qualify_local(bundle_dir)
    assert explicit.qualified is False
    assert explicit.requires_save_button is True

    # Autosave / inline-save mode passes on persisted readback without a Save button.
    autosave_report = qualify_local(bundle_dir, autosave=True)
    assert autosave_report.qualified is True
    assert autosave_report.save_mode == "autosave"
    assert autosave_report.requires_save_button is False
    assert autosave_report.persisted_fields_verified == ("status_code", "effective_date")

    # CLI invocation via `openadapt qualify --local --inline-save --json`.
    runner = CliRunner()
    res = runner.invoke(
        cli.main,
        ["qualify", "--local", "--inline-save", "--json", str(bundle_dir)],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["schema"] == LOCAL_QUALIFY_SCHEMA
    assert doc["qualified"] is True
    assert doc["save_mode"] == "inline-save"
    assert doc["requires_save_button"] is False


def test_qualify_without_local_forwards_to_flow_unchanged(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)

    runner = CliRunner()
    res = runner.invoke(cli.main, ["qualify", "--", "bundle-dir", "--json"])
    assert res.exit_code == 0
    assert seen == [["qualify", "--", "bundle-dir", "--json"]]


# =============================================================================
# 3. openadapt cohort-receipt
# =============================================================================


def test_cohort_receipt_zero_phi_merkle_sign_and_verify(tmp_path: Path) -> None:
    key_dir = tmp_path / "trust"
    init_trust_key(key_dir=key_dir, storage="file")

    # Create a worklist containing sensitive raw IDs/PHI fields that must NOT leak.
    worklist_path = tmp_path / "worklist.json"
    worklist_path.write_text(
        json.dumps(
            {
                "cohort_id": "batch-2026-11-28",
                "pagination_complete": True,
                "items": [
                    {
                        "item_id": "MRN-998877-ALICE-SMITH",
                        "patient_name": "Alice Smith",
                        "ssn": "123-45-6789",
                        "status": "VERIFIED",
                        "receipt_digest": "a" * 64,
                    },
                    {
                        "item_id": "MRN-112233-BOB-JONES",
                        "patient_name": "Bob Jones",
                        "status": "VERIFIED",
                        "receipt_digest": "b" * 64,
                    },
                    {
                        "item_id": "MRN-445566-CAROL-LEE",
                        "status": "HALTED_FOR_HUMAN",
                        "receipt_digest": "c" * 64,
                    },
                    {
                        "item_id": "MRN-778899-DAN-RAY",
                        # Missing status -> counted as silently_dropped
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # Create an audit_sample_v1 manifest using openadapt.audit_sample.
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_id": "MRN-998877-ALICE-SMITH",
                        "workflow": "claims",
                        "status": "VERIFIED",
                        "timestamp": "2026-11-28T00:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "run_id": "MRN-112233-BOB-JONES",
                        "workflow": "claims",
                        "status": "VERIFIED",
                        "timestamp": "2026-11-28T00:01:00Z",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sample_res = sample_runs(ledger_path, rate=1.0, seed="fixed-seed")
    sample_res.manifest["samples"][0]["spot_check"]["verdict"] = "AGREE"
    sample_res.manifest["samples"][1]["spot_check"]["verdict"] = "DISAGREE"
    audit_path = tmp_path / "audit_sample.json"
    write_sample_manifest(sample_res.manifest, audit_path)

    manifest = build_cohort_receipt(
        worklist_path,
        audit_sample_path=audit_path,
        key_dir=key_dir,
    )
    assert manifest["schema"] == COHORT_RECEIPT_SCHEMA
    assert manifest["phi_free"] is True
    assert manifest["summary"] == {
        "total_expected": 4,
        "verified": 2,
        "halted_for_human": 1,
        "silently_dropped": 1,
        "pagination_complete": True,
        "field_agreement_rate": 0.5,
    }
    assert manifest["audit_sample"]["schema"] == AUDIT_SAMPLE_SCHEMA
    assert manifest["merkle"]["leaf_count"] == 4
    assert manifest["merkle"]["root"] == compute_merkle_root(
        manifest["merkle"]["leaf_digests"]
    )

    # Zero PHI guarantee: raw names, SSN, MRNs must not appear anywhere in manifest JSON.
    serialized = json.dumps(manifest)
    for forbidden in ("Alice Smith", "Bob Jones", "123-45-6789", "MRN-998877"):
        assert forbidden not in serialized

    # Verify passes against trust key_dir.
    ver = verify_cohort_receipt(manifest, key_dir=key_dir)
    assert ver.ok is True

    # Tampering with summary fails signature verification.
    tampered = json.loads(serialized)
    tampered["summary"]["silently_dropped"] = 0
    tampered["summary"]["verified"] = 3
    ver_tampered = verify_cohort_receipt(tampered, key_dir=key_dir)
    assert ver_tampered.ok is False

    # CLI build + --verify round-trip.
    out_file = tmp_path / "cohort.receipt.json"
    runner = CliRunner()
    build_cli = runner.invoke(
        cli.main,
        [
            "cohort-receipt",
            "--worklist",
            str(worklist_path),
            "--audit-sample",
            str(audit_path),
            "--key-dir",
            str(key_dir),
            "--out",
            str(out_file),
            "--json",
        ],
    )
    assert build_cli.exit_code == 0, build_cli.output
    assert out_file.is_file()

    verify_cli = runner.invoke(
        cli.main,
        [
            "cohort-receipt",
            "--verify",
            str(out_file),
            "--key-dir",
            str(key_dir),
            "--json",
        ],
    )
    assert verify_cli.exit_code == 0, verify_cli.output
    assert json.loads(verify_cli.stdout)["ok"] is True


# =============================================================================
# 4. openadapt pool
# =============================================================================


def test_pool_apple_silicon_platform_and_lifecycle(tmp_path: Path) -> None:
    assert (
        resolve_container_platform("auto", host_system="Darwin", host_machine="arm64")
        == "linux/amd64"
    )
    assert (
        resolve_container_platform(
            "linux/arm64", host_system="Darwin", host_machine="arm64"
        )
        == "linux/arm64"
    )

    calls: list[list[str]] = []

    def fake_runner(argv: Sequence[str]) -> CommandResult:
        cmd = list(argv)
        calls.append(cmd)
        if cmd[1] == "run":
            return CommandResult(0, stdout="cid-12345\n")
        if cmd[1] == "ps":
            return CommandResult(
                0,
                stdout=(
                    "clinic-pool-worker-1\trunning\tUp 10 seconds (healthy)\tcid-1\n"
                    "clinic-pool-worker-2\trunning\tUp 9 seconds (healthy)\tcid-2\n"
                ),
            )
        if cmd[1] == "rm":
            return CommandResult(0, stdout=cmd[-1] + "\n")
        return CommandResult(0)

    state_dir = tmp_path / "pool-state"

    # Dry-run produces full command plan with zero subprocess calls.
    dry_plan = pool_up(
        workers=2,
        backend="xfreerdp",
        pool_name="clinic-pool",
        runtime="docker",
        container_platform="linux/amd64",
        dry_run=True,
        state_dir=state_dir,
        runner=fake_runner,
    )
    assert dry_plan.dry_run is True
    assert len(dry_plan.workers) == 2
    assert calls == []
    assert "--platform" in dry_plan.workers[0].command
    assert "linux/amd64" in dry_plan.workers[0].command
    assert "xfreerdp3" in dry_plan.workers[0].healthcheck["test"][1]

    # Live run executes docker run for each worker and persists state.
    live_plan = pool_up(
        workers=2,
        backend="xfreerdp",
        pool_name="clinic-pool",
        runtime="docker",
        container_platform="linux/amd64",
        dry_run=False,
        state_dir=state_dir,
        runner=fake_runner,
    )
    assert len(live_plan.started_containers) == 2
    assert len(calls) == 2
    assert (state_dir / "clinic-pool.json").is_file()

    # Status parses healthy containers.
    st = pool_status(
        pool_name="clinic-pool",
        runtime="docker",
        state_dir=state_dir,
        runner=fake_runner,
    )
    assert st["running"] == 2
    assert st["containers"][0]["health"] == "healthy"

    # Down removes both containers and cleans state file.
    down = pool_down(
        pool_name="clinic-pool",
        runtime="docker",
        state_dir=state_dir,
        runner=fake_runner,
    )
    assert down["removed"] == ["clinic-pool-worker-1", "clinic-pool-worker-2"]
    assert not (state_dir / "clinic-pool.json").exists()

    # Invalid worker count raises PoolError.
    with pytest.raises(PoolError):
        pool_up(workers=0, dry_run=True)

    # CLI dry-run JSON test.
    runner = CliRunner()
    res = runner.invoke(
        cli.main,
        [
            "pool",
            "up",
            "--workers",
            "3",
            "--backend",
            "xfreerdp",
            "--platform",
            "linux/amd64",
            "--dry-run",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["worker_count"] == 3
    assert doc["platform"] == "linux/amd64"


# =============================================================================
# 5. openadapt support-bundle
# =============================================================================


def test_support_bundle_redaction_determinism_and_verify(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": "run-001",
                "patient_name": "Jane Doe",
                "dob": "1980-05-12",
                "notes": "Contact jane.doe@example.org or 555-867-5309, SSN 123-45-6789, MRN:A99881",
                "status": "VERIFIED",
            }
        ),
        encoding="utf-8",
    )
    fake_bearer = "abcd" * 4
    fake_api_key = "sk-" + "placeholder" + "12345"
    (run_dir / "trace.log").write_text(
        f"Authorization: Bearer {fake_bearer}\napi_key={fake_api_key}\n",
        encoding="utf-8",
    )
    # Excluded files that must never enter the support bundle:
    (run_dir / "receipt.private.json").write_text('{"openings": "secret"}\n', encoding="utf-8")
    (run_dir / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\nraw-pixels")
    (run_dir / "signing.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

    out_a = tmp_path / "bundle-a.approved.zip"
    out_b = tmp_path / "bundle-b.approved.zip"
    res_a = create_support_bundle(run_dir, out_path=out_a)
    res_b = create_support_bundle(run_dir, out_path=out_b)

    # Byte-for-byte deterministic zip output.
    assert res_a.bundle_sha256 == res_b.bundle_sha256
    assert out_a.read_bytes() == out_b.read_bytes()
    assert res_a.excluded_count == 3
    assert set(res_a.included_files) == {"report.json", "trace.log"}

    with zipfile.ZipFile(out_a, "r") as zf:
        names = set(zf.namelist())
        assert names == {"report.json", "trace.log", MANIFEST_FILENAME}
        report_text = zf.read("report.json").decode("utf-8")
        trace_text = zf.read("trace.log").decode("utf-8")
        manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))

    assert manifest["schema"] == SUPPORT_BUNDLE_SCHEMA
    for sensitive in (
        "Jane Doe",
        "1980-05-12",
        "jane.doe@example.org",
        "555-867-5309",
        "123-45-6789",
        "MRN:A99881",
        fake_bearer,
        fake_api_key,
    ):
        assert sensitive not in report_text
        assert sensitive not in trace_text

    # Verify passes on untampered archive.
    ver = verify_support_bundle(out_a)
    assert ver.ok is True
    assert ver.verified_files == 2

    # Tampering with a file inside the zip fails SHA-256 verification.
    tampered_zip = tmp_path / "tampered.approved.zip"
    with zipfile.ZipFile(out_a, "r") as src, zipfile.ZipFile(tampered_zip, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "trace.log":
                data = data + b"tampered\n"
            dst.writestr(item, data)
    ver_bad = verify_support_bundle(tampered_zip)
    assert ver_bad.ok is False
    assert "SHA-256 mismatch" in ver_bad.reason

    # Verify openadapt-privacy hook is used when installed.
    fake_privacy = types.SimpleNamespace(
        scrub_text=lambda text: text.replace("VERIFIED", "PRIVACY_SCRUBBED")
    )
    monkeypatch.setitem(sys.modules, "openadapt_privacy", fake_privacy)
    out_priv = tmp_path / "priv.approved.zip"
    res_priv = create_support_bundle(run_dir, out_path=out_priv)
    assert res_priv.redaction_engine == "openadapt-privacy"
    with zipfile.ZipFile(out_priv, "r") as zf:
        assert "PRIVACY_SCRUBBED" in zf.read("report.json").decode("utf-8")

    # CLI round-trip (create + --verify).
    runner = CliRunner()
    cli_out = tmp_path / "cli-out.approved.zip"
    create_res = runner.invoke(
        cli.main,
        ["support-bundle", str(run_dir), "--out", str(cli_out), "--json"],
    )
    assert create_res.exit_code == 0, create_res.output
    assert cli_out.is_file()

    verify_res = runner.invoke(
        cli.main,
        ["support-bundle", "--verify", str(cli_out), "--json"],
    )
    assert verify_res.exit_code == 0, verify_res.output
    assert json.loads(verify_res.stdout)["ok"] is True
