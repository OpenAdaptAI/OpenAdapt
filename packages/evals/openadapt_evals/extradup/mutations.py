"""Operators over a gold CREATE: omit, extra, dup, unsubmit, claim.

``control`` is the unmutated gold write. Mutants construct a wrong effect.

``OPERATORS`` is frozen: the kill-scan corpus and the Phase-1 M-freeze pin it.
``EVAL_ONLY_OPERATORS`` holds families added after that freeze. They run in the
kit suite and in the published environment's eval dataset, never in the frozen
corpus and never in a training reward.

``wrong_record`` is the only operator that leaves the content alone. It
writes every correct field to a different patient. Cardinality matches gold,
the screen matches gold, and a content check matches gold. Only a read that
resolves the record by the contract's ``oracle_identity`` sees it.
"""

from __future__ import annotations

from typing import Any

from openadapt_evals.extradup.gold import WriteSpec, decoy_of
from openadapt_evals.extradup.store import Screen, Store

# ExtraDup operators named in the workshop paper, plus the clean control.
OPERATORS: tuple[str, ...] = (
    "control",
    "dup",
    "extra",
    "omit",
    "unsubmit",
    "claim",
)

MUTANTS: tuple[str, ...] = tuple(op for op in OPERATORS if op != "control")

# Eval-only families. They are NOT in OPERATORS, so they stay out of the
# frozen kill-scan corpus and out of the pre-registered mutant set that
# M_FREEZE_CERTIFIED_REWARD_RL_PILOT_2026_09_02.json pins. Adding one here
# does not amend that freeze. Same treatment the freeze gives identity_swap.
EVAL_ONLY_OPERATORS: tuple[str, ...] = ("wrong_record",)

ALL_OPERATORS: tuple[str, ...] = OPERATORS + EVAL_ONLY_OPERATORS


def apply(
    store: Store,
    spec: WriteSpec,
    operator: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Screen]:
    """Reset, apply ``operator`` to the gold CREATE, return (before, after, screen)."""
    if operator not in ALL_OPERATORS:
        raise KeyError(f"unknown ExtraDup operator {operator!r}")
    store.reset()
    before = store.snapshot()
    fields = dict(spec.fields)
    screen = Screen()
    if operator == "unsubmit":
        # Form is filled. Nothing is posted. WorkArena W-NO-SUBMIT.
        screen = Screen(form_looks_complete=True)
    elif operator == "claim":
        # Optimistic banner / agent self-report. Nothing lands.
        screen = Screen(banner_saved=True, claimed_success=True)
    else:
        if operator == "omit":
            fields.pop(spec.omit_field, None)
        elif operator == "extra":
            fields[spec.extra_field] = spec.extra_value
        elif operator == "wrong_record":
            # Right content, wrong chart. Nothing else changes.
            fields.update(decoy_of(spec))
        screen = store.write(fields)
        if operator == "dup":
            # Extra-NI: duplicate the non-idempotent CREATE.
            screen = store.write(fields)
    after = store.snapshot()
    return before, after, screen
