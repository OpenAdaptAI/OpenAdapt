"""Statistical random spot-check sampling over verified run receipts.

Operations and compliance teams use ``openadapt audit sample`` to draw a random
sample of ``VERIFIED`` runs (for example, 1% to 5%) for human spot-checking so
gaps in verifier coverage (such as an unmonitored sibling table) are caught
even when automated checks pass.

Zero per-app logic lives here. The loader reads standard OpenAdapt record
formats (DSSE ``record/run/v1`` receipts, ``openadapt.run-ledger/v1`` JSONL
ledgers, ``openadapt.run-receipt/v2`` summaries, and generic JSON/JSONL run
records).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

AUDIT_SAMPLE_SCHEMA = "audit_sample_v1"
DEFAULT_RECEIPTS_DIR = Path.home() / ".openadapt" / "verify"
DEFAULT_RATE = 0.05
DEFAULT_STATUS = "VERIFIED"

_RELATIVE_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])\s*$")
_SKIP_FILENAMES = frozenset(
    {
        "receipt.private.json",
        "seal.private.json",
        "keys.json",
        "setup.json",
    }
)


class AuditSampleError(ValueError):
    """Raised when audit sampling arguments or inputs are invalid."""


@dataclass(frozen=True)
class RunRecord:
    """Normalized metadata for one run record eligible for spot-check sampling."""

    run_id: str
    workflow: str
    timestamp: Optional[str]
    status: str
    receipt_digest: str
    source_path: str
    verification_channel: str
    fields_checked: tuple[str, ...] = ()

    def sort_key(self) -> tuple[str, str, str]:
        return (self.timestamp or "", self.run_id, self.receipt_digest)


@dataclass(frozen=True)
class SampleResult:
    """Result of one statistical audit sampling pass."""

    manifest: dict[str, Any]
    samples: tuple[RunRecord, ...] = field(default_factory=tuple)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 date or timestamp into an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_since(since: str, *, now: Optional[datetime] = None) -> datetime:
    """Parse ``--since`` as a relative window (``7d``, ``24h``) or ISO date/time."""
    if not isinstance(since, str) or not since.strip():
        raise AuditSampleError(
            "--since must be a non-empty date or duration (e.g. 7d, 24h, 2026-11-01)."
        )
    ref = (now or _utc_now()).astimezone(timezone.utc)
    match = _RELATIVE_WINDOW_RE.match(since)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if amount <= 0:
            raise AuditSampleError(f"--since duration must be positive, got {since!r}.")
        delta_map = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }
        return ref - delta_map[unit]
    parsed = parse_timestamp(since)
    if parsed is None:
        raise AuditSampleError(
            f"Invalid --since value {since!r}. Use an ISO date/timestamp (e.g. 2026-11-01) "
            "or relative window (e.g. 24h, 7d, 2w)."
        )
    return parsed


def validate_sampling_bounds(
    *,
    rate: float,
    count: Optional[int] = None,
    min_samples: Optional[int] = None,
    max_samples: Optional[int] = None,
) -> None:
    """Validate sampling rate and sample-count bounds."""
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        raise AuditSampleError(f"--rate must be a number in (0.0, 1.0], got {rate!r}.")
    if not math.isfinite(rate) or rate <= 0.0 or rate > 1.0:
        raise AuditSampleError(f"--rate must be in (0.0, 1.0], got {rate}.")
    if count is not None and count < 1:
        raise AuditSampleError(f"--count must be at least 1, got {count}.")
    if min_samples is not None and min_samples < 0:
        raise AuditSampleError(f"--min-samples must be at least 0, got {min_samples}.")
    if max_samples is not None and max_samples < 1:
        raise AuditSampleError(f"--max-samples must be at least 1, got {max_samples}.")
    if (
        min_samples is not None
        and max_samples is not None
        and min_samples > max_samples
    ):
        raise AuditSampleError(
            f"--min-samples ({min_samples}) cannot exceed --max-samples ({max_samples})."
        )


def compute_sample_size(
    eligible_count: int,
    *,
    rate: float = DEFAULT_RATE,
    count: Optional[int] = None,
    min_samples: Optional[int] = None,
    max_samples: Optional[int] = None,
) -> int:
    """Compute the number of runs to sample from ``eligible_count``."""
    validate_sampling_bounds(
        rate=rate, count=count, min_samples=min_samples, max_samples=max_samples
    )
    if eligible_count <= 0:
        return 0
    if count is not None:
        target = count
    else:
        target = math.ceil(round(eligible_count * rate, 12))
    if min_samples is not None:
        target = max(target, min_samples)
    if max_samples is not None:
        target = min(target, max_samples)
    return min(target, eligible_count)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(obj: Any) -> str:
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _normalize_fields_checked(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("field")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return tuple(names)


def _extract_dsse_record(
    doc: dict[str, Any], source: Path, raw_bytes: bytes
) -> Optional[RunRecord]:
    """Extract normalized metadata from a DSSE ``record/run/v1`` receipt or card."""
    envelope = doc.get("envelope") if isinstance(doc.get("envelope"), dict) else doc
    payload_b64 = envelope.get("payload")
    payload_type = envelope.get("payloadType")
    if (
        not isinstance(payload_b64, str)
        or payload_type != "application/vnd.in-toto+json"
    ):
        return None
    try:
        payload_bytes = base64.b64decode(payload_b64)
        statement = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(statement, dict):
        return None
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        return None

    digest = _sha256_bytes(payload_bytes)
    outcome = str(predicate.get("outcome") or "UNKNOWN").strip().upper()
    program = predicate.get("program")
    workflow = (
        str(program.get("id"))
        if isinstance(program, dict) and program.get("id")
        else "unknown"
    )
    openings = doc.get("openings")
    if isinstance(openings, dict):
        disclosed_name = openings.get("program.name")
        if isinstance(disclosed_name, dict) and isinstance(
            disclosed_name.get("value"), str
        ):
            workflow = disclosed_name["value"]

    run_id = ""
    if source.name == "receipt.json" and source.parent.name:
        run_id = source.parent.name
    if not run_id:
        subjects = statement.get("subject")
        if isinstance(subjects, list) and subjects:
            first = subjects[0]
            if isinstance(first, dict) and isinstance(first.get("digest"), dict):
                sha = first["digest"].get("sha256")
                if isinstance(sha, str) and sha:
                    run_id = sha[:16]
    if not run_id:
        run_id = digest[:16]

    timestamp: Optional[str] = None
    started_hour = predicate.get("started_hour")
    if isinstance(started_hour, str) and started_hour:
        timestamp = started_hour
    report_path = source.parent / "report.json"
    if source.name == "receipt.json" and report_path.is_file():
        try:
            report_doc = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(report_doc, dict):
                for key in ("ended_at", "started_at", "timestamp"):
                    val = report_doc.get(key)
                    if isinstance(val, str) and parse_timestamp(val) is not None:
                        timestamp = val
                        break
                if workflow.startswith("prog-") and isinstance(
                    report_doc.get("workflow"), str
                ):
                    workflow = report_doc["workflow"]
        except (OSError, ValueError):
            pass

    lines = predicate.get("lines")
    channel = "unknown"
    fields: list[str] = []
    if isinstance(lines, list) and lines:
        strengths = sorted(
            {
                str(line.get("strength"))
                for line in lines
                if isinstance(line, dict) and line.get("strength")
            }
        )
        if strengths:
            channel = ",".join(strengths)
        for line in lines:
            if isinstance(line, dict):
                step = line.get("step")
                op = line.get("operation") or "write"
                target = line.get("target_kind") or "target"
                fields.append(f"step_{step}:{target}:{op}")

    evidence_scope = predicate.get("evidence_scope")
    if channel == "unknown" and isinstance(evidence_scope, dict):
        basis = evidence_scope.get("basis") or evidence_scope.get("channel")
        if isinstance(basis, str) and basis:
            channel = basis

    return RunRecord(
        run_id=run_id,
        workflow=workflow,
        timestamp=timestamp,
        status=outcome,
        receipt_digest=digest or _sha256_bytes(raw_bytes),
        source_path=str(source),
        verification_channel=channel,
        fields_checked=tuple(fields),
    )


def _extract_dict_record(
    doc: dict[str, Any], source: Path, raw_bytes: Optional[bytes] = None
) -> Optional[RunRecord]:
    """Extract normalized metadata from a ledger row, run summary, or generic receipt."""
    if not isinstance(doc, dict):
        return None
    dsse = _extract_dsse_record(doc, source, raw_bytes or b"")
    if dsse is not None:
        return dsse

    # Skip audit_sample_v1 manifests if they live beside receipts.
    if doc.get("schema") == AUDIT_SAMPLE_SCHEMA:
        return None

    status_raw = (
        doc.get("outcome")
        or doc.get("status")
        or doc.get("verdict")
        or doc.get("result")
    )
    if not isinstance(status_raw, str) or not status_raw.strip():
        return None
    status = status_raw.strip().upper()

    run_id_raw = (
        doc.get("run_id")
        or doc.get("row_id")
        or doc.get("task_id")
        or doc.get("id")
        or doc.get("correlation_id")
    )
    if isinstance(run_id_raw, str) and run_id_raw.strip():
        run_id = run_id_raw.strip()
    elif source.is_file() and source.stem not in ("receipt", "report", "run-summary"):
        run_id = source.stem
    elif source.parent.name:
        run_id = source.parent.name
    else:
        run_id = _canonical_digest(doc)[:16]

    workflow_raw = (
        doc.get("workflow")
        or doc.get("workflow_name")
        or doc.get("task_type")
        or doc.get("program_name")
        or doc.get("program_id")
        or doc.get("contract")
        or doc.get("executor_label")
        or "unknown"
    )
    workflow = str(workflow_raw).strip() or "unknown"

    timestamp: Optional[str] = None
    for key in (
        "timestamp",
        "ended_at",
        "completed_at",
        "started_at",
        "created_at",
        "started_hour",
    ):
        val = doc.get(key)
        if isinstance(val, str) and val.strip():
            timestamp = val.strip()
            break

    digest_raw = (
        doc.get("receipt_digest")
        or doc.get("receipt_sha256")
        or doc.get("sha256")
        or doc.get("digest")
    )
    if isinstance(digest_raw, str) and digest_raw.strip():
        receipt_digest = digest_raw.strip()
    elif raw_bytes is not None:
        receipt_digest = _sha256_bytes(raw_bytes)
    else:
        receipt_digest = _canonical_digest(doc)

    channel_raw = (
        doc.get("verification_channel")
        or doc.get("readback_tier")
        or doc.get("channel")
        or doc.get("strength")
        or doc.get("outcome_reason")
        or "unknown"
    )
    verification_channel = str(channel_raw).strip() or "unknown"

    fields = _normalize_fields_checked(
        doc.get("fields_checked")
        or doc.get("fields_written")
        or doc.get("checked_fields")
    )

    return RunRecord(
        run_id=run_id,
        workflow=workflow,
        timestamp=timestamp,
        status=status,
        receipt_digest=receipt_digest,
        source_path=str(source),
        verification_channel=verification_channel,
        fields_checked=fields,
    )


def _load_jsonl_file(path: Path) -> list[RunRecord]:
    records: list[RunRecord] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rec = _extract_dict_record(obj, path, stripped.encode("utf-8"))
            if rec is not None:
                records.append(rec)
    return records


def _load_json_file(path: Path) -> list[RunRecord]:
    raw_bytes = path.read_bytes()
    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    if isinstance(doc, dict):
        for list_key in ("runs", "receipts", "items", "records"):
            nested = doc.get(list_key)
            if isinstance(nested, list):
                out: list[RunRecord] = []
                for item in nested:
                    if isinstance(item, dict):
                        rec = _extract_dict_record(item, path)
                        if rec is not None:
                            out.append(rec)
                return out
        rec = _extract_dict_record(doc, path, raw_bytes)
        return [rec] if rec is not None else []
    if isinstance(doc, list):
        out = []
        for item in doc:
            if isinstance(item, dict):
                rec = _extract_dict_record(item, path)
                if rec is not None:
                    out.append(rec)
        return out
    return []


def load_run_records(source: Path) -> list[RunRecord]:
    """Load and normalize run records from a receipts directory or JSON/JSONL file."""
    if not source.exists():
        return []
    raw_records: list[RunRecord] = []
    if source.is_file():
        if source.suffix.lower() == ".jsonl":
            raw_records.extend(_load_jsonl_file(source))
        else:
            raw_records.extend(_load_json_file(source))
    elif source.is_dir():
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            if path.name in _SKIP_FILENAMES:
                continue
            # Avoid reading sibling report.json as a separate record when receipt.json exists.
            if path.name == "report.json" and (path.parent / "receipt.json").is_file():
                continue
            suffix = path.suffix.lower()
            if suffix == ".jsonl":
                raw_records.extend(_load_jsonl_file(path))
            elif suffix == ".json":
                raw_records.extend(_load_json_file(path))

    seen: set[tuple[str, str]] = set()
    unique: list[RunRecord] = []
    for rec in raw_records:
        key = (rec.run_id, rec.receipt_digest)
        if key not in seen:
            seen.add(key)
            unique.append(rec)
    unique.sort(key=lambda r: r.sort_key())
    return unique


def _extract_ids_from_doc(doc: Any, ids: set[str], digests: set[str]) -> None:
    if isinstance(doc, dict):
        if isinstance(doc.get("samples"), list):
            for entry in doc["samples"]:
                _extract_ids_from_doc(entry, ids, digests)
            return
        run_id = doc.get("run_id") or doc.get("row_id") or doc.get("id")
        if isinstance(run_id, str) and run_id.strip():
            ids.add(run_id.strip())
        digest = doc.get("receipt_digest") or doc.get("receipt_sha256")
        if isinstance(digest, str) and digest.strip():
            digests.add(digest.strip())
    elif isinstance(doc, list):
        for entry in doc:
            _extract_ids_from_doc(entry, ids, digests)


def load_already_sampled(paths: Sequence[Path]) -> tuple[set[str], set[str]]:
    """Load previously sampled run IDs and receipt digests from manifests or ledgers.

    Missing paths are treated as empty so first-run scheduled jobs do not fail.
    """
    ids: set[str] = set()
    digests: set[str] = set()

    def _read_file(file_path: Path) -> None:
        text = file_path.read_text(encoding="utf-8")
        if not text.strip():
            return
        if file_path.suffix.lower() == ".jsonl":
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    _extract_ids_from_doc(json.loads(stripped), ids, digests)
                except ValueError:
                    ids.add(stripped)
            return
        try:
            _extract_ids_from_doc(json.loads(text), ids, digests)
            return
        except ValueError:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    ids.add(stripped)

    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            _read_file(path)
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in (
                    ".json",
                    ".jsonl",
                    ".txt",
                ):
                    _read_file(child)
    return ids, digests


def _parse_status_filter(status: str) -> tuple[str, ...]:
    parts = [part.strip().upper() for part in status.split(",") if part.strip()]
    if not parts:
        raise AuditSampleError(
            "--status must be a non-empty status name (e.g. VERIFIED)."
        )
    return tuple(parts)


def _build_sample_entry(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "workflow": record.workflow,
        "timestamp": record.timestamp,
        "status": record.status,
        "receipt_digest": record.receipt_digest,
        "source_path": record.source_path,
        "verification_channel": record.verification_channel,
        "fields_checked": list(record.fields_checked),
        "spot_check": {
            "verdict": None,
            "reviewer": None,
            "reviewed_at": None,
            "findings": "",
            "unmonitored_side_effects": None,
        },
    }


def sample_runs(
    source: Path,
    *,
    rate: float = DEFAULT_RATE,
    count: Optional[int] = None,
    min_samples: Optional[int] = None,
    max_samples: Optional[int] = None,
    since: Optional[str] = None,
    seed: Optional[str | int] = None,
    status: str = DEFAULT_STATUS,
    exclude_already_sampled: Sequence[Path] = (),
    now: Optional[datetime] = None,
) -> SampleResult:
    """Filter run records and draw a uniform random statistical spot-check sample."""
    validate_sampling_bounds(
        rate=rate, count=count, min_samples=min_samples, max_samples=max_samples
    )
    status_filter = _parse_status_filter(status)
    match_all_statuses = any(token in ("*", "ALL") for token in status_filter)
    ref_now = (now or _utc_now()).astimezone(timezone.utc)
    cutoff = parse_since(since, now=ref_now) if since is not None else None

    all_records = load_run_records(source)

    if match_all_statuses:
        status_matched = list(all_records)
    else:
        allowed = set(status_filter)
        status_matched = [r for r in all_records if r.status.upper() in allowed]

    if cutoff is not None:
        since_matched = []
        for rec in status_matched:
            ts = parse_timestamp(rec.timestamp)
            if ts is not None and ts >= cutoff:
                since_matched.append(rec)
    else:
        since_matched = list(status_matched)

    excluded_ids, excluded_digests = load_already_sampled(exclude_already_sampled)
    eligible: list[RunRecord] = []
    excluded_count = 0
    for rec in since_matched:
        if rec.run_id in excluded_ids or rec.receipt_digest in excluded_digests:
            excluded_count += 1
        else:
            eligible.append(rec)

    target_size = compute_sample_size(
        len(eligible),
        rate=rate,
        count=count,
        min_samples=min_samples,
        max_samples=max_samples,
    )

    if target_size == 0:
        chosen: list[RunRecord] = []
    elif seed is not None:
        rng = random.Random(str(seed))
        chosen = rng.sample(eligible, target_size)
    else:
        rng = secrets.SystemRandom()
        chosen = rng.sample(eligible, target_size)

    chosen.sort(key=lambda r: r.sort_key())

    manifest: dict[str, Any] = {
        "schema": AUDIT_SAMPLE_SCHEMA,
        "generated_at": _iso_utc(ref_now),
        "source": str(source),
        "criteria": {
            "status": list(status_filter),
            "rate": rate,
            "count": count,
            "min_samples": min_samples,
            "max_samples": max_samples,
            "since": since,
            "since_cutoff": _iso_utc(cutoff) if cutoff is not None else None,
            "seed": str(seed) if seed is not None else None,
            "sampling_method": (
                "deterministic_seeded" if seed is not None else "cryptographic_uniform"
            ),
            "excluded_ledgers": [str(p) for p in exclude_already_sampled],
        },
        "population": {
            "total_loaded": len(all_records),
            "status_matched": len(status_matched),
            "since_matched": len(since_matched),
            "excluded_already_sampled": excluded_count,
            "eligible": len(eligible),
            "sampled": len(chosen),
        },
        "samples": [_build_sample_entry(rec) for rec in chosen],
    }
    return SampleResult(manifest=manifest, samples=tuple(chosen))


def write_sample_manifest(manifest: dict[str, Any], out_path: Path) -> None:
    """Write ``audit_sample_v1`` manifest to JSON or JSONL."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".jsonl":
        lines: list[str] = []
        for entry in manifest.get("samples", []):
            row = {
                "schema": AUDIT_SAMPLE_SCHEMA,
                "generated_at": manifest.get("generated_at"),
                "seed": (manifest.get("criteria") or {}).get("seed"),
                **entry,
            }
            lines.append(json.dumps(row, sort_keys=True))
        out_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        return
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def format_sample_table(manifest: dict[str, Any]) -> str:
    """Render a plain-English table for operators."""
    pop = manifest.get("population") or {}
    criteria = manifest.get("criteria") or {}
    samples: Iterable[dict[str, Any]] = manifest.get("samples") or []
    sample_list = list(samples)

    rate_pct = f"{float(criteria.get('rate', DEFAULT_RATE)) * 100:.2f}%".rstrip(
        "0"
    ).rstrip(".")
    status_label = ",".join(criteria.get("status") or [DEFAULT_STATUS])
    header_parts = [
        f"Audit sample ({status_label}): {pop.get('sampled', 0)} of {pop.get('eligible', 0)} eligible runs",
        f"rate={rate_pct}%",
    ]
    if criteria.get("count") is not None:
        header_parts.append(f"count={criteria['count']}")
    if criteria.get("since"):
        header_parts.append(f"since={criteria['since']}")
    if criteria.get("seed") is not None:
        header_parts.append(f"seed={criteria['seed']}")
    if pop.get("excluded_already_sampled"):
        header_parts.append(f"excluded={pop['excluded_already_sampled']}")

    lines = [" | ".join(header_parts)]
    if not sample_list:
        lines.append("No eligible runs to sample.")
        return "\n".join(lines)

    columns = ("RUN ID", "WORKFLOW", "TIMESTAMP", "STATUS", "CHANNEL", "DIGEST")
    rows = [
        (
            str(item.get("run_id") or "-"),
            str(item.get("workflow") or "-"),
            str(item.get("timestamp") or "-"),
            str(item.get("status") or "-"),
            str(item.get("verification_channel") or "-"),
            str(item.get("receipt_digest") or "-")[:12],
        )
        for item in sample_list
    ]
    widths = [
        max(len(col), *(len(row[idx]) for row in rows))
        for idx, col in enumerate(columns)
    ]

    def _fmt(row: Sequence[str]) -> str:
        return "  ".join(val.ljust(widths[i]) for i, val in enumerate(row))

    lines.append("")
    lines.append(_fmt(columns))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(_fmt(row))
    return "\n".join(lines)
