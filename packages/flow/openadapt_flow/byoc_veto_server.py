"""Lightweight Shared BYOC Multi-LoRA Veto HTTP Server for zero-GPU laptops.

Exposes a minimal stdlib ``ThreadingHTTPServer`` endpoint (``POST /v1/verify``,
``GET /healthz``) so zero-GPU laptops can route pre/post-submit visual veto
checks to a single customer-VPC GPU running :class:`MultiLoraVetoRouter`
(routing compact per-app LoRA adapters by server-authorized
``(tenant_id, app_id, workflow_id)``).

Security & fail-closed invariants:
- **Mandatory tenant-key authentication & app allow-list:** Refuses to start
  with an empty key table or without an explicit ``tenant_allowed_apps`` entry
  for every tenant; compares bearer/header tokens in constant time
  (``hmac.compare_digest``).
- **Server-side adapter/workflow authority:** Supports server-side
  ``run_bindings`` (authoritative ``request_id -> RunBinding``) and blocks
  workflow-adapter downgrade attempts (dropping ``workflow_id`` when a
  workflow-specific adapter exists for the app).
- **Non-blocking worker-thread TLS handshake:** Wraps accepted sockets inside
  worker threads with a bounded handshake timeout so idle TCP peers cannot
  stall the accept loop.
- **Fail-closed ``ABSTAIN -> HALT_ESCALATE``:** Never allows DOM success claims
  to override an abstained visual check; backend failures fail closed with
  ``503`` and ``decision="HALT_ESCALATE"``.
"""

from __future__ import annotations

import hmac
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openadapt_flow.multi_lora_veto import (
    _NONCE_RE,
    MultiLoraVetoRouter,
    VetoDecision,
    VetoInferenceRequest,
    VetoOutcome,
    _validate_sha256_hex,
    _validate_sha256_map,
)

DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_TLS_HANDSHAKE_TIMEOUT_S = 5.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RunBinding(BaseModel):
    """Server-side authoritative binding for one run/request.

    When provided to :class:`ByocVetoServer`, the server verifies that the
    authenticated tenant and payload ``app_id``, ``workflow_id``, and
    ``step_nonce`` match the server-side binding (preventing a caller from
    switching apps, dropping ``workflow_id``, or replaying older image digests).
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    step_nonce: str | None = None


class ByocVerifyPayload(BaseModel):
    """Value-free HTTP request schema for ``POST /v1/verify``.

    Carries only 64-hex SHA-256 image references (dereferenced from the
    customer-VPC artifact store by the backend) and keyed HMAC-SHA256 field
    digests (no raw form values or caller self-vouching booleans).
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    domain: str | None = None
    tenant_id: str | None = None
    step_nonce: str | None = None
    requested_adapter_id: str | None = None
    pre_submit_image_sha256: str = Field(min_length=64, max_length=64)
    post_submit_image_sha256: str = Field(min_length=64, max_length=64)
    expected_field_hashes: dict[str, str] = Field(default_factory=dict)
    dom_claims_success: bool = False

    @field_validator("pre_submit_image_sha256")
    @classmethod
    def _check_pre_image_sha256(cls, value: str) -> str:
        return _validate_sha256_hex(value, "pre_submit_image_sha256")

    @field_validator("post_submit_image_sha256")
    @classmethod
    def _check_post_image_sha256(cls, value: str) -> str:
        return _validate_sha256_hex(value, "post_submit_image_sha256")

    @field_validator("step_nonce")
    @classmethod
    def _check_step_nonce(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _NONCE_RE.match(value):
            raise ValueError(f"step_nonce must match {_NONCE_RE.pattern!r}.")
        return value

    @field_validator("expected_field_hashes")
    @classmethod
    def _check_expected_field_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_sha256_map(value)


class _ByocThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        router: MultiLoraVetoRouter,
        tenant_api_keys: dict[str, str],
        tenant_allowed_apps: dict[str, frozenset[str]],
        run_bindings: dict[str, RunBinding] | None,
        max_body_bytes: int,
        ssl_context: ssl.SSLContext | None,
        tls_handshake_timeout_s: float,
    ) -> None:
        self.router = router
        self.tenant_api_keys = dict(tenant_api_keys)
        self.tenant_allowed_apps = dict(tenant_allowed_apps)
        self.run_bindings = dict(run_bindings) if run_bindings is not None else None
        self.max_body_bytes = max_body_bytes
        self.ssl_context = ssl_context
        self.tls_handshake_timeout_s = tls_handshake_timeout_s
        self.eval_lock = threading.Lock()
        super().__init__(server_address, _ByocVetoRequestHandler)

    def finish_request(self, request: Any, client_address: Any) -> None:
        """Perform TLS handshake on the worker thread with a bounded timeout."""
        if self.ssl_context is not None:
            tls_sock: Any = None
            try:
                request.settimeout(self.tls_handshake_timeout_s)
                tls_sock = self.ssl_context.wrap_socket(
                    request,
                    server_side=True,
                    do_handshake_on_connect=False,
                )
                tls_sock.settimeout(self.tls_handshake_timeout_s)
                tls_sock.do_handshake()
                tls_sock.settimeout(None)
                super().finish_request(tls_sock, client_address)
            except (OSError, ssl.SSLError):
                return
            finally:
                if tls_sock is not None:
                    try:
                        tls_sock.close()
                    except OSError:
                        pass
            return
        super().finish_request(request, client_address)


class _ByocVetoRequestHandler(BaseHTTPRequestHandler):
    server: _ByocThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default stderr access logging to keep server output clean
        # and avoid leaking request metadata.
        return

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _extract_token(self) -> str | None:
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return token
        header_key = self.headers.get("X-OpenAdapt-Tenant-Key", "").strip()
        return header_key or None

    def _authenticate_tenant(self) -> tuple[str | None, HTTPStatus | None, str | None]:
        token = self._extract_token()
        if not token:
            return None, HTTPStatus.UNAUTHORIZED, "Missing tenant API key."

        matched_tenant: str | None = None
        for tenant_id, expected_key in self.server.tenant_api_keys.items():
            if hmac.compare_digest(token, expected_key):
                matched_tenant = tenant_id
                break
        if matched_tenant is None:
            return None, HTTPStatus.UNAUTHORIZED, "Invalid tenant API key."

        header_tenant = self.headers.get("X-OpenAdapt-Tenant-Id")
        if header_tenant is not None and header_tenant.strip() != matched_tenant:
            return (
                None,
                HTTPStatus.FORBIDDEN,
                "X-OpenAdapt-Tenant-Id does not match authenticated tenant.",
            )
        return matched_tenant, None, None

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/healthz":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return

        # Unauthenticated liveness probe: never leak tenant adapter IDs or counters.
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/v1/verify":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return

        tenant_id, err_status, err_msg = self._authenticate_tenant()
        if err_status is not None or tenant_id is None:
            self._send_json(
                err_status or HTTPStatus.UNAUTHORIZED,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": err_msg or "Unauthorized.",
                },
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": "Invalid Content-Length.",
                },
            )
            return

        if content_length <= 0 or content_length > self.server.max_body_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                if content_length > self.server.max_body_bytes
                else HTTPStatus.BAD_REQUEST,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": f"Request body size must be between 1 and {self.server.max_body_bytes} bytes.",
                },
            )
            return

        raw_body = self.rfile.read(content_length)
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": f"Invalid JSON: {exc}",
                },
            )
            return

        try:
            payload = ByocVerifyPayload.model_validate(parsed)
        except ValidationError as exc:
            # Strip raw `input` and `ctx` so bad requests never echo client values back.
            sanitized_errors = [
                {
                    "loc": list(err.get("loc", ())),
                    "msg": str(err.get("msg", "validation error")),
                    "type": str(err.get("type", "value_error")),
                }
                for err in exc.errors()
            ]
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": "Request schema validation failed.",
                    "details": sanitized_errors,
                },
            )
            return

        if payload.tenant_id is not None and payload.tenant_id != tenant_id:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": "Payload tenant_id does not match authenticated tenant.",
                },
            )
            return

        # Mandatory server-side app allow-list per tenant (fail-closed).
        allowed_apps = self.server.tenant_allowed_apps.get(tenant_id, frozenset())
        if payload.app_id not in allowed_apps:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": f"Tenant {tenant_id!r} is not authorized for app_id {payload.app_id!r}.",
                },
            )
            return

        # Server-side run binding check when configured (authoritative app/workflow/nonce).
        bound_app_id = payload.app_id
        bound_workflow_id = payload.workflow_id
        bound_step_nonce = payload.step_nonce
        if self.server.run_bindings is not None:
            binding = self.server.run_bindings.get(payload.request_id)
            if binding is None:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "decision": VetoDecision.HALT_ESCALATE.value,
                        "error": f"Unknown server-side run binding for request_id {payload.request_id!r}.",
                    },
                )
                return
            if (
                binding.tenant_id != tenant_id
                or binding.app_id != payload.app_id
                or binding.workflow_id != payload.workflow_id
                or (
                    binding.step_nonce is not None
                    and binding.step_nonce != payload.step_nonce
                )
            ):
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "decision": VetoDecision.HALT_ESCALATE.value,
                        "error": "Request parameters do not match server-side run binding.",
                    },
                )
                return
            bound_app_id = binding.app_id
            bound_workflow_id = binding.workflow_id
            bound_step_nonce = binding.step_nonce

        # Anti-downgrade guard: if workflow-specific adapters exist for this app,
        # never allow a caller to drop workflow_id to fall back to a looser app adapter.
        if (
            bound_workflow_id is None
            and self.server.router.registry.has_workflow_adapters(
                app_id=bound_app_id,
                tenant_id=tenant_id,
            )
        ):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": (
                        f"workflow_id is required for app_id {bound_app_id!r} because "
                        "workflow-specific veto adapters are registered."
                    ),
                },
            )
            return

        inference_req = VetoInferenceRequest(
            request_id=payload.request_id,
            app_id=bound_app_id,
            workflow_id=bound_workflow_id,
            domain=payload.domain,
            tenant_id=tenant_id,
            step_nonce=bound_step_nonce,
            pre_submit_image_sha256=payload.pre_submit_image_sha256,
            post_submit_image_sha256=payload.post_submit_image_sha256,
            expected_field_hashes=payload.expected_field_hashes,
            dom_claims_success=payload.dom_claims_success,
        )

        try:
            with self.server.eval_lock:
                outcome = self.server.router.evaluate(
                    inference_req,
                    requested_adapter_id=payload.requested_adapter_id,
                )
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"decision": VetoDecision.HALT_ESCALATE.value, "error": str(exc)},
            )
            return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"decision": VetoDecision.HALT_ESCALATE.value, "error": str(exc)},
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"decision": VetoDecision.HALT_ESCALATE.value, "error": str(exc)},
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": f"Fail-closed backend error: {type(exc).__name__}",
                },
            )
            return

        # Defense-in-depth fail-closed invariant: ABSTAIN always yields HALT_ESCALATE.
        if outcome.abstained and outcome.decision is not VetoDecision.HALT_ESCALATE:
            outcome = outcome.model_copy(
                update={
                    "decision": VetoDecision.HALT_ESCALATE,
                    "dom_override_blocked": inference_req.dom_claims_success,
                }
            )

        self._send_json(HTTPStatus.OK, outcome.model_dump(mode="json"))


class ByocVetoServer:
    """Managed stdlib HTTP server fronting :class:`MultiLoraVetoRouter`."""

    def __init__(
        self,
        *,
        router: MultiLoraVetoRouter,
        tenant_api_keys: Mapping[str, str],
        tenant_allowed_apps: Mapping[str, frozenset[str] | set[str]],
        run_bindings: Mapping[str, RunBinding] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        ssl_context: ssl.SSLContext | None = None,
        tls_handshake_timeout_s: float = DEFAULT_TLS_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        if not tenant_api_keys:
            raise ValueError("tenant_api_keys must contain at least one tenant key.")
        if not tenant_allowed_apps:
            raise ValueError(
                "tenant_allowed_apps is required and must specify allowed apps per tenant."
            )
        if host.strip().lower() not in _LOOPBACK_HOSTS and ssl_context is None:
            raise ValueError(
                f"Binding BYOC veto server to non-loopback host {host!r} requires ssl_context (TLS)."
            )
        cleaned_keys: dict[str, str] = {}
        seen_secrets: set[str] = set()
        for tid, secret in tenant_api_keys.items():
            if not tid or not tid.strip():
                raise ValueError("Tenant IDs in tenant_api_keys must be non-empty.")
            if not secret or not secret.strip():
                raise ValueError(f"API key for tenant {tid!r} must be non-empty.")
            if secret in seen_secrets:
                raise ValueError("Duplicate API keys across tenants are forbidden.")
            seen_secrets.add(secret)
            cleaned_keys[tid.strip()] = secret.strip()

        cleaned_allowed: dict[str, frozenset[str]] = {}
        for tid in cleaned_keys:
            apps = tenant_allowed_apps.get(tid)
            if not apps:
                raise ValueError(
                    f"tenant_allowed_apps must declare at least one allowed app for tenant {tid!r}."
                )
            cleaned_allowed[tid] = frozenset(
                str(a).strip() for a in apps if str(a).strip()
            )
            if not cleaned_allowed[tid]:
                raise ValueError(
                    f"tenant_allowed_apps for tenant {tid!r} cannot be empty."
                )

        self._use_tls = ssl_context is not None
        self._httpd = _ByocThreadingHTTPServer(
            (host, port),
            router=router,
            tenant_api_keys=cleaned_keys,
            tenant_allowed_apps=cleaned_allowed,
            run_bindings=dict(run_bindings) if run_bindings is not None else None,
            max_body_bytes=max_body_bytes,
            ssl_context=ssl_context,
            tls_handshake_timeout_s=tls_handshake_timeout_s,
        )
        self._thread: threading.Thread | None = None

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        scheme = "https" if self._use_tls else "http"
        return f"{scheme}://{host}:{port}"

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
            name="openadapt-byoc-veto-server",
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> ByocVetoServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def create_byoc_veto_server(
    *,
    router: MultiLoraVetoRouter,
    tenant_api_keys: Mapping[str, str],
    tenant_allowed_apps: Mapping[str, frozenset[str] | set[str]],
    run_bindings: Mapping[str, RunBinding] | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ssl_context: ssl.SSLContext | None = None,
    tls_handshake_timeout_s: float = DEFAULT_TLS_HANDSHAKE_TIMEOUT_S,
) -> ByocVetoServer:
    """Create a configured :class:`ByocVetoServer` ready to start."""
    return ByocVetoServer(
        router=router,
        tenant_api_keys=tenant_api_keys,
        tenant_allowed_apps=tenant_allowed_apps,
        run_bindings=run_bindings,
        host=host,
        port=port,
        max_body_bytes=max_body_bytes,
        ssl_context=ssl_context,
        tls_handshake_timeout_s=tls_handshake_timeout_s,
    )


def _fail_closed_outcome(
    payload: Mapping[str, Any],
    *,
    reason: str,
) -> VetoOutcome:
    return VetoOutcome(
        request_id=str(payload.get("request_id") or "unknown"),
        adapter_id="unavailable",
        base_model="unavailable",
        decision=VetoDecision.HALT_ESCALATE,
        calibrated_fault_prob=1.0,
        calibrated_threshold=0.5,
        temperature=1.0,
        abstained=True,
        dom_override_blocked=bool(payload.get("dom_claims_success", False)),
        reason=reason,
    )


def post_verify_request(
    base_url: str,
    *,
    api_key: str,
    payload: Mapping[str, Any],
    tenant_id: str | None = None,
    timeout_s: float = 10.0,
    ssl_context: ssl.SSLContext | None = None,
) -> VetoOutcome:
    """Send a ``POST /v1/verify`` request and fail closed to ``HALT_ESCALATE`` on any error.

    Refuses plaintext ``http://`` to non-loopback hosts, checks that the
    response ``request_id`` matches the sent ``request_id``, and converts any
    transport, HTTP, TLS, serialization, or validation error into
    ``HALT_ESCALATE``.
    """
    parsed = urllib.parse.urlparse(base_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"}:
        return _fail_closed_outcome(
            payload,
            reason=f"Fail-closed client scheme error: unsupported scheme {scheme!r}",
        )
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        return _fail_closed_outcome(
            payload,
            reason=f"Fail-closed client TLS error: refusing plaintext http:// to non-loopback host {host!r}",
        )

    url = base_url.rstrip("/") + "/v1/verify"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if tenant_id is not None:
        headers["X-OpenAdapt-Tenant-Id"] = tenant_id
    expected_request_id = payload.get("request_id")
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=timeout_s, context=ssl_context
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        outcome = VetoOutcome.model_validate(body)
    except urllib.error.HTTPError as exc:
        return _fail_closed_outcome(
            payload, reason=f"Fail-closed client HTTP error: {exc.code}"
        )
    except Exception as exc:  # noqa: BLE001
        return _fail_closed_outcome(
            payload, reason=f"Fail-closed client error: {type(exc).__name__}"
        )

    if expected_request_id is not None and outcome.request_id != str(
        expected_request_id
    ):
        return _fail_closed_outcome(
            payload,
            reason=(
                "Fail-closed client response error: response request_id "
                f"{outcome.request_id!r} != sent {expected_request_id!r}"
            ),
        )
    return outcome


__all__ = [
    "ByocVerifyPayload",
    "ByocVetoServer",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TLS_HANDSHAKE_TIMEOUT_S",
    "RunBinding",
    "create_byoc_veto_server",
    "post_verify_request",
]
