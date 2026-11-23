"""Tests for Mitigation #4: Shared BYOC Multi-LoRA Veto HTTP Server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from openadapt_flow.byoc_veto_server import (
    ByocVetoServer,
    RunBinding,
    create_byoc_veto_server,
    post_verify_request,
)
from openadapt_flow.multi_lora_veto import (
    AdapterSpec,
    AppAdapterRegistry,
    MultiLoraVetoRouter,
    RawVetoScore,
    TenantScope,
    VetoDecision,
    VetoInferenceRequest,
)

_BASE_MODEL = "Qwen/Qwen3.5-9B"
_SHA_BASE = "0" * 64
_SHA_A = "1" * 64
_SHA_B = "2" * 64
_IMG_PRE = "a" * 64
_IMG_POST = "b" * 64


class _StubLoraBackend:
    """In-memory multi-LoRA backend for deterministic HTTP server tests."""

    def __init__(
        self, scores_by_request: dict[str, RawVetoScore] | None = None
    ) -> None:
        self._active: str | None = None
        self._scores = dict(scores_by_request or {})
        self.fail_next = False
        self.loaded_sequence: list[str] = []

    @property
    def active_adapter_id(self) -> str | None:
        return self._active

    def ensure_adapter(self, spec: AdapterSpec) -> bool:
        self.loaded_sequence.append(spec.adapter_id)
        if self._active == spec.adapter_id:
            return False
        self._active = spec.adapter_id
        return True

    def score(self, spec: AdapterSpec, request: VetoInferenceRequest) -> RawVetoScore:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated GPU OOM")
        return self._scores.get(
            request.request_id,
            RawVetoScore(fault_logit=-3.0, rationale=f"clean via {spec.adapter_id}"),
        )


def _build_router(
    scores: dict[str, RawVetoScore] | None = None,
) -> tuple[MultiLoraVetoRouter, _StubLoraBackend]:
    registry = AppAdapterRegistry(
        default_base_adapter=AdapterSpec(
            adapter_id="veto-base-9b-v4",
            base_model=_BASE_MODEL,
            adapter_uri="local:///adapters/veto-base-9b-v4",
            sha256=_SHA_BASE,
            calibrated_threshold=0.5,
            temperature=0.95,
            tenant_scope=TenantScope.PUBLIC,
        ),
        adapters=[
            AdapterSpec(
                adapter_id="veto-clinic-a-ehr",
                base_model=_BASE_MODEL,
                adapter_uri="local:///adapters/clinic-a-ehr",
                sha256=_SHA_A,
                calibrated_threshold=0.5,
                temperature=0.95,
                tenant_scope=TenantScope.BYOC,
                tenant_id="clinic-a",
                app_id="ehr-portal",
                workflow_id="intake",
            ),
            AdapterSpec(
                adapter_id="veto-clinic-b-ehr",
                base_model=_BASE_MODEL,
                adapter_uri="local:///adapters/clinic-b-ehr",
                sha256=_SHA_B,
                calibrated_threshold=0.5,
                temperature=0.95,
                tenant_scope=TenantScope.BYOC,
                tenant_id="clinic-b",
                app_id="ehr-portal",
                workflow_id="intake",
            ),
        ],
    )
    backend = _StubLoraBackend(scores)
    router = MultiLoraVetoRouter(
        base_model=_BASE_MODEL, registry=registry, backend=backend
    )
    return router, backend


def _raw_http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_server_refuses_unauthenticated_duplicate_key_or_missing_allowlist_startup() -> (
    None
):
    router, _ = _build_router()
    with pytest.raises(ValueError, match="at least one tenant key"):
        ByocVetoServer(router=router, tenant_api_keys={}, tenant_allowed_apps={})
    with pytest.raises(ValueError, match="tenant_allowed_apps"):
        ByocVetoServer(
            router=router,
            tenant_api_keys={"clinic-a": "secret-a"},
            tenant_allowed_apps={},
        )
    with pytest.raises(ValueError, match="Duplicate API keys"):
        ByocVetoServer(
            router=router,
            tenant_api_keys={"clinic-a": "shared-secret", "clinic-b": "shared-secret"},
            tenant_allowed_apps={
                "clinic-a": {"ehr-portal"},
                "clinic-b": {"ehr-portal"},
            },
        )


def test_live_http_roundtrip_hotswap_and_abstain_halts() -> None:
    router, backend = _build_router(
        {
            "req-abstain": RawVetoScore(
                fault_logit=-4.0,
                abstain=True,
                abstain_reason="occluding toast over target field",
            ),
            "req-veto": RawVetoScore(
                fault_logit=3.5,
                rationale="homoglyph mismatch O vs 0",
            ),
        }
    )
    keys = {"clinic-a": "key-clinic-a-secret", "clinic-b": "key-clinic-b-secret"}
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal"}, "clinic-b": {"ehr-portal"}},
    ) as srv:
        status, health = _raw_http(f"{srv.base_url}/healthz")
        assert status == 200
        assert health == {"status": "ok"}

        # 1. Clinic A routes to its BYOC adapter -> ALLOW
        out_a = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-a"],
            tenant_id="clinic-a",
            payload={
                "request_id": "req-a-clean",
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert out_a.adapter_id == "veto-clinic-a-ehr"
        assert out_a.decision is VetoDecision.ALLOW

        # 2. Clinic B hot-swaps to its isolated BYOC adapter -> VETO
        out_b = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-b"],
            tenant_id="clinic-b",
            payload={
                "request_id": "req-veto",
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "dom_claims_success": True,
            },
        )
        assert out_b.adapter_id == "veto-clinic-b-ehr"
        assert out_b.decision is VetoDecision.VETO
        assert out_b.dom_override_blocked is True

        # 3. ABSTAIN -> HALT_ESCALATE even when dom_claims_success=True
        out_abstain = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-a"],
            payload={
                "request_id": "req-abstain",
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "dom_claims_success": True,
            },
        )
        assert out_abstain.decision is VetoDecision.HALT_ESCALATE
        assert out_abstain.abstained is True
        assert out_abstain.dom_override_blocked is True

        # 4. Healthz stays minimal (never leaks tenant adapter IDs or counters)
        status, health2 = _raw_http(f"{srv.base_url}/healthz")
        assert status == 200
        assert health2 == {"status": "ok"}
        assert router.request_count == 3
        assert router.swap_count == 3
        assert backend.loaded_sequence == [
            "veto-clinic-a-ehr",
            "veto-clinic-b-ehr",
            "veto-clinic-a-ehr",
        ]


def test_non_loopback_requires_tls_and_client_fails_closed_when_server_stopped() -> (
    None
):
    router, _ = _build_router()
    keys = {"clinic-a": "key-clinic-a-secret"}

    # Non-loopback bind without ssl_context is refused
    with pytest.raises(ValueError, match="requires ssl_context"):
        ByocVetoServer(
            router=router,
            tenant_api_keys=keys,
            tenant_allowed_apps={"clinic-a": {"ehr-portal"}},
            host="0.0.0.0",
        )

    # Client refuses plaintext http:// to non-loopback host (fail closed)
    remote_plain = post_verify_request(
        "http://veto.internal.example:8443",
        api_key=keys["clinic-a"],
        payload={
            "request_id": "req-plain",
            "app_id": "ehr-portal",
            "pre_submit_image_sha256": _IMG_PRE,
            "post_submit_image_sha256": _IMG_POST,
        },
    )
    assert remote_plain.decision is VetoDecision.HALT_ESCALATE
    assert "http://" in remote_plain.reason

    # Client catches TypeError (non-JSON-serializable payload) and fails closed
    bad_ser = post_verify_request(
        "http://127.0.0.1:9",
        api_key=keys["clinic-a"],
        payload={"request_id": "req-ser", "bad": object()},
    )
    assert bad_ser.decision is VetoDecision.HALT_ESCALATE
    assert "TypeError" in bad_ser.reason

    # 422 response never echoes raw client input values
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal"}},
    ) as srv:
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": "req-bad",
                "app_id": "ehr-portal",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "expected_field_hashes": {"ssn": "123-45-6789"},
            },
        )
        assert code == 422
        assert "123-45-6789" not in json.dumps(body)

        # Dropping workflow_id when a workflow adapter exists is blocked (anti-downgrade -> 403)
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": "req-drop-wf",
                "app_id": "ehr-portal",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"

        # Unauthorized app_id for tenant -> 403
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": "req-unauth-app",
                "app_id": "other-app",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"
        stopped_url = srv.base_url

    # Server is now stopped -> post_verify_request fails closed to HALT_ESCALATE
    offline = post_verify_request(
        stopped_url,
        api_key=keys["clinic-a"],
        payload={
            "request_id": "req-offline",
            "app_id": "ehr-portal",
            "pre_submit_image_sha256": _IMG_PRE,
            "post_submit_image_sha256": _IMG_POST,
            "dom_claims_success": True,
        },
        timeout_s=1.0,
    )
    assert offline.decision is VetoDecision.HALT_ESCALATE
    assert offline.abstained is True
    assert offline.dom_override_blocked is True


def test_http_tenant_auth_cross_tenant_rejection_and_fail_closed_503() -> None:
    router, backend = _build_router()
    keys = {"clinic-a": "key-clinic-a-secret", "clinic-b": "key-clinic-b-secret"}
    base_payload = {
        "request_id": "req-1",
        "app_id": "ehr-portal",
        "workflow_id": "intake",
        "pre_submit_image_sha256": _IMG_PRE,
        "post_submit_image_sha256": _IMG_POST,
    }
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal"}, "clinic-b": {"ehr-portal"}},
        run_bindings={
            "req-1": RunBinding(
                tenant_id="clinic-a",
                app_id="ehr-portal",
                workflow_id="intake",
                step_nonce="nonce-1",
            )
        },
    ) as srv:
        base_payload = {**base_payload, "step_nonce": "nonce-1"}
        # Mismatched step_nonce against server-side run binding -> 403
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={**base_payload, "step_nonce": "replayed-nonce"},
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"
        # Missing auth -> 401
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify", method="POST", payload=base_payload
        )
        assert code == 401
        assert body["decision"] == "HALT_ESCALATE"

        # Invalid auth -> 401
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": "Bearer wrong-key"},
            payload=base_payload,
        )
        assert code == 401
        assert body["decision"] == "HALT_ESCALATE"

        # Cross-tenant payload tenant_id -> 403
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"X-OpenAdapt-Tenant-Key": keys["clinic-a"]},
            payload={**base_payload, "tenant_id": "clinic-b"},
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"

        # Cross-tenant requested_adapter_id -> 403
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={**base_payload, "requested_adapter_id": "veto-clinic-b-ehr"},
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"

        # Unexpected backend exception -> fail-closed 503 with HALT_ESCALATE
        backend.fail_next = True
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload=base_payload,
        )
        assert code == 503
        assert body["decision"] == "HALT_ESCALATE"

        # Unknown route -> 404
        code, _ = _raw_http(f"{srv.base_url}/v1/unknown")
        assert code == 404


def test_client_rejects_mismatched_response_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ForgedResponse:
        def __enter__(self) -> _ForgedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "request_id": "forged-other-req",
                    "adapter_id": "veto-clinic-a-ehr",
                    "base_model": _BASE_MODEL,
                    "decision": "ALLOW",
                    "calibrated_fault_prob": 0.01,
                    "calibrated_threshold": 0.5,
                    "temperature": 1.0,
                    "abstained": False,
                    "dom_override_blocked": False,
                    "reason": "replayed",
                }
            ).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _ForgedResponse())
    res = post_verify_request(
        "http://127.0.0.1:9999",
        api_key="k",
        payload={
            "request_id": "expected-req-123",
            "app_id": "ehr-portal",
            "pre_submit_image_sha256": _IMG_PRE,
            "post_submit_image_sha256": _IMG_POST,
        },
    )
    assert res.decision is VetoDecision.HALT_ESCALATE
    assert "request_id" in res.reason
