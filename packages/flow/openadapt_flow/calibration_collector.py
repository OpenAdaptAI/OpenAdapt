"""Online False-Pause Calibration Collector (value-free, opt-in local queue).

Records operator override events from Exception Inbox or live-run pauses into
strictly tenant-scoped, value-free local JSONL queues for per-app LoRA
calibration.

Security & privacy invariants:
- **Opt-in by default (off):** Refuses to write unless ``consent_enabled=True``
  is explicitly set by the operator/deployment.
- **Value-free:** Stores only SHA-256 screenshot hashes, structural metadata
  from an explicit allowlist, and verdict labels. Never accepts or persists
  raw form values, screenshots, DOM text, or free-text notes.
- **Fail-closed tenant isolation:** Refuses missing/invalid ``tenant_id`` and
  rejects cross-tenant writes or reads when bound to a tenant.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.multi_lora_veto import VetoDecision

CALIBRATION_RECORD_SCHEMA: Literal["openadapt.veto-calibration/v1"] = (
    "openadapt.veto-calibration/v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_STEP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Simple PII pattern guards for structural metadata string values.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")

ALLOWED_STRUCTURAL_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "aria_error_count",
        "device_scale_factor",
        "dom_claims_success",
        "dom_field_count",
        "http_status",
        "latency_ms",
        "roi_crop_used",
        "theme_hint",
        "ui_framework_hint",
        "viewport_height",
        "viewport_width",
    }
)


class CalibrationVerdict(str, Enum):
    """Verdict labels accepted for online false-pause calibration records."""

    ALLOW = "ALLOW"
    VETO = "VETO"
    HALT_ESCALATE = "HALT_ESCALATE"
    ABSTAIN = "ABSTAIN"


class OverrideKind(str, Enum):
    """Derived calibration signal comparing model prediction vs operator verdict."""

    FALSE_PAUSE = "false_pause"
    TRUE_FAULT = "true_fault"
    MISSED_FAULT = "missed_fault"
    CONFIRMED_ALLOW = "confirmed_allow"


def _coerce_verdict(
    value: CalibrationVerdict | VetoDecision | str,
) -> CalibrationVerdict:
    if isinstance(value, CalibrationVerdict):
        return value
    if isinstance(value, VetoDecision):
        return CalibrationVerdict(value.value)
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized == "PASS":
            return CalibrationVerdict.ALLOW
        if normalized == "ACCEPT":
            return CalibrationVerdict.ALLOW
        return CalibrationVerdict(normalized)
    raise ValueError(f"Unsupported verdict type: {type(value).__name__}")


def _derive_override_kind(
    predicted: CalibrationVerdict,
    operator: CalibrationVerdict,
) -> OverrideKind:
    pred_paused = predicted is not CalibrationVerdict.ALLOW
    op_allowed = operator is CalibrationVerdict.ALLOW
    if pred_paused and op_allowed:
        return OverrideKind.FALSE_PAUSE
    if pred_paused and not op_allowed:
        return OverrideKind.TRUE_FAULT
    if not pred_paused and not op_allowed:
        return OverrideKind.MISSED_FAULT
    return OverrideKind.CONFIRMED_ALLOW


def _validate_slug(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    cleaned = value.strip()
    if (
        ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or not _SAFE_SLUG_RE.match(cleaned)
    ):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return cleaned


def _validate_structural_metadata(
    raw: dict[str, Any] | None,
) -> dict[str, str | int | float | bool]:
    if not raw:
        return {}
    cleaned: dict[str, str | int | float | bool] = {}
    for key, val in raw.items():
        if key not in ALLOWED_STRUCTURAL_METADATA_KEYS:
            raise ValueError(
                f"Value-free violation: disallowed structural_metadata key {key!r}. "
                f"Allowed keys: {sorted(ALLOWED_STRUCTURAL_METADATA_KEYS)}"
            )
        if isinstance(val, bool):
            cleaned[key] = val
        elif isinstance(val, (int, float)):
            cleaned[key] = val
        elif isinstance(val, str):
            s = val.strip()
            if len(s) > 64:
                raise ValueError(
                    f"Value-free violation: structural_metadata[{key!r}] exceeds 64 characters."
                )
            if _EMAIL_RE.search(s) or _SSN_RE.search(s) or _PHONE_RE.search(s):
                raise ValueError(
                    f"Value-free violation: structural_metadata[{key!r}] matched a PII pattern."
                )
            cleaned[key] = s
        else:
            raise ValueError(
                f"Value-free violation: structural_metadata[{key!r}] must be a scalar "
                f"(str, int, float, or bool), got {type(val).__name__}."
            )
    return cleaned


class CalibrationOverrideRecord(BaseModel):
    """Value-free operator override record for offline LoRA calibration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.veto-calibration/v1"] = CALIBRATION_RECORD_SCHEMA
    tenant_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    screenshot_hash: str
    pre_submit_screenshot_hash: str | None = None
    predicted_verdict: CalibrationVerdict
    operator_verdict: CalibrationVerdict
    override_kind: OverrideKind
    adapter_id: str | None = None
    calibrated_fault_prob: float | None = Field(default=None, ge=0.0, le=1.0)
    structural_metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict
    )
    recorded_at: str = Field(min_length=1)
    fingerprint: str

    @field_validator("tenant_id")
    @classmethod
    def _check_tenant_id(cls, value: str) -> str:
        return _validate_slug(value, "tenant_id")

    @field_validator("app_id")
    @classmethod
    def _check_app_id(cls, value: str) -> str:
        return _validate_slug(value, "app_id")

    @field_validator("workflow_id")
    @classmethod
    def _check_workflow_id(cls, value: str) -> str:
        return _validate_slug(value, "workflow_id")

    @field_validator("step_id")
    @classmethod
    def _check_step_id(cls, value: str) -> str:
        return _validate_slug(value, "step_id")

    @field_validator("adapter_id")
    @classmethod
    def _check_adapter_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_slug(value, "adapter_id")

    @field_validator("screenshot_hash")
    @classmethod
    def _check_screenshot_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError(
                f"screenshot_hash must be 64 lowercase hex characters (got {value!r})"
            )
        return normalized

    @field_validator("pre_submit_screenshot_hash")
    @classmethod
    def _check_pre_submit_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError(
                f"pre_submit_screenshot_hash must be 64 lowercase hex characters (got {value!r})"
            )
        return normalized

    @field_validator("fingerprint")
    @classmethod
    def _check_fingerprint(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError(
                f"fingerprint must be 64 lowercase hex characters (got {value!r})"
            )
        return normalized


class CalibrationReadinessSummary(BaseModel):
    """Summary of queued calibration overrides for one ``(tenant_id, app_id)``."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    app_id: str
    total_records: int = Field(ge=0)
    false_pause_count: int = Field(ge=0)
    true_fault_count: int = Field(ge=0)
    missed_fault_count: int = Field(ge=0)
    min_samples_required: int = Field(ge=1)
    ready_for_calibration: bool


def _compute_fingerprint(
    *,
    tenant_id: str,
    app_id: str,
    workflow_id: str,
    step_id: str,
    screenshot_hash: str,
    predicted_verdict: CalibrationVerdict,
    operator_verdict: CalibrationVerdict,
) -> str:
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "app_id": app_id,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "screenshot_hash": screenshot_hash,
            "predicted_verdict": predicted_verdict.value,
            "operator_verdict": operator_verdict.value,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CalibrationCollector:
    """Tenant-isolated, value-free JSONL queue for offline per-app LoRA calibration.

    No trainer is wired in this module; records are queued locally for future
    offline calibration workflows.
    """

    def __init__(
        self,
        queue_root: Path,
        *,
        bound_tenant_id: str | None = None,
        consent_enabled: bool = False,
    ) -> None:
        self._queue_root = Path(queue_root).resolve()
        self._bound_tenant_id = (
            _validate_slug(bound_tenant_id, "bound_tenant_id")
            if bound_tenant_id is not None
            else None
        )
        self._consent_enabled = bool(consent_enabled)
        self._lock = threading.Lock()

    @property
    def consent_enabled(self) -> bool:
        return self._consent_enabled

    @property
    def queue_root(self) -> Path:
        return self._queue_root

    @property
    def bound_tenant_id(self) -> str | None:
        return self._bound_tenant_id

    def _enforce_tenant_access(self, tenant_id: str) -> str:
        validated = _validate_slug(tenant_id, "tenant_id")
        if self._bound_tenant_id is not None and validated != self._bound_tenant_id:
            raise PermissionError(
                f"Cross-tenant calibration access denied: collector is bound to "
                f"{self._bound_tenant_id!r}, got {validated!r}."
            )
        return validated

    def queue_path_for(self, tenant_id: str, app_id: str) -> Path:
        safe_tenant = self._enforce_tenant_access(tenant_id)
        safe_app = _validate_slug(app_id, "app_id")
        tenant_dir = (self._queue_root / safe_tenant).resolve()
        if tenant_dir.parent != self._queue_root:
            raise PermissionError("Resolved tenant queue directory escapes queue_root.")
        target = (tenant_dir / f"{safe_app}.jsonl").resolve()
        if target.parent != tenant_dir:
            raise PermissionError(
                "Resolved calibration queue file escapes tenant directory."
            )
        return target

    def record_operator_override(
        self,
        *,
        tenant_id: str,
        app_id: str,
        workflow_id: str,
        step_id: str,
        screenshot_hash: str,
        predicted_verdict: CalibrationVerdict | VetoDecision | str,
        operator_verdict: CalibrationVerdict | VetoDecision | str,
        pre_submit_screenshot_hash: str | None = None,
        adapter_id: str | None = None,
        calibrated_fault_prob: float | None = None,
        structural_metadata: dict[str, Any] | None = None,
        recorded_at: str | None = None,
    ) -> CalibrationOverrideRecord:
        """Append one value-free calibration override record to the tenant queue."""
        if not self._consent_enabled:
            raise PermissionError(
                "Calibration collection is disabled by default; set consent_enabled=True to record."
            )
        safe_tenant = self._enforce_tenant_access(tenant_id)
        safe_app = _validate_slug(app_id, "app_id")
        safe_workflow = _validate_slug(workflow_id, "workflow_id")
        pred = _coerce_verdict(predicted_verdict)
        op = _coerce_verdict(operator_verdict)
        clean_meta = _validate_structural_metadata(structural_metadata)
        norm_hash = screenshot_hash.strip().lower()
        timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
        fp = _compute_fingerprint(
            tenant_id=safe_tenant,
            app_id=safe_app,
            workflow_id=safe_workflow,
            step_id=step_id.strip(),
            screenshot_hash=norm_hash,
            predicted_verdict=pred,
            operator_verdict=op,
        )
        record = CalibrationOverrideRecord(
            tenant_id=safe_tenant,
            app_id=safe_app,
            workflow_id=safe_workflow,
            step_id=step_id,
            screenshot_hash=norm_hash,
            pre_submit_screenshot_hash=pre_submit_screenshot_hash,
            predicted_verdict=pred,
            operator_verdict=op,
            override_kind=_derive_override_kind(pred, op),
            adapter_id=adapter_id,
            calibrated_fault_prob=calibrated_fault_prob,
            structural_metadata=clean_meta,
            recorded_at=timestamp,
            fingerprint=fp,
        )

        target = self.queue_path_for(safe_tenant, safe_app)
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n"
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
        return record

    def read_tenant_queue(
        self,
        tenant_id: str,
        *,
        app_id: str | None = None,
        deduplicate: bool = True,
    ) -> list[CalibrationOverrideRecord]:
        """Read value-free calibration records belonging strictly to ``tenant_id``."""
        safe_tenant = self._enforce_tenant_access(tenant_id)
        tenant_dir = (self._queue_root / safe_tenant).resolve()
        if not tenant_dir.is_dir():
            return []

        files: list[Path]
        if app_id is not None:
            path = self.queue_path_for(safe_tenant, app_id)
            files = [path] if path.is_file() else []
        else:
            files = sorted(tenant_dir.glob("*.jsonl"))

        seen: set[str] = set()
        records: list[CalibrationOverrideRecord] = []
        with self._lock:
            for file_path in files:
                for raw_line in file_path.read_text(encoding="utf-8").splitlines():
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    rec = CalibrationOverrideRecord.model_validate_json(stripped)
                    if rec.tenant_id != safe_tenant:
                        raise PermissionError(
                            f"Tenant isolation violation in {file_path}: expected "
                            f"{safe_tenant!r}, found {rec.tenant_id!r}."
                        )
                    if deduplicate and rec.fingerprint in seen:
                        continue
                    seen.add(rec.fingerprint)
                    records.append(rec)
        return records

    def calibration_readiness(
        self,
        tenant_id: str,
        app_id: str,
        *,
        min_samples: int = 10,
    ) -> CalibrationReadinessSummary:
        """Report whether enough deduplicated overrides exist for offline LoRA calibration."""
        records = self.read_tenant_queue(tenant_id, app_id=app_id, deduplicate=True)
        false_pauses = sum(
            1 for r in records if r.override_kind is OverrideKind.FALSE_PAUSE
        )
        true_faults = sum(
            1 for r in records if r.override_kind is OverrideKind.TRUE_FAULT
        )
        missed_faults = sum(
            1 for r in records if r.override_kind is OverrideKind.MISSED_FAULT
        )
        return CalibrationReadinessSummary(
            tenant_id=tenant_id,
            app_id=app_id,
            total_records=len(records),
            false_pause_count=false_pauses,
            true_fault_count=true_faults,
            missed_fault_count=missed_faults,
            min_samples_required=min_samples,
            ready_for_calibration=len(records) >= min_samples,
        )


def record_operator_override(
    tenant_id: str,
    app_id: str,
    workflow_id: str,
    step_id: str,
    screenshot_hash: str,
    predicted_verdict: CalibrationVerdict | VetoDecision | str,
    operator_verdict: CalibrationVerdict | VetoDecision | str,
    *,
    queue_root: Path,
    consent_enabled: bool = False,
    bound_tenant_id: str | None = None,
    pre_submit_screenshot_hash: str | None = None,
    adapter_id: str | None = None,
    calibrated_fault_prob: float | None = None,
    structural_metadata: dict[str, Any] | None = None,
    recorded_at: str | None = None,
) -> CalibrationOverrideRecord:
    """Convenience entry point for recording one value-free operator override."""
    collector = CalibrationCollector(
        queue_root,
        bound_tenant_id=bound_tenant_id,
        consent_enabled=consent_enabled,
    )
    return collector.record_operator_override(
        tenant_id=tenant_id,
        app_id=app_id,
        workflow_id=workflow_id,
        step_id=step_id,
        screenshot_hash=screenshot_hash,
        predicted_verdict=predicted_verdict,
        operator_verdict=operator_verdict,
        pre_submit_screenshot_hash=pre_submit_screenshot_hash,
        adapter_id=adapter_id,
        calibrated_fault_prob=calibrated_fault_prob,
        structural_metadata=structural_metadata,
        recorded_at=recorded_at,
    )


__all__ = [
    "ALLOWED_STRUCTURAL_METADATA_KEYS",
    "CALIBRATION_RECORD_SCHEMA",
    "CalibrationCollector",
    "CalibrationOverrideRecord",
    "CalibrationReadinessSummary",
    "CalibrationVerdict",
    "OverrideKind",
    "record_operator_override",
]
