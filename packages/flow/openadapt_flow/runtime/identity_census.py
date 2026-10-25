"""Does the verified identity pick one record? Count the look-alikes.

The identity ladder (:mod:`openadapt_flow.runtime.identity`) answers "does the
row at the resolved point read like the recorded row?". That is not the same
question as "is this the recorded record?". Two worklist rows that both read
"John Smith Follow-up Dr Lee" each match the recorded text exactly. When they
swap order, every resolution rung (structural, template, text) can land on the
other row, and the ladder verifies it, because the text cannot tell the two
apart.

So after the ladder verifies, the runtime counts the shown rows that carry the
same identity (``IdentityCandidatesBackend.identity_candidates_at`` for DOM
rows, an OCR pass over the frame for pixel-only screens) and decides:

* one such row: the identity picks one record; the verdict stands;
* several rows, and the bundle recorded the row's stable record id (an
  app-owned ``data-*`` id, the row link's path): act only on the one row with
  that id. When the resolver landed on another row, move to the right one
  (``retarget``) or, when it cannot move, refuse (``wrong_record``);
* several rows and no recorded id, or more than one row with the recorded id:
  ``ambiguous``. The step halts before it acts, whatever its risk;
* the rows could not all be counted: ``uncounted``, which also halts;
* the target is not a row (a standalone control, a form field):
  ``not_a_row``; the verdict stands;
* the surface cannot count rows at all (a backend without a census, a pixel
  identity with no OCR band to read): ``cannot_count``. The runtime halts a
  step that leads into a consequential write, and records it elsewhere.

A recorded id is required even when the row is unique: a single row with
another id is a different record that happens to read the same
(``wrong_record``).
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Mapping, NamedTuple, Optional

CensusVerdict = Literal[
    "unique",
    "retarget",
    "ambiguous",
    "uncounted",
    "wrong_record",
    "not_a_row",
    "cannot_count",
]


class CensusDecision(NamedTuple):
    """What the candidate census says about the resolved target.

    Attributes:
        verdict: See the module docstring.
        candidate_count: Shown rows with the target's identity, the target
            included; None when they could not be counted.
        offset: For ``retarget``, the ``(dx, dy)`` from the resolved row to
            the row with the recorded record id, in click coordinates.
    """

    verdict: CensusVerdict
    candidate_count: Optional[int] = None
    offset: Optional[tuple[float, float]] = None


def decide(
    census: Optional[Mapping[str, Any]],
    *,
    has_recorded_key: bool,
    key_matches: Callable[[Optional[str]], bool],
) -> CensusDecision:
    """Turn a backend census into a decision.

    Args:
        census: ``identity_candidates_at``'s result: ``{"row": False}`` when
            the point has no row identity, None when the census failed.
        has_recorded_key: Whether the bundle recorded the row's record id
            (and the identity is not bound to a run parameter).
        key_matches: Whether a live record id equals the recorded one.
    """
    if not isinstance(census, Mapping):
        return CensusDecision("cannot_count")
    if census.get("row") is False:
        return CensusDecision("not_a_row")
    if census.get("incomplete"):
        return CensusDecision("uncounted")
    raw = census.get("candidates")
    if not isinstance(raw, list):
        return CensusDecision("uncounted")
    candidates = [c for c in raw if isinstance(c, Mapping)]
    if len(candidates) != len(raw) or sum(bool(c.get("self")) for c in raw) != 1:
        # The row under the point must appear exactly once in its own census.
        return CensusDecision("uncounted")
    count = len(candidates)
    if has_recorded_key:
        matching = [c for c in candidates if key_matches(c.get("record_key"))]
        if len(matching) > 1:
            return CensusDecision("ambiguous", count)
        if not matching:
            return CensusDecision("wrong_record", count)
        chosen = matching[0]
        if chosen.get("self"):
            return CensusDecision("unique", count)
        if not census.get("top_level", True):
            return CensusDecision("wrong_record", count)
        try:
            offset = (float(chosen["dx"]), float(chosen["dy"]))
        except (KeyError, TypeError, ValueError):
            return CensusDecision("wrong_record", count)
        return CensusDecision("retarget", count, offset)
    if count == 1:
        return CensusDecision("unique", count)
    return CensusDecision("ambiguous", count)


def count_matching_rows(
    rows: list[tuple[int, str]],
    *,
    target_y: int,
    verifies: Callable[[str], bool],
    same_row_px: int,
) -> int:
    """Count OCR rows whose text verifies against the recorded identity.

    Pixel-only screens have no DOM to ask. Each OCR text row of the frame,
    other than the resolved target's own row (within ``same_row_px`` of
    ``target_y``), is checked with the same verifier the ladder used; the
    result counts the target too.

    Args:
        rows: ``(center_y, text)`` per OCR text row of the frame.
        target_y: The resolved point's y.
        verifies: Whether a row's text verifies as the recorded identity.
        same_row_px: Rows closer than this to ``target_y`` are the target's.
    """
    others = sum(
        1
        for y, text in rows
        if abs(y - target_y) >= same_row_px and text.strip() and verifies(text)
    )
    return others + 1


# Structural roles (backend-neutral, as the recorder stores them) of a form
# field or a control. A target recorded with one of these is not a record row.
CONTROL_ROLES = frozenset(
    {
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "spinbutton",
        "switch",
        "slider",
        "button",
        "tab",
        "menuitem",
    }
)


def teach_time_role(step: Any) -> Literal["row", "control", "unknown"]:
    """What teach time recorded the step's target as.

    ``row`` when the recorder saw a record row (the census at record time),
    ``control`` when it saw no row there, or when the target's structural
    locator names a form-field or control role (:data:`CONTROL_ROLES`).
    ``unknown`` otherwise, which the runtime counts like a row: a link, a
    cell, a list item, or a target with no recorded role.
    """
    anchor = getattr(step, "anchor", None)
    if anchor is None:
        return "unknown"
    template = getattr(anchor, "identity_template", None)
    recorded_row = getattr(template, "structured_row", None)
    if recorded_row is True:
        return "row"
    if recorded_row is False:
        return "control"
    if getattr(template, "structured", None):
        # A structured identity was recorded but nothing counted its rows (a
        # backend without a census, a legacy bundle): it may be a row's text.
        return "unknown"
    structural = getattr(anchor, "structural", None)
    role = (getattr(structural, "role", None) or "").strip().casefold()
    return "control" if role in CONTROL_ROLES else "unknown"
