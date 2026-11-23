"""Tests for Mitigation #4: Shared BYOC Multi-LoRA Veto HTTP Server."""

from __future__ import annotations

import hashlib
import json
import socket
import time
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
_SHA_C = "3" * 64
_IMG_PRE = "a" * 64
_IMG_POST = "b" * 64
_IMG_OTHER = "c" * 64
_HMAC_KEY_A = b"clinic-a-vpc-only-hmac-key-00001"
_HMAC_KEY_B = b"clinic-b-vpc-only-hmac-key-00002"


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
                adapter_id="veto-clinic-a-ehr-app-fallback",
                base_model=_BASE_MODEL,
                adapter_uri="local:///adapters/clinic-a-ehr-app",
                sha256=_SHA_C,
                calibrated_threshold=0.95,
                temperature=1.0,
                tenant_scope=TenantScope.BYOC,
                tenant_id="clinic-a",
                app_id="ehr-portal",
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
            AdapterSpec(
                adapter_id="veto-lenient-domain",
                base_model=_BASE_MODEL,
                adapter_uri="local:///adapters/lenient-domain",
                sha256=_SHA_C,
                calibrated_threshold=0.99,
                temperature=1.0,
                tenant_scope=TenantScope.PUBLIC,
                domain_pattern="*.lenient.example",
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
    with pytest.raises(ValueError, match="at least 16 bytes"):
        ByocVetoServer(
            router=router,
            tenant_api_keys={"clinic-a": "secret-a"},
            tenant_allowed_apps={"clinic-a": {"ehr-portal"}},
            tenant_hmac_keys={"clinic-a": b"too-short"},
        )


def test_live_http_roundtrip_hotswap_and_abstain_halts() -> None:
    router, backend = _build_router()
    keys = {"clinic-a": "key-clinic-a-secret", "clinic-b": "key-clinic-b-secret"}
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal"}, "clinic-b": {"ehr-portal"}},
        tenant_hmac_keys={"clinic-a": _HMAC_KEY_A, "clinic-b": _HMAC_KEY_B},
    ) as srv:
        status, health = _raw_http(f"{srv.base_url}/healthz")
        assert status == 200
        assert health == {"status": "ok"}

        # 1. Clinic A routes to its BYOC adapter -> ALLOW (with server-issued HMAC binding)
        b_a = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
            expected_field_values={"dob": "1980-02-14"},
        )
        assert srv.verify_field_hmac(
            tenant_id="clinic-a",
            field_slug="dob",
            observed_value="1980-02-14",
            expected_digest=b_a.expected_field_hashes["dob"],
        )
        assert not srv.verify_field_hmac(
            tenant_id="clinic-b",
            field_slug="dob",
            observed_value="1980-02-14",
            expected_digest=b_a.expected_field_hashes["dob"],
        )

        out_a = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-a"],
            tenant_id="clinic-a",
            payload={
                "request_id": b_a.request_id,
                "step_nonce": b_a.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "expected_field_hashes": b_a.expected_field_hashes,
            },
        )
        assert out_a.adapter_id == "veto-clinic-a-ehr"
        assert out_a.decision is VetoDecision.ALLOW

        # Replaying the exact same binding/nonce -> 409 HALT_ESCALATE
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_a.request_id,
                "step_nonce": b_a.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "expected_field_hashes": b_a.expected_field_hashes,
            },
        )
        assert code == 409
        assert body["decision"] == "HALT_ESCALATE"

        # 2. Clinic B hot-swaps to its isolated BYOC adapter -> VETO
        b_b = srv.issue_run_binding(
            tenant_id="clinic-b",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        backend._scores[b_b.request_id] = RawVetoScore(
            fault_logit=3.5, rationale="homoglyph mismatch O vs 0"
        )
        out_b = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-b"],
            tenant_id="clinic-b",
            payload={
                "request_id": b_b.request_id,
                "step_nonce": b_b.step_nonce,
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
        b_abs = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        backend._scores[b_abs.request_id] = RawVetoScore(
            fault_logit=-4.0,
            abstain=True,
            abstain_reason="occluding toast over target field",
        )
        out_abstain = post_verify_request(
            srv.base_url,
            api_key=keys["clinic-a"],
            payload={
                "request_id": b_abs.request_id,
                "step_nonce": b_abs.step_nonce,
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


def test_anti_steering_workflow_downgrade_default_fallback_and_hmac_enforcement() -> (
    None
):
    router, _ = _build_router()
    keys = {"clinic-a": "key-clinic-a-secret"}
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal", "unadapted-app"}},
        tenant_hmac_keys={"clinic-a": _HMAC_KEY_A},
    ) as srv:
        # 1. Caller-invented request_id with no server binding -> 403 HALT_ESCALATE
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": "caller-invented-id",
                "step_nonce": "caller-nonce",
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"

        # 2. Bogus workflow_id cannot downgrade from workflow adapter to looser app adapter
        b_bogus = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="wf-bogus",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_bogus.request_id,
                "step_nonce": b_bogus.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "wf-bogus",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"
        assert "workflow" in body["error"].lower()

        # 3. Allowed app with NO registered adapter does NOT fall back to default_base_adapter
        #    and caller-supplied payload.domain is ignored (cannot steer to lenient domain adapter)
        b_unadapted = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="unadapted-app",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_unadapted.request_id,
                "step_nonce": b_unadapted.step_nonce,
                "app_id": "unadapted-app",
                "domain": "evil.lenient.example",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"
        assert "default adapter fallback is disabled" in body["error"]

        # 4. Plain unkeyed sha256("1980-02-14") in expected_field_hashes is rejected
        b_hmac = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
            expected_field_values={"dob": "1980-02-14"},
        )
        plain_sha256 = hashlib.sha256(b"1980-02-14").hexdigest()
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_hmac.request_id,
                "step_nonce": b_hmac.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "expected_field_hashes": {"dob": plain_sha256},
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"
        assert "HMAC" in body["error"]

        # 5. Swapped image digest against server-side RunBinding -> 403 HALT_ESCALATE
        b_img = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_img.request_id,
                "step_nonce": b_img.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_OTHER,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"


def test_non_loopback_requires_tls_and_client_fails_closed() -> None:
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

    # Malformed URL fails closed instead of raising ValueError
    bad_url = post_verify_request(
        "http://[::1",
        api_key=keys["clinic-a"],
        payload={"request_id": "req-bad-url"},
    )
    assert bad_url.decision is VetoDecision.HALT_ESCALATE
    assert "URL error" in bad_url.reason

    # Non-mapping payload fails closed instead of raising AttributeError
    non_map = post_verify_request(
        "http://127.0.0.1:9",
        api_key=keys["clinic-a"],
        payload=["not", "a", "mapping"],  # type: ignore[arg-type]
    )
    assert non_map.decision is VetoDecision.HALT_ESCALATE
    assert "Mapping" in non_map.reason

    # Missing request_id fails closed
    missing_rid = post_verify_request(
        "http://127.0.0.1:9",
        api_key=keys["clinic-a"],
        payload={"app_id": "ehr-portal"},
    )
    assert missing_rid.decision is VetoDecision.HALT_ESCALATE
    assert "request_id" in missing_rid.reason

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
                "step_nonce": "nonce-bad",
                "app_id": "ehr-portal",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
                "expected_field_hashes": {"ssn": "123-45-6789"},
            },
        )
        assert code == 422
        assert "123-45-6789" not in json.dumps(body)

        # Unauthorized app_id for tenant -> 403
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": "req-unauth-app",
                "step_nonce": "nonce-unauth",
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
            "step_nonce": "nonce-offline",
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
                pre_submit_image_sha256=_IMG_PRE,
                post_submit_image_sha256=_IMG_POST,
            )
        },
    ) as srv:
        base_payload = {
            "request_id": "req-1",
            "step_nonce": "nonce-1",
            "app_id": "ehr-portal",
            "workflow_id": "intake",
            "pre_submit_image_sha256": _IMG_PRE,
            "post_submit_image_sha256": _IMG_POST,
        }
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

        # Cross-tenant requested_adapter_id -> 403 (consumes req-1)
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={**base_payload, "requested_adapter_id": "veto-clinic-b-ehr"},
        )
        assert code == 403
        assert body["decision"] == "HALT_ESCALATE"

        # Unexpected backend exception -> fail-closed 503 with HALT_ESCALATE
        b_503 = srv.issue_run_binding(
            tenant_id="clinic-a",
            app_id="ehr-portal",
            workflow_id="intake",
            pre_submit_image_sha256=_IMG_PRE,
            post_submit_image_sha256=_IMG_POST,
        )
        backend.fail_next = True
        code, body = _raw_http(
            f"{srv.base_url}/v1/verify",
            method="POST",
            headers={"Authorization": f"Bearer {keys['clinic-a']}"},
            payload={
                "request_id": b_503.request_id,
                "step_nonce": b_503.step_nonce,
                "app_id": "ehr-portal",
                "workflow_id": "intake",
                "pre_submit_image_sha256": _IMG_PRE,
                "post_submit_image_sha256": _IMG_POST,
            },
        )
        assert code == 503
        assert body["decision"] == "HALT_ESCALATE"

        # Unknown route -> 404
        code, _ = _raw_http(f"{srv.base_url}/v1/unknown")
        assert code == 404


def test_server_read_timeout_and_connection_cap() -> None:
    router, _ = _build_router()
    keys = {"clinic-a": "key-clinic-a-secret"}
    with create_byoc_veto_server(
        router=router,
        tenant_api_keys=keys,
        tenant_allowed_apps={"clinic-a": {"ehr-portal"}},
        request_timeout_s=0.25,
        max_concurrent_connections=1,
    ) as srv:
        host, port = srv.server_address
        # Hold the single connection slot open with an idle TCP peer.
        idle = socket.create_connection((host, port), timeout=2.0)
        try:
            # Give the server a brief moment to accept the idle connection.
            time.sleep(0.05)
            # Second concurrent connection is dropped immediately when cap=1.
            with pytest.raises((OSError, urllib.error.URLError)):
                _raw_http(f"{srv.base_url}/healthz")
            # After request_timeout_s elapses, the idle peer is timed out and
            # the slot is released so subsequent requests succeed.
            time.sleep(0.35)
            status, health = _raw_http(f"{srv.base_url}/healthz")
            assert status == 200
            assert health == {"status": "ok"}
        finally:
            idle.close()


@pytest.mark.parametrize(
    "forged_body,expected_substr",
    [
        (
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
            },
            "request_id",
        ),
        (
            {
                "request_id": "expected-req-123",
                "adapter_id": "veto-clinic-a-ehr",
                "base_model": _BASE_MODEL,
                "decision": "ALLOW",
                "calibrated_fault_prob": 0.01,
                "calibrated_threshold": 0.5,
                "temperature": 1.0,
                "abstained": True,
                "dom_override_blocked": False,
                "reason": "contradictory abstain+allow",
            },
            "invariant",
        ),
        (
            {
                "request_id": "expected-req-123",
                "adapter_id": "veto-clinic-a-ehr",
                "base_model": _BASE_MODEL,
                "decision": "ALLOW",
                "calibrated_fault_prob": 0.85,
                "calibrated_threshold": 0.5,
                "temperature": 1.0,
                "abstained": False,
                "dom_override_blocked": False,
                "reason": "contradictory prob>=threshold+allow",
            },
            "invariant",
        ),
    ],
)
def test_client_rejects_mismatched_request_id_and_contradictory_allow(
    monkeypatch: pytest.MonkeyPatch,
    forged_body: dict[str, Any],
    expected_substr: str,
) -> None:
    class _ForgedResponse:
        def __enter__(self) -> _ForgedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(forged_body).encode("utf-8")

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
    assert expected_substr in res.reason
