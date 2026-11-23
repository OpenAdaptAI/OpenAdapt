"""Unit tests for Multi-LoRA app-adapter registry, hot-swap router, and JSONL exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from openadapt_flow.multi_lora_veto import (
    ADAPTER_REGISTRY_SCHEMA,
    AdapterSpec,
    AppAdapterRegistry,
    InboxResolutionItem,
    InboxResolutionLabel,
    MultiLoraVetoRouter,
    RawVetoScore,
    TenantScope,
    VetoDecision,
    VetoInferenceRequest,
    calibrate_probability,
    export_inbox_training_jsonl,
    hmac_field_value,
    ollama_model_tag,
    vllm_request_payload,
)

BASE_MODEL = "openadapt-veto-4b"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _make_spec(
    adapter_id: str,
    *,
    base_model: str = BASE_MODEL,
    sha256: str = SHA_A,
    threshold: float = 0.5,
    temperature: float = 1.0,
    tenant_scope: TenantScope = TenantScope.PUBLIC,
    tenant_id: str | None = None,
    app_id: str | None = None,
    workflow_id: str | None = None,
    domain_pattern: str | None = None,
    adapter_uri: str | None = None,
) -> AdapterSpec:
    return AdapterSpec(
        adapter_id=adapter_id,
        base_model=base_model,
        adapter_uri=adapter_uri or f"s3://adapters/{adapter_id}",
        sha256=sha256,
        calibrated_threshold=threshold,
        temperature=temperature,
        tenant_scope=tenant_scope,
        tenant_id=tenant_id,
        app_id=app_id,
        workflow_id=workflow_id,
        domain_pattern=domain_pattern,
    )


def _sample_registry() -> AppAdapterRegistry:
    base = _make_spec("veto-base", sha256=SHA_A, threshold=0.60, temperature=1.0)
    adapters = [
        _make_spec(
            "veto-app-alpha",
            sha256=SHA_B,
            threshold=0.55,
            temperature=1.2,
            app_id="app-alpha",
        ),
        _make_spec(
            "veto-app-alpha-intake",
            sha256=SHA_C,
            threshold=0.45,
            temperature=0.8,
            app_id="app-alpha",
            workflow_id="intake-v1",
        ),
        _make_spec(
            "veto-tenant1-alpha-intake",
            sha256=SHA_D,
            threshold=0.35,
            temperature=0.5,
            tenant_scope=TenantScope.BYOC,
            tenant_id="tenant-1",
            app_id="app-alpha",
            workflow_id="intake-v1",
        ),
        _make_spec(
            "veto-tenant1-alpha",
            sha256=SHA_E,
            threshold=0.40,
            temperature=0.75,
            tenant_scope=TenantScope.BYOC,
            tenant_id="tenant-1",
            app_id="app-alpha",
        ),
        _make_spec(
            "veto-portal-domain",
            sha256=SHA_B,
            threshold=0.50,
            temperature=1.0,
            domain_pattern="*.portal.example",
        ),
    ]
    return AppAdapterRegistry(default_base_adapter=base, adapters=adapters)


class _MockHotSwapBackend:
    """In-memory mock LoraBackend tracking adapter hot-swaps."""

    def __init__(self, scores_by_request: dict[str, RawVetoScore]) -> None:
        self._scores = scores_by_request
        self._active: str | None = None
        self.swap_history: list[str] = []

    @property
    def active_adapter_id(self) -> str | None:
        return self._active

    def ensure_adapter(self, spec: AdapterSpec) -> bool:
        if self._active == spec.adapter_id:
            return False
        self._active = spec.adapter_id
        self.swap_history.append(spec.adapter_id)
        return True

    def score(self, spec: AdapterSpec, request: VetoInferenceRequest) -> RawVetoScore:
        return self._scores[request.request_id]


def test_registry_routing_fallback_chain() -> None:
    reg = _sample_registry()

    # 1. Exact BYOC (tenant, app, workflow)
    s1 = reg.resolve(app_id="app-alpha", workflow_id="intake-v1", tenant_id="tenant-1")
    assert s1.adapter_id == "veto-tenant1-alpha-intake"

    # 2. Exact BYOC (tenant, app) when workflow has no BYOC override
    s2 = reg.resolve(app_id="app-alpha", workflow_id="other-wf", tenant_id="tenant-1")
    assert s2.adapter_id == "veto-tenant1-alpha"

    # 3. Public (app, workflow) for another tenant (never sees tenant-1 BYOC)
    s3 = reg.resolve(app_id="app-alpha", workflow_id="intake-v1", tenant_id="tenant-2")
    assert s3.adapter_id == "veto-app-alpha-intake"

    # 4. Public (app) fallback
    s4 = reg.resolve(app_id="app-alpha", workflow_id="unknown-wf", tenant_id="tenant-2")
    assert s4.adapter_id == "veto-app-alpha"

    # 5. Domain pattern fallback
    s5 = reg.resolve(app_id="unseen-app", domain="claims.portal.example")
    assert s5.adapter_id == "veto-portal-domain"

    # 6. Cold-start default fallback
    s6 = reg.resolve(app_id="brand-new-app", domain="unknown.example")
    assert s6.adapter_id == "veto-base"


def test_strict_byoc_tenant_isolation_raises_permission_error() -> None:
    reg = _sample_registry()

    # Owner tenant can explicitly select its BYOC adapter
    own = reg.resolve(
        app_id="app-alpha",
        tenant_id="tenant-1",
        requested_adapter_id="veto-tenant1-alpha",
    )
    assert own.adapter_id == "veto-tenant1-alpha"

    # Another tenant requesting tenant-1's BYOC adapter raises PermissionError
    with pytest.raises(PermissionError, match="does not match server-resolved adapter"):
        reg.resolve(
            app_id="app-alpha",
            tenant_id="tenant-2",
            requested_adapter_id="veto-tenant1-alpha",
        )

    # Anonymous caller requesting BYOC adapter raises PermissionError
    with pytest.raises(PermissionError, match="does not match server-resolved adapter"):
        reg.resolve(
            app_id="app-alpha",
            tenant_id=None,
            requested_adapter_id="veto-tenant1-alpha-intake",
        )

    # Caller cannot steer app-alpha to a more lenient public adapter
    lenient_reg = AppAdapterRegistry(
        default_base_adapter=_make_spec("veto-base", threshold=0.5),
        adapters=[
            _make_spec("strict-a", app_id="app-a", threshold=0.5),
            _make_spec("lenient-b", app_id="app-b", threshold=0.99),
        ],
    )
    with pytest.raises(PermissionError, match="does not match server-resolved adapter"):
        lenient_reg.resolve(app_id="app-a", requested_adapter_id="lenient-b")


def test_registry_loads_from_json_and_yaml(tmp_path: Path) -> None:
    payload = {
        "schema_version": ADAPTER_REGISTRY_SCHEMA,
        "default_base_adapter": _make_spec("veto-base").model_dump(mode="json"),
        "adapters": [
            _make_spec("veto-app-beta", sha256=SHA_B, app_id="app-beta").model_dump(
                mode="json"
            )
        ],
    }

    json_path = tmp_path / "registry.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    from_json = AppAdapterRegistry.from_json(json_path)
    assert from_json.resolve(app_id="app-beta").adapter_id == "veto-app-beta"

    yaml_path = tmp_path / "registry.yaml"
    yaml_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    from_yaml = AppAdapterRegistry.from_yaml(yaml_path)
    assert from_yaml.resolve(app_id="app-beta").adapter_id == "veto-app-beta"

    # Validation guard: BYOC without tenant_id fails
    with pytest.raises(ValidationError):
        _make_spec("bad-byoc", tenant_scope=TenantScope.BYOC, tenant_id=None)


def test_router_hot_swaps_temperature_calibration_and_abstain_halt() -> None:
    reg = _sample_registry()
    backend = _MockHotSwapBackend(
        {
            # logit=0.2, temp=0.5 -> sigmoid(0.4)=0.5987 >= threshold 0.35 -> VETO
            "req-1": RawVetoScore(
                fault_logit=0.2, rationale="Field blanked after submit."
            ),
            # Same adapter (no swap!), logit=-1.5 -> sigmoid(-3.0)=0.0474 < 0.35 -> ALLOW
            "req-2": RawVetoScore(fault_logit=-1.5, rationale="All fields persisted."),
            # Different adapter (swap!), abstain=True while dom_claims_success=True -> HALT_ESCALATE
            "req-3": RawVetoScore(
                fault_logit=-4.0,
                abstain=True,
                abstain_reason="Modal occluded target region during post-submit capture.",
            ),
        }
    )
    router = MultiLoraVetoRouter(base_model=BASE_MODEL, registry=reg, backend=backend)

    r1 = router.evaluate(
        VetoInferenceRequest(
            request_id="req-1",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-1",
            pre_submit_image_sha256="1" * 64,
            post_submit_image_sha256="2" * 64,
            dom_claims_success=True,
        )
    )
    assert r1.adapter_id == "veto-tenant1-alpha-intake"
    assert r1.decision is VetoDecision.VETO
    assert r1.dom_override_blocked is True
    assert r1.calibrated_fault_prob == pytest.approx(calibrate_probability(0.2, 0.5))
    assert router.swap_count == 1

    r2 = router.evaluate(
        VetoInferenceRequest(
            request_id="req-2",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-1",
            pre_submit_image_sha256="3" * 64,
            post_submit_image_sha256="4" * 64,
            dom_claims_success=True,
        )
    )
    assert r2.adapter_id == "veto-tenant1-alpha-intake"
    assert r2.decision is VetoDecision.ALLOW
    assert r2.dom_override_blocked is False
    # Reused active adapter without swapping
    assert router.swap_count == 1

    r3 = router.evaluate(
        VetoInferenceRequest(
            request_id="req-3",
            app_id="brand-new-app",
            pre_submit_image_sha256="5" * 64,
            post_submit_image_sha256="6" * 64,
            dom_claims_success=True,
        )
    )
    assert r3.adapter_id == "veto-base"
    assert r3.decision is VetoDecision.HALT_ESCALATE
    assert r3.abstained is True
    assert r3.dom_override_blocked is True
    assert "occluded" in r3.reason
    assert router.swap_count == 2
    assert router.request_count == 3
    assert backend.swap_history == ["veto-tenant1-alpha-intake", "veto-base"]


def test_router_refuses_mismatched_base_model_and_backend_helpers() -> None:
    base = _make_spec("veto-base")
    wrong_base = _make_spec("veto-other", base_model="other-model-7b", app_id="app-z")
    reg = AppAdapterRegistry(default_base_adapter=base, adapters=[wrong_base])
    backend = _MockHotSwapBackend({})
    router = MultiLoraVetoRouter(base_model=BASE_MODEL, registry=reg, backend=backend)

    req = VetoInferenceRequest(
        request_id="req-mismatch",
        app_id="app-z",
        step_nonce="run-42-step-3",
        pre_submit_image_sha256="1" * 64,
        post_submit_image_sha256="2" * 64,
    )
    with pytest.raises(ValueError, match="targets base_model"):
        router.evaluate(req)

    payload = vllm_request_payload(base, req)
    assert payload["model"] == "veto-base"
    assert payload["metadata"]["base_model"] == BASE_MODEL
    assert payload["metadata"]["step_nonce"] == "run-42-step-3"
    assert "expected_field_matches" not in payload
    assert ollama_model_tag(base) == f"{BASE_MODEL}:veto-base"
    ollama_spec = _make_spec("veto-oll", adapter_uri="ollama://openadapt/veto:v2")
    assert ollama_model_tag(ollama_spec) == "openadapt/veto:v2"


def test_hmac_field_value_and_strict_schema_guards() -> None:
    d1 = hmac_field_value("tenant-1-vpc-secret-key-0001", "dob", "1980-02-14")
    d2 = hmac_field_value("tenant-2-vpc-secret-key-0002", "dob", "1980-02-14")
    assert len(d1) == 64 and d1 != d2
    with pytest.raises(ValueError, match="16 bytes"):
        hmac_field_value("short", "dob", "1980-02-14")
    with pytest.raises(ValueError, match="field_slug"):
        hmac_field_value("tenant-1-vpc-secret-key-0001", "Jane Doe DOB", "1980-02-14")

    # Free-text image reference is rejected (must be 64 lowercase hex)
    with pytest.raises(ValidationError):
        VetoInferenceRequest(
            request_id="req-bad-img",
            app_id="app-alpha",
            pre_submit_image_sha256="Jane Doe MRN 0123456 screenshot",
            post_submit_image_sha256=SHA_B,
        )
    # Free-text field hash key is rejected
    with pytest.raises(ValidationError):
        VetoInferenceRequest(
            request_id="req-bad-key",
            app_id="app-alpha",
            pre_submit_image_sha256=SHA_A,
            post_submit_image_sha256=SHA_B,
            expected_field_hashes={"Jane Doe DOB 1980-02-14": d1},
        )
    # Caller self-vouching field (expected_field_matches) is forbidden
    with pytest.raises(ValidationError):
        VetoInferenceRequest.model_validate(
            {
                "request_id": "req-self-vouch",
                "app_id": "app-alpha",
                "pre_submit_image_sha256": SHA_A,
                "post_submit_image_sha256": SHA_B,
                "expected_field_matches": {"dob": True},
            }
        )


def test_export_inbox_training_jsonl_dedup_and_tenant_isolation(tmp_path: Path) -> None:
    hmac_a = hmac_field_value("tenant-1-vpc-secret-key-0001", "email", "a@example.com")
    hmac_b = hmac_field_value("tenant-1-vpc-secret-key-0001", "email", "b@example.com")
    hmac_c = hmac_field_value("tenant-2-vpc-secret-key-0002", "email", "c@example.com")
    items = [
        InboxResolutionItem(
            item_id="inbox-1",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-1",
            label=InboxResolutionLabel.TRUE_FAULT,
            pre_submit_image_ref=SHA_A,
            post_submit_image_ref=SHA_B,
            expected_field_hashes={"email": hmac_a},
            resolved_at="2026-11-22T18:00:00Z",
        ),
        # Duplicate content fingerprint (different item_id) -> must deduplicate
        InboxResolutionItem(
            item_id="inbox-1-dup",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-1",
            label=InboxResolutionLabel.TRUE_FAULT,
            pre_submit_image_ref=SHA_A,
            post_submit_image_ref=SHA_B,
            expected_field_hashes={"email": hmac_a},
            resolved_at="2026-11-22T18:05:00Z",
        ),
        InboxResolutionItem(
            item_id="inbox-2",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-1",
            label=InboxResolutionLabel.FALSE_ALARM,
            pre_submit_image_ref=SHA_C,
            post_submit_image_ref=SHA_D,
            expected_field_hashes={"email": hmac_b},
            resolved_at="2026-11-22T18:10:00Z",
        ),
        # Different tenant -> excluded when exporting tenant-1 VPC dataset
        InboxResolutionItem(
            item_id="inbox-3",
            app_id="app-alpha",
            workflow_id="intake-v1",
            tenant_id="tenant-2",
            label=InboxResolutionLabel.TRUE_FAULT,
            pre_submit_image_ref=SHA_D,
            post_submit_image_ref=SHA_E,
            expected_field_hashes={"email": hmac_c},
            resolved_at="2026-11-22T18:15:00Z",
        ),
    ]

    # Raw form values are rejected by value-free schema
    with pytest.raises(ValidationError):
        InboxResolutionItem(
            item_id="inbox-raw",
            app_id="app-alpha",
            tenant_id="tenant-1",
            label=InboxResolutionLabel.TRUE_FAULT,
            pre_submit_image_ref=SHA_A,
            post_submit_image_ref=SHA_B,
            expected_field_hashes={"email": "patient@example.com"},
            resolved_at="2026-11-22T18:00:00Z",
        )

    out_path = tmp_path / "byoc" / "tenant-1-train.jsonl"
    count = export_inbox_training_jsonl(
        items, out_path, tenant_id="tenant-1", app_id="app-alpha"
    )
    assert count == 2

    lines = [
        json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 2
    assert all(rec["metadata"]["tenant_id"] == "tenant-1" for rec in lines)
    assert all(rec["metadata"]["app_id"] == "app-alpha" for rec in lines)
    assert all("reviewer_note" not in rec["messages"][2]["content"] for rec in lines)
    verdicts = [json.loads(rec["messages"][2]["content"])["verdict"] for rec in lines]
    assert verdicts == ["FAULT", "CLEAN"]
