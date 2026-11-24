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
- **Mandatory server-issued run bindings (anti-steering & anti-replay):**
  Every ``POST /v1/verify`` request MUST match a server-side :class:`RunBinding`
  (issued via :meth:`ByocVetoServer.issue_run_binding` or pre-registered at
  startup) that authoritatively binds ``(tenant_id, app_id, workflow_id,
  domain, step_nonce, pre_submit_image_sha256, post_submit_image_sha256)``.
  Each binding and ``step_nonce`` is strictly single-use, payload ``domain`` is
  ignored, workflow-adapter downgrades (missing or unregistered ``workflow_id``)
  are rejected, and fallback to ``default_base_adapter`` is disabled by default.
- **Server-side VPC HMAC enforcement:** Per-tenant HMAC keys stay strictly on
  the VPC server. Any ``expected_field_hashes`` submitted on ``/v1/verify``
  must match server-bound keyed HMAC-SHA256 digests computed with the tenant's
  VPC key; plain SHA-256 digests or digests without a configured key fail
  closed with ``403 HALT_ESCALATE``.
- **Bounded TLS handshake, read timeout, and concurrency cap:** Wraps accepted
  sockets inside worker threads with bounded handshake and request read
  timeouts (never clearing socket timeouts to ``None``) and caps concurrent
  connections with a semaphore so idle peers cannot exhaust threads.
- **Fail-closed ``ABSTAIN -> HALT_ESCALATE``:** Never allows DOM success claims
  to override an abstained visual check; backend failures fail closed with
  ``503`` and ``decision="HALT_ESCALATE"``.
"""

from __future__ import annotations

import hmac
import json
import secrets
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
    _MIN_HMAC_KEY_BYTES,
    _NONCE_RE,
    MultiLoraVetoRouter,
    VetoDecision,
    VetoInferenceRequest,
    VetoOutcome,
    _validate_sha256_hex,
    _validate_sha256_map,
    hmac_field_value,
)

DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_TLS_HANDSHAKE_TIMEOUT_S = 5.0
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 64
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RunBinding(BaseModel):
    """Server-side authoritative, single-use binding for one verification step.

    Binds the authenticated tenant, target ``app_id``, ``workflow_id``,
    optional ``domain``, single-use ``step_nonce``, expected pre/post-submit
    image SHA-256 digests, and optional server-computed keyed HMAC-SHA256
    ``expected_field_hashes``.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    domain: str | None = None
    step_nonce: str = Field(min_length=1)
    pre_submit_image_sha256: str = Field(min_length=64, max_length=64)
    post_submit_image_sha256: str = Field(min_length=64, max_length=64)
    expected_field_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("step_nonce")
    @classmethod
    def _check_step_nonce(cls, value: str) -> str:
        if not _NONCE_RE.match(value):
            raise ValueError(f"step_nonce must match {_NONCE_RE.pattern!r}.")
        return value

    @field_validator("pre_submit_image_sha256")
    @classmethod
    def _check_pre_image_sha256(cls, value: str) -> str:
        return _validate_sha256_hex(value, "pre_submit_image_sha256")

    @field_validator("post_submit_image_sha256")
    @classmethod
    def _check_post_image_sha256(cls, value: str) -> str:
        return _validate_sha256_hex(value, "post_submit_image_sha256")

    @field_validator("expected_field_hashes")
    @classmethod
    def _check_expected_field_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_sha256_map(value)


class IssuedRunBinding(BaseModel):
    """Opaque server-issued token and single-use nonce returned to the caller."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    step_nonce: str = Field(min_length=1)
    expected_field_hashes: dict[str, str] = Field(default_factory=dict)


class ByocVerifyPayload(BaseModel):
    """Value-free HTTP request schema for ``POST /v1/verify``.

    Carries only the server-issued ``request_id``, single-use ``step_nonce``,
    64-hex SHA-256 image references (dereferenced from the customer-VPC
    artifact store by the backend), and keyed HMAC-SHA256 field digests.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    workflow_id: str | None = None
    domain: str | None = None
    tenant_id: str | None = None
    step_nonce: str = Field(min_length=1)
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
    def _check_step_nonce(cls, value: str) -> str:
        if not _NONCE_RE.match(value):
            raise ValueError(f"step_nonce must match {_NONCE_RE.pattern!r}.")
        return value

    @field_validator("expected_field_hashes")
    @classmethod
    def _check_expected_field_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_sha256_map(value)


def _digests_equal(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    if set(left.keys()) != set(right.keys()):
        return False
    matched = True
    for key, expected in right.items():
        if not hmac.compare_digest(left[key], expected):
            matched = False
    return matched


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
        tenant_hmac_keys: dict[str, bytes],
        run_bindings: dict[str, RunBinding],
        allow_default_fallback: bool,
        max_body_bytes: int,
        ssl_context: ssl.SSLContext | None,
        tls_handshake_timeout_s: float,
        request_timeout_s: float,
        max_concurrent_connections: int,
    ) -> None:
        self.router = router
        self.tenant_api_keys = dict(tenant_api_keys)
        self.tenant_allowed_apps = dict(tenant_allowed_apps)
        self.tenant_hmac_keys = dict(tenant_hmac_keys)
        self.run_bindings = dict(run_bindings)
        self.consumed_bindings: set[str] = set()
        self.consumed_nonces: set[tuple[str, str]] = set()
        self.allow_default_fallback = allow_default_fallback
        self.max_body_bytes = max_body_bytes
        self.ssl_context = ssl_context
        self.tls_handshake_timeout_s = tls_handshake_timeout_s
        self.request_timeout_s = request_timeout_s
        self.eval_lock = threading.Lock()
        self.binding_lock = threading.Lock()
        self._conn_semaphore = threading.BoundedSemaphore(max_concurrent_connections)
        super().__init__(server_address, _ByocVetoRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Cap concurrent worker threads; drop excess connections immediately."""
        if not self._conn_semaphore.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._conn_semaphore.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_semaphore.release()

    def finish_request(self, request: Any, client_address: Any) -> None:
        """Enforce bounded TLS handshake and post-handshake read timeouts."""
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
                tls_sock.settimeout(self.request_timeout_s)
                super().finish_request(tls_sock, client_address)
            except (OSError, ssl.SSLError, TimeoutError):
                return
            finally:
                if tls_sock is not None:
                    try:
                        tls_sock.close()
                    except OSError:
                        pass
            return
        try:
            request.settimeout(self.request_timeout_s)
            super().finish_request(request, client_address)
        except (OSError, TimeoutError):
            return


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
        if len(raw_body) != content_length:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "decision": VetoDecision.HALT_ESCALATE.value,
                    "error": "Incomplete request body.",
                },
            )
            return

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

        # Mandatory server-side run binding + single-use step_nonce + image/HMAC verification.
        with self.server.binding_lock:
            binding = self.server.run_bindings.get(payload.request_id)
            if binding is None:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "decision": VetoDecision.HALT_ESCALATE.value,
                        "error": (
                            f"Unknown server-side run binding for request_id "
                            f"{payload.request_id!r}."
                        ),
                    },
                )
                return

            if (
                payload.request_id in self.server.consumed_bindings
                or (tenant_id, payload.step_nonce) in self.server.consumed_nonces
            ):
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "decision": VetoDecision.HALT_ESCALATE.value,
                        "error": "Run binding or step_nonce already consumed (replay rejected).",
                    },
                )
                return

            if (
                binding.tenant_id != tenant_id
                or binding.app_id != payload.app_id
                or binding.workflow_id != payload.workflow_id
                or not hmac.compare_digest(binding.step_nonce, payload.step_nonce)
                or not hmac.compare_digest(
                    binding.pre_submit_image_sha256, payload.pre_submit_image_sha256
                )
                or not hmac.compare_digest(
                    binding.post_submit_image_sha256, payload.post_submit_image_sha256
                )
            ):
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "decision": VetoDecision.HALT_ESCALATE.value,
                        "error": "Request parameters or image digests do not match server-side run binding.",
                    },
                )
                return

            # Server-side HMAC enforcement: laptops never hold tenant HMAC keys,
            # and unkeyed SHA-256 digests are rejected.
            if payload.expected_field_hashes or binding.expected_field_hashes:
                if tenant_id not in self.server.tenant_hmac_keys:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "decision": VetoDecision.HALT_ESCALATE.value,
                            "error": (
                                "expected_field_hashes cannot be verified without a "
                                "server-side tenant HMAC key."
                            ),
                        },
                    )
                    return
                if not binding.expected_field_hashes or not _digests_equal(
                    payload.expected_field_hashes, binding.expected_field_hashes
                ):
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "decision": VetoDecision.HALT_ESCALATE.value,
                            "error": (
                                "expected_field_hashes do not match server-bound "
                                "keyed HMAC-SHA256 digests."
                            ),
                        },
                    )
                    return

            # Mark binding & step_nonce consumed atomically before scoring.
            self.server.consumed_bindings.add(payload.request_id)
            self.server.consumed_nonces.add((tenant_id, binding.step_nonce))

        # Resolve strictly from server-side binding; ignore caller-supplied payload.domain.
        inference_req = VetoInferenceRequest(
            request_id=payload.request_id,
            app_id=binding.app_id,
            workflow_id=binding.workflow_id,
            domain=binding.domain,
            tenant_id=tenant_id,
            step_nonce=binding.step_nonce,
            pre_submit_image_sha256=binding.pre_submit_image_sha256,
            post_submit_image_sha256=binding.post_submit_image_sha256,
            expected_field_hashes=dict(binding.expected_field_hashes),
            dom_claims_success=payload.dom_claims_success,
        )

        try:
            with self.server.eval_lock:
                outcome = self.server.router.evaluate(
                    inference_req,
                    requested_adapter_id=payload.requested_adapter_id,
                    allow_default_fallback=self.server.allow_default_fallback,
                    require_exact_workflow=True,
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
        except (
            RuntimeError,
            OSError,
            TimeoutError,
            LookupError,
            TypeError,
            ArithmeticError,
            ValidationError,
        ) as exc:
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
        tenant_hmac_keys: Mapping[str, bytes | str] | None = None,
        run_bindings: Mapping[str, RunBinding] | None = None,
        allow_default_fallback: bool = False,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        ssl_context: ssl.SSLContext | None = None,
        tls_handshake_timeout_s: float = DEFAULT_TLS_HANDSHAKE_TIMEOUT_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        max_concurrent_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
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
        if request_timeout_s <= 0.0:
            raise ValueError("request_timeout_s must be > 0.")
        if max_concurrent_connections <= 0:
            raise ValueError("max_concurrent_connections must be >= 1.")

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

        cleaned_hmac_keys: dict[str, bytes] = {}
        if tenant_hmac_keys is not None:
            for tid, raw_key in tenant_hmac_keys.items():
                key_tid = tid.strip()
                if key_tid not in cleaned_keys:
                    raise ValueError(
                        f"tenant_hmac_keys contains unknown tenant {tid!r}."
                    )
                key_bytes = (
                    raw_key.encode("utf-8")
                    if isinstance(raw_key, str)
                    else bytes(raw_key)
                )
                if len(key_bytes) < _MIN_HMAC_KEY_BYTES:
                    raise ValueError(
                        f"HMAC key for tenant {key_tid!r} must be at least "
                        f"{_MIN_HMAC_KEY_BYTES} bytes long."
                    )
                cleaned_hmac_keys[key_tid] = key_bytes

        cleaned_bindings: dict[str, RunBinding] = {}
        if run_bindings is not None:
            for rid, binding in run_bindings.items():
                if binding.tenant_id not in cleaned_keys:
                    raise ValueError(
                        f"RunBinding {rid!r} references unknown tenant {binding.tenant_id!r}."
                    )
                if binding.app_id not in cleaned_allowed[binding.tenant_id]:
                    raise ValueError(
                        f"RunBinding {rid!r} references unauthorized app_id {binding.app_id!r} "
                        f"for tenant {binding.tenant_id!r}."
                    )
                cleaned_bindings[rid] = binding

        self._use_tls = ssl_context is not None
        self._httpd = _ByocThreadingHTTPServer(
            (host, port),
            router=router,
            tenant_api_keys=cleaned_keys,
            tenant_allowed_apps=cleaned_allowed,
            tenant_hmac_keys=cleaned_hmac_keys,
            run_bindings=cleaned_bindings,
            allow_default_fallback=allow_default_fallback,
            max_body_bytes=max_body_bytes,
            ssl_context=ssl_context,
            tls_handshake_timeout_s=tls_handshake_timeout_s,
            request_timeout_s=request_timeout_s,
            max_concurrent_connections=max_concurrent_connections,
        )
        self._thread: threading.Thread | None = None

    def issue_run_binding(
        self,
        *,
        tenant_id: str,
        app_id: str,
        pre_submit_image_sha256: str,
        post_submit_image_sha256: str,
        workflow_id: str | None = None,
        domain: str | None = None,
        expected_field_values: Mapping[str, str] | None = None,
    ) -> IssuedRunBinding:
        """Issue an opaque, single-use server-side run binding for one step.

        When ``expected_field_values`` is supplied, keyed HMAC-SHA256 digests
        are computed strictly server-side using the tenant's VPC HMAC key so
        laptops never need access to the key.
        """
        if tenant_id not in self._httpd.tenant_api_keys:
            raise PermissionError(f"Unknown tenant_id: {tenant_id!r}")
        if app_id not in self._httpd.tenant_allowed_apps[tenant_id]:
            raise PermissionError(
                f"Tenant {tenant_id!r} is not authorized for app_id {app_id!r}."
            )

        field_hashes: dict[str, str] = {}
        if expected_field_values:
            hmac_key = self._httpd.tenant_hmac_keys.get(tenant_id)
            if hmac_key is None:
                raise PermissionError(
                    f"Tenant {tenant_id!r} has no server-side HMAC key configured."
                )
            for slug, value in expected_field_values.items():
                field_hashes[slug] = hmac_field_value(hmac_key, slug, value)

        request_id = f"run-{secrets.token_urlsafe(18)}"
        step_nonce = f"nonce-{secrets.token_urlsafe(16)}"
        binding = RunBinding(
            tenant_id=tenant_id,
            app_id=app_id,
            workflow_id=workflow_id,
            domain=domain,
            step_nonce=step_nonce,
            pre_submit_image_sha256=pre_submit_image_sha256,
            post_submit_image_sha256=post_submit_image_sha256,
            expected_field_hashes=field_hashes,
        )
        with self._httpd.binding_lock:
            self._httpd.run_bindings[request_id] = binding
        return IssuedRunBinding(
            request_id=request_id,
            step_nonce=step_nonce,
            expected_field_hashes=dict(field_hashes),
        )

    def verify_field_hmac(
        self,
        *,
        tenant_id: str,
        field_slug: str,
        observed_value: str,
        expected_digest: str,
    ) -> bool:
        """Verify an observed screen field value against a server-side tenant HMAC digest.

        Fails closed (returns ``False``) if the tenant has no configured VPC
        HMAC key or if the digest does not match in constant time.
        """
        hmac_key = self._httpd.tenant_hmac_keys.get(tenant_id)
        if hmac_key is None:
            return False
        computed = hmac_field_value(hmac_key, field_slug, observed_value)
        return hmac.compare_digest(computed, expected_digest.strip().lower())

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
    tenant_hmac_keys: Mapping[str, bytes | str] | None = None,
    run_bindings: Mapping[str, RunBinding] | None = None,
    allow_default_fallback: bool = False,
    host: str = "127.0.0.1",
    port: int = 0,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ssl_context: ssl.SSLContext | None = None,
    tls_handshake_timeout_s: float = DEFAULT_TLS_HANDSHAKE_TIMEOUT_S,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_concurrent_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
) -> ByocVetoServer:
    """Create a configured :class:`ByocVetoServer` ready to start."""
    return ByocVetoServer(
        router=router,
        tenant_api_keys=tenant_api_keys,
        tenant_allowed_apps=tenant_allowed_apps,
        tenant_hmac_keys=tenant_hmac_keys,
        run_bindings=run_bindings,
        allow_default_fallback=allow_default_fallback,
        host=host,
        port=port,
        max_body_bytes=max_body_bytes,
        ssl_context=ssl_context,
        tls_handshake_timeout_s=tls_handshake_timeout_s,
        request_timeout_s=request_timeout_s,
        max_concurrent_connections=max_concurrent_connections,
    )


def _fail_closed_outcome(
    payload: Mapping[str, Any] | Any,
    *,
    reason: str,
) -> VetoOutcome:
    if isinstance(payload, Mapping):
        req_id = str(payload.get("request_id") or "unknown")
        dom_claims = bool(payload.get("dom_claims_success", False))
    else:
        req_id = "unknown"
        dom_claims = False
    return VetoOutcome(
        request_id=req_id,
        adapter_id="unavailable",
        base_model="unavailable",
        decision=VetoDecision.HALT_ESCALATE,
        calibrated_fault_prob=1.0,
        calibrated_threshold=0.5,
        temperature=1.0,
        abstained=True,
        dom_override_blocked=dom_claims,
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

    Validates payload type, non-empty ``request_id``, URL syntax, TLS for
    non-loopback hosts, response ``request_id`` match, and fail-closed decision
    invariants (never accepting ``ALLOW`` when ``abstained=True`` or
    ``calibrated_fault_prob >= calibrated_threshold``).
    """
    if not isinstance(payload, Mapping):
        return _fail_closed_outcome(
            {},
            reason=f"Fail-closed client payload error: expected Mapping, got {type(payload).__name__}",
        )

    raw_request_id = payload.get("request_id")
    if not isinstance(raw_request_id, str) or not raw_request_id.strip():
        return _fail_closed_outcome(
            payload,
            reason="Fail-closed client request error: non-empty request_id is required.",
        )

    try:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        parsed = urllib.parse.urlparse(base_url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
    except (ValueError, TypeError, AttributeError) as exc:
        return _fail_closed_outcome(
            payload,
            reason=f"Fail-closed client URL error: {type(exc).__name__}",
        )

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
    expected_request_id = raw_request_id
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
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        return _fail_closed_outcome(
            payload, reason=f"Fail-closed client error: {type(exc).__name__}"
        )

    if outcome.request_id != expected_request_id:
        return _fail_closed_outcome(
            payload,
            reason=(
                "Fail-closed client response error: response request_id "
                f"{outcome.request_id!r} != sent {expected_request_id!r}"
            ),
        )

    # Enforce fail-closed invariants client-side so a compromised/buggy server
    # cannot sneak ALLOW past abstained=True or prob >= threshold.
    if outcome.decision is VetoDecision.ALLOW and (
        outcome.abstained
        or outcome.calibrated_fault_prob >= outcome.calibrated_threshold
    ):
        return _fail_closed_outcome(
            payload,
            reason=(
                "Fail-closed client invariant error: ALLOW rejected when "
                "abstained=True or calibrated_fault_prob >= calibrated_threshold."
            ),
        )
    return outcome


__all__ = [
    "ByocVerifyPayload",
    "ByocVetoServer",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_CONCURRENT_CONNECTIONS",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_TLS_HANDSHAKE_TIMEOUT_S",
    "IssuedRunBinding",
    "RunBinding",
    "create_byoc_veto_server",
    "post_verify_request",
]
