"""Default-deny Obligation, Capability, and Saga Ledger.

Episodes begin UNVERIFIED and transition to VERIFIED only when every required
obligation has an independent post-commit read-back proof and no uncommitted
form fields remain. Timeouts, step-budget exhaustion, crashes, and unverified
exits deterministically resolve to ESCALATED.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from openadapt_kernel.canonical import FieldType

ObligationState = Literal["PENDING", "COMMITTED", "EXEMPT", "ESCALATED"]
EpisodeOutcome = Literal["UNVERIFIED", "VERIFIED", "ESCALATED", "AWAITING_MICRO_HANDOFF"]
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
    ) -> Obligation:
        ob = Obligation(
            obligation_id=obligation_id,
            description=description,
            target_system=target_system,
        )
        self.obligations[obligation_id] = ob
        return ob

    def mark_committed(self, obligation_id: str, proof: ReadBackProof) -> None:
        if obligation_id not in self.obligations:
            raise KeyError(f"Unknown obligation_id: {obligation_id}")
        if not proof.is_valid():
            raise ValueError(f"ReadBackProof for {obligation_id} lacks locator or verbatim citations")
        ob = self.obligations[obligation_id]
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
            )
        for tok_d in data.get("capabilities", []):
            ledger.capabilities.append(CapabilityToken(**tok_d))
        return ledger


@dataclass
class ClosureVerdict:
    outcome: EpisodeOutcome
    allowed: bool
    reason: str
    missing_dossier_keys: List[str] = field(default_factory=list)
    uncited_triage_keys: List[str] = field(default_factory=list)
    pending_obligations: List[str] = field(default_factory=list)


_DATE_SCAN_RE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b"
)
_CURRENCY_SCAN_RE = re.compile(
    r"(?:\$\(\s*[\d,]+(?:\.\d{1,2})?\s*\)|\(\s*\$?\s*[\d,]+(?:\.\d{1,2})?\s*\)|[-+]?\s*(?:USD\s*)?\$\s*[\d,]+(?:\.\d{1,2})?|[-+]?\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b|[-+]?\b\d+\.\d{2}\b)"
)
_PHONE_SCAN_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")


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


def verify_completion_closure(
    ledger: ObligationLedger, *, step_idx: int, max_steps: int, required_dossier_keys: Sequence[str] = (),
    readback_values: Optional[Mapping[str, str]] = None, dossier_values: Optional[Mapping[str, str]] = None,
    field_types: Optional[Mapping[str, FieldType]] = None, triage_summary_text: str = "",
    required_triage_evidence: Optional[Mapping[str, str]] = None,
) -> ClosureVerdict:
    """Enforce non-waivable step-limit default-deny, dossier key coverage, and triage citation."""
    from openadapt_kernel.canonical import canonical_equal

    rb, dos, ftypes, triage_req = readback_values or {}, dossier_values or {}, field_types or {}, required_triage_evidence or {}
    pending = ledger.pending_ids()
    unproven = [oid for oid, ob in ledger.obligations.items() if ob.readback_proof is None or not ob.readback_proof.is_valid()]
    all_obs_verified = bool(ledger.obligations) and not pending and not unproven
    uncited = [
        k for k, exp in triage_req.items()
        if not _evidence_cited_in_text(exp, triage_summary_text, _lookup_key(ftypes, k) or "casefold_ws")
    ]
    if step_idx >= max_steps and ((ledger.obligations and not all_obs_verified) or (not ledger.obligations and (not triage_req or bool(uncited)))):
        ledger.status, ledger.escalation_reason = "ESCALATED", f"default_deny_step_limit: step_idx={step_idx}>=max_steps={max_steps}"
        return ClosureVerdict("ESCALATED", False, "default_deny_step_limit", uncited_triage_keys=uncited, pending_obligations=pending or unproven)
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
    ledger.status, ledger.escalation_reason = "VERIFIED", ""
    return ClosureVerdict("VERIFIED", True, "closure_verified")
