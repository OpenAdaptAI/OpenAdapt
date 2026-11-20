"""Shared, app-agnostic data contracts for the Transactional Commit Layer (tx).

Zero vision/GUI imports. Zero application names. All per-app behavior lives in
declarative ``EntityLensSpec`` / ``CertifiedLensPack`` data structures.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any, Literal, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

ENTITY_LENS_SCHEMA = "openadapt.entity-lens/v1"
LENS_PROBE_REPORT_SCHEMA = "openadapt.lens-probe-report/v1"
CERTIFIED_LENS_PACK_SCHEMA = LENS_PROBE_REPORT_SCHEMA
TX_RECEIPT_SCHEMA = "openadapt.tx-receipt/v3"

TxOutcome = Literal["CONFIRMED", "REFUTED", "UNCERTAIN", "BLOCKED_PRECOMMIT"]
OCCStatus = Literal[
    "UNCONTESTED",
    "CONCURRENT_MODIFICATION",
    "NOT_APPLICABLE",
    "UNKNOWN",
]
WriteMode = Literal["create", "patch", "replace"]
CodecKind = Literal[
    "exact",
    "casefold",
    "upper",
    "lower",
    "capitalize_words",
    "strip_whitespace",
    "phone_digits",
    "iso_date",
    "bijection",
]
JudgeTier = Literal[
    "exact",
    "codec",
    "entailment_oracle",
    "frame_eq",
    "volatile_skip",
    "failed",
]


def canonical_sha256(obj: Any) -> str:
    """Deterministic SHA-256 hex digest of JSON-serializable data."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def salted_sha256(salt_bytes: bytes, obj: Any) -> str:
    """HMAC-style salted SHA-256 hex digest so public receipts never leak raw PII/PHI."""
    import hmac

    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(salt_bytes, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def open_exact_http(request: Any, *, timeout: float) -> Any:
    """No redirects: response attribution and credentials stay at the declared endpoint."""
    import urllib.request

    return urllib.request.build_opener(_RefuseRedirects()).open(
        request, timeout=timeout
    )


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldCodec(_FrozenModel):
    """Declarative normalization or bijective value map for an intended field."""

    kind: CodecKind = "exact"
    mapping: dict[str, str] = Field(default_factory=dict)


class WireMutationRecord(_FrozenModel):
    """Immutable record of a single state-mutating request observed on the wire."""

    seq: int
    method: str
    url: str
    path: str
    request_body_sha256: str
    response_status: Optional[int] = None
    response_body_sha256: Optional[str] = None
    extracted_entity_id: Optional[str] = None
    etag: Optional[str] = None
    version: Optional[str] = None
    blocked: bool = False
    block_reason: str = ""
    is_graphql_mutation: bool = False
    timestamp_utc: str = ""


class WireSessionSummary(_FrozenModel):
    """O(1) session-scoped write completeness summary from SessionWireTap."""

    session_id: str
    total_requests: int = 0
    read_requests: int = 0
    mutations_allowed: int = 0
    mutations_blocked: int = 0
    collateral_mutations: int = 0
    mutations: tuple[WireMutationRecord, ...] = ()
    ledger_sha256: str = ""


class FieldVerdict(_FrozenModel):
    """Verification outcome for one field path in R_0 / R_1."""

    field_path: str
    category: Literal["intended", "frame", "volatile"]
    passed: bool
    tier_used: JudgeTier
    reason: str = ""


class JudgeVerdict(_FrozenModel):
    """Two-law verification result (Pierce Lens Frame Preservation + Intent Entailment)."""

    passed: bool
    frame_preserved: bool
    intent_satisfied: bool
    clobbered_fields: tuple[str, ...] = ()
    mismatched_fields: tuple[str, ...] = ()
    extra_fields: tuple[str, ...] = ()
    field_verdicts: tuple[FieldVerdict, ...] = ()
    reasons: tuple[str, ...] = ()


@runtime_checkable
class EntailmentOracle(Protocol):
    """Optional semantic/model judge for non-identical intended field representations."""

    def entails(
        self,
        field_path: str,
        intended_value: Any,
        stored_value: Any,
        before_value: Any = None,
    ) -> tuple[bool, str]: ...


@runtime_checkable
class JudgeProtocol(Protocol):
    """Protocol implemented by FrameAndIntentJudge in tx.judge."""

    def evaluate(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_delta: Mapping[str, Any],
        *,
        volatile_fields: tuple[str, ...] = (),
        field_codecs: Optional[Mapping[str, FieldCodec]] = None,
    ) -> JudgeVerdict: ...


@runtime_checkable
class WireTapProtocol(Protocol):
    """Protocol implemented by SessionWireTap in tx.wire_tap."""

    @property
    def base_url(self) -> str: ...

    def reset_session(self, session_id: Optional[str] = None) -> str: ...

    def summary(self) -> WireSessionSummary: ...


class EndpointTemplate(_FrozenModel):
    """HTTP endpoint template for O(1) entity reads, writes, or duplicate checks."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path_template: str
    query_params: dict[str, str] = Field(default_factory=dict)
    records_key: Optional[str] = None
    id_field: str = "id"
    etag_header: Optional[str] = "ETag"
    version_field: Optional[str] = None
    author_field: Optional[str] = None


class EntityLensSpec(_FrozenModel):
    """Declarative O(1) read-merge-write-verify specification for one entity type."""

    schema_version: str = ENTITY_LENS_SCHEMA
    entity_name: str
    base_url: str
    id_field: str = "id"
    write_mode: WriteMode = "patch"
    get_one: EndpointTemplate
    write_endpoint: EndpointTemplate
    create_endpoint: Optional[EndpointTemplate] = None
    duplicate_search: Optional[EndpointTemplate] = None
    delete_endpoint: Optional[EndpointTemplate] = None
    identity_fields: tuple[str, ...] = ()
    volatile_fields: tuple[str, ...] = (
        "updated_at",
        "updatedAt",
        "modified_at",
        "lastModified",
        "meta.lastUpdated",
        "meta.versionId",
        "etag",
        "version",
    )
    create_defaults: tuple[str, ...] = ()
    field_codecs: dict[str, FieldCodec] = Field(default_factory=dict)
    duplicate_keys: tuple[str, ...] = ()
    expected_author: Optional[str] = None


def effective_volatile_fields(lens: EntityLensSpec) -> tuple[str, ...]:
    """Use only metadata fields explicitly declared by the lens, consistently."""
    return tuple(
        dict.fromkeys(
            (
                *lens.volatile_fields,
                *(
                    field
                    for endpoint in (lens.get_one, lens.write_endpoint)
                    for field in (endpoint.version_field, endpoint.author_field)
                    if field
                ),
            )
        )
    )


class TxTask(_FrozenModel):
    """Authorized write intent supplied by the caller/orchestrator (never the agent)."""

    task_id: str
    operation: Literal["create", "update"]
    entity_id: Optional[str] = None
    expected_identity: dict[str, Any] = Field(default_factory=dict)
    intended_delta: dict[str, Any] = Field(default_factory=dict)
    actor_id: str = "agent"


class TxCommitResult(_FrozenModel):
    """Final outcome of a transactional create or update through TransactionalCommitEngine."""

    task_id: str
    outcome: TxOutcome
    occ_status: OCCStatus = "NOT_APPLICABLE"
    entity_id: Optional[str] = None
    wire_summary: Optional[WireSessionSummary] = None
    judge_verdict: Optional[JudgeVerdict] = None
    reasons: tuple[str, ...] = ()
    can_compensate: bool = False
    verification_scope: Literal["target_record"] = "target_record"
    collateral_effects_verified: Literal[False] = False
    qualification_state: Literal["unqualified", "atomic_row_enforced"] = "unqualified"
    receipt_path: Optional[str] = None
    receipt_id: Optional[str] = None
    rows_read: int = 0
    duration_ms: float = 0.0


class CertificationProbeResult(_FrozenModel):
    """Outcome of one mechanical fault or concurrency probe during Lens certification."""

    probe_name: str
    polarity: Literal["harmful", "benign"]
    expected_reject: bool
    observed_reject: bool
    passed: bool
    detail: str = ""


class LensProbeReport(_FrozenModel):
    """Unqualified observations from bounded probes; never an execution authority."""

    schema_version: str = CERTIFIED_LENS_PACK_SCHEMA
    lens: EntityLensSpec
    certified: Literal[False] = False
    probes_passed: bool = False
    sensitivity: float
    specificity: float
    probes: tuple[CertificationProbeResult, ...] = ()
    pack_sha256: str = ""

    def admission_errors(self) -> tuple[str, ...]:
        return ("unqualified_probe_report", *self.probe_integrity_errors())

    def probe_integrity_errors(self) -> tuple[str, ...]:
        """Check observation consistency without conferring execution authority."""
        expected = {
            "clean_roundtrip": "benign",
            "benign_concurrent_other_record": "benign",
            "fault_partial_clobber": "harmful",
            "fault_wrong_value": "harmful",
            "fault_wrong_record": "harmful",
            "fault_no_persist": "harmful",
        }
        errors: list[str] = []
        if not self.probes_passed or self.sensitivity != 1.0 or self.specificity != 1.0:
            errors.append("uncertified_probe_results")
        if len(self.probes) != len(expected) or {
            p.probe_name for p in self.probes
        } != set(expected):
            errors.append("incomplete_probe_set")
        for probe in self.probes:
            polarity = expected.get(probe.probe_name)
            should_reject = polarity == "harmful"
            if (
                probe.polarity != polarity
                or probe.expected_reject != should_reject
                or probe.observed_reject != should_reject
                or not probe.passed
            ):
                errors.append("failed_or_inconsistent_probe")
        if self.pack_sha256 != self.with_computed_digest().pack_sha256:
            errors.append("pack_digest_mismatch")
        return tuple(dict.fromkeys(errors))

    def with_computed_digest(self) -> "LensProbeReport":
        data = self.model_dump(exclude={"pack_sha256"})
        digest = canonical_sha256(data)
        return self.model_copy(update={"pack_sha256": digest})


# Source compatibility only: the legacy name cannot carry certified=True.
CertifiedLensPack = LensProbeReport


def check_mapping_subset(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    """Helper for exact identity subset checks."""
    return all(actual.get(k) == v for k, v in expected.items())
