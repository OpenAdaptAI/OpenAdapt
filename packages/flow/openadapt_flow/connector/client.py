"""Outbound-only HTTP client to the control plane (the BYOC transport).

The hosted lane PUSHES work; a BYOC customer runs the data plane inside their own
VPC/on-prem with NO inbound firewall hole. So the Connector makes ONLY OUTBOUND
HTTPS connections and PULLS jobs down them (the shape of a GitHub self-hosted
runner or a Citrix Cloud Connector). The customer opens ZERO inbound ports.

Endpoints (all outbound):
  * ``POST /api/connector/register``     enroll once -> per-connector token
  * ``POST /api/connector/poll``         long-poll -> lease the next job
  * ``POST /api/connector/ack``          release the lease (done|failed)
  * ``POST /api/internal/run-callback``  PHI-free status/metrics (existing boundary)

Auth: the per-connector token as ``Authorization: Bearer`` on poll/ack; the org
enrollment secret (``x-byoc-enrollment-secret``) once on enroll; the run-scoped
``x-run-token`` on the callback. Only ``httpx`` (already core) + stdlib is used.

A custom ``transport`` (an :class:`httpx.BaseTransport`) may be injected so tests
drive the whole enroll -> poll -> callback -> ack loop with ZERO network.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from openadapt_flow.hosted import HostedError


class ConnectorClientError(HostedError):
    """An outbound control-plane call failed."""


class ConnectorClient:
    def __init__(
        self,
        control_plane_url: str,
        *,
        token: Optional[str] = None,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base = control_plane_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(
            base_url=self.base, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ConnectorClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _bearer(self) -> dict[str, str]:
        if not self.token:
            raise ConnectorClientError("connector is not enrolled (no token)")
        return {"authorization": f"Bearer {self.token}"}

    def enroll(
        self, *, enrollment_secret: Optional[str], org_id: Optional[str], name: str
    ) -> dict[str, Any]:
        """Enroll this machine as a Connector; returns the register response
        (including the per-connector ``token``, shown exactly once)."""
        headers: dict[str, str] = {}
        body: dict[str, Any] = {"name": name}
        if enrollment_secret:
            headers["x-byoc-enrollment-secret"] = enrollment_secret
        if org_id:
            body["org_id"] = org_id
        resp = self._client.post("/api/connector/register", json=body, headers=headers)
        if resp.status_code != 200:
            raise ConnectorClientError(
                f"enroll refused: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        token = data.get("token")
        if token:
            self.token = token
        return data

    def poll(self, wait_s: int) -> Optional[dict[str, Any]]:
        """Long-poll for the next leased job. Returns the poll envelope
        ``{"job": {...}}`` or None on a 204 (no work in the wait window)."""
        resp = self._client.post(
            "/api/connector/poll", json={"wait": wait_s}, headers=self._bearer()
        )
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise ConnectorClientError(
                f"poll failed: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        return data.get("job")

    def ack(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> dict[str, Any]:
        """Release a leased job (``done`` | ``failed``)."""
        resp = self._client.post(
            "/api/connector/ack",
            json={"job_id": job_id, "status": status, "error": error},
            headers=self._bearer(),
        )
        # 409 = the lease was already released/re-offered; not fatal, move on.
        if resp.status_code not in (200, 409):
            raise ConnectorClientError(
                f"ack failed: {resp.status_code} {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def run_callback(self, body: dict[str, Any], *, run_token: Optional[str]) -> None:
        """POST PHI-free run status/metrics via the existing callback boundary.

        Authenticated by the run-scoped ``x-run-token`` delivered in the job
        (proves this run; forbids forging another's status). Best-effort:
        observability/status must not crash the loop, but a hard transport error
        propagates so the caller records it.
        """
        headers = {"content-type": "application/json"}
        if run_token:
            headers["x-run-token"] = run_token
        resp = self._client.post(
            "/api/internal/run-callback", json=body, headers=headers
        )
        if resp.status_code >= 500:
            raise ConnectorClientError(
                f"run-callback failed: {resp.status_code} {resp.text[:300]}"
            )
