"""Send a waiting save to Cloud so a person can approve it from their phone.

Off unless the deployment opts in (``runtime.phone_approval: true`` or
``run --phone-approval``). A local-only or no-outbound install never builds a
relay, and every save keeps waiting for a person in Desktop's Run it.

What crosses to Cloud, and when:

* ``publish``: the request's ids and digests, the step's own intent, and the
  same filled-form screenshot Desktop shows. Cloud keeps the screenshot
  encrypted and only for the request's life (at most 4 hours); it shows it
  only to a signed-in (AAL2) member of the organization. The push
  notification Cloud sends carries an opaque id and fixed words, never the
  screenshot, a name, a value or the workflow's name.
* ``poll``: the Cloud-signed decision, once a person tapped Approve or
  Don't save. The run verifies it against the runner's pinned decision key
  and the exact request digest (:mod:`.cloud_decision`), then writes it as a
  ``cloud_phone`` answer, created once. A Desktop answer that landed first
  wins and the phone decision is ignored.
* ``withdraw``: when the request was answered elsewhere, expired or the run
  stopped waiting, so the phone link stops working.

Any network or Cloud failure leaves the save to Desktop. Nothing here retries
a save, and an approval still only lets the run re-check the form and save
exactly what was shown.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol

from openadapt_flow.approval_mode.cloud_decision import CLOUD_DECISION_SCHEMA

PUBLISH_SCHEMA: Literal["openadapt.save-approval-publish/v1"] = (
    "openadapt.save-approval-publish/v1"
)
WithdrawReason = Literal["answered_elsewhere", "expired", "run_stopped_waiting"]
#: How often the run asks Cloud for a phone decision (seconds).
PHONE_POLL_S = 3.0
CLOUD_URL_ENV = "OPENADAPT_CLOUD_URL"
RUNNER_TOKEN_ENV = "OPENADAPT_RUNNER_TOKEN"
RUNNER_ID_ENV = "OPENADAPT_RUNNER_ID"


class PhoneRelay(Protocol):
    runner_id: str

    def publish(self, request: Any, frame_png: bytes) -> bool: ...

    def poll(self, request: Any) -> Optional[dict[str, Any]]: ...

    def withdraw(self, request: Any, reason: WithdrawReason) -> None: ...


class PhoneRelayError(RuntimeError):
    pass


def publish_body(request: Any, frame_png: bytes, runner_id: str) -> dict[str, Any]:
    """The exact publish body. Cloud refuses any other field."""

    return {
        "schema": PUBLISH_SCHEMA,
        "runner_id": runner_id,
        "request_id": request.request_id,
        "request_sha256": request.digest(),
        "run_id": request.run_id,
        "workflow_name": request.workflow_name,
        "step_id": request.step_id,
        "step_index": request.step_index,
        "step_intent": request.step_intent,
        "step_class": request.step_class.value,
        "mode": request.mode.value,
        "form_state_sha256": request.form_state_sha256,
        "screenshot_sha256": request.screenshot_sha256,
        "screenshot_png_b64": base64.b64encode(frame_png).decode("ascii"),
        "created_at": request.created_at,
        "expires_at": request.expires_at,
    }


@dataclass
class CloudPhoneRelay:
    """The HTTPS relay to Cloud's runner API (``Authorization: Bearer oar_…``)."""

    base_url: str
    token: str
    runner_id: str
    timeout_s: float = 15.0
    transport: Any = None

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://") and not self.base_url.startswith(
            "http://127.0.0.1"
        ):
            raise PhoneRelayError("phone approval needs an https Cloud URL")
        if not self.token.startswith("oar_"):
            raise PhoneRelayError("phone approval needs this runner's Cloud token")
        import httpx

        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout_s,
            transport=self.transport,
            headers={"authorization": f"Bearer {self.token}"},
        )

    def publish(self, request: Any, frame_png: bytes) -> bool:
        response = self._client.post(
            "/api/runners/save-approvals",
            json=publish_body(request, frame_png, self.runner_id),
        )
        return response.status_code in (200, 201)

    def poll(self, request: Any) -> Optional[dict[str, Any]]:
        response = self._client.get(f"/api/runners/save-approvals/{request.request_id}")
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict) or body.get("status") != "decided":
            return None
        decision = body.get("decision")
        if not isinstance(decision, dict):
            return None
        if decision.get("schema_version") != CLOUD_DECISION_SCHEMA:
            return None
        return decision

    def withdraw(self, request: Any, reason: WithdrawReason) -> None:
        self._client.post(
            f"/api/runners/save-approvals/{request.request_id}/withdraw",
            json={"reason": reason},
        )

    def decision_keys(self) -> dict[str, str]:
        response = self._client.get("/api/runners/save-approvals/keys")
        if response.status_code != 200:
            raise PhoneRelayError(f"Cloud answered {response.status_code}")
        keys = (response.json() or {}).get("decision_keys")
        if not isinstance(keys, dict) or not keys:
            raise PhoneRelayError("Cloud returned no decision keys")
        return {str(k): str(v) for k, v in keys.items()}


def relay_from_env(env: Optional[dict[str, str]] = None) -> CloudPhoneRelay:
    """Build the relay from the runner's Cloud settings. Raises when missing:
    an operator who asked for phone approval must not silently get none."""

    source = os.environ if env is None else env
    url = source.get(CLOUD_URL_ENV) or "https://app.openadapt.ai"
    token = source.get(RUNNER_TOKEN_ENV) or ""
    runner_id = source.get(RUNNER_ID_ENV) or ""
    if not token or not runner_id:
        raise PhoneRelayError(
            f"phone approval needs {RUNNER_TOKEN_ENV} and {RUNNER_ID_ENV} "
            "(pair this computer with OpenAdapt Cloud first)"
        )
    return CloudPhoneRelay(url, token, runner_id)


__all__ = [
    "CloudPhoneRelay",
    "PHONE_POLL_S",
    "PUBLISH_SCHEMA",
    "PhoneRelay",
    "PhoneRelayError",
    "publish_body",
    "relay_from_env",
]
