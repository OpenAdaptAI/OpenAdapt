"""Fail-closed Linux X11 window resolution and XComposite pixel capture.

The accessibility tree does not provide pixels.  This producer uses EWMH/X11
metadata to resolve one local top-level window and XComposite to name that
window's backing pixmap.  It refuses native Wayland sessions because Capture
does not yet own a portal session that binds a selected window, its pixels,
and event-time coordinates.

All XCB imports and display access stay inside call paths.  Importing this
module is safe on a headless host and on non-Linux platforms.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from PIL import Image

    from openadapt_capture.window_capture import TargetWindow, WindowTarget


class LinuxWindowCaptureError(RuntimeError):
    """The Linux session cannot produce an exact window-scoped frame."""


@dataclass(frozen=True)
class X11WindowRecord:
    """Process-neutral metadata read from one EWMH client window."""

    window_id: int
    title: str
    pid: int
    bounds: tuple[int, int, int, int]
    viewable: bool


@dataclass(frozen=True)
class X11PixelFormat:
    """The server format needed to decode one X11 ZPixmap reply."""

    bits_per_pixel: int
    scanline_pad: int
    image_byte_order: int
    red_mask: int
    green_mask: int
    blue_mask: int
    alpha_mask: int = 0
    visual_class: int = 4  # X11 TrueColor


class _X11ClientProtocol(Protocol):
    """Small injectable surface used by headless tests."""

    root_bounds: tuple[int, int, int, int]

    def window_ids(self) -> list[int]: ...

    def window_record(self, window_id: int) -> X11WindowRecord | None: ...

    def capture_window(self, window_id: int, width: int, height: int) -> "Image.Image": ...


def _require_x11_session(environ: dict[str, str] | None = None) -> str:
    """Return DISPLAY or refuse a session whose coordinate contract is unsafe."""
    values = os.environ if environ is None else environ
    session_type = values.get("XDG_SESSION_TYPE", "").strip().casefold()
    if session_type == "wayland" or values.get("WAYLAND_DISPLAY", "").strip():
        raise LinuxWindowCaptureError(
            "window-scoped capture refuses native Wayland sessions: no portal "
            "contract currently binds a selected window, exact pixels, and "
            "event-time coordinates; use an X11 session"
        )
    display = values.get("DISPLAY", "").strip()
    if not display:
        raise LinuxWindowCaptureError(
            "window-scoped capture requires an X11 desktop and DISPLAY is not set"
        )
    return display


def _bytes(value) -> bytes:
    """Copy an xcffib byte list without retaining its reply buffer."""
    try:
        return bytes(value)
    except TypeError:
        return b"".join(value)


def _u32_values(payload: bytes) -> list[int]:
    """Decode X11 32-bit property items in native client byte order."""
    import struct

    if len(payload) % 4:
        return []
    if not payload:
        return []
    return list(struct.unpack(f"={len(payload) // 4}I", payload))


def _rect_within(
    inner: tuple[int, int, int, int],
    outer: tuple[int, int, int, int],
) -> bool:
    """Return whether a positive rectangle is fully inside another rectangle."""
    x, y, width, height = inner
    ox, oy, outer_width, outer_height = outer
    return (
        width > 0
        and height > 0
        and x >= ox
        and y >= oy
        and x + width <= ox + outer_width
        and y + height <= oy + outer_height
    )


class X11CompositeClient(AbstractContextManager["X11CompositeClient"]):
    """One short-lived checked XCB connection to the active X11 screen."""

    def __init__(self, display: str | None = None) -> None:
        display_name = display or _require_x11_session()
        try:
            import xcffib
            import xcffib.composite
            import xcffib.xproto
        except (ImportError, OSError) as exc:
            raise LinuxWindowCaptureError(
                "Linux window capture requires xcffib and the system libxcb runtime"
            ) from exc

        self._xcffib = xcffib
        self._composite_module = xcffib.composite
        self._xproto = xcffib.xproto
        try:
            self._conn = xcffib.connect(display=display_name)
        except Exception as exc:
            raise LinuxWindowCaptureError(
                f"could not open X11 display {display_name!r}; authorize the capture process"
            ) from exc
        setup = self._conn.get_setup()
        try:
            self._screen = setup.roots[self._conn.pref_screen]
        except (IndexError, TypeError) as exc:
            self._conn.disconnect()
            raise LinuxWindowCaptureError("X11 did not expose the selected screen") from exc
        self._setup = setup
        self._root = int(self._screen.root)
        self.root_bounds = (
            0,
            0,
            int(self._screen.width_in_pixels),
            int(self._screen.height_in_pixels),
        )
        self._atoms: dict[str, int] = {}

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._conn.disconnect()

    def _atom(self, name: str, *, existing: bool = False) -> int:
        key = f"{int(existing)}:{name}"
        if key not in self._atoms:
            reply = self._conn.core.InternAtom(
                existing,
                len(name.encode("ascii")),
                name.encode("ascii"),
            ).reply()
            self._atoms[key] = int(reply.atom)
        return self._atoms[key]

    def _property(self, window_id: int, name: str, *, length: int) -> tuple[int, bytes]:
        atom = self._atom(name, existing=True)
        if atom == 0:
            return (0, b"")
        reply = self._conn.core.GetProperty(
            False,
            window_id,
            atom,
            self._xproto.Atom.Any,
            0,
            length,
        ).reply()
        return (int(reply.format), _bytes(reply.value))

    def window_ids(self) -> list[int]:
        """Return EWMH client windows in bottom-to-top stacking order."""
        for name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            value_format, payload = self._property(self._root, name, length=1 << 20)
            if value_format == 32:
                window_ids = [value for value in _u32_values(payload) if value]
                if window_ids:
                    return window_ids
        raise LinuxWindowCaptureError(
            "the X11 window manager does not expose an EWMH client window list"
        )

    def _title(self, window_id: int) -> str:
        for name in ("_NET_WM_NAME", "WM_NAME"):
            value_format, payload = self._property(window_id, name, length=4096)
            if value_format == 8 and payload:
                return payload.rstrip(b"\0").decode("utf-8", errors="replace")
        return ""

    def _pid(self, window_id: int) -> int:
        value_format, payload = self._property(window_id, "_NET_WM_PID", length=1)
        values = _u32_values(payload) if value_format == 32 else []
        return values[0] if len(values) == 1 else 0

    def window_record(self, window_id: int) -> X11WindowRecord | None:
        """Read one viewability, identity, and root-pixel geometry snapshot."""
        try:
            attributes = self._conn.core.GetWindowAttributes(window_id).reply()
            if int(attributes._class) != self._xproto.WindowClass.InputOutput or bool(
                attributes.override_redirect
            ):
                return None
            geometry = self._conn.core.GetGeometry(window_id).reply()
            translated = self._conn.core.TranslateCoordinates(
                window_id,
                self._root,
                0,
                0,
            ).reply()
            title = self._title(window_id)
            pid = self._pid(window_id)
        except Exception:
            # A client can close between EWMH list and metadata requests.
            return None
        if not translated.same_screen:
            return None
        bounds = (
            int(translated.dst_x),
            int(translated.dst_y),
            int(geometry.width),
            int(geometry.height),
        )
        return X11WindowRecord(
            window_id=window_id,
            title=title,
            pid=pid,
            bounds=bounds,
            viewable=(int(attributes.map_state) == self._xproto.MapState.Viewable),
        )

    def _visual(self, visual_id: int):
        for depth in self._screen.allowed_depths:
            for visual in depth.visuals:
                if int(visual.visual_id) == visual_id:
                    return visual
        raise LinuxWindowCaptureError(f"X11 visual {visual_id} is absent from the selected screen")

    def _pixel_format(self, depth: int, visual_id: int) -> X11PixelFormat:
        formats = [value for value in self._setup.pixmap_formats if int(value.depth) == depth]
        if len(formats) != 1:
            raise LinuxWindowCaptureError(f"X11 did not expose one pixmap format for depth {depth}")
        value = formats[0]
        visual = self._visual(visual_id)
        color_mask = int(visual.red_mask) | int(visual.green_mask) | int(visual.blue_mask)
        alpha_mask = ((1 << depth) - 1) & ~color_mask
        return X11PixelFormat(
            bits_per_pixel=int(value.bits_per_pixel),
            scanline_pad=int(value.scanline_pad),
            image_byte_order=int(self._setup.image_byte_order),
            red_mask=int(visual.red_mask),
            green_mask=int(visual.green_mask),
            blue_mask=int(visual.blue_mask),
            alpha_mask=alpha_mask,
            visual_class=int(visual._class),
        )

    def _name_window_pixmap(self, window_id: int) -> tuple[int, bool]:
        """Name an existing redirect, or create one automatic redirect."""
        composite = self._conn(self._composite_module.key)
        try:
            version = composite.QueryVersion(0, 4).reply()
        except Exception as exc:
            raise LinuxWindowCaptureError(
                "the X11 server does not expose the Composite extension"
            ) from exc
        if (int(version.major_version), int(version.minor_version)) < (0, 2):
            raise LinuxWindowCaptureError("XComposite 0.2 or newer is required")

        pixmap = self._conn.generate_id()
        try:
            composite.NameWindowPixmap(window_id, pixmap, is_checked=True).check()
            return (pixmap, False)
        except Exception:
            # A compositor usually redirects top-level windows already.  A
            # plain X server does not, so request automatic redirection and
            # undo only the redirect owned by this connection.
            redirected = False
            try:
                composite.RedirectWindow(
                    window_id,
                    self._composite_module.Redirect.Automatic,
                    is_checked=True,
                ).check()
                redirected = True
                pixmap = self._conn.generate_id()
                composite.NameWindowPixmap(
                    window_id,
                    pixmap,
                    is_checked=True,
                ).check()
            except Exception as exc:
                if redirected:
                    try:
                        composite.UnredirectWindow(
                            window_id,
                            self._composite_module.Redirect.Automatic,
                            is_checked=True,
                        ).check()
                    except Exception:
                        pass
                raise LinuxWindowCaptureError(
                    f"XComposite could not name window {window_id}'s backing pixmap"
                ) from exc
            return (pixmap, True)

    def capture_window(self, window_id: int, width: int, height: int) -> "Image.Image":
        """Read the exact named-window pixmap, independent of root occlusion."""
        if width <= 0 or height <= 0:
            raise LinuxWindowCaptureError(f"window {window_id} has empty bounds")
        try:
            attributes = self._conn.core.GetWindowAttributes(window_id).reply()
            if int(attributes.map_state) != self._xproto.MapState.Viewable:
                raise LinuxWindowCaptureError(f"window {window_id} is not viewable")
            window_geometry = self._conn.core.GetGeometry(window_id).reply()
        except LinuxWindowCaptureError:
            raise
        except Exception as exc:
            raise LinuxWindowCaptureError(
                f"could not revalidate X11 window {window_id} before capture"
            ) from exc
        actual_size = (int(window_geometry.width), int(window_geometry.height))
        if actual_size != (width, height):
            raise LinuxWindowCaptureError(
                f"window {window_id} changed size before capture: "
                f"expected {(width, height)}, got {actual_size}"
            )

        pixmap = 0
        redirected = False
        try:
            pixmap, redirected = self._name_window_pixmap(window_id)
            pixmap_geometry = self._conn.core.GetGeometry(pixmap).reply()
            pixmap_size = (int(pixmap_geometry.width), int(pixmap_geometry.height))
            if pixmap_size != (width, height):
                raise LinuxWindowCaptureError(
                    f"XComposite pixmap size {pixmap_size} does not match "
                    f"window size {(width, height)}"
                )
            reply = self._conn.core.GetImage(
                self._xproto.ImageFormat.ZPixmap,
                pixmap,
                0,
                0,
                width,
                height,
                0xFFFFFFFF,
            ).reply()
            if int(reply.depth) != int(pixmap_geometry.depth):
                raise LinuxWindowCaptureError("X11 image depth changed during capture")
            pixel_format = self._pixel_format(
                int(pixmap_geometry.depth),
                int(attributes.visual),
            )
            return decode_zpixmap(_bytes(reply.data), width, height, pixel_format)
        except LinuxWindowCaptureError:
            raise
        except Exception as exc:
            raise LinuxWindowCaptureError(
                f"XComposite pixel capture failed for window {window_id}"
            ) from exc
        finally:
            if pixmap:
                try:
                    self._conn.core.FreePixmap(pixmap, is_checked=True).check()
                except Exception:
                    pass
            if redirected:
                try:
                    composite = self._conn(self._composite_module.key)
                    composite.UnredirectWindow(
                        window_id,
                        self._composite_module.Redirect.Automatic,
                        is_checked=True,
                    ).check()
                except Exception:
                    pass


def _channel(pixels, mask: int):
    """Scale one contiguous TrueColor mask to an unsigned 8-bit channel."""
    import numpy as np

    if mask <= 0:
        raise LinuxWindowCaptureError("X11 TrueColor channel mask is empty")
    shift = (mask & -mask).bit_length() - 1
    maximum = mask >> shift
    if maximum & (maximum + 1):
        raise LinuxWindowCaptureError("X11 TrueColor channel mask is not contiguous")
    values = ((pixels & mask) >> shift).astype(np.uint64)
    return ((values * 255 + maximum // 2) // maximum).astype(np.uint8)


def decode_zpixmap(
    payload: bytes,
    width: int,
    height: int,
    pixel_format: X11PixelFormat,
) -> "Image.Image":
    """Decode an X11 TrueColor ZPixmap without assuming BGRA or 32-bit rows."""
    import numpy as np
    from PIL import Image

    if width <= 0 or height <= 0:
        raise LinuxWindowCaptureError("X11 image dimensions must be positive")
    if pixel_format.visual_class != 4:
        raise LinuxWindowCaptureError(
            "X11 window capture supports TrueColor visuals only; indexed colormaps "
            "cannot be decoded without a live colormap snapshot"
        )
    bits_per_pixel = pixel_format.bits_per_pixel
    scanline_pad = pixel_format.scanline_pad
    if bits_per_pixel not in {16, 24, 32}:
        raise LinuxWindowCaptureError(
            f"unsupported X11 ZPixmap depth: {bits_per_pixel} bits per pixel"
        )
    if scanline_pad not in {8, 16, 32}:
        raise LinuxWindowCaptureError(f"unsupported X11 scanline padding: {scanline_pad} bits")
    row_bits = width * bits_per_pixel
    row_bytes = ((row_bits + scanline_pad - 1) // scanline_pad) * (scanline_pad // 8)
    required = row_bytes * height
    if len(payload) < required:
        raise LinuxWindowCaptureError(
            f"X11 image payload is truncated: expected {required} bytes, got {len(payload)}"
        )
    raw = np.frombuffer(payload, dtype=np.uint8, count=required)
    little_endian = pixel_format.image_byte_order == 0
    if bits_per_pixel == 16:
        dtype = np.dtype("<u2" if little_endian else ">u2")
        pixels = np.ndarray(
            shape=(height, width),
            dtype=dtype,
            buffer=raw,
            strides=(row_bytes, 2),
        ).astype(np.uint32)
    elif bits_per_pixel == 32:
        dtype = np.dtype("<u4" if little_endian else ">u4")
        pixels = np.ndarray(
            shape=(height, width),
            dtype=dtype,
            buffer=raw,
            strides=(row_bytes, 4),
        ).astype(np.uint32)
    else:
        octets = np.ndarray(
            shape=(height, width, 3),
            dtype=np.uint8,
            buffer=raw,
            strides=(row_bytes, 3, 1),
        ).astype(np.uint32)
        if little_endian:
            pixels = octets[..., 0] | (octets[..., 1] << 8) | (octets[..., 2] << 16)
        else:
            pixels = (octets[..., 0] << 16) | (octets[..., 1] << 8) | octets[..., 2]
    if pixel_format.alpha_mask:
        alpha = _channel(pixels, pixel_format.alpha_mask)
        if not bool(np.all(alpha == 255)):
            raise LinuxWindowCaptureError(
                "X11 window pixels contain transparency and cannot be flattened into "
                "exact RGB pixels without the compositor's retained background"
            )
    rgb = np.stack(
        (
            _channel(pixels, pixel_format.red_mask),
            _channel(pixels, pixel_format.green_mask),
            _channel(pixels, pixel_format.blue_mask),
        ),
        axis=-1,
    )
    return Image.fromarray(rgb)


def _default_process_identity(pid: int) -> tuple[str, float]:
    """Bind an EWMH PID to the exact live process instance."""
    import psutil

    process = psutil.Process(pid)
    return (process.name(), float(process.create_time()))


def resolve_window_linux(
    target: "WindowTarget",
    *,
    client_factory: Callable[[], AbstractContextManager[_X11ClientProtocol]] | None = None,
    process_identity: Callable[[int], tuple[str, float]] | None = None,
) -> "TargetWindow | None":
    """Resolve the largest matching, fully on-screen EWMH client window."""
    _require_x11_session()
    from openadapt_capture.window_capture import TargetWindow

    factory = client_factory or X11CompositeClient
    identity_reader = process_identity or _default_process_identity
    owner_text = target.owner.casefold() if target.owner else None
    title_text = target.title.casefold() if target.title else None
    matches: list[tuple[int, TargetWindow]] = []
    try:
        with factory() as client:
            root_bounds = client.root_bounds
            for stacking_index, window_id in enumerate(client.window_ids()):
                record = client.window_record(window_id)
                if (
                    record is None
                    or not record.viewable
                    or record.pid <= 0
                    or not _rect_within(record.bounds, root_bounds)
                ):
                    continue
                if title_text is not None and title_text not in record.title.casefold():
                    continue
                try:
                    owner, process_start_time = identity_reader(record.pid)
                except Exception:
                    continue
                if not owner or owner_text is not None and owner_text not in owner.casefold():
                    continue
                matches.append(
                    (
                        stacking_index,
                        TargetWindow(
                            window_id=record.window_id,
                            owner=owner,
                            title=record.title,
                            pid=record.pid,
                            bounds=tuple(float(value) for value in record.bounds),
                            on_screen=True,
                            process_start_time=process_start_time,
                            coordinate_source="x11-root-physical-pixels",
                        ),
                    )
                )
    except LinuxWindowCaptureError:
        raise
    except Exception as exc:
        raise LinuxWindowCaptureError("X11 window resolution failed") from exc
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            item[1].bounds[2] * item[1].bounds[3],
            item[0],
        ),
    )[1]


def capture_window_linux(
    window: "TargetWindow",
    *,
    client_factory: Callable[[], AbstractContextManager[_X11ClientProtocol]] | None = None,
) -> "Image.Image":
    """Capture one process-bound X11 window from its named backing pixmap."""
    _require_x11_session()
    if not window.on_screen:
        raise LinuxWindowCaptureError(f"window {window.window_id} is not on screen")
    x, y, width_value, height_value = window.bounds
    values = (x, y, width_value, height_value)
    if any(not float(value).is_integer() for value in values):
        raise LinuxWindowCaptureError(
            "X11 root-pixel window bounds must contain integer coordinates"
        )
    width = int(width_value)
    height = int(height_value)
    factory = client_factory or X11CompositeClient
    try:
        with factory() as client:
            if not _rect_within(tuple(int(value) for value in values), client.root_bounds):
                raise LinuxWindowCaptureError(
                    f"window {window.window_id} is not fully on the X11 root screen"
                )
            image = client.capture_window(window.window_id, width, height)
    except LinuxWindowCaptureError:
        raise
    except Exception as exc:
        raise LinuxWindowCaptureError(
            f"XComposite pixel capture failed for window {window.window_id}"
        ) from exc
    if image.size != (width, height):
        raise LinuxWindowCaptureError(
            f"XComposite returned {image.size} for window bounds {(width, height)}"
        )
    return image


__all__ = [
    "LinuxWindowCaptureError",
    "X11CompositeClient",
    "X11PixelFormat",
    "X11WindowRecord",
    "capture_window_linux",
    "decode_zpixmap",
    "resolve_window_linux",
]
