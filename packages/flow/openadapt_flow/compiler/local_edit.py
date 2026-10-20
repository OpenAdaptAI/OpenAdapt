"""Classify the common local-edit patterns from what the recording shows.

Proposal: docs/design/LOCAL_EDIT_CLASSIFICATION.md. A compiled program flags
every special key and every unlabelled or navigation-shaped click for a
person's review, because the step alone can't prove its risk. A few patterns
recur in every form-filling program, and the recording can prove them local:

- ``select_all``: Control+A (or Meta+A) in a text field, right before typing
  into it;
- ``confirm_option``: Enter right after typing an option's label into a
  dropdown;
- ``confirm_entry``: Enter right after typing into a text field;
- ``in_form_tab``: a click on a tab of the same form;
- ``dismiss_dialog``: the dismiss of an informational dialog ("Continue to
  tasks", "Got it", "Close").

A step is classified only when every piece of evidence holds: the field's
structural role, the steps around it, URL, title and page count unchanged
across the action, the record store unchanged when the recorder snapshotted
it, and network capture showing no write request in its window. Anything
missing or mixed leaves the step for a person. A classified step gets an
outcome check (``local_edit``, plus ``field_value`` after Enter, or the
dismissed label gone after a dismiss) that halts the run when, at run time,
the action did anything else.

Model-free and deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind, Step
from openadapt_flow.risk import (
    DISMISS_PREFIX,
    LOCAL_EDIT_PREFIX,
    infer_step_risk,
    is_local_edit,
    only_weak_write_words,
    step_text,
)

__all__ = ["LOCAL_EDIT_PREFIX", "LocalEdit", "is_local_edit", "local_edits"]
TEXT_ROLES = frozenset({"textbox", "searchbox", "spinbutton"})
CHOICE_ROLES = frozenset({"combobox", "listbox"})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

Pattern = Literal[
    "select_all", "confirm_option", "confirm_entry", "in_form_tab", "dismiss_dialog"
]


@dataclass(frozen=True)
class LocalEdit:
    step_id: str
    pattern: Pattern
    explanation: str
    postconditions: list[Postcondition] = field(default_factory=list)


def _event_index(step: Step) -> Optional[int]:
    prefix, _, number = step.id.rpartition("_")
    return int(number) if prefix == "step" and number.isdigit() else None


def _key_chord(step: Step) -> str:
    parts = [p.strip().lower() for p in (step.key or "").split("+") if p.strip()]
    parts += [m.lower() for m in step.modifiers]
    return "+".join(sorted(set(parts)))


def _structural(step: Optional[Step]) -> Any:
    if step is None or step.anchor is None:
        return None
    return step.anchor.structural


def _unchanged(event: dict[str, Any]) -> bool:
    """URL, title and page count were recorded on both sides and didn't change."""

    for before, after in (
        ("url_before", "url_after"),
        ("title_before", "title_after"),
        ("pages_before", "pages_after"),
    ):
        if event.get(before) is None or event.get(after) is None:
            return False
        if event[before] != event[after]:
            return False
    return True


def _no_write(
    events: Sequence[dict[str, Any]],
    i: int,
    network: Optional[Sequence[dict[str, Any]]],
) -> Optional[bool]:
    """No write request in the action's window, or None without capture.

    The window is from the previous event to the next one, as wide as the
    API proposal uses for a step's reads: a request the action caused can
    finish just after its event was stamped.
    """

    if network is None:
        return None
    lo = float(events[i - 1]["t"]) if i > 0 else float("-inf")
    hi = float(events[i + 1]["t"]) if i + 1 < len(events) else float("inf")
    for line in network:
        method = str(line.get("method") or "").upper()
        if method in _READ_METHODS:
            continue
        try:
            t = float(line["t"])
        except (KeyError, TypeError, ValueError):
            return False
        if lo < t <= hi:
            return False
    return True


#: A dismiss control's label: it closes an informational dialog. ``continue``
#: and ``next`` are write words for the risk inference; only these two may be
#: reconsidered, and only with the full evidence below.
_DISMISS_LABEL = re.compile(
    r"^\s*(?:continue(?:\s+to\s+\w+(?:\s+\w+)?)?|next|close|ok(?:ay)?|got\s+it|"
    r"dismiss|not\s+now|maybe\s+later|skip\s+for\s+now)\s*$",
    re.IGNORECASE,
)


def _store_unchanged(event: dict[str, Any]) -> Optional[bool]:
    """The record store snapshots around the step, when the recorder took them.

    True when both were taken and are equal, False when they differ (or only
    one was taken), None when neither was taken.
    """

    from openadapt_flow.compiler.effect_mining import SOR_AFTER_KEY, SOR_BEFORE_KEY

    before, after = event.get(SOR_BEFORE_KEY), event.get(SOR_AFTER_KEY)
    if before is None and after is None:
        return None
    return before is not None and before == after


def _weak_write_dismiss(step: Step) -> bool:
    """An irreversible click that is irreversible only for "continue" or "next".

    Its risk must still be the per-step keyword inference, unchanged: a step a
    later rule made irreversible (the click that confirms a dialog, an
    icon-only click presumed to save) is never reconsidered, and any other
    write word in its context still wins.
    """

    if step.action is not ActionKind.CLICK:
        return False
    if not _DISMISS_LABEL.match(_label(step) or ""):
        return False
    if not only_weak_write_words(step_text(step)):
        return False
    inference = infer_step_risk(step)
    return (
        inference.risk == "irreversible"
        and inference.explanation == step.risk_explanation
        and any(e.startswith("write_keyword:") for e in inference.evidence)
    )


def _typed_param(
    events: Sequence[dict[str, Any]], step: Optional[Step]
) -> Optional[str]:
    if step is None or step.action is not ActionKind.TYPE:
        return None
    i = _event_index(step)
    if i is None or i >= len(events):
        return None
    value = events[i].get("param") or step.param
    return value if isinstance(value, str) and value else None


def _frame(step: Optional[Step]) -> Optional[list[str]]:
    target = _structural(step)
    if target is None:
        return None
    return list(target.frame_path or [])


def local_edits(
    steps: Sequence[Step],
    events: Sequence[dict[str, Any]],
    network: Optional[Sequence[dict[str, Any]]] = None,
) -> list[LocalEdit]:
    """The steps the recording proves are local edits, with their checks.

    ``network`` is the recording's captured requests (``network.jsonl``
    lines), or None when the recording has no capture. Every pattern needs
    both halves of the evidence:

    - the recording shows no mutating request in the step's window (network
      capture is required), URL, title and page count unchanged, and, when
      the recorder snapshotted the record store, the store unchanged;
    - a check that the step made only the expected local change, which the
      runtime runs every time (focus and field value, or the page unchanged
      and the dialog gone).

    Anything short of that is left for a person.
    """

    found: list[LocalEdit] = []
    for k, step in enumerate(steps):
        if step.effects or step.api_binding:
            continue
        i = _event_index(step)
        if i is None or i >= len(events):
            continue
        event = events[i]
        if not _unchanged(event):
            continue
        if _no_write(events, i, network) is not True:
            continue
        if _store_unchanged(event) is False:
            continue
        if step.risk == "irreversible" and not _weak_write_dismiss(step):
            continue
        prev = steps[k - 1] if k > 0 else None
        before_prev = steps[k - 2] if k > 1 else None
        nxt = steps[k + 1] if k + 1 < len(steps) else None
        edit = (
            _key_edit(step, prev, before_prev, nxt, events)
            if step.action is ActionKind.KEY
            else _click_edit(step, steps, k)
            if step.action is ActionKind.CLICK
            else None
        )
        if edit is not None:
            found.append(edit)
    return found


_EVIDENCE = "URL, title and page count unchanged and no write request in the recording"


def _key_edit(
    step: Step,
    prev: Optional[Step],
    before_prev: Optional[Step],
    nxt: Optional[Step],
    events: Sequence[dict[str, Any]],
) -> Optional[LocalEdit]:
    chord = _key_chord(step)
    clicked = _structural(prev)
    if (
        chord in {"a+control", "a+meta"}
        and prev is not None
        and prev.action is ActionKind.CLICK
        and clicked is not None
        and clicked.selector
        and (clicked.role or "").lower() in TEXT_ROLES
        and nxt is not None
        and nxt.action is ActionKind.TYPE
    ):
        # The next step's exact typed-input read-back checks the field's
        # whole value; this one checks focus stayed in the field.
        return LocalEdit(
            step.id,
            "select_all",
            f"{LOCAL_EDIT_PREFIX} selects the text of the field {prev.id} "
            f"clicked, before {nxt.id} types into it; {_EVIDENCE}",
            [
                Postcondition(
                    kind=PostconditionKind.LOCAL_EDIT,
                    field_selector=clicked.selector,
                )
            ],
        )
    field = _structural(before_prev)
    typed = _typed_param(events, prev)
    role = (field.role or "").lower() if field is not None else ""
    if (
        chord == "enter"
        and typed is not None
        and before_prev is not None
        and before_prev.action is ActionKind.CLICK
        and field is not None
        and field.selector
        and role in (CHOICE_ROLES | TEXT_ROLES)
    ):
        kind = "dropdown" if role in CHOICE_ROLES else "field"
        return LocalEdit(
            step.id,
            "confirm_option" if role in CHOICE_ROLES else "confirm_entry",
            f"{LOCAL_EDIT_PREFIX} confirms the value typed into the {kind} "
            f"{before_prev.id} clicked; {_EVIDENCE}",
            [
                Postcondition(
                    kind=PostconditionKind.LOCAL_EDIT, field_selector=field.selector
                ),
                Postcondition(kind=PostconditionKind.FIELD_VALUE, param=typed),
            ],
        )
    return None


def _label(step: Step) -> Optional[str]:
    target = _structural(step)
    if target is not None and target.name:
        return str(target.name)
    return step.anchor.ocr_text if step.anchor is not None else None


def _click_edit(step: Step, steps: Sequence[Step], k: int) -> Optional[LocalEdit]:
    target = _structural(step)
    if target is None:
        return None
    role = (target.role or "").lower()
    label = _label(step) or ""
    if role in {"button", "link"} and _DISMISS_LABEL.match(label):
        # An informational dialog's dismiss (MockMed's What's New "Continue
        # to tasks"): the page stays, and the control is gone afterwards.
        return LocalEdit(
            step.id,
            "dismiss_dialog",
            f"{DISMISS_PREFIX} ({label.strip()!r}); {_EVIDENCE}",
            [
                Postcondition(kind=PostconditionKind.LOCAL_EDIT),
                Postcondition(kind=PostconditionKind.TEXT_ABSENT, text=label.strip()),
            ],
        )
    # The next step that acts on a target (a scroll or wait between them has
    # none) must be in the same frame: the form is still there.
    acting = next(
        (
            later
            for later in steps[k + 1 : k + 4]
            if later.action not in {ActionKind.SCROLL, ActionKind.WAIT}
        ),
        None,
    )
    if (
        role in {"tab", "button"}
        and acting is not None
        and _frame(acting) is not None
        and _frame(acting) == _frame(step)
        and (role == "tab" or not step.expect or _region_only(step))
    ):
        return LocalEdit(
            step.id,
            "in_form_tab",
            f"{LOCAL_EDIT_PREFIX} switches the panel of the same form ({role}), "
            f"and {acting.id} acts in the same frame; {_EVIDENCE}",
            [Postcondition(kind=PostconditionKind.LOCAL_EDIT)],
        )
    return None


def _region_only(step: Step) -> bool:
    """The click's only mined check is a region change (a panel switch)."""

    return all(p.kind is PostconditionKind.REGION_STABLE for p in step.expect)
