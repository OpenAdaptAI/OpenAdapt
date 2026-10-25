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

import re
from typing import Any, Callable, Literal, Mapping, NamedTuple, Optional, Protocol

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


# ---------------------------------------------------------------------------
# Row census over an accessibility tree (macOS AX, Linux AT-SPI)
# ---------------------------------------------------------------------------
#
# The browser backend counts look-alike rows in page JavaScript. A native
# accessibility tree is walked here instead, through a small adapter, so the
# rules are the same on every backend: find the record row that holds the
# target, read the same identity from every sibling row, and report each row
# that reads the same with its stable record id. The Windows agent runs in the
# guest without this package and carries a mirror of this function
# (backends/win_agent/server.py, ``_identity_candidates_at``).

#: Rows read before a census gives up and reports ``incomplete``.
TREE_CENSUS_ROW_CAP = 2000

_SINGLE_NUMBER = re.compile(r"^\D*(\d+)\D*$")
_POSITION_WORDS = (
    "row|item|idx|index|cell|col|column|line|pos|position|listviewitem|node|child"
)
# A whole token that is a position word with its number (row3, item_12 is
# split into item + 12) or a grid coordinate (r3c2).
_POSITION_TOKEN = re.compile(rf"^(?:(?:{_POSITION_WORDS})\d+|r\d+c\d+)$", re.IGNORECASE)
_POSITION_WORD_TOKEN = re.compile(rf"^(?:{_POSITION_WORDS}|r|c)$", re.IGNORECASE)


def positional_id(value: str, index: int) -> bool:
    """Whether a row id names a position rather than a record.

    Such an id moves with the row (``row-3``, ``r3c2``, ``item_12``, a bare
    ``3``): after the rows reorder or scroll it points at whichever record now
    sits there, so it never tells rows apart. The id is split into
    alphanumeric tokens. It is positional when a token is a position word with
    its number (``row3``, ``r3c2``), when a position word token is followed by
    a number token (``row-3``, ``item_12``, ``row-3-col-1``), when every token
    is a short number (grid coordinates such as ``3-1``), or when its only
    number is the row's index (0- or 1-based). UUIDs, ULIDs, MRNs and
    composite record ids (``ENC2024-000123``) are not.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", value) if t]
    if any(_POSITION_TOKEN.match(t) for t in tokens):
        return True
    for word, following in zip(tokens, tokens[1:]):
        if _POSITION_WORD_TOKEN.match(word) and following.isdigit():
            return True
    if len(tokens) > 1 and all(t.isdigit() and len(t) <= 3 for t in tokens):
        return True
    match = _SINGLE_NUMBER.match(value)
    return bool(match and int(match.group(1)) in (index, index + 1))


class TreeView(Protocol):
    """Read-only access to one accessibility tree, for :func:`tree_row_census`.

    Nodes are opaque. ``origin`` is the node's top-left in the backend's click
    coordinates, or None when it cannot be mapped. ``id_attributes`` lists the
    node's candidate record ids as ``(name, value)`` pairs (an AX identifier,
    an AT-SPI ``id`` attribute, a URL). ``row_count`` is the number of rows the
    container says it has, when it says (None otherwise). ``scrollable`` says
    whether a container without a row count can scroll rows out of view;
    anything but a definite False makes the census ``incomplete``.
    """

    def parent(self, node: Any) -> Any: ...

    def children(self, node: Any) -> list[Any]: ...

    def role(self, node: Any) -> Optional[str]: ...

    def origin(self, node: Any) -> Optional[tuple[float, float]]: ...

    def id_attributes(self, node: Any) -> list[tuple[str, str]]: ...

    def row_count(self, container: Any) -> Optional[int]: ...

    def scrollable(self, container: Any) -> Optional[bool]:
        """Whether the container can scroll rows out of view; None unknown."""
        ...


def _same(view: TreeView, a: Any, b: Any) -> bool:
    """Whether two node handles are the same node.

    A view MAY define ``same(a, b)``: AX hands back a fresh wrapper for the
    same element on every read, so equality there is ``CFEqual``.
    """
    if a is b:
        return True
    same = getattr(view, "same", None)
    if callable(same):
        try:
            return bool(same(a, b))
        except Exception:
            return False
    return bool(a == b)


def _record_key(view: TreeView, row: Any, path: list[int], index: int) -> Optional[str]:
    """The stable record id of ``row``: ids on the row and on the path to the
    target, minus values that are only the row's position."""

    parts: set[str] = set()
    node = row
    nodes = [row]
    for step in path:
        children = view.children(node)
        if step >= len(children):
            break
        node = children[step]
        nodes.append(node)
    for item in nodes:
        for name, value in view.id_attributes(item):
            value = (value or "").strip()
            if not value or len(value) > 256:
                continue
            if positional_id(value, index):
                continue
            parts.add(f"{name}={value}")
    return "\n".join(sorted(parts)) or None


def tree_row_census(
    target: Any,
    view: TreeView,
    *,
    row_roles: frozenset[str],
    identity: Callable[[Any, list[int]], Optional[str]],
    container_roles: frozenset[str] = frozenset(),
    max_depth: int = 8,
    max_rows: int = TREE_CENSUS_ROW_CAP,
) -> Optional[dict[str, Any]]:
    """Count the sibling rows that read the same identity as ``target``'s row.

    Args:
        target: The node under the point.
        view: The tree adapter.
        row_roles: Roles that make a node a record row.
        identity: ``identity(row, path)`` reads a row's identity the way the
            backend's ``structured_text_at`` does, where ``path`` is the child
            index path from the row to the target's place in it. None when the
            row has nothing to read there.
        container_roles: Roles of a list or table whose direct children are
            its rows even when they carry no row role (an ``AXList`` item).
        max_depth: How far above ``target`` to look for its row.
        max_rows: More sibling rows than this reports ``incomplete``.

    Returns:
        The census shape of ``IdentityCandidatesBackend.identity_candidates_at``,
        ``{"row": False}`` when the target is in no row, or None when the
        target's own identity cannot be read.
    """
    row = None
    previous = None
    node = target
    for _ in range(max_depth + 1):
        if node is None:
            break
        role = view.role(node)
        if role in row_roles:
            row = node
            break
        if role in container_roles and previous is not None:
            row = previous
            break
        previous, node = node, view.parent(node)
    if row is None:
        return {"row": False}
    path: list[int] = []
    node = target
    while not _same(view, node, row):
        parent = view.parent(node)
        if parent is None:
            return None
        siblings = view.children(parent)
        try:
            path.insert(
                0, next(i for i, c in enumerate(siblings) if _same(view, c, node))
            )
        except StopIteration:
            return None
        node = parent
    own_identity = identity(row, path)
    if not own_identity:
        return None
    container = view.parent(row)
    siblings = view.children(container) if container is not None else [row]
    row_role = view.role(row)
    rows = [c for c in siblings if _same(view, c, row) or view.role(c) == row_role]
    declared = view.row_count(container) if container is not None else None
    if declared is not None:
        hidden_rows = declared > len(rows)
    elif container is None:
        hidden_rows = False
    else:
        # No row count to check against: only a container that cannot
        # scroll shows every row it has. Unknown scrollability is incomplete.
        try:
            hidden_rows = view.scrollable(container) is not False
        except Exception:
            hidden_rows = True
    incomplete = len(rows) > max_rows or hidden_rows
    row_index = next((i for i, c in enumerate(rows) if _same(view, c, row)), 0)
    result: dict[str, Any] = {
        "record_key": _record_key(view, row, path, row_index),
        "incomplete": incomplete,
        "top_level": True,
        "candidates": [],
    }
    if incomplete:
        return result
    origin = view.origin(row)
    for index, candidate in enumerate(rows):
        is_row = _same(view, candidate, row)
        if is_row:
            observed: Optional[str] = own_identity
        else:
            node = candidate
            for step in path:
                children = view.children(node)
                if step >= len(children):
                    # The row is not populated where the target sits (a lazily
                    # built row): its identity cannot be read, so not every
                    # row was counted.
                    result["incomplete"] = True
                    result["candidates"] = []
                    return result
                node = children[step]
            observed = identity(candidate, path)
        if observed != own_identity:
            continue
        here = view.origin(candidate)
        offset = (
            (here[0] - origin[0], here[1] - origin[1])
            if here is not None and origin is not None
            else (None, None)
        )
        result["candidates"].append(
            {
                "self": is_row,
                "record_key": (
                    result["record_key"]
                    if is_row
                    else _record_key(view, candidate, path, index)
                ),
                "dx": offset[0],
                "dy": offset[1],
            }
        )
    return result


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
