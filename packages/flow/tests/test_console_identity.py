"""Deployment identity authorization on the real console HTTP boundary.

Synthetic server-side sessions exercise the integration seam. These tests do
not implement or qualify an OIDC/SAML provider, a passkey, or a person signer.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openadapt_flow.console.app import create_app
from openadapt_flow.console.identity import (
    ConsoleIdentity,
    ConsoleIdentityPolicy,
    ConsoleIdentityRefused,
    ConsoleRequestBinding,
    ConsoleRole,
    authorize_identity,
    required_role,
)
from openadapt_flow.runtime.durable.attended import AttendedActionStore
from tests.test_attended_actions import _paused

_POLICY = ConsoleIdentityPolicy(
    issuer="https://identity.example.test/issuer",
    audience="console-synthetic",
    tenant="synthetic-tenant",
)


class SyntheticResolver:
    """Test-owned identity state; HTTP headers and body cannot choose it."""

    def __init__(self, role=ConsoleRole.OPERATOR):
        self.role = role
        self.revoked = False
        self.failure = False
        self.overrides = {}
        self.bindings = []
        self.last = None
        self.replay = False

    async def __call__(self, _request, binding):
        self.bindings.append(binding)
        if self.failure:
            raise RuntimeError("provider exception with protected diagnostic")
        if self.revoked:
            return None
        if self.replay:
            return self.last
        now = datetime.now(timezone.utc)
        fields = {
            "issuer": _POLICY.issuer,
            "audience": _POLICY.audience,
            "tenant": _POLICY.tenant,
            "subject": "person-001",
            "session_sha256": "sha256:" + "a" * 64,
            "role": self.role,
            "authentication_method": "oidc",
            "authenticated_at": now - timedelta(seconds=10),
            "verified_at": now,
            "expires_at": now + timedelta(minutes=5),
            "request_binding_sha256": binding.digest,
        }
        fields.update(self.overrides)
        self.last = ConsoleIdentity(**fields)
        return self.last


def _app(tmp_path, resolver, **kwargs):
    bundles, runs = tmp_path / "bundles", tmp_path / "runs"
    bundles.mkdir()
    runs.mkdir()
    return create_app(
        bundles, runs, identity_policy=_POLICY, identity_resolver=resolver, **kwargs
    )


def _client(app):
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )


def _attended_app(tmp_path, resolver):
    _workflow, bundle, run, _store, capability = _paused(tmp_path)
    app = create_app(
        bundle.parent,
        run.parent,
        attend=True,
        allow_actions=True,
        identity_policy=_POLICY,
        identity_resolver=resolver,
    )
    return app, run, capability


def _task(client):
    item = client.get("/api/attention").json()[0]
    detail = client.get(f"/api/attention/{item['id']}").json()
    path = f"/api/attention/{item['id']}/actions/teach"
    payload = {
        "capability_digest": item["capability"]["digest"],
        "task_digest": detail["task_digest"],
        "task_signature": detail["task"]["signature"],
        "idempotency_key": "identity-http-teach-0001",
        "action": "teach",
        "disposition": "teach_requested",
    }
    return path, payload


def test_identity_configuration_requires_explicit_trusted_pair(tmp_path):
    for policy, resolver in (
        (_POLICY, None),
        (None, SyntheticResolver()),
        (_POLICY, 3),
    ):
        with pytest.raises(ValueError):
            create_app(
                tmp_path, tmp_path, identity_policy=policy, identity_resolver=resolver
            )


@pytest.mark.parametrize("role", list(ConsoleRole))
def test_each_known_role_reads_without_exposing_identity(tmp_path, role):
    resolver = SyntheticResolver(role)
    client = _client(_app(tmp_path, resolver))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert resolver.last.subject not in response.text
    assert "session_sha256" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "https://other.example.test"},
        {"audience": "another-console"},
        {"tenant": "other-tenant"},
        {"request_binding_sha256": "sha256:" + "0" * 64},
        {"role": "owner"},
        {"authentication_method": "ingest_token"},
        {"authentication_method": "webauthn"},
        {"subject": "display name with spaces"},
        {"unexpected_claim": "operator"},
        {"authenticated_at": datetime.now(timezone.utc) - timedelta(days=2)},
        {"authenticated_at": datetime.now(timezone.utc) + timedelta(days=2)},
        {"verified_at": datetime.now(timezone.utc) - timedelta(days=1)},
        {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)},
        {"expires_at": datetime(2026, 1, 1)},
    ],
)
def test_invalid_or_unsupported_claims_refuse_before_route(tmp_path, overrides):
    resolver = SyntheticResolver()
    resolver.overrides = overrides
    response = _client(_app(tmp_path, resolver)).get("/api/health")
    assert response.status_code == 403
    assert response.json() == {"detail": "console identity authorization refused"}
    assert "protected diagnostic" not in response.text


def test_resolver_is_rechecked_for_revocation_error_and_session_replay(tmp_path):
    resolver = SyntheticResolver()
    client = _client(_app(tmp_path, resolver))
    assert client.get("/api/health").status_code == 200
    resolver.replay = True
    assert client.get("/api/health").status_code == 403
    resolver.replay = False
    resolver.revoked = True
    assert client.get("/api/health").status_code == 403
    resolver.revoked = False
    resolver.failure = True
    assert client.get("/api/health").status_code == 403
    assert len(resolver.bindings) == 4
    assert len({binding.nonce for binding in resolver.bindings}) == 4


@pytest.mark.parametrize("value", [{"role": "administrator"}, "person-001", None])
def test_wire_shaped_resolver_outputs_are_not_identity_objects(tmp_path, value):
    async def resolver(_request, _binding):
        return value

    assert _client(_app(tmp_path, resolver)).get("/api/health").status_code == 403


@pytest.mark.parametrize(
    ("endpoint", "action", "role", "allowed"),
    [
        ("execute_attention_action", "teach", ConsoleRole.READER, False),
        ("execute_attention_action", "teach", ConsoleRole.OPERATOR, True),
        ("execute_attention_action", "teach", ConsoleRole.ADMINISTRATOR, True),
        ("execute_skill_action", "promote", ConsoleRole.OPERATOR, False),
        ("execute_skill_action", "promote", ConsoleRole.ADMINISTRATOR, True),
        ("execute_bundle_action", "certify", ConsoleRole.READER, False),
        ("execute_run_action", "approve", ConsoleRole.OPERATOR, True),
    ],
)
def test_closed_role_action_permissions(endpoint, action, role, allowed):
    now = datetime.now(timezone.utc)
    binding = ConsoleRequestBinding(
        method="POST",
        authority="127.0.0.1",
        raw_path_hex="2f",
        query_hex="",
        body_sha256="sha256:" + "a" * 64,
        nonce="server-only",
        created_at=now,
    )
    identity = ConsoleIdentity(
        issuer=_POLICY.issuer,
        audience=_POLICY.audience,
        tenant=_POLICY.tenant,
        subject="person-001",
        session_sha256="sha256:" + "b" * 64,
        role=role,
        authentication_method="saml",
        authenticated_at=now,
        verified_at=now,
        expires_at=now + timedelta(seconds=1),
        request_binding_sha256=binding.digest,
    )
    minimum = required_role(endpoint, "POST", action)
    if allowed:
        assert (
            authorize_identity(identity, _POLICY, binding, minimum, now=now) == identity
        )
    else:
        with pytest.raises(ConsoleIdentityRefused):
            authorize_identity(identity, _POLICY, binding, minimum, now=now)


@pytest.mark.parametrize(
    "path",
    [
        "/api/not-registered",
        "/api/attention/missing/actions/invented",
        "/api/save-approvals/missing/actions/reject",
        "/api/standard-changes/missing/revert",
        "/api/workflows/missing/actions/run",
        "/api/runs/missing/actions/resume",
    ],
)
def test_unknown_and_unattributed_actions_have_no_fallback(tmp_path, path):
    resolver = SyntheticResolver(ConsoleRole.ADMINISTRATOR)
    response = _client(_app(tmp_path, resolver, attend=True, allow_actions=True)).post(
        path, json={}
    )
    assert response.status_code == 403
    assert resolver.bindings == []


def test_exact_bytes_path_query_method_authority_and_nonce_are_bound(tmp_path):
    resolver = SyntheticResolver()
    client = _client(_app(tmp_path, resolver))
    for query in ("x=%2f&x=1", "x=%2F&x=1", "x=1&x=%2f"):
        assert client.get("/api/health?" + query).status_code == 200
        binding = resolver.bindings[-1]
        assert binding.query_hex == query.encode().hex()
        assert binding.raw_path_hex == b"/api/health".hex()
        assert binding.authority == "127.0.0.1"
        assert binding.method == "GET"
        assert binding.body_sha256 == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert len({b.digest for b in resolver.bindings}) == 3


def test_identity_models_reject_forged_copy_and_ambiguous_audit_ids(tmp_path):
    resolver = SyntheticResolver()
    client = _client(_app(tmp_path, resolver))
    assert client.get("/api/health").status_code == 200
    binding, identity = resolver.bindings[-1], resolver.last
    forged = identity.model_copy(update={"role": "superuser"})
    with pytest.raises(ConsoleIdentityRefused):
        authorize_identity(forged, _POLICY, binding, ConsoleRole.READER)
    with pytest.raises(ValidationError):
        identity.subject = "another-person"
    assert (
        identity.operator_ref
        != identity.model_copy(update={"issuer": "other-issuer"}).operator_ref
    )
    assert (
        identity.operator_ref
        != identity.model_copy(update={"tenant": "other-tenant"}).operator_ref
    )
    assert (
        identity.operator_ref
        != identity.model_copy(update={"subject": "other-person"}).operator_ref
    )
    one = identity.model_copy(update={"issuer": "a/b", "tenant": "c", "subject": "d"})
    two = identity.model_copy(update={"issuer": "a", "tenant": "b/c", "subject": "d"})
    assert one.operator_ref != two.operator_ref


def test_operator_action_is_bound_to_verified_person_and_original_task_guards(tmp_path):
    resolver = SyntheticResolver()
    app, run, _capability = _attended_app(tmp_path, resolver)
    client = _client(app)
    path, payload = _task(client)
    resolver.role = ConsoleRole.READER
    assert client.post(path, json=payload).status_code == 403
    resolver.role = ConsoleRole.OPERATOR
    assert (
        client.post(path, json={**payload, "subject": "person-forged"}).status_code
        == 422
    )
    for field, value in (
        ("task_signature", "hmac-sha256:" + "0" * 64),
        ("task_digest", "sha256:" + "0" * 64),
        ("capability_digest", "sha256:" + "0" * 64),
    ):
        assert client.post(path, json={**payload, field: value}).status_code == 409
    response = client.post(
        path,
        content=json.dumps(payload, separators=(",", ":")),
        headers={
            "Content-Type": "application/json",
            "X-OpenAdapt-Subject": "person-forged",
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "demonstration_requested"
    decision = AttendedActionStore(run)._read_log().decisions[-1]
    assert decision.operator == resolver.last.operator_ref
    assert "person-forged" not in decision.operator
    binding = resolver.bindings[-1]
    assert (
        binding.body_sha256
        == "sha256:"
        + hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode()
        ).hexdigest()
    )
    client.post(path, json=payload)
    assert len(AttendedActionStore(run)._read_log().decisions) == 1


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Authorization": "Bearer oai_ingest_synthetic"}, 401),
        ({"Authorization": "Bearer synthetic-cloud-session"}, 401),
        ({"Origin": "https://other.example.test"}, 403),
        ({"X-OpenAdapt-CSRF": "bad"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Host": "not-loopback.example.test"}, 400),
    ],
)
def test_enterprise_operator_never_bypasses_original_http_guards(
    tmp_path, headers, status
):
    resolver = SyntheticResolver(ConsoleRole.ADMINISTRATOR)
    app, _run, _capability = _attended_app(tmp_path, resolver)
    client = _client(app)
    path, payload = _task(client)
    count = len(resolver.bindings)
    assert client.post(path, json=payload, headers=headers).status_code == status
    assert len(resolver.bindings) == count


def test_real_loopback_server_headless_browser_and_negative_http_controls(tmp_path):
    from playwright.sync_api import sync_playwright

    resolver = SyntheticResolver()
    app, run, _capability = _attended_app(tmp_path, resolver)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False)
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert server.started
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            headers = {
                "Authorization": f"Bearer {app.state.console_access_token}",
                "Origin": base,
                "X-OpenAdapt-CSRF": app.state.console_csrf_token,
            }
            with httpx.Client(base_url=base, headers=headers) as client:
                path, payload = _task(client)
                assert (
                    client.post(
                        path, json=payload, headers={"X-OpenAdapt-CSRF": "bad"}
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        path,
                        json=payload,
                        headers={"Origin": "https://other.example.test"},
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        path,
                        json={**payload, "task_signature": "hmac-sha256:" + "0" * 64},
                    ).status_code
                    == 409
                )
                resolver.role = ConsoleRole.READER
                assert client.post(path, json=payload).status_code == 403
                resolver.role = ConsoleRole.OPERATOR
                resolver.revoked = True
                assert client.post(path, json=payload).status_code == 403
                resolver.revoked = False
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        assert page.goto(base).status == 200
                        result = page.evaluate(
                            """async ({path, payload, token, csrf}) => {
                              const response = await fetch(path, {method: 'POST',
                                headers: {'Authorization': 'Bearer ' + token,
                                  'X-OpenAdapt-CSRF': csrf, 'Content-Type': 'application/json'},
                                body: JSON.stringify(payload)});
                              return {status: response.status, receipt: await response.json()};
                            }""",
                            {
                                "path": path,
                                "payload": payload,
                                "token": app.state.console_access_token,
                                "csrf": app.state.console_csrf_token,
                            },
                        )
                        assert result["status"] == 200
                        assert result["receipt"]["state"] == "demonstration_requested"
                        assert (
                            AttendedActionStore(run)._read_log().decisions[-1].operator
                            == resolver.last.operator_ref
                        )
                    finally:
                        browser.close()
                resolver.failure = True
                assert client.get("/api/health").status_code == 403
        finally:
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive()
