"""Multi-LoRA app-adapter registry, hot-swap veto router, and active-learning exporter.

Supports loading a single base visual-veto model and hot-swapping compact
per-app or per-tenant LoRA adapters at request time with strict BYOC tenant
isolation, per-adapter temperature calibration, fail-closed ``ABSTAIN -> HALT``
semantics, and deduplicated QLoRA JSONL export from resolved Exception Inbox
items.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ADAPTER_REGISTRY_SCHEMA = "openadapt.adapter-registry/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class TenantScope(str, Enum):
    """Visibility boundary of a registered LoRA adapter."""

    PUBLIC = "public"
    BYOC = "byoc"


class VetoDecision(str, Enum):
    """Fail-closed decision emitted by the visual veto router."""

    ALLOW = "ALLOW"
    VETO = "VETO"
    HALT_ESCALATE = "HALT_ESCALATE"


class InboxResolutionLabel(str, Enum):
    """Human adjudication outcome from the Exception Inbox."""

    TRUE_FAULT = "true_fault"
    FALSE_ALARM = "false_alarm"


class AdapterSpec(BaseModel):
    """Declarative specification for one hot-swappable LoRA veto adapter.

    Note: ``sha256`` is currently validated for format only (metadata-only /
    unenforced in this control-plane module). Any weight loader wired to this
    registry MUST verify ``sha256`` against the adapter bytes before load,
    accept only ``.safetensors`` (never ``torch.load``/pickle), and restrict
    ``adapter_uri`` to an allowlisted root.
    """

    model_config = ConfigDict(extra="forbid")

    adapter_id: str = Field(min_length=1)
    base_model: str = Field(min_length=1)
    adapter_uri: str = Field(min_length=1)
    sha256: str
    calibrated_threshold: float = Field(ge=0.0, le=1.0)
    temperature: float = Field(default=1.0, gt=0.0)
    tenant_scope: TenantScope = TenantScope.PUBLIC
    tenant_id: str | None = None
    app_id: str | None = None
    workflow_id: str | None = None
    domain_pattern: str | None = None

    @field_validator("adapter_id")
    @classmethod
    def _validate_adapter_id(cls, value: str) -> str:
        if not _SAFE_ID_RE.match(value):
            raise ValueError(f"Invalid adapter_id: {value!r}")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError(
                f"sha256 must be 64 lowercase hex characters (got {value!r})"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_tenant_scope(self) -> AdapterSpec:
        if self.tenant_scope is TenantScope.BYOC:
            if not self.tenant_id or not self.tenant_id.strip():
                raise ValueError("BYOC adapter requires a non-empty tenant_id.")
        elif self.tenant_id is not None:
            raise ValueError("Public adapter must not bind a private tenant_id.")
        return self


def _validate_sha256_map(values: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, digest in values.items():
        if not key or not key.strip():
            raise ValueError("Field hash key must be non-empty.")
        normalized = digest.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError(
                f"expected_field_hashes[{key!r}] must be 64 lowercase hex characters "
                "(raw form values are forbidden)."
            )
        cleaned[key.strip()] = normalized
    return cleaned


class VetoInferenceRequest(BaseModel):
    """Value-free pre/post-submit visual veto request routed to an app adapter.

    Design: this control-plane request carries only SHA-256 image references
    and value-free field digests/booleans (zero raw form values or screenshots
    on the wire). A real :class:`LoraBackend` dereferences
    ``pre_submit_image_sha256`` / ``post_submit_image_sha256`` from a local or
    customer-VPC encrypted artifact store and MUST return ``abstain=True`` if
    either image cannot be loaded or verified against its SHA-256 digest.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    domain: str | None = None
    tenant_id: str | None = None
    pre_submit_image_sha256: str = Field(min_length=1)
    post_submit_image_sha256: str = Field(min_length=1)
    expected_field_hashes: dict[str, str] = Field(default_factory=dict)
    expected_field_matches: dict[str, bool] = Field(default_factory=dict)
    dom_claims_success: bool = False

    @field_validator("expected_field_hashes")
    @classmethod
    def _check_expected_field_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_sha256_map(value)


class RawVetoScore(BaseModel):
    """Unscaled backend score before per-adapter temperature calibration."""

    model_config = ConfigDict(extra="forbid")

    fault_logit: float
    abstain: bool = False
    abstain_reason: str = ""
    rationale: str = ""


class VetoOutcome(BaseModel):
    """Calibrated, fail-closed veto outcome returned by ``MultiLoraVetoRouter``."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    adapter_id: str
    base_model: str
    decision: VetoDecision
    calibrated_fault_prob: float = Field(ge=0.0, le=1.0)
    calibrated_threshold: float = Field(ge=0.0, le=1.0)
    temperature: float = Field(gt=0.0)
    abstained: bool
    dom_override_blocked: bool
    reason: str


def calibrate_probability(fault_logit: float, temperature: float) -> float:
    """Apply temperature scaling to a fault logit and return sigmoid probability."""
    if temperature <= 0.0:
        raise ValueError(f"temperature must be > 0 (got {temperature})")
    scaled = fault_logit / temperature
    if scaled >= 0.0:
        z = math.exp(-scaled)
        return 1.0 / (1.0 + z)
    z = math.exp(scaled)
    return z / (1.0 + z)


def vllm_request_payload(
    spec: AdapterSpec,
    request: VetoInferenceRequest,
) -> dict[str, Any]:
    """Build a value-free vLLM multi-LoRA inference request payload targeting ``spec.adapter_id``."""
    return {
        "model": spec.adapter_id,
        "metadata": {
            "base_model": spec.base_model,
            "request_id": request.request_id,
            "app_id": request.app_id,
            "workflow_id": request.workflow_id,
            "pre_submit_image_sha256": request.pre_submit_image_sha256,
            "post_submit_image_sha256": request.post_submit_image_sha256,
        },
        "expected_field_hashes": dict(request.expected_field_hashes),
        "expected_field_matches": dict(request.expected_field_matches),
    }


def ollama_model_tag(spec: AdapterSpec) -> str:
    """Return the deterministic Ollama model tag for a base + adapter pair."""
    if spec.adapter_uri.startswith("ollama://"):
        return spec.adapter_uri.removeprefix("ollama://")
    return f"{spec.base_model}:{spec.adapter_id}"


class AppAdapterRegistry:
    """Declarative registry mapping ``(tenant, app, workflow, domain)`` to ``AdapterSpec``."""

    def __init__(
        self,
        *,
        default_base_adapter: AdapterSpec,
        adapters: Sequence[AdapterSpec] = (),
    ) -> None:
        if default_base_adapter.tenant_scope is not TenantScope.PUBLIC:
            raise ValueError("default_base_adapter must have tenant_scope='public'.")
        self._default_base = default_base_adapter
        self._by_id: dict[str, AdapterSpec] = {
            default_base_adapter.adapter_id: default_base_adapter
        }
        self._adapters: list[AdapterSpec] = []
        for spec in adapters:
            self.register(spec)

    @property
    def default_base_adapter(self) -> AdapterSpec:
        return self._default_base

    def register(self, spec: AdapterSpec) -> None:
        if spec.adapter_id in self._by_id:
            raise ValueError(f"Duplicate adapter_id in registry: {spec.adapter_id!r}")
        self._by_id[spec.adapter_id] = spec
        self._adapters.append(spec)

    def get(self, adapter_id: str, *, tenant_id: str | None = None) -> AdapterSpec:
        if adapter_id not in self._by_id:
            raise KeyError(f"Unknown adapter_id: {adapter_id!r}")
        spec = self._by_id[adapter_id]
        if spec.tenant_scope is TenantScope.BYOC and spec.tenant_id != tenant_id:
            raise PermissionError(
                f"Cross-tenant access denied for BYOC adapter {adapter_id!r}."
            )
        return spec

    def _is_accessible(self, spec: AdapterSpec, tenant_id: str | None) -> bool:
        if spec.tenant_scope is TenantScope.PUBLIC:
            return True
        return tenant_id is not None and spec.tenant_id == tenant_id

    def resolve(
        self,
        *,
        app_id: str,
        workflow_id: str | None = None,
        domain: str | None = None,
        tenant_id: str | None = None,
        requested_adapter_id: str | None = None,
    ) -> AdapterSpec:
        """Resolve the authoritative adapter from server-side bindings with strict BYOC isolation.

        Fallback chain:
        1. Exact BYOC ``(tenant_id, app_id, workflow_id)``
        2. Exact BYOC ``(tenant_id, app_id)`` (no workflow binding)
        3. Public ``(app_id, workflow_id)``
        4. Public ``(app_id)`` (no workflow binding)
        5. Matching ``domain_pattern`` (BYOC for ``tenant_id`` first, then public)
        6. Cold-start ``default_base_adapter``

        If ``requested_adapter_id`` is supplied by the caller, it is accepted
        ONLY when it matches the server-resolved adapter; otherwise
        ``PermissionError`` is raised so a caller can never steer scoring to a
        more lenient adapter.
        """
        resolved = self._resolve_authoritative(
            app_id=app_id,
            workflow_id=workflow_id,
            domain=domain,
            tenant_id=tenant_id,
        )
        if (
            requested_adapter_id is not None
            and requested_adapter_id != resolved.adapter_id
        ):
            raise PermissionError(
                f"Caller-requested adapter {requested_adapter_id!r} does not match "
                f"server-resolved adapter {resolved.adapter_id!r} for app_id={app_id!r}."
            )
        return resolved

    def _resolve_authoritative(
        self,
        *,
        app_id: str,
        workflow_id: str | None = None,
        domain: str | None = None,
        tenant_id: str | None = None,
    ) -> AdapterSpec:
        accessible = [s for s in self._adapters if self._is_accessible(s, tenant_id)]

        if tenant_id is not None and workflow_id is not None:
            for spec in accessible:
                if (
                    spec.tenant_scope is TenantScope.BYOC
                    and spec.tenant_id == tenant_id
                    and spec.app_id == app_id
                    and spec.workflow_id == workflow_id
                ):
                    return spec

        if tenant_id is not None:
            for spec in accessible:
                if (
                    spec.tenant_scope is TenantScope.BYOC
                    and spec.tenant_id == tenant_id
                    and spec.app_id == app_id
                    and spec.workflow_id is None
                ):
                    return spec

        if workflow_id is not None:
            for spec in accessible:
                if (
                    spec.tenant_scope is TenantScope.PUBLIC
                    and spec.app_id == app_id
                    and spec.workflow_id == workflow_id
                ):
                    return spec

        for spec in accessible:
            if (
                spec.tenant_scope is TenantScope.PUBLIC
                and spec.app_id == app_id
                and spec.workflow_id is None
            ):
                return spec

        if domain:
            normalized_domain = domain.strip().lower()
            for scope in (TenantScope.BYOC, TenantScope.PUBLIC):
                for spec in accessible:
                    if (
                        spec.tenant_scope is scope
                        and spec.domain_pattern
                        and fnmatch.fnmatch(
                            normalized_domain, spec.domain_pattern.lower()
                        )
                    ):
                        return spec

        return self._default_base

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppAdapterRegistry:
        schema = data.get("schema_version", ADAPTER_REGISTRY_SCHEMA)
        if schema != ADAPTER_REGISTRY_SCHEMA:
            raise ValueError(f"Unsupported registry schema_version: {schema!r}")
        default_base = AdapterSpec.model_validate(data["default_base_adapter"])
        adapters = [
            AdapterSpec.model_validate(item) for item in data.get("adapters", [])
        ]
        return cls(default_base_adapter=default_base, adapters=adapters)

    @classmethod
    def from_json(cls, source: str | Path) -> AppAdapterRegistry:
        raw = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Adapter registry JSON root must be an object.")
        return cls.from_dict(data)

    @classmethod
    def from_yaml(cls, source: str | Path) -> AppAdapterRegistry:
        raw = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError("Adapter registry YAML root must be a mapping.")
        return cls.from_dict(data)


class LoraBackend(Protocol):
    """Pluggable inference backend (PEFT ``set_adapter``, vLLM, or Ollama)."""

    @property
    def active_adapter_id(self) -> str | None: ...

    def ensure_adapter(self, spec: AdapterSpec) -> bool:
        """Activate ``spec`` on the loaded base model. Return True if swapped."""
        ...

    def score(self, spec: AdapterSpec, request: VetoInferenceRequest) -> RawVetoScore:
        """Run visual veto scoring using ``spec``."""
        ...


class MultiLoraVetoRouter:
    """Single-base, multi-LoRA hot-swap router with fail-closed calibration."""

    def __init__(
        self,
        *,
        base_model: str,
        registry: AppAdapterRegistry,
        backend: LoraBackend,
    ) -> None:
        if not base_model.strip():
            raise ValueError("base_model must be non-empty.")
        self._base_model = base_model
        self._registry = registry
        self._backend = backend
        self.swap_count = 0
        self.request_count = 0

    @property
    def base_model(self) -> str:
        return self._base_model

    @property
    def active_adapter_id(self) -> str | None:
        return self._backend.active_adapter_id

    def evaluate(
        self,
        request: VetoInferenceRequest,
        *,
        requested_adapter_id: str | None = None,
    ) -> VetoOutcome:
        spec = self._registry.resolve(
            app_id=request.app_id,
            workflow_id=request.workflow_id,
            domain=request.domain,
            tenant_id=request.tenant_id,
            requested_adapter_id=requested_adapter_id,
        )
        if spec.base_model != self._base_model:
            raise ValueError(
                f"Adapter {spec.adapter_id!r} targets base_model={spec.base_model!r}, "
                f"expected {self._base_model!r}."
            )
        if spec.app_id is not None and spec.app_id != request.app_id:
            raise PermissionError(
                f"Adapter {spec.adapter_id!r} is bound to app_id={spec.app_id!r}, "
                f"not {request.app_id!r}."
            )
        if spec.workflow_id is not None and spec.workflow_id != request.workflow_id:
            raise PermissionError(
                f"Adapter {spec.adapter_id!r} is bound to workflow_id={spec.workflow_id!r}, "
                f"not {request.workflow_id!r}."
            )

        swapped = self._backend.ensure_adapter(spec)
        if swapped:
            self.swap_count += 1
        self.request_count += 1

        raw = self._backend.score(spec, request)
        prob = calibrate_probability(raw.fault_logit, spec.temperature)

        if raw.abstain:
            reason = raw.abstain_reason.strip() or (
                "Visual veto abstained (occlusion or unreadable state); halting for human review."
            )
            return VetoOutcome(
                request_id=request.request_id,
                adapter_id=spec.adapter_id,
                base_model=spec.base_model,
                decision=VetoDecision.HALT_ESCALATE,
                calibrated_fault_prob=prob,
                calibrated_threshold=spec.calibrated_threshold,
                temperature=spec.temperature,
                abstained=True,
                dom_override_blocked=request.dom_claims_success,
                reason=reason,
            )

        if prob >= spec.calibrated_threshold:
            reason = raw.rationale.strip() or (
                f"Calibrated fault probability {prob:.4f} >= threshold "
                f"{spec.calibrated_threshold:.4f}; commit vetoed."
            )
            return VetoOutcome(
                request_id=request.request_id,
                adapter_id=spec.adapter_id,
                base_model=spec.base_model,
                decision=VetoDecision.VETO,
                calibrated_fault_prob=prob,
                calibrated_threshold=spec.calibrated_threshold,
                temperature=spec.temperature,
                abstained=False,
                dom_override_blocked=request.dom_claims_success,
                reason=reason,
            )

        reason = raw.rationale.strip() or (
            f"Calibrated fault probability {prob:.4f} < threshold "
            f"{spec.calibrated_threshold:.4f}; visual check passed."
        )
        return VetoOutcome(
            request_id=request.request_id,
            adapter_id=spec.adapter_id,
            base_model=spec.base_model,
            decision=VetoDecision.ALLOW,
            calibrated_fault_prob=prob,
            calibrated_threshold=spec.calibrated_threshold,
            temperature=spec.temperature,
            abstained=False,
            dom_override_blocked=False,
            reason=reason,
        )


class InboxResolutionItem(BaseModel):
    """Value-free Exception Inbox resolution item eligible for QLoRA active learning."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    tenant_id: str = Field(min_length=1)
    label: InboxResolutionLabel
    pre_submit_image_ref: str = Field(min_length=1)
    post_submit_image_ref: str = Field(min_length=1)
    expected_field_hashes: dict[str, str] = Field(default_factory=dict)
    resolved_at: str = Field(min_length=1)

    @field_validator("expected_field_hashes")
    @classmethod
    def _check_expected_field_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_sha256_map(value)


def _dedup_fingerprint(item: InboxResolutionItem) -> str:
    canonical = json.dumps(
        {
            "tenant_id": item.tenant_id,
            "app_id": item.app_id,
            "workflow_id": item.workflow_id,
            "pre_submit_image_ref": item.pre_submit_image_ref,
            "post_submit_image_ref": item.post_submit_image_ref,
            "label": item.label.value,
            "expected_field_hashes": item.expected_field_hashes,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export_inbox_training_jsonl(
    items: Sequence[InboxResolutionItem],
    output_path: Path,
    *,
    tenant_id: str | None = None,
    app_id: str | None = None,
) -> int:
    """Export resolved Exception Inbox items into deduplicated, value-free QLoRA JSONL pairs.

    Never writes raw form values or free-text reviewer notes into training files.
    When ``tenant_id`` is provided, enforces strict single-tenant VPC isolation
    by exporting only records belonging to that tenant.
    """
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for item in items:
        if tenant_id is not None and item.tenant_id != tenant_id:
            continue
        if app_id is not None and item.app_id != app_id:
            continue
        fp = _dedup_fingerprint(item)
        if fp in seen:
            continue
        seen.add(fp)

        target_verdict = (
            "FAULT" if item.label is InboxResolutionLabel.TRUE_FAULT else "CLEAN"
        )
        records.append(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Compare the pre-submit and post-submit screens against expected "
                            "field digests. Output FAULT if any write failed or drifted, or CLEAN if "
                            "all fields persisted."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "app_id": item.app_id,
                                "workflow_id": item.workflow_id,
                                "pre_submit_image_ref": item.pre_submit_image_ref,
                                "post_submit_image_ref": item.post_submit_image_ref,
                                "expected_field_hashes": item.expected_field_hashes,
                            },
                            sort_keys=True,
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "verdict": target_verdict,
                                "label": item.label.value,
                            },
                            sort_keys=True,
                        ),
                    },
                ],
                "metadata": {
                    "item_id": item.item_id,
                    "app_id": item.app_id,
                    "workflow_id": item.workflow_id,
                    "tenant_id": item.tenant_id,
                    "label": item.label.value,
                    "resolved_at": item.resolved_at,
                    "fingerprint": fp,
                },
            }
        )

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent, prefix=".lora-train-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for rec in records:
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
        os.replace(tmp_name, dest)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    return len(records)


__all__ = [
    "ADAPTER_REGISTRY_SCHEMA",
    "AdapterSpec",
    "AppAdapterRegistry",
    "InboxResolutionItem",
    "InboxResolutionLabel",
    "LoraBackend",
    "MultiLoraVetoRouter",
    "RawVetoScore",
    "TenantScope",
    "VetoDecision",
    "VetoInferenceRequest",
    "VetoOutcome",
    "calibrate_probability",
    "export_inbox_training_jsonl",
    "ollama_model_tag",
    "vllm_request_payload",
]
