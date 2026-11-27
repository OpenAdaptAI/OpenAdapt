"""Unit and CLI tests for ``openadapt audit sample``."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt.audit_sample import (
    AUDIT_SAMPLE_SCHEMA,
    AuditSampleError,
    compute_sample_size,
    format_sample_table,
    load_run_records,
    parse_since,
    sample_runs,
    validate_sampling_bounds,
)
from openadapt.cli import main as cli_main


def _make_ledger_row(
    run_id: str,
    *,
    status: str = "VERIFIED",
    workflow: str = "update_record",
    ended_at: str = "2026-11-26T12:00:00Z",
    digest: str | None = None,
    channel: str = "independent_readback",
    fields: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "schema": "openadapt.run-ledger/v1",
        "row_id": run_id,
        "task_id": f"task-{run_id}",
        "task_type": workflow,
        "executor_kind": "compiled",
        "executor_label": workflow,
        "started_at": ended_at,
        "ended_at": ended_at,
        "outcome": status,
        "outcome_reason": "readback_verified"
        if status == "VERIFIED"
        else "crashed_no_write",
        "readback_tier": channel if status == "VERIFIED" else None,
        "receipt_digest": digest or f"{run_id.encode().hex():0<64}"[:64],
        "fields_written": fields
        if fields is not None
        else [
            {"name": "status", "check": "matches"},
            {"name": "amount", "check": "matches"},
        ],
    }


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _write_dsse_receipt_folder(
    root: Path,
    run_id: str,
    *,
    outcome: str = "VERIFIED",
    started_hour: str = "2026-11-26T14:00:00Z",
    ended_at: str = "2026-11-26T14:15:00Z",
    workflow: str = "record_update",
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "report.json", "digest": {"sha256": "a" * 64}}],
        "predicateType": "https://openadapt.ai/record/run/v1",
        "predicate": {
            "program": {"id": "prog-0123456789abcdef", "version_sha256": "b" * 64},
            "started_hour": started_hour,
            "outcome": outcome,
            "steps": {"total": 3, "ok": 3},
            "lines": [
                {
                    "step": 2,
                    "target_kind": "api",
                    "operation": "write",
                    "check": "confirmed" if outcome == "VERIFIED" else "not_checked",
                    "strength": "read_back" if outcome == "VERIFIED" else "none",
                    "not_checked": [],
                }
            ],
            "checks": {
                "identity": {"required": 1, "confirmed": 1},
                "postconditions": {"required": 1, "confirmed": 1},
                "effects": {"required": 1, "confirmed": 1},
                "authorization": {"required": 0, "confirmed": 0},
                "lines_confirmed": 1,
                "lines_refuted": 0,
                "lines_not_checked": 0,
            },
            "model_calls": 0,
            "disclosure": {
                "version": "openadapt.public-disclosure/v1",
                "commitments": {},
            },
        },
    }
    payload_bytes = json.dumps(statement, sort_keys=True).encode("utf-8")
    envelope = {
        "payloadType": "application/vnd.in-toto+json",
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
        "signatures": [{"keyid": "seal-ed25519-0123456789abcdef", "sig": "AAAA"}],
    }
    (run_dir / "receipt.json").write_text(json.dumps(envelope), encoding="utf-8")
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow": workflow,
                "ended_at": ended_at,
                "outcome": outcome,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "receipt.private.json").write_text(
        json.dumps({"schema": "openadapt.receipt-openings/v1", "openings": {}}),
        encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# Unit tests: validation & math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_rate", [0.0, -0.05, 1.01, 2.5, float("nan"), float("inf")]
)
def test_rate_bounds_rejected(bad_rate: float) -> None:
    with pytest.raises(AuditSampleError, match="--rate"):
        validate_sampling_bounds(rate=bad_rate)


def test_count_and_min_max_bounds_rejected() -> None:
    with pytest.raises(AuditSampleError, match="--count"):
        validate_sampling_bounds(rate=0.05, count=0)
    with pytest.raises(AuditSampleError, match="--min-samples"):
        validate_sampling_bounds(rate=0.05, min_samples=-1)
    with pytest.raises(AuditSampleError, match="--max-samples"):
        validate_sampling_bounds(rate=0.05, max_samples=0)
    with pytest.raises(AuditSampleError, match="cannot exceed"):
        validate_sampling_bounds(rate=0.05, min_samples=10, max_samples=5)


def test_compute_sample_size_precision_and_caps() -> None:
    # Avoid float rounding overshoot (100 * 0.07 == 7.000000000000001).
    assert compute_sample_size(100, rate=0.07) == 7
    assert compute_sample_size(100, rate=0.05) == 5
    assert compute_sample_size(10, rate=0.05) == 1
    assert compute_sample_size(0, rate=0.05) == 0
    assert compute_sample_size(100, rate=0.01, min_samples=3) == 3
    assert compute_sample_size(2, rate=0.01, min_samples=5) == 2
    assert compute_sample_size(100, rate=0.5, max_samples=10) == 10
    assert compute_sample_size(100, rate=0.05, count=12) == 12
    assert compute_sample_size(4, rate=0.05, count=12) == 4


def test_parse_since_relative_and_iso() -> None:
    now = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("30m", now=now) == now - timedelta(minutes=30)
    assert parse_since("2w", now=now) == now - timedelta(weeks=2)
    assert parse_since("2026-11-20", now=now) == datetime(
        2026, 11, 20, 0, 0, 0, tzinfo=timezone.utc
    )
    assert parse_since("2026-11-20T12:30:00Z", now=now) == datetime(
        2026, 11, 20, 12, 30, 0, tzinfo=timezone.utc
    )
    with pytest.raises(AuditSampleError, match="--since"):
        parse_since("not-a-date", now=now)
    with pytest.raises(AuditSampleError, match="positive"):
        parse_since("0d", now=now)


# ---------------------------------------------------------------------------
# Unit tests: loading, filtering, determinism, deduplication
# ---------------------------------------------------------------------------


def test_empty_and_missing_directories(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    res_empty = sample_runs(empty_dir, rate=0.05, seed="s1")
    assert res_empty.manifest["schema"] == AUDIT_SAMPLE_SCHEMA
    assert res_empty.manifest["population"]["eligible"] == 0
    assert res_empty.manifest["population"]["sampled"] == 0
    assert res_empty.samples == ()

    missing_dir = tmp_path / "does-not-exist"
    res_missing = sample_runs(missing_dir, rate=0.05, seed="s1")
    assert res_missing.manifest["population"]["total_loaded"] == 0
    assert res_missing.samples == ()


def test_non_verified_runs_excluded_by_default(tmp_path: Path) -> None:
    rows = [
        _make_ledger_row("run-v1", status="VERIFIED"),
        _make_ledger_row("run-v2", status="VERIFIED"),
        _make_ledger_row("run-f1", status="FAILED"),
        _make_ledger_row("run-s1", status="STOPPED"),
        _make_ledger_row("run-e1", status="ESCALATED"),
        _make_ledger_row("run-c1", status="CAUGHT_ERROR"),
        _make_ledger_row("run-u1", status="UNKNOWN"),
    ]
    ledger = _write_jsonl(tmp_path / "ledger.jsonl", rows)

    default_res = sample_runs(ledger, rate=1.0, seed="fixed")
    assert {r.run_id for r in default_res.samples} == {"run-v1", "run-v2"}
    assert default_res.manifest["population"]["total_loaded"] == 7
    assert default_res.manifest["population"]["status_matched"] == 2

    # Case-insensitive status match
    lower_res = sample_runs(ledger, rate=1.0, status="verified", seed="fixed")
    assert {r.run_id for r in lower_res.samples} == {"run-v1", "run-v2"}

    # Explicit non-verified status filter
    failed_res = sample_runs(ledger, rate=1.0, status="failed", seed="fixed")
    assert {r.run_id for r in failed_res.samples} == {"run-f1"}


def test_deterministic_seed_reproducibility_and_cryptographic_default(
    tmp_path: Path,
) -> None:
    rows = [
        _make_ledger_row(
            f"run-{i:03d}", ended_at=f"2026-11-26T12:{i // 60:02d}:{i % 60:02d}Z"
        )
        for i in range(100)
    ]
    ledger = _write_jsonl(tmp_path / "ledger.jsonl", rows)

    res_a = sample_runs(ledger, rate=0.05, seed="audit-2026-w48")
    res_b = sample_runs(ledger, rate=0.05, seed="audit-2026-w48")
    res_c = sample_runs(ledger, rate=0.05, seed="audit-2026-w49")

    ids_a = [r.run_id for r in res_a.samples]
    ids_b = [r.run_id for r in res_b.samples]
    ids_c = [r.run_id for r in res_c.samples]

    assert len(ids_a) == 5
    assert ids_a == ids_b
    assert ids_a != ids_c
    assert res_a.manifest["criteria"]["sampling_method"] == "deterministic_seeded"

    # Without seed: uses cryptographic uniform sampling
    res_rand = sample_runs(ledger, rate=0.05)
    assert len(res_rand.samples) == 5
    assert len({r.run_id for r in res_rand.samples}) == 5
    assert res_rand.manifest["criteria"]["sampling_method"] == "cryptographic_uniform"


def test_since_window_filtering(tmp_path: Path) -> None:
    now = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
    rows = [
        _make_ledger_row("run-old", ended_at="2026-11-10T10:00:00Z"),
        _make_ledger_row("run-week", ended_at="2026-11-22T10:00:00Z"),
        _make_ledger_row("run-today", ended_at="2026-11-27T12:00:00Z"),
    ]
    ledger = _write_jsonl(tmp_path / "ledger.jsonl", rows)

    res_7d = sample_runs(ledger, rate=1.0, since="7d", now=now, seed="s")
    assert {r.run_id for r in res_7d.samples} == {"run-week", "run-today"}

    res_24h = sample_runs(ledger, rate=1.0, since="24h", now=now, seed="s")
    assert {r.run_id for r in res_24h.samples} == {"run-today"}

    res_iso = sample_runs(ledger, rate=1.0, since="2026-11-25", now=now, seed="s")
    assert {r.run_id for r in res_iso.samples} == {"run-today"}


def test_exclude_already_sampled_deduplication(tmp_path: Path) -> None:
    rows = [_make_ledger_row(f"run-{i:02d}") for i in range(10)]
    ledger = _write_jsonl(tmp_path / "ledger.jsonl", rows)

    # First scheduled run (exclude file doesn't exist yet -> handled cleanly)
    missing_prior = tmp_path / "nonexistent_prior.json"
    first = sample_runs(
        ledger,
        count=4,
        seed="day1",
        exclude_already_sampled=(missing_prior,),
    )
    first_ids = {r.run_id for r in first.samples}
    assert len(first_ids) == 4

    prior_json = tmp_path / "day1_manifest.json"
    prior_json.write_text(json.dumps(first.manifest), encoding="utf-8")

    # Second scheduled run excludes all 4 runs from day 1
    second = sample_runs(
        ledger,
        count=4,
        seed="day2",
        exclude_already_sampled=(prior_json,),
    )
    second_ids = {r.run_id for r in second.samples}
    assert len(second_ids) == 4
    assert first_ids.isdisjoint(second_ids)
    assert second.manifest["population"]["excluded_already_sampled"] == 4
    assert second.manifest["population"]["eligible"] == 6

    # Third run excluding both JSON and JSONL prior manifests gets the remaining 2 runs
    prior_jsonl = tmp_path / "day2_manifest.jsonl"
    prior_jsonl.write_text(
        "\n".join(json.dumps(s) for s in second.manifest["samples"]) + "\n",
        encoding="utf-8",
    )
    third = sample_runs(
        ledger,
        count=4,
        seed="day3",
        exclude_already_sampled=(prior_json, prior_jsonl),
    )
    third_ids = {r.run_id for r in third.samples}
    assert len(third_ids) == 2
    assert third_ids.isdisjoint(first_ids | second_ids)


def test_dsse_receipt_folder_loading_skips_private_sidecar(tmp_path: Path) -> None:
    _write_dsse_receipt_folder(tmp_path, "run-dsse-1", outcome="VERIFIED")
    _write_dsse_receipt_folder(tmp_path, "run-dsse-2", outcome="STOPPED")

    records = load_run_records(tmp_path)
    assert len(records) == 2
    by_id = {r.run_id: r for r in records}
    assert by_id["run-dsse-1"].status == "VERIFIED"
    assert by_id["run-dsse-1"].verification_channel == "read_back"
    assert by_id["run-dsse-1"].workflow == "record_update"
    assert by_id["run-dsse-2"].status == "STOPPED"


# ---------------------------------------------------------------------------
# CLI tests: openadapt audit sample
# ---------------------------------------------------------------------------


def test_cli_audit_sample_table_json_and_out(tmp_path: Path) -> None:
    rows = [_make_ledger_row(f"run-{i:03d}") for i in range(20)]
    ledger = _write_jsonl(tmp_path / "receipts.jsonl", rows)
    out_json = tmp_path / "spotcheck.json"
    out_jsonl = tmp_path / "spotcheck.jsonl"

    runner = CliRunner()

    # Table output + --out JSON manifest
    table_res = runner.invoke(
        cli_main,
        [
            "audit",
            "sample",
            "--receipts-dir",
            str(ledger),
            "--rate",
            "0.1",
            "--seed",
            "42",
            "--out",
            str(out_json),
        ],
    )
    assert table_res.exit_code == 0, table_res.output
    assert "Audit sample (VERIFIED): 2 of 20 eligible runs" in table_res.output
    assert "RUN ID" in table_res.output
    assert out_json.is_file()

    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert manifest["schema"] == AUDIT_SAMPLE_SCHEMA
    assert len(manifest["samples"]) == 2
    sample0 = manifest["samples"][0]
    assert sample0["spot_check"] == {
        "verdict": None,
        "reviewer": None,
        "reviewed_at": None,
        "findings": "",
        "unmonitored_side_effects": None,
    }
    assert sample0["verification_channel"] == "independent_readback"
    assert sample0["fields_checked"] == ["status", "amount"]

    # --input alias + --json + --out JSONL + --exclude-already-sampled
    json_res = runner.invoke(
        cli_main,
        [
            "audit",
            "sample",
            "--input",
            str(ledger),
            "--count",
            "3",
            "--seed",
            "42",
            "--exclude-already-sampled",
            str(out_json),
            "--out",
            str(out_jsonl),
            "--json",
        ],
    )
    assert json_res.exit_code == 0, json_res.output
    stdout_manifest = json.loads(json_res.output)
    assert stdout_manifest["population"]["excluded_already_sampled"] == 2
    assert stdout_manifest["population"]["sampled"] == 3
    first_ids = {s["run_id"] for s in manifest["samples"]}
    second_ids = {s["run_id"] for s in stdout_manifest["samples"]}
    assert first_ids.isdisjoint(second_ids)

    jsonl_lines = [
        json.loads(line)
        for line in out_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(jsonl_lines) == 3
    assert all(line["schema"] == AUDIT_SAMPLE_SCHEMA for line in jsonl_lines)


def test_cli_audit_sample_rejects_invalid_rate(tmp_path: Path) -> None:
    runner = CliRunner()
    for bad in ("0", "-0.1", "1.5"):
        res = runner.invoke(
            cli_main,
            ["audit", "sample", "--receipts-dir", str(tmp_path), "--rate", bad],
        )
        assert res.exit_code != 0
        assert "--rate" in res.output


def test_format_sample_table_empty_message() -> None:
    manifest = {
        "criteria": {"status": ["VERIFIED"], "rate": 0.05},
        "population": {"eligible": 0, "sampled": 0},
        "samples": [],
    }
    rendered = format_sample_table(manifest)
    assert "0 of 0 eligible runs" in rendered
    assert "No eligible runs to sample." in rendered
