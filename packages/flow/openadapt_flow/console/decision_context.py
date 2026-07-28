"""The part of a halt that CAN cross a third-party relay, proved field by field.

``openadapt_flow.console.halt_detail`` already answers "what broke, what was
tried, and what a continuation re-proves" in a closed vocabulary — typed halt
category, step ordinal, action kind, control role, per-rung verdicts, and the
list of contracts the engine re-checks. It is currently returned only inside the
**local** ``presentation`` block, and
``human_decisions.portable_remote_decision_task`` discards it wholesale.

That wholesale discard was a boundary drawn around the *block*, not around its
*fields*. The block contains exactly one field that is not a closed enum, a
bounded integer, or a boolean: ``target_label``, the target control's own
accessible name, released by ``halt_detail._safe_target_label`` only after six
independent proofs. That single string is why the whole block stayed local.

This module re-derives the block **without it**. :class:`RemoteHaltContextV1`
has no string-valued field at all — every value is drawn from a closed
vocabulary pinned to the engine's own literals, a bounded integer, or a
boolean. A relay that stores this object is structurally incapable of
representing a patient name, an MRN, an observed value, a path, or a workflow
label, in precisely the same sense as
:class:`~openadapt_types.HumanDecisionTaskV1`. The claim is checkable by reading
the schema rather than by trusting a detector.

What the operator loses at this tier is one adjective: the phone says "OpenAdapt
could not find the button" instead of "the button labelled 'Open'". The label
stays available at :attr:`~openadapt_flow.decision_delivery.DecisionDeliveryTier.LOCAL_FULL`,
which is the boundary that already serves protected screenshot crops.

Fail-closed, in both directions
-------------------------------

Projection is a re-validation, never a copy. Every value is re-checked against
the closed vocabulary here even though ``halt_detail`` produced it, so a future
change there cannot silently widen what leaves the runner: an unrecognised
value is dropped to ``None`` (or the row is dropped), and a structurally
unusable block yields ``None`` rather than a partial object. Pydantic's
``extra="forbid"`` then refuses any key this module did not name.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.console.attention import _KNOWN_CATEGORIES
from openadapt_flow.console.halt_detail import (
    _CONTENT_ROLES,
    _CONTROL_ROLES,
    RUNG_ORDER,
    RecheckKind,
    RungEvidence,
    RungVerdict,
)
from openadapt_flow.ir import ActionKind

REMOTE_HALT_CONTEXT_SCHEMA: Literal["openadapt.remote-halt-context/v1"] = (
    "openadapt.remote-halt-context/v1"
)

#: Halt categories a remote surface may be told about. Taken from the console's
#: own set plus the two synthesised values ``attention`` can produce, so this
#: can never drift into a parallel vocabulary. ``tests/test_decision_context.py``
#: pins it against ``attention._KNOWN_CATEGORIES``.
_REMOTE_CATEGORIES = frozenset(_KNOWN_CATEGORIES) | {
    "operator_review",
    "delivery_uncertain",
}

#: Roles ``halt_detail`` may emit, both control and content. The role is a
#: *shape*, never a name: "the button", "the row".
_REMOTE_ROLES = frozenset(_CONTROL_ROLES) | frozenset(_CONTENT_ROLES)

_ACTION_KINDS = frozenset(kind.value for kind in ActionKind)
_RUNGS = frozenset(RUNG_ORDER)
_RUNG_EVIDENCE = frozenset(get_args(RungEvidence))
_RUNG_VERDICTS = frozenset(get_args(RungVerdict))
_RECHECK_KINDS = frozenset(get_args(RecheckKind))

#: Bound on every integer that crosses. A workflow with more steps than this,
#: or an identity contract requiring more signals, still halts and is still
#: answerable locally; the remote projection simply withholds the number rather
#: than emitting an unbounded one.
_MAX_COUNT = 10_000

#: Bound on both projected lists. The ladder is exactly ``len(RUNG_ORDER)`` and
#: ``will_recheck`` is drawn from a five-member closed set, so this is slack,
#: not a truncation the operator can notice.
_MAX_ROWS = 16


class RemoteResolutionRung(BaseModel):
    """One rung of the resolution ladder, as three closed enums."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rung: str = Field(min_length=1, max_length=32)
    evidence: str = Field(min_length=1, max_length=32)
    verdict: str = Field(min_length=1, max_length=32)


class RemoteRecheck(BaseModel):
    """One contract a continuation re-proves, and how many of it there are."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str = Field(min_length=1, max_length=48)
    count: Optional[int] = Field(default=None, ge=0, le=_MAX_COUNT)


class RemoteHaltContextV1(BaseModel):
    """Why the engine halted, in values that cannot carry protected content.

    Every field is a closed enum, a bounded integer, or a boolean. There is no
    free-text field, no image reference, no path, and no observed value. A
    consumer composes operator prose from these; the runner never sends prose.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.remote-halt-context/v1"] = (
        REMOTE_HALT_CONTEXT_SCHEMA
    )
    #: The engine's typed halt category.
    category: str = Field(min_length=1, max_length=48)
    #: Which step, and of how many. ``None`` when the run's manifest was
    #: encrypted or unreadable — the detail loses shape, never correctness.
    step_ordinal: Optional[int] = Field(default=None, ge=1, le=_MAX_COUNT)
    step_count: Optional[int] = Field(default=None, ge=1, le=_MAX_COUNT)
    #: What the step was going to do.
    action_kind: Optional[str] = Field(default=None, max_length=32)
    #: The target's *shape* — never its name.
    target_role: Optional[str] = Field(default=None, max_length=32)
    #: True when the target had an accessible name that this tier withholds.
    #: The consumer renders the role noun alone; it must not imply the element
    #: was unnamed.
    target_label_withheld: bool = False
    #: Strongest-first, one row per rung the engine knows about.
    resolution_ladder: list[RemoteResolutionRung] = Field(
        default_factory=list, max_length=_MAX_ROWS
    )
    #: What "I fixed it" actually buys.
    will_recheck: list[RemoteRecheck] = Field(
        default_factory=list, max_length=_MAX_ROWS
    )


def _closed(value: Any, allowed: frozenset[str]) -> Optional[str]:
    """Project one value onto a closed vocabulary, or drop it."""
    if not isinstance(value, str):
        return None
    return value if value in allowed else None


def _bounded(value: Any, *, minimum: int = 0) -> Optional[int]:
    """Project one value onto a bounded non-negative integer, or drop it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= _MAX_COUNT else None


def remote_halt_context(halt: Any) -> Optional[RemoteHaltContextV1]:
    """Project the local halt detail into its relay-safe subset.

    Args:
        halt: The ``presentation["halt"]`` mapping produced by
            :func:`openadapt_flow.console.halt_detail.halt_detail`, or any
            mapping-shaped value. Anything else yields ``None``.

    Returns:
        A :class:`RemoteHaltContextV1`, or ``None`` when the input is not a
        mapping or carries no recognised halt category. ``None`` means the
        remote surface is told nothing beyond the signed task — which is the
        pre-existing behaviour, not a degraded new one.

    Notes:
        ``target_label`` is read only to set ``target_label_withheld``; its
        value is never copied into the result. That is enforced structurally —
        :class:`RemoteHaltContextV1` has nowhere to put it.
    """
    if not isinstance(halt, dict):
        return None
    category = _closed(halt.get("category"), _REMOTE_CATEGORIES)
    if category is None:
        # An unrecognised category means this module's vocabulary and the
        # engine's have drifted. Withhold the whole block rather than emit a
        # context whose central claim is unverified.
        return None

    ladder: list[RemoteResolutionRung] = []
    raw_ladder = halt.get("resolution_ladder")
    if isinstance(raw_ladder, list):
        for row in raw_ladder[:_MAX_ROWS]:
            if not isinstance(row, dict):
                continue
            rung = _closed(row.get("rung"), _RUNGS)
            evidence = _closed(row.get("evidence"), _RUNG_EVIDENCE)
            verdict = _closed(row.get("verdict"), _RUNG_VERDICTS)
            if rung is None or evidence is None or verdict is None:
                continue
            ladder.append(
                RemoteResolutionRung(rung=rung, evidence=evidence, verdict=verdict)
            )

    rechecks: list[RemoteRecheck] = []
    raw_rechecks = halt.get("will_recheck")
    if isinstance(raw_rechecks, list):
        for row in raw_rechecks[:_MAX_ROWS]:
            if not isinstance(row, dict):
                continue
            check = _closed(row.get("check"), _RECHECK_KINDS)
            if check is None:
                continue
            rechecks.append(
                RemoteRecheck(check=check, count=_bounded(row.get("count")))
            )

    # A label the local tier withheld is still withheld here; a label the local
    # tier released is withheld *by this tier*. Either way the consumer is told
    # the element has a name it is not being shown, so it renders the role noun
    # rather than implying the control was anonymous.
    label_withheld = bool(halt.get("target_label_withheld")) or bool(
        halt.get("target_label")
    )

    return RemoteHaltContextV1(
        category=category,
        step_ordinal=_bounded(halt.get("step_ordinal"), minimum=1),
        step_count=_bounded(halt.get("step_count"), minimum=1),
        action_kind=_closed(halt.get("action_kind"), _ACTION_KINDS),
        target_role=_closed(halt.get("target_role"), _REMOTE_ROLES),
        target_label_withheld=label_withheld,
        resolution_ladder=ladder,
        will_recheck=rechecks,
    )
