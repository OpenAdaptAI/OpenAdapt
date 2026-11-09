"""Exact, practice-scoped eligibility route resolution.

The shipped YAML is a synthetic TEST-mode example.  Production payer maps are
deployment data/recipes: the public engine supplies the schema and fail-closed
resolver, while each practice supplies reviewed bindings from Stedi's Payer
Network and its own portal policy.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from openadapt_flow.eligibility.client import (
    ApplicationMode,
    EligibilityRequest,
    EligibilityResult,
    StediEligibilityClient,
)

DEFAULT_REGISTRY_PATH = Path(__file__).parent / "payer_routes.yaml"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EligibilityRoute(str, Enum):
    API = "api"
    PORTAL = "portal"
    QUEUE = "queue"
    EXCLUDED = "excluded"


class PayerCapability(BaseModel):
    """Reviewed route binding for one exact payer in one practice account."""

    key: str
    display_name: str = ""
    route: EligibilityRoute
    aliases: list[str] = Field(default_factory=list)
    request_payer_id: Optional[str] = None
    stedi_id: Optional[str] = None
    application_mode: Optional[ApplicationMode] = None
    practice_account_id: Optional[str] = None
    supported_service_type_codes: list[str] = Field(default_factory=list)
    payer_record_sha256: Optional[str] = None
    portal_banned: bool = False
    portal_fallback_reviewed: bool = False
    verified_on: Optional[str] = None
    notes: str = ""

    @field_validator("verified_on")
    @classmethod
    def _verification_date(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _api_binding_complete(self) -> "PayerCapability":
        if self.route is EligibilityRoute.PORTAL and self.portal_banned:
            raise ValueError("a portal-banned payer cannot use the portal route")
        if self.portal_banned and self.portal_fallback_reviewed:
            raise ValueError("a portal-banned payer cannot approve portal fallback")
        if self.route is EligibilityRoute.API:
            missing = [
                name
                for name, value in (
                    ("request_payer_id", self.request_payer_id),
                    ("stedi_id", self.stedi_id),
                    ("application_mode", self.application_mode),
                    ("practice_account_id", self.practice_account_id),
                    ("supported_service_type_codes", self.supported_service_type_codes),
                    ("payer_record_sha256", self.payer_record_sha256),
                    ("verified_on", self.verified_on),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"API route lacks exact reviewed binding fields: {missing}"
                )
            if not re.fullmatch(r"[A-Z0-9]{5}", self.stedi_id or ""):
                raise ValueError("stedi_id must be the stable five-character Stedi ID")
            if not _SHA256_RE.fullmatch(self.payer_record_sha256 or ""):
                raise ValueError(
                    "payer_record_sha256 must bind the reviewed directory record"
                )
        return self


class PayerRegistry(BaseModel):
    version: int = 2
    default_route: EligibilityRoute = EligibilityRoute.QUEUE
    payers: dict[str, PayerCapability] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_names(self) -> "PayerRegistry":
        if self.default_route is not EligibilityRoute.QUEUE:
            raise ValueError("unmatched payers must default to the attended queue")
        seen: dict[str, str] = {}
        for key, cap in self.payers.items():
            for name in (
                key,
                cap.key,
                cap.display_name,
                cap.request_payer_id,
                cap.stedi_id,
                *cap.aliases,
            ):
                if name is None:
                    continue
                normalized = _normalize(name)
                if not normalized:
                    continue
                previous = seen.get(normalized)
                if previous is not None and previous != key:
                    raise ValueError(
                        f"payer registry name/alias {name!r} maps to both {previous!r} and {key!r}"
                    )
                seen[normalized] = key
        return self

    def find(self, payer: str) -> Optional[PayerCapability]:
        wanted = _normalize(payer)
        if not wanted:
            return None
        for cap in self.payers.values():
            if any(
                _normalize(name) == wanted
                for name in (
                    cap.key,
                    cap.display_name,
                    cap.request_payer_id,
                    cap.stedi_id,
                    *cap.aliases,
                )
                if name
            ):
                return cap
        return None


class RouteDecision(BaseModel):
    route: EligibilityRoute
    payer: str
    capability: Optional[PayerCapability] = None
    reason: str = ""

    @property
    def use_api(self) -> bool:
        return self.route is EligibilityRoute.API


class WaterfallOutcome(BaseModel):
    final_route: EligibilityRoute
    decision: RouteDecision
    result: Optional[EligibilityResult] = None
    trail: list[str] = Field(default_factory=list)

    @property
    def answered_by_api(self) -> bool:
        return self.result is not None and self.result.is_answer


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def load_payer_routes(path: Union[str, Path, None] = None) -> PayerRegistry:
    """Load and fully validate a route registry; malformed input fails loud."""
    import yaml

    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("payers"), dict):
        raise ValueError("payer route registry must contain a payer mapping")
    payers: dict[str, PayerCapability] = {}
    for key, entry in raw["payers"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"payer route entry {key!r} is not a mapping")
        payers[str(key)] = PayerCapability(key=str(key), **entry)
    return PayerRegistry(
        version=int(raw.get("version", 2)),
        default_route=EligibilityRoute(raw.get("default_route", "queue")),
        payers=payers,
    )


def resolve_route(
    payer: str, registry: Optional[PayerRegistry] = None
) -> RouteDecision:
    reg = registry if registry is not None else load_payer_routes()
    cap = reg.find(payer)
    if cap is None:
        return RouteDecision(
            route=EligibilityRoute.QUEUE,
            payer=payer,
            reason=f"payer {payer!r} has no exact reviewed route binding; queue for resolution",
        )
    return RouteDecision(
        route=cap.route,
        payer=payer,
        capability=cap,
        reason=f"payer {payer!r} resolved to reviewed {cap.route.value} route {cap.key!r}",
    )


def _queue(
    decision: RouteDecision,
    trail: list[str],
    reason: str,
    result: Optional[EligibilityResult] = None,
) -> WaterfallOutcome:
    trail.append(reason)
    return WaterfallOutcome(
        final_route=EligibilityRoute.QUEUE,
        decision=decision,
        result=result,
        trail=trail,
    )


def run_waterfall(
    request: EligibilityRequest,
    *,
    payer: Optional[str] = None,
    client: Optional[StediEligibilityClient] = None,
    registry: Optional[PayerRegistry] = None,
) -> WaterfallOutcome:
    """Run one exact route decision and bounded idempotent API attempt."""
    decision = resolve_route(payer or request.payer_id, registry)
    trail = [decision.reason]
    cap = decision.capability
    if decision.route is EligibilityRoute.QUEUE:
        return WaterfallOutcome(
            final_route=decision.route, decision=decision, trail=trail
        )
    if decision.route is EligibilityRoute.EXCLUDED:
        return WaterfallOutcome(
            final_route=decision.route, decision=decision, trail=trail
        )
    if decision.route is EligibilityRoute.PORTAL:
        return WaterfallOutcome(
            final_route=decision.route, decision=decision, trail=trail
        )
    assert cap is not None

    if request.payer_id != cap.request_payer_id:
        return _queue(
            decision,
            trail,
            "request payer ID does not exactly match the reviewed route binding",
        )
    unsupported = set(request.service_type_codes) - set(
        cap.supported_service_type_codes
    )
    if unsupported:
        return _queue(
            decision,
            trail,
            "requested service type is not in the reviewed payer binding",
        )
    if client is None:
        return _queue(decision, trail, "API route has no practice-held client boundary")
    if (
        client.account.practice_account_id != cap.practice_account_id
        or client.account.application_mode is not cap.application_mode
    ):
        return _queue(
            decision,
            trail,
            "client account/mode does not match the reviewed payer binding",
        )

    result = client.check(request)
    trail.append(result.reason)
    if result.is_answer:
        return WaterfallOutcome(
            final_route=EligibilityRoute.API,
            decision=decision,
            result=result,
            trail=trail,
        )
    if (
        result.portal_fallback_allowed
        and cap.portal_fallback_reviewed
        and not cap.portal_banned
    ):
        trail.append(
            "bounded transient API attempts exhausted; use the reviewed portal fallback"
        )
        return WaterfallOutcome(
            final_route=EligibilityRoute.PORTAL,
            decision=decision,
            result=result,
            trail=trail,
        )
    if cap.portal_banned:
        return _queue(
            decision,
            trail,
            "portal fallback is barred; route the evidence-bearing halt to practice staff",
            result,
        )
    if result.portal_fallback_allowed and not cap.portal_fallback_reviewed:
        return _queue(
            decision,
            trail,
            "transient API attempts exhausted, but no portal fallback is explicitly reviewed",
            result,
        )
    return _queue(
        decision,
        trail,
        "non-transient or ambiguous API outcome requires practice review",
        result,
    )
