"""Read the published release-admission ledger. Never mint an admission.

``openadapt-flow admit status`` answers one question from the public
projection: does this target currently hold a live, non-revoked admission?
An empty, expired, or revoked row is ``not_actively_admitted``. A fetch or
parse failure is also that state. The command does not sign, issue, or
rewrite the ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

SCHEMA_VERSION = "openadapt.release-admission-status/v1"
DEFAULT_LEDGER_URL = "https://openadapt.ai/production-lifecycle.json"
NOT_ADMITTED = "not_actively_admitted"
ADMITTED = "admitted"
FLOW_TARGET = "flow"
EXPECTED_TARGETS = (
    "agent",
    "capture",
    "cloud",
    "desktop",
    "docs",
    "flow",
    "openadapt",
)
PACK_FIELDS = (
    "wheel_digest",
    "substrate_matrix",
    "conformance_suite",
    "known_fails",
    "soak_hours",
    "rollback",
    "expires_at",
    "revocation_path",
)


class LedgerError(ValueError):
    """The ledger could not be read or did not match the public schema."""


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_ledger(source: str | Path) -> dict[str, Any]:
    """Load a public production-lifecycle projection from a path or URL."""

    text = str(source)
    if Path(text).exists():
        raw = Path(text).read_text(encoding="utf-8")
    elif "://" in text:
        request = Request(text, headers={"User-Agent": "openadapt-flow-admit-status"})
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except (URLError, TimeoutError, OSError) as exc:
            raise LedgerError(f"failed to fetch ledger {text}: {exc}") from exc
    else:
        raise LedgerError(f"ledger not found: {text}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"ledger is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerError("ledger root must be an object")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise LedgerError("ledger has no targets list")
    return payload


def _target_map(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in ledger["targets"]:
        if not isinstance(row, dict):
            continue
        target_id = row.get("id")
        if isinstance(target_id, str) and target_id:
            out[target_id] = row
    return out


def classify_admission(
    admission: Any, *, now: datetime | None = None
) -> tuple[str, str]:
    """Return (state, reason) for one latest_admission value."""

    clock = _now(now)
    if admission is None:
        return NOT_ADMITTED, "no live, non-revoked admission in the published ledger"
    if not isinstance(admission, dict) or not admission:
        return NOT_ADMITTED, "latest_admission is not a signed admission object"
    if admission.get("revoked_at"):
        return NOT_ADMITTED, "latest admission is revoked"
    expires = _parse_time(admission.get("expires_at"))
    if expires is None:
        return NOT_ADMITTED, "latest admission has no usable expires_at"
    if expires <= clock:
        return NOT_ADMITTED, "latest admission is expired"
    admission_id = admission.get("admission_id")
    if not isinstance(admission_id, str) or not admission_id:
        return NOT_ADMITTED, "latest admission has no admission_id"
    return ADMITTED, f"live admission {admission_id}"


def target_status(
    ledger: dict[str, Any], target_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    rows = _target_map(ledger)
    row = rows.get(target_id)
    if row is None:
        return {
            "id": target_id,
            "state": NOT_ADMITTED,
            "latest_admission": None,
            "reason": f"target {target_id!r} is missing from the ledger",
        }
    state, reason = classify_admission(row.get("latest_admission"), now=now)
    latest = row.get("latest_admission")
    return {
        "id": target_id,
        "display_name": row.get("display_name"),
        "state": state,
        "latest_admission": latest if isinstance(latest, dict) else None,
        "reason": reason,
    }


def pack_status(flow_row: dict[str, Any]) -> dict[str, Any]:
    """Describe the Flow admission pack. Unsigned rows are incomplete."""

    latest = flow_row.get("latest_admission")
    present: list[str] = []
    if isinstance(latest, dict):
        artifacts = latest.get("release", {}).get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if (
                    isinstance(artifact, dict)
                    and artifact.get("kind") == "wheel"
                    and artifact.get("sha256")
                ):
                    present.append("wheel_digest")
                    break
        if latest.get("expires_at"):
            present.append("expires_at")
        if latest.get("admission_id"):
            present.append("revocation_path")
    missing = [field for field in PACK_FIELDS if field not in present]
    return {
        "complete": not missing,
        "present": present,
        "missing": missing,
        "note": (
            "This CLI reports the published pack. It does not mint a digest, "
            "a soak, or a signature."
        ),
    }


def status_report(
    ledger: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    rows = _target_map(ledger)
    targets = [
        target_status(ledger, target_id, now=now) for target_id in EXPECTED_TARGETS
    ]
    flow = next(item for item in targets if item["id"] == FLOW_TARGET)
    product_wide = all(item["state"] == ADMITTED for item in targets) and len(
        rows
    ) >= len(EXPECTED_TARGETS)
    return {
        "schema_version": SCHEMA_VERSION,
        "product_wide_production": product_wide,
        "flow": flow,
        "targets": targets,
        "pack": pack_status(rows.get(FLOW_TARGET, {})),
        "derivation": ledger.get("derivation"),
    }


def render_status(report: dict[str, Any], *, ledger_source: str) -> str:
    flow = report["flow"]
    product = "yes" if report["product_wide_production"] else "no"
    lines = [
        "OpenAdapt release admission",
        f"ledger: {ledger_source}",
        f"product-wide Production: {product}",
        f"flow: {flow['state']}",
        f"  latest_admission: {flow['latest_admission'] and flow['latest_admission'].get('admission_id') or 'none'}",
        f"  reason: {flow['reason']}",
    ]
    pack = report["pack"]
    lines.append("pack: complete" if pack["complete"] else "pack: incomplete")
    if pack["missing"]:
        lines.append("  missing: " + ", ".join(pack["missing"]))
    lines.append("  " + pack["note"])
    return "\n".join(lines) + "\n"
