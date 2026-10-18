"""Route each effect to the read for the table or resource it concerns.

One save can write more than one place: the encounter row, a billing row, an
audit row. A single read sees one of them. A bad write to another place passes
any check built on that read alone (``benchmark/effect_e2e/EFFECT_E2E.md``: one
REST read let 9 of 72 bad writes through because they went to a table it did
not read).

:class:`SurfaceRoutedVerifier` holds one verifier per named surface and sends
each effect to the surface its ``probe`` names (``"surface=<name>"`` or
``"surface=<name>|free text"``). An effect that names no surface, or names one
that is not configured, is INDETERMINATE: it halts instead of being judged
against the wrong read.

The pre-state is captured once per surface and carried inside the returned
:class:`EffectState`, because the replayer captures one pre-state and passes
it to every ``verify`` call.

The evidence tier of a routed effect is the tier of the verifier it routes
to; the verifier as a whole advertises its weakest surface.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.verification import VerificationTier

SURFACE_PREFIX = "surface="


def surface_of(effect: Effect) -> Optional[str]:
    """The surface an effect's ``probe`` names, or ``None``."""

    probe = (effect.probe or "").strip()
    if not probe.startswith(SURFACE_PREFIX):
        return None
    name = probe[len(SURFACE_PREFIX) :].split("|", 1)[0].strip()
    return name or None


def surface_probe(name: str, note: str = "") -> str:
    """The ``probe`` value that routes an effect to ``name``."""

    return f"{SURFACE_PREFIX}{name}" + (f"|{note}" if note else "")


def _tier(verifier: Any) -> Optional[VerificationTier]:
    value = getattr(verifier, "verification_tier", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return VerificationTier(value)
    except (TypeError, ValueError):
        return None


class SurfaceRoutedVerifier:
    """One verifier per surface; each effect is judged by its own surface.

    Args:
        verifiers: Surface name -> a verifier implementing the
            :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier`
            protocol. At least one is required.
    """

    substrate = "surfaces"

    def __init__(self, verifiers: Mapping[str, Any]) -> None:
        if not verifiers:
            raise ValueError("a surface-routed verifier needs at least one surface")
        self._verifiers = dict(verifiers)
        tiers = [_tier(verifier) for verifier in self._verifiers.values()]
        declared = [tier for tier in tiers if tier is not None]
        if len(declared) != len(tiers):
            raise ValueError("every surface verifier must declare a verification tier")
        #: The weakest surface (highest number) sets the whole verifier's tier.
        self.verification_tier: VerificationTier = max(declared)
        self.independent_system_of_record = all(
            bool(getattr(verifier, "independent_system_of_record", True))
            for verifier in self._verifiers.values()
        )

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(self._verifiers)

    @property
    def verifiers(self) -> dict[str, Any]:
        """Surface name -> its verifier (a copy of the mapping)."""
        return dict(self._verifiers)

    def verification_tier_for(self, effect: Effect) -> VerificationTier:
        """The tier of the surface this effect routes to (weakest if none)."""

        name = surface_of(effect)
        verifier = self._verifiers.get(name or "")
        tier = _tier(verifier) if verifier is not None else None
        return tier if tier is not None else self.verification_tier

    def capture_pre_state(self, context: Any = None) -> EffectState:
        detail: dict[str, Any] = {}
        reachable = True
        for name, verifier in self._verifiers.items():
            state = verifier.capture_pre_state(context)
            detail[name] = {
                "substrate": state.substrate,
                "reachable": state.reachable,
                "records": state.records,
            }
            reachable = reachable and state.reachable
        return EffectState(
            substrate=self.substrate, reachable=reachable, records=[], detail=detail
        )

    def capture_post_state(self, context: Any = None) -> EffectState:
        return self.capture_pre_state(context)

    def test_connection(self, context: Any = None) -> Any:
        from openadapt_flow.runtime.effects.adapter import ConnectionProbe

        state = self.capture_pre_state(context)
        down = [
            name
            for name, sub in state.detail.items()
            if not (isinstance(sub, dict) and sub.get("reachable"))
        ]
        return ConnectionProbe(
            ok=not down,
            substrate=self.substrate,
            reason="reachable" if not down else "unreachable: " + ", ".join(down),
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        name = surface_of(expected)
        verifier = self._verifiers.get(name or "")
        if name is None or verifier is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    f"effect names surface {name!r}, which this check does not "
                    "read; HALT rather than judge it against another read"
                    if name
                    else "effect names no surface (probe 'surface=<name>'); "
                    "HALT rather than guess which read covers it"
                ),
            )
        sub = before.detail.get(name, {}) if isinstance(before.detail, dict) else {}
        sub_before = EffectState(
            substrate=str(sub.get("substrate") or getattr(verifier, "substrate", "")),
            reachable=bool(sub.get("reachable", False)),
            records=list(sub.get("records", [])),
        )
        return verifier.verify(expected, sub_before, context)
