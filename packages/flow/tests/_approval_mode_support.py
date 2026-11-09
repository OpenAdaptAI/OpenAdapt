"""Shared fixtures for the approval-mode tests. Made-up identifiers only."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openadapt_flow.approval_mode.gate import dedicated_user_reference
from openadapt_flow.approval_mode.model import (
    DESKTOP_LIST_WITH_HEADER,
    ItemContext,
)
from openadapt_flow.approval_mode.switch import (
    StatusRequest,
    SwitchLog,
    SwitchRecord,
    sign_status,
)

BUNDLE_DIGEST = "a" * 64
ENVIRONMENT_DIGEST = "sha256:" + "e" * 64
VERIFIER_IDENTITY = "sha256:" + "f" * 64
RUNNER_ID = "runner-test-0001"
KEY_ID = "cloud-status-test-1"
DEDICATED_USER = "openadapt.intake"
USER_KEY = b"runner-local-hmac-key-for-tests"


def status_keypair() -> tuple[Ed25519PrivateKey, dict[str, str]]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, {KEY_ID: base64.b64encode(public).decode("ascii")}


def make_record(**overrides: Any) -> SwitchRecord:
    fields: dict[str, Any] = {
        "switch_id": "switch-test-0001",
        "org_id": "org-clinic-test",
        "workflow_id": "workflow-referral-test",
        "runner_id": RUNNER_ID,
        "bundle_content_digest": BUNDLE_DIGEST,
        "environment_digest": ENVIRONMENT_DIGEST,
        "verifier_identities": (VERIFIER_IDENTITY,),
        "dedicated_user_ref": dedicated_user_reference(DEDICATED_USER, USER_KEY),
        "rung_floor": {},
        "allowed_interstitials": {},
    }
    fields.update(overrides)
    return SwitchRecord(**fields)


def open_log(tmp_path: Path, record: SwitchRecord) -> SwitchLog:
    return SwitchLog.open_new(tmp_path / "switch" / "log.jsonl", record)


def good_item(**overrides: Any) -> ItemContext:
    fields: dict[str, Any] = {
        "channel": DESKTOP_LIST_WITH_HEADER,
        "header_maps_every_input": True,
        "rows_well_formed": True,
        "values_pass_types": True,
        "is_retry": False,
        "allow_repeat": False,
        "record_key_seen_recently": False,
        "caller_supplied_configuration": False,
        "holds_switch_lease": True,
    }
    fields.update(overrides)
    return ItemContext(**fields)


class FakeCloud:
    """Signs a status for every request, like Cloud's status route."""

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        state: str = "on",
        off_reason: Optional[str] = None,
    ) -> None:
        self.private_key = private_key
        self.state = state
        self.off_reason = off_reason
        self.requests: list[StatusRequest] = []
        self.fail = False
        self.tamper: Optional[dict[str, Any]] = None

    def __call__(self, request: StatusRequest) -> Any:
        self.requests.append(request)
        if self.fail:
            raise ConnectionError("cloud unreachable")
        fields = {
            "switch_id": request.switch_id,
            "switch_record_sha256": request.switch_record_sha256,
            "runner_id": request.runner_id,
            "nonce": request.nonce,
            "runner_event_seq": request.runner_event_seq,
            "runner_event_sha256": request.runner_event_sha256,
            "state": self.state,
            "off_reason": self.off_reason,
            "issued_at": "2026-10-12T10:00:00+00:00",
            "key_id": KEY_ID,
        }
        status = sign_status(fields, self.private_key)
        if self.tamper:
            return {**status.model_dump(mode="json"), **self.tamper}
        return status


class FakeSignedInUser:
    """P2 stand-in: answers a fixed verdict and binds the dedicated user."""

    kind = "login_id"

    def __init__(self, verdict: str = "match", identifier: str = DEDICATED_USER):
        self.verdict = verdict
        self.identifier = identifier
        self.frames: list[bytes] = []

    def reference(self, key: bytes) -> str:
        return dedicated_user_reference(self.identifier, key)

    def check(self, frame_png: bytes) -> str:
        self.frames.append(frame_png)
        return self.verdict


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now
