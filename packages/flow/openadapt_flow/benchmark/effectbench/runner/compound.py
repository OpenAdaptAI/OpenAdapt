"""Compound consequential-save oracle — one verdict over several sub-effects.

A real "save" is rarely one atomic assertion. The MockMed / EMR consequential
write the benchmark scores is a **compound** contract:

* ``record_written`` — exactly one NEW encounter for the target selector landed
  (at-most-once + no collateral loss); catches phantom, duplicate, double-click
  and lost-update faults; AND
* ``field_equals`` — that encounter's note reads back the run's exact value;
  catches a *partial save* that persists the row but drops a field.

:class:`~openadapt_flow.benchmark.effectbench.oracle.score_episode` verifies a
SINGLE :class:`Effect` through the oracle. This module supplies a thin
:class:`~openadapt_flow.runtime.effects.effect.EffectVerifier` that wraps ANY
concrete base verifier (the in-process
:class:`~openadapt_flow.benchmark.effectbench.oracle.RecordSnapshotOracle`, a
:class:`~openadapt_flow.runtime.effects.rest.RestRecordVerifier`, …) and, when
``score_episode`` calls :meth:`verify` with the primary ``record_written``
effect, ALSO checks the authored extra sub-effects against the SAME pre-state
snapshot, reducing the per-effect true-effect states with
:func:`~openadapt_flow.benchmark.effectbench.oracle.combine_true_states`.

It returns the single **deciding** :class:`EffectVerdict` whose
``effect_state`` equals the combined state (the fault-model precedence: the
primary ``record_written`` verdict decides unless it CONFIRMED, in which case
the first non-confirmed sub-effect decides). This is exactly the reduction the
reference re-expression (``benchmark/effectbench/reference_fault_model.py``)
performs inline, factored out so both the independent benchmark oracle AND an
arm's own product-side verifier can share it without duplicating the logic.
"""

from __future__ import annotations

from typing import Any, Sequence

from openadapt_flow.benchmark.effectbench.oracle import (
    TrueEffectState,
    combine_true_states,
    effect_state,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    EffectVerifier,
)


class CompoundEffectVerifier:
    """Verify a compound consequential-save contract through one base verifier.

    Args:
        base: The concrete :class:`EffectVerifier` that actually reads the
            system of record (an in-process snapshot oracle OR a REST readback).
            Every sub-effect is checked against the same base, against the same
            captured pre-state.
        extra_effects: The already-parameter-resolved sub-effects to check in
            ADDITION to the primary effect ``verify`` is called with (e.g. the
            ``field_equals`` note read-back). Order matters only for the
            deciding-verdict tie-break; the combined state is order-independent
            except for the primary/record precedence baked into
            :func:`combine_true_states`.
    """

    def __init__(
        self, base: EffectVerifier, *, extra_effects: Sequence[Effect] = ()
    ) -> None:
        self._base = base
        self._extra: tuple[Effect, ...] = tuple(extra_effects)
        #: Stable substrate name for audit — the base verifier's channel.
        self.substrate: str = base.substrate

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the system of record once, before the action (delegated)."""
        return self._base.capture_pre_state(context)

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Verify ``expected`` PLUS every extra sub-effect; return the decider.

        The primary ``expected`` effect (``score_episode`` passes the task's
        resolved ``record_written`` contract) is authoritative for the
        phantom-vs-wrong split: if it is not CONFIRMED, ITS verdict decides
        (an ABSENT record makes a note mismatch moot). If it CONFIRMED, the
        first non-confirmed sub-effect decides (a partial save shows up as the
        refuting note read-back), else the last (all-confirmed) verdict.
        """
        verdicts: list[EffectVerdict] = [self._base.verify(expected, before, context)]
        for eff in self._extra:
            verdicts.append(self._base.verify(eff, before, context))

        states = [effect_state(v) for v in verdicts]
        combined = combine_true_states(states[0], *states[1:])

        primary = verdicts[0]
        if not primary.confirmed:
            deciding = primary
        else:
            deciding = next((v for v in verdicts[1:] if not v.confirmed), verdicts[-1])

        # Invariant the reduction guarantees: the deciding verdict's scoreable
        # state is the combined state score_episode will classify on. Assert it
        # so a future change to combine precedence can never silently diverge.
        if effect_state(deciding) is not combined:  # contract guard, never expected
            raise AssertionError(
                f"compound decider state {effect_state(deciding)} != combined "
                f"{combined}; combine_true_states / deciding-verdict precedence "
                "have drifted apart"
            )
        return deciding


# Re-export for callers that reduce states directly.
__all__ = ["CompoundEffectVerifier", "TrueEffectState"]
