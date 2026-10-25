"""Row census on native accessibility trees (macOS AX, Linux AT-SPI, Windows UIA).

A worklist of two rows that read "John Smith Follow-up Dr Lee" must halt
before it opens either chart, distinct record ids must pick the recorded row,
and a unique row must still run. Each backend is exercised through its real
census code over a fake accessibility tree; the browser census has its own
real-browser tests (tests/test_realism_browser.py).
"""

from __future__ import annotations

import types
import uuid
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
        self.scrolls: Optional[bool] = False

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

    def scrollable(self, container: Node) -> Optional[bool]:
        return container.scrolls


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


@pytest.mark.parametrize(
    "ids",
    [
        ("row-0", "row-1"),
        ("r1c2", "r2c2"),
        ("row-3-col-1", "row-4-col-1"),
        ("row-53", "row-54"),  # absolute positions in a longer list
        ("item_12", "item_13"),
        ("0", "1"),
    ],
)
def test_positional_ids_never_tell_rows_apart(ids) -> None:
    table = _table((ROW, (ids[0],)), (ROW, (ids[1],)))
    census = _census(table.kids[0].kids[0])
    assert census is not None
    assert [c["record_key"] for c in census["candidates"]] == [None, None]
    assert _decide(census).verdict == "ambiguous"


_STABLE_ID_FAMILIES = {
    "uuid4": lambda r: str(uuid.UUID(int=r.getrandbits(128), version=4)),
    "uuid4 without dashes": lambda r: uuid.UUID(int=r.getrandbits(128)).hex,
    "encounter uuid": lambda r: f"encounter-{uuid.UUID(int=r.getrandbits(128))}",
    "MRN-A1234": lambda r: f"MRN-A{r.randint(1000, 99999)}",
    "zero-padded MRN": lambda r: f"{r.randint(1, 99_999_999):08d}",
    "chart-1001": lambda r: f"chart-{r.randint(1000, 999_999)}",
    "patient_N": lambda r: f"patient_{r.randint(100, 999_999)}",
    "ENC2024-000123": lambda r: (
        f"ENC{r.randint(2015, 2026)}-{r.randint(1, 999_999):06d}"
    ),
    "FHIR Patient/123": lambda r: f"Patient/{r.randint(1, 99_999)}",
    "dated visit": lambda r: f"visit-2026-10-{r.randint(10, 28)}-{r.randint(1, 99)}",
    "ObjectId": lambda r: "%024x" % r.getrandbits(96),
    "ULID": lambda r: (
        "01H" + "".join(r.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(23))
    ),
}


@pytest.mark.parametrize("family", sorted(_STABLE_ID_FAMILIES))
def test_stable_record_ids_are_never_dropped_as_positional(family) -> None:
    """Measured: 0 of 1000 ids per family read as a position."""

    import random

    from openadapt_flow.runtime.identity_census import positional_id

    rng = random.Random(family)
    values = [_STABLE_ID_FAMILIES[family](rng) for _ in range(1000)]
    dropped = [v for i, v in enumerate(values) if positional_id(v, i % 50)]
    assert dropped == []


def test_the_windows_agent_drops_the_same_ids() -> None:
    import random

    from openadapt_flow.runtime.identity_census import positional_id

    rng = random.Random(3)
    values = [g(rng) for g in _STABLE_ID_FAMILIES.values() for _ in range(50)]
    values += ["row-3", "r3c2", "item_12", "row-3-col-1", "3-1", "ListViewItem-0", "7"]
    for index, value in enumerate(values):
        assert server._positional_id(value, index % 9) == positional_id(
            value, index % 9
        ), value


@pytest.mark.parametrize("scrolls", [True, None])
def test_a_list_without_a_row_count_that_may_scroll_is_incomplete(scrolls) -> None:
    table = _table((ROW, ()), (OTHER, ()))
    table.scrolls = scrolls  # no row count, and rows may be out of view
    census = _census(table.kids[1].kids[0])
    assert census is not None and census["incomplete"] is True


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
        self.broken = False

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
        if self.broken:
            raise RuntimeError("provider failed")
        if self.grid_rows is None:
            return None  # unsupported, as uiautomation reports it
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


def test_windows_a_failing_provider_is_incomplete() -> None:
    listing = _uia_list((ROW, ""), (OTHER, ""))
    listing.broken = True
    census = server._identity_candidates_at(
        _auto(listing.children[1].children[0]), 5, 45
    )
    assert _decide(census).verdict == "uncounted"


def test_windows_a_row_without_cells_is_incomplete() -> None:
    listing = _uia_list((ROW, ""), (OTHER, ""))
    listing.add(UiaControl("DataItemControl", top=80))  # not built yet
    census = server._identity_candidates_at(
        _auto(listing.children[0].children[0]), 5, 5
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


# -- pixel census (remote display, no OCR band) ---------------------------------


def _crop_png(bar: int = 60, height: int = 48, width: int = 240) -> Any:
    import numpy as np

    img = np.full((height, width), 255, np.uint8)
    img[8:40, bar : bar + 6] = 0
    img[8:40, bar + 40 : bar + 44] = 0
    return img


def _png(arr: Any) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def test_template_peaks_lists_every_place_and_refuses_a_crowded_frame() -> None:
    import numpy as np

    from openadapt_flow.vision.match import MAX_PEAKS, template_peaks

    crop = _crop_png()
    gap = np.full((10, crop.shape[1]), 255, np.uint8)
    frame = np.vstack([crop, gap, crop, gap, _crop_png(bar=150)])
    peaks = template_peaks(_png(frame), _png(crop), threshold=0.9)
    assert peaks is not None and sorted(y for _x, y in peaks) == [0, 58]
    crowded = np.vstack([np.vstack([crop, gap])] * (MAX_PEAKS + 1))
    assert template_peaks(_png(crowded), _png(crop), threshold=0.9) is None
    assert template_peaks(_png(crop[:10, :10]), _png(crop)) is None  # too small


def _pixel_step(tmp_path, recorded: bytes) -> Step:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "id.png").write_bytes(recorded)
    (tmp_path / "templates" / "t.png").write_bytes(recorded)
    return Step(
        id="open",
        intent="click 'Open referral'",
        action=ActionKind.CLICK,
        identity_armed=True,
        anchor=Anchor(
            template="templates/t.png",
            region=(0, 0, 240, 48),
            click_point=(0, 0),
            identifier_crop="templates/id.png",
            identifier_region=(0, 0, 240, 48),
        ),
    )


class _PixelBackend:
    viewport = (240, 400)

    def screenshot(self) -> bytes:
        return b""


def _pixel_census(
    tmp_path, frame: Any, *, verify: bool, monkeypatch=None, verdict=None
):
    from openadapt_flow.runtime import identity as identity_mod

    if verdict is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(
            identity_mod, "verify_pixel_identity", lambda *a, **k: verdict
        )
    replayer = Replayer(
        _PixelBackend(),
        vision=object(),
        poll_interval_s=0.01,
        pixel_verify_enabled=verify,
    )
    step = _pixel_step(tmp_path, _png(_crop_png()))
    resolution = Resolution(rung="template", point=(0, 0), confidence=1.0, elapsed_ms=1)
    return replayer._pixel_census(
        step, resolution, _png(frame), Workflow(name="wf"), tmp_path
    )


def _two_copies() -> Any:
    import numpy as np

    gap = np.full((10, 240), 255, np.uint8)
    return np.vstack([_crop_png(), gap, _crop_png()])


def test_pixel_census_counts_a_second_copy(tmp_path) -> None:
    decision = _pixel_census(tmp_path, _two_copies(), verify=True)
    assert decision == CensusDecision("ambiguous", 2)


def test_pixel_census_counts_a_copy_when_verify_is_off(tmp_path) -> None:
    """Verify disabled never yields 'verified'; the copy must still count."""

    decision = _pixel_census(tmp_path, _two_copies(), verify=False)
    assert decision == CensusDecision("ambiguous", 2)


@pytest.mark.parametrize(
    "verdict",
    [None, IdentityCheck(status="abstain", mode="pixel")],
    ids=["no-verdict", "abstain"],
)
def test_pixel_census_counts_anything_short_of_a_mismatch(
    tmp_path, monkeypatch, verdict
) -> None:
    decision = _pixel_census(
        tmp_path, _two_copies(), verify=True, monkeypatch=monkeypatch, verdict=verdict
    )
    assert decision.verdict == "ambiguous"


def test_pixel_census_rules_out_a_clear_mismatch(tmp_path, monkeypatch) -> None:
    decision = _pixel_census(
        tmp_path,
        _two_copies(),
        verify=True,
        monkeypatch=monkeypatch,
        verdict=IdentityCheck(status="mismatch", mode="pixel"),
    )
    assert decision == CensusDecision("unique", 1)


def test_pixel_census_is_uncounted_at_another_scale(tmp_path) -> None:
    """The target's own place is not found at the recorded scale."""

    import cv2

    frame = cv2.resize(_two_copies(), None, fx=1.3, fy=1.3)
    decision = _pixel_census(tmp_path, frame, verify=True)
    assert decision.verdict == "uncounted"


def test_pixel_census_is_uncounted_when_a_place_cannot_be_cut(
    tmp_path, monkeypatch
) -> None:
    from openadapt_flow.runtime import identity as identity_mod

    real = identity_mod.crop_region
    # The target's own crop cuts; the second place does not.
    monkeypatch.setattr(
        identity_mod,
        "crop_region",
        lambda png, region: real(png, region) if region[1] == 0 else None,
    )
    decision = _pixel_census(tmp_path, _two_copies(), verify=True)
    assert decision.verdict == "uncounted"


# -- pixel census tie-break: sibling tabs share chrome, not their label ---------

_TAB_W, _TAB_H = 300, 40
_TABS = ["Who", "Contact", "Choices", "Employer", "Stats", "Misc"]


def _tab(label: str) -> Any:
    """An OpenEMR-like section toggle: chrome dominates, the label is small."""

    import cv2
    import numpy as np

    img = np.full((_TAB_H, _TAB_W), 245, np.uint8)
    cv2.rectangle(img, (0, 0), (_TAB_W - 1, _TAB_H - 1), 120, 2)
    cv2.rectangle(img, (0, 0), (_TAB_W - 1, 10), 200, -1)
    cv2.circle(img, (16, 25), 6, 60, -1)
    cv2.putText(
        img, label, (30, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 40, 1, cv2.LINE_AA
    )
    return img


def _tabs(labels: list[str]) -> Any:
    import numpy as np

    gap = np.full((12, _TAB_W), 255, np.uint8)
    return np.vstack([part for label in labels for part in (_tab(label), gap)])


class _TabOcr:
    """OCR that reads a tab crop as its nearest rendered label (or nothing)."""

    def __init__(self, readable: bool = True):
        self.readable = readable

    def ocr(self, png: bytes, *, region=None):
        import cv2
        import numpy as np

        if not self.readable:
            return []
        crop = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        if crop is None or crop.shape != (_TAB_H, _TAB_W):
            return []
        label = min(
            _TABS + ["Contact"],
            key=lambda name: float(np.abs(crop.astype(int) - _tab(name)).mean()),
        )
        return [types.SimpleNamespace(text=label)]


def _tab_census(tmp_path, labels: list[str], target: int, *, readable: bool = True):
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    recorded = _png(_tab(labels[target]))
    (tmp_path / "templates" / "id.png").write_bytes(recorded)
    (tmp_path / "templates" / "t.png").write_bytes(recorded)
    top = target * (_TAB_H + 12)
    step = Step(
        id="open_contact",
        intent="click 'Contact'",
        action=ActionKind.CLICK,
        identity_armed=True,
        anchor=Anchor(
            template="templates/t.png",
            region=(0, top, _TAB_W, _TAB_H),
            click_point=(0, top),
            identifier_crop="templates/id.png",
            identifier_region=(0, top, _TAB_W, _TAB_H),
        ),
    )
    replayer = Replayer(
        _PixelBackend(),
        vision=_TabOcr(readable),
        poll_interval_s=0.01,
        pixel_verify_enabled=True,
    )
    resolution = Resolution(
        rung="template", point=(0, top), confidence=1.0, elapsed_ms=1
    )
    return replayer._pixel_census(
        step, resolution, _png(_tabs(labels)), Workflow(name="wf"), tmp_path
    )


def test_a_section_tab_among_sibling_tabs_is_unique_by_its_label(tmp_path) -> None:
    """The OpenEMR "Contact" toggle: its siblings share the chrome, and some
    are not a clear pixel mismatch, but their labels read differently."""

    assert _tab_census(tmp_path, _TABS, 1) == CensusDecision("unique", 1)


def test_sibling_tabs_with_unreadable_labels_still_halt(tmp_path) -> None:
    decision = _tab_census(tmp_path, _TABS, 1, readable=False)
    assert decision.verdict == "ambiguous"


def test_two_tabs_with_the_same_label_halt(tmp_path) -> None:
    decision = _tab_census(tmp_path, ["Who", "Contact", "Misc", "Contact"], 1)
    assert decision == CensusDecision("ambiguous", 2)


def test_featureless_crops_are_recognized() -> None:
    import cv2
    import numpy as np

    from openadapt_flow.vision.match import crop_is_featureless

    band = np.full((64, 160), 40, np.uint8)
    band[32:34, :] = 220  # OpenEMR step_016: a band with one rule
    assert crop_is_featureless(_png(band))
    assert crop_is_featureless(_png(np.full((48, 240), 128, np.uint8)))
    text = np.full((48, 240), 255, np.uint8)
    cv2.putText(text, "MG4408 Okafor", (6, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 0, 2)
    assert not crop_is_featureless(_png(text))
    assert not crop_is_featureless(_png(_tab("Contact")))
