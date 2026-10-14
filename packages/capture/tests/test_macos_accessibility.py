"""Tests for the permissive native macOS accessibility implementation."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="PyObjC accessibility APIs are available only on macOS",
)


def test_ax_geometry_values_convert_to_primitives() -> None:
    import ApplicationServices

    from openadapt_capture.window import _macos

    point = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGPointType,
        (1.5, 2.5),
    )
    size = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGSizeType,
        (3.5, 4.5),
    )
    rect = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGRectType,
        ((1.0, 2.0), (3.0, 4.0)),
    )

    assert _macos.deepconvert_objc(point) == {
        "x": 1.5,
        "y": 2.5,
        "type": "CGPoint",
    }
    assert _macos.deepconvert_objc(size) == {
        "w": 3.5,
        "h": 4.5,
        "type": "CGSize",
    }
    assert _macos.deepconvert_objc(rect) == {
        "x": 1.0,
        "y": 2.0,
        "w": 3.0,
        "h": 4.0,
        "type": "CGRect",
    }


def test_structural_observer_reads_native_ax_geometry(monkeypatch) -> None:
    import ApplicationServices

    from openadapt_capture.structural_observer.macos import _AXRuntime

    position = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGPointType,
        (10.5, 20.5),
    )
    size = ApplicationServices.AXValueCreate(
        ApplicationServices.kAXValueCGSizeType,
        (100.0, 40.0),
    )
    element = object()
    runtime = _AXRuntime.__new__(_AXRuntime)
    runtime.ax = ApplicationServices
    monkeypatch.setattr(
        runtime,
        "attribute",
        lambda candidate, name: {
            (element, "AXPosition"): position,
            (element, "AXSize"): size,
        }.get((candidate, name)),
    )

    bounds = runtime.bounds(element)

    assert bounds is not None
    assert bounds.model_dump() == {
        "left": 10.5,
        "top": 20.5,
        "right": 110.5,
        "bottom": 60.5,
    }


def test_structural_observer_uses_the_native_ax_pid_signature() -> None:
    import ApplicationServices

    from openadapt_capture.structural_observer.macos import _AXRuntime

    runtime = _AXRuntime.__new__(_AXRuntime)
    runtime.ax = ApplicationServices
    system = ApplicationServices.AXUIElementCreateSystemWide()

    assert runtime.process_id(system) is None


def test_element_at_position_uses_native_application_services(
    monkeypatch,
) -> None:
    from openadapt_capture.window import _macos

    system_wide = object()
    element = object()
    calls = []

    monkeypatch.setattr(
        _macos.ApplicationServices,
        "AXUIElementCreateSystemWide",
        lambda: system_wide,
    )

    def copy_element(system, x, y, output):
        calls.append((system, x, y, output))
        return _macos.ApplicationServices.kAXErrorSuccess, element

    monkeypatch.setattr(
        _macos.ApplicationServices,
        "AXUIElementCopyElementAtPosition",
        copy_element,
    )
    monkeypatch.setattr(
        _macos,
        "dump_state",
        lambda candidate: {"AXRole": "AXButton"} if candidate is element else {},
    )

    assert _macos.get_active_element_state(120, 240) == {
        "AXRole": "AXButton"
    }
    assert calls == [(system_wide, 120.0, 240.0, None)]
