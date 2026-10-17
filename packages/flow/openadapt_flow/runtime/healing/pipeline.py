r"""Promotion pipeline: candidate -> gate -> canary -> promote / rollback.

The deterministic, ``$0`` control flow that turns a raw heal into either a
PROMOTED patch (applied, folded into the healed bundle) or a QUARANTINED one
(never applied, the run HALTS). No model calls.

    candidate  --gate-->  promotable  --canary-->  promoted
                   \                        \
                    quarantined (HALT)       rolled_back (HALT)

The gate is the invariant check (:class:`RegressionGate`); the canary is an
optional apply-and-monitor step (a callable supplied by the caller -- e.g. the
perturbation harness or a prior-trace replay) that can still veto a
gate-passing patch. Either veto refuses the patch and signals the run to halt,
so an unverified repair is never auto-applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from openadapt_flow.ir import Anchor, HealEvent, Step
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    EffectVerdictFn,
    GateResult,
    RegressionGate,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch

_PATCH_JSON_NAME = "patch.json"

#: A canary applies a promotable patch and monitors it, returning
#: ``(ok, reason)``. ``ok=False`` rolls the patch back and halts the run.
CanaryFn = Callable[[HealPatch], "tuple[bool, str]"]


class PromotionOutcome(BaseModel):
    """Result of running a candidate patch through the promotion pipeline."""

    patch: HealPatch
    promoted: bool
    gate: GateResult
    #: Set when the patch was refused (quarantined or rolled back). The
    #: replayer surfaces this as the step error so the run halts.
    halt_reason: Optional[str] = None
    canary_ran: bool = False
    canary_reason: str = ""


def run_promotion(
    patch: HealPatch,
    old_anchor: Anchor,
    new_anchor: Anchor,
    *,
    old_step: Step,
    new_step: Step,
    gate: Optional[RegressionGate] = None,
    band_verifier: BandVerifier = _default_band_verifier,
    effect_baseline: Optional[dict[str, bool]] = None,
    effect_now: Optional[dict[str, EffectVerdictFn]] = None,
    canary: Optional[CanaryFn] = None,
) -> PromotionOutcome:
    """Run one candidate patch through gate -> canary -> promote/quarantine."""
    gate = gate or RegressionGate()
    result = gate.evaluate(
        patch,
        old_anchor,
        new_anchor,
        old_step=old_step,
        new_step=new_step,
        band_verifier=band_verifier,
        effect_baseline=effect_baseline,
        effect_now=effect_now,
    )
    if not result.passed:
        patch.status = "quarantined"
        patch.reject_reason = "; ".join(result.failures)
        return PromotionOutcome(
            patch=patch,
            promoted=False,
            gate=result,
            halt_reason=(
                f"heal quarantined for step {patch.step_id!r}: {patch.reject_reason}"
            ),
        )

    patch.status = "promotable"
    if canary is not None:
        ok, reason = canary(patch)
        if not ok:
            patch.status = "rolled_back"
            patch.reject_reason = reason
            return PromotionOutcome(
                patch=patch,
                promoted=False,
                gate=result,
                canary_ran=True,
                canary_reason=reason,
                halt_reason=(
                    f"heal rolled back for step {patch.step_id!r} after "
                    f"canary regression: {reason}"
                ),
            )
        patch.status = "promoted"
        return PromotionOutcome(
            patch=patch,
            promoted=True,
            gate=result,
            canary_ran=True,
            canary_reason=reason,
        )

    patch.status = "promoted"
    return PromotionOutcome(patch=patch, promoted=True, gate=result)


def persist_patch(patch: HealPatch, run_dir: Path) -> Path:
    """Write the patch under ``run_dir/heals/<step_id>/patch.json``.

    Written for BOTH promoted and quarantined patches -- the quarantine
    record is the audit trail for a refused (halting) repair.
    """
    heal_dir = Path(run_dir) / "heals" / patch.step_id
    heal_dir.mkdir(parents=True, exist_ok=True)
    path = heal_dir / _PATCH_JSON_NAME
    path.write_text(patch.model_dump_json(indent=2))
    return path


class HealOutcome(BaseModel):
    """What the replayer's heal hook returns for one step.

    ``promoted`` -> the heal was applied; the caller records ``event`` and the
    new crop. Otherwise the caller must HALT the run with ``halt_reason`` and
    NOT apply the heal (the weakened / regressing repair is quarantined).
    """

    step_id: str
    promoted: bool
    patch: HealPatch
    #: The applied event, present only when promoted (so the replayer records
    #: it on the step result exactly as before).
    event: Optional[HealEvent] = None
    halt_reason: Optional[str] = None


def govern_heal(
    step: Step,
    event: HealEvent,
    *,
    run_dir: Path,
    gate: Optional[RegressionGate] = None,
    band_verifier: BandVerifier = _default_band_verifier,
    effect_baseline: Optional[dict[str, bool]] = None,
    effect_now: Optional[dict[str, EffectVerdictFn]] = None,
    canary: Optional[CanaryFn] = None,
) -> HealOutcome:
    """Govern a freshly-built heal event: gate, persist the patch, decide.

    The single entrypoint the replayer's heal hook calls. It NEVER applies the
    heal itself (the replayer owns applying/persisting the event and crop) --
    it only decides whether the heal is safe to apply and records the
    reviewable patch either way.

    Returns:
        A :class:`HealOutcome`. When ``promoted`` is False the replayer must
        halt the run and must not apply the heal.
    """
    patch = HealPatch.from_event(event)
    outcome = run_promotion(
        patch,
        event.old_anchor,
        event.new_anchor,
        # A heal never rebuilds the whole Step, only its anchor; the risk
        # class is carried on the step and is unchanged by the heal, so the
        # risk-regression check compares the step against itself (asserting a
        # future heal that DID touch risk cannot slip a downgrade through).
        old_step=step,
        new_step=step,
        gate=gate,
        band_verifier=band_verifier,
        effect_baseline=effect_baseline,
        effect_now=effect_now,
        canary=canary,
    )
    persist_patch(outcome.patch, run_dir)
    return HealOutcome(
        step_id=step.id,
        promoted=outcome.promoted,
        patch=outcome.patch,
        event=event if outcome.promoted else None,
        halt_reason=outcome.halt_reason,
    )
