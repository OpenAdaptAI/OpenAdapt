"""Regressions against reporting a capture that did not happen.

Every test here pins the boundary between "measured / observed" and "could not
measure / could not observe". A plausible default returned from the second case
is written into the recording and is indistinguishable from a real measurement.
"""

from __future__ import annotations

import importlib
import queue
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from openadapt_capture import platform as capture_platform
from openadapt_capture import recorder, window
from openadapt_capture.platform import DisplayMetricsUnavailable
from openadapt_capture.structural import StructuralObservationRequest
from openadapt_capture.structural_observer.windows import WindowsUIAStructuralObserver
from openadapt_capture.window import WindowCaptureUnavailable


def _unavailable_provider(*_args, **_kwargs):
    raise NotImplementedError("Platform not supported: test")


class TestDisplayMetricsAreMeasuredOrRefused:
    def test_pixel_ratio_raises_instead_of_returning_one(self, monkeypatch) -> None:
        # 1.0 is a real, common answer. Returning it for an unmeasurable
        # display records a Retina screen as standard density and rescales
        # every captured coordinate by half.
        monkeypatch.setattr(
            capture_platform, "get_platform_provider", _unavailable_provider
        )

        with pytest.raises(DisplayMetricsUnavailable):
            capture_platform.get_display_pixel_ratio()

    def test_screen_dimensions_raise_instead_of_returning_1920x1080(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            capture_platform, "get_platform_provider", _unavailable_provider
        )

        class _BrokenImageGrab:
            @staticmethod
            def grab():
                raise OSError("no display")

        monkeypatch.setitem(sys.modules, "PIL.ImageGrab", _BrokenImageGrab)

        with pytest.raises(DisplayMetricsUnavailable):
            capture_platform.get_screen_dimensions()

    def test_accessibility_state_is_none_when_undetermined(self, monkeypatch) -> None:
        # `True` here used to mean "I did not check".
        monkeypatch.setattr(
            capture_platform, "get_platform_provider", _unavailable_provider
        )

        assert capture_platform.is_accessibility_enabled() is None

    def test_recording_stores_null_pixel_ratio_when_unmeasurable(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(recorder.utils, "get_monitor_dims", lambda: (1280, 720))
        monkeypatch.setattr(
            recorder.utils, "get_double_click_distance_pixels", lambda: 5.0
        )
        monkeypatch.setattr(
            recorder.utils, "get_double_click_interval_seconds", lambda: 0.5
        )

        def _unmeasurable() -> float:
            raise DisplayMetricsUnavailable("probe failed")

        monkeypatch.setattr(
            recorder.platform, "get_display_pixel_ratio", _unmeasurable
        )

        recording, _db_path = recorder.create_recording(
            "test task", capture_dir=str(tmp_path / "capture")
        )

        # NULL, not 1.0: the column is nullable so that unknown is
        # representable in the recording itself.
        assert recording.pixel_ratio is None


class TestWindowBackendAvailability:
    def test_require_impl_raises_when_no_backend_loaded(self, monkeypatch) -> None:
        monkeypatch.setattr(window, "impl", None)
        monkeypatch.setattr(
            window, "impl_unavailable_reason", "backend failed to import"
        )

        with pytest.raises(WindowCaptureUnavailable):
            window.require_impl()

    def test_active_window_data_is_none_not_empty_dict(self, monkeypatch) -> None:
        # The annotation and docstring both promise None. Returning {} made
        # "no backend" look like a window with no fields.
        monkeypatch.setattr(window, "get_active_window_state", lambda _flag: None)

        assert window.get_active_window_data() is None

    def test_window_reader_refuses_instead_of_polling_forever(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(window, "impl", None)
        monkeypatch.setattr(
            window, "impl_unavailable_reason", "backend failed to import"
        )
        started_event = threading.Event()
        terminate = threading.Event()

        with pytest.raises(WindowCaptureUnavailable):
            recorder.read_window_events(
                queue.Queue(),
                terminate,
                SimpleNamespace(timestamp=100.0),
                started_event,
            )

        # The pre-fix loop spun on an empty result and never announced
        # readiness, so recording startup hung with no stated cause.
        assert not started_event.is_set()


class _WrapperWithUnreadableElement:
    """A UIA wrapper whose element_info read fails, as on a vanished element."""

    @property
    def element_info(self):
        raise OSError("COM call failed")

    def friendly_class_name(self) -> str | None:
        return "Button"

    def parent(self):
        return None

    def top_level_parent(self):
        return self

    def descendants(self, **_kwargs):
        return []

    def window_text(self) -> str | None:
        return None


class TestStructuralObservationIsEvidence:
    def test_unreadable_element_yields_no_observation(self) -> None:
        target = _WrapperWithUnreadableElement()
        observer = WindowsUIAStructuralObserver(
            runtime=SimpleNamespace(
                from_point=lambda _x, _y: target,
                focused_element=lambda: target,
            ),
            process_name_resolver=lambda _pid: None,
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

        # An observation with an identity-free element is positive evidence
        # that windows_uia observed this action. It must not be emitted when
        # nothing could be read.
        assert observed is None


class TestLinuxElementState:
    def test_element_state_is_none_not_a_placeholder_dict(self, monkeypatch) -> None:
        # Import the Linux backend on any host by stubbing its X11 dependency:
        # the function under test never touches X11, and gating this test on
        # Linux would leave the regression unproven on the PR matrix.
        for name in ("xcffib", "xcffib.xproto"):
            stub = ModuleType(name)
            stub.Connection = object  # annotation target at import time
            monkeypatch.setitem(sys.modules, name, stub)
        monkeypatch.delitem(sys.modules, "openadapt_capture.window._linux", raising=False)
        _linux = importlib.import_module("openadapt_capture.window._linux")

        # The placeholder dict was persisted verbatim onto every ActionEvent
        # and read downstream as captured accessibility data.
        assert _linux.get_active_element_state(10, 20) is None
