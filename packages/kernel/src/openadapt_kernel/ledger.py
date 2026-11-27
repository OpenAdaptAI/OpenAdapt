"""Default-deny Obligation, Capability, and Saga Ledger.

Episodes begin UNVERIFIED and transition to VERIFIED only when every required
obligation has an independent post-commit read-back proof and no uncommitted
form fields remain. Timeouts, step-budget exhaustion, crashes, and unverified
exits deterministically resolve to ESCALATED.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from openadapt_kernel.canonical import FieldType

ObligationState = Literal["PENDING", "COMMITTED", "EXEMPT", "ESCALATED"]
EpisodeOutcome = Literal["UNVERIFIED", "VERIFIED", "ESCALATED", "AWAITING_MICRO_HANDOFF", "HALTED_INFRA_FAULT"]
CapabilityKind = Literal["consent", "ssa_literal", "constraint"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_literal(value: str) -> str:
    return " ".join(str(value).strip().split()).lower()


@dataclass
class ReadBackProof:
    channel: Literal["fresh_reload", "cross_tab", "api_read", "sql_read", "pdf_stream", "artifact_file"]
    url_or_locator: str
    cited_substrings: List[str]
    external_ref: Optional[str] = None
    observed_at_utc: str = field(default_factory=_utc_now)

    def is_valid(self) -> bool:
        return bool(self.url_or_locator and any(len(s.strip()) >= 2 for s in self.cited_substrings))


@dataclass
class Obligation:
    obligation_id: str
    description: str
    target_system: str = "default"
    state: ObligationState = "PENDING"
    readback_proof: Optional[ReadBackProof] = None
    exempt_reason: Optional[str] = None
    committed_at_utc: Optional[str] = None
    dependent_on: Optional[str] = None
    is_post_commit: bool = False


@dataclass
class CapabilityToken:
    kind: CapabilityKind
    key: str
    value: str
    source_window: str
    turn: int
    subject_key: Optional[str] = None
    obs_hash: str = ""

    def __post_init__(self) -> None:
        if not self.obs_hash:
            raw = f"{self.kind}|{self.key}|{self.value}|{self.source_window}|{self.subject_key or ''}"
            self.obs_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProvenanceViolation:
    field_name: str
    attempted_value: str
    reason: str


@dataclass
class FinalizationDecision:
    outcome: EpisodeOutcome
    reason: str
    pending_obligations: List[str] = field(default_factory=list)
    dirty_fields: List[str] = field(default_factory=list)
    committed_refs: Dict[str, str] = field(default_factory=dict)


class ObligationLedger:
    """Tracks step-0 obligations, typed capabilities, SSA provenance, and Saga state."""

    def __init__(self, episode_id: str, subject_key: Optional[str] = None) -> None:
        self.episode_id = episode_id
        self.subject_key = subject_key
        self.status: EpisodeOutcome = "UNVERIFIED"
        self.obligations: Dict[str, Obligation] = {}
        self.capabilities: List[CapabilityToken] = []
        self.escalation_reason: str = ""

    # ---- Obligations ----

    def add_obligation(
        self,
        obligation_id: str,
        description: str,
        target_system: str = "default",
        *,
        dependent_on: Optional[str] = None,
        is_post_commit: bool = False,
    ) -> Obligation:
        if dependent_on is not None and dependent_on not in self.obligations:
            raise KeyError(f"Unknown dependent_on obligation_id: {dependent_on}")
        ob = Obligation(
            obligation_id=obligation_id,
            description=description,
            target_system=target_system,
            dependent_on=dependent_on,
            is_post_commit=is_post_commit,
        )
        self.obligations[obligation_id] = ob
        return ob

    def add_post_commit_obligation(
        self,
        obligation_id: str,
        description: str,
        *,
        dependent_on: str,
        target_system: str = "default",
    ) -> Obligation:
        """Declare an ordered post-commit follow-up obligation dependent on a prior obligation."""
        return self.add_obligation(
            obligation_id, description, target_system=target_system, dependent_on=dependent_on, is_post_commit=True
        )

    def mark_committed(self, obligation_id: str, proof: ReadBackProof) -> None:
        if obligation_id not in self.obligations:
            raise KeyError(f"Unknown obligation_id: {obligation_id}")
        if not proof.is_valid():
            raise ValueError(f"ReadBackProof for {obligation_id} lacks locator or verbatim citations")
        ob = self.obligations[obligation_id]
        if ob.dependent_on is not None and not self.is_committed(ob.dependent_on):
            raise ValueError(f"Cannot commit {obligation_id!r} before dependency {ob.dependent_on!r}")
        ob.state = "COMMITTED"
        ob.readback_proof = proof
        ob.committed_at_utc = _utc_now()

    def mark_exempt(self, obligation_id: str, reason: str, proof: ReadBackProof) -> None:
        if obligation_id not in self.obligations:
            raise KeyError(f"Unknown obligation_id: {obligation_id}")
        if not proof.is_valid():
            raise ValueError(f"Exemption for {obligation_id} requires read-back proof")
        ob = self.obligations[obligation_id]
        ob.state = "EXEMPT"
        ob.exempt_reason = reason
        ob.readback_proof = proof
        ob.committed_at_utc = _utc_now()

    def is_committed(self, obligation_id: str) -> bool:
        ob = self.obligations.get(obligation_id)
        return ob is not None and ob.state in ("COMMITTED", "EXEMPT")

    def pending_ids(self) -> List[str]:
        return [oid for oid, ob in self.obligations.items() if ob.state == "PENDING"]

    # ---- Typed Capabilities & Constraints (SEAM + Consent + SSA Provenance) ----

    def record_consent(self, scope: str, user_text: str, turn: int, source_window: str = "user") -> CapabilityToken:
        tok = CapabilityToken(
            kind="consent",
            key=scope,
            value=user_text,
            source_window=source_window,
            turn=turn,
            subject_key=self.subject_key,
        )
        self.capabilities.append(tok)
        return tok

    def record_constraint(
        self,
        key: str,
        warning_text: str,
        source_window: str,
        turn: int,
        subject_key: Optional[str] = None,
    ) -> CapabilityToken:
        tok = CapabilityToken(
            kind="constraint",
            key=key,
            value=warning_text,
            source_window=source_window,
            turn=turn,
            subject_key=subject_key or self.subject_key,
        )
        self.capabilities.append(tok)
        return tok

    def record_ssa_literal(
        self,
        key: str,
        value: str,
        source_window: str,
        turn: int,
        subject_key: Optional[str] = None,
    ) -> CapabilityToken:
        tok = CapabilityToken(
            kind="ssa_literal",
            key=key,
            value=value,
            source_window=source_window,
            turn=turn,
            subject_key=subject_key or self.subject_key,
        )
        self.capabilities.append(tok)
        return tok

    def has_consent(self, scope_substring: str = "") -> bool:
        needle = scope_substring.strip().lower()
        for tok in self.capabilities:
            if tok.kind != "consent":
                continue
            if not needle or needle in tok.key.lower() or needle in tok.value.lower():
                return True
        return False

    def verify_ssa_provenance(
        self,
        field_values: Dict[str, str],
        opaque_fields: Iterable[str],
        target_subject_key: Optional[str] = None,
    ) -> List[ProvenanceViolation]:
        """Verify opaque identifiers carry matching SSA provenance for the active subject."""
        opaque_set = {f.strip().lower() for f in opaque_fields}
        active_subject = target_subject_key or self.subject_key
        violations: List[ProvenanceViolation] = []

        ssa_tokens = [t for t in self.capabilities if t.kind == "ssa_literal"]
        for field_name, val in field_values.items():
            if field_name.strip().lower() not in opaque_set:
                continue
            norm_val = _normalize_literal(val)
            if not norm_val:
                continue
            matches = [t for t in ssa_tokens if _normalize_literal(t.value) == norm_val]
            if not matches:
                violations.append(
                    ProvenanceViolation(
                        field_name=field_name,
                        attempted_value=val,
                        reason="unsourced_opaque_identifier",
                    )
                )
                continue
            if active_subject is not None:
                subject_matches = [t for t in matches if t.subject_key == active_subject]
                if not subject_matches:
                    found_subjects = sorted({str(t.subject_key) for t in matches})
                    violations.append(
                        ProvenanceViolation(
                            field_name=field_name,
                            attempted_value=val,
                            reason=f"cross_subject_bleed(expected={active_subject}, observed={found_subjects})",
                        )
                    )
        return violations

    def render_auditor_context(self) -> Dict[str, Any]:
        """Build zero-history prompt payload for a stateless commit auditor."""
        return {
            "episode_id": self.episode_id,
            "subject_key": self.subject_key,
            "MANDATORY_CONSTRAINTS": [
                {"key": t.key, "warning": t.value, "source": t.source_window, "turn": t.turn}
                for t in self.capabilities
                if t.kind == "constraint"
            ],
            "VERIFIED_CAPABILITIES": {
                "consents": [
                    {"scope": t.key, "user_text": t.value, "turn": t.turn}
                    for t in self.capabilities
                    if t.kind == "consent"
                ],
                "ssa_literals": [
                    {"key": t.key, "value": t.value, "source": t.source_window, "subject": t.subject_key}
                    for t in self.capabilities
                    if t.kind == "ssa_literal"
                ],
            },
            "obligations": {
                oid: {
                    "description": ob.description,
                    "state": ob.state,
                    "external_ref": ob.readback_proof.external_ref if ob.readback_proof else None,
                }
                for oid, ob in self.obligations.items()
            },
        }

    # ---- Default-Deny Finalization ----

    def finalize(
        self,
        trigger: Literal["done", "step_limit", "timeout", "error", "manual_abort"],
        dirty_fields: Sequence[str] = (),
        error_detail: str = "",
    ) -> FinalizationDecision:
        """Apply the default-deny receipting law.

        Only trigger=='done' with zero pending obligations, zero dirty fields,
        and valid read-back proofs on every obligation transitions to VERIFIED.
        """
        dirty = list(dirty_fields)
        pending = self.pending_ids()
        refs = {
            oid: ob.readback_proof.external_ref
            for oid, ob in self.obligations.items()
            if ob.readback_proof and ob.readback_proof.external_ref
        }

        if trigger != "done":
            self.status = "ESCALATED"
            detail = f": {error_detail}" if error_detail else ""
            self.escalation_reason = f"default_deny_{trigger}{detail}"
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=self.escalation_reason,
                pending_obligations=pending,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        if not self.obligations:
            self.status = "ESCALATED"
            self.escalation_reason = "no_declared_obligations_verified"
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=self.escalation_reason,
                pending_obligations=pending,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        if dirty:
            self.status = "ESCALATED"
            self.escalation_reason = f"uncommitted_dirty_fields: {dirty}"
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=self.escalation_reason,
                pending_obligations=pending,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        if pending:
            self.status = "ESCALATED"
            self.escalation_reason = f"unverified_obligations: {pending}"
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=self.escalation_reason,
                pending_obligations=pending,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        unproven = [
            oid
            for oid, ob in self.obligations.items()
            if ob.readback_proof is None or not ob.readback_proof.is_valid()
        ]
        if unproven:
            self.status = "ESCALATED"
            self.escalation_reason = f"missing_readback_proof: {unproven}"
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=self.escalation_reason,
                pending_obligations=unproven,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        self.status = "VERIFIED"
        self.escalation_reason = ""
        return FinalizationDecision(
            outcome="VERIFIED",
            reason="all_obligations_verified_with_readback",
            pending_obligations=[],
            dirty_fields=[],
            committed_refs=refs,
        )

    # ---- Durable Saga Checkpointing ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "openadapt.kernel.saga/v1",
            "episode_id": self.episode_id,
            "subject_key": self.subject_key,
            "status": self.status,
            "escalation_reason": self.escalation_reason,
            "obligations": {oid: asdict(ob) for oid, ob in self.obligations.items()},
            "capabilities": [asdict(tok) for tok in self.capabilities],
        }

    def save_saga(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load_saga(cls, path: Path) -> "ObligationLedger":
        data = json.loads(path.read_text(encoding="utf-8"))
        ledger = cls(episode_id=data["episode_id"], subject_key=data.get("subject_key"))
        ledger.status = data.get("status", "UNVERIFIED")
        ledger.escalation_reason = data.get("escalation_reason", "")
        for oid, ob_d in data.get("obligations", {}).items():
            proof_d = ob_d.get("readback_proof")
            proof = ReadBackProof(**proof_d) if proof_d else None
            ledger.obligations[oid] = Obligation(
                obligation_id=ob_d["obligation_id"],
                description=ob_d["description"],
                target_system=ob_d.get("target_system", "default"),
                state=ob_d["state"],
                readback_proof=proof,
                exempt_reason=ob_d.get("exempt_reason"),
                committed_at_utc=ob_d.get("committed_at_utc"),
                dependent_on=ob_d.get("dependent_on"),
                is_post_commit=bool(ob_d.get("is_post_commit", False)),
            )
        for tok_d in data.get("capabilities", []):
            ledger.capabilities.append(CapabilityToken(**tok_d))
        return ledger


@dataclass(frozen=True)
class SpanAnchor:
    source_key: str
    start: int
    end: int
    quote: str


def verify_span_anchor(source_texts: Mapping[str, str], anchor: SpanAnchor) -> bool:
    """Verify that a SpanAnchor's quote matches source_texts[source_key][start:end] verbatim."""
    raw = source_texts.get(anchor.source_key)
    if raw is None or not anchor.quote:
        return False
    if anchor.start < 0 or anchor.end <= anchor.start or anchor.end > len(raw):
        return False
    return raw[anchor.start : anchor.end] == anchor.quote


def _coerce_span_anchor(raw: Any) -> Optional[SpanAnchor]:
    if isinstance(raw, SpanAnchor):
        return raw
    if isinstance(raw, Mapping) and all(k in raw for k in ("source_key", "start", "end", "quote")):
        try:
            return SpanAnchor(str(raw["source_key"]), int(raw["start"]), int(raw["end"]), str(raw["quote"]))
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class ExtractedFact:
    category: str
    value: str
    source_key: str
    required_in_freetext: bool = True
    span_anchor: Optional[SpanAnchor] = None


@dataclass(frozen=True)
class FactCoverageReport:
    covered: bool
    matched_facts: tuple[str, ...]
    missing_facts: tuple[str, ...]


CriticCalibrationAction = Literal["ALLOW_COMPLETE", "ESCALATE", "PREPARE"]


@dataclass(frozen=True)
class CriticCalibrationVerdict:
    action: CriticCalibrationAction
    overrode_false_escalation: bool
    reason: str
    critic_reason: str = ""


@dataclass
class EpistemicArtifact:
    artifact_id: str
    kind: str
    label: str
    required_before_commit: bool = True
    read: bool = False


@dataclass
class EpistemicReadTracker:
    """Tracks required documents/tabs/attachments that must be read before commit."""

    artifacts: Dict[str, EpistemicArtifact] = field(default_factory=dict)

    def register_artifact(
        self,
        artifact_id: str,
        kind: str,
        label: str,
        *,
        required_before_commit: bool = True,
        read: bool = False,
    ) -> EpistemicArtifact:
        art = EpistemicArtifact(artifact_id, kind, label, required_before_commit=required_before_commit, read=read)
        self.artifacts[artifact_id] = art
        return art

    def mark_read(self, artifact_id: str) -> EpistemicArtifact:
        if artifact_id not in self.artifacts:
            raise KeyError(f"Unknown epistemic artifact: {artifact_id!r}")
        self.artifacts[artifact_id].read = True
        return self.artifacts[artifact_id]

    def unread_required_artifacts(self) -> List[str]:
        return [aid for aid, art in self.artifacts.items() if art.required_before_commit and not art.read]


@dataclass
class ClosureVerdict:
    outcome: EpisodeOutcome
    allowed: bool
    reason: str
    missing_dossier_keys: List[str] = field(default_factory=list)
    uncited_triage_keys: List[str] = field(default_factory=list)
    pending_obligations: List[str] = field(default_factory=list)
    missing_freetext_facts: List[str] = field(default_factory=list)
    unread_epistemic_artifacts: List[str] = field(default_factory=list)
    decision: str = ""

    def __post_init__(self) -> None:
        if not self.decision:
            if self.outcome == "HALTED_INFRA_FAULT":
                self.decision = "HALTED_INFRA_FAULT"
            elif self.allowed and self.outcome == "VERIFIED":
                self.decision = "COMPLETE"
            else:
                self.decision = "ESCALATE"

    @property
    def passed(self) -> bool:
        return self.allowed and self.outcome == "VERIFIED"


_DATE_SCAN_RE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b"
)
_CURRENCY_SCAN_RE = re.compile(
    r"(?:\$\(\s*[\d,]+(?:\.\d{1,2})?\s*\)|\(\s*\$?\s*[\d,]+(?:\.\d{1,2})?\s*\)|[-+]?\s*(?:USD\s*)?\$\s*[\d,]+(?:\.\d{1,2})?|[-+]?\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b|[-+]?\b\d+\.\d{2}\b)"
)
_PHONE_SCAN_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_CODE_SCAN_RE = re.compile(
    r"\b(?:[A-Z]{1,5}[-_](?=[A-Z0-9_-]*\d)[A-Z0-9]{2,}(?:[-_.][A-Z0-9]+)*|[A-Z]\d{2}(?:\.\d{1,4})?)\b"
)
_DEADLINE_SCAN_RE = re.compile(
    r"\b(?:within\s+\d{1,3}\s+(?:(?:calendar|business|working)\s+)?(?:days?|hours?|weeks?|months?)"
    r"|\d{1,3}\s+(?:calendar|business|working)\s+(?:days?|hours?|weeks?|months?)"
    r"|net\s+\d{1,3}|\d{1,3}-day\s+(?:window|limit|period|deadline))\b",
    re.IGNORECASE,
)
_POLICY_CLAUSE_RE = re.compile(
    r"\b(?:(?:do\s+not|must\s+not|cannot\s+be|not\s+eligible|requires?\s+(?:prior\s+authorization|approval|review|escalation)|refer\s+to|escalate\s+to)[^.;\n]{2,80})",
    re.IGNORECASE,
)
_CONTRAST_CLAUSE_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9 _/-]{2,45}\s+rather\s+than\s+(?:a\s+|an\s+|the\s+)?[A-Za-z0-9 _/-]{2,45}"
    r"|(?:is|was|represents?)\s+(?:a|an)\s+[A-Za-z0-9 _/-]{2,40},?\s+not\s+(?:a|an)\s+[A-Za-z0-9 _/-]{2,40})",
    re.IGNORECASE,
)
_ROUTING_CLAUSE_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9 _/-]{2,55}\s+(?:requires?\s+re-?routing|must\s+be\s+re-?routed|must\s+be\s+routed)[^.;\n]{0,60})",
    re.IGNORECASE,
)
_FINDING_CLAUSE_RE = re.compile(
    r"\b(?:shows?|demonstrates?|reveals?|revealed|finding)\s*:?\s+([A-Za-z0-9+][A-Za-z0-9+_., /-]{2,70}?)(?=[.;\n]|$|\s+(?:and\s+requires|which\s+|that\s+))",
    re.IGNORECASE,
)
_LEADING_COPULA_RE = re.compile(r"^(?:this|it|that|the\s+issue|the\s+case)\s+(?:is|was)\s+(?:a|an)\s+", re.IGNORECASE)
_KV_FINDING_RE = re.compile(r"(?:^|[;\n])\s*([A-Za-z][A-Za-z0-9 _/-]{1,28}):\s*([^\n;]{2,80})")
_URL_SCAN_RE = re.compile(r"https?://[^\s)\]>\"']+")
_QUOTED_SCAN_RE = re.compile(r'"([^"]{2,80})"|\'([^\']{2,80})\'')
_NUMBER_SCAN_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")


def _lookup_key(mapping: Mapping[str, Any], key: str) -> Any:
    return mapping[key] if key in mapping else mapping.get(key.lstrip("$"))


def _evidence_cited_in_text(expected: str, summary_text: str, ftype: FieldType) -> bool:
    from openadapt_kernel import canonical as _can

    if not expected or not summary_text:
        return False
    if ftype == "exact":
        return expected in summary_text
    if (norm_exp := _can.normalize_casefold_ws(expected)) and norm_exp in _can.normalize_casefold_ws(summary_text):
        return True
    if (ftype == "date" or ftype == "casefold_ws") and (td := _can.parse_canonical_date(expected)) is not None:
        return any(_can.parse_canonical_date(m.group(0)) == td for m in _DATE_SCAN_RE.finditer(summary_text))
    if ftype == "currency" and (tc := _can.parse_canonical_currency_cents(expected)) is not None:
        return any(_can.parse_canonical_currency_cents(m.group(0)) == tc for m in _CURRENCY_SCAN_RE.finditer(summary_text))
    if ftype == "phone" and (tp := _can.parse_canonical_phone(expected)) is not None:
        return any(_can.parse_canonical_phone(m.group(0)) == tp for m in _PHONE_SCAN_RE.finditer(summary_text))
    if ftype == "code_prefix":
        tok = _can.extract_code_prefix_token(expected)
        return bool(tok and re.search(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", summary_text.upper()))
    if ftype == "masked_id":
        return any(_can.match_masked_id(expected, tok) for tok in re.findall(r"[A-Za-z0-9*\u2022\u25cf\u00b7-]+", summary_text))
    return False


_ATOMIC_FACT_CATEGORIES = frozenset({"code", "date", "currency", "masked_id", "phone", "derived_calc"})
_PROSE_FACT_CATEGORIES = frozenset({"policy_clause", "routing_clause", "kv_finding"})
_PROSE_STOP_WORDS = frozenset(
    "a an the is was are were be been being of to in on for with by at from as that this it its "
    "and or but if then than into onto per via have has had do does did will would should could may might".split()
)


def _infer_fact_category(key: str, fact_categories: Mapping[str, str]) -> str:
    if key in fact_categories:
        return fact_categories[key].strip().lower()
    if ":" in key:
        return key.split(":", 1)[0].strip().lower()
    return ""


def verify_freetext_fact_coverage(
    text: str,
    required_facts: Mapping[str, str],
    *,
    field_types: Optional[Mapping[str, FieldType]] = None,
    fact_categories: Optional[Mapping[str, str]] = None,
    min_content_overlap: float = 0.65,
    entailment_fn: Optional[Callable[[str, str], bool]] = None,
) -> FactCoverageReport:
    """Verify free-text notes cover required facts: strict for atomic values, overlap/entailment for prose."""
    ftypes = field_types or {}
    fcats = fact_categories or {}
    text_tokens = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    matched: List[str] = []
    missing: List[str] = []
    for k, exp in required_facts.items():
        ftype: FieldType = _lookup_key(ftypes, k) or "casefold_ws"
        cat = _infer_fact_category(k, fcats)
        is_atomic = cat in _ATOMIC_FACT_CATEGORIES or ftype in ("exact", "date", "currency", "phone", "masked_id", "code_prefix")
        if _evidence_cited_in_text(exp, text, ftype):
            matched.append(k)
            continue
        if is_atomic:
            missing.append(k)
            continue
        exp_tokens = re.findall(r"[a-z0-9]+", (exp or "").lower())
        if len(exp_tokens) >= 2 and all(tok in text_tokens for tok in exp_tokens):
            matched.append(k)
            continue
        content_words = [t for t in exp_tokens if t not in _PROSE_STOP_WORDS and len(t) >= 2]
        if (cat in _PROSE_FACT_CATEGORIES or len(content_words) >= 3) and content_words:
            overlap = sum(1 for t in content_words if t in text_tokens) / len(content_words)
            if overlap >= min_content_overlap:
                matched.append(k)
                continue
        if entailment_fn is not None and bool(entailment_fn(text, exp)):
            matched.append(k)
            continue
        missing.append(k)
    return FactCoverageReport(not missing, tuple(matched), tuple(missing))


@dataclass(frozen=True)
class DerivedCalculation:
    slot_name: str
    expression: str
    value: Union[int, float, str]
    formatted_value: str
    unit: str = ""
    provenance: str = "derived_calc"


def _safe_add_days(iso_date: Any, days: Any) -> str:
    from openadapt_kernel import canonical as _can

    parsed = _can.parse_canonical_date(str(iso_date))
    if parsed is None:
        raise ValueError(f"invalid_date_for_add_days:{iso_date!r}")
    base = date(parsed[0], parsed[1], parsed[2])
    return (base + timedelta(days=int(days))).isoformat()


def _eval_calc_ast(node: ast.AST, env: Mapping[str, Any]) -> Union[int, float, str]:
    if isinstance(node, ast.Expression):
        return _eval_calc_ast(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
            raise ValueError(f"disallowed_constant:{type(node.value).__name__}")
        return node.value
    if isinstance(node, ast.Name):
        clean = node.id.lstrip("$")
        if node.id in env:
            val = env[node.id]
        elif clean in env:
            val = env[clean]
        elif f"${clean}" in env:
            val = env[f"${clean}"]
        else:
            raise ValueError(f"unknown_variable:{node.id}")
        if isinstance(val, bool) or not isinstance(val, (int, float, str)):
            raise ValueError(f"unsupported_variable_type:{node.id}")
        return val
    if isinstance(node, ast.UnaryOp):
        operand = _eval_calc_ast(node.operand, env)
        if not isinstance(operand, (int, float)):
            raise ValueError("unary_op_requires_number")
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"disallowed_unary_op:{type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _eval_calc_ast(node.left, env)
        right = _eval_calc_ast(node.right, env)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            raise ValueError("binary_op_requires_numbers")
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 100:
                raise ValueError("exponent_out_of_bounds")
            return float(left**right)
        raise ValueError(f"disallowed_bin_op:{type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("disallowed_call_form")
        fname = node.func.id
        args = [_eval_calc_ast(a, env) for a in node.args]
        if fname == "abs" and len(args) == 1 and isinstance(args[0], (int, float)):
            return abs(args[0])
        if fname == "round" and len(args) in (1, 2) and isinstance(args[0], (int, float)):
            return round(args[0]) if len(args) == 1 else round(args[0], int(args[1]))
        if fname == "min" and args and all(isinstance(a, (int, float)) for a in args):
            return min(a for a in args if isinstance(a, (int, float)))
        if fname == "max" and args and all(isinstance(a, (int, float)) for a in args):
            return max(a for a in args if isinstance(a, (int, float)))
        if fname == "add_days" and len(args) == 2:
            return _safe_add_days(args[0], args[1])
        raise ValueError(f"disallowed_function:{fname}")
    raise ValueError(f"disallowed_ast_node:{type(node).__name__}")


def evaluate_derived_calculation(
    slot_name: str,
    expression: str,
    variables: Mapping[str, Any],
    *,
    unit: str = "",
    precision: int = 2,
) -> DerivedCalculation:
    """Evaluate a pure arithmetic or add_days expression via a strict Python AST whitelist."""
    tree = ast.parse((expression or "").strip(), mode="eval")
    val = _eval_calc_ast(tree, variables)
    if isinstance(val, str):
        formatted = val
    elif isinstance(val, int) or (isinstance(val, float) and val.is_integer()):
        formatted = f"{int(val)}{unit}"
    else:
        formatted = f"{round(val, precision):.{precision}f}".rstrip("0").rstrip(".") + unit
    return DerivedCalculation(slot_name, expression, val, formatted, unit=unit, provenance="derived_calc")


def bind_derived_calculation(dossier: Mapping[str, str], calc: DerivedCalculation) -> Dict[str, str]:
    """Return an updated dossier copy containing the derived calculation's formatted value."""
    updated = dict(dossier)
    updated[calc.slot_name] = calc.formatted_value
    return updated


@dataclass(frozen=True)
class QueryViewPreconditionReport:
    passed: bool
    matched_filters: Tuple[str, ...]
    missing_filters: Tuple[str, ...]
    reason: str = ""


def verify_query_view_preconditions(
    required_filters: Mapping[str, str],
    *,
    current_url: str,
    active_filter_chips: Sequence[str] = (),
    active_tab: str = "",
) -> QueryViewPreconditionReport:
    """Verify required task filter predicates appear in active URL query params, filter chips, or active tab."""
    qs_raw = parse_qs(urlparse(current_url or "").query)
    qs_map: Dict[str, set[str]] = {
        k.strip().lower(): {" ".join(v.strip().split()).casefold() for v in vals}
        for k, vals in qs_raw.items()
    }
    chips_norm = [" ".join(str(c).strip().split()).casefold() for c in active_filter_chips if str(c).strip()]
    tab_norm = " ".join(str(active_tab or "").strip().split()).casefold()

    matched: List[str] = []
    missing: List[str] = []
    for fkey, fval in required_filters.items():
        nk = fkey.strip().lower()
        nv = " ".join(str(fval).strip().split()).casefold()
        label = f"{fkey}={fval}"
        in_url = nk in qs_map and nv in qs_map[nk]
        in_tab = nk in ("tab", "view", "section") and bool(tab_norm) and (
            tab_norm == nv or bool(re.search(rf"(?<![a-z0-9]){re.escape(nv)}(?![a-z0-9])", tab_norm))
        )
        in_chips = any(
            chip == nv
            or f"{nk}:{nv}" in chip
            or f"{nk}={nv}" in chip
            or f"{nk}: {nv}" in chip
            or (
                bool(re.search(rf"(?<![a-z0-9]){re.escape(nk)}(?![a-z0-9])", chip))
                and bool(re.search(rf"(?<![a-z0-9]){re.escape(nv)}(?![a-z0-9])", chip))
            )
            for chip in chips_norm
        )
        (matched if (in_url or in_tab or in_chips) else missing).append(label)
    reason = "all_query_view_filters_verified" if not missing else f"missing_query_view_filters:{missing}"
    return QueryViewPreconditionReport(not missing, tuple(matched), tuple(missing), reason)


def extract_dossier_facts(
    source_texts: Mapping[str, str],
    *,
    extra_patterns: Optional[Mapping[str, str]] = None,
    llm_extractor_fn: Optional[Callable[[Mapping[str, str]], Sequence[Mapping[str, Any]]]] = None,
    enable_english_regex_heuristics: bool = True,
    require_llm_span_anchors: bool = False,
) -> tuple[ExtractedFact, ...]:
    """Deterministically extract structured facts and optionally merge span-anchored LLM-extracted facts."""
    from openadapt_kernel import canonical as _can

    facts: List[ExtractedFact] = []
    seen: set[tuple[str, str]] = set()

    def _add(
        category: str,
        value: str,
        source_key: str,
        required: bool = True,
        anchor: Optional[SpanAnchor] = None,
    ) -> None:
        cleaned = " ".join(str(value).strip().split())
        if not cleaned:
            return
        dedup_key = (category, cleaned.casefold())
        if dedup_key not in seen:
            seen.add(dedup_key)
            facts.append(ExtractedFact(category, cleaned, source_key, required, span_anchor=anchor))

    for src_key, raw in source_texts.items():
        text = str(raw or "")
        if not text.strip():
            continue
        for m in _CODE_SCAN_RE.finditer(text):
            _add("code", m.group(0), src_key, True, SpanAnchor(src_key, m.start(), m.end(), m.group(0)))
        for m in _DATE_SCAN_RE.finditer(text):
            if _can.parse_canonical_date(m.group(0)) is not None:
                _add("date", m.group(0), src_key, True, SpanAnchor(src_key, m.start(), m.end(), m.group(0)))
        for m in _CURRENCY_SCAN_RE.finditer(text):
            if _can.parse_canonical_currency_cents(m.group(0)) is not None:
                _add("currency", m.group(0), src_key, True, SpanAnchor(src_key, m.start(), m.end(), m.group(0)))
        if enable_english_regex_heuristics:
            for m in _DEADLINE_SCAN_RE.finditer(text):
                _add("deadline", m.group(0), src_key, True, SpanAnchor(src_key, m.start(), m.end(), m.group(0)))
            for m in _POLICY_CLAUSE_RE.finditer(text):
                _add("policy_clause", m.group(0).rstrip(".,;"), src_key, True)
            for m in _CONTRAST_CLAUSE_RE.finditer(text):
                clause = _LEADING_COPULA_RE.sub("", m.group(1).strip().rstrip(".,;"))
                _add("policy_clause", clause, src_key, True)
            for m in _ROUTING_CLAUSE_RE.finditer(text):
                _add("routing_clause", m.group(1).strip().rstrip(".,;"), src_key, True)
            for m in _FINDING_CLAUSE_RE.finditer(text):
                _add("kv_finding", m.group(1).strip().rstrip(".,;"), src_key, True)
        for m in _KV_FINDING_RE.finditer(text):
            k_part, v_part = m.group(1).strip(), m.group(2).strip().rstrip(".")
            if k_part.lower() in ("http", "https") or v_part.startswith("//"):
                continue
            _add("kv_finding", v_part, f"{src_key}:{k_part}", True)
        if extra_patterns:
            for cat, pat in extra_patterns.items():
                for m in re.finditer(pat, text):
                    _add(cat, m.group(1) if m.groups() else m.group(0), src_key, True)

    if llm_extractor_fn is not None:
        for item in llm_extractor_fn(source_texts) or ():
            if not isinstance(item, Mapping):
                continue
            anchor = _coerce_span_anchor(item.get("span_anchor"))
            if require_llm_span_anchors:
                if anchor is None or not verify_span_anchor(source_texts, anchor):
                    continue
            _add(
                str(item.get("category") or "kv_finding").strip(),
                str(item.get("value") or "").strip(),
                str(item.get("source_key") or (anchor.source_key if anchor else "llm_extracted")).strip(),
                bool(item.get("required_in_freetext", True)),
                anchor=anchor,
            )
    return tuple(facts)


def verify_readonly_answer_provenance(
    answer_text: str,
    observed_spans: Sequence[str],
    observed_urls: Sequence[str] = (),
) -> FactCoverageReport:
    """Verify concrete tokens (URLs, IDs, dates, currencies, numbers, quotes) in a read-only answer are grounded."""
    from openadapt_kernel import canonical as _can

    ans = (answer_text or "").strip()
    if not ans:
        return FactCoverageReport(False, (), ("empty_answer",))

    combined = "\n".join([*(str(s) for s in observed_spans), *(str(u) for u in observed_urls)])
    norm_combined = _can.normalize_casefold_ws(combined)
    norm_urls = [str(u).strip().rstrip(".,;:") for u in observed_urls if str(u).strip()]

    candidates: List[tuple[str, str, FieldType]] = []
    consumed_spans: List[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in consumed_spans)

    for m in _URL_SCAN_RE.finditer(ans):
        url_tok = m.group(0).rstrip(".,;:")
        candidates.append((f"url:{url_tok}", url_tok, "exact"))
        consumed_spans.append(m.span())
    for m in _QUOTED_SCAN_RE.finditer(ans):
        q_tok = (m.group(1) or m.group(2) or "").strip()
        if q_tok and not _overlaps(*m.span()):
            candidates.append((f"quote:{q_tok}", q_tok, "casefold_ws"))
            consumed_spans.append(m.span())
    for m in _CODE_SCAN_RE.finditer(ans):
        if not _overlaps(*m.span()):
            candidates.append((f"code:{m.group(0)}", m.group(0), "code_prefix"))
            consumed_spans.append(m.span())
    for m in _DATE_SCAN_RE.finditer(ans):
        if not _overlaps(*m.span()) and _can.parse_canonical_date(m.group(0)) is not None:
            candidates.append((f"date:{m.group(0)}", m.group(0), "date"))
            consumed_spans.append(m.span())
    for m in _CURRENCY_SCAN_RE.finditer(ans):
        ctok = m.group(0).strip()
        if not _overlaps(*m.span()) and _can.parse_canonical_currency_cents(ctok) is not None:
            candidates.append((f"currency:{ctok}", ctok, "currency"))
            consumed_spans.append(m.span())
    for m in _NUMBER_SCAN_RE.finditer(ans):
        if not _overlaps(*m.span()):
            candidates.append((f"number:{m.group(0)}", m.group(0), "exact"))

    matched: List[str] = []
    missing: List[str] = []
    seen_cands: set[str] = set()
    for label, tok, ftype in candidates:
        if label in seen_cands:
            continue
        seen_cands.add(label)
        if label.startswith("url:"):
            ok = any(tok == u or tok in u or u in tok for u in norm_urls) or tok in combined
        elif label.startswith("number:"):
            ok = bool(re.search(rf"(?<![0-9.]){re.escape(tok)}(?![0-9.])", combined))
        elif ftype == "casefold_ws":
            ok = _can.normalize_casefold_ws(tok) in norm_combined
        else:
            ok = _evidence_cited_in_text(tok, combined, ftype)
        (matched if ok else missing).append(label)
    return FactCoverageReport(not missing, tuple(matched), tuple(missing))


def calibrate_critic_decision(
    closure_report: ClosureVerdict,
    *,
    critic_vetoed: bool,
    critic_reason: str = "",
    policy_requires_human: bool = False,
    require_extracted_facts: bool = False,
    extracted_facts_count: int = 0,
    untemplated_freetext: bool = False,
    ood_case: bool = False,
) -> CriticCalibrationVerdict:
    """Calibrate an LLM critic veto against deterministic closure verification and human-policy gates."""
    if policy_requires_human:
        return CriticCalibrationVerdict("PREPARE", False, "policy_requires_human", critic_reason)
    if not closure_report.passed:
        return CriticCalibrationVerdict(
            "ESCALATE", False, f"closure_not_verified:{closure_report.reason}", critic_reason
        )
    if critic_vetoed:
        if untemplated_freetext or ood_case:
            return CriticCalibrationVerdict(
                "ESCALATE", False, "preserved_critic_veto_on_untemplated_or_ood_prose", critic_reason
            )
        if require_extracted_facts and extracted_facts_count <= 0:
            return CriticCalibrationVerdict(
                "ESCALATE", False, "unextracted_facts_preserve_critic_veto", critic_reason
            )
        return CriticCalibrationVerdict(
            "ALLOW_COMPLETE", True, "overrode_conservative_critic_on_verified_closure", critic_reason
        )
    return CriticCalibrationVerdict("ALLOW_COMPLETE", False, "closure_and_critic_agreed", critic_reason)


def verify_completion_closure(
    ledger: ObligationLedger, *, step_idx: int, max_steps: int, required_dossier_keys: Sequence[str] = (),
    readback_values: Optional[Mapping[str, str]] = None, dossier_values: Optional[Mapping[str, str]] = None,
    field_types: Optional[Mapping[str, FieldType]] = None, triage_summary_text: str = "",
    required_triage_evidence: Optional[Mapping[str, str]] = None,
    post_commit_obligations: Sequence[str] = (),
    freetext_notes: str = "",
    required_freetext_facts: Optional[Mapping[str, str]] = None,
    extracted_facts: Sequence[ExtractedFact] = (),
    min_content_overlap: float = 0.65,
    entailment_fn: Optional[Callable[[str, str], bool]] = None,
    epistemic_artifacts: Sequence[EpistemicArtifact] = (),
    require_positive_oracle_proof: bool = False,
    has_independent_oracle_proof: bool = True,
    infra_fault: str = "",
) -> ClosureVerdict:
    """Enforce closed-world Simplex invariants: infra fault isolation, epistemic reads, oracle proof, and closure."""
    from openadapt_kernel.canonical import canonical_equal

    if (fault := str(infra_fault or "").strip()):
        ledger.status, ledger.escalation_reason = "HALTED_INFRA_FAULT", f"infra_fault:{fault}"
        return ClosureVerdict("HALTED_INFRA_FAULT", False, f"infra_fault:{fault}", decision="HALTED_INFRA_FAULT")

    unread_art = [a.artifact_id for a in epistemic_artifacts if a.required_before_commit and not a.read]
    if unread_art:
        ledger.status, ledger.escalation_reason = "ESCALATED", f"unread_epistemic_artifacts: {unread_art}"
        return ClosureVerdict(
            "ESCALATED", False, "unread_epistemic_artifacts", unread_epistemic_artifacts=unread_art, decision="ESCALATE"
        )

    if require_positive_oracle_proof and not has_independent_oracle_proof:
        ledger.status, ledger.escalation_reason = "ESCALATED", "no_independent_oracle_proof"
        return ClosureVerdict("ESCALATED", False, "no_independent_oracle_proof", decision="PREPARE_REQUIRED")

    rb, dos, ftypes, triage_req = readback_values or {}, dossier_values or {}, dict(field_types or {}), required_triage_evidence or {}
    pending = ledger.pending_ids()
    unproven = [oid for oid, ob in ledger.obligations.items() if ob.readback_proof is None or not ob.readback_proof.is_valid()]
    unverified_post = [
        oid for oid in post_commit_obligations
        if oid not in ledger.obligations or not ledger.is_committed(oid)
        or (proof := ledger.obligations[oid].readback_proof) is None or not proof.is_valid()
    ]
    all_obs_verified = bool(ledger.obligations) and not pending and not unproven and not unverified_post
    uncited = [
        k for k, exp in triage_req.items()
        if not _evidence_cited_in_text(exp, triage_summary_text, _lookup_key(ftypes, k) or "casefold_ws")
    ]
    if step_idx >= max_steps and ((ledger.obligations and not all_obs_verified) or (not ledger.obligations and (not triage_req or bool(uncited)))):
        ledger.status, ledger.escalation_reason = "ESCALATED", f"default_deny_step_limit: step_idx={step_idx}>=max_steps={max_steps}"
        return ClosureVerdict("ESCALATED", False, "default_deny_step_limit", uncited_triage_keys=uncited, pending_obligations=pending or unproven or unverified_post)
    if unverified_post:
        ledger.status, ledger.escalation_reason = "ESCALATED", f"unverified_post_commit_obligations: {unverified_post}"
        return ClosureVerdict("ESCALATED", False, "unverified_post_commit_obligations", pending_obligations=unverified_post)
    if ledger.obligations and not all_obs_verified:
        ledger.status, ledger.escalation_reason = "ESCALATED", f"unverified_obligations: {pending or unproven}"
        return ClosureVerdict("ESCALATED", False, "unverified_obligations", pending_obligations=pending or unproven)
    if not ledger.obligations and not triage_req:
        ledger.status, ledger.escalation_reason = "ESCALATED", "no_declared_obligations_verified"
        return ClosureVerdict("ESCALATED", False, "no_declared_obligations_verified")
    missing_keys = [
        key for key in required_dossier_keys
        if (exp := _lookup_key(dos, key)) is None
        or (obs := _lookup_key(rb, key)) is None
        or not canonical_equal(exp, obs, _lookup_key(ftypes, key) or "casefold_ws")
    ]
    if missing_keys:
        ledger.status, ledger.escalation_reason = "ESCALATED", f"missing_required_dossier_keys: {missing_keys}"
        return ClosureVerdict("ESCALATED", False, "missing_required_dossier_keys", missing_dossier_keys=missing_keys)
    if uncited:
        ledger.status, ledger.escalation_reason = "ESCALATED", f"uncited_triage_evidence: {uncited}"
        return ClosureVerdict("ESCALATED", False, "uncited_triage_evidence", uncited_triage_keys=uncited)

    merged_freetext_facts: Dict[str, str] = dict(required_freetext_facts or {})
    cat_to_type: Dict[str, FieldType] = {"date": "date", "currency": "currency", "code": "code_prefix", "derived_calc": "exact"}
    for idx, ef in enumerate(extracted_facts):
        if ef.required_in_freetext:
            fkey = f"{ef.category}:{ef.source_key}:{idx}"
            merged_freetext_facts[fkey] = ef.value
            if ef.category in cat_to_type:
                ftypes.setdefault(fkey, cat_to_type[ef.category])
    if merged_freetext_facts:
        cov = verify_freetext_fact_coverage(
            freetext_notes,
            merged_freetext_facts,
            field_types=ftypes,
            min_content_overlap=min_content_overlap,
            entailment_fn=entailment_fn,
        )
        if not cov.covered:
            ledger.status, ledger.escalation_reason = "ESCALATED", f"missing_freetext_facts: {list(cov.missing_facts)}"
            return ClosureVerdict("ESCALATED", False, "missing_freetext_facts", missing_freetext_facts=list(cov.missing_facts))
    ledger.status, ledger.escalation_reason = "VERIFIED", ""
    return ClosureVerdict("VERIFIED", True, "closure_verified")
