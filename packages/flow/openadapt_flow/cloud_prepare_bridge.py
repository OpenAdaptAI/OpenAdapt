"""Opt-in zero-PHI bridge between local FormHaltItem halts and Cloud Prepare.

Converts live local ``FormHaltItem`` halts into zero-PHI ``PrepareCard`` JSON
payloads for ``POST /api/prepare``, polls ``GET /api/prepare?card_id=<id>`` for
the signed operator resolution, verifies the canonical SHA-256 digest, and
converts the resolution back into a ``HumanResolutionInput`` (with optional
durable submission to ``BoundDecisionStore``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from openadapt_flow.copilot_bridge import (
    is_secret_key_name,
    is_secret_selector,
)
from openadapt_flow.copilot_protocol import (
    BoundDecisionStore,
    CopilotBoundDecision,
    CopilotRunBinding,
    FormHaltItem,
    FormHaltKind,
    HumanDecisionAction,
    HumanResolutionInput,
)

_RAW_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_RAW_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RAW_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_RAW_SECRET_RE = re.compile(
    r"(?:"
    r"\bBearer\s+\S+"
    r"|\bgh[pousr]_[A-Za-z0-9_]{16,}\b"
    r"|\bsk-[A-Za-z0-9_\-]{16,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"
    r"|\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"
    r")",
    re.I,
)

_DIGEST_ENVELOPE_KEYS = frozenset(
    {
        "payload_sha256",
        "digest_sha256",
        "signature_scheme",
        "signature_preview",
    }
)


class ZeroPhiViolationError(ValueError):
    """Raised when raw PHI or secret material would cross the cloud boundary."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_zero_phi_value(value: str, context_label: str) -> None:
    """Reject raw SSNs, emails, phone numbers, and secret tokens."""
    if _RAW_SSN_RE.search(value):
        raise ZeroPhiViolationError(
            f"Refused raw SSN pattern in {context_label}; use a $var reference instead."
        )
    if _RAW_EMAIL_RE.search(value):
        raise ZeroPhiViolationError(
            f"Refused raw email address in {context_label}; use a $var reference instead."
        )
    if _RAW_PHONE_RE.search(value):
        raise ZeroPhiViolationError(
            f"Refused raw phone number in {context_label}; use a $var reference instead."
        )
    if _RAW_SECRET_RE.search(value):
        raise ZeroPhiViolationError(
            f"Refused secret token value in {context_label}; keep credentials local."
        )


def _assert_zero_phi_recursive(value: Any, context_label: str) -> None:
    if isinstance(value, str):
        assert_zero_phi_value(value, context_label)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_str = str(key)
            assert_zero_phi_value(key_str, f"{context_label} key")
            _assert_zero_phi_recursive(item, f"{context_label}[{key_str}]")
        return
    if isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            _assert_zero_phi_recursive(item, f"{context_label}[{idx}]")


def assert_prepare_card_zero_phi(card: Mapping[str, Any]) -> None:
    """Verify that a PrepareCard payload contains no raw PHI or secret values."""
    _assert_zero_phi_recursive(card, "PrepareCard")

    staged = card.get("staged_bindings")
    if isinstance(staged, Mapping):
        for idx, val in staged.items():
            if is_secret_key_name(str(val)):
                raise ZeroPhiViolationError(
                    f"Refused secret variable binding in staged_bindings[{idx}]."
                )

    labels = card.get("control_labels")
    if isinstance(labels, Mapping):
        for idx, label in labels.items():
            if is_secret_key_name(str(label)):
                raise ZeroPhiViolationError(
                    f"Refused secret field label in control_labels[{idx}]."
                )

    selectors = card.get("control_selectors")
    if isinstance(selectors, Mapping):
        for idx, selector in selectors.items():
            if is_secret_selector(str(selector)):
                raise ZeroPhiViolationError(
                    f"Refused secret selector in control_selectors[{idx}]."
                )

    slots = card.get("unresolved_slots")
    if isinstance(slots, (list, tuple)):
        for idx, slot in enumerate(slots):
            if not isinstance(slot, Mapping):
                continue
            if is_secret_key_name(str(slot.get("control_label") or "")):
                raise ZeroPhiViolationError(
                    f"Refused secret control_label in unresolved_slots[{idx}]."
                )
            if is_secret_key_name(str(slot.get("parameter_name") or "")):
                raise ZeroPhiViolationError(
                    f"Refused secret parameter_name in unresolved_slots[{idx}]."
                )


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically for SHA-256 hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_resolution_sha256(payload: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 hex digest over the unsigned resolution payload."""
    unsigned = {
        key: val
        for key, val in payload.items()
        if key not in _DIGEST_ENVELOPE_KEYS
    }
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def verify_resolution_digest(payload: Mapping[str, Any]) -> str | None:
    """Verify ``payload_sha256`` and/or ``digest_sha256`` when present.

    Returns the verified hex digest, or ``None`` if neither digest field is present.
    Raises ``ValueError`` when a digest or signature preview does not match.
    """
    has_payload_sha = "payload_sha256" in payload and payload["payload_sha256"] is not None
    has_digest_sha = "digest_sha256" in payload and payload["digest_sha256"] is not None
    if not has_payload_sha and not has_digest_sha:
        return None

    recomputed = compute_resolution_sha256(payload)

    if has_payload_sha:
        claimed = payload["payload_sha256"]
        if not isinstance(claimed, str) or not claimed.strip():
            raise ValueError("Resolution payload_sha256 must be a non-empty hex string.")
        if not hmac.compare_digest(claimed.strip().lower(), recomputed):
            raise ValueError(
                "Resolution payload_sha256 digest mismatch; payload may have been tampered with."
            )

    if has_digest_sha:
        claimed_digest = payload["digest_sha256"]
        if not isinstance(claimed_digest, str) or not claimed_digest.strip():
            raise ValueError("Resolution digest_sha256 must be a non-empty hex string.")
        if not hmac.compare_digest(claimed_digest.strip().lower(), recomputed):
            raise ValueError(
                "Resolution digest_sha256 mismatch; payload may have been tampered with."
            )
        sig_preview = payload.get("signature_preview")
        if sig_preview is not None:
            expected_sig = hashlib.sha256(
                f"ed25519:{recomputed}".encode("utf-8")
            ).hexdigest()[:32]
            if not isinstance(sig_preview, str) or not hmac.compare_digest(
                sig_preview.strip().lower(), expected_sig
            ):
                raise ValueError("Resolution signature_preview verification failed.")

    return recomputed


def _pause_reason_for_halt(kind: FormHaltKind) -> str:
    if kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD:
        return "missing_required_field"
    if kind in {
        FormHaltKind.MODAL_CONFIRMATION_REQUIRED,
        FormHaltKind.AUTH_WALL_DETECTED,
        FormHaltKind.MUTATION_BUDGET_EXCEEDED,
    }:
        return "high_risk_approval"
    return "policy_conflict"


def _select_control_idx(
    halt: FormHaltItem,
    staged: Mapping[int, str],
    labels: Mapping[int, str],
    selectors: Mapping[int, str],
) -> int:
    if halt.dom_selector:
        for idx, sel in selectors.items():
            if sel == halt.dom_selector:
                return idx
    if halt.field_label:
        target = halt.field_label.strip().casefold()
        for idx, label in labels.items():
            if label.strip().casefold() == target:
                return idx
    if halt.control_id and halt.control_id.isdigit():
        parsed = int(halt.control_id)
        if parsed not in staged:
            return parsed
    existing = set(staged.keys()) | set(labels.keys()) | set(selectors.keys())
    return (max(existing) + 1) if existing else 0


def form_halt_to_prepare_card(
    halt: FormHaltItem,
    *,
    portal_origin: str,
    card_id: str | None = None,
    staged_bindings: Mapping[int, str] | None = None,
    control_labels: Mapping[int, str] | None = None,
    control_selectors: Mapping[int, str] | None = None,
    workflow_summary: str | None = None,
    office_name: str | None = None,
    portal_name: str | None = None,
) -> dict[str, Any]:
    """Convert a live local ``FormHaltItem`` into a zero-PHI ``PrepareCard`` JSON dict.

    Never forwards ``halt.portal_value`` or ``halt.dossier_value`` into the cloud card.
    Raises ``ZeroPhiViolationError`` (a ``ValueError`` subclass) if any card field
    contains a raw SSN, email, phone number, secret token, or targets a secret field.
    """
    clean_origin = portal_origin.strip()
    if not clean_origin:
        raise ValueError("portal_origin must be a non-empty string.")
    parsed_origin = urllib.parse.urlsplit(clean_origin)
    if parsed_origin.username or parsed_origin.password:
        raise ZeroPhiViolationError(
            "Refused credentials embedded in portal_origin."
        )

    if (
        is_secret_key_name(halt.dossier_key)
        or is_secret_key_name(halt.control_id)
        or is_secret_key_name(halt.field_label)
        or is_secret_selector(halt.dom_selector)
    ):
        raise ZeroPhiViolationError(
            "Refused to send a secret or credential halt to Cloud Prepare; handle it locally."
        )

    resolved_card_id = (card_id or "").strip() or f"prep_{halt.halt_id}"

    staged: dict[int, str] = {
        int(idx): str(val) for idx, val in (staged_bindings or {}).items()
    }
    labels: dict[int, str] = {
        int(idx): str(val) for idx, val in (control_labels or {}).items()
    }
    selectors: dict[int, str] = {
        int(idx): str(val) for idx, val in (control_selectors or {}).items()
    }

    control_idx = _select_control_idx(halt, staged, labels, selectors)
    staged.pop(control_idx, None)

    slot_label = (
        halt.field_label.strip()
        or (halt.control_id or "").strip()
        or (halt.dossier_key or "").strip()
        or halt.kind.value
    )
    labels[control_idx] = slot_label
    if halt.dom_selector and halt.dom_selector.strip():
        selectors[control_idx] = halt.dom_selector.strip()

    allowed_enum_options: list[str] = []
    seen_options: set[str] = set()
    for opt in halt.available_options:
        entry = opt.label.strip() or opt.value.strip()
        if entry and entry not in seen_options:
            seen_options.add(entry)
            allowed_enum_options.append(entry)

    input_mode = "choice" if allowed_enum_options else "typed"

    unresolved_slot: dict[str, Any] = {
        "control_idx": control_idx,
        "control_label": slot_label,
        "reason": halt.message,
        "candidate_vars": [],
        "allowed_enum_options": allowed_enum_options,
        "expected_field_type": "exact",
        "input_mode": input_mode,
    }
    if halt.dossier_key and halt.dossier_key.strip():
        unresolved_slot["parameter_name"] = halt.dossier_key.strip()

    pause_reason = _pause_reason_for_halt(halt.kind)
    pause_category = (
        "Missing Info"
        if input_mode == "typed" or pause_reason == "missing_required_field"
        else "Policy Sign-Off"
    )

    card: dict[str, Any] = {
        "card_id": resolved_card_id,
        "portal_origin": clean_origin,
        "staged_bindings": staged,
        "unresolved_slots": [unresolved_slot],
        "control_labels": labels,
        "control_selectors": selectors,
        "created_at_utc": _utc_now_iso(),
        "commit_mode": "DRAFT_TO_COMMIT",
        "redacted_preview": True,
        "pause_reason": pause_reason,
        "pause_category": pause_category,
        "session_state": "live",
    }
    if office_name and office_name.strip():
        card["office_name"] = office_name.strip()
    if portal_name and portal_name.strip():
        card["portal_name"] = portal_name.strip()
    if workflow_summary and workflow_summary.strip():
        card["workflow_summary"] = workflow_summary.strip()

    assert_prepare_card_zero_phi(card)
    return card


def _extract_bound_slot_value(
    halt: FormHaltItem,
    resolution_payload: Mapping[str, Any],
    nested_resolution: Mapping[str, Any],
) -> str:
    candidate_maps: list[Mapping[Any, Any]] = []
    for source in (
        nested_resolution.get("slot_bindings"),
        resolution_payload.get("slot_bindings"),
        resolution_payload.get("merged_bindings"),
    ):
        if isinstance(source, Mapping) and source:
            candidate_maps.append(source)

    for mapping in candidate_maps:
        if len(mapping) == 1:
            only_val = next(iter(mapping.values()))
            text = str(only_val).strip()
            if text:
                return text

        # Prefer control_id when numeric, else slot 0, else lowest integer key.
        preferred_keys: list[Any] = []
        if halt.control_id and halt.control_id.isdigit():
            preferred_keys.extend([int(halt.control_id), halt.control_id])
        preferred_keys.extend([0, "0"])
        for key in preferred_keys:
            if key in mapping:
                text = str(mapping[key]).strip()
                if text:
                    return text

        sorted_items = sorted(
            ((int(k), str(v).strip()) for k, v in mapping.items() if str(k).lstrip("-").isdigit()),
            key=lambda pair: pair[0],
        )
        for _, text in sorted_items:
            if text:
                return text

    param_values = nested_resolution.get("parameter_values")
    if isinstance(param_values, Mapping) and param_values:
        if halt.dossier_key and halt.dossier_key in param_values:
            text = str(param_values[halt.dossier_key]).strip()
            if text:
                return text
        for val in param_values.values():
            text = str(val).strip()
            if text:
                return text

    raise ValueError(
        "Approved PrepareResolution is missing a non-empty bound slot value."
    )


def _map_option_label_to_value(halt: FormHaltItem, chosen: str) -> str:
    if not halt.available_options:
        return chosen
    for opt in halt.available_options:
        if chosen == opt.value:
            return opt.value
    for opt in halt.available_options:
        if chosen == opt.label:
            return opt.value
    chosen_cf = chosen.strip().casefold()
    for opt in halt.available_options:
        if opt.label.strip().casefold() == chosen_cf:
            return opt.value
    return chosen


def prepare_resolution_to_human_input(
    halt: FormHaltItem,
    resolution_payload: Mapping[str, Any],
    *,
    remember_as_candidate_rule: bool = False,
) -> HumanResolutionInput:
    """Verify a signed Cloud Prepare resolution and convert it to ``HumanResolutionInput``."""
    verified_digest = verify_resolution_digest(resolution_payload)

    action = str(resolution_payload.get("action") or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise ValueError(
            f"Unsupported PrepareResolution action {action!r}; expected 'approve' or 'reject'."
        )

    nested = resolution_payload.get("resolution")
    nested_resolution: Mapping[str, Any] = nested if isinstance(nested, Mapping) else {}

    actor_id = (
        str(
            nested_resolution.get("operator_id")
            or resolution_payload.get("operator_id")
            or "cloud_prepare_operator"
        ).strip()
        or "cloud_prepare_operator"
    )
    card_id = (
        str(
            resolution_payload.get("card_id")
            or nested_resolution.get("card_id")
            or halt.halt_id
        ).strip()
        or halt.halt_id
    )
    resolved_at = str(
        nested_resolution.get("resolved_at_utc")
        or resolution_payload.get("resolved_at_utc")
        or ""
    ).strip()

    if verified_digest:
        idempotency_key = f"cloud-prepare:{card_id}:{verified_digest[:16]}"
    else:
        fallback_digest = compute_resolution_sha256(resolution_payload)[:16]
        suffix = resolved_at or fallback_digest
        idempotency_key = f"cloud-prepare:{card_id}:{action}:{suffix}"

    if action == "reject":
        reason_note = (
            str(
                resolution_payload.get("rejection_reason")
                or nested_resolution.get("rejection_reason")
                or resolution_payload.get("note")
                or "Rejected in Cloud Prepare"
            ).strip()
            or "Rejected in Cloud Prepare"
        )
        assert_zero_phi_value(reason_note, "rejection_reason")
        return HumanResolutionInput(
            halt_id=halt.halt_id,
            action=HumanDecisionAction.ABORT_RUN,
            actor_id=actor_id,
            actor_role="operator",
            provided_value=None,
            mapped_dossier_key=None,
            remember_as_candidate_rule=False,
            note=reason_note,
            idempotency_key=idempotency_key,
        )

    chosen_raw = _extract_bound_slot_value(halt, resolution_payload, nested_resolution)
    assert_zero_phi_value(chosen_raw, "approved slot_binding")
    provided_value = _map_option_label_to_value(halt, chosen_raw)

    note = str(resolution_payload.get("note") or "").strip()
    if note:
        assert_zero_phi_value(note, "resolution note")

    return HumanResolutionInput(
        halt_id=halt.halt_id,
        action=HumanDecisionAction.PROVIDE_VALUE,
        actor_id=actor_id,
        actor_role="operator",
        provided_value=provided_value,
        mapped_dossier_key=halt.dossier_key,
        remember_as_candidate_rule=remember_as_candidate_rule,
        note=note,
        idempotency_key=idempotency_key,
    )


def _extract_resolution_from_poll_response(
    data: Mapping[str, Any], card_id: str
) -> dict[str, Any] | None:
    resolutions = data.get("resolutions")
    if isinstance(resolutions, list):
        for item in resolutions:
            if isinstance(item, Mapping) and item.get("card_id") == card_id:
                return dict(item)

    for key in ("audit", "resolution"):
        candidate = data.get(key)
        if isinstance(candidate, Mapping) and (
            candidate.get("card_id") == card_id or "action" in candidate
        ):
            if "action" in candidate:
                return dict(candidate)

    if data.get("card_id") == card_id and "action" in data:
        return dict(data)

    return None


class CloudPrepareClient:
    """Stdlib HTTP client for enqueuing and polling zero-PHI Cloud Prepare cards."""

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        clean_base = base_url.strip().rstrip("/")
        if not clean_base.startswith(("http://", "https://")):
            raise ValueError("base_url must start with 'http://' or 'https://'.")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
        self.base_url = clean_base
        self.api_token = api_token.strip() if api_token else None
        self.request_timeout_seconds = float(request_timeout_seconds)

    def _headers(self, *, has_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body_bytes = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=self._headers(has_body=body_bytes is not None),
            method=method,
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.request_timeout_seconds
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Cloud Prepare {method} {url} failed with HTTP {exc.code}: {detail}"
            ) from exc

        parsed = json.loads(raw) if raw.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Expected JSON object from Cloud Prepare {method} {url}, got {type(parsed).__name__}."
            )
        return parsed

    def enqueue(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """Enqueue a zero-PHI ``PrepareCard`` via ``POST /api/prepare``."""
        assert_prepare_card_zero_phi(card)
        url = f"{self.base_url}/api/prepare"
        return self._request_json(
            "POST",
            url,
            payload={"type": "enqueue", "card": dict(card)},
        )

    def poll_resolution(
        self,
        card_id: str,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Poll ``GET /api/prepare?card_id=<id>`` until a resolution arrives or timeout."""
        clean_card_id = card_id.strip()
        if not clean_card_id:
            raise ValueError("card_id must be a non-empty string.")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative.")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative.")

        query = urllib.parse.urlencode({"card_id": clean_card_id})
        url = f"{self.base_url}/api/prepare?{query}"
        deadline = time.monotonic() + timeout_seconds

        while True:
            data = self._request_json("GET", url)
            found = _extract_resolution_from_poll_response(data, clean_card_id)
            if found is not None:
                return found

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for Cloud Prepare resolution for card_id={clean_card_id!r} "
                    f"after {timeout_seconds}s."
                )
            time.sleep(min(poll_interval_seconds, remaining))

    def resolve_halt_via_cloud(
        self,
        halt: FormHaltItem,
        *,
        portal_origin: str,
        card_id: str | None = None,
        staged_bindings: Mapping[int, str] | None = None,
        control_labels: Mapping[int, str] | None = None,
        control_selectors: Mapping[int, str] | None = None,
        workflow_summary: str | None = None,
        office_name: str | None = None,
        portal_name: str | None = None,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
        remember_as_candidate_rule: bool = False,
        binding: CopilotRunBinding | None = None,
        store: BoundDecisionStore | None = None,
        validate_current: Callable[[CopilotBoundDecision], None] | None = None,
    ) -> HumanResolutionInput:
        """Enqueue a halt, poll for the signed resolution, convert it, and optionally store it."""
        if (binding is None) != (store is None):
            raise ValueError(
                "Both binding and store must be provided together when persisting a bound decision."
            )

        card = form_halt_to_prepare_card(
            halt,
            portal_origin=portal_origin,
            card_id=card_id,
            staged_bindings=staged_bindings,
            control_labels=control_labels,
            control_selectors=control_selectors,
            workflow_summary=workflow_summary,
            office_name=office_name,
            portal_name=portal_name,
        )
        self.enqueue(card)
        resolution_payload = self.poll_resolution(
            card["card_id"],
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        decision = prepare_resolution_to_human_input(
            halt,
            resolution_payload,
            remember_as_candidate_rule=remember_as_candidate_rule,
        )

        if binding is not None and store is not None:
            bound = CopilotBoundDecision(binding=binding, decision=decision)

            def _default_validate(item: CopilotBoundDecision) -> None:
                if item.binding != binding or item.binding.expires_at_unix <= time.time():
                    raise ValueError(
                        "Cloud Prepare decision does not match an active run binding."
                    )

            store.append(bound, validate_current or _default_validate)

        return decision


__all__ = [
    "CloudPrepareClient",
    "ZeroPhiViolationError",
    "assert_prepare_card_zero_phi",
    "assert_zero_phi_value",
    "canonical_json",
    "compute_resolution_sha256",
    "form_halt_to_prepare_card",
    "prepare_resolution_to_human_input",
    "verify_resolution_digest",
]
