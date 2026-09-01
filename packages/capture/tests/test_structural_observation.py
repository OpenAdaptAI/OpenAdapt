"""Contract tests for optional native structural observations."""

from __future__ import annotations

import os
import queue
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.recorder import on_click, write_action_event
from openadapt_capture.structural import (
    MAX_STRUCTURAL_ANCESTRY_DEPTH,
    MAX_STRUCTURAL_TEXT_LENGTH,
    StructuralBounds,
    StructuralElement,
    StructuralObservation,
    StructuralObservationRequest,
    StructuralTreeNode,
    create_structural_observer,
    observe_structural_action,
    observe_window_tree,
    omit_tree_value,
)
from openadapt_capture.structural_observer.linux import (
    LinuxATSpiStructuralObserver,
    _GIAtspiRuntime,
)
from openadapt_capture.structural_observer.linux import (
    _fields as _atspi_fields,
)
from openadapt_capture.structural_observer.macos import (
    MacOSAXStructuralObserver,
    _AXRuntime,
)
from openadapt_capture.structural_observer.windows import (
    WindowsUIAStructuralObserver,
    _PywinautoRuntime,
)


class _Wrapper:
    def __init__(
        self,
        *,
        automation_id: str | None = None,
        control_type: str | None = None,
        name: str | None = None,
        role: str | None = None,
        process_id: int | None = None,
        parent: "_Wrapper | None" = None,
        top: "_Wrapper | None" = None,
        descendants: list["_Wrapper"] | None = None,
        children: list["_Wrapper"] | None = None,
        title: str | None = None,
        patterns: tuple[str, ...] = (),
        runtime_id: object | None = None,
        enabled: bool | None = True,
        focused: bool = False,
        value: str | None = None,
        is_password: bool = False,
        class_name: str | None = None,
    ) -> None:
        self.element_info = SimpleNamespace(
            automation_id=automation_id,
            control_type=control_type,
            name=name,
            class_name=class_name,
            framework_id=None,
            handle=None,
            process_id=process_id,
            rectangle=SimpleNamespace(left=10, top=20, right=110, bottom=60),
            runtime_id=runtime_id,
            has_keyboard_focus=focused,
            is_password=is_password,
        )
        self._role = role
        self._parent = parent
        self._top = top
        self._descendants = descendants or []
        self._children = children or []
        self._title = title
        self._enabled = enabled
        self._value = value
        self.descendant_queries: list[dict[str, object]] = []
        for pattern in patterns:
            setattr(self, f"iface_{pattern}", object())

    def friendly_class_name(self) -> str | None:
        return self._role

    def parent(self) -> "_Wrapper | None":
        return self._parent

    def top_level_parent(self) -> "_Wrapper":
        return self._top or self

    def descendants(self, **kwargs) -> list["_Wrapper"]:
        self.descendant_queries.append(kwargs)
        return self._descendants

    def children(self) -> list["_Wrapper"]:
        return self._children

    def is_enabled(self) -> bool | None:
        return self._enabled

    def get_value(self) -> str | None:
        return self._value

    def window_text(self) -> str | None:
        return self._title


def _observation(event_timestamp: float) -> StructuralObservation:
    return StructuralObservation(
        provider="windows_uia",
        event_timestamp=event_timestamp,
        observed_at=event_timestamp + 0.001,
        query_kind="point",
        element=StructuralElement(
            automation_id="submit-order",
            role="Button",
            control_type="Button",
            name="Submit",
        ),
    )


class _Observer:
    def observe(
        self,
        request: StructuralObservationRequest,
    ) -> StructuralObservation:
        return _observation(request.event_timestamp)


def _recording(capture_dir: Path):
    capture_dir.mkdir()
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    recording = crud.insert_recording(
        session,
        {
            "timestamp": 100.0,
            "monitor_width": 1280,
            "monitor_height": 720,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "win32",
            "task_description": "Structural observation test",
        },
    )
    return engine, session, recording


def test_windows_uia_observer_returns_exact_available_evidence() -> None:
    window = _Wrapper(
        automation_id="main-window",
        control_type="Window",
        name="Orders",
        role="Window",
        title="Orders - Example",
    )
    panel = _Wrapper(
        automation_id="order-panel",
        control_type="Pane",
        name="Order",
        role="Pane",
        parent=window,
        top=window,
    )
    target = _Wrapper(
        automation_id="submit-order",
        control_type="Button",
        name="Submit",
        role="Button",
        process_id=42,
        parent=panel,
        top=window,
        patterns=("invoke",),
    )
    duplicate = _Wrapper(
        automation_id="submit-order",
        control_type="Button",
        name="Other name",
        role="Button",
        top=window,
    )
    window._descendants = [target, duplicate]
    runtime = SimpleNamespace(
        from_point=lambda _x, _y: target,
        focused_element=lambda: target,
    )
    observer = WindowsUIAStructuralObserver(
        runtime=runtime,
        process_name_resolver=lambda process_id: "example.exe" if process_id == 42 else None,
        clock=lambda: 101.25,
    )

    observed = observer.observe(
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="click",
            x=25,
            y=35,
        )
    )

    assert observed is not None
    assert observed.element.automation_id == "submit-order"
    assert observed.element.supported_patterns == ["Invoke"]
    assert observed.process is not None
    assert observed.process.process_name == "example.exe"
    assert observed.window is not None
    assert observed.window.title == "Orders - Example"
    assert [ancestor.automation_id for ancestor in observed.ancestry or []] == [
        "order-panel",
        "main-window",
    ]
    assert observed.candidate_count == 2
    assert observed.candidate_context is not None
    assert observed.candidate_context.matched_fields == [
        "automation_id",
        "control_type",
    ]
    assert window.descendant_queries == [{"control_type": "Button"}]
    assert "framework_id" not in observed.element.model_dump(exclude_none=True)


def test_windows_uia_omits_oversized_text_instead_of_truncating_identity() -> None:
    oversized = "x" * (MAX_STRUCTURAL_TEXT_LENGTH + 1)
    target = _Wrapper(
        automation_id="member-id",
        control_type="Edit",
        name=oversized,
        role="Edit",
        process_id=42,
        title=oversized,
    )
    target._top = target
    observer = WindowsUIAStructuralObserver(
        runtime=SimpleNamespace(
            from_point=lambda _x, _y: target,
            focused_element=lambda: target,
        ),
        process_name_resolver=lambda _process_id: oversized,
    )

    observed = observer.observe(
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="click",
            x=25,
            y=35,
        )
    )

    assert observed is not None
    assert observed.element.automation_id == "member-id"
    assert observed.element.name is None
    assert observed.process is not None
    assert observed.process.process_name is None
    assert observed.window is not None
    assert observed.window.title is None


def test_structural_contract_accepts_namespaced_extension_provider() -> None:
    observation = StructuralObservation(
        provider="example_macos_ax",
        event_timestamp=101.0,
        observed_at=101.1,
        query_kind="focused",
        element=StructuralElement(role="AXTextField"),
    )

    assert observation.provider == "example_macos_ax"


def test_structural_bounds_reject_non_finite_or_inverted_provider_geometry() -> None:
    with pytest.raises(ValueError, match="finite"):
        StructuralBounds(left=0, top=0, right=float("nan"), bottom=10)
    with pytest.raises(ValueError, match="inverted"):
        StructuralBounds(left=10, top=0, right=5, bottom=10)


def test_windows_uia_ancestry_depth_is_bounded() -> None:
    with pytest.raises(ValueError, match="maximum_ancestry_depth"):
        WindowsUIAStructuralObserver(
            runtime=SimpleNamespace(),
            maximum_ancestry_depth=MAX_STRUCTURAL_ANCESTRY_DEPTH + 1,
        )


class _AXFakeRuntime:
    def __init__(self) -> None:
        self.window = object()
        self.parent = object()
        self.target = object()
        self.attributes = {
            (self.target, "AXIdentifier"): "submit-order",
            (self.target, "AXRole"): "AXButton",
            (self.target, "AXTitle"): "Submit",
            (self.target, "AXParent"): self.parent,
            (self.target, "AXWindow"): self.window,
            (self.parent, "AXRole"): "AXGroup",
            (self.parent, "AXTitle"): "Order",
            (self.parent, "AXParent"): self.window,
            (self.window, "AXRole"): "AXWindow",
            (self.window, "AXTitle"): "Orders",
            (self.window, "AXWindowNumber"): 44,
            (self.target, "AXEnabled"): True,
            (self.target, "AXValue"): "on",
            (self.parent, "AXEnabled"): True,
            (self.window, "AXEnabled"): True,
        }

    def attribute(self, element, name):
        return self.attributes.get((element, name))

    def element_at_point(self, x, y):
        assert (x, y) == (25, 35)
        return self.target

    def focused_element(self):
        return self.target

    def process_id(self, element):
        assert element is self.target
        return 42

    def actions(self, element):
        return ["AXPress"] if element is self.target else None

    def children(self, element):
        if element is self.window:
            return [self.parent]
        if element is self.parent:
            return [self.target]
        return []

    def bounds(self, element):
        if element is self.target:
            from openadapt_capture.structural import StructuralBounds

            return StructuralBounds(left=10, top=20, right=110, bottom=60)
        return None


def test_macos_ax_observer_returns_action_time_evidence() -> None:
    observer = MacOSAXStructuralObserver(
        runtime=_AXFakeRuntime(),
        process_name_resolver=lambda pid: "Example" if pid == 42 else None,
        clock=lambda: 101.25,
    )

    observed = observer.observe(
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="click",
            x=25,
            y=35,
        )
    )

    assert observed is not None
    assert observed.provider == "macos_ax"
    assert observed.query_kind == "point"
    assert observed.element.automation_id == "submit-order"
    assert observed.element.supported_patterns == ["AXPress"]
    assert observed.process is not None
    assert observed.process.process_name == "Example"
    assert observed.window is not None
    assert observed.window.native_window_handle == 44
    assert [item.role for item in observed.ancestry or []] == [
        "AXGroup",
        "AXWindow",
    ]


def test_macos_ax_runtime_falls_back_to_the_frontmost_application() -> None:
    system = object()
    application = object()
    focused = object()
    calls: list[tuple[object, str]] = []

    runtime = _AXRuntime.__new__(_AXRuntime)
    runtime.system = system
    runtime.frontmost_process_id = lambda: 42
    runtime.ax = SimpleNamespace(
        kAXErrorSuccess=0,
        AXUIElementCreateApplication=lambda process_id: (
            application if process_id == 42 else None
        ),
        AXUIElementCopyAttributeValue=lambda element, name, _output: (
            calls.append((element, name))
            or (0, focused if element is application else None)
        ),
    )

    assert runtime.focused_element() is focused
    assert calls == [
        (system, "AXFocusedUIElement"),
        (application, "AXFocusedUIElement"),
    ]


class _ATSpiElement:
    def __init__(self, name: str, parent=None) -> None:
        self.name = name
        self.parent = parent


class _ATSpiFakeRuntime:
    def __init__(self) -> None:
        self.application = _ATSpiElement("Example")
        self.window = _ATSpiElement("Orders", self.application)
        self.parent_element = _ATSpiElement("Order", self.window)
        self.target = _ATSpiElement("Submit", self.parent_element)

    def element_at_point(self, x, y):
        assert (x, y) == (25, 35)
        return self.target

    def focused_element(self):
        return self.target

    def parent(self, element):
        return element.parent

    def attributes(self, element):
        if element is self.target:
            return {"accessible-id": "submit-order", "class": "GtkButton"}
        return {}

    def role_name(self, element):
        return {
            self.target: "push button",
            self.parent_element: "panel",
            self.window: "frame",
            self.application: "application",
        }[element]

    def bounds(self, element):
        if element is self.target:
            from openadapt_capture.structural import StructuralBounds

            return StructuralBounds(left=10, top=20, right=110, bottom=60)
        return None

    def action_names(self, element):
        return ["click"] if element is self.target else None

    def process_id(self, element):
        return 42

    def children(self, element):
        if element is self.window:
            return [self.parent_element]
        if element is self.parent_element:
            return [self.target]
        return []

    def text_value(self, element):
        if element is self.target:
            return "Submit"
        return None

    def runtime_id(self, element):
        if element is self.target:
            return "atspi:submit"
        if element is self.window:
            return "atspi:window"
        return None


def test_linux_atspi_observer_returns_action_time_evidence() -> None:
    observer = LinuxATSpiStructuralObserver(
        runtime=_ATSpiFakeRuntime(),
        process_name_resolver=lambda pid: "example" if pid == 42 else None,
        clock=lambda: 101.5,
    )

    observed = observer.observe(
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="press",
        )
    )

    assert observed is not None
    assert observed.provider == "linux_atspi"
    assert observed.query_kind == "focused"
    assert observed.element.automation_id == "submit-order"
    assert observed.element.class_name == "GtkButton"
    assert observed.element.supported_patterns == ["click"]
    assert observed.process is not None
    assert observed.process.process_name == "example"
    assert observed.window is not None
    assert observed.window.title == "Orders"
    assert [item.role for item in observed.ancestry or []] == [
        "panel",
        "frame",
        "application",
    ]


def test_gi_atspi_runtime_descends_from_desktop_point_query() -> None:
    class StateSet:
        def __init__(self, *states) -> None:
            self.states = set(states)

        def contains(self, state) -> bool:
            return state in self.states

    class Component:
        def __init__(self, child=None) -> None:
            self.child = child

        def get_accessible_at_point(self, x, y, coordinates):
            assert (x, y, coordinates) == (25, 35, "screen")
            return self.child

    class Node:
        def __init__(self, *, child=None, states=()) -> None:
            self.component = Component(child)
            self.states = StateSet(*states)

        def get_component_iface(self):
            return self.component

        def get_state_set(self):
            return self.states

    target = Node(states=("focused",))
    window = Node(child=target, states=("active",))
    application = Node(child=window)
    desktop = Node(child=application)
    runtime = _GIAtspiRuntime.__new__(_GIAtspiRuntime)
    runtime.atspi = SimpleNamespace(
        CoordType=SimpleNamespace(SCREEN="screen"),
        StateType=SimpleNamespace(ACTIVE="active", FOCUSED="focused"),
    )
    runtime.desktop = desktop

    assert runtime.element_at_point(25, 35) is target


def test_gi_atspi_runtime_omits_a_point_query_beyond_the_depth_bound() -> None:
    class Component:
        def __init__(self) -> None:
            self.child = None

        def get_accessible_at_point(self, _x, _y, _coordinates):
            return self.child

    class Node:
        def __init__(self) -> None:
            self.component = Component()

        def get_component_iface(self):
            return self.component

    nodes = [Node() for _ in range(MAX_STRUCTURAL_ANCESTRY_DEPTH + 3)]
    for current, child in zip(nodes, nodes[1:]):
        current.component.child = child
    runtime = _GIAtspiRuntime.__new__(_GIAtspiRuntime)
    runtime.atspi = SimpleNamespace(CoordType=SimpleNamespace(SCREEN="screen"))
    runtime.desktop = nodes[0]

    assert runtime.element_at_point(25, 35) is None


def test_gi_atspi_runtime_refuses_an_unbounded_child_list() -> None:
    node = SimpleNamespace(
        get_child_count=lambda: _GIAtspiRuntime._MAX_NODES + 1,
    )
    runtime = _GIAtspiRuntime.__new__(_GIAtspiRuntime)

    with pytest.raises(RuntimeError, match="child count"):
        runtime.children(node)


def test_gi_atspi_focused_search_refuses_a_truncated_tree() -> None:
    class Node:
        def __init__(self, *children, focused=False) -> None:
            self.children = list(children)
            self.focused = focused

        def get_child_count(self):
            return len(self.children)

        def get_child_at_index(self, index):
            return self.children[index]

        def get_state_set(self):
            return SimpleNamespace(contains=lambda state: state == "focused" and self.focused)

    first = Node(focused=True)
    second = Node()
    desktop = Node(first, second)
    runtime = _GIAtspiRuntime.__new__(_GIAtspiRuntime)
    runtime._MAX_NODES = 1
    runtime.atspi = SimpleNamespace(
        StateType=SimpleNamespace(FOCUSED="focused"),
    )
    runtime.desktop = desktop

    assert runtime.focused_element() is None


def test_gi_atspi_process_id_ascends_past_an_unowned_element() -> None:
    parent = SimpleNamespace(
        get_process_id=lambda: 42,
        parent=None,
    )
    target = SimpleNamespace(
        get_process_id=lambda: 0,
        parent=parent,
    )
    runtime = _GIAtspiRuntime.__new__(_GIAtspiRuntime)

    assert runtime.process_id(target) == 42


def test_atspi_fields_prefer_the_native_accessible_id() -> None:
    target = SimpleNamespace(
        name="Submit",
        get_accessible_id=lambda: "native-submit",
    )
    runtime = SimpleNamespace(
        attributes=lambda _element: {"accessible-id": "legacy-submit"},
        role_name=lambda _element: "push button",
        bounds=lambda _element: None,
        action_names=lambda _element: None,
    )

    fields = _atspi_fields(runtime, target)

    assert fields["automation_id"] == "native-submit"


@pytest.mark.parametrize(
    "observer_type",
    [MacOSAXStructuralObserver, LinuxATSpiStructuralObserver],
)
def test_cross_platform_runtime_is_owned_by_the_delivery_thread(observer_type) -> None:
    lifecycle: list[tuple[str, int]] = []
    fake = _AXFakeRuntime() if observer_type is MacOSAXStructuralObserver else _ATSpiFakeRuntime()

    class Runtime:
        def __getattr__(self, name):
            return getattr(fake, name)

        def close(self):
            lifecycle.append(("close", threading.get_ident()))

    def factory():
        lifecycle.append(("create", threading.get_ident()))
        return Runtime()

    observer = observer_type(runtime_factory=factory)

    def deliver() -> None:
        observer.open_current_thread()
        try:
            observation = observer.observe(
                StructuralObservationRequest(
                    event_timestamp=101.0,
                    action_name="press",
                )
            )
            assert observation is not None
        finally:
            observer.close_current_thread()

    thread = threading.Thread(target=deliver)
    thread.start()
    thread.join()

    assert [name for name, _thread_id in lifecycle] == ["create", "close"]
    assert len({thread_id for _name, thread_id in lifecycle}) == 1


def test_pywinauto_runtime_quantizes_points_and_uses_exact_focused_api() -> None:
    calls: list[tuple[str, object]] = []
    desktop = SimpleNamespace(
        from_point=lambda x, y: calls.append(("point", (x, y))) or "point-target",
        backend=SimpleNamespace(
            generic_wrapper_class=lambda info: calls.append(("wrap", info)) or "focused-target"
        ),
    )
    iui = SimpleNamespace(
        get_focused_element=lambda: calls.append(("focused", None)) or "raw-focused"
    )

    class ElementInfo:
        def __init__(self, raw: object) -> None:
            calls.append(("info", raw))

    runtime = _PywinautoRuntime.__new__(_PywinautoRuntime)
    runtime._desktop = desktop
    runtime._iui = iui
    runtime._element_info_type = ElementInfo

    assert runtime.from_point(25.49, -35.5) == "point-target"
    assert calls[0] == ("point", (25, -36))
    point = calls[0][1]
    assert isinstance(point, tuple)
    assert all(isinstance(value, int) for value in point)

    assert runtime.focused_element() == "focused-target"
    assert ("focused", None) in calls
    assert ("info", "raw-focused") in calls
    assert not hasattr(desktop, "get_active")


def test_uia_observer_owns_one_balanced_observation_thread() -> None:
    lifecycle: list[tuple[str, int]] = []
    observations: list[StructuralObservation | None] = []
    target = _Wrapper(
        automation_id="member-id",
        control_type="Edit",
        name="Member ID",
        role="Edit",
    )

    class Apartment:
        def __enter__(self):
            lifecycle.append(("enter", threading.get_ident()))
            return self

        def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
            lifecycle.append(("exit", threading.get_ident()))

    class Runtime:
        def __init__(self) -> None:
            lifecycle.append(("runtime", threading.get_ident()))

        def from_point(self, _x: float, _y: float) -> _Wrapper:
            lifecycle.append(("point", threading.get_ident()))
            return target

        def focused_element(self) -> _Wrapper:
            lifecycle.append(("focused", threading.get_ident()))
            return target

        def close(self) -> None:
            lifecycle.append(("runtime-close", threading.get_ident()))

    observer = WindowsUIAStructuralObserver(
        runtime_factory=Runtime,
        apartment_factory=Apartment,
    )

    def observe_on_delivery_thread() -> None:
        observer.open_current_thread()
        try:
            observations.append(
                observer.observe(
                    StructuralObservationRequest(
                        event_timestamp=101.0,
                        action_name="click",
                        x=25.0,
                        y=35.0,
                    )
                )
            )
            observations.append(
                observer.observe(
                    StructuralObservationRequest(
                        event_timestamp=102.0,
                        action_name="press",
                    )
                )
            )
        finally:
            observer.close_current_thread()

    thread = threading.Thread(target=observe_on_delivery_thread)
    thread.start()
    thread.join()

    point, focused = observations
    assert point is not None and point.query_kind == "point"
    assert focused is not None and focused.query_kind == "focused"
    thread_ids = {thread_id for _name, thread_id in lifecycle}
    assert len(thread_ids) == 1
    assert [name for name, _thread_id in lifecycle] == [
        "enter",
        "runtime",
        "point",
        "focused",
        "runtime-close",
        "exit",
    ]


def test_structural_evidence_persists_through_capture_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    capture_dir = tmp_path / "capture"
    engine, session, recording = _recording(capture_dir)
    monkeypatch.setattr(
        "openadapt_capture.recorder.window.get_active_element_state",
        lambda _x, _y: {},
    )
    monkeypatch.setattr(
        "openadapt_capture.recorder.utils.get_timestamp",
        lambda: 101.2,
    )
    event_queue: queue.Queue = queue.Queue()
    perf_queue: queue.Queue = queue.Queue()

    on_click(
        event_queue,
        None,
        25,
        35,
        "left",
        True,
        timestamp=101.0,
        structural_observer=_Observer(),
    )
    on_click(
        event_queue,
        None,
        25,
        35,
        "left",
        False,
        timestamp=101.1,
        structural_observer=_Observer(),
    )
    write_action_event(session, recording, event_queue.get_nowait(), perf_queue)
    write_action_event(session, recording, event_queue.get_nowait(), perf_queue)
    session.close()
    engine.dispose()

    with CaptureSession.load(capture_dir) as capture:
        raw = capture.raw_events()
        actions = list(capture.actions())

        assert raw[0].structural_observation is not None
        assert raw[0].structural_observation.element.automation_id == "submit-order"
        assert raw[1].structural_observation is None
        assert len(actions) == 1
        assert actions[0].structural_observation is not None
        assert actions[0].structural_observation.element.name == "Submit"


def test_legacy_action_table_is_migrated_without_fabricated_evidence(
    tmp_path: Path,
) -> None:
    capture_dir = tmp_path / "legacy"
    engine, session, recording = _recording(capture_dir)
    crud.insert_action_event(
        session,
        recording,
        101.0,
        {
            "name": "scroll",
            "mouse_x": 25,
            "mouse_y": 35,
            "mouse_dx": 0,
            "mouse_dy": -1,
        },
    )
    session.close()
    engine.dispose()

    database_path = capture_dir / "recording.db"
    connection = sqlite3.connect(database_path)
    connection.execute("ALTER TABLE action_event DROP COLUMN structural_observation")
    connection.commit()
    connection.close()

    with CaptureSession.load(capture_dir) as capture:
        event = capture.raw_events()[0]
        assert event.structural_observation is None

    connection = sqlite3.connect(database_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(action_event)")}
    connection.close()
    assert "structural_observation" in columns


@pytest.mark.parametrize(
    ("platform_name", "target"),
    [
        (
            "darwin",
            "openadapt_capture.structural_observer.macos.MacOSAXStructuralObserver",
        ),
        (
            "linux",
            "openadapt_capture.structural_observer.linux.LinuxATSpiStructuralObserver",
        ),
    ],
)
def test_factory_selects_each_native_structural_provider(
    monkeypatch, platform_name, target
) -> None:
    sentinel = object()
    monkeypatch.setattr(target, lambda: sentinel)
    assert create_structural_observer(platform_name=platform_name) is sentinel


def test_unsupported_platform_factory_is_headless_safe() -> None:
    assert create_structural_observer(platform_name="freebsd") is None


def test_windows_factory_omits_optional_evidence_when_uia_cannot_start(
    monkeypatch,
) -> None:
    def fail() -> None:
        raise RuntimeError("COM unavailable")

    monkeypatch.setattr(
        "openadapt_capture.structural_observer.windows.WindowsUIAStructuralObserver",
        fail,
    )
    assert create_structural_observer(platform_name="win32") is None


def test_lazy_uia_start_failure_does_not_abort_native_capture() -> None:
    attempts = 0

    def fail_runtime():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("UIA unavailable")

    observer = WindowsUIAStructuralObserver(
        runtime_factory=fail_runtime,
        apartment_factory=lambda: SimpleNamespace(
            __enter__=lambda: None,
            __exit__=lambda *_args: None,
        ),
    )
    request = StructuralObservationRequest(
        event_timestamp=101.0,
        action_name="click",
        x=25.0,
        y=35.0,
    )
    assert observer.observe(request) is None
    assert observer.observe(request) is None
    assert attempts == 1


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION") != "1",
    reason="requires the explicit interactive production qualification rig",
)
def test_live_native_structural_provider_returns_exact_focused_evidence() -> None:
    observer = create_structural_observer()
    assert observer is not None, "the native structural provider is required"
    expected_provider = {
        "win32": "windows_uia",
        "darwin": "macos_ax",
        "linux": "linux_atspi",
    }["linux" if sys.platform.startswith("linux") else sys.platform]
    observation = observe_structural_action(
        observer,
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="press",
        ),
    )
    assert observation is not None
    assert observation.provider == expected_provider
    assert observation.query_kind == "focused"
    assert observation.event_timestamp == 101.0
    assert observation.element.model_dump(exclude_none=True)
    assert observation.process is not None
    assert observation.process.process_id is not None
    assert observation.window is not None


def test_windows_uia_window_tree_omits_password_values() -> None:
    field = _Wrapper(
        automation_id="member-id",
        control_type="Edit",
        name="Member",
        role="Edit",
        runtime_id=(1, 4),
        value="queued",
    )
    secret = _Wrapper(
        automation_id="password",
        control_type="Edit",
        name="Password",
        role="Edit",
        runtime_id=(1, 5),
        value="typed-secret",
        is_password=True,
    )
    button = _Wrapper(
        automation_id="btnContinue",
        control_type="Button",
        name="Continue",
        role="Button",
        runtime_id=(1, 3),
    )
    window = _Wrapper(
        automation_id="main-window",
        control_type="Window",
        name="Orders",
        role="Window",
        title="Orders - Example",
        process_id=42,
        runtime_id=(1, 1),
        children=[field, secret, button],
    )
    field._top = window
    secret._top = window
    button._top = window
    window._top = window
    observer = WindowsUIAStructuralObserver(
        runtime=SimpleNamespace(
            from_point=lambda _x, _y: button,
            focused_element=lambda: button,
        ),
        process_name_resolver=lambda process_id: "example.exe" if process_id == 42 else None,
        clock=lambda: 101.25,
    )

    observed = observe_window_tree(
        observer,
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="observe",
            x=25,
            y=35,
        ),
    )

    assert observed is not None
    assert observed.query_kind == "window_tree"
    assert observed.window is not None
    assert observed.window.title == "Orders - Example"
    assert observed.tree is not None
    root = observed.tree[0]
    children = {child.automation_id: child for child in root.children or []}
    assert children["member-id"].value == "queued"
    assert children["member-id"].name == "Member"
    assert children["password"].value is None
    assert children["password"].name == "Password"
    assert children["btnContinue"].provider_runtime_id == "1.3"
    dumped = root.model_dump_json()
    assert "typed-secret" not in dumped


def test_macos_ax_window_tree_walks_from_the_ax_window() -> None:
    observer = MacOSAXStructuralObserver(
        runtime=_AXFakeRuntime(),
        process_name_resolver=lambda pid: "Example" if pid == 42 else None,
        clock=lambda: 101.25,
    )
    observed = observe_window_tree(
        observer,
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="observe",
            x=25,
            y=35,
        ),
    )
    assert observed is not None
    assert observed.query_kind == "window_tree"
    assert observed.element.role == "AXWindow"
    assert observed.tree is not None
    assert observed.tree[0].role == "AXWindow"
    assert observed.tree[0].children is not None
    group = observed.tree[0].children[0]
    target = group.children[0]
    assert target.automation_id == "submit-order"
    assert target.provider_runtime_id == "submit-order"
    assert target.value == "on"


def test_linux_atspi_window_tree_walks_from_the_frame() -> None:
    observer = LinuxATSpiStructuralObserver(
        runtime=_ATSpiFakeRuntime(),
        process_name_resolver=lambda pid: "example" if pid == 42 else None,
        clock=lambda: 101.5,
    )
    observed = observe_window_tree(
        observer,
        StructuralObservationRequest(event_timestamp=101.0, action_name="observe"),
    )
    assert observed is not None
    assert observed.query_kind == "window_tree"
    assert observed.tree is not None
    assert observed.tree[0].role == "frame"
    assert observed.tree[0].provider_runtime_id == "atspi:window"
    target = observed.tree[0].children[0].children[0]
    assert target.automation_id == "submit-order"
    assert target.value == "Submit"


def test_window_tree_is_rejected_on_point_observations() -> None:
    with pytest.raises(ValueError, match="window_tree"):
        StructuralObservation(
            provider="windows_uia",
            event_timestamp=101.0,
            observed_at=101.1,
            query_kind="point",
            element=StructuralElement(role="Button"),
            tree=[StructuralTreeNode(role="Button")],
        )


def test_omit_tree_value_detects_password_and_secure_roles() -> None:
    assert omit_tree_value("AXSecureTextField")
    assert omit_tree_value("password text")
    assert omit_tree_value("PasswordBox")
    assert omit_tree_value("Edit", is_password=True)
    assert not omit_tree_value("AXButton")
    assert not omit_tree_value("Edit")


def test_macos_ax_secure_text_field_omits_value() -> None:
    runtime = _AXFakeRuntime()
    secure = object()
    runtime.attributes[runtime.window, "AXChildren"] = [runtime.parent, secure]
    runtime.attributes[secure, "AXRole"] = "AXSecureTextField"
    runtime.attributes[secure, "AXIdentifier"] = "password"
    runtime.attributes[secure, "AXValue"] = "typed-secret"
    runtime.attributes[secure, "AXEnabled"] = True

    def children(element):
        if element is runtime.window:
            return [runtime.parent, secure]
        if element is runtime.parent:
            return [runtime.target]
        return []

    runtime.children = children
    observer = MacOSAXStructuralObserver(
        runtime=runtime,
        process_name_resolver=lambda pid: "Example" if pid == 42 else None,
        clock=lambda: 101.25,
    )
    observed = observe_window_tree(
        observer,
        StructuralObservationRequest(
            event_timestamp=101.0,
            action_name="observe",
            x=25,
            y=35,
        ),
    )
    assert observed is not None
    assert observed.tree is not None
    secure_node = observed.tree[0].children[1]
    assert secure_node.role == "AXSecureTextField"
    assert secure_node.value is None
    assert "typed-secret" not in observed.model_dump_json()


def test_linux_atspi_password_text_omits_value() -> None:
    runtime = _ATSpiFakeRuntime()
    secret = _ATSpiElement("Password", runtime.window)

    def role_name(element):
        if element is secret:
            return "password text"
        return {
            runtime.target: "push button",
            runtime.parent_element: "panel",
            runtime.window: "frame",
            runtime.application: "application",
        }[element]

    def children(element):
        if element is runtime.window:
            return [runtime.parent_element, secret]
        if element is runtime.parent_element:
            return [runtime.target]
        return []

    def text_value(element):
        if element is secret:
            return "typed-secret"
        if element is runtime.target:
            return "Submit"
        return None

    runtime.role_name = role_name
    runtime.children = children
    runtime.text_value = text_value
    observer = LinuxATSpiStructuralObserver(
        runtime=runtime,
        process_name_resolver=lambda pid: "example" if pid == 42 else None,
        clock=lambda: 101.5,
    )
    observed = observe_window_tree(
        observer,
        StructuralObservationRequest(event_timestamp=101.0, action_name="observe"),
    )
    assert observed is not None
    assert observed.tree is not None
    secret_node = observed.tree[0].children[1]
    assert secret_node.role == "password text"
    assert secret_node.value is None
    assert "typed-secret" not in observed.model_dump_json()
