"""Shadow-to-Autopilot induction, 4-mutant offline qualification, Micro-Handoff, and Ed25519 receipts."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from openadapt_kernel.ledger import ObligationLedger

AutonomyStage = Literal["SHADOW", "DRAFT_TO_COMMIT", "VERIFIED_AUTOPILOT"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ShadowTrace:
    """One passive observation of a live human workflow completion (zero actuation)."""

    trace_id: str
    portal_origin: str
    source_dossier: Dict[str, str]
    submitted_fields: Dict[str, str]
    readback_url: str
    readback_text: str
    confirmation_ref: Optional[str] = None


@dataclass
class PortalPack:
    """Zero-PII structural pack shared across tenants on the same portal."""

    pack_id: str
    portal_origin: str
    variable_fields: List[str]
    static_defaults: Dict[str, str]
    provenance_map: Dict[str, str]  # target_field -> source_dossier_key
    readback_url_pattern: str
    stage: AutonomyStage = "SHADOW"
    shadow_passes: int = 0
    edge_branches: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class MutantQualificationReport:
    qualified: bool
    pre_write_refuted: bool
    toast_only_refuted: bool
    field_perturbation_refuted: bool
    theme_jitter_verified: bool
    reasons: List[str] = field(default_factory=list)


@dataclass
class MicroHandoffRequest:
    step_id: str
    prompt: str
    staged_count: int
    total_steps: int


class ShadowObserver:
    """Induces zero-PII PortalPacks from passive human runs with no staging DB."""

    @staticmethod
    def induce_portal_pack(pack_id: str, traces: Sequence[ShadowTrace]) -> PortalPack:
        if not traces:
            raise ValueError("At least one ShadowTrace is required to induce a PortalPack")

        origin = traces[0].portal_origin
        all_fields = sorted({k for t in traces for k in t.submitted_fields})
        static_defaults: Dict[str, str] = {}
        variable_fields: List[str] = []
        provenance_map: Dict[str, str] = {}

        for fname in all_fields:
            vals = [t.submitted_fields.get(fname, "") for t in traces]
            if len(traces) >= 2 and len(set(vals)) == 1:
                static_defaults[fname] = vals[0]
            else:
                variable_fields.append(fname)

            # Infer SSA provenance when target field matches a source dossier key across runs
            candidate_sources: Optional[set[str]] = None
            for t in traces:
                val = t.submitted_fields.get(fname, "").strip().lower()
                if not val:
                    continue
                matching_keys = {
                    sk for sk, sv in t.source_dossier.items() if str(sv).strip().lower() == val
                }
                if candidate_sources is None:
                    candidate_sources = matching_keys
                else:
                    candidate_sources &= matching_keys
            if candidate_sources:
                provenance_map[fname] = sorted(candidate_sources)[0]

        readback_url = traces[-1].readback_url.split("?")[0]
        return PortalPack(
            pack_id=pack_id,
            portal_origin=origin,
            variable_fields=variable_fields,
            static_defaults=static_defaults,
            provenance_map=provenance_map,
            readback_url_pattern=readback_url,
            stage="SHADOW",
            shadow_passes=len(traces),
        )


def qualify_verifier_offline(
    *,
    expected_fields: Dict[str, str],
    pre_write_text: str,
    post_write_text: str,
    verifier_fn: Callable[[Dict[str, str], str], bool],
) -> MutantQualificationReport:
    """Run the 4-mutant offline 'Plant-and-Catch' qualification suite (zero live writes).

    Mutant 1 (Pre-Write Frame): must REFUTE.
    Mutant 2 (Toast-Only Splice): pre-write text + fake 'Saved successfully!' toast -> must REFUTE.
    Mutant 3 (Single-Field Perturbation): corrupt one expected field in post-write text -> must REFUTE.
    Mutant 4 (Theme / Whitespace / Codec Jitter): nuisance wrapper/whitespace change -> must VERIFY.
    """
    if not isinstance(pre_write_text, str) or not pre_write_text.strip():
        raise ValueError("pre_write_text must be a non-empty captured pre-write DOM snapshot")
    if not isinstance(post_write_text, str) or not post_write_text.strip():
        raise ValueError("post_write_text must be a non-empty captured post-write DOM snapshot")
    reasons: List[str] = []

    pre_write_refuted = not verifier_fn(expected_fields, pre_write_text)
    if not pre_write_refuted:
        reasons.append("pre_write_frame_falsely_verified")

    toast_text = f"{pre_write_text}\n[Banner] Saved successfully! Changes recorded."
    toast_only_refuted = not verifier_fn(expected_fields, toast_text)
    if not toast_only_refuted:
        reasons.append("toast_only_splice_falsely_verified")

    perturbed_text = post_write_text
    mutated_any = False
    for val in expected_fields.values():
        if val and val in perturbed_text:
            replacement = f"MUTATED_{hashlib.sha256(val.encode('utf-8')).hexdigest()[:8]}"
            perturbed_text = perturbed_text.replace(val, replacement, 1)
            mutated_any = True
            break
    if not mutated_any:
        perturbed_text = ""
    field_perturbation_refuted = not verifier_fn(expected_fields, perturbed_text)
    if not field_perturbation_refuted:
        reasons.append("single_field_perturbation_falsely_verified")

    jitter_text = f"  [theme:dark-v2]\n{post_write_text.replace(' ', '  ')}\n[footer:build-99]  "
    theme_jitter_verified = bool(verifier_fn(expected_fields, jitter_text))
    if not theme_jitter_verified:
        reasons.append("theme_codec_jitter_falsely_refuted")

    qualified = (
        pre_write_refuted
        and toast_only_refuted
        and field_perturbation_refuted
        and theme_jitter_verified
    )
    return MutantQualificationReport(
        qualified=qualified,
        pre_write_refuted=pre_write_refuted,
        toast_only_refuted=toast_only_refuted,
        field_perturbation_refuted=field_perturbation_refuted,
        theme_jitter_verified=theme_jitter_verified,
        reasons=reasons,
    )


class MicroHandoffSession:
    """Suspends execution at a single blocked control and assimilates the human's 1-click fix."""

    def __init__(self, pack: PortalPack, steps: Sequence[Dict[str, Any]]) -> None:
        self.pack = pack
        self.steps = list(steps)
        self.cursor = 0
        self.completed_steps: List[str] = []
        self.pending_handoff: Optional[MicroHandoffRequest] = None

    def run_until_blocked(
        self, executor: Callable[[Dict[str, Any]], bool]
    ) -> Tuple[Literal["COMPLETED", "AWAITING_MICRO_HANDOFF"], Optional[MicroHandoffRequest]]:
        while self.cursor < len(self.steps):
            step = self.steps[self.cursor]
            branch_key = str(step.get("branch_key") or "")
            if branch_key and branch_key in self.pack.edge_branches:
                step = {**step, **self.pack.edge_branches[branch_key]}
                self.steps[self.cursor] = step
            if executor(step):
                self.completed_steps.append(str(step.get("step_id", self.cursor)))
                self.cursor += 1
            else:
                req = MicroHandoffRequest(
                    step_id=str(step.get("step_id", self.cursor)),
                    prompt=str(step.get("prompt") or f"Resolve control for step {self.cursor}"),
                    staged_count=self.cursor,
                    total_steps=len(self.steps),
                )
                self.pending_handoff = req
                return "AWAITING_MICRO_HANDOFF", req
        self.pending_handoff = None
        return "COMPLETED", None

    def resolve_micro_handoff(self, resolution: Dict[str, str]) -> None:
        """Record the operator's 1-click fix, update the PortalPack, and advance past the step."""
        if self.pending_handoff is None:
            raise RuntimeError("No pending micro-handoff to resolve")
        step = self.steps[self.cursor]
        branch_key = str(step.get("branch_key") or step.get("step_id") or self.cursor)
        self.pack.edge_branches[branch_key] = dict(resolution)
        self.steps[self.cursor] = {**step, **resolution}
        self.completed_steps.append(self.pending_handoff.step_id)
        self.cursor += 1
        self.pending_handoff = None


def generate_ephemeral_ed25519_keypair() -> Tuple[bytes, bytes]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


TERMINAL_RECEIPT_OUTCOMES = frozenset(
    {"VERIFIED", "HALTED", "FAILED", "PREPARE_REQUIRED", "ESCALATED", "READY_FOR_HUMAN_COMMIT"}
)


def _origin_host_only(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.hostname:
        return parsed.hostname.lower()
    if parsed.scheme:
        return parsed.scheme.lower()
    return "local" if raw.startswith("/") else raw


def _sha256_hex(val: str) -> str:
    return hashlib.sha256(str(val).encode("utf-8")).hexdigest()


def mint_receipt(
    ledger: ObligationLedger,
    workflow_name: str,
    private_key_pem: bytes,
    *,
    allow_escalation_receipt: bool = False,
    include_plaintext_values: bool = False,
    outcome_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint an Ed25519-signed openadapt.kernel.receipt/v1 artifact (zero-PHI redacted by default)."""
    outcome = outcome_override or ledger.status
    if outcome not in TERMINAL_RECEIPT_OUTCOMES and not allow_escalation_receipt:
        raise ValueError(f"Cannot mint receipt for non-terminal outcome {outcome!r}")

    obligations_payload: Dict[str, Any] = {}
    for oid, ob in ledger.obligations.items():
        if ob.readback_proof is None:
            proof_obj: Optional[Dict[str, Any]] = None
        elif include_plaintext_values:
            proof_obj = asdict(ob.readback_proof)
        else:
            proof_obj = {
                "channel": ob.readback_proof.channel,
                "origin_host": _origin_host_only(ob.readback_proof.url_or_locator),
                "cited_value_sha256": [_sha256_hex(s) for s in ob.readback_proof.cited_substrings],
                "external_ref_sha256": (
                    _sha256_hex(ob.readback_proof.external_ref) if ob.readback_proof.external_ref else None
                ),
                "observed_at_utc": ob.readback_proof.observed_at_utc,
            }
        target_sys = ob.target_system if include_plaintext_values else _origin_host_only(ob.target_system)
        obligations_payload[oid] = {
            "state": ob.state,
            "target_system": target_sys,
            "proof": proof_obj,
        }

    payload = {
        "schema": "openadapt.kernel.receipt/v1",
        "episode_id": ledger.episode_id,
        "workflow_name": workflow_name,
        "outcome": outcome,
        "redacted": not include_plaintext_values,
        "escalation_reason": ledger.escalation_reason,
        "issued_at_utc": _utc_now(),
        "obligations": obligations_payload,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    priv = serialization.load_pem_private_key(private_key_pem, password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    sig = priv.sign(canonical)
    return {
        "payload": payload,
        "sha256": digest,
        "signature_b64": base64.b64encode(sig).decode("ascii"),
    }


def verify_receipt(receipt: Dict[str, Any], public_key_pem: bytes) -> bool:
    """Verify Ed25519 signature and SHA-256 digest offline."""
    try:
        payload = receipt["payload"]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != receipt["sha256"]:
            return False
        pub = serialization.load_pem_public_key(public_key_pem)
        assert isinstance(pub, Ed25519PublicKey)
        pub.verify(base64.b64decode(receipt["signature_b64"]), canonical)
        return True
    except Exception:
        return False
