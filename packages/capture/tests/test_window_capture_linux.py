"""Linux X11 window producer contracts, including an opt-in live check."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_capture.desktop_capture import DesktopCaptureScope
from openadapt_capture.window_capture import (
    TargetWindow,
    WindowCaptureError,
    WindowCaptureScope,
    WindowTarget,
)
from openadapt_capture.window_capture_linux import (
    LinuxWindowCaptureError,
    X11CompositeClient,
    X11PixelFormat,
    X11WindowRecord,
    capture_window_linux,
    decode_zpixmap,
    resolve_window_linux,
)


class FakeX11Client:
    """Display-free implementation of the Linux producer's X11 seam."""

    def __init__(
        self,
        records: list[X11WindowRecord],
        *,
        root_bounds: tuple[int, int, int, int] = (0, 0, 1920, 1080),
        image: Image.Image | None = None,
    ) -> None:
        self.records = {record.window_id: record for record in records}
        self.order = [record.window_id for record in records]
        self.root_bounds = root_bounds
        self.image = image
        self.capture_calls: list[tuple[int, int, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def window_ids(self) -> list[int]:
        return list(self.order)

    def window_record(self, window_id: int) -> X11WindowRecord | None:
        return self.records.get(window_id)

    def capture_window(self, window_id: int, width: int, height: int) -> Image.Image:
        self.capture_calls.append((window_id, width, height))
        return self.image or Image.new("RGB", (width, height), "navy")


@pytest.fixture
def x11_environment(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def _record(
    window_id: int,
    *,
    title: str = "Remote session",
    pid: int = 1000,
    bounds: tuple[int, int, int, int] = (100, 80, 800, 600),
    viewable: bool = True,
) -> X11WindowRecord:
    return X11WindowRecord(
        window_id=window_id,
        title=title,
        pid=pid,
        bounds=bounds,
        viewable=viewable,
    )


def test_resolver_binds_process_and_root_pixel_coordinates(x11_environment) -> None:
    client = FakeX11Client([_record(41), _record(42, bounds=(50, 40, 1200, 700))])
    identities = {
        1000: ("citrix-workspace", 1700000000.25),
    }

    window = resolve_window_linux(
        WindowTarget(owner="citrix", title="remote"),
        client_factory=lambda: client,
        process_identity=identities.__getitem__,
    )

    assert window == TargetWindow(
        window_id=42,
        owner="citrix-workspace",
        title="Remote session",
        pid=1000,
        bounds=(50.0, 40.0, 1200.0, 700.0),
        on_screen=True,
        process_start_time=1700000000.25,
        coordinate_source="x11-root-physical-pixels",
    )


@pytest.mark.parametrize(
    "record",
    [
        _record(1, viewable=False),
        _record(1, bounds=(-1, 0, 100, 100)),
        _record(1, bounds=(1850, 1000, 100, 100)),
        _record(1, pid=0),
    ],
)
def test_resolver_refuses_unviewable_offscreen_or_unbound_windows(
    x11_environment,
    record: X11WindowRecord,
) -> None:
    client = FakeX11Client([record])
    assert (
        resolve_window_linux(
            WindowTarget(owner="citrix"),
            client_factory=lambda: client,
            process_identity=lambda _pid: ("citrix", 100.0),
        )
        is None
    )


def test_resolver_refuses_process_lookup_failure(x11_environment) -> None:
    client = FakeX11Client([_record(1)])

    def missing_process(_pid: int):
        raise ProcessLookupError

    assert (
        resolve_window_linux(
            WindowTarget(title="remote"),
            client_factory=lambda: client,
            process_identity=missing_process,
        )
        is None
    )


@pytest.mark.parametrize(
    ("session_type", "wayland_display"),
    [("wayland", None), ("x11", "wayland-0")],
)
def test_resolver_refuses_native_wayland(
    monkeypatch,
    session_type: str,
    wayland_display: str | None,
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", session_type)
    if wayland_display is None:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    else:
        monkeypatch.setenv("WAYLAND_DISPLAY", wayland_display)

    with pytest.raises(LinuxWindowCaptureError, match="refuses native Wayland"):
        resolve_window_linux(
            WindowTarget(owner="citrix"),
            client_factory=lambda: FakeX11Client([]),
        )


def test_resolver_refuses_missing_x11_display(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    with pytest.raises(LinuxWindowCaptureError, match="DISPLAY is not set"):
        resolve_window_linux(
            WindowTarget(owner="citrix"),
            client_factory=lambda: FakeX11Client([]),
        )


def test_capture_uses_exact_named_window_size(x11_environment) -> None:
    expected = Image.new("RGB", (800, 600), "purple")
    client = FakeX11Client([_record(42)], image=expected)
    window = TargetWindow(
        window_id=42,
        owner="citrix",
        title="Remote session",
        pid=1000,
        bounds=(100.0, 80.0, 800.0, 600.0),
        process_start_time=100.0,
        coordinate_source="x11-root-physical-pixels",
    )

    image = capture_window_linux(window, client_factory=lambda: client)

    assert image is expected
    assert client.capture_calls == [(42, 800, 600)]


def test_capture_refuses_wrong_pixel_extent(x11_environment) -> None:
    client = FakeX11Client([_record(42)], image=Image.new("RGB", (799, 600)))
    window = TargetWindow(
        window_id=42,
        owner="citrix",
        title="Remote session",
        pid=1000,
        bounds=(100.0, 80.0, 800.0, 600.0),
        process_start_time=100.0,
        coordinate_source="x11-root-physical-pixels",
    )
    with pytest.raises(LinuxWindowCaptureError, match="returned .* for window bounds"):
        capture_window_linux(window, client_factory=lambda: client)


class _Cookie:
    def __init__(self, *, reply=None, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error

    def reply(self):
        if self._error:
            raise self._error
        return self._reply

    def check(self) -> None:
        if self._error:
            raise self._error


class _CompositeFailure:
    def __init__(self) -> None:
        self.name_calls = 0
        self.redirect_calls = 0
        self.unredirect_calls = 0

    def QueryVersion(self, *_args):
        return _Cookie(reply=SimpleNamespace(major_version=0, minor_version=4))

    def NameWindowPixmap(self, *_args, **_kwargs):
        self.name_calls += 1
        return _Cookie(error=RuntimeError("name failed"))

    def RedirectWindow(self, *_args, **_kwargs):
        self.redirect_calls += 1
        return _Cookie()

    def UnredirectWindow(self, *_args, **_kwargs):
        self.unredirect_calls += 1
        return _Cookie()


def test_xcomposite_redirect_is_undone_when_pixmap_naming_fails() -> None:
    composite = _CompositeFailure()
    client = object.__new__(X11CompositeClient)
    client._composite_module = SimpleNamespace(
        key="composite", Redirect=SimpleNamespace(Automatic=0)
    )
    # Special methods resolve on the type, so use a small callable connection.
    client._conn = type(
        "FakeConnection",
        (),
        {
            "generate_id": lambda self: next(self.ids),
            "__call__": lambda self, _key: composite,
            "ids": iter([100, 101]),
        },
    )()

    with pytest.raises(LinuxWindowCaptureError, match="could not name"):
        client._name_window_pixmap(42)

    assert composite.name_calls == 2
    assert composite.redirect_calls == 1
    assert composite.unredirect_calls == 1


def test_decode_32_bit_little_endian_truecolor() -> None:
    image = decode_zpixmap(
        bytes(
            [
                0x00,
                0x00,
                0xFF,
                0x00,
                0x00,
                0xFF,
                0x00,
                0x00,
            ]
        ),
        2,
        1,
        X11PixelFormat(
            bits_per_pixel=32,
            scanline_pad=32,
            image_byte_order=0,
            red_mask=0x00FF0000,
            green_mask=0x0000FF00,
            blue_mask=0x000000FF,
        ),
    )
    assert [image.getpixel((x, 0)) for x in range(2)] == [(255, 0, 0), (0, 255, 0)]


def test_decode_16_bit_rgb565_with_row_padding() -> None:
    # One RGB565 pixel plus a two-byte scanline pad.
    image = decode_zpixmap(
        bytes([0x00, 0xF8, 0x00, 0x00]),
        1,
        1,
        X11PixelFormat(
            bits_per_pixel=16,
            scanline_pad=32,
            image_byte_order=0,
            red_mask=0xF800,
            green_mask=0x07E0,
            blue_mask=0x001F,
        ),
    )
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_decode_refuses_indexed_visual() -> None:
    with pytest.raises(LinuxWindowCaptureError, match="TrueColor visuals only"):
        decode_zpixmap(
            b"\0" * 4,
            1,
            1,
            X11PixelFormat(
                bits_per_pixel=32,
                scanline_pad=32,
                image_byte_order=0,
                red_mask=0,
                green_mask=0,
                blue_mask=0,
                visual_class=3,
            ),
        )


def test_decode_refuses_nonopaque_argb_pixels() -> None:
    with pytest.raises(LinuxWindowCaptureError, match="contain transparency"):
        decode_zpixmap(
            bytes([0x00, 0x00, 0xFF, 0x7F]),
            1,
            1,
            X11PixelFormat(
                bits_per_pixel=32,
                scanline_pad=32,
                image_byte_order=0,
                red_mask=0x00FF0000,
                green_mask=0x0000FF00,
                blue_mask=0x000000FF,
                alpha_mask=0xFF000000,
            ),
        )


def _linux_target(bounds=(700.0, 100.0, 600.0, 500.0)) -> TargetWindow:
    return TargetWindow(
        window_id=42,
        owner="citrix",
        title="Remote session",
        pid=1000,
        bounds=bounds,
        process_start_time=100.0,
        coordinate_source="x11-root-physical-pixels",
    )


def _topology(monitors: list[list[int]]) -> dict:
    return {
        "schema_version": "openadapt.capture.display-topology/v1",
        "coordinate_space": "virtual_desktop_pixels",
        "monitors": monitors,
        "topology_sha256": "a" * 64,
    }


def test_scope_accepts_window_spanning_adjacent_monitors() -> None:
    target = _linux_target()
    scope = WindowCaptureScope(
        WindowTarget(owner="citrix"),
        resolver=lambda _target: target,
        capturer=lambda _window: Image.new("RGB", (600, 500)),
    )
    scope.bind_display_topology(
        _topology([[0, 0, 1000, 800], [1000, 0, 1000, 800]]),
        lambda **_kwargs: None,
    )
    image, _changed = scope.capture_frame()
    assert image.size == (600, 500)


def test_scope_refuses_window_crossing_a_topology_gap() -> None:
    target = _linux_target()
    scope = WindowCaptureScope(
        WindowTarget(owner="citrix"),
        resolver=lambda _target: target,
        capturer=lambda _window: Image.new("RGB", (600, 500)),
    )
    scope.bind_display_topology(
        _topology([[0, 0, 800, 800], [1000, 0, 1000, 800]]),
        lambda **_kwargs: None,
    )
    with pytest.raises(WindowCaptureError, match="not fully covered"):
        scope.capture_frame()


_LIVE_LINUX = sys.platform.startswith("linux")
_LIVE_ENABLED = os.environ.get("OPENADAPT_CAPTURE_LINUX_WINDOW_QUALIFICATION") == "1"


@pytest.mark.slow
@pytest.mark.skipif(not _LIVE_LINUX, reason="Linux X11 qualification runs only on Linux")
@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason="set OPENADAPT_CAPTURE_LINUX_WINDOW_QUALIFICATION=1 on an interactive X11 rig",
)
def test_live_linux_xcomposite_window_capture() -> None:
    """Qualify identity, topology, exact pixels, and revalidation on a live rig."""
    owner = os.environ.get("OPENADAPT_WINDOW_SMOKE_OWNER", "").strip()
    title = os.environ.get("OPENADAPT_WINDOW_SMOKE_TITLE", "").strip() or None
    assert owner, "OPENADAPT_WINDOW_SMOKE_OWNER must name the qualification application"
    scope = WindowCaptureScope(WindowTarget(owner=owner, title=title))
    desktop = DesktopCaptureScope.current()
    scope.bind_display_topology(desktop.snapshot(), desktop.assert_current)

    first, changed = scope.capture_frame()
    assert changed is True
    state = scope.window_event_data()["state"]
    assert state["coordinate_source"] == "x11-root-physical-pixels"
    assert state["source_viewport"] == [int(state["bounds"][2]), int(state["bounds"][3])]
    assert first.size == tuple(state["source_viewport"])
    assert state["pid"] > 0
    assert state["process_start_time"] > 0

    second, _changed = scope.capture_frame()
    scope.assert_current()
    assert second.size == first.size
    assert scope.snapshot()["window_id"] == state["window_id"]
