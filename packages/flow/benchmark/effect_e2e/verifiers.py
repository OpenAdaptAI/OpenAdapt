"""A composite effect verifier that audits MORE than one record surface.

The default out-of-band record oracle (a single
:class:`~openadapt_flow.runtime.effects.RestRecordVerifier` over
``/api/records``) reads exactly one surface -- the encounters table -- and so
is structurally blind to a collateral write on another surface (billing). This
composite closes that gap the way a real deployment does: it routes each typed
:class:`Effect` to the read-only SQL verifier for the surface it concerns, so
the effect contract's read path covers EVERY mutable table.

It is used by the harness's ``effect_full`` arm and demonstrates, through the
REAL replayer, that effect verification catches the collateral-write class once
its read path is complete -- while remaining independent of the harness's
ground truth (a separate read-only connection and a separate classifier).

Routing is by an explicit ``Effect.probe`` marker (``"surface=<name>"``); an
effect with no such marker routes to the default surface.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)


def surface_of(effect: Effect, default: str) -> str:
    """The surface an effect targets, from its ``probe`` marker."""
    probe = effect.probe or ""
    if probe.startswith("surface="):
        return probe[len("surface=") :].split("|", 1)[0].strip()
    return default


class CompositeSqlVerifier:
    """Route each effect to the read-only verifier for its record surface.

    Args:
        verifiers: Mapping of surface name -> a verifier implementing the
            :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier`
            protocol (here read-only :class:`SqlRecordVerifier` instances, each
            on its own connection).
        default_surface: Surface for effects with no ``surface=`` probe marker.
    """

    substrate = "composite-sql"

    def __init__(self, verifiers: dict[str, Any], *, default_surface: str) -> None:
        self._verifiers = verifiers
        self._default = default_surface

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot every surface; encode each sub-state in ``detail``.

        The replayer captures the pre-state ONCE and passes the same object to
        every ``verify`` call, so the per-surface baselines are carried inside
        this returned state (stateless) and rebuilt in :meth:`verify`.
        """
        detail: dict[str, Any] = {}
        reachable = True
        for name, verifier in self._verifiers.items():
            state = verifier.capture_pre_state(context)
            detail[name] = {"reachable": state.reachable, "records": state.records}
            reachable = reachable and state.reachable
        return EffectState(
            substrate=self.substrate, reachable=reachable, records=[], detail=detail
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        name = surface_of(expected, self._default)
        verifier = self._verifiers[name]
        sub = before.detail.get(name, {})
        sub_before = EffectState(
            substrate=verifier.substrate,
            reachable=bool(sub.get("reachable", False)),
            records=list(sub.get("records", [])),
        )
        return verifier.verify(expected, sub_before, context)
