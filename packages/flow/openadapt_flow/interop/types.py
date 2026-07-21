"""openadapt-types interop shim — adopt the canonical action vocabulary at the boundary.

flow's :mod:`openadapt_flow.ir` is, and stays, the internal source of truth
(imported by 44 files, FROZEN in ``DESIGN.md``). This module is a thin,
*optional* boundary layer that translates flow's compiler IR to/from the
ecosystem's passive canonical schema, ``openadapt-types`` (``Action`` /
``ActionType`` / ``ActionTarget`` / ``ActionResult``), so a flow bundle can
interoperate with evals, emit, and any agent that speaks the shared vocabulary.

Design (per ``docs/ECOSYSTEM_INTEGRATION.md`` §2):

* **Adopt the words, keep the core.** flow's ``ActionKind`` (6 members) is a
  byte-identical *subset* of ``ActionType`` (21 members) — string values match
  exactly, so the enum map (:data:`ACTION_KIND_TO_ACTION_TYPE`) is trivial and
  exhaustive.
* **Drop compiler-only IR at the boundary.** ``Anchor`` (template crops,
  regions, OCR/context/structured identity, landmarks), ``Postcondition``,
  ``Resolution``, ``IdentityCheck``, ``HealEvent``, ``risk`` and
  ``identity_armed`` are net-new *compiler evidence* the passive canonical
  schema deliberately does not model. They are flow-only and are NOT smuggled
  into ``Action.raw``; the shared vocabulary carries only the shared fields.
* **Lazy, optional dependency.** ``openadapt_types`` is imported *inside* the
  functions, never at module import time, so importing this module stays
  cheap and the dependency stays optional (extra: ``openadapt-flow[interop]``).

Field map (flow ``Step`` -> ``Action``), shared fields only:

======================================  ==========================================
flow ``ir``                             ``openadapt-types``
======================================  ==========================================
``Step.action`` (``ActionKind``)        ``Action.type`` (``ActionType``), 1:1 value
``Step.intent``                         ``Action.reasoning`` (human-readable purpose)
``Anchor.click_point (x, y)``           ``Action.target = ActionTarget(x, y,``
                                        ``is_normalized=False)`` (pixel coords)
``Anchor.ocr_text`` / ``context_text``  ``ActionTarget.description`` (lossy; export)
``Step.text``                           ``Action.text``
``Step.param``                          ``Action.text = "{param}"`` placeholder when
                                        no literal ``text`` (TYPE needs non-empty text)
``Step.key``                            ``Action.key``
``Step.scroll_dx`` / ``scroll_dy``      ``Action.scroll_direction`` + ``scroll_amount``
======================================  ==========================================

Result map (flow ``StepResult`` -> ``ActionResult``):

======================================  ==========================================
flow ``ir``                             ``openadapt-types``
======================================  ==========================================
``StepResult.ok``                       ``ActionResult.success``
``StepResult.error``                    ``ActionResult.error``
``StepResult.elapsed_ms``               ``ActionResult.duration_ms`` (rounded int)
``StepResult.resolution.point``         ``ActionResult.resolved_coordinates``
(inferred from identity / postconditions / ``ActionResult.error_type``
resolution)
======================================  ==========================================

Reverse direction — :func:`action_to_step` — is provided but intentionally
**partial**: it reconstructs only the shared *vocabulary* (action kind, text,
key, scroll deltas, intent). It CANNOT reconstruct flow's visual ``Anchor``
(template crop + region + OCR evidence), ``Postcondition``, or identity gates
from a passive ``Action`` — those never existed in the canonical schema — so
the target coordinates in ``Action.target`` are dropped and the resulting
``Step`` has ``anchor=None`` and is **not replayable**. It exists for
ingest/round-trip of the shared vocabulary (evals -> flow), not to rebuild a
compiled bundle. ``Action`` types outside flow's 6-member ``ActionKind`` (e.g.
``right_click``, ``drag``, ``hotkey``, ``goto``) raise ``ValueError`` rather
than silently dropping an untranslatable action.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from openadapt_flow import ir

# A parameterized-TYPE placeholder Action.text of exactly ``{name}`` (emitted by
# ``step_to_action`` for a Step with a ``param`` and no literal text). Matched on
# the reverse so it round-trips back to ``Step.param`` instead of becoming
# literal characters a consumer would type verbatim.
_PARAM_PLACEHOLDER_RE = re.compile(r"^\{(\w+)\}$")

if TYPE_CHECKING:  # import-light: only for type checkers, never at runtime import
    from openadapt_types import Action, ActionResult

# The trivial, exhaustive enum map. flow's ActionKind values are a byte-identical
# subset of openadapt-types ActionType, so we key by the shared string value and
# resolve the ActionType member lazily (keeps this module import-light). Every
# ActionKind member appears here; a CI/regression test asserts exhaustiveness.
ACTION_KIND_TO_ACTION_TYPE: dict[ir.ActionKind, str] = {
    kind: kind.value for kind in ir.ActionKind
}


def _scroll_to_canonical(
    scroll_dx: Optional[int], scroll_dy: Optional[int]
) -> tuple[Optional[str], Optional[int]]:
    """Convert flow wheel deltas (px) to canonical (direction, amount).

    Vertical dominates when both axes are set (flow scrolls one axis per step).
    flow sign convention: dy > 0 == down, dx > 0 == right.
    """
    dx = scroll_dx or 0
    dy = scroll_dy or 0
    if dy != 0:
        return ("down" if dy > 0 else "up", abs(dy))
    if dx != 0:
        return ("right" if dx > 0 else "left", abs(dx))
    return (None, None)


def _canonical_to_scroll(
    direction: Optional[str], amount: Optional[int]
) -> tuple[Optional[int], Optional[int]]:
    """Inverse of :func:`_scroll_to_canonical` (direction/amount -> dx, dy)."""
    if direction is None or amount is None:
        return (None, None)
    mag = abs(amount)
    if direction == "down":
        return (None, mag)
    if direction == "up":
        return (None, -mag)
    if direction == "right":
        return (mag, None)
    if direction == "left":
        return (-mag, None)
    return (None, None)


def step_to_action(step: ir.Step) -> "Action":
    """Map a flow ``Step`` onto an ``openadapt_types.Action`` (shared fields).

    Preserves the shared vocabulary (action type, target coordinates, text,
    key, scroll, intent) and drops compiler-only evidence (anchor templates,
    postconditions, identity gates, risk). The returned ``Action`` passes
    openadapt-types' own per-type validators (TYPE needs text, KEY needs key).

    Args:
        step: A compiled flow step.

    Returns:
        A validated canonical ``Action``.

    Raises:
        ValueError: If openadapt-types cannot validate the action (should not
            happen for well-formed steps).
    """
    from openadapt_types import Action, ActionTarget, ActionType

    action_type = ActionType(ACTION_KIND_TO_ACTION_TYPE[step.action])

    # Target: canonical coordinates come from the anchor's click point. The
    # OCR/context label is exported as a (lossy) natural-language description.
    target: Optional[ActionTarget] = None
    if step.anchor is not None:
        x, y = step.anchor.click_point
        description = step.anchor.ocr_text or step.anchor.context_text
        target = ActionTarget(
            x=float(x), y=float(y), is_normalized=False, description=description
        )

    # Text: literal text if present; otherwise a param placeholder so a
    # parameterized TYPE step still yields a valid Action (TYPE requires
    # non-empty text). The param NAME is the only flow-specific bit surfaced,
    # and only because the canonical vocabulary has nowhere else to say
    # "this text is substituted at run time".
    text = step.text
    if action_type == ActionType.TYPE and not text and step.param:
        text = "{" + step.param + "}"

    scroll_direction, scroll_amount = _scroll_to_canonical(
        step.scroll_dx, step.scroll_dy
    )

    return Action(
        type=action_type,
        target=target,
        text=text,
        key=step.key,
        scroll_direction=scroll_direction,
        scroll_amount=scroll_amount,
        reasoning=step.intent or None,
    )


def result_to_action_result(step_result: ir.StepResult) -> "ActionResult":
    """Map a flow ``StepResult`` onto an ``openadapt_types.ActionResult``.

    Carries ``ok`` -> ``success``, ``error`` -> ``error``,
    ``elapsed_ms`` -> ``duration_ms``, and the resolved click point
    (``resolution.point``) -> ``resolved_coordinates``. ``error_type`` is a
    best-effort classification from flow's richer verdict fields (identity
    mismatch / failed postconditions / unresolved target); it is diagnostic,
    not part of the strict field map.

    Args:
        step_result: A flow per-step runtime result.

    Returns:
        A canonical ``ActionResult``.
    """
    from openadapt_types import ActionResult

    resolved_coordinates: Optional[tuple[int, int]] = None
    if step_result.resolution is not None:
        px, py = step_result.resolution.point
        resolved_coordinates = (int(px), int(py))

    duration_ms: Optional[int] = None
    if step_result.elapsed_ms:
        duration_ms = int(round(step_result.elapsed_ms))

    # error_type is a coarse, best-effort classification into the canonical
    # vocabulary; it is diagnostic, not a strict field. NOTE the vocabulary has
    # no distinct term for an identity (wrong-entity) mismatch vs a
    # postcondition (expected-end-state) miss — BOTH surface as
    # ``state_mismatch``, so a consumer must read ``error`` (or flow's own
    # StepResult.identity) to separate them, not error_type alone.
    error_type = None
    if not step_result.ok:
        identity = step_result.identity
        err = (step_result.error or "").lower()
        if "timeout" in err or "timed out" in err:
            error_type = "timeout"
        elif identity is not None and identity.status == "mismatch":
            error_type = "state_mismatch"  # wrong-entity (see NOTE above)
        elif step_result.postconditions_ok is False:
            error_type = "state_mismatch"  # postcondition miss (see NOTE above)
        elif step_result.resolution is None:
            error_type = "grounding_error"
        else:
            error_type = "execution_error"

    return ActionResult(
        success=step_result.ok,
        error=step_result.error,
        error_type=error_type,
        duration_ms=duration_ms,
        resolved_coordinates=resolved_coordinates,
    )


def action_to_step(action: "Action", *, step_id: str = "ingested") -> ir.Step:
    """Partial reverse hydrate: canonical ``Action`` -> flow ``Step`` (vocabulary only).

    Reconstructs ONLY the shared vocabulary — action kind, text, key, scroll
    deltas, and intent (from ``Action.reasoning``). It cannot rebuild flow's
    visual ``Anchor`` (template crop + region + OCR evidence), postconditions,
    or identity gates from a passive ``Action``, so the returned ``Step`` has
    ``anchor=None`` and is **not replayable**; the target coordinates in
    ``Action.target`` are intentionally dropped (flow locates targets via
    visual anchors, which an ``Action`` cannot supply). Use this for
    ingest/round-trip of the shared vocabulary (e.g. evals -> flow), not to
    reconstitute a compiled bundle.

    Args:
        action: A canonical ``Action``.
        step_id: Id to assign the reconstructed step.

    Returns:
        A partial flow ``Step`` carrying the shared fields.

    Raises:
        ValueError: If ``action.type`` is outside flow's 6-member ``ActionKind``
            (e.g. ``right_click``, ``drag``, ``hotkey``, ``goto``) — such
            actions are untranslatable and are refused rather than dropped.
    """
    try:
        kind = ir.ActionKind(action.type.value)
    except ValueError as exc:
        supported = ", ".join(k.value for k in ir.ActionKind)
        raise ValueError(
            f"Action type {action.type.value!r} has no flow ActionKind equivalent "
            f"(flow supports: {supported}); cannot ingest as a flow Step."
        ) from exc

    scroll_dx, scroll_dy = _canonical_to_scroll(
        action.scroll_direction, action.scroll_amount
    )

    # Reverse the parameterized-TYPE placeholder: an Action.text of exactly
    # "{name}" originated from Step.param (step_to_action), NOT from literal
    # typing. Restore it to param so a consumer substitutes the value at run
    # time rather than typing the characters "{name}" verbatim (the
    # placeholder round-trip corruption finding). A genuine literal that happens
    # to be "{name}" on a non-TYPE action is left as text.
    text: Optional[str] = action.text
    param: Optional[str] = None
    if kind is ir.ActionKind.TYPE and text is not None:
        m = _PARAM_PLACEHOLDER_RE.match(text)
        if m is not None:
            text, param = None, m.group(1)

    return ir.Step(
        id=step_id,
        intent=action.reasoning or "",
        action=kind,
        text=text,
        param=param,
        key=action.key,
        scroll_dx=scroll_dx,
        scroll_dy=scroll_dy,
    )
