"""Local-OS pixel backend: drive a REMOTE-DISPLAY CLIENT WINDOW, pixels only.

This is the faithful **Citrix analog** the healthcare pilot actually reuses. Over
Citrix, the local machine holds a **Citrix Workspace/Receiver window that paints
the pixels of a remote session**; there is no in-guest agent on our side of the
ICA boundary and **UIA/MSAA does not cross ICA**. The production Accuro-over-Citrix
wire is therefore NOT the RDP protocol client (:mod:`openadapt_flow.backends.rdp_backend`
speaks RDP, which is not ICA) — it is a **local-OS backend that screenshots the
Workspace client window and injects OS-level input into it**. This module is that
backend, built on macOS (Quartz / AppKit) against any on-screen client window.

For the proof on infra we control the client window is the **Parallels Desktop VM
window** showing the guest desktop: same *class* of substrate as Citrix Workspace
— host-side pixels of a remote guest, host OS input injected into the window, and
**no access to the guest UIA tree through the window**. Swapping the target from
the Parallels window to a "Citrix Workspace"/"Accuro" window is a one-line title
change; the screenshot + inject code is identical. (What real Citrix adds — HDX
compression, network latency, DPI/credential/lock-screen drift — is documented in
``docs/desktop/CITRIX_PIXEL.md``, not simulated here.)

Faithful pixel-only property (the whole point):

* :meth:`RemoteDisplayBackend.screenshot` captures **only the client window's
  pixels** (``CGWindowListCreateImage`` by window id) — the driving process sees
  the remote display, nothing else.
* input is injected at the **host OS level** (``CGEvent`` mouse / keyboard /
  wheel) into that window, mapped from screenshot-pixel space to screen points.
* the backend deliberately implements **only** the base
  :class:`openadapt_flow.backend.Backend` protocol — NOT ``StructuralBackend``,
  ``IdentityBackend`` or ``StructuralActionBackend``. So the resolver's
  ``structural`` (UIA) rung is **unavailable** and resolution runs on the visual
  floor (template / OCR / geometry / grounder); identity falls back to the OCR
  name+DOB tier — exactly the Citrix constraint (``backend.py`` protocol notes,
  ``docs/desktop/LIMITS.md``).

Permission reality (macOS, and the honest failure mode): window capture needs
**Screen Recording** permission and input injection needs **Accessibility**
permission for the driving app (Terminal / the Python host). If Accessibility is
not granted, ``CGEventPost`` is **silently dropped** — a dropped click that looked
like success is the exact "silent wrong action" this project refuses. So every
input method FAILS LOUD (raises :class:`RemoteDisplayError`) when the process is
not Accessibility-trusted; a caller never mistakes a no-op for a completed action.

The macOS bindings are isolated behind :class:`MacWindowClient` (a small
:class:`WindowClient` seam) so the coordinate math, the pixel<->point mapping, and
the fail-loud contract are unit-testable with a fake client and no live window.
The Windows-host counterpart of that seam is
:class:`openadapt_flow.backends.win32_window_client.Win32WindowClient`
(PrintWindow/BitBlt client-area capture + SendInput injection, per-monitor-v2
DPI, same fail-loud contract); :class:`RemoteDisplayBackend` picks the host's
client automatically when none is injected.
"""

from __future__ import annotations

import io
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RemoteDisplayError(RuntimeError):
    """A remote-display capture/inject operation failed (or is not permitted)."""


@dataclass(frozen=True)
class WindowInfo:
    """One on-screen window, as reported by the window server.

    ``bounds`` is ``(x, y, w, h)`` in **screen points** (top-left origin — the
    same space ``CGEvent`` mouse coordinates use), so a captured-pixel point maps
    to a screen point by ``bounds_origin + pixel / scale``.
    """

    window_id: int
    owner: str
    title: str
    pid: int
    bounds: tuple[float, float, float, float]
    on_screen: bool = True


# macOS US-layout virtual key codes for printable characters. A synthetic
# Unicode keystroke (keycode 0 + ``CGEventKeyboardSetUnicodeString``) is NOT
# forwarded into a Parallels/Citrix guest — the remote display forwards
# hardware-like SCANCODES, so a real key code (with Shift for the upper glyph)
# is required. ``(keycode, needs_shift)``.
_CHAR_KEYCODES: dict[str, tuple[int, bool]] = {}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _CHAR_KEYCODES[_ch] = (
        {
            "a": 0x00,
            "b": 0x0B,
            "c": 0x08,
            "d": 0x02,
            "e": 0x0E,
            "f": 0x03,
            "g": 0x05,
            "h": 0x04,
            "i": 0x22,
            "j": 0x26,
            "k": 0x28,
            "l": 0x25,
            "m": 0x2E,
            "n": 0x2D,
            "o": 0x1F,
            "p": 0x23,
            "q": 0x0C,
            "r": 0x0F,
            "s": 0x01,
            "t": 0x11,
            "u": 0x20,
            "v": 0x09,
            "w": 0x0D,
            "x": 0x07,
            "y": 0x10,
            "z": 0x06,
        }[_ch],
        False,
    )
    _CHAR_KEYCODES[_ch.upper()] = (_CHAR_KEYCODES[_ch][0], True)
_DIGIT_KEYCODES = {
    "0": 0x1D,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "5": 0x17,
    "6": 0x16,
    "7": 0x1A,
    "8": 0x1C,
    "9": 0x19,
}
for _d, _kc in _DIGIT_KEYCODES.items():
    _CHAR_KEYCODES[_d] = (_kc, False)
# punctuation / symbols used by clinical text (base glyph, then shifted glyph)
_CHAR_KEYCODES.update(
    {
        " ": (0x31, False),
        ".": (0x2F, False),
        ",": (0x2B, False),
        "-": (0x1B, False),
        "/": (0x2C, False),
        ";": (0x29, False),
        "=": (0x18, False),
        "'": (0x27, False),
        "[": (0x21, False),
        "]": (0x1E, False),
        "\\": (0x2A, False),
        "`": (0x32, False),
        ":": (0x29, True),
        "_": (0x1B, True),
        "?": (0x2C, True),
        "!": (0x12, True),
        "(": (0x19, True),
        ")": (0x1D, True),
        "+": (0x18, True),
        '"': (0x27, True),
        "@": (0x13, True),
        "#": (0x14, True),
        "%": (0x17, True),
        "&": (0x1A, True),
        "*": (0x1C, True),
        "$": (0x15, True),
    }
)

# macOS virtual key codes for the named keys the Backend protocol emits. A
# printable character is typed via ``_CHAR_KEYCODES`` above, so only
# non-printable keys need a code here.
_MAC_KEYCODES = {
    "enter": 0x24,
    "return": 0x24,
    "tab": 0x30,
    "escape": 0x35,
    "esc": 0x35,
    "backspace": 0x33,
    "delete": 0x75,  # forward delete
    "space": 0x31,
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "pagedown": 0x79,
    "up": 0x7E,
    "down": 0x7D,
    "left": 0x7B,
    "right": 0x7C,
    # single letters used inside chords (e.g. Ctrl+A select-all)
    "a": 0x00,
    "c": 0x08,
    "v": 0x09,
    "x": 0x07,
    "z": 0x06,
}

# Modifier tokens -> a symbolic flag name resolved against Quartz at call time
# (kept as strings so this module imports without Quartz, for CI). On a Windows
# GUEST the app expects Ctrl-based shortcuts, so ``meta``/``ctrl``/``controlormeta``
# all map to the CONTROL flag — a Ctrl+A the guest reads as select-all — matching
# WindowsBackend's ``controlormeta -> ctrl`` decision.
_MODIFIER_FLAGS = {
    "ctrl": "control",
    "control": "control",
    "controlormeta": "control",
    "meta": "control",
    "cmd": "control",
    "command": "control",
    "win": "control",
    "alt": "alternate",
    "option": "alternate",
    "shift": "shift",
}

_NAMED_KEY_ALIASES = {
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _png_size(png: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a PNG's IHDR chunk."""
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


def _split_chord(key: str) -> tuple[list[str], str]:
    """Split ``'ControlOrMeta+a'`` into (modifier tokens, final key token).

    Returns the modifiers (canonicalized flag names) and the final key token
    (a named key or a single character), each lower-cased. Raises ValueError on
    an empty chord.
    """
    parts = [p for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    mods: list[str] = []
    for p in parts[:-1]:
        flag = _MODIFIER_FLAGS.get(p.lower())
        if flag is None:
            raise ValueError(f"unknown modifier in chord {key!r}: {p!r}")
        mods.append(flag)
    final = parts[-1]
    final = _NAMED_KEY_ALIASES.get(final.lower(), final)
    return mods, final


@runtime_checkable
class WindowClient(Protocol):
    """Minimal host-OS window client the backend drives (seam for tests).

    Deliberately tiny and honest: locate a window, capture its pixels, and
    inject OS-level input at screen points. The real macOS implementation is
    :class:`MacWindowClient`; tests pass a fake. Coordinates passed to the input
    methods are **screen points** (top-left origin).
    """

    def input_trusted(self) -> bool:
        """True iff this process may inject OS input (Accessibility granted)."""
        ...

    def frontmost_pid(self) -> Optional[int]:
        """PID of the frontmost application, or None if unknown."""
        ...

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        """Return every exact owner/title match in window-server z-order."""
        ...

    def key_window_id(self, pid: int) -> Optional[int]:
        """Return the front/key normal-window id for ``pid``, or None."""
        ...

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        """Return the topmost visible window id accepting a screen point."""
        ...

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        """Capture window ``window_id``; return ``(png_bytes, px_w, px_h)``."""
        ...

    def activate(self, pid: int) -> None:
        """Un-hide and bring the app owning ``pid`` frontmost (route keystrokes)."""
        ...

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        """Post a mouse button transition at screen point (x, y)."""
        ...

    def mouse_move(self, x: float, y: float) -> None:
        """Post a mouse-moved event to screen point (x, y)."""
        ...

    def type_chars(self, text: str) -> None:
        """Type ``text`` into the focused window via hardware-like key codes."""
        ...

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        """Post a key transition for ``keycode`` with modifier ``flags``."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Post a wheel gesture (line units; Backend sign convention)."""
        ...

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        """Resolve a named-key or single-character token from a chord to
        ``(client keycode, needs_shift)``, or None when unmapped.

        Keycodes are CLIENT-DEFINED (macOS virtual key codes vs Windows VKs);
        the backend never interprets them, it only round-trips them into
        :meth:`key`. An unmapped token makes the backend halt loudly.
        """
        ...


def resolve_mac_key(token: str) -> Optional[tuple[int, bool]]:
    """macOS key resolution: named-key table first, then US-layout characters.

    Shared by :class:`MacWindowClient` and the offline test fakes so the
    backend's chord semantics stay byte-identical to the historical in-backend
    lookup (named keys resolve unshifted; an upper-case character token adds
    Shift).
    """
    code = _MAC_KEYCODES.get(token.lower())
    if code is not None:
        return code, False
    return _CHAR_KEYCODES.get(token)


def _default_window_client() -> "WindowClient":
    """The host OS's live window client (macOS Quartz or Win32).

    Fail-loud on hosts with no window-scoped replay client rather than
    constructing a client whose bindings can never work.
    """
    if sys.platform == "darwin":
        return MacWindowClient()
    if sys.platform == "win32":
        from openadapt_flow.backends.win32_window_client import Win32WindowClient

        return Win32WindowClient()
    raise RemoteDisplayError(
        f"no host WindowClient exists for platform {sys.platform!r}; "
        "window-scoped remote-display replay is implemented on macOS "
        "(Quartz) and Windows (Win32) hosts only"
    )


class RemoteDisplayBackend:
    """`Backend` over a remote-display CLIENT WINDOW (pixels in, OS input out).

    Args:
        client: The host-OS window client (a real :class:`MacWindowClient` /
            ``Win32WindowClient`` or a fake). Defaults to the current host's
            live client; unsupported hosts fail loud at construction.
        owner_substr: Exact case-insensitive owner-app name (default
            ``"Parallels Desktop"``; use the exact Citrix Workspace owner name
            for a real ICA client).
        title_substr: Optional exact case-insensitive window title. Zero or
            multiple matches fail closed; the backend never picks the largest
            result from a partial match.
        require_input_trust: When True (default) every input method raises if the
            process is not Accessibility-trusted, so a silently-dropped click can
            never masquerade as a completed action.
        activate_before_input: When True (default) raise the target app frontmost
            before each input burst so keystrokes route to it.
        settle_s: Seconds to pause after activating / between click edges.
        max_frame_age_s: Maximum age of the captured frame whose pixel geometry
            an input may use. Older coordinates are refused and must be
            re-captured/re-resolved.
        readiness_probe: Optional deployment-specific pixel predicate evaluated
            before every input. Return False on a lock/login/disconnect or
            unexpected application screen to fail closed. Generic client-window
            capture cannot identify a remote session lock state portably.
    """

    def __init__(
        self,
        client: Optional[WindowClient] = None,
        *,
        owner_substr: str = "Parallels Desktop",
        title_substr: Optional[str] = None,
        require_input_trust: bool = True,
        activate_before_input: bool = True,
        settle_s: float = 0.03,
        max_frame_age_s: float = 10.0,
        readiness_probe: Optional[Callable[[bytes], bool]] = None,
    ) -> None:
        self._client = client if client is not None else _default_window_client()
        self._owner_substr = owner_substr
        self._title_substr = title_substr
        self._require_input_trust = require_input_trust
        self._activate_before_input = activate_before_input
        self._settle_s = settle_s
        self._max_frame_age_s = float(max_frame_age_s)
        if self._max_frame_age_s <= 0:
            raise ValueError("max_frame_age_s must be positive")
        self._readiness_probe = readiness_probe
        self._window: Optional[WindowInfo] = None
        self._viewport: Optional[tuple[int, int]] = None
        self._scale: float = 1.0
        self._scale_x: float = 1.0
        self._scale_y: float = 1.0
        self._frame_window: Optional[WindowInfo] = None
        self._last_frame_monotonic: Optional[float] = None
        # Serialize capture/geometry validation with the entire input gesture;
        # otherwise another thread can replace the frame lease between mouse
        # down and up or between a key's down/up edges.
        self._input_lock = threading.RLock()

    # -- window resolution ---------------------------------------------------

    def _resolve_window(self, *, refresh: bool = False) -> WindowInfo:
        """Locate (and cache) the target client window.

        Raises:
            RemoteDisplayError: If no matching window is on screen.
        """
        if self._window is not None and not refresh:
            return self._window
        matches = self._client.find_windows(self._owner_substr, self._title_substr)
        if not matches:
            raise RemoteDisplayError(
                "no window exactly matching the configured remote-display "
                "target; is the client window open and visible?"
            )
        if len(matches) != 1:
            raise RemoteDisplayError(
                "ambiguous remote-display target: expected one exact owner/title "
                f"match, found {len(matches)}; target identities are withheld "
                "because window titles can contain sensitive record data"
            )
        self._window = matches[0]
        return matches[0]

    def ensure_foreground(self, *, retries: int = 10, settle_s: float = 0.4) -> None:
        """Un-hide + activate the client window and wait until it is on screen.

        A remote-display client window can be Cmd+H-hidden or de-fronted between
        actions; a hidden window captures nothing. Call this once before driving
        (and the harness may re-call it). Requires input trust only insofar as
        activation is a UI operation; it never injects input.

        Raises:
            RemoteDisplayError: If the window never comes on screen.
        """
        with self._input_lock:
            win = self._resolve_window(refresh=True)
            for _ in range(max(1, retries)):
                self._client.activate(win.pid)
                time.sleep(settle_s)
                win = self._resolve_window(refresh=True)
                # on_screen alone is insufficient: a window occluded by another
                # app is still "on screen", yet clicks would hit the occluder.
                if (
                    win.on_screen
                    and self._client.frontmost_pid() == win.pid
                    and self._client.key_window_id(win.pid) == win.window_id
                ):
                    return
        raise RemoteDisplayError(
            "configured client window did not come to the foreground "
            "(frontmost check failed)"
        )

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the captured client-window frame, in pixels."""
        if self._viewport is None:
            self.screenshot()
        assert self._viewport is not None
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the client window's current pixels as PNG bytes.

        Captures ONLY the target window (``CGWindowListCreateImage`` by id), so
        the frame is exactly the remote display — the coordinate space
        :meth:`click` maps into. Also refreshes the cached scale (captured
        pixels per screen point) and viewport so they can never disagree with
        the frame just returned.

        Raises:
            RemoteDisplayError: If the window is gone or capture returns nothing.
        """
        with self._input_lock:
            # Refresh on every capture: a cached id/bounds is unsafe after the
            # user moves, resizes, zooms, or reopens a remote-display window.
            win = self._resolve_window(refresh=True)
            try:
                png, px_w, px_h = self._client.capture(win.window_id)
            except RemoteDisplayError:
                # The window id may be stale (window reopened); re-resolve once.
                win = self._resolve_window(refresh=True)
                png, px_w, px_h = self._client.capture(win.window_id)
            if not png or px_w <= 0 or px_h <= 0:
                raise RemoteDisplayError("client-window capture returned no pixels")
            # Validate and record the frame's true pixel size.
            w, h = _png_size(png)
            if (w, h) != (int(px_w), int(px_h)):
                raise RemoteDisplayError(
                    f"capture metadata {(px_w, px_h)!r} disagrees with PNG {(w, h)!r}"
                )
            bounds_w, bounds_h = win.bounds[2], win.bounds[3]
            if bounds_w <= 0 or bounds_h <= 0:
                raise RemoteDisplayError(
                    f"client window has invalid bounds {win.bounds!r}"
                )
            scale_x, scale_y = w / bounds_w, h / bounds_h
            # One captured-pixel coordinate must map to one unambiguous screen
            # point. Anisotropic scaling indicates chrome/crop/zoom geometry we
            # have not calibrated; guessing would mis-target clicks.
            if abs(scale_x - scale_y) > max(0.01, 0.01 * max(scale_x, scale_y)):
                raise RemoteDisplayError(
                    "captured frame and client bounds have inconsistent DPI scale "
                    f"({scale_x:.4f}x vs {scale_y:.4f}y); refusing uncalibrated input"
                )
            self._viewport = (w, h)
            self._scale_x, self._scale_y = scale_x, scale_y
            self._scale = scale_x  # compatibility for existing diagnostics
            self._frame_window = win
            self._last_frame_monotonic = time.monotonic()
            return png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at captured-pixel coordinates (x, y)."""
        with self._input_lock:
            self._ensure_input_ready(point=(int(x), int(y)))
            sx, sy = self._to_screen(int(x), int(y))
            self._assert_click_target(sx, sy)
            self._assert_frame_fresh()
            self._client.mouse_move(sx, sy)
            time.sleep(self._settle_s)
            counts = 2 if double else 1
            for i in range(counts):
                # Activation/focus and the move/settle call above can block.
                # Revalidate immediately before every pointer-down edge.
                self._assert_click_target(sx, sy)
                self._assert_frame_fresh()
                self._client.mouse(sx, sy, button="left", down=True, click_count=i + 1)
                time.sleep(self._settle_s)
                self._client.mouse(sx, sy, button="left", down=False, click_count=i + 1)
                time.sleep(self._settle_s)

    def type_text(self, text: str) -> None:
        """Type ``text`` into the focused control (hardware-like key codes)."""
        if not text:
            return
        with self._input_lock:
            self._ensure_input_ready()
            self._client.type_chars(text)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``.

        Modifiers are pressed (flags applied to the key events), the final key
        is pressed and released, then modifiers are released. Every key-up is
        best-effort in a ``finally`` so a failure can never leave a modifier
        latched (a stuck Ctrl silently corrupts the next input — a wrong action).
        """
        mods, final = _split_chord(key)
        with self._input_lock:
            self._ensure_input_ready()
            # A bare printable key with no modifiers: type it as a character.
            if len(final) == 1 and not mods:
                self._client.type_chars(final)
                return
            # Named key, or a modified key: the CLIENT owns the keycode
            # namespace (macOS virtual key codes vs Windows VKs), so key
            # resolution is delegated to it; an unmapped token halts loudly.
            resolved = self._client.resolve_key(final)
            if resolved is None:
                raise RemoteDisplayError(f"no key mapping for {final!r} in {key!r}")
            code, shift = resolved
            flags = list(mods) + (["shift"] if shift else [])
            try:
                self._client.key(code, down=True, flags=flags)
            finally:
                self._client.key(code, down=False, flags=flags)

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture by ``(dx, dy)`` pixels."""
        if dx == 0 and dy == 0:
            return
        with self._input_lock:
            self._ensure_input_ready()
            self._client.scroll(int(dx), int(dy))

    # -- internals -----------------------------------------------------------

    def _ensure_input_ready(self, *, point: Optional[tuple[int, int]] = None) -> None:
        """Fail LOUD if input can't actually be delivered; else focus the app.

        A silently-dropped synthetic event (Accessibility not granted) would let
        a no-op look like a completed click/keystroke — the silent wrong action
        this project refuses. So refuse to act at all when untrusted.
        """
        if self._require_input_trust and not self._client.input_trusted():
            raise RemoteDisplayError(
                "process is not trusted to inject OS input, so input would be "
                "silently dropped. macOS: grant Accessibility to the driving "
                "app (System Settings > Privacy & Security > Accessibility). "
                "Windows: the target window belongs to an elevated (or "
                "unknown-elevation) process and UIPI would discard our input "
                "— run the driver elevated. Refusing to emit an input that "
                "cannot be delivered (a dropped click must never look like "
                "success)."
            )
        if point is not None and self._last_frame_monotonic is None:
            raise RemoteDisplayError(
                "no captured frame lease for coordinate input; capture and resolve "
                "the target before clicking"
            )
        if self._activate_before_input:
            win = self._resolve_window(refresh=True)
            self._client.activate(win.pid)
            time.sleep(self._settle_s)
        current = self._resolve_window(refresh=True)
        if (
            not current.on_screen
            or self._client.frontmost_pid() != current.pid
            or self._client.key_window_id(current.pid) != current.window_id
        ):
            raise RemoteDisplayError(
                "the exact remote-display window is not visible, app-frontmost, "
                "and keyboard-frontmost after activation; refusing input"
            )
        if self._last_frame_monotonic is None:
            # Establish a frame lease for direct Backend callers. Once a lease
            # exists, staleness fails closed so pixel coordinates are never
            # silently refreshed without resolver involvement.
            self.screenshot()
            current = self._resolve_window(refresh=True)
        assert self._last_frame_monotonic is not None
        self._assert_frame_fresh()
        assert self._frame_window is not None
        lease = self._frame_window
        if (
            current.window_id != lease.window_id
            or current.pid != lease.pid
            or current.bounds != lease.bounds
        ):
            raise RemoteDisplayError(
                "remote-display window identity or geometry changed since capture; "
                "capture and re-resolve before input"
            )
        assert self._viewport is not None
        if point is not None:
            x, y = point
            if not (0 <= x < self._viewport[0] and 0 <= y < self._viewport[1]):
                raise RemoteDisplayError(
                    f"input point {(x, y)!r} is outside captured frame "
                    f"{self._viewport!r}"
                )
        if self._readiness_probe is not None:
            # Check current pixels without replacing the resolver's coordinate
            # lease. This detects a lock/disconnect that appears after capture
            # while preserving the requirement to re-resolve before acting on
            # changed content.
            png, px_w, px_h = self._client.capture(current.window_id)
            if _png_size(png) != self._viewport or (px_w, px_h) != self._viewport:
                raise RemoteDisplayError(
                    "remote-display dimensions changed during readiness check; "
                    "capture and re-resolve before input"
                )
            if not self._readiness_probe(png):
                raise RemoteDisplayError(
                    "remote-display readiness probe rejected the current frame "
                    "(locked, disconnected, or unexpected session); refusing input"
                )
        # Activation, window resolution, capture and readiness/OCR may all
        # block. Re-resolve the exact window/key identity and age again at the
        # last common point before input.
        post = self._resolve_window(refresh=True)
        if (
            not post.on_screen
            or self._client.frontmost_pid() != post.pid
            or self._client.key_window_id(post.pid) != post.window_id
            or post.window_id != lease.window_id
            or post.pid != lease.pid
            or post.bounds != lease.bounds
        ):
            raise RemoteDisplayError(
                "remote-display window identity, key-window state, or geometry "
                "changed during readiness validation; refusing input"
            )
        self._assert_frame_fresh()

    def _assert_frame_fresh(self) -> None:
        if self._last_frame_monotonic is None:
            raise RemoteDisplayError("no captured frame lease")
        age = time.monotonic() - self._last_frame_monotonic
        if age > self._max_frame_age_s:
            raise RemoteDisplayError(
                f"remote-display frame is stale ({age:.3f}s > "
                f"{self._max_frame_age_s:.3f}s); halting intentionally so the "
                "runtime can capture, re-resolve, and re-check identity"
            )

    def _assert_click_target(self, sx: float, sy: float) -> None:
        assert self._frame_window is not None
        hit = self._client.window_at_point(sx, sy)
        if hit != self._frame_window.window_id:
            raise RemoteDisplayError(
                f"screen point {(sx, sy)!r} is covered by window {hit!r}, not "
                f"the leased remote-display window {self._frame_window.window_id}; "
                "refusing a click that could hit an occluder"
            )

    def _to_screen(self, px: int, py: int) -> tuple[float, float]:
        """Map a captured-pixel point to a screen point (for CGEvent)."""
        if self._frame_window is None or self._viewport is None:
            raise RemoteDisplayError("no captured frame lease; capture before input")
        win = self._frame_window
        ox, oy = win.bounds[0], win.bounds[1]
        return (ox + px / self._scale_x, oy + py / self._scale_y)


# =============================================================================
# Real macOS client (Quartz / AppKit). Imported lazily so this module loads on
# any platform / in CI without pyobjc.
# =============================================================================


class MacWindowClient:
    """Live :class:`WindowClient` over Quartz (capture/input) + AppKit (activate).

    Every macOS binding is imported inside the method that uses it, so importing
    this class costs nothing and CI (Linux) can import the module to unit-test
    :class:`RemoteDisplayBackend` against a fake client.
    """

    def input_trusted(self) -> bool:
        try:
            from ApplicationServices import AXIsProcessTrusted

            return bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001 - absence == untrusted
            return False

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        """macOS virtual key code for a named-key/character chord token."""
        return resolve_mac_key(token)

    def capture_trusted(self) -> bool:
        """Whether this process may capture other applications' windows."""
        try:
            import Quartz

            preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
            # Screen Recording consent did not exist before macOS 10.15. When
            # the preflight API is absent, window capture itself remains the
            # authoritative check performed by ``capture``.
            return True if preflight is None else bool(preflight())
        except Exception:  # noqa: BLE001 - absence cannot be treated as consent
            return False

    def request_capture_access(self) -> bool:
        """Ask macOS to show its Screen Recording consent prompt."""
        try:
            import Quartz

            request = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
            return True if request is None else bool(request())
        except Exception:  # noqa: BLE001
            return False

    def request_input_access(self) -> bool:
        """Ask macOS to show its Accessibility consent prompt."""
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            return bool(
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            )
        except Exception:  # noqa: BLE001
            return False

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        """Return every exact owner/title match, front to back.

        Both the native macOS and remote-display backends perform their own
        uniqueness gate. Exact case-insensitive matching here keeps a broad
        substring from silently expanding the candidate set between capture
        and input while still tolerating harmless case variation.
        """
        import Quartz

        owner_l = owner.casefold()
        title_l = title.casefold() if title is not None else None
        # kCGWindowListOptionAll (not OnScreenOnly): a remote-display client is
        # sometimes momentarily HIDDEN (Cmd+H / focus loss) — we still need its
        # pid to un-hide + foreground it, and CGWindowListCreateImage can capture
        # a de-fronted window. on_screen is recorded so a caller can foreground.
        opts = Quartz.kCGWindowListOptionAll
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        matches: list[WindowInfo] = []
        for w in wins or []:
            actual_owner = str(w.get("kCGWindowOwnerName", "") or "")
            name = str(w.get("kCGWindowName", "") or "")
            if actual_owner.casefold() != owner_l:
                continue
            if title_l is not None and name.casefold() != title_l:
                continue
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                continue
            b = w.get("kCGWindowBounds", {}) or {}
            bounds = (
                float(b.get("X", 0.0)),
                float(b.get("Y", 0.0)),
                float(b.get("Width", 0.0)),
                float(b.get("Height", 0.0)),
            )
            window_id = int(w.get("kCGWindowNumber", 0) or 0)
            pid = int(w.get("kCGWindowOwnerPID", 0) or 0)
            if window_id <= 0 or pid <= 0 or bounds[2] <= 0 or bounds[3] <= 0:
                continue
            matches.append(
                WindowInfo(
                    window_id=window_id,
                    owner=actual_owner,
                    title=name,
                    pid=pid,
                    bounds=bounds,
                    on_screen=bool(w.get("kCGWindowIsOnscreen", False)),
                )
            )
        return matches

    def find_window(self, owner: str, title: Optional[str]) -> Optional[WindowInfo]:
        """Compatibility helper over the exact, ambiguity-visible candidate API."""
        best: Optional[WindowInfo] = None
        best_area = -1.0
        for window in self.find_windows(owner, title):
            area = window.bounds[2] * window.bounds[3]
            if area > best_area:
                best_area = area
                best = window
        return best

    def key_window_id(self, pid: int) -> Optional[int]:
        """Best public window-server proxy for an app's keyboard/key window.

        CoreGraphics returns on-screen windows front-to-back. The first normal
        layer window owned by the frontmost app is the only window we admit for
        keyboard input; multiple same-process windows therefore cannot be
        confused by merely checking the PID.
        """
        import Quartz

        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for w in wins or []:
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                continue
            if int(w.get("kCGWindowOwnerPID", 0) or 0) != int(pid):
                continue
            return int(w.get("kCGWindowNumber", 0) or 0) or None
        return None

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        """Return the topmost visible window accepting a screen point."""
        return self.window_id_at_point(x, y)

    def frontmost_window_id(self) -> Optional[int]:
        """ID of the topmost on-screen normal window, or None if unknown."""
        try:
            import Quartz

            opts = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
            for window in wins or []:
                if int(window.get("kCGWindowLayer", 0) or 0) != 0:
                    continue
                window_id = int(window.get("kCGWindowNumber", 0) or 0)
                if window_id > 0:
                    return window_id
        except Exception:  # noqa: BLE001
            return None
        return None

    def window_id_at_point(self, x: float, y: float) -> Optional[int]:
        """Frontmost visible window containing a screen point, any layer."""
        try:
            import Quartz

            opts = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
            for window in wins or []:
                bounds = window.get("kCGWindowBounds", {}) or {}
                left = float(bounds.get("X", 0.0))
                top = float(bounds.get("Y", 0.0))
                width = float(bounds.get("Width", 0.0))
                height = float(bounds.get("Height", 0.0))
                if width <= 0 or height <= 0:
                    continue
                if left <= x < left + width and top <= y < top + height:
                    window_id = int(window.get("kCGWindowNumber", 0) or 0)
                    return window_id if window_id > 0 else None
        except Exception:  # noqa: BLE001 - unknown point target fails closed
            return None
        return None

    def raise_window(self, window: WindowInfo) -> bool:
        """Raise one exact native window within its owning application.

        App activation alone does not select a document when macOS restores
        several windows into one process. Match the already-unique
        CoreGraphics target by its exact AX title, require one AX candidate,
        and perform ``AXRaise``. The native backend independently re-checks
        the exact CoreGraphics window id afterward, so a stale title or failed
        AX mapping remains a fail-closed refusal.
        """
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue,
                AXUIElementCreateApplication,
                AXUIElementPerformAction,
                AXUIElementSetAttributeValue,
                kAXFocusedAttribute,
                kAXFocusedWindowAttribute,
                kAXMainAttribute,
                kAXRaiseAction,
                kAXTitleAttribute,
                kAXWindowsAttribute,
            )

            app = AXUIElementCreateApplication(int(window.pid))
            error, ax_windows = AXUIElementCopyAttributeValue(
                app, kAXWindowsAttribute, None
            )
            if error != 0 or ax_windows is None:
                return False
            matches = []
            for candidate in ax_windows:
                title_error, title = AXUIElementCopyAttributeValue(
                    candidate, kAXTitleAttribute, None
                )
                if title_error == 0 and str(title or "") == window.title:
                    matches.append(candidate)
            if len(matches) != 1:
                return False
            target = matches[0]
            # AXRaise alone changes visual stacking but need not make the
            # document the app's key/main window. Request all three states;
            # unsupported attributes remain harmless because the native
            # backend independently requires the exact topmost CoreGraphics
            # id. Physical/global input additionally requires active app PID;
            # exact-element AX text does not route through that global stream.
            for element, attribute, value in (
                (app, kAXFocusedWindowAttribute, target),
                (target, kAXMainAttribute, True),
                (target, kAXFocusedAttribute, True),
            ):
                try:
                    AXUIElementSetAttributeValue(element, attribute, value)
                except Exception:  # noqa: BLE001 - final foreground gate decides
                    pass
            if AXUIElementPerformAction(target, kAXRaiseAction) != 0:
                return False
            focused_error, focused_window = AXUIElementCopyAttributeValue(
                app, kAXFocusedWindowAttribute, None
            )
            main_error, is_main = AXUIElementCopyAttributeValue(
                target, kAXMainAttribute, None
            )
            return (
                focused_error == 0
                and focused_window == target
                and main_error == 0
                and bool(is_main)
            )
        except Exception:  # noqa: BLE001 - caller verifies exact topmost id
            return False

    def replace_selected_text(self, window: WindowInfo, text: str) -> bool:
        """Replace the selection in the exact target window's focused element.

        Quartz events may carry a Unicode payload, but application frameworks
        are permitted to ignore that payload and translate only the virtual
        keycode. A discarded payload must not look like successful native text
        delivery. Accessibility selected-text replacement is layout independent
        and returns an explicit delivery result. This method requires a unique
        AX window title and proves that the focused element belongs to it before
        writing; the caller separately verifies the exact topmost CG id. The
        active-app PID remains mandatory only for global/physical input.
        """
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue,
                AXUIElementCreateApplication,
                AXUIElementIsAttributeSettable,
                AXUIElementSetAttributeValue,
                kAXFocusedUIElementAttribute,
                kAXFocusedWindowAttribute,
                kAXMainAttribute,
                kAXSelectedTextAttribute,
                kAXTitleAttribute,
                kAXTopLevelUIElementAttribute,
                kAXWindowsAttribute,
            )

            app = AXUIElementCreateApplication(int(window.pid))
            windows_error, ax_windows = AXUIElementCopyAttributeValue(
                app, kAXWindowsAttribute, None
            )
            if windows_error != 0 or ax_windows is None:
                return False
            matching_windows = []
            for candidate in ax_windows:
                title_error, title = AXUIElementCopyAttributeValue(
                    candidate, kAXTitleAttribute, None
                )
                if title_error == 0 and str(title or "") == window.title:
                    matching_windows.append(candidate)
            if len(matching_windows) != 1:
                return False
            target = matching_windows[0]

            focused_window_error, focused_window = AXUIElementCopyAttributeValue(
                app, kAXFocusedWindowAttribute, None
            )
            main_error, is_main = AXUIElementCopyAttributeValue(
                target, kAXMainAttribute, None
            )
            if (
                focused_window_error != 0
                or focused_window != target
                or main_error != 0
                or not bool(is_main)
            ):
                return False

            focused_error, focused = AXUIElementCopyAttributeValue(
                app, kAXFocusedUIElementAttribute, None
            )
            if focused_error != 0 or focused is None:
                return False
            top_error, top_level = AXUIElementCopyAttributeValue(
                focused, kAXTopLevelUIElementAttribute, None
            )
            if top_error != 0 or top_level is None:
                return False
            top_title_error, top_title = AXUIElementCopyAttributeValue(
                top_level, kAXTitleAttribute, None
            )
            if top_title_error != 0 or str(top_title or "") != window.title:
                return False
            if top_level != target:
                return False

            settable_error, settable = AXUIElementIsAttributeSettable(
                focused, kAXSelectedTextAttribute, None
            )
            if settable_error != 0 or not settable:
                return False
            return (
                AXUIElementSetAttributeValue(focused, kAXSelectedTextAttribute, text)
                == 0
            )
        except Exception:  # noqa: BLE001 - unsupported AX is a safe refusal
            return False

    def exact_window_focused_main(self, window: WindowInfo) -> bool:
        """Whether one exact AX window is both focused and main, read-only."""
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue,
                AXUIElementCreateApplication,
                kAXFocusedWindowAttribute,
                kAXMainAttribute,
                kAXTitleAttribute,
                kAXWindowsAttribute,
            )

            app = AXUIElementCreateApplication(int(window.pid))
            windows_error, ax_windows = AXUIElementCopyAttributeValue(
                app, kAXWindowsAttribute, None
            )
            if windows_error != 0 or ax_windows is None:
                return False
            matches = []
            for candidate in ax_windows:
                title_error, title = AXUIElementCopyAttributeValue(
                    candidate, kAXTitleAttribute, None
                )
                if title_error == 0 and str(title or "") == window.title:
                    matches.append(candidate)
            if len(matches) != 1:
                return False
            target = matches[0]
            focused_error, focused = AXUIElementCopyAttributeValue(
                app, kAXFocusedWindowAttribute, None
            )
            main_error, is_main = AXUIElementCopyAttributeValue(
                target, kAXMainAttribute, None
            )
            return (
                focused_error == 0
                and focused == target
                and main_error == 0
                and bool(is_main)
            )
        except Exception:  # noqa: BLE001 - missing proof must fail closed
            return False

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        import Quartz
        from PIL import Image

        img_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if img_ref is None:
            raise RemoteDisplayError(
                f"CGWindowListCreateImage returned None for {window_id}"
            )
        w = int(Quartz.CGImageGetWidth(img_ref))
        h = int(Quartz.CGImageGetHeight(img_ref))
        if w <= 0 or h <= 0:
            raise RemoteDisplayError("captured image has zero size")
        bpr = int(Quartz.CGImageGetBytesPerRow(img_ref))
        provider = Quartz.CGImageGetDataProvider(img_ref)
        data = Quartz.CGDataProviderCopyData(provider)
        buf = bytes(data)
        # CGImage from the window server is BGRA, premultiplied; PIL reads BGRA
        # with a row stride and we drop alpha for a stable RGB PNG.
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", bpr, 1)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue(), w, h

    def frontmost_pid(self) -> Optional[int]:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else None
        except Exception:  # noqa: BLE001
            return None

    def focused_application_pid(self) -> Optional[int]:
        """PID accepting keyboard input according to system-wide AX state.

        ``NSWorkspace.frontmostApplication`` can lag behind the accessibility
        and WindowServer focus state under remote control. Global physical
        input therefore uses the system-wide ``AXFocusedApplication`` proof,
        which Apple defines as the application accepting keyboard input. Any
        missing attribute, AX error, invalid PID, or binding failure is an
        unknown result and must remain a fail-closed refusal at the backend.
        """
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue,
                AXUIElementCreateSystemWide,
                AXUIElementGetPid,
                kAXFocusedApplicationAttribute,
            )

            system = AXUIElementCreateSystemWide()
            focused_error, focused_app = AXUIElementCopyAttributeValue(
                system, kAXFocusedApplicationAttribute, None
            )
            if focused_error != 0 or focused_app is None:
                return None
            pid_error, pid = AXUIElementGetPid(focused_app, None)
            if pid_error != 0 or pid is None or int(pid) <= 0:
                return None
            return int(pid)
        except Exception:  # noqa: BLE001 - missing proof must fail closed
            return None

    def activate(self, pid: int) -> None:
        # Use AppKit's explicit source-aware handoff where available. Merely
        # raising an AX window can put it visually above the active application
        # while NSWorkspace still routes key events elsewhere. The caller must
        # observe the target as both active app and exact topmost CG window.
        try:
            from AppKit import (
                NSApplicationActivateAllWindows,
                NSApplicationActivateIgnoringOtherApps,
                NSRunningApplication,
                NSWorkspace,
            )

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None:
                return
            if app.isHidden():
                app.unhide()
            options = (
                NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
            )
            source = NSWorkspace.sharedWorkspace().frontmostApplication()
            transfer = getattr(app, "activateFromApplication_options_", None)
            transferred = False
            if (
                source is not None
                and int(source.processIdentifier()) != int(pid)
                and transfer is not None
            ):
                transferred = bool(transfer(source, options))
            if not transferred:
                app.activateWithOptions_(options)
        except Exception:  # noqa: BLE001 - activation is best-effort focus
            pass
        # Preserve the remote-display/Citrix path's legacy focus fallback.
        # Accessory/background driver processes cannot always transfer focus
        # through AppKit alone. This exact-PID System Events request changes
        # only application focus; native input still verifies AX focused-app,
        # exact AX window, and exact topmost CG id before emitting any event.
        if self.frontmost_pid() == pid:
            return
        try:
            import subprocess

            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to set frontmost of '
                    f"(first process whose unix id is {int(pid)}) to true",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception:  # noqa: BLE001 - caller's focus proof is authoritative
            pass

    # -- input injection (screen points) ------------------------------------

    def _flags_mask(self, flags: list[str]) -> int:
        import Quartz

        mask = 0
        table = {
            "control": Quartz.kCGEventFlagMaskControl,
            "command": Quartz.kCGEventFlagMaskCommand,
            "alternate": Quartz.kCGEventFlagMaskAlternate,
            "shift": Quartz.kCGEventFlagMaskShift,
        }
        for f in flags:
            mask |= int(table.get(f, 0))
        return mask

    def _post(self, event: object) -> None:
        import Quartz

        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        import Quartz

        kind = {
            ("left", True): Quartz.kCGEventLeftMouseDown,
            ("left", False): Quartz.kCGEventLeftMouseUp,
            ("right", True): Quartz.kCGEventRightMouseDown,
            ("right", False): Quartz.kCGEventRightMouseUp,
        }[(button, down)]
        btn = (
            Quartz.kCGMouseButtonLeft
            if button == "left"
            else Quartz.kCGMouseButtonRight
        )
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
        if click_count > 1:
            Quartz.CGEventSetIntegerValueField(
                ev, Quartz.kCGMouseEventClickState, int(click_count)
            )
        self._post(ev)

    def mouse_move(self, x: float, y: float) -> None:
        import Quartz

        ev = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
        )
        self._post(ev)

    def type_chars(self, text: str) -> None:
        import Quartz

        for ch in text:
            mapping = _CHAR_KEYCODES.get(ch)
            if mapping is not None:
                keycode, shift = mapping
                flags = ["shift"] if shift else []
                self.key(keycode, down=True, flags=flags)
                self.key(keycode, down=False, flags=flags)
            else:
                # Fallback for a character with no US-layout code: a synthetic
                # Unicode keystroke. NOTE: a remote-display guest may drop this
                # (it forwards scancodes) — hence the keycode path is primary.
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                    self._post(ev)
            time.sleep(0.006)

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        import Quartz

        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        mask = self._flags_mask(flags)
        if mask:
            Quartz.CGEventSetFlags(ev, mask)
        self._post(ev)

    def scroll(self, dx: int, dy: int) -> None:
        import Quartz

        # Backend sign convention: positive dy scrolls content up / view down.
        # CGEvent wheel positive scrolls content down / view up, so negate dy.
        lines_v = -_lines(dy)
        lines_h = _lines(dx)
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, lines_v, lines_h
        )
        self._post(ev)


def _lines(pixels: int) -> int:
    """Convert a pixel delta to wheel lines (~40 px per line; >=1 when nonzero)."""
    if pixels == 0:
        return 0
    lines = round(abs(pixels) / 40)
    return max(1, lines) * (1 if pixels > 0 else -1)
