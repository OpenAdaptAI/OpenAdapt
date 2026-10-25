"""Row census on native accessibility trees (macOS AX, Linux AT-SPI, Windows UIA).

A worklist of two rows that read "John Smith Follow-up Dr Lee" must halt
before it opens either chart, distinct record ids must pick the recorded row,
and a unique row must still run. Each backend is exercised through its real
census code over a fake accessibility tree; the browser census has its own
real-browser tests (tests/test_realism_browser.py).
"""

from __future__ import annotations

import types
from typing import Any, Optional

import pytest

from openadapt_flow.backends import linux_backend
from openadapt_flow.backends.win_agent import server
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.runtime import identity_template as itmpl
from openadapt_flow.runtime.identity_census import (
    CensusDecision,
    decide,
    tree_row_census,
)
from openadapt_flow.runtime.replayer import Replayer

ROW = ("John Smith", "Follow-up", "Dr Lee")
OTHER = ("Maria Garcia", "Intake", "Dr Lee")


def _decide(census: Optional[dict], recorded_key: Optional[str] = None):
    tmpl = itmpl.build_identity_template(
        None, structured_identity="x", record_key=recorded_key
    )
    assert tmpl is not None
    return decide(
        census,
        has_recorded_key=tmpl.record_key is not None,
        key_matches=lambda key: itmpl.record_key_matches(tmpl, key),
    )


# -- generic tree census -------------------------------------------------------


class Node:
    def __init__(self, role: str, text: str = "", *, ids=(), y: float = 0.0):
        self.role = role
        self.text = text
        self.ids = list(ids)
        self.y = y
        self.parent: Optional[Node] = None
        self.kids: list[Node] = []
        self.declared_rows: Optional[int] = None

    def add(self, *children: "Node") -> "Node":
        for child in children:
            child.parent = self
            self.kids.append(child)
        return self


class View:
    def parent(self, node: Node) -> Any:
        return node.parent

    def children(self, node: Node) -> list[Any]:
        return list(node.kids)

    def role(self, node: Node) -> Optional[str]:
        return node.role

    def origin(self, node: Node) -> Optional[tuple[float, float]]:
        return (0.0, node.y)

    def id_attributes(self, node: Node) -> list[tuple[str, str]]:
        return [("id", value) for value in node.ids]

    def row_count(self, container: Node) -> Optional[int]:
        return container.declared_rows


def _table(*rows: tuple[tuple[str, ...], tuple[str, ...]]) -> Node:
    """A table of rows ``(cells, row ids)``, 40 px apart."""

    table = Node("table")
    for index, (cells, ids) in enumerate(rows):
        row = Node("row", ids=ids, y=40.0 * index)
        row.add(*(Node("cell", text) for text in cells))
        table.add(row)
    return table


def _row_identity(row: Node, path: list[int]) -> Optional[str]:
    """Row text except the target's cell (the Windows/Linux reading)."""

    return " ".join(c.text for i, c in enumerate(row.kids) if i != path[0]) or None


def _census(target: Node) -> Optional[dict]:
    return tree_row_census(
        target, View(), row_roles=frozenset({"row"}), identity=_row_identity
    )


def test_two_identical_rows_are_ambiguous() -> None:
    table = _table((ROW, ()), (ROW, ()), (OTHER, ()))
    census = _census(table.kids[1].kids[0])
    assert census is not None and len(census["candidates"]) == 2
    assert _decide(census) == CensusDecision("ambiguous", 2)


def test_three_identical_rows_are_ambiguous() -> None:
    table = _table((ROW, ()), (OTHER, ()), (ROW, ()), (ROW, ()))
    assert _decide(_census(table.kids[0].kids[0])) == CensusDecision("ambiguous", 3)


def test_distinct_record_ids_pick_the_recorded_row() -> None:
    table = _table((ROW, ("chart-1002",)), (ROW, ("chart-1001",)))
    census = _census(table.kids[0].kids[0])  # resolver landed on 1002
    assert _decide(census, "id=chart-1001") == CensusDecision(
        "retarget", 2, (0.0, 40.0)
    )
    assert _decide(_census(table.kids[1].kids[0]), "id=chart-1001") == (
        CensusDecision("unique", 2)
    )


def test_a_unique_row_runs() -> None:
    table = _table((ROW, ()), (OTHER, ()))
    assert _decide(_census(table.kids[1].kids[2])) == CensusDecision("unique", 1)


def test_a_control_outside_any_row_is_not_a_row() -> None:
    window = Node("window").add(Node("button", "Sign visit note"))
    assert _census(window.kids[0]) == {"row": False}


def test_positional_ids_never_tell_rows_apart() -> None:
    table = _table((ROW, ("row-0",)), (ROW, ("row-1",)))
    census = _census(table.kids[0].kids[0])
    assert census is not None
    assert [c["record_key"] for c in census["candidates"]] == [None, None]
    assert _decide(census).verdict == "ambiguous"


def test_a_table_with_more_rows_than_it_exposes_is_incomplete() -> None:
    table = _table((ROW, ()), (OTHER, ()))
    table.declared_rows = 50  # a virtualized list: rows built on scroll
    census = _census(table.kids[1].kids[0])
    assert census is not None and census["incomplete"] is True
    assert _decide(census).verdict == "uncounted"


def test_a_row_whose_cells_are_not_built_is_incomplete() -> None:
    table = _table((ROW, ()), (OTHER, ()))
    table.add(Node("row", y=80.0))  # placeholder row, no cells yet
    census = _census(table.kids[0].kids[2])
    assert census is not None and census["incomplete"] is True


def test_list_items_without_a_row_role_are_rows_of_their_list() -> None:
    """macOS AXList: the list's direct children are its rows."""

    listing = Node("AXList")
    for index, name in enumerate(("John Smith", "John Smith", "Maria Garcia")):
        item = Node("AXGroup", y=30.0 * index).add(Node("AXStaticText", name))
        listing.add(item)

    def own_text(row: Node, path: list[int]) -> Optional[str]:
        node = row
        for step in path:
            node = node.kids[step]
        return node.text or None

    census = tree_row_census(
        listing.kids[2].kids[0],
        View(),
        row_roles=frozenset({"AXRow"}),
        container_roles=frozenset({"AXList"}),
        identity=own_text,
    )
    assert _decide(census) == CensusDecision("unique", 1)
    census = tree_row_census(
        listing.kids[0].kids[0],
        View(),
        row_roles=frozenset({"AXRow"}),
        container_roles=frozenset({"AXList"}),
        identity=own_text,
    )
    assert _decide(census) == CensusDecision("ambiguous", 2)


# -- Linux AT-SPI client -------------------------------------------------------


class _States:
    def __init__(self, names: set[str]):
        self._names = names

    def contains(self, state: str) -> bool:
        return state in self._names


class _Extents:
    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.width, self.height = x, y, w, h


class AtspiNode:
    def __init__(self, role: str, name: str = "", *, bounds=(0, 0, 10, 10), ident=""):
        self._role = role
        self._name = name
        self._bounds = bounds
        self._ident = ident
        self._parent: Optional[AtspiNode] = None
        self._kids: list[AtspiNode] = []

    def add(self, *kids: "AtspiNode") -> "AtspiNode":
        for kid in kids:
            kid._parent = self
            self._kids.append(kid)
        return self

    def get_role_name(self) -> str:
        return self._role

    def get_name(self) -> str:
        return self._name

    def get_parent(self):
        return self._parent

    def get_child_count(self) -> int:
        return len(self._kids)

    def get_child_at_index(self, index: int):
        return self._kids[index]

    def get_state_set(self) -> _States:
        return _States({"VISIBLE", "SHOWING", "ENABLED"})

    def get_component_iface(self):
        node = self

        class _Component:
            def get_extents(self, _coord):
                return _Extents(*node._bounds)

        return _Component()

    def get_accessible_id(self) -> str:
        return self._ident


def _atspi_client() -> linux_backend.AtspiLinuxClient:
    client = object.__new__(linux_backend.AtspiLinuxClient)
    client._atspi = types.SimpleNamespace(
        StateType=types.SimpleNamespace(
            VISIBLE="VISIBLE", SHOWING="SHOWING", ENABLED="ENABLED", FOCUSED="FOCUSED"
        ),
        CoordType=types.SimpleNamespace(SCREEN="SCREEN"),
    )
    return client


def _atspi_window(*rows: tuple[tuple[str, ...], str]):
    listing = AtspiNode("list", bounds=(0, 0, 400, 300))
    for index, (cells, ident) in enumerate(rows):
        top = 40 * index
        row = AtspiNode("list item", bounds=(0, top, 400, 40), ident=ident)
        for column, text in enumerate(cells):
            row.add(AtspiNode("label", text, bounds=(100 * column, top, 100, 40)))
        listing.add(row)
    frame = AtspiNode("frame", "Worklist", bounds=(0, 0, 400, 300)).add(listing)
    return linux_backend.LinuxWindow(
        "w1", "app", "Worklist", 1, (0, 0, 400, 300), frame
    )


def test_linux_identical_rows_are_ambiguous() -> None:
    window = _atspi_window((ROW, ""), (ROW, ""), (OTHER, ""))
    census = _atspi_client().row_census(window, 150, 50, limit=100)
    assert _decide(census) == CensusDecision("ambiguous", 2)


def test_linux_record_ids_pick_the_recorded_row() -> None:
    window = _atspi_window((ROW, "chart-1002"), (ROW, "chart-1001"))
    census = _atspi_client().row_census(window, 150, 10, limit=100)
    assert _decide(census, "atspi-id=chart-1001") == CensusDecision(
        "retarget", 2, (0.0, 40.0)
    )


def test_linux_unique_row_runs_and_matches_structured_text() -> None:
    client = _atspi_client()
    window = _atspi_window((ROW, ""), (OTHER, ""))
    element = client.element_at_point(window, 150, 50)
    assert element is not None
    # The census reads rows exactly as the identity check does.
    assert client.structured_text(element) == "Maria Garcia Dr Lee"
    census = client.row_census(window, 150, 50, limit=100)
    assert _decide(census) == CensusDecision("unique", 1)


# -- Windows UIA agent ---------------------------------------------------------


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class UiaControl:
    def __init__(self, control_type: str, name: str = "", *, aid: str = "", top=0):
        self.ControlTypeName = control_type
        self.Name = name
        self.AutomationId = aid
        self.BoundingRectangle = _Rect(0, top, 400, top + 40)
        self.parent: Optional[UiaControl] = None
        self.children: list[UiaControl] = []
        self.grid_rows: Optional[int] = None
        self.virtual = False

    def add(self, *kids: "UiaControl") -> "UiaControl":
        for kid in kids:
            kid.parent = self
            self.children.append(kid)
        return self

    def GetParentControl(self):
        return self.parent

    def GetChildren(self):
        return list(self.children)

    def GetGridPattern(self):
        if self.grid_rows is None:
            raise RuntimeError("no grid pattern")
        return types.SimpleNamespace(RowCount=self.grid_rows)

    def GetScrollPattern(self):
        return types.SimpleNamespace(VerticallyScrollable=self.virtual)

    def GetItemContainerPattern(self):
        return object() if self.virtual else None


def _uia_list(*rows: tuple[tuple[str, ...], str]):
    listing = UiaControl("ListControl")
    for index, (cells, aid) in enumerate(rows):
        item = UiaControl("DataItemControl", aid=aid, top=40 * index)
        item.add(*(UiaControl("TextControl", text, top=40 * index) for text in cells))
        listing.add(item)
    return listing


def _auto(target: UiaControl):
    return types.SimpleNamespace(ControlFromPoint=lambda _x, _y: target)


def test_windows_identical_rows_are_ambiguous() -> None:
    listing = _uia_list((ROW, ""), (ROW, ""), (ROW, ""))
    census = server._identity_candidates_at(
        _auto(listing.children[1].children[0]), 5, 45
    )
    assert _decide(census) == CensusDecision("ambiguous", 3)


def test_windows_automation_ids_pick_the_recorded_row() -> None:
    listing = _uia_list((ROW, "chart-1002"), (ROW, "chart-1001"))
    census = server._identity_candidates_at(
        _auto(listing.children[0].children[0]), 5, 5
    )
    assert _decide(census, "automation-id=chart-1001") == CensusDecision(
        "retarget", 2, (0.0, 40.0)
    )


def test_windows_positional_automation_ids_do_not_tell_rows_apart() -> None:
    listing = _uia_list((ROW, "ListViewItem-0"), (ROW, "ListViewItem-1"))
    census = server._identity_candidates_at(
        _auto(listing.children[0].children[0]), 5, 5
    )
    assert _decide(census).verdict == "ambiguous"


def test_windows_unique_row_runs_and_matches_structured_text() -> None:
    listing = _uia_list((ROW, ""), (OTHER, ""))
    target = listing.children[1].children[0]
    auto = _auto(target)
    census = server._identity_candidates_at(auto, 5, 45)
    assert _decide(census) == CensusDecision("unique", 1)
    # The census reads rows exactly as the identity check does: the clicked
    # cell is left out.
    assert server._structured_text_at(auto, 5, 45) == "Intake Dr Lee"


def test_windows_virtualized_or_short_grids_are_incomplete() -> None:
    listing = _uia_list((ROW, ""), (OTHER, ""))
    listing.grid_rows = 300
    census = server._identity_candidates_at(
        _auto(listing.children[1].children[0]), 5, 45
    )
    assert _decide(census).verdict == "uncounted"
    listing.grid_rows = None
    listing.virtual = True
    census = server._identity_candidates_at(
        _auto(listing.children[1].children[0]), 5, 45
    )
    assert _decide(census).verdict == "uncounted"


def test_windows_standalone_control_is_not_a_row() -> None:
    window = UiaControl("WindowControl").add(UiaControl("ButtonControl", "Sign"))
    census = server._identity_candidates_at(_auto(window.children[0]), 5, 5)
    assert _decide(census).verdict == "not_a_row"


def test_windows_agent_serves_the_census_operation() -> None:
    listing = _uia_list((ROW, ""), (ROW, ""))
    reply = server._perform_uia_initialized(
        _auto(listing.children[0].children[0]),
        "identity-candidates-at",
        {"x": 5, "y": 5},
    )
    assert reply["status"] == "ok" and len(reply["census"]["candidates"]) == 2


# -- macOS backend -------------------------------------------------------------


def test_macos_backend_converts_row_offsets_to_pixels() -> None:
    from tests.test_macos_structural import FakeMacAXClient, backend

    class CensusAX(FakeMacAXClient):
        def row_census(self, pid, window_title, x, y, *, limit):
            return {
                "record_key": None,
                "incomplete": False,
                "top_level": True,
                "candidates": [
                    {"self": True, "record_key": None, "dx": 0.0, "dy": 0.0},
                    {"self": False, "record_key": None, "dx": 0.0, "dy": 22.0},
                ],
            }

    target = backend(ax_client=CensusAX())
    target.screenshot()
    census = target.identity_candidates_at(10, 10)
    assert census is not None
    assert census["candidates"][1]["dy"] == pytest.approx(22.0 * (target._scale_y or 1))
    assert _decide(census).verdict == "ambiguous"


def test_macos_client_without_a_census_cannot_count() -> None:
    from tests.test_macos_structural import FakeMacAXClient, backend

    assert backend(ax_client=FakeMacAXClient()).identity_candidates_at(10, 10) is None


# -- the replayer on a native backend ------------------------------------------


class NativeBackend:
    """Structured text and a row census over the fake tree, like a native backend."""

    viewport = (400, 300)

    def __init__(self, table: Node, at: dict[tuple[int, int], Node]):
        self._table = table
        self._at = at

    def screenshot(self) -> bytes:
        return b""

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        node = self._at.get((x, y))
        if node is None:
            return None
        row = node.parent
        return _row_identity(row, [row.kids.index(node)])

    def identity_candidates_at(self, x: int, y: int) -> Optional[dict]:
        node = self._at.get((x, y))
        return _census(node) if node is not None else None


def _step(recorded_key: Optional[str]) -> Step:
    template = itmpl.build_identity_template(
        None,
        structured_identity="Follow-up Dr Lee",
        record_key=recorded_key,
        structured_row=True,
    )
    return Step(
        id="open",
        intent="click 'John Smith'",
        action=ActionKind.CLICK,
        identity_armed=True,
        anchor=Anchor(
            template="templates/open.png",
            region=(0, 0, 100, 40),
            click_point=(50, 20),
            identity_template=template,
        ),
    )


def _sign() -> Step:
    return Step(id="sign", intent="click 'Sign visit note'", action=ActionKind.CLICK)


def _verify(backend: NativeBackend, step: Step, point) -> IdentityCheck:
    replayer = Replayer(backend, vision=object(), poll_interval_s=0.01)
    resolution = Resolution(rung="template", point=point, confidence=1.0, elapsed_ms=1)
    return replayer._verify_identity(
        step, resolution, b"", {}, Workflow(name="wf", steps=[step, _sign()])
    )


def _native(rows) -> tuple[NativeBackend, Node]:
    table = _table(*rows)
    at = {(50, 20 + 40 * i): row.kids[0] for i, row in enumerate(table.kids)}
    return NativeBackend(table, at), table


def test_replay_halts_on_identical_native_rows() -> None:
    backend, _ = _native([(ROW, ()), (ROW, ())])
    check = _verify(backend, _step(None), (50, 20))
    assert check.status == "abstain"
    assert check.ambiguity_halt == "identical_candidates"
    assert check.candidate_count == 2


def test_replay_moves_to_the_native_row_with_the_recorded_id() -> None:
    backend, _ = _native([(ROW, ("chart-1002",)), (ROW, ("chart-1001",))])
    step = _step("id=chart-1001")
    replayer = Replayer(backend, vision=object(), poll_interval_s=0.01)
    resolution = Resolution(
        rung="template", point=(50, 20), confidence=1.0, elapsed_ms=1
    )
    moved, _region = replayer._retarget_by_record_key(step, resolution, (0, 0, 100, 40))
    assert moved.point == (50, 60)
    check = _verify(backend, step, moved.point)
    assert check.status == "verified" and check.candidate_count == 2


def test_replay_runs_a_unique_native_row() -> None:
    backend, _ = _native([(ROW, ()), (OTHER, ())])
    check = _verify(backend, _step(None), (50, 20))
    assert check.status == "verified" and check.candidate_count == 1
