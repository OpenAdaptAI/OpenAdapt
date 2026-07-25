"""Contract tests for optional native structural observations."""

from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db, crud
from openadapt_capture.recorder import on_click, write_action_event
from openadapt_capture.structural import (
    StructuralElement,
    StructuralObservation,
    StructuralObservationRequest,
    create_structural_observer,
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
        title: str | None = None,
        patterns: tuple[str, ...] = (),
    ) -> None:
        self.element_info = SimpleNamespace(
            automation_id=automation_id,
            control_type=control_type,
            name=name,
            class_name=None,
            framework_id=None,
            handle=None,
            process_id=process_id,
            rectangle=SimpleNamespace(left=10, top=20, right=110, bottom=60),
        )
        self._role = role
        self._parent = parent
        self._top = top
        self._descendants = descendants or []
        self._title = title
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
        process_name_resolver=lambda process_id: (
            "example.exe" if process_id == 42 else None
        ),
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


def test_pywinauto_runtime_quantizes_points_and_uses_exact_focused_api() -> None:
    calls: list[tuple[str, object]] = []
    desktop = SimpleNamespace(
        from_point=lambda x, y: calls.append(("point", (x, y))) or "point-target",
        backend=SimpleNamespace(
            generic_wrapper_class=lambda info: calls.append(("wrap", info))
            or "focused-target"
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
    connection.execute(
        "ALTER TABLE action_event DROP COLUMN structural_observation"
    )
    connection.commit()
    connection.close()

    with CaptureSession.load(capture_dir) as capture:
        event = capture.raw_events()[0]
        assert event.structural_observation is None

    connection = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(action_event)")
    }
    connection.close()
    assert "structural_observation" in columns


def test_non_windows_factory_is_headless_safe() -> None:
    assert create_structural_observer(platform_name="linux") is None


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
