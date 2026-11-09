"""Privacy-safe automation failure signals for product learning.

The complete report, screenshots, OCR, parameters, application names, and
operator text stay inside the execution boundary.  This module reduces a
terminal report to a closed categorical envelope that a control plane may
aggregate across runs.  The envelope cannot authorize, create, validate, or
promote a repair.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from openadapt_flow import __version__

AUTOMATION_FAILURE_SIGNAL_SCHEMA = "openadapt.automation-failure-signal/v1"
_VERSION = re.compile(r"^(?:unknown|[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)$")


def failure_signal_sharing_enabled() -> bool:
    """Whether optional categorical failure sharing is enabled.

    Customer-controlled connectors may opt out with the standard
    ``DO_NOT_TRACK`` convention or ``OPENADAPT_TELEMETRY=0``.  The governed run
    callback itself remains operationally required; only this optional envelope
    is omitted.
    """

    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return os.environ.get("OPENADAPT_TELEMETRY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _outcome(report: dict[str, Any], status: str) -> str:
    value = report.get("execution_outcome")
    allowed = {
        "VERIFIED",
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    }
    if value in allowed:
        return str(value)
    return "FAILED" if status == "failed" else "HALTED"


def _effect_tier(report: dict[str, Any]) -> str:
    envelope = report.get("outcome_envelope")
    if not isinstance(envelope, dict):
        return "unknown"
    classes = envelope.get("evidence_classes")
    if isinstance(classes, list):
        for tier in (1, 2, 3, 4):
            if f"effect_tier_{tier}" in classes:
                return f"tier_{tier}"
    required = envelope.get("required_contracts")
    if isinstance(required, dict) and required.get("effect") == 0:
        return "none"
    return "unknown"


def automation_failure_signal(
    report: Optional[dict[str, Any]], status: str, substrate: str
) -> Optional[dict[str, Any]]:
    """Return the closed v1 envelope for a non-success terminal report."""

    if status == "success":
        return None
    report = report if isinstance(report, dict) else {}
    results = report.get("results")
    failed: dict[str, Any] = {}
    if isinstance(results, list):
        failed = next(
            (
                item
                for item in reversed(results)
                if isinstance(item, dict) and item.get("ok") is False
            ),
            {},
        )

    identity = failed.get("identity")
    identity_status = identity.get("status") if isinstance(identity, dict) else None
    identity_status_key = identity_status if isinstance(identity_status, str) else ""
    identity_state = {
        "verified": "verified",
        "mismatch": "refuted",
        "conflict": "refuted",
        "abstain": "unverifiable",
        "unreadable": "unverifiable",
        "unverifiable": "unverifiable",
    }.get(identity_status_key, "unknown")

    resolution = failed.get("resolution")
    rung = resolution.get("rung") if isinstance(resolution, dict) else None
    rung_key = rung if isinstance(rung, str) else ""
    resolution_rung = {
        "structural": "structural",
        "template": "template",
        "template_global": "template",
        "ocr": "ocr",
        "geometry": "geometry",
        "grounder": "grounder",
    }.get(
        rung_key, "none" if failed and not isinstance(resolution, dict) else "unknown"
    )

    uncertainty = failed.get("delivery_uncertainty")
    delivery_state = (
        "uncertain"
        if isinstance(uncertainty, dict)
        else "confirmed"
        if isinstance(failed.get("delivery_receipt"), dict)
        else "unknown"
    )
    error = str(failed.get("error") or "").lower()
    if isinstance(uncertainty, dict):
        failure_kind = "delivery_uncertain"
    elif identity_state == "refuted":
        failure_kind = "identity_refuted"
    elif identity_state == "unverifiable":
        failure_kind = "identity_unverifiable"
    elif failed.get("effect_verified") is False:
        evidence = failed.get("effect_evidence")
        refuted = isinstance(evidence, list) and any(
            isinstance(item, dict) and item.get("final_verdict") == "refuted"
            for item in evidence
        )
        failure_kind = "effect_refuted" if refuted else "effect_unverifiable"
    elif failed.get("postconditions_ok") is False or "stale" in error:
        failure_kind = "stale_state"
    elif "ambiguous" in error:
        failure_kind = "resolution_ambiguous"
    elif status == "failed" and not report:
        failure_kind = "infrastructure_failure"
    elif failed.get("failure_category") == "runtime_failure" or status == "failed":
        failure_kind = "runtime_bug"
    else:
        failure_kind = "target_not_found"

    profile = report.get("execution_profile")
    if profile not in {"demo", "standard", "regulated"}:
        profile = "unknown"
    network = report.get("external_network_calls")
    if network not in {"none", "observed", "unknown"}:
        network = "unknown"
    model_calls = report.get("model_calls")
    if (
        isinstance(model_calls, bool)
        or not isinstance(model_calls, int)
        or model_calls < 0
    ):
        model_calls = 0
    known_substrates = {"web", "windows", "macos", "linux", "rdp", "citrix", "mixed"}
    runtime_version = __version__ if _VERSION.fullmatch(__version__) else "unknown"
    occurred = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return {
        "schema": AUTOMATION_FAILURE_SIGNAL_SCHEMA,
        "failure_kind": failure_kind,
        "substrate": substrate if substrate in known_substrates else "unknown",
        "action_kind": "unknown",
        "risk_class": "unknown",
        "resolution_rung": resolution_rung,
        "identity_state": identity_state,
        "effect_tier": _effect_tier(report),
        "delivery_state": delivery_state,
        "execution_profile": profile,
        "outcome": _outcome(report, status),
        "runtime_version": runtime_version,
        "model_calls": min(model_calls, 1_000_000),
        "external_network_calls": network,
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
    }
