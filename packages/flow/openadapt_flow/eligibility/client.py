"""Fail-closed Stedi 270/271 eligibility client.

The public module is mechanism, not a production payer recipe.  A deployment
supplies a practice-owned account boundary and an exact, reviewed payer binding
through :mod:`openadapt_flow.eligibility.waterfall`.

Primary contract references (reviewed 2026-07-21):

* https://www.stedi.com/docs/healthcare/api-reference/post-healthcare-eligibility
* https://www.stedi.com/docs/healthcare/send-eligibility-checks
* https://www.stedi.com/docs/healthcare/eligibility-troubleshooting
* https://www.stedi.com/docs/healthcare/eligibility-patient-responsibility-benefits
* https://www.stedi.com/docs/healthcare/integrated-account-overview

No request body or response body is logged.  Reasons contain only payer IDs,
HTTP/AAA codes, and fixed classifications.  Raw 271 bytes are returned solely
for the explicit PHI-bearing local artifact boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.runtime.effects.auth import AuthRef

STEDI_ELIGIBILITY_URL = (
    "https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3"
)
SERVICE_TYPE_DENTAL = "35"
STEDI_API_KEY_ENV = "STEDI_API_KEY"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_AAA_TRANSIENT = {"42", "80"}
_AAA_MEMBER = {"65", "67", "72", "73", "75", "76"}
_AAA_PROVIDER = {
    "41",
    "43",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",
    "50",
    "51",
    "52",
    "53",
    "97",
}
_AAA_INVALID_PAYER = {"T4"}
_CODE_ACTIVE = "1"
_CODES_INACTIVE = {"6", "7", "8"}
_DATE_RE = re.compile(r"^\d{8}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ApplicationMode(str, Enum):
    TEST = "test"
    PRODUCTION = "production"


class EligibilityStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NOT_FOUND = "not_found"
    PAYER_UNAVAILABLE = "payer_unavailable"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class ErrorCategory(str, Enum):
    AUTH_CONFIGURATION = "auth_configuration"
    INVALID_PAYER = "invalid_payer"
    INVALID_REQUEST = "invalid_request"
    MEMBER_IDENTITY = "member_identity"
    PROVIDER_CONFIGURATION = "provider_configuration"
    THROTTLED = "throttled"
    PAYER_TRANSIENT = "payer_transient"
    TRANSPORT_TRANSIENT = "transport_transient"
    SERVER_TRANSIENT = "server_transient"
    RESPONSE_INVALID = "response_invalid"
    RESPONSE_AMBIGUOUS = "response_ambiguous"


class RetryDisposition(str, Enum):
    NO_RETRY_QUEUE = "no_retry_queue"
    RETRY_THEN_PORTAL = "retry_then_portal"
    RETRY_THEN_QUEUE = "retry_then_queue"


class StediAccountBoundary(BaseModel):
    """Practice-held Stedi credential and tenancy boundary.

    Production keys are accepted only when the practice owns the account and
    its BAA has been confirmed.  The identifier must be operational, not PHI.
    """

    practice_account_id: str
    application_mode: ApplicationMode
    credential_env: str = STEDI_API_KEY_ENV
    practice_holds_account: bool = True
    baa_confirmed: bool = False

    @field_validator("practice_account_id", "credential_env")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("must be a non-PHI operational identifier")
        return value

    @model_validator(mode="after")
    def _production_boundary(self) -> "StediAccountBoundary":
        if not self.practice_holds_account:
            raise ValueError("Stedi credentials must belong to the practice account")
        if (
            self.application_mode is ApplicationMode.PRODUCTION
            and not self.baa_confirmed
        ):
            raise ValueError(
                "production Stedi use requires the practice BAA confirmation"
            )
        return self


class BenefitSelection(BaseModel):
    """Qualifiers for the practice-facing normalized benefit values.

    The complete qualified benefit list is always retained.  Convenience
    values are populated only when this selector yields one unambiguous value.
    """

    network_code: Optional[Literal["Y", "N", "W"]] = None
    coverage_level_code: Optional[str] = None
    time_qualifier_code: Optional[str] = None
    procedure_code: Optional[str] = None


class EligibilityRequest(BaseModel):
    """One idempotently tracked 270 read.

    ``operation_id`` is local correlation/idempotency data and is never sent to
    Stedi.  PHI fields are suppressed from repr, and callers must not log the
    result of ``model_dump``.
    """

    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    payer_id: str
    member_id: str = Field(repr=False)
    first_name: Optional[str] = Field(default=None, repr=False)
    last_name: Optional[str] = Field(default=None, repr=False)
    date_of_birth: Optional[str] = Field(default=None, repr=False)
    provider_npi: str
    provider_organization: Optional[str] = Field(default=None, repr=False)
    provider_first_name: Optional[str] = Field(default=None, repr=False)
    provider_last_name: Optional[str] = Field(default=None, repr=False)
    service_type_codes: list[str] = Field(default_factory=lambda: [SERVICE_TYPE_DENTAL])
    date_of_service: str
    benefit_selection: BenefitSelection = Field(default_factory=BenefitSelection)

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("operation_id must be an opaque non-PHI identifier")
        return value

    @field_validator("payer_id")
    @classmethod
    def _payer_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", value):
            raise ValueError("payer_id must be an exact 1-80 character identifier")
        return value

    @field_validator("member_id")
    @classmethod
    def _member_id(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 128:
            raise ValueError("member_id is required for verified eligibility")
        return value

    @field_validator("service_type_codes")
    @classmethod
    def _service_codes(cls, values: list[str]) -> list[str]:
        if not values or len(values) > 99 or len(set(values)) != len(values):
            raise ValueError("service_type_codes must contain 1-99 unique codes")
        if any(not v or len(v) > 3 or v != v.strip() for v in values):
            raise ValueError("service type codes must be exact non-empty strings")
        return values

    @field_validator("date_of_birth", "date_of_service")
    @classmethod
    def _date8(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            if not _DATE_RE.fullmatch(value):
                raise ValueError("date must use YYYYMMDD")
            datetime.strptime(value, "%Y%m%d")
        return value

    @model_validator(mode="after")
    def _identity_and_provider(self) -> "EligibilityRequest":
        org = bool(self.provider_organization)
        person = bool(self.provider_first_name and self.provider_last_name)
        if org == person:
            raise ValueError(
                "provider requires exactly organization or first/last name"
            )
        if not re.fullmatch(r"\d{10}", self.provider_npi):
            raise ValueError("provider_npi must contain exactly 10 digits")
        return self

    def to_stedi_body(self) -> dict[str, Any]:
        provider: dict[str, Any] = {"npi": self.provider_npi}
        if self.provider_organization:
            provider["organizationName"] = self.provider_organization
        else:
            provider["firstName"] = self.provider_first_name
            provider["lastName"] = self.provider_last_name
        subscriber: dict[str, Any] = {}
        for key, value in (
            ("firstName", self.first_name),
            ("lastName", self.last_name),
            ("dateOfBirth", self.date_of_birth),
            ("memberId", self.member_id),
        ):
            if value:
                subscriber[key] = value
        encounter: dict[str, Any] = {"serviceTypeCodes": list(self.service_type_codes)}
        encounter["dateOfService"] = self.date_of_service
        return {
            "tradingPartnerServiceId": self.payer_id,
            "provider": provider,
            "subscriber": subscriber,
            "encounter": encounter,
        }

    def safe_summary(self) -> dict[str, Any]:
        """Non-PHI operational summary suitable for diagnostics."""
        return {
            "operation_id": self.operation_id,
            "payer_id": self.payer_id,
            "service_type_codes": list(self.service_type_codes),
            "date_of_service_present": self.date_of_service is not None,
        }


class QualifiedBenefit(BaseModel):
    code: str
    value: str
    value_kind: Literal["amount", "percent"]
    service_type_codes: list[str]
    network_code: Optional[str] = None
    coverage_level_code: Optional[str] = None
    time_qualifier_code: Optional[str] = None
    procedure_code: Optional[str] = None
    benefit_dates: dict[str, str] = Field(default_factory=dict)


class EligibilityResult(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    status: EligibilityStatus
    payer_id: str
    operation_id: str
    application_mode: Optional[ApplicationMode] = None
    payer_name: Optional[str] = None
    plan_name: Optional[str] = None
    plan_begin: Optional[str] = None
    plan_end: Optional[str] = None
    coverage_by_service: dict[str, EligibilityStatus] = Field(default_factory=dict)
    benefits: list[QualifiedBenefit] = Field(default_factory=list)
    copay: Optional[str] = None
    coinsurance_percent: Optional[str] = None
    deductible_total: Optional[str] = None
    deductible_remaining: Optional[str] = None
    out_of_pocket_total: Optional[str] = None
    out_of_pocket_remaining: Optional[str] = None
    service_type_codes: list[str] = Field(default_factory=list)
    aaa_codes: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    error_category: Optional[ErrorCategory] = None
    retry_disposition: RetryDisposition = RetryDisposition.NO_RETRY_QUEUE
    reason: str = ""
    raw_271: Optional[dict[str, Any]] = Field(default=None, exclude=True, repr=False)
    raw_271_sha256: Optional[str] = None
    raw_271_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
    response_subject_sha256: Optional[str] = None
    http_status: Optional[int] = None
    checked_at: str = ""
    source: str = "stedi"
    attempt_count: int = 1
    request_sha256: str

    @field_validator("request_sha256", "raw_271_sha256", "response_subject_sha256")
    @classmethod
    def _digest(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("eligibility digests must be lowercase SHA-256")
        return value

    @property
    def is_answer(self) -> bool:
        return (
            self.status in (EligibilityStatus.ACTIVE, EligibilityStatus.INACTIVE)
            and not self.ambiguities
            and self.error_category is None
            and isinstance(self.application_mode, ApplicationMode)
            and self.response_subject_sha256 is not None
            and self.http_status is not None
            and 200 <= self.http_status < 300
            and self.raw_271_bytes is not None
            and self.raw_271_sha256 is not None
            and hashlib.sha256(self.raw_271_bytes).hexdigest() == self.raw_271_sha256
        )

    @property
    def retryable(self) -> bool:
        return self.retry_disposition in (
            RetryDisposition.RETRY_THEN_PORTAL,
            RetryDisposition.RETRY_THEN_QUEUE,
        )

    @property
    def portal_fallback_allowed(self) -> bool:
        return self.retry_disposition is RetryDisposition.RETRY_THEN_PORTAL


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def eligibility_request_sha256(request: EligibilityRequest) -> str:
    """Digest the exact JSON request body without persisting its PHI."""
    payload = json.dumps(
        request.to_stedi_body(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_identity_text(value: object) -> str:
    """Conservatively normalize identity text without dropping punctuation."""
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _verify_response_subject(
    body: Mapping[str, Any], request: EligibilityRequest
) -> tuple[Optional[str], Optional[str]]:
    """Bind a subscriber-only request to the exact subject returned by the payer.

    The current request schema deliberately has no dependent field.  A response
    containing dependent results is therefore not silently interpreted as the
    subscriber's answer.  The member ID is mandatory; every additional identity
    field supplied in the request must also be echoed and match after conservative
    Unicode/case/whitespace normalization.
    """
    dependents = body.get("dependents")
    if dependents not in (None, []):
        return None, "response contains dependent subjects for a subscriber request"
    if dependents is not None and not isinstance(dependents, list):
        return None, "response dependent subject collection is malformed"

    subscriber = body.get("subscriber")
    if not isinstance(subscriber, dict):
        return None, "response subscriber identity is missing or malformed"

    request_member = _normalized_identity_text(request.member_id)
    response_member_raw = subscriber.get("memberId")
    if response_member_raw is None:
        return None, "response subscriber member ID is missing"
    response_member = _normalized_identity_text(response_member_raw)
    if not response_member or response_member != request_member:
        return None, "response subscriber member ID does not match request"

    for request_field, response_field, label in (
        (request.first_name, "firstName", "first name"),
        (request.last_name, "lastName", "last name"),
        (request.date_of_birth, "dateOfBirth", "date of birth"),
    ):
        if request_field is None:
            continue
        response_value = subscriber.get(response_field)
        if response_value is None:
            return None, f"response subscriber {label} is missing"
        expected = _normalized_identity_text(request_field)
        observed = _normalized_identity_text(response_value)
        if not observed or observed != expected:
            return None, f"response subscriber {label} does not match request"
    return "subscriber", None


def _verify_response_provider(
    body: Mapping[str, Any], request: EligibilityRequest
) -> Optional[str]:
    provider = body.get("provider")
    if not isinstance(provider, dict):
        return "response provider identity is missing or malformed"
    response_npi = provider.get("npi")
    if response_npi is None or str(response_npi) != request.provider_npi:
        return "response provider NPI does not match request"
    return None


def _benefit_entries(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = body.get("benefitsInformation")
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def _collect_aaa_codes(body: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    errors = body.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict) and error.get("code") is not None:
                code = str(error["code"])
                if code not in codes:
                    codes.append(code)
    return codes


def _date_value(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _date_range(value: str) -> tuple[Optional[date], Optional[date]]:
    if "-" in value:
        begin, end = value.split("-", 1)
        return _date_value(begin), _date_value(end)
    one = _date_value(value)
    return one, one


def _coverage_interval(
    dates: Mapping[str, Any], service_date: date
) -> tuple[Optional[bool], Optional[str]]:
    starts: list[date] = []
    ends: list[date] = []
    recognized = False
    for key, raw in dates.items():
        is_range = key in {"benefit", "plan", "eligibility", "policy"}
        is_begin = key.lower().endswith(("begin", "effective"))
        is_end = key.lower().endswith(("end", "expiration"))
        if not (is_range or is_begin or is_end):
            continue
        recognized = True
        if not isinstance(raw, str):
            return None, "returned coverage interval is malformed"
        if is_range:
            begin, end = _date_range(raw)
            if begin is None or end is None or begin > end:
                return None, "returned coverage interval is malformed"
            if begin:
                starts.append(begin)
            if end:
                ends.append(end)
        elif is_begin:
            parsed = _date_value(raw)
            if parsed is None:
                return None, "returned coverage interval is malformed"
            starts.append(parsed)
        elif is_end:
            parsed = _date_value(raw)
            if parsed is None:
                return None, "returned coverage interval is malformed"
            ends.append(parsed)
    if not recognized or (not starts and not ends):
        return None, None
    if starts and ends and max(starts) > min(ends):
        return None, "returned coverage interval is contradictory"
    return (
        (not starts or service_date >= max(starts))
        and (not ends or service_date <= min(ends)),
        None,
    )


def _covers_service_date(
    dates: Mapping[str, Any], service_date: date
) -> Optional[bool]:
    covered, _problem = _coverage_interval(dates, service_date)
    return covered


def _coverage_by_service(
    body: Mapping[str, Any], request: EligibilityRequest
) -> tuple[dict[str, EligibilityStatus], list[str]]:
    entries = _benefit_entries(body)
    plan_status = body.get("planStatus")
    if isinstance(plan_status, list):
        entries = entries + [e for e in plan_status if isinstance(e, dict)]
    service_date = (
        _date_value(request.date_of_service) if request.date_of_service else None
    )
    output: dict[str, EligibilityStatus] = {}
    problems: list[str] = []
    for service in request.service_type_codes:
        matching = [
            e
            for e in entries
            if isinstance(e.get("serviceTypeCodes"), list)
            and service in [str(v) for v in e["serviceTypeCodes"]]
        ]
        codes = {str(e.get("code", e.get("statusCode", ""))) for e in matching}
        active = _CODE_ACTIVE in codes
        inactive = bool(codes & _CODES_INACTIVE)
        if active and inactive:
            problems.append(f"service {service}: conflicting active/inactive coverage")
            continue
        if not active and not inactive:
            problems.append(f"service {service}: no exact coverage signal")
            continue
        status = EligibilityStatus.ACTIVE if active else EligibilityStatus.INACTIVE
        if status is EligibilityStatus.ACTIVE and service_date:
            interval_results: list[bool] = []
            for entry in matching:
                if str(entry.get("code", entry.get("statusCode", ""))) != _CODE_ACTIVE:
                    continue
                info = entry.get("benefitsDateInformation")
                if isinstance(info, dict):
                    covered, interval_problem = _coverage_interval(info, service_date)
                    if interval_problem:
                        problems.append(f"service {service}: {interval_problem}")
                        continue
                    if covered is not None:
                        interval_results.append(covered)
            if not interval_results:
                plan_dates = body.get("planDateInformation")
                if isinstance(plan_dates, dict):
                    covered, interval_problem = _coverage_interval(
                        plan_dates, service_date
                    )
                    if interval_problem:
                        problems.append(f"service {service}: {interval_problem}")
                    elif covered is not None:
                        interval_results.append(covered)
            if not interval_results:
                problems.append(
                    f"service {service}: no returned active coverage interval"
                )
                continue
            if not all(interval_results):
                problems.append(
                    f"service {service}: service date outside returned benefit dates"
                )
                continue
        output[service] = status
    return output, problems


def _qualified_benefits(body: Mapping[str, Any]) -> list[QualifiedBenefit]:
    output: list[QualifiedBenefit] = []
    for entry in _benefit_entries(body):
        code = str(entry.get("code", ""))
        value_key = "benefitPercent" if code == "A" else "benefitAmount"
        if code not in {"A", "B", "C", "G"} or entry.get(value_key) is None:
            continue
        procedure = entry.get("compositeMedicalProcedureIdentifier")
        procedure_code = (
            procedure.get("procedureCode") if isinstance(procedure, dict) else None
        )
        date_info = entry.get("benefitsDateInformation")
        output.append(
            QualifiedBenefit(
                code=code,
                value=str(entry[value_key]),
                value_kind="percent" if value_key == "benefitPercent" else "amount",
                service_type_codes=[str(v) for v in entry.get("serviceTypeCodes", [])],
                network_code=(
                    str(entry["inPlanNetworkIndicatorCode"])
                    if entry.get("inPlanNetworkIndicatorCode") is not None
                    else None
                ),
                coverage_level_code=(
                    str(entry["coverageLevelCode"])
                    if entry.get("coverageLevelCode") is not None
                    else None
                ),
                time_qualifier_code=(
                    str(entry["timeQualifierCode"])
                    if entry.get("timeQualifierCode") is not None
                    else None
                ),
                procedure_code=str(procedure_code) if procedure_code else None,
                benefit_dates={
                    str(k): str(v)
                    for k, v in date_info.items()
                    if isinstance(v, (str, int, float))
                }
                if isinstance(date_info, dict)
                else {},
            )
        )
    return output


def _selection_ambiguity(
    benefits: list[QualifiedBenefit], request: EligibilityRequest
) -> Optional[str]:
    """Refuse an answer when explicit benefit qualifiers match no evidence.

    Coverage status entries do not always carry network/coverage/time metadata.
    When the caller declares one of those qualifiers, an unrelated active
    service row must therefore not be enough to produce a consumable answer.
    """
    selection = request.benefit_selection
    requested = {
        "network": selection.network_code,
        "coverage level": selection.coverage_level_code,
        "time period": selection.time_qualifier_code,
        "procedure": selection.procedure_code,
    }
    explicit = {name: value for name, value in requested.items() if value is not None}
    if not explicit:
        return None

    service_date = _date_value(request.date_of_service)
    service_candidates = [
        benefit
        for benefit in benefits
        if any(
            service in benefit.service_type_codes
            for service in request.service_type_codes
        )
    ]
    qualified = [
        benefit
        for benefit in service_candidates
        if (
            selection.network_code is None
            or benefit.network_code in {selection.network_code, "W"}
        )
        and (
            selection.coverage_level_code is None
            or benefit.coverage_level_code == selection.coverage_level_code
        )
        and (
            selection.time_qualifier_code is None
            or benefit.time_qualifier_code == selection.time_qualifier_code
        )
        and (
            selection.procedure_code is None
            or benefit.procedure_code == selection.procedure_code
        )
        and (
            service_date is None
            or not benefit.benefit_dates
            or _covers_service_date(benefit.benefit_dates, service_date) is True
        )
    ]
    if qualified:
        return None
    labels = ", ".join(sorted(explicit))
    return f"no patient-responsibility evidence matches requested {labels} qualifiers"


def _select_value(
    benefits: list[QualifiedBenefit],
    request: EligibilityRequest,
    *,
    code: str,
    time_code: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    selection = request.benefit_selection
    service_date = (
        _date_value(request.date_of_service) if request.date_of_service else None
    )
    candidates = [
        b
        for b in benefits
        if b.code == code
        and any(
            service in b.service_type_codes for service in request.service_type_codes
        )
        and (time_code is None or b.time_qualifier_code == time_code)
        and (
            selection.time_qualifier_code is None
            or time_code is not None
            or b.time_qualifier_code == selection.time_qualifier_code
        )
        and (
            selection.coverage_level_code is None
            or b.coverage_level_code == selection.coverage_level_code
        )
        and (
            selection.procedure_code is None
            or b.procedure_code == selection.procedure_code
        )
        and (
            selection.network_code is None
            or b.network_code in {selection.network_code, "W"}
        )
        and (
            service_date is None
            or not b.benefit_dates
            or _covers_service_date(b.benefit_dates, service_date) is True
        )
    ]
    if selection.network_code is not None:
        exact = [b for b in candidates if b.network_code == selection.network_code]
        if exact:
            candidates = exact
    values = sorted({b.value for b in candidates})
    if len(values) > 1:
        suffix = f"/{time_code}" if time_code else ""
        return None, f"benefit {code}{suffix}: conflicting qualified values"
    return (values[0] if values else None), None


def _base_result(
    request: EligibilityRequest, body_bytes: bytes, http_status: int
) -> dict[str, Any]:
    return {
        "payer_id": request.payer_id,
        "operation_id": request.operation_id,
        "service_type_codes": list(request.service_type_codes),
        "raw_271_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "raw_271_bytes": body_bytes,
        "http_status": http_status,
        "checked_at": _utcnow(),
        "request_sha256": eligibility_request_sha256(request),
    }


def _error_result(
    request: EligibilityRequest,
    body_bytes: bytes,
    *,
    status: EligibilityStatus,
    category: ErrorCategory,
    disposition: RetryDisposition,
    reason: str,
    body: Optional[dict[str, Any]] = None,
    aaa_codes: Optional[list[str]] = None,
    http_status: int,
) -> EligibilityResult:
    return EligibilityResult(
        status=status,
        error_category=category,
        retry_disposition=disposition,
        reason=reason,
        raw_271=body,
        aaa_codes=aaa_codes or [],
        **_base_result(request, body_bytes, http_status),
    )


def parse_271(
    request: EligibilityRequest,
    body_bytes: bytes,
    *,
    http_status: int = 200,
    expected_mode: Optional[ApplicationMode] = None,
) -> EligibilityResult:
    """Normalize one response without ever choosing a first matching benefit."""
    # Transport status is authoritative even when an intermediary returns an
    # HTML/text error body.  In particular, a non-JSON 429 remains a bounded
    # throttle retry rather than being mislabeled as an invalid response.
    if http_status in {401, 403}:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.REJECTED,
            category=ErrorCategory.AUTH_CONFIGURATION,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: HTTP {http_status} authentication/configuration rejection",
            http_status=http_status,
        )
    if http_status == 429:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.PAYER_UNAVAILABLE,
            category=ErrorCategory.THROTTLED,
            disposition=RetryDisposition.RETRY_THEN_PORTAL,
            reason=f"payer {request.payer_id}: HTTP 429 request throttled before processing",
            http_status=http_status,
        )
    if http_status >= 500:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.PAYER_UNAVAILABLE,
            category=ErrorCategory.SERVER_TRANSIENT,
            disposition=RetryDisposition.RETRY_THEN_PORTAL,
            reason=f"payer {request.payer_id}: HTTP {http_status} clearinghouse/server failure",
            http_status=http_status,
        )
    if http_status < 200 or 300 <= http_status < 400:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_INVALID,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: HTTP {http_status} is not a successful response",
            http_status=http_status,
        )
    try:
        decoded = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_INVALID,
            disposition=(
                RetryDisposition.RETRY_THEN_PORTAL
                if http_status >= 500
                else RetryDisposition.NO_RETRY_QUEUE
            ),
            reason=f"payer {request.payer_id}: HTTP {http_status} non-JSON response",
            http_status=http_status,
        )
    if not isinstance(decoded, dict):
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_INVALID,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: response JSON is not an object",
            http_status=http_status,
        )
    body = decoded

    aaa_codes = _collect_aaa_codes(body)
    if http_status == 400 and "79" in aaa_codes:
        category = ErrorCategory.INVALID_PAYER
    elif http_status >= 400:
        category = ErrorCategory.INVALID_REQUEST
    else:
        category = None
    if category is not None:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.REJECTED,
            category=category,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: HTTP {http_status} {category.value}",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )

    response_payer = body.get("tradingPartnerServiceId")
    if response_payer is None or str(response_payer) != request.payer_id:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.INVALID_PAYER,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: response payer ID does not match request binding",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )

    meta = body.get("meta")
    raw_mode = meta.get("applicationMode") if isinstance(meta, dict) else None
    application_mode: Optional[ApplicationMode] = None
    if raw_mode in {m.value for m in ApplicationMode}:
        application_mode = ApplicationMode(raw_mode)
    if application_mode is None or (
        expected_mode is not None and application_mode is not expected_mode
    ):
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.AUTH_CONFIGURATION,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: response application mode does not match account boundary",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )

    if aaa_codes:
        code_set = set(aaa_codes)
        if code_set <= _AAA_TRANSIENT or code_set == {"42", "79"}:
            category = ErrorCategory.PAYER_TRANSIENT
            status = EligibilityStatus.PAYER_UNAVAILABLE
            disposition = RetryDisposition.RETRY_THEN_PORTAL
        elif code_set & _AAA_MEMBER:
            category = ErrorCategory.MEMBER_IDENTITY
            status = EligibilityStatus.NOT_FOUND
            disposition = RetryDisposition.NO_RETRY_QUEUE
        elif code_set & _AAA_PROVIDER:
            category = ErrorCategory.PROVIDER_CONFIGURATION
            status = EligibilityStatus.REJECTED
            disposition = RetryDisposition.NO_RETRY_QUEUE
        elif code_set & _AAA_INVALID_PAYER or code_set == {"79"}:
            category = ErrorCategory.INVALID_PAYER
            status = EligibilityStatus.REJECTED
            disposition = RetryDisposition.NO_RETRY_QUEUE
        else:
            category = ErrorCategory.INVALID_REQUEST
            status = EligibilityStatus.REJECTED
            disposition = RetryDisposition.NO_RETRY_QUEUE
        result = _error_result(
            request,
            body_bytes,
            status=status,
            category=category,
            disposition=disposition,
            reason=f"payer {request.payer_id}: AAA {','.join(aaa_codes)} classified {category.value}",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )
        result.application_mode = application_mode
        return result

    if body.get("error") is not None or body.get("errors"):
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_INVALID,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: response includes an unclassified error",
            body=body,
            http_status=http_status,
        )

    subject_role, subject_problem = _verify_response_subject(body, request)
    if subject_problem is not None:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.MEMBER_IDENTITY,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: {subject_problem}",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )
    assert subject_role == "subscriber"
    provider_problem = _verify_response_provider(body, request)
    if provider_problem is not None:
        return _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.PROVIDER_CONFIGURATION,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: {provider_problem}",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )
    response_subject_sha256 = hashlib.sha256(
        b"openadapt.eligibility.subject.v1\0"
        + eligibility_request_sha256(request).encode("ascii")
        + b"\0"
        + hashlib.sha256(body_bytes).hexdigest().encode("ascii")
        + b"\0subscriber"
    ).hexdigest()

    coverage, problems = _coverage_by_service(body, request)
    statuses = set(coverage.values())
    if (
        problems
        or len(coverage) != len(request.service_type_codes)
        or len(statuses) != 1
    ):
        result = _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_AMBIGUOUS,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: requested-service coverage is incomplete or conflicting",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )
        result.application_mode = application_mode
        result.coverage_by_service = coverage
        result.ambiguities = problems or [
            "requested services have mixed coverage states"
        ]
        return result

    status = next(iter(statuses))
    try:
        benefits = _qualified_benefits(body)
    except (TypeError, ValueError):
        result = _error_result(
            request,
            body_bytes,
            status=EligibilityStatus.INDETERMINATE,
            category=ErrorCategory.RESPONSE_INVALID,
            disposition=RetryDisposition.NO_RETRY_QUEUE,
            reason=f"payer {request.payer_id}: qualified benefit structure is malformed",
            body=body,
            aaa_codes=aaa_codes,
            http_status=http_status,
        )
        result.application_mode = application_mode
        return result
    selections: dict[str, Optional[str]] = {}
    selection_problem = _selection_ambiguity(benefits, request)
    ambiguities: list[str] = [selection_problem] if selection_problem else []
    for field, code, time_code in (
        ("copay", "B", None),
        ("coinsurance_percent", "A", None),
        ("deductible_total", "C", "23"),
        ("deductible_remaining", "C", "29"),
        ("out_of_pocket_total", "G", "23"),
        ("out_of_pocket_remaining", "G", "29"),
    ):
        value, problem = _select_value(
            benefits, request, code=code, time_code=time_code
        )
        selections[field] = value
        if problem:
            ambiguities.append(problem)

    payer = body.get("payer")
    plan = body.get("planInformation")
    dates = body.get("planDateInformation")
    result = EligibilityResult(
        status=status,
        payer_id=request.payer_id,
        operation_id=request.operation_id,
        application_mode=application_mode,
        payer_name=str(payer.get("name"))
        if isinstance(payer, dict) and payer.get("name")
        else None,
        plan_name=(
            str(plan.get("planDescription") or plan.get("groupDescription"))
            if isinstance(plan, dict)
            and (plan.get("planDescription") or plan.get("groupDescription"))
            else None
        ),
        plan_begin=(
            str(dates.get("planBegin") or dates.get("eligibilityBegin"))
            if isinstance(dates, dict)
            and (dates.get("planBegin") or dates.get("eligibilityBegin"))
            else None
        ),
        plan_end=(
            str(dates.get("planEnd") or dates.get("eligibilityEnd"))
            if isinstance(dates, dict)
            and (dates.get("planEnd") or dates.get("eligibilityEnd"))
            else None
        ),
        coverage_by_service=coverage,
        benefits=benefits,
        ambiguities=ambiguities,
        reason=f"payer {request.payer_id}: exact requested-service coverage parsed {status.value}",
        raw_271=body,
        raw_271_sha256=hashlib.sha256(body_bytes).hexdigest(),
        raw_271_bytes=body_bytes,
        response_subject_sha256=response_subject_sha256,
        http_status=http_status,
        checked_at=_utcnow(),
        request_sha256=eligibility_request_sha256(request),
        service_type_codes=list(request.service_type_codes),
        copay=selections["copay"],
        coinsurance_percent=selections["coinsurance_percent"],
        deductible_total=selections["deductible_total"],
        deductible_remaining=selections["deductible_remaining"],
        out_of_pocket_total=selections["out_of_pocket_total"],
        out_of_pocket_remaining=selections["out_of_pocket_remaining"],
    )
    if ambiguities:
        result.error_category = ErrorCategory.RESPONSE_AMBIGUOUS
        result.reason = f"payer {request.payer_id}: coverage parsed but qualified benefit values conflict"
    return result


class StediEligibilityClient:
    """Bounded synchronous Stedi client with explicit account ownership."""

    def __init__(
        self,
        *,
        account: StediAccountBoundary,
        auth: Optional[AuthRef] = None,
        base_url: str = STEDI_ELIGIBILITY_URL,
        timeout_s: float = 30.0,
        max_attempts: int = 3,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_concurrency: int = 4,
        transport: Any = None,
        env: Optional[Mapping[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if base_url != STEDI_ELIGIBILITY_URL:
            raise ValueError(
                "eligibility endpoint is not the allowlisted Stedi endpoint"
            )
        if timeout_s <= 0 or max_attempts < 1 or max_attempts > 5:
            raise ValueError("timeout/max_attempts outside bounded policy")
        if max_response_bytes < 1024 or max_concurrency < 1 or max_concurrency > 32:
            raise ValueError("response/concurrency limit outside bounded policy")
        self.account = account
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.max_response_bytes = max_response_bytes
        self._transport = transport
        self._sleep = sleep
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._headers = self._resolve_headers(auth, env)

    def _resolve_headers(
        self, auth: Optional[AuthRef], env: Optional[Mapping[str, str]]
    ) -> dict[str, str]:
        if auth is not None:
            headers = auth.resolve_headers(env)
        else:
            import os

            source = os.environ if env is None else env
            key = source.get(self.account.credential_env, "")
            if not key:
                raise ValueError(
                    f"practice-held credential environment variable {self.account.credential_env!r} is empty"
                )
            # Current Stedi docs prefer the bare key.  The old ``Key `` prefix
            # is accepted only for backwards compatibility and is not emitted.
            if key.startswith("Key "):
                key = key[4:]
            headers = {"Authorization": key}
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Key "):
            authorization = authorization[4:]
        if not authorization or "\r" in authorization or "\n" in authorization:
            raise ValueError("practice-held Authorization credential is invalid")
        headers["Authorization"] = authorization
        headers.setdefault("Content-Type", "application/json")
        return headers

    def _transport_failure(
        self, request: EligibilityRequest, category: ErrorCategory, reason: str
    ) -> EligibilityResult:
        retryable = category in {
            ErrorCategory.TRANSPORT_TRANSIENT,
            ErrorCategory.THROTTLED,
            ErrorCategory.SERVER_TRANSIENT,
            ErrorCategory.PAYER_TRANSIENT,
        }
        return EligibilityResult(
            status=(
                EligibilityStatus.PAYER_UNAVAILABLE
                if retryable
                else EligibilityStatus.INDETERMINATE
            ),
            payer_id=request.payer_id,
            operation_id=request.operation_id,
            application_mode=self.account.application_mode,
            service_type_codes=list(request.service_type_codes),
            error_category=category,
            retry_disposition=(
                RetryDisposition.RETRY_THEN_PORTAL
                if retryable
                else RetryDisposition.NO_RETRY_QUEUE
            ),
            reason=reason,
            checked_at=_utcnow(),
            request_sha256=eligibility_request_sha256(request),
        )

    def _check_once(self, request: EligibilityRequest) -> EligibilityResult:
        import httpx

        acquired = self._semaphore.acquire(timeout=self.timeout_s)
        if not acquired:
            return self._transport_failure(
                request,
                ErrorCategory.THROTTLED,
                f"payer {request.payer_id}: local eligibility concurrency bound reached",
            )
        try:
            with httpx.Client(
                transport=self._transport, timeout=self.timeout_s
            ) as session:
                try:
                    with session.stream(
                        "POST",
                        self.base_url,
                        headers=self._headers,
                        json=request.to_stedi_body(),
                    ) as response:
                        content_length = response.headers.get("content-length")
                        try:
                            declared_length = (
                                int(content_length) if content_length else None
                            )
                        except ValueError:
                            return self._transport_failure(
                                request,
                                ErrorCategory.RESPONSE_INVALID,
                                f"payer {request.payer_id}: invalid response content-length",
                            )
                        if (
                            declared_length is not None
                            and declared_length > self.max_response_bytes
                        ):
                            return self._transport_failure(
                                request,
                                ErrorCategory.RESPONSE_INVALID,
                                f"payer {request.payer_id}: response exceeds configured byte limit",
                            )
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            payload.extend(chunk)
                            if len(payload) > self.max_response_bytes:
                                return self._transport_failure(
                                    request,
                                    ErrorCategory.RESPONSE_INVALID,
                                    f"payer {request.payer_id}: response exceeds configured byte limit",
                                )
                        return parse_271(
                            request,
                            bytes(payload),
                            http_status=response.status_code,
                            expected_mode=self.account.application_mode,
                        )
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    return self._transport_failure(
                        request,
                        ErrorCategory.TRANSPORT_TRANSIENT,
                        f"payer {request.payer_id}: transient transport {type(exc).__name__}",
                    )
                except httpx.HTTPError as exc:
                    return self._transport_failure(
                        request,
                        ErrorCategory.RESPONSE_INVALID,
                        f"payer {request.payer_id}: HTTP client failure {type(exc).__name__}",
                    )
        finally:
            self._semaphore.release()

    def check(self, request: EligibilityRequest) -> EligibilityResult:
        """Run a bounded retry loop for this idempotent 270 read.

        Stedi's eligibility endpoint does not advertise an idempotency-key
        header.  Retrying is nevertheless side-effect safe because 270 is a
        read; ``operation_id`` binds all attempts to one local logical check.
        """
        result: Optional[EligibilityResult] = None
        for attempt in range(1, self.max_attempts + 1):
            result = self._check_once(request)
            result.attempt_count = attempt
            if not result.retryable or attempt == self.max_attempts:
                return result
            self._sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        assert result is not None
        return result
