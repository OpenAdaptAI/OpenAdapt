"""Governed healing: the invariant, the regression gate, the promotion pipeline.

A heal is a REPAIR, and a repair carries the same risk as any edit to a
safety-critical program: it can silently weaken the thing that made the
program safe. Two external reviews found the concrete instance -- a heal could
refresh a step's identity context to ``None``, disabling the pre-click
identity gate for that step (an ARMED step silently downgraded to UNARMED and
still reported green). This module closes that hole with one invariant and a
gate that refuses any patch that would violate it:

    A repair may change HOW an operation is performed (its locator / rung),
    but NEVER silently weaken WHAT it means (its identity band) or how its
    effects are verified (its effect coverage), nor downgrade its risk class.

Everything here is deterministic and ``$0`` -- NO model calls on the runtime
hot path. The identity check reuses the same OCR band matcher the pre-click
gate uses (:func:`openadapt_flow.runtime.identity.verify_target_identity`), so
a patch is judged by the very rule that protects the click.

Pipeline: candidate patch -> regression GATE (identity + effect + risk) ->
CANARY (apply, monitor against prior traces + perturbations) -> PROMOTE or
ROLLBACK. A patch that fails the gate is QUARANTINED and the run HALTS
(refuse-rather-than-guess); it is never auto-applied.
"""

from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import Anchor, Step
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.healing.patch import IDENTITY_FIELDS, HealPatch

# A verifier maps (recorded_band, observed_band) -> verdict status string.
# Defaults to the production OCR identity matcher; injectable for tests.
BandVerifier = Callable[[str, str], str]


def _default_band_verifier(recorded: str, observed: str) -> str:
    return identity_mod.verify_target_identity(recorded, observed).status


class PreservationVerdict(BaseModel):
    """Whether a heal preserved the step's identity band."""

    preserved: bool
    reason: str = ""
    #: The band-match status when both bands were readable (diagnostic).
    band_status: Optional[str] = None


def identity_preserved(
    old_anchor: Anchor,
    new_anchor: Anchor,
    *,
    band_verifier: BandVerifier = _default_band_verifier,
) -> PreservationVerdict:
    """The core invariant: a heal must never WEAKEN the identity band.

    A repair passes iff, for the identity evidence the pre-heal anchor
    carried, the post-heal anchor carries evidence that is at least as
    strong:

    1. **Arming is never dropped.** If the old anchor was identity-armed
       (a recorded context band or structured identity), the new one must be
       too. Refreshing the band to ``None`` -- the exact reviewed bug --
       flips the step from ARMED to UNARMED and fails here.
    2. **Structured identity is never dropped.** The heal only re-derives the
       OCR context band; the structured-text tier (the highest-fidelity
       identity evidence) must survive verbatim.
    3. **The refreshed band still means the SAME entity.** When the old
       anchor had a context band, the new band -- read as the "observed"
       text -- must still VERIFY against it. A band that no longer verifies
       (unreadable, too generic, or affirmatively a DIFFERENT entity) is a
       silent weakening even though the field is non-empty, so it fails too.

    Locator changes (region / click_point / ocr_text) are always allowed --
    that is the legitimate work of a heal.

    Returns:
        A :class:`PreservationVerdict`; ``preserved=False`` means the patch
        must be quarantined and the run halted.
    """
    # A program-level revision checks every surviving step, including anchors
    # the revision did not touch.  An unchanged PHI-free identity template has
    # no readable band to feed through the live-verification path below, but it
    # also cannot have been weakened.  Admit that narrow no-op case only when
    # *all* identity-bearing anchor fields are value-identical.  Locator fields
    # are deliberately excluded: moving the region/click point or refreshing
    # locator text is the legitimate work of a heal.
    if all(
        getattr(old_anchor, field) == getattr(new_anchor, field)
        for field in IDENTITY_FIELDS
    ):
        return PreservationVerdict(
            preserved=True,
            reason="identity evidence unchanged",
        )

    # PHI-free bundles (audit REM-2) carry a salted-hash ``identity_template``
    # in place of the plaintext ``context_text`` / ``structured_identity``. The
    # gate must treat a template-armed anchor as ARMED (else a heal silently
    # unprotects a protected step — the very bug this module exists to prevent)
    # and verify the refreshed band against the template.
    old_tmpl = old_anchor.identity_template
    new_tmpl = new_anchor.identity_template
    old_struct = old_anchor.structured_identity or (
        old_tmpl.structured if old_tmpl else None
    )
    new_struct = new_anchor.structured_identity or (
        new_tmpl.structured if new_tmpl else None
    )
    old_has_band = bool(old_anchor.context_text) or bool(old_tmpl and old_tmpl.tokens)
    new_has_band = bool(new_anchor.context_text) or bool(new_tmpl and new_tmpl.tokens)
    old_armed = bool(
        old_anchor.context_text or old_struct or (old_tmpl and old_tmpl.tokens)
    )

    # Unlike a freshly OCR-read context band, these identity tiers cannot be
    # re-verified by this gate.  A heal therefore must preserve them exactly.
    # Any addition, removal, or mutation needs a separately reviewed compile /
    # teach revision rather than being smuggled through as locator repair.
    if old_tmpl != new_tmpl:
        return PreservationVerdict(
            preserved=False,
            reason="heal changed identity_template evidence",
        )

    for field in ("identifier_crop", "identifier_region"):
        if getattr(old_anchor, field) != getattr(new_anchor, field):
            return PreservationVerdict(
                preserved=False,
                reason=f"heal changed {field} identity evidence",
            )

    # (2) structured identity may never be added, dropped, or changed by a
    # heal. Adding a new identity tier is still an identity revision and needs
    # reviewed compile/teach evidence; a locator repair may not smuggle it in.
    if old_struct != new_struct:
        if not old_struct:
            change = "added"
        elif not new_struct:
            change = "dropped"
        else:
            change = "changed"
        return PreservationVerdict(
            preserved=False,
            reason=(
                f"heal {change} structured_identity: the highest-fidelity "
                "identity tier requires a reviewed compile / teach revision"
            ),
        )

    # A newly readable context band may be accepted only when this gate has
    # recorded OCR/template evidence to verify it against below. An unarmed or
    # structured-only anchor has no such band, so adding context_text would be
    # an unreviewed identity-evidence change rather than locator repair.
    if not old_has_band and old_anchor.context_text != new_anchor.context_text:
        return PreservationVerdict(
            preserved=False,
            reason=(
                "heal added context_text identity evidence without a recorded "
                "context/template band to verify; this requires a reviewed "
                "compile / teach revision"
            ),
        )

    if not old_armed:
        # Nothing to preserve: the step had no identity protection to weaken.
        # A locator-only heal on an unarmed step is fine (it was already
        # unprotected by design; docs/LIMITS.md), and the heal may leave the
        # band None without weakening anything.
        return PreservationVerdict(
            preserved=True, reason="pre-heal step was not identity-armed"
        )

    new_armed = bool(
        new_anchor.context_text or new_struct or (new_tmpl and new_tmpl.tokens)
    )
    if not new_armed:
        # (1) the reviewed bug: ARMED -> UNARMED.
        return PreservationVerdict(
            preserved=False,
            reason=(
                "heal would disable identity protection: the step is "
                "identity-armed but the refreshed anchor carries no context "
                "band or structured identity (ARMED -> UNARMED)"
            ),
        )

    # (3) the refreshed band must still verify the recorded identity. Only
    # meaningful when the old anchor's evidence was the OCR band; a surviving
    # structured identity already satisfied (2).
    if old_has_band:
        if not new_has_band:
            # structured survived (else (1) caught it), but the OCR band --
            # independent evidence the gate also reads -- was dropped.
            return PreservationVerdict(
                preserved=False,
                reason=(
                    "heal dropped the recorded context band; the OCR identity "
                    "tier would be lost even though structured identity survived"
                ),
            )
        # The heal re-derives the new band from LIVE OCR lines, so
        # ``new_anchor.context_text`` is plaintext at heal time and can be read
        # as the "observed" band against the old evidence (plaintext or
        # template). A template-only new band (no plaintext to compare) cannot
        # be proven to mean the same entity, so it fails safe.
        observed = new_anchor.context_text
        if not observed:
            return PreservationVerdict(
                preserved=False,
                reason=(
                    "heal produced only a hashed identity template with no "
                    "readable band to verify against the recorded identity"
                ),
            )
        if old_anchor.context_text:
            status = band_verifier(old_anchor.context_text, observed)
        else:
            from openadapt_flow.runtime import identity_template as itmpl

            # old_has_band is True with no plaintext context_text, so the band
            # evidence is the template (old_tmpl.tokens); narrow for the checker.
            assert old_tmpl is not None
            status = itmpl.verify_template_identity(old_tmpl, observed).status
        if status != "verified":
            return PreservationVerdict(
                preserved=False,
                band_status=status,
                reason=(
                    "refreshed context band no longer verifies the recorded "
                    f"target identity (band verdict {status!r})"
                ),
            )
        return PreservationVerdict(
            preserved=True, band_status=status, reason="identity band preserved"
        )

    return PreservationVerdict(preserved=True, reason="identity band preserved")


# --- effect regression ------------------------------------------------------
#
# A heal touches only a step's ANCHOR (how a target is located), never its
# effects (what must be true of the system of record). So a heal can regress
# effect coverage in exactly one way: by making a previously-verifiable effect
# NO LONGER verifiable. We model that as a baseline of per-effect verdicts
# taken BEFORE the patch, re-checked AFTER: a patch is refused if any effect
# that was CONFIRMED (verifiable, correct) becomes non-confirmed. The verifier
# is injected (the effects runtime, PR #63) so this stays deterministic and
# substrate-neutral; when no effect baseline is supplied the check is a no-op
# pass (a locator heal on a step with no system-of-record effect).

EffectVerdictFn = Callable[[], bool]
"""Returns True when the effect is currently verifiable+confirmed."""


class EffectRegression(BaseModel):
    """Result of the effect-regression check."""

    ok: bool = True
    newly_unverifiable: list[str] = Field(default_factory=list)


def effect_regression(
    effect_baseline: Optional[dict[str, bool]],
    effect_now: Optional[dict[str, EffectVerdictFn]],
) -> EffectRegression:
    """Refuse a patch that makes a confirmed effect no longer verifiable.

    Args:
        effect_baseline: effect id -> was it CONFIRMED before the patch.
        effect_now: effect id -> callable re-checking it after the patch.
            Only ids that were confirmed in the baseline are re-checked.

    Returns:
        ``ok=False`` (with the offending effect ids) if any effect that was
        confirmed before is not confirmed after; otherwise ``ok=True``.
    """
    if not effect_baseline or not effect_now:
        return EffectRegression(ok=True)
    regressed: list[str] = []
    for effect_id, was_confirmed in effect_baseline.items():
        if not was_confirmed:
            continue
        check = effect_now.get(effect_id)
        # A confirmed effect whose re-check is missing or now fails is a
        # regression: coverage that existed before must not vanish.
        if check is None or not check():
            regressed.append(effect_id)
    return EffectRegression(ok=not regressed, newly_unverifiable=regressed)


# --- risk regression --------------------------------------------------------


class RiskRegression(BaseModel):
    ok: bool = True
    reason: str = ""


def risk_regression(old_step: Step, new_step: Step) -> RiskRegression:
    """Refuse a patch that DOWNGRADES a step's risk class.

    An armed / irreversible step must never be silently relaxed by a heal: a
    repair that flipped ``irreversible`` -> ``reversible`` would drop the
    step's refuse-when-unverifiable protection. (A heal never edits risk, so
    in practice this holds by construction; the gate asserts it anyway so a
    future heal that DID touch risk cannot slip a downgrade through.)
    """
    if old_step.risk == "irreversible" and new_step.risk != "irreversible":
        return RiskRegression(
            ok=False,
            reason=(
                "heal would downgrade step risk "
                f"({old_step.risk} -> {new_step.risk}): an irreversible step's "
                "refuse-when-unverifiable protection would be lost"
            ),
        )
    return RiskRegression(ok=True)


# --- the gate ---------------------------------------------------------------


class GateResult(BaseModel):
    """Verdict of the full regression gate over a candidate patch."""

    passed: bool
    identity_ok: bool
    effect_ok: bool
    risk_ok: bool
    failures: list[str] = Field(default_factory=list)


class RegressionGate(BaseModel):
    """Deterministic promotability gate for a candidate :class:`HealPatch`.

    A patch is PROMOTABLE iff it passes every regression check:

    - **identity regression** -- no armed step downgraded, band still verifies
      the recorded identity (:func:`identity_preserved`);
    - **effect regression** -- no effect newly unverifiable
      (:func:`effect_regression`);
    - **risk regression** -- no armed / irreversible step downgraded
      (:func:`risk_regression`).

    Any failure quarantines the patch; the caller then HALTS the run.
    """

    model_config = {"arbitrary_types_allowed": True}

    def evaluate(
        self,
        patch: HealPatch,
        old_anchor: Anchor,
        new_anchor: Anchor,
        *,
        old_step: Step,
        new_step: Step,
        band_verifier: BandVerifier = _default_band_verifier,
        effect_baseline: Optional[dict[str, bool]] = None,
        effect_now: Optional[dict[str, EffectVerdictFn]] = None,
    ) -> GateResult:
        failures: list[str] = []

        identity = identity_preserved(
            old_anchor, new_anchor, band_verifier=band_verifier
        )
        if not identity.preserved:
            failures.append(f"identity regression: {identity.reason}")

        effects = effect_regression(effect_baseline, effect_now)
        if not effects.ok:
            failures.append(
                "effect regression: effects newly unverifiable: "
                + ", ".join(effects.newly_unverifiable)
            )

        risk = risk_regression(old_step, new_step)
        if not risk.ok:
            failures.append(f"risk regression: {risk.reason}")

        return GateResult(
            passed=not failures,
            identity_ok=identity.preserved,
            effect_ok=effects.ok,
            risk_ok=risk.ok,
            failures=failures,
        )
