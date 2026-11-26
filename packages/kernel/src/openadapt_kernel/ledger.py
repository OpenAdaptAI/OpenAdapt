"""Default-deny Obligation, Capability, and Saga Ledger.

Episodes begin UNVERIFIED and transition to VERIFIED only when every required
obligation has an independent post-commit read-back proof and no uncommitted
form fields remain. Timeouts, step-budget exhaustion, crashes, and unverified
exits deterministically resolve to ESCALATED.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

ObligationState = Literal["PENDING", "COMMITTED", "EXEMPT", "ESCALATED"]
EpisodeOutcome = Literal[
    "UNVERIFIED",
    "VERIFIED",
    "ESCALATED",
    "HALTED",
    "FAILED",
    "PREPARE_REQUIRED",
    "AWAITING_MICRO_HANDOFF",
]
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

    def __init__(self, episode_id: Optional[str] = None, subject_key: Optional[str] = None) -> None:
        self.episode_id = episode_id if (episode_id and episode_id.strip()) else f"ep-{uuid.uuid4()}"
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

        def _escalate(reason: str, pending_ids: Optional[List[str]] = None) -> FinalizationDecision:
            self.status = "ESCALATED"
            self.escalation_reason = reason
            return FinalizationDecision(
                outcome="ESCALATED",
                reason=reason,
                pending_obligations=pending if pending_ids is None else pending_ids,
                dirty_fields=dirty,
                committed_refs=refs,
            )

        if trigger != "done":
            detail = f": {error_detail}" if error_detail else ""
            return _escalate(f"default_deny_{trigger}{detail}")
        if not self.obligations:
            return _escalate("no_declared_obligations_verified")
        if dirty:
            return _escalate(f"uncommitted_dirty_fields: {dirty}")
        if pending:
            return _escalate(f"unverified_obligations: {pending}")
        unproven = [
            oid for oid, ob in self.obligations.items()
            if ob.readback_proof is None or not ob.readback_proof.is_valid()
        ]
        if unproven:
            return _escalate(f"missing_readback_proof: {unproven}", pending_ids=unproven)

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
