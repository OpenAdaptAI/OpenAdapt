"""Classify the common local-edit patterns from what the recording shows.

Proposal: docs/design/LOCAL_EDIT_CLASSIFICATION.md. A compiled program flags
every special key and every unlabelled or navigation-shaped click for a
person's review, because the step alone can't prove its risk. Three patterns
recur in every form-filling program, and the recording can prove them local:

- ``select_all``: Control+A (or Meta+A) in a text field, right before typing
  into it;
- ``confirm_option``: Enter right after typing an option's label into a
  dropdown;
- ``in_form_tab``: a click on a tab of the same form.

A step is classified only when every piece of evidence holds: the field's
structural role, the steps around it, URL, title and page count unchanged
across the action, and no write request in its window (network capture is
required for Enter and for tab clicks, since either can submit). Anything
missing or mixed leaves the step for a person. A classified step gets an
outcome check (``local_edit``, and ``field_value`` for a dropdown) that halts
the run when, at run time, the action did anything else.

Model-free and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind, Step
from openadapt_flow.risk import LOCAL_EDIT_PREFIX, is_local_edit

__all__ = ["LOCAL_EDIT_PREFIX", "LocalEdit", "is_local_edit", "local_edits"]
TEXT_ROLES = frozenset({"textbox", "searchbox", "spinbutton"})
CHOICE_ROLES = frozenset({"combobox", "listbox"})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

Pattern = Literal["select_all", "confirm_option", "in_form_tab"]


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


def local_edits(
    steps: Sequence[Step],
    events: Sequence[dict[str, Any]],
    network: Optional[Sequence[dict[str, Any]]] = None,
) -> list[LocalEdit]:
    """The steps the recording proves are local edits, with their checks.

    ``network`` is the recording's captured requests (``network.jsonl``
    lines), or None when the recording has no capture.
    """

    found: list[LocalEdit] = []
    for k, step in enumerate(steps):
        if step.risk == "irreversible" or step.effects or step.api_binding:
            continue
        i = _event_index(step)
        if i is None or i >= len(events):
            continue
        event = events[i]
        if not _unchanged(event):
            continue
        prev = steps[k - 1] if k > 0 else None
        nxt = steps[k + 1] if k + 1 < len(steps) else None
        no_write = _no_write(events, i, network)
        if step.action is ActionKind.KEY:
            chord = _key_chord(step)
            target = _structural(prev)
            if (
                chord in {"a+control", "a+meta"}
                and prev is not None
                and prev.action is ActionKind.CLICK
                and target is not None
                and target.selector
                and (target.role or "").lower() in TEXT_ROLES
                and nxt is not None
                and nxt.action is ActionKind.TYPE
                and no_write is not False
            ):
                found.append(
                    LocalEdit(
                        step.id,
                        "select_all",
                        f"{LOCAL_EDIT_PREFIX} selects the text of the field "
                        f"{prev.id} clicked, before {nxt.id} types into it; "
                        "URL, title and page count unchanged in the recording"
                        + ("; no write request" if no_write else ""),
                        [
                            Postcondition(
                                kind=PostconditionKind.LOCAL_EDIT,
                                field_selector=target.selector,
                            )
                        ],
                    )
                )
                continue
            before_prev = steps[k - 2] if k > 1 else None
            choice = _structural(before_prev)
            typed_param = (
                events[_event_index(prev)].get("param")
                if prev is not None
                and _event_index(prev) is not None
                and _event_index(prev) < len(events)  # type: ignore[operator]
                else None
            )
            if (
                chord == "enter"
                and prev is not None
                and prev.action is ActionKind.TYPE
                and before_prev is not None
                and before_prev.action is ActionKind.CLICK
                and choice is not None
                and choice.selector
                and (choice.role or "").lower() in CHOICE_ROLES
                and isinstance(typed_param, str)
                and typed_param
                and no_write is True
            ):
                found.append(
                    LocalEdit(
                        step.id,
                        "confirm_option",
                        f"{LOCAL_EDIT_PREFIX} confirms the option typed into the "
                        f"dropdown {before_prev.id} clicked; URL, title and page "
                        "count unchanged and no write request in the recording",
                        [
                            Postcondition(
                                kind=PostconditionKind.LOCAL_EDIT,
                                field_selector=choice.selector,
                            ),
                            Postcondition(
                                kind=PostconditionKind.FIELD_VALUE,
                                param=typed_param,
                            ),
                        ],
                    )
                )
            continue
        if step.action is ActionKind.CLICK:
            target = _structural(step)
            role = (target.role or "").lower() if target is not None else ""
            # The next step that acts on a target (a scroll or wait between
            # them has none) must be in the same frame: the form is still
            # there.
            acting = next(
                (
                    later
                    for later in steps[k + 1 : k + 4]
                    if later.action not in {ActionKind.SCROLL, ActionKind.WAIT}
                ),
                None,
            )
            nxt = acting
            same_frame = (
                target is not None
                and acting is not None
                and _structural(acting) is not None
                and list(_structural(acting).frame_path) == list(target.frame_path)
            )
            if (
                target is not None
                and role in {"tab", "button"}
                and no_write is True
                and same_frame
                and (role == "tab" or not step.expect or _region_only(step))
            ):
                found.append(
                    LocalEdit(
                        step.id,
                        "in_form_tab",
                        f"{LOCAL_EDIT_PREFIX} switches the panel of the same form "
                        f"({role}); URL, title and page count unchanged, no write "
                        f"request, and {nxt.id if nxt else 'the next step'} acts in "
                        "the same frame in the recording",
                        [Postcondition(kind=PostconditionKind.LOCAL_EDIT)],
                    )
                )
    return found


def _region_only(step: Step) -> bool:
    """The click's only mined check is a region change (a panel switch)."""

    return all(p.kind is PostconditionKind.REGION_STABLE for p in step.expect)
