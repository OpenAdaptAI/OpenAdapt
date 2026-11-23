"""3-layer surgical selector healing and signed, tenant-scoped candidate patches.

When a compiled workflow encounters a broken locator on a drifted UI, this
module provides a deterministic, fail-closed alternative to falling back to a
full multi-step agent loop:

1. **Identity-preserving surgical ranking:** Rank visible, enabled DOM/ARIA
   candidates using deterministic semantic signals. An accessible-name change,
   ARIA-role mismatch, or input-type mismatch is treated as a **lost identity
   anchor** (fail closed to ``ABSTAIN_HALT``) rather than fuzzy-matched onto a
   near-miss sibling field.
2. **Fail-closed score & margin gate:** Accept a candidate only when all
   identity anchors hold, ``top_score >= min_score``, and
   ``top_score - second_score >= min_margin``; otherwise return
   ``ABSTAIN_HALT`` so an operator can intervene.
3. **Ed25519-signed, tenant-scoped candidate patches:** Emit a signed
   :class:`FleetSelectorPatch` (bound to ``tenant_id``, ``program_id``,
   ``program_sha256``, ``app_origin``, and ``workflow_id``) only after an
   independent read-back oracle confirms the healed step succeeded. Applying a
   patch verifies the Ed25519 signature against a pinned public key and emits a
   :class:`CandidatePatchedProgram` marked ``PENDING_QUALIFICATION`` so the
   changed program must pass qualification and admission before running.
"""

from __future__ import annotations

import base64
import copy
import difflib
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

FLEET_PATCH_SCHEMA_VERSION = "openadapt.fleet-selector-patch/v2"

HealVerdict = Literal["HEALED", "ABSTAIN_HALT"]

VisualLocatorCallback = Callable[["ElementTarget", "SurgicalHealCandidate"], float]
PreActionIdentityCheck = Callable[["ElementTarget", "SurgicalHealCandidate"], bool]
ReadbackOracle = Callable[["SurgicalHealCandidate"], bool | tuple[bool, str]]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Deterministic semantic signal weights (renormalized over signals present on
# the target so optional hints never penalize a well-specified target).
_SIGNAL_WEIGHTS: dict[str, float] = {
    "aria_role": 0.30,
    "accessible_name": 0.40,
    "form_section": 0.15,
    "input_type": 0.15,
}


class FleetPatchError(ValueError):
    """Raised when a fleet selector patch fails signature, scope, or step checks."""


def _normalize_text(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _text_similarity(expected: str, observed: str) -> float:
    norm_exp = _normalize_text(expected)
    norm_obs = _normalize_text(observed)
    if not norm_exp or not norm_obs:
        return 0.0
    if norm_exp == norm_obs:
        return 1.0
    exp_tokens = set(norm_exp.split())
    obs_tokens = set(norm_obs.split())
    jaccard = len(exp_tokens & obs_tokens) / len(exp_tokens | obs_tokens)
    seq_ratio = difflib.SequenceMatcher(None, norm_exp, norm_obs).ratio()
    return 0.5 * jaccard + 0.5 * seq_ratio


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _canonical_patch_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def compute_program_sha256(steps: Sequence[Mapping[str, Any]]) -> str:
    """Compute a deterministic SHA-256 over a compiled program's step list."""
    canonical = json.dumps(
        [dict(step) for step in steps],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class PatchScope:
    """Tenant, program, and origin scope bound into a signed selector patch."""

    tenant_id: str
    program_id: str
    program_sha256: str
    app_origin: str
    workflow_id: str

    def validate(self) -> None:
        for field_name, field_val in (
            ("tenant_id", self.tenant_id),
            ("program_id", self.program_id),
            ("program_sha256", self.program_sha256),
            ("app_origin", self.app_origin),
            ("workflow_id", self.workflow_id),
        ):
            if not isinstance(field_val, str) or not field_val.strip():
                raise FleetPatchError(
                    f"PatchScope.{field_name} must be a non-empty string"
                )


@dataclass(frozen=True)
class ElementTarget:
    """Semantic specification of a broken compiled-workflow step locator."""

    workflow_id: str
    step_id: str
    old_selector: str
    aria_role: str = ""
    accessible_name: str = ""
    form_section: str = ""
    input_type: str = ""
    extra_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class SurgicalHealCandidate:
    """One candidate DOM/ARIA element observed on the drifted page."""

    selector: str
    aria_role: str = ""
    accessible_name: str = ""
    form_section: str = ""
    input_type: str = ""
    visible: bool = True
    enabled: bool = True
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate element together with its deterministic ranking breakdown."""

    candidate: SurgicalHealCandidate
    score: float
    semantic_score: float
    visual_score: float | None
    signals: dict[str, float]


@dataclass(frozen=True)
class FleetSelectorPatch:
    """Ed25519-signed, tenant-scoped selector patch promoted after read-back."""

    tenant_id: str
    program_id: str
    program_sha256: str
    app_origin: str
    workflow_id: str
    step_id: str
    old_selector: str
    new_selector: str
    readback_verified: bool
    patch_sha256: str
    signer_key_id: str
    signature_b64: str
    confidence: float = 1.0
    margin: float = 1.0
    readback_evidence_sha256: str = ""
    schema_version: str = FLEET_PATCH_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        scope: PatchScope,
        step_id: str,
        old_selector: str,
        new_selector: str,
        readback_verified: bool,
        confidence: float,
        margin: float,
        signing_key: Ed25519PrivateKey,
        signer_key_id: str,
        readback_evidence_sha256: str = "",
    ) -> FleetSelectorPatch:
        if type(readback_verified) is not bool or readback_verified is not True:
            raise FleetPatchError(
                "refusing to create FleetSelectorPatch without readback_verified=True"
            )
        if not isinstance(scope, PatchScope):
            raise FleetPatchError("scope must be a PatchScope instance")
        scope.validate()
        if not isinstance(signing_key, Ed25519PrivateKey):
            raise FleetPatchError("signing_key must be an Ed25519PrivateKey")
        for field_name, field_val in (
            ("step_id", step_id),
            ("old_selector", old_selector),
            ("new_selector", new_selector),
            ("signer_key_id", signer_key_id),
        ):
            if not isinstance(field_val, str) or not field_val.strip():
                raise FleetPatchError(f"{field_name} must be a non-empty string")
        payload = cls._digest_payload(
            schema_version=FLEET_PATCH_SCHEMA_VERSION,
            tenant_id=scope.tenant_id,
            program_id=scope.program_id,
            program_sha256=scope.program_sha256,
            app_origin=scope.app_origin,
            workflow_id=scope.workflow_id,
            step_id=step_id,
            old_selector=old_selector,
            new_selector=new_selector,
            readback_verified=True,
            confidence=round(float(confidence), 6),
            margin=round(float(margin), 6),
            readback_evidence_sha256=readback_evidence_sha256,
            signer_key_id=signer_key_id,
        )
        canonical_bytes = _canonical_json_bytes(payload)
        sig_bytes = signing_key.sign(canonical_bytes)
        sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
        return cls(
            tenant_id=scope.tenant_id,
            program_id=scope.program_id,
            program_sha256=scope.program_sha256,
            app_origin=scope.app_origin,
            workflow_id=scope.workflow_id,
            step_id=step_id,
            old_selector=old_selector,
            new_selector=new_selector,
            readback_verified=True,
            patch_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
            signer_key_id=signer_key_id,
            signature_b64=sig_b64,
            confidence=round(float(confidence), 6),
            margin=round(float(margin), 6),
            readback_evidence_sha256=readback_evidence_sha256,
            schema_version=FLEET_PATCH_SCHEMA_VERSION,
        )

    @staticmethod
    def _digest_payload(
        *,
        schema_version: str,
        tenant_id: str,
        program_id: str,
        program_sha256: str,
        app_origin: str,
        workflow_id: str,
        step_id: str,
        old_selector: str,
        new_selector: str,
        readback_verified: bool,
        confidence: float,
        margin: float,
        readback_evidence_sha256: str,
        signer_key_id: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "tenant_id": tenant_id,
            "program_id": program_id,
            "program_sha256": program_sha256,
            "app_origin": app_origin,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "old_selector": old_selector,
            "new_selector": new_selector,
            "readback_verified": readback_verified,
            "confidence": confidence,
            "margin": margin,
            "readback_evidence_sha256": readback_evidence_sha256,
            "signer_key_id": signer_key_id,
        }

    def verify_signature(
        self,
        verify_key: Ed25519PublicKey | Mapping[str, Ed25519PublicKey],
    ) -> bool:
        """Verify schema, strict bool flag, scope fields, digest, and Ed25519 signature."""
        if self.schema_version != FLEET_PATCH_SCHEMA_VERSION:
            return False
        if (
            type(self.readback_verified) is not bool
            or self.readback_verified is not True
        ):
            return False
        required_strings = (
            self.tenant_id,
            self.program_id,
            self.program_sha256,
            self.app_origin,
            self.workflow_id,
            self.step_id,
            self.old_selector,
            self.new_selector,
            self.patch_sha256,
            self.signer_key_id,
            self.signature_b64,
        )
        if not all(isinstance(v, str) and bool(v.strip()) for v in required_strings):
            return False

        if isinstance(verify_key, Mapping):
            pubkey = verify_key.get(self.signer_key_id)
            if not isinstance(pubkey, Ed25519PublicKey):
                return False
        elif isinstance(verify_key, Ed25519PublicKey):
            pubkey = verify_key
        else:
            return False

        payload = self._digest_payload(
            schema_version=self.schema_version,
            tenant_id=self.tenant_id,
            program_id=self.program_id,
            program_sha256=self.program_sha256,
            app_origin=self.app_origin,
            workflow_id=self.workflow_id,
            step_id=self.step_id,
            old_selector=self.old_selector,
            new_selector=self.new_selector,
            readback_verified=self.readback_verified,
            confidence=self.confidence,
            margin=self.margin,
            readback_evidence_sha256=self.readback_evidence_sha256,
            signer_key_id=self.signer_key_id,
        )
        canonical_bytes = _canonical_json_bytes(payload)
        expected_sha = hashlib.sha256(canonical_bytes).hexdigest()
        if not hmac.compare_digest(self.patch_sha256, expected_sha):
            return False
        try:
            sig_bytes = base64.b64decode(
                self.signature_b64.encode("ascii"), validate=True
            )
            pubkey.verify(sig_bytes, canonical_bytes)
        except (ValueError, InvalidSignature):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "program_id": self.program_id,
            "program_sha256": self.program_sha256,
            "app_origin": self.app_origin,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "old_selector": self.old_selector,
            "new_selector": self.new_selector,
            "readback_verified": self.readback_verified,
            "confidence": self.confidence,
            "margin": self.margin,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "signer_key_id": self.signer_key_id,
            "signature_b64": self.signature_b64,
            "patch_sha256": self.patch_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FleetSelectorPatch:
        try:
            raw_verified = data["readback_verified"]
            if type(raw_verified) is not bool:
                raise FleetPatchError(
                    f"readback_verified must be a JSON boolean, got {type(raw_verified).__name__}"
                )
            return cls(
                schema_version=str(data["schema_version"]),
                tenant_id=str(data["tenant_id"]),
                program_id=str(data["program_id"]),
                program_sha256=str(data["program_sha256"]),
                app_origin=str(data["app_origin"]),
                workflow_id=str(data["workflow_id"]),
                step_id=str(data["step_id"]),
                old_selector=str(data["old_selector"]),
                new_selector=str(data["new_selector"]),
                readback_verified=raw_verified,
                patch_sha256=str(data["patch_sha256"]),
                signer_key_id=str(data["signer_key_id"]),
                signature_b64=str(data["signature_b64"]),
                confidence=float(data.get("confidence", 1.0)),
                margin=float(data.get("margin", 1.0)),
                readback_evidence_sha256=str(data.get("readback_evidence_sha256", "")),
            )
        except FleetPatchError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise FleetPatchError(f"invalid FleetSelectorPatch payload: {exc}") from exc

    @classmethod
    def from_json(cls, raw: str) -> FleetSelectorPatch:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FleetPatchError(f"invalid FleetSelectorPatch JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise FleetPatchError("FleetSelectorPatch JSON must be an object")
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class CandidatePatchedProgram:
    """Candidate program produced by applying a signed selector patch.

    A patched program is never runnable directly: it carries
    ``status="PENDING_QUALIFICATION"``, ``requires_re_qualification=True``,
    ``requires_admission_approval=True``, and a new ``candidate_program_sha256``
    distinct from ``source_program_sha256`` so it must pass qualification and
    admission before execution.
    """

    tenant_id: str
    program_id: str
    app_origin: str
    source_program_sha256: str
    candidate_program_sha256: str
    applied_patch_sha256: str
    steps: tuple[dict[str, Any], ...]
    status: Literal["PENDING_QUALIFICATION"] = "PENDING_QUALIFICATION"
    requires_re_qualification: bool = True
    requires_admission_approval: bool = True


@dataclass(frozen=True)
class SurgicalHealResult:
    """Outcome of a 1-step surgical heal attempt (always fail-closed)."""

    verdict: HealVerdict
    reason: str
    selected: ScoredCandidate | None
    ranked: tuple[ScoredCandidate, ...]
    margin: float
    readback_verified: bool = False
    patch: FleetSelectorPatch | None = None


class SurgicalSelectorHealer:
    """Identity-preserving deterministic ranker with fail-closed margin and read-back gates."""

    def __init__(
        self,
        *,
        min_score: float = 0.75,
        min_margin: float = 0.15,
        visual_locator: VisualLocatorCallback | None = None,
        visual_weight: float = 0.20,
    ) -> None:
        if not (math.isfinite(min_score) and 0.0 < min_score <= 1.0):
            raise ValueError("min_score must be finite and in (0.0, 1.0]")
        if not (math.isfinite(min_margin) and 0.0 < min_margin <= 1.0):
            raise ValueError("min_margin must be finite and in (0.0, 1.0]")
        if not (math.isfinite(visual_weight) and 0.0 <= visual_weight <= 0.5):
            raise ValueError("visual_weight must be finite and in [0.0, 0.5]")
        self.min_score = float(min_score)
        self.min_margin = float(min_margin)
        self.visual_locator = visual_locator
        self.visual_weight = float(visual_weight)

    @staticmethod
    def _matches_identity_anchors(
        target: ElementTarget,
        candidate: SurgicalHealCandidate,
    ) -> tuple[bool, str]:
        """Enforce hard identity anchors so renamed/sibling fields never fuzzy-heal."""
        if target.accessible_name.strip():
            if _normalize_text(target.accessible_name) != _normalize_text(
                candidate.accessible_name
            ):
                return (
                    False,
                    "lost accessible_name identity anchor: "
                    f"expected {target.accessible_name!r}, observed {candidate.accessible_name!r}",
                )
        if target.aria_role.strip():
            if (
                target.aria_role.strip().casefold()
                != candidate.aria_role.strip().casefold()
            ):
                return (
                    False,
                    "lost aria_role identity anchor: "
                    f"expected {target.aria_role!r}, observed {candidate.aria_role!r}",
                )
        if target.input_type.strip():
            if (
                target.input_type.strip().casefold()
                != candidate.input_type.strip().casefold()
            ):
                return (
                    False,
                    "lost input_type identity anchor: "
                    f"expected {target.input_type!r}, observed {candidate.input_type!r}",
                )
        return True, ""

    def _score_candidate(
        self,
        target: ElementTarget,
        candidate: SurgicalHealCandidate,
    ) -> ScoredCandidate:
        signals: dict[str, float] = {}
        active_weight = 0.0
        weighted_sum = 0.0

        if target.aria_role.strip():
            role_match = (
                1.0
                if target.aria_role.strip().casefold()
                == candidate.aria_role.strip().casefold()
                else 0.0
            )
            signals["aria_role"] = role_match
            weighted_sum += _SIGNAL_WEIGHTS["aria_role"] * role_match
            active_weight += _SIGNAL_WEIGHTS["aria_role"]

        if target.accessible_name.strip():
            name_sim = _text_similarity(
                target.accessible_name, candidate.accessible_name
            )
            signals["accessible_name"] = round(name_sim, 6)
            weighted_sum += _SIGNAL_WEIGHTS["accessible_name"] * name_sim
            active_weight += _SIGNAL_WEIGHTS["accessible_name"]

        if target.form_section.strip():
            section_sim = _text_similarity(target.form_section, candidate.form_section)
            signals["form_section"] = round(section_sim, 6)
            weighted_sum += _SIGNAL_WEIGHTS["form_section"] * section_sim
            active_weight += _SIGNAL_WEIGHTS["form_section"]

        if target.input_type.strip():
            type_match = (
                1.0
                if target.input_type.strip().casefold()
                == candidate.input_type.strip().casefold()
                else 0.0
            )
            signals["input_type"] = type_match
            weighted_sum += _SIGNAL_WEIGHTS["input_type"] * type_match
            active_weight += _SIGNAL_WEIGHTS["input_type"]

        semantic_score = weighted_sum / active_weight if active_weight > 0.0 else 0.0
        visual_score: float | None = None
        total_score = semantic_score

        if self.visual_locator is not None and self.visual_weight > 0.0:
            raw_visual = float(self.visual_locator(target, candidate))
            if not (math.isfinite(raw_visual) and 0.0 <= raw_visual <= 1.0):
                raise ValueError(
                    f"visual_locator returned out-of-range score: {raw_visual!r}"
                )
            visual_score = round(raw_visual, 6)
            signals["visual"] = visual_score
            total_score = (
                1.0 - self.visual_weight
            ) * semantic_score + self.visual_weight * raw_visual

        return ScoredCandidate(
            candidate=candidate,
            score=round(max(0.0, min(1.0, total_score)), 6),
            semantic_score=round(semantic_score, 6),
            visual_score=visual_score,
            signals=signals,
        )

    def rank_and_select(
        self,
        target: ElementTarget,
        candidates: Sequence[SurgicalHealCandidate],
    ) -> SurgicalHealResult:
        """Rank candidates with hard identity-anchor and confidence margin gates."""
        if not target.accessible_name.strip():
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason="target has no accessible_name identity anchor for deterministic healing",
                selected=None,
                ranked=(),
                margin=0.0,
            )

        eligible = [
            c
            for c in candidates
            if c.visible
            and c.enabled
            and isinstance(c.selector, str)
            and c.selector.strip()
        ]
        if not eligible:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason="no eligible visible and enabled candidates",
                selected=None,
                ranked=(),
                margin=0.0,
            )

        identity_matched: list[SurgicalHealCandidate] = []
        rejection_reasons: list[str] = []
        for candidate in eligible:
            ok, reason = self._matches_identity_anchors(target, candidate)
            if ok:
                identity_matched.append(candidate)
            else:
                rejection_reasons.append(reason)

        if not identity_matched:
            detail = (
                rejection_reasons[0]
                if rejection_reasons
                else "no candidate matched identity anchors"
            )
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason=f"identity anchor check failed ({detail})",
                selected=None,
                ranked=(),
                margin=0.0,
            )

        scored: list[ScoredCandidate] = []
        for candidate in identity_matched:
            try:
                scored.append(self._score_candidate(target, candidate))
            except Exception as exc:  # noqa: BLE001 - fail closed on callback errors
                return SurgicalHealResult(
                    verdict="ABSTAIN_HALT",
                    reason=f"visual locator callback failed: {type(exc).__name__}",
                    selected=None,
                    ranked=(),
                    margin=0.0,
                )

        scored.sort(key=lambda item: (-item.score, item.candidate.selector))
        ranked = tuple(scored)
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = round(top.score - second_score, 6)

        if top.score < self.min_score:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason=(
                    f"top candidate score {top.score:.3f} below "
                    f"min_score {self.min_score:.3f}"
                ),
                selected=None,
                ranked=ranked,
                margin=margin,
            )

        if len(ranked) > 1 and margin < self.min_margin:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason=(
                    f"ambiguous candidates: margin {margin:.3f} below "
                    f"min_margin {self.min_margin:.3f}"
                ),
                selected=None,
                ranked=ranked,
                margin=margin,
            )

        return SurgicalHealResult(
            verdict="HEALED",
            reason="selected unique identity-anchored candidate above score and margin thresholds",
            selected=top,
            ranked=ranked,
            margin=margin,
        )

    def heal_and_verify(
        self,
        target: ElementTarget,
        candidates: Sequence[SurgicalHealCandidate],
        *,
        scope: PatchScope,
        signing_key: Ed25519PrivateKey,
        signer_key_id: str,
        readback_oracle: ReadbackOracle,
        pre_action_identity_check: PreActionIdentityCheck | None = None,
    ) -> SurgicalHealResult:
        """Select an identity-anchored candidate, run pre-action & read-back checks, then sign a patch."""
        scope.validate()
        if scope.workflow_id != target.workflow_id:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason=(
                    f"scope workflow_id {scope.workflow_id!r} does not match "
                    f"target workflow_id {target.workflow_id!r}"
                ),
                selected=None,
                ranked=(),
                margin=0.0,
            )

        selection = self.rank_and_select(target, candidates)
        if selection.verdict != "HEALED" or selection.selected is None:
            return selection

        if pre_action_identity_check is not None:
            try:
                identity_ok = bool(
                    pre_action_identity_check(target, selection.selected.candidate)
                )
            except Exception as exc:  # noqa: BLE001 - fail closed
                return SurgicalHealResult(
                    verdict="ABSTAIN_HALT",
                    reason=f"pre-action identity check raised {type(exc).__name__}",
                    selected=selection.selected,
                    ranked=selection.ranked,
                    margin=selection.margin,
                    readback_verified=False,
                    patch=None,
                )
            if not identity_ok:
                return SurgicalHealResult(
                    verdict="ABSTAIN_HALT",
                    reason="pre-action identity check rejected candidate before acting",
                    selected=selection.selected,
                    ranked=selection.ranked,
                    margin=selection.margin,
                    readback_verified=False,
                    patch=None,
                )

        evidence_sha256 = ""
        try:
            verdict = readback_oracle(selection.selected.candidate)
        except Exception as exc:  # noqa: BLE001 - fail closed on oracle failure
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason=f"read-back oracle raised {type(exc).__name__}",
                selected=selection.selected,
                ranked=selection.ranked,
                margin=selection.margin,
                readback_verified=False,
                patch=None,
            )

        if isinstance(verdict, tuple):
            ok, evidence_sha256 = bool(verdict[0]), str(verdict[1])
        else:
            ok = bool(verdict)

        if not ok:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason="independent read-back verification failed; patch withheld",
                selected=selection.selected,
                ranked=selection.ranked,
                margin=selection.margin,
                readback_verified=False,
                patch=None,
            )

        patch = FleetSelectorPatch.create(
            scope=scope,
            step_id=target.step_id,
            old_selector=target.old_selector,
            new_selector=selection.selected.candidate.selector,
            readback_verified=True,
            confidence=selection.selected.score,
            margin=selection.margin,
            signing_key=signing_key,
            signer_key_id=signer_key_id,
            readback_evidence_sha256=evidence_sha256,
        )
        return SurgicalHealResult(
            verdict="HEALED",
            reason="healed selector confirmed by independent read-back and signed",
            selected=selection.selected,
            ranked=selection.ranked,
            margin=selection.margin,
            readback_verified=True,
            patch=patch,
        )


def apply_fleet_patch(
    pack_steps: Sequence[Mapping[str, Any]],
    patch: FleetSelectorPatch,
    *,
    verify_key: Ed25519PublicKey | Mapping[str, Ed25519PublicKey],
    scope: PatchScope,
) -> CandidatePatchedProgram:
    """Verify a signed, tenant-scoped patch and produce a candidate program pending qualification.

    Does not mutate ``pack_steps``. Raises :class:`FleetPatchError` if the
    patch fails Ed25519 verification, scope matching (tenant, program, digest,
    origin, workflow), or target step selector matching.
    """
    if not isinstance(patch, FleetSelectorPatch):
        raise FleetPatchError("patch must be a FleetSelectorPatch instance")
    if not isinstance(scope, PatchScope):
        raise FleetPatchError("scope must be a PatchScope instance")
    scope.validate()
    if type(patch.readback_verified) is not bool or patch.readback_verified is not True:
        raise FleetPatchError(
            "refusing to apply FleetSelectorPatch with readback_verified != True"
        )
    if not patch.verify_signature(verify_key):
        raise FleetPatchError(
            "FleetSelectorPatch signature/integrity verification failed"
        )

    actual_program_sha256 = compute_program_sha256(pack_steps)
    if not hmac.compare_digest(actual_program_sha256, scope.program_sha256):
        raise FleetPatchError(
            f"pack program_sha256 mismatch: expected {scope.program_sha256!r}, "
            f"computed {actual_program_sha256!r}"
        )

    for field_name, patch_val, scope_val in (
        ("tenant_id", patch.tenant_id, scope.tenant_id),
        ("program_id", patch.program_id, scope.program_id),
        ("program_sha256", patch.program_sha256, scope.program_sha256),
        ("app_origin", patch.app_origin, scope.app_origin),
        ("workflow_id", patch.workflow_id, scope.workflow_id),
    ):
        if not hmac.compare_digest(patch_val, scope_val):
            raise FleetPatchError(
                f"patch scope mismatch on {field_name}: patch has {patch_val!r}, "
                f"target pack has {scope_val!r}"
            )

    updated = [copy.deepcopy(dict(step)) for step in pack_steps]
    matches = [
        idx for idx, step in enumerate(updated) if step.get("step_id") == patch.step_id
    ]
    if not matches:
        raise FleetPatchError(f"step_id {patch.step_id!r} not found in workflow pack")
    if len(matches) > 1:
        raise FleetPatchError(
            f"step_id {patch.step_id!r} appears {len(matches)} times in workflow pack"
        )

    target_step = updated[matches[0]]
    step_workflow_id = target_step.get("workflow_id")
    if not isinstance(step_workflow_id, str) or not step_workflow_id.strip():
        raise FleetPatchError(
            f"step {patch.step_id!r} is missing required workflow_id; refusing to patch"
        )
    if step_workflow_id != patch.workflow_id:
        raise FleetPatchError(
            f"workflow_id mismatch: step has {step_workflow_id!r}, "
            f"patch targets {patch.workflow_id!r}"
        )

    current_selector = target_step.get("selector")
    if current_selector != patch.old_selector:
        raise FleetPatchError(
            f"selector mismatch on step {patch.step_id!r}: expected "
            f"{patch.old_selector!r}, found {current_selector!r}"
        )

    target_step["selector"] = patch.new_selector
    target_step["healed_by_patch_sha256"] = patch.patch_sha256
    candidate_sha = compute_program_sha256(updated)
    return CandidatePatchedProgram(
        tenant_id=scope.tenant_id,
        program_id=scope.program_id,
        app_origin=scope.app_origin,
        source_program_sha256=actual_program_sha256,
        candidate_program_sha256=candidate_sha,
        applied_patch_sha256=patch.patch_sha256,
        steps=tuple(updated),
        status="PENDING_QUALIFICATION",
        requires_re_qualification=True,
        requires_admission_approval=True,
    )


def apply_fleet_patch_to_packs(
    packs: Mapping[str, Sequence[Mapping[str, Any]]],
    patch: FleetSelectorPatch,
    *,
    verify_key: Ed25519PublicKey | Mapping[str, Ed25519PublicKey],
    pack_scopes: Mapping[str, PatchScope],
) -> dict[str, CandidatePatchedProgram]:
    """Produce candidate patched programs only for packs matching the signed patch's scope."""
    propagated: dict[str, CandidatePatchedProgram] = {}
    for pack_name, steps in packs.items():
        scope = pack_scopes.get(pack_name)
        if scope is None:
            raise FleetPatchError(f"missing PatchScope for pack {pack_name!r}")
        propagated[pack_name] = apply_fleet_patch(
            steps,
            patch,
            verify_key=verify_key,
            scope=scope,
        )
    return propagated


__all__ = [
    "FLEET_PATCH_SCHEMA_VERSION",
    "CandidatePatchedProgram",
    "ElementTarget",
    "FleetPatchError",
    "FleetSelectorPatch",
    "HealVerdict",
    "PatchScope",
    "PreActionIdentityCheck",
    "ReadbackOracle",
    "ScoredCandidate",
    "SurgicalHealCandidate",
    "SurgicalHealResult",
    "SurgicalSelectorHealer",
    "VisualLocatorCallback",
    "apply_fleet_patch",
    "apply_fleet_patch_to_packs",
    "compute_program_sha256",
]
