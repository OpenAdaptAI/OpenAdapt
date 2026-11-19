"""Outbound-only HTTP client to the control plane (the BYOC transport).

The hosted lane PUSHES work; a BYOC customer runs the data plane inside their own
VPC/on-prem with NO inbound firewall hole. So the Connector makes ONLY OUTBOUND
HTTPS connections and PULLS jobs down them (the shape of a GitHub self-hosted
runner or a Citrix Cloud Connector). The customer opens ZERO inbound ports.

Endpoints (all outbound):
  * ``POST /api/connector/register``     mock/dev enrollment compatibility
  * ``POST /api/connector/poll``         long-poll -> lease the next job
  * ``POST /api/connector/heartbeat``    liveness + control while jobs run
  * ``POST /api/connector/ack``          release the lease (done|failed)
  * ``POST /api/connector/rotate``       swap this connector's token
  * ``POST /api/internal/run-callback``  metadata-only status and receipt

Every body is built by :mod:`openadapt_flow.connector.egress` (a closed
allow-list) and scanned for local secret values before it is sent. Every
connector request is signed (timestamp, nonce, HMAC over method, path and body
digest) so a captured request cannot be replayed or altered.

A custom ``transport`` (an :class:`httpx.BaseTransport`) may be injected so tests
drive the whole loop with ZERO network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

from openadapt_flow.connector import egress
from openadapt_flow.hosted import HostedError

MANAGED_QUALIFICATION_AUTHORITY_CAPABILITY = "managed_qualification_authority_v2"
CONTROL_HEADER = "x-oa-connector-control"


class ConnectorClientError(HostedError):
    """An outbound control-plane call failed."""


class RunsStopped(ConnectorClientError):
    """The organization's kill switch is on: lease nothing, stop what runs."""


@dataclass
class ConnectorControl:
    """Server-side control state delivered on every poll and heartbeat."""

    max_concurrent_leases: int = 1
    runs_stopped: bool = False
    cancel_job_ids: frozenset[str] = field(default_factory=frozenset)
    rotate_recommended: bool = False
    token_expires_at: Optional[str] = None

    @classmethod
    def parse(cls, raw: Any) -> Optional["ConnectorControl"]:
        if not isinstance(raw, dict):
            return None
        cap = raw.get("max_concurrent_leases", 1)
        if isinstance(cap, bool) or not isinstance(cap, int):
            cap = 1
        cancel = raw.get("cancel_job_ids") or []
        expires = raw.get("token_expires_at")
        return cls(
            max_concurrent_leases=max(1, min(16, cap)),
            runs_stopped=raw.get("runs_stopped") is True,
            cancel_job_ids=frozenset(str(i) for i in cancel if isinstance(i, str)),
            rotate_recommended=raw.get("rotate_recommended") is True,
            token_expires_at=expires if isinstance(expires, str) else None,
        )


def sign_request(
    token: str, method: str, path: str, body: bytes, *, ts: int, nonce: str
) -> str:
    """The v1 request signature (see the BYOC connector protocol)."""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("utf-8")
    message = "\n".join(
        ["v1", str(ts), nonce, method.upper(), path, hashlib.sha256(body).hexdigest()]
    ).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class ConnectorClient:
    def __init__(
        self,
        control_plane_url: str,
        *,
        token: Optional[str] = None,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
        egress_audit_log: Optional[str] = None,
    ) -> None:
        self.base = control_plane_url.rstrip("/")
        self.token = token
        self.control = ConnectorControl()
        self.egress_audit_log = egress_audit_log
        self._secret_values: set[str] = set()
        self._lock = threading.Lock()
        self._client = httpx.Client(
            base_url=self.base, timeout=timeout, transport=transport
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ConnectorClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_secret_values(self, values: Iterable[str]) -> None:
        """Register local secret values the egress scan must never let out."""
        with self._lock:
            self._secret_values.update(v for v in values if v)

    # -- transport -----------------------------------------------------------

    def _audit(self, path: str, raw: bytes) -> None:
        if not self.egress_audit_log:
            return
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": path,
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body": json.loads(raw.decode("utf-8")) if raw else None,
        }
        with self._lock:
            with open(self.egress_audit_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        signed: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        with self._lock:
            secret_values = tuple(self._secret_values)
        raw = egress.serialize_checked(body, secret_values)
        headers = {"content-type": "application/json"}
        if signed:
            if not self.token:
                raise ConnectorClientError("connector is not enrolled (no token)")
            ts = int(time.time())
            nonce = _secrets.token_hex(16)
            headers.update(
                {
                    "authorization": f"Bearer {self.token}",
                    "x-oa-connector-ts": str(ts),
                    "x-oa-connector-nonce": nonce,
                    "x-oa-connector-signature": sign_request(
                        self.token, "POST", path, raw, ts=ts, nonce=nonce
                    ),
                }
            )
        if extra_headers:
            headers.update(extra_headers)
        self._audit(path, raw)
        return self._client.post(path, content=raw, headers=headers)

    def _absorb_control(self, resp: httpx.Response, data: Any = None) -> None:
        control = None
        if isinstance(data, dict):
            control = ConnectorControl.parse(data.get("connector"))
        if control is None:
            header = resp.headers.get(CONTROL_HEADER)
            if header:
                try:
                    control = ConnectorControl.parse(json.loads(header))
                except ValueError:
                    control = None
        if control is not None:
            self.control = control

    # -- endpoints -----------------------------------------------------------

    def enroll(
        self, *, enrollment_secret: Optional[str], org_id: Optional[str], name: str
    ) -> dict[str, Any]:
        """Mock/development enrollment (live Cloud mints tokens in Settings)."""
        headers: dict[str, str] = {}
        body: dict[str, Any] = {"name": str(name)[:100]}
        if enrollment_secret:
            headers["x-byoc-enrollment-secret"] = enrollment_secret
        if org_id:
            body["org_id"] = egress._ident(org_id, "org_id")
        resp = self._post(
            "/api/connector/register", body, signed=False, extra_headers=headers
        )
        if resp.status_code != 200:
            raise ConnectorClientError(f"enroll refused: {resp.status_code}")
        data = resp.json()
        token = data.get("token")
        if token:
            self.token = token
        return data

    def poll(
        self, wait_s: int, *, active_job_ids: Iterable[str] = ()
    ) -> Optional[dict[str, Any]]:
        """Long-poll for the next leased job. Returns the job envelope
        ``{"id", "payload", ...}`` or None (no work, or at the lease cap).

        Raises :class:`RunsStopped` while the organization's kill switch is on.
        """
        body = egress.guard_poll(
            int(wait_s),
            [MANAGED_QUALIFICATION_AUTHORITY_CAPABILITY],
            list(active_job_ids),
        )
        resp = self._post("/api/connector/poll", body)
        if resp.status_code == 204:
            self._absorb_control(resp)
            return None
        data = resp.json() if resp.content else {}
        if resp.status_code == 423:
            self._absorb_control(resp, data)
            self.control.runs_stopped = True
            raise RunsStopped("organization runs are stopped by an administrator")
        if resp.status_code != 200:
            raise ConnectorClientError(
                f"poll failed: {resp.status_code} {str(data.get('error', ''))[:80]}"
            )
        self._absorb_control(resp, data)
        return data.get("job")

    def heartbeat(self, active_job_ids: Iterable[str]) -> ConnectorControl:
        resp = self._post(
            "/api/connector/heartbeat", egress.guard_heartbeat(list(active_job_ids))
        )
        if resp.status_code != 200:
            raise ConnectorClientError(f"heartbeat failed: {resp.status_code}")
        self._absorb_control(resp, resp.json())
        return self.control

    def ack(
        self, job_id: str, status: str, error_code: Optional[str] = None
    ) -> dict[str, Any]:
        """Release a leased job (``done`` | ``failed``) with a closed code."""
        resp = self._post(
            "/api/connector/ack", egress.guard_ack(job_id, status, error_code)
        )
        # 409 = the lease was already released or reaped; not fatal, move on.
        if resp.status_code not in (200, 409):
            raise ConnectorClientError(f"ack failed: {resp.status_code}")
        return resp.json() if resp.content else {}

    def rotate(self) -> dict[str, Any]:
        """Swap this connector's token. The caller persists it, then uses it."""
        resp = self._post("/api/connector/rotate", egress.guard_empty())
        if resp.status_code != 200:
            raise ConnectorClientError(f"rotate failed: {resp.status_code}")
        data = resp.json()
        token = data.get("token")
        if not isinstance(token, str) or not token.startswith("oaconn_"):
            raise ConnectorClientError("rotate returned no connector token")
        return data

    def run_callback(self, body: dict[str, Any], *, run_token: Optional[str]) -> None:
        """POST the metadata-only run outcome (allow-listed, run-token auth).

        Every non-2xx response propagates: the caller must not ACK a lease whose
        authoritative run outcome the control plane rejected or did not receive.
        """
        headers = {}
        if run_token:
            headers["x-run-token"] = run_token
        resp = self._post(
            "/api/internal/run-callback",
            egress.guard_callback(body),
            signed=False,
            extra_headers=headers,
        )
        if not 200 <= resp.status_code < 300:
            raise ConnectorClientError(f"run-callback failed: {resp.status_code}")


def default_audit_path(home: Optional[str] = None) -> Optional[str]:
    """``$OPENADAPT_HOME/connector-egress.jsonl`` when OPENADAPT_HOME is set."""
    root = home or os.environ.get("OPENADAPT_HOME")
    return os.path.join(root, "connector-egress.jsonl") if root else None
