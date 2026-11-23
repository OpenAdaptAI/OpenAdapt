"""3-layer surgical selector healing and read-back-gated fleet patch propagation.

When a compiled workflow encounters a broken locator on a drifted UI, this
module provides a deterministic, fail-closed alternative to falling back to a
full multi-step agent loop:

1. **Surgical 1-step ranking:** Rank visible, enabled DOM/ARIA candidates using
   deterministic semantic signals (ARIA role, accessible name/label similarity,
   form section proximity, input type match) plus an optional local
   visual-locator score callback.
2. **Fail-closed margin gate:** Accept a candidate only when
   ``top_score >= min_score`` and ``top_score - second_score >= min_margin``;
   otherwise return ``ABSTAIN_HALT`` so a person can intervene.
3. **Post-heal read-back gate & fleet propagation:** Emit a content-addressed
   :class:`FleetSelectorPatch` (with ``readback_verified=True`` and
   ``patch_sha256``) only after an independent read-back oracle confirms the
   healed step succeeded, and apply it atomically across compiled workflow
   packs after verifying integrity.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

FLEET_PATCH_SCHEMA_VERSION = "openadapt.fleet-selector-patch/v1"

HealVerdict = Literal["HEALED", "ABSTAIN_HALT"]

VisualLocatorCallback = Callable[["ElementTarget", "SurgicalHealCandidate"], float]
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
    """Raised when a fleet selector patch fails integrity or applicability checks."""


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


def _canonical_patch_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
    """Content-addressed selector patch promoted only after read-back succeeds."""

    workflow_id: str
    step_id: str
    old_selector: str
    new_selector: str
    readback_verified: bool
    patch_sha256: str
    confidence: float = 1.0
    margin: float = 1.0
    readback_evidence_sha256: str = ""
    schema_version: str = FLEET_PATCH_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        step_id: str,
        old_selector: str,
        new_selector: str,
        readback_verified: bool,
        confidence: float,
        margin: float,
        readback_evidence_sha256: str = "",
    ) -> FleetSelectorPatch:
        if readback_verified is not True:
            raise FleetPatchError(
                "refusing to create FleetSelectorPatch without readback_verified=True"
            )
        for field_name, field_val in (
            ("workflow_id", workflow_id),
            ("step_id", step_id),
            ("old_selector", old_selector),
            ("new_selector", new_selector),
        ):
            if not isinstance(field_val, str) or not field_val.strip():
                raise FleetPatchError(f"{field_name} must be a non-empty string")
        payload = cls._digest_payload(
            schema_version=FLEET_PATCH_SCHEMA_VERSION,
            workflow_id=workflow_id,
            step_id=step_id,
            old_selector=old_selector,
            new_selector=new_selector,
            readback_verified=True,
            confidence=round(float(confidence), 6),
            margin=round(float(margin), 6),
            readback_evidence_sha256=readback_evidence_sha256,
        )
        return cls(
            workflow_id=workflow_id,
            step_id=step_id,
            old_selector=old_selector,
            new_selector=new_selector,
            readback_verified=True,
            patch_sha256=_canonical_patch_digest(payload),
            confidence=round(float(confidence), 6),
            margin=round(float(margin), 6),
            readback_evidence_sha256=readback_evidence_sha256,
            schema_version=FLEET_PATCH_SCHEMA_VERSION,
        )

    @staticmethod
    def _digest_payload(
        *,
        schema_version: str,
        workflow_id: str,
        step_id: str,
        old_selector: str,
        new_selector: str,
        readback_verified: bool,
        confidence: float,
        margin: float,
        readback_evidence_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "old_selector": old_selector,
            "new_selector": new_selector,
            "readback_verified": readback_verified,
            "confidence": confidence,
            "margin": margin,
            "readback_evidence_sha256": readback_evidence_sha256,
        }

    def verify_integrity(self) -> bool:
        """Verify schema, readback flag, non-empty fields, and SHA-256 digest."""
        if self.schema_version != FLEET_PATCH_SCHEMA_VERSION:
            return False
        if self.readback_verified is not True:
            return False
        if not all(
            isinstance(v, str) and bool(v.strip())
            for v in (
                self.workflow_id,
                self.step_id,
                self.old_selector,
                self.new_selector,
                self.patch_sha256,
            )
        ):
            return False
        payload = self._digest_payload(
            schema_version=self.schema_version,
            workflow_id=self.workflow_id,
            step_id=self.step_id,
            old_selector=self.old_selector,
            new_selector=self.new_selector,
            readback_verified=self.readback_verified,
            confidence=self.confidence,
            margin=self.margin,
            readback_evidence_sha256=self.readback_evidence_sha256,
        )
        expected = _canonical_patch_digest(payload)
        return hmac.compare_digest(self.patch_sha256, expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "old_selector": self.old_selector,
            "new_selector": self.new_selector,
            "readback_verified": self.readback_verified,
            "confidence": self.confidence,
            "margin": self.margin,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "patch_sha256": self.patch_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FleetSelectorPatch:
        try:
            return cls(
                schema_version=str(data["schema_version"]),
                workflow_id=str(data["workflow_id"]),
                step_id=str(data["step_id"]),
                old_selector=str(data["old_selector"]),
                new_selector=str(data["new_selector"]),
                readback_verified=bool(data["readback_verified"]),
                patch_sha256=str(data["patch_sha256"]),
                confidence=float(data.get("confidence", 1.0)),
                margin=float(data.get("margin", 1.0)),
                readback_evidence_sha256=str(data.get("readback_evidence_sha256", "")),
            )
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
    """Deterministic semantic ranker with fail-closed margin and read-back gates."""

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
        """Rank candidates and apply the fail-closed confidence margin gate."""
        has_semantic_anchor = any(
            v.strip()
            for v in (
                target.aria_role,
                target.accessible_name,
                target.form_section,
                target.input_type,
            )
        )
        if not has_semantic_anchor:
            return SurgicalHealResult(
                verdict="ABSTAIN_HALT",
                reason="target has no semantic anchors for deterministic healing",
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

        scored: list[ScoredCandidate] = []
        for candidate in eligible:
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

        if margin < self.min_margin:
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
            reason="selected unique candidate above score and margin thresholds",
            selected=top,
            ranked=ranked,
            margin=margin,
        )

    def heal_and_verify(
        self,
        target: ElementTarget,
        candidates: Sequence[SurgicalHealCandidate],
        *,
        readback_oracle: ReadbackOracle,
    ) -> SurgicalHealResult:
        """Select a candidate, verify via independent read-back, then emit a patch."""
        selection = self.rank_and_select(target, candidates)
        if selection.verdict != "HEALED" or selection.selected is None:
            return selection

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
            workflow_id=target.workflow_id,
            step_id=target.step_id,
            old_selector=target.old_selector,
            new_selector=selection.selected.candidate.selector,
            readback_verified=True,
            confidence=selection.selected.score,
            margin=selection.margin,
            readback_evidence_sha256=evidence_sha256,
        )
        return SurgicalHealResult(
            verdict="HEALED",
            reason="healed selector confirmed by independent read-back",
            selected=selection.selected,
            ranked=selection.ranked,
            margin=selection.margin,
            readback_verified=True,
            patch=patch,
        )


def apply_fleet_patch(
    pack_steps: Sequence[Mapping[str, Any]],
    patch: FleetSelectorPatch,
) -> list[dict[str, Any]]:
    """Atomically apply a verified FleetSelectorPatch to a compiled workflow pack.

    Does not mutate ``pack_steps``. Raises :class:`FleetPatchError` if the
    patch is unverified, tampered, missing its target step, or if the target
    step's current selector does not match ``patch.old_selector``.
    """
    if not isinstance(patch, FleetSelectorPatch):
        raise FleetPatchError("patch must be a FleetSelectorPatch instance")
    if patch.readback_verified is not True:
        raise FleetPatchError(
            "refusing to apply FleetSelectorPatch with readback_verified != True"
        )
    if not patch.verify_integrity():
        raise FleetPatchError("FleetSelectorPatch integrity check failed (bad sha256)")

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
    if step_workflow_id is not None and step_workflow_id != patch.workflow_id:
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
    return updated


def apply_fleet_patch_to_packs(
    packs: Mapping[str, Sequence[Mapping[str, Any]]],
    patch: FleetSelectorPatch,
) -> dict[str, list[dict[str, Any]]]:
    """Atomically propagate a verified FleetSelectorPatch across all agency packs."""
    propagated: dict[str, list[dict[str, Any]]] = {}
    for agency_id, steps in packs.items():
        propagated[agency_id] = apply_fleet_patch(steps, patch)
    return propagated


__all__ = [
    "FLEET_PATCH_SCHEMA_VERSION",
    "ElementTarget",
    "FleetPatchError",
    "FleetSelectorPatch",
    "HealVerdict",
    "ReadbackOracle",
    "ScoredCandidate",
    "SurgicalHealCandidate",
    "SurgicalHealResult",
    "SurgicalSelectorHealer",
    "VisualLocatorCallback",
    "apply_fleet_patch",
    "apply_fleet_patch_to_packs",
]
