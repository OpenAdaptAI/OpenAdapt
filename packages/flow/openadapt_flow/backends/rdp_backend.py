"""FreeRDP / RDP backend: drive a pixel-only remote desktop over RDP.

The L1/Retinology wedge (docs/L1_INTEGRATION.md) reaches a legacy
ophthalmology EMR over **RDP**, read **pixel-only** — there is no accessibility
tree, no DOM, no structured layer of any kind. That is exactly the substrate
the vision-only runtime was built for: PNG frames in, pixel-coordinate clicks
and keys out. So RDP is *an adapter, not a rewrite* — this module implements
the :class:`openadapt_flow.backend.Backend` protocol on top of a small,
swappable RDP transport.

Design (two layers, so the adapter is CI-testable without a live RDP server
and the RDP library stays replaceable):

* :class:`RDPTransport` — a minimal, honest protocol for *any* RDP client:
  ``connect`` / ``disconnect`` / ``framebuffer`` / ``pointer`` / ``key`` /
  ``wheel``. Nothing flow-specific leaks in.
* :class:`FreeRDPBackend` — implements the flow ``Backend`` protocol in terms
  of an ``RDPTransport``: ``screenshot`` PNG-encodes the framebuffer,
  ``viewport`` reports its size, ``click`` sends a pointer down/up (double =
  the sequence twice), ``type_text`` sends per-character key down/up,
  ``press`` decomposes a key/chord into ordered key down/up events, ``scroll``
  sends a wheel gesture.
* :class:`AardwolfTransport` — a real transport over the pure-Python async
  ``aardwolf`` RDP client, bridged to the sync ``RDPTransport`` API with a
  dedicated event-loop thread. Lazily imported and gated behind the optional
  ``rdp`` extra (``pip install 'openadapt-flow[rdp]'``); importing this module
  never imports aardwolf.

``press`` additionally detects an optional ``supports_physical_key`` /
``physical_key`` transport seam. Aardwolf implements it with layout-bound
scancodes because Unicode text events cannot participate in physical Windows
shortcuts; ``type_text`` deliberately stays on the Unicode ``key`` path.

Coordinate space: the backend works entirely in **framebuffer pixels** — the
same pixels the resolver emits and the same pixels :meth:`screenshot` encodes,
because both come from :meth:`RDPTransport.framebuffer`. A transport that
downsamples the remote desktop MUST report the downsampled ``(width, height)``
from ``framebuffer`` so screenshot pixels and click pixels stay in one space;
no scaling then happens here (see :class:`AardwolfTransport`, which is 1:1).

Identity note: RDP is a **pure-pixel substrate** — there is no structured
(DOM / a11y) text to read, so this backend deliberately does NOT implement the
optional ``IdentityBackend.structured_text_at`` (nor the ``StructuralBackend``
url/title/page-count observations). Claiming a capability it cannot honor would
be a lie; identity instead falls back to the OCR name+DOB-primary tier exactly
as documented for pixel-only substrates (openadapt_flow/backend.py,
docs/LIMITS.md). This mirrors how WindowsBackend omits StructuralBackend.
"""

from __future__ import annotations

import io
import threading
import time
from typing import Callable, Optional, Protocol, Union, runtime_checkable

from PIL import Image

# What a transport may hand back as the current frame: a PIL image, or raw
# pixel bytes (RGB or RGBA, row-major) that the backend wraps with the
# reported (width, height).
Framebuffer = tuple[Union["Image.Image", bytes], int, int]


@runtime_checkable
class RDPTransport(Protocol):
    """Minimal transport an RDP client must expose to back a flow backend.

    Deliberately tiny and honest: a single framebuffer read plus three input
    primitives. Anything that can connect to a remote desktop, hand back the
    current frame, and inject pointer/key/wheel events can be a transport —
    the real :class:`AardwolfTransport`, or a scripted fake in tests.

    All methods are SYNCHRONOUS. An async RDP client (aardwolf) is bridged to
    this API behind a dedicated event-loop thread (see
    :class:`AardwolfTransport`).
    """

    def connect(self) -> None:
        """Establish the RDP session (blocks until a frame is available)."""
        ...

    def disconnect(self) -> None:
        """Tear the RDP session down (idempotent; never raises)."""
        ...

    def framebuffer(self) -> Framebuffer:
        """Return ``(frame, width, height)`` for the current desktop.

        ``frame`` is either a PIL ``Image`` or raw RGB/RGBA bytes; ``width``
        and ``height`` are the pixel dimensions of THAT frame (already
        downsampled if the transport downsamples), so they define the single
        coordinate space the backend clicks in.
        """
        ...

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        """Send a pointer button transition at framebuffer pixel (x, y).

        ``button`` is ``'left'`` / ``'right'`` / ``'middle'``; ``down`` is the
        press (True) or release (False) edge.
        """
        ...

    def key(self, keysym_or_char: str, down: bool) -> None:
        """Send a key transition.

        ``keysym_or_char`` is either a single character to type (``'a'``,
        ``'1'``, ``'!'``) or a normalized key name (``'enter'``, ``'tab'``,
        ``'ctrl'``, ``'shift'``, ``'up'`` ...); ``down`` is press/release.
        """
        ...

    def wheel(self, dx: int, dy: int) -> None:
        """Send a wheel gesture by ``(dx, dy)`` framebuffer pixels (Backend
        convention: positive ``dy`` scrolls content up / view down).

        A transport MAY only support vertical scrolling: the real
        :class:`AardwolfTransport` drops a non-zero ``dx`` because aardwolf's
        wheel API has no horizontal event. A transport that dispatches the
        wheel at a cursor position SHOULD use the last pointer location (the
        remote OS routes the wheel to the window under the cursor), not a fixed
        origin.
        """
        ...


# -- key normalization (shared by the backend; understood by every transport) --

# Playwright/recorder-style modifier names -> normalized transport tokens.
_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "controlormeta": "ctrl",  # on a Windows RDP target ControlOrMeta = Ctrl
    "meta": "meta",
    "cmd": "meta",
    "command": "meta",
    "win": "meta",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}

# Playwright/recorder-style named keys -> normalized transport tokens.
_NAMED_KEYS = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "space": "space",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def normalize_key_part(part: str) -> str:
    """Normalize one key/modifier name to a transport token.

    Modifiers and named keys are canonicalized (``'Meta'`` -> ``'meta'``,
    ``'ArrowDown'`` -> ``'down'``); a single character passes through
    unchanged so its case (which encodes Shift for letters) is preserved.
    """
    lower = part.lower()
    if lower in _MODIFIER_ALIASES:
        return _MODIFIER_ALIASES[lower]
    if lower in _NAMED_KEYS:
        return _NAMED_KEYS[lower]
    return part


def normalize_chord(key: str) -> list[str]:
    """Split a key or ``+``-joined chord into normalized transport tokens.

    Raises:
        ValueError: If the chord is empty.
    """
    parts = [normalize_key_part(p) for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    return parts


class FreeRDPBackend:
    """`Backend` implementation over an :class:`RDPTransport`.

    Args:
        transport: The RDP transport to drive (real or fake).
        viewport: Optional ``(width, height)`` override; when omitted it is
            derived once from the first framebuffer and cached.
        connect: When True (default) connect the transport on construction.
            Pass False if the caller manages the transport lifecycle.
        max_frame_age_s: Maximum age of the screenshot that established an
            action's coordinate/session context. A stale frame is refused
            instead of sending input into a desktop that may have changed.
        readiness_probe: Optional deployment-specific pixel predicate. It is
            evaluated on the last screenshot before every input and should
            return False for lock/login/disconnect or unexpected-app screens.
            Generic RDP has no portable structured lock-state signal, so a
            consequential deployment should supply this fail-closed hook.
    """

    def __init__(
        self,
        transport: RDPTransport,
        *,
        viewport: Optional[tuple[int, int]] = None,
        connect: bool = True,
        max_frame_age_s: float = 10.0,
        readiness_probe: Optional[Callable[[bytes], bool]] = None,
    ) -> None:
        self._transport = transport
        self._viewport = viewport
        self._max_frame_age_s = float(max_frame_age_s)
        if self._max_frame_age_s <= 0:
            raise ValueError("max_frame_age_s must be positive")
        self._readiness_probe = readiness_probe
        self._last_frame_monotonic: Optional[float] = None
        # Keep capture/geometry validation and a complete input gesture in one
        # critical section. A concurrent screenshot may otherwise replace the
        # coordinate lease between pointer-down and pointer-up.
        self._input_lock = threading.RLock()
        if connect:
            self._transport.connect()

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the remote desktop, from the framebuffer."""
        if self._viewport is None:
            _, w, h = self._transport.framebuffer()
            self._viewport = (int(w), int(h))
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the current remote frame as PNG bytes.

        The frame comes straight from :meth:`RDPTransport.framebuffer`; a PIL
        image is PNG-encoded directly, raw bytes are wrapped with the reported
        ``(width, height)`` (RGB or RGBA inferred from the byte length) and
        then encoded. The PNG's pixels are the exact coordinate space
        :meth:`click` expects.

        Raises:
            RuntimeError: If the transport hands back nothing usable.
        """
        with self._input_lock:
            frame, w, h = self._transport.framebuffer()
            img = self._to_image(frame, int(w), int(h))
            # Cache the viewport from the frame we actually encoded, so viewport
            # and screenshot can never disagree.
            self._viewport = img.size
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            png = buf.getvalue()
            self._last_frame_monotonic = time.monotonic()
            return png

    def wait_first_frame(self, *, retries: int = 20, settle_s: float = 0.25) -> bytes:
        """Poll :meth:`screenshot` until a non-blank frame, returning its PNG.

        The first frame(s) an RDP session paints are often a single-colour
        blank canvas that arrives before the desktop actually renders — a real
        EMR grab right after connect hits this too, and asserting on that frame
        is racy. This helper grabs up to ``retries`` frames, sleeping
        ``settle_s`` between attempts, and returns the first frame with more
        than one distinct colour; if none appears within the budget it returns
        the last frame grabbed (the caller still gets a frame and decides).

        Opt-in by design: :meth:`screenshot` stays a single cheap grab with no
        hidden sleeping. Call this once, right after connect, when you need the
        desktop to have painted before you read pixels.
        """
        import time

        last = self.screenshot()
        for attempt in range(max(1, retries)):
            if attempt:
                last = self.screenshot()
            img = Image.open(io.BytesIO(last)).convert("RGB")
            colors = img.getcolors(maxcolors=1 << 24)
            if colors is None or len(colors) > 1:
                return last
            time.sleep(settle_s)
        return last

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at framebuffer pixel coordinates.

        A single click is a pointer down then up at (x, y); a double click is
        that press/release sequence sent twice.
        """
        with self._input_lock:
            self._ensure_input_ready(point=(int(x), int(y)))
            presses = 2 if double else 1
            for _ in range(presses):
                self._assert_frame_fresh()
                self._transport.pointer(int(x), int(y), "left", True)
                self._transport.pointer(int(x), int(y), "left", False)

    def type_text(self, text: str) -> None:
        """Type text into the focused control, one key down/up per character.

        Every character's key-up is sent in a ``finally`` so a transport
        failure between down and up can never leave a key latched down: a real
        RDP ``key()`` can time out mid-character, and a stuck key silently
        corrupts every subsequent input (auto-repeat, or the held key acting as
        a modifier). Releasing a key that never actually registered is a
        harmless no-op on the target, so the guarantee costs nothing.
        """
        if not text:
            return
        with self._input_lock:
            self._ensure_input_ready()
            for ch in text:
                try:
                    self._transport.key(ch, True)
                finally:
                    self._release_keys((ch,))

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``.

        A chord is pressed by sending every part down in order, then releasing
        every part in reverse order — the natural nesting a human produces
        (modifiers wrap the key). A single key is a plain down/up.

        Every key sent (or attempted) down is released in a ``finally``, so a
        transport exception mid-chord — a real RDP ``key()`` timeout — can never
        leave a modifier latched. That matters because a stuck ``Ctrl`` turns
        the next click into ``Ctrl+click`` and the next text into a shortcut
        (``Ctrl+a`` then a character wipes the field): a silent wrong-action.
        A part is queued for release *before* its down is sent, so even a key
        whose own down raised (it may already have registered on the wire) is
        released; a redundant release is a no-op on the target.
        """
        parts = normalize_chord(key)
        with self._input_lock:
            # Printable characters sent through Aardwolf's Unicode path cannot
            # participate in physical shortcuts (Win+R, Ctrl+A, and similar).
            # A transport may therefore expose a separate physical-key seam
            # for press/chord only. Plain transports retain the original key()
            # behavior, and type_text() always remains on key()/Unicode.
            physical_key = getattr(self._transport, "physical_key", None)
            supports_physical_key = getattr(
                self._transport, "supports_physical_key", None
            )
            if callable(physical_key) and callable(supports_physical_key):
                unsupported = [
                    part for part in parts if not supports_physical_key(part)
                ]
                if unsupported:
                    raise ValueError(
                        "RDP transport cannot safely emit physical chord keys: "
                        f"{unsupported!r}"
                    )
                sender = physical_key
            else:
                sender = self._transport.key
            self._ensure_input_ready()
            pressed: list[str] = []
            try:
                for part in parts:
                    pressed.append(part)
                    sender(part, True)
            finally:
                self._release_keys(reversed(pressed), sender=sender)

    def _release_keys(self, parts, *, sender=None) -> None:
        """Release each key token, best-effort: one failing release never
        blocks the others and never masks an in-flight exception."""
        if sender is None:
            sender = self._transport.key
        for part in parts:
            try:
                sender(part, False)
            except Exception:  # noqa: BLE001 - release is best-effort teardown
                pass

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture by ``(dx, dy)`` pixels.

        Limitation — horizontal scroll: the real :class:`AardwolfTransport`
        can only emit *vertical* wheel events (aardwolf's ``send_mouse``
        exposes ``WHEEL_UP``/``WHEEL_DOWN`` but no horizontal ``HWHEEL``), so a
        non-zero ``dx`` is silently dropped by that transport. This is a
        documented capability gap, not a bug in this method; the in-repo
        :class:`FakeRDPTransport` models the same drop so a test cannot pass on
        a capability the live transport lacks.
        """
        if dx == 0 and dy == 0:
            return
        with self._input_lock:
            self._ensure_input_ready()
            self._transport.wheel(int(dx), int(dy))

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Disconnect the underlying transport (idempotent)."""
        self._transport.disconnect()

    # -- internals -----------------------------------------------------------

    def _ensure_input_ready(self, *, point: Optional[tuple[int, int]] = None) -> None:
        """Validate the frame lease, dimensions, bounds and readiness hook.

        The resolver acts on screenshot pixels. Sending those coordinates after
        the frame ages out or the negotiated desktop size changes risks a
        silent wrong-target action, so both conditions fail closed and force a
        new screenshot/resolution cycle.
        """
        if self._last_frame_monotonic is None and point is not None:
            raise RuntimeError(
                "no captured RDP frame lease for coordinate input; capture and "
                "resolve the target before clicking"
            )
        if self._last_frame_monotonic is None:
            # Keyboard/wheel callers still need a current session-readiness
            # lease even though they do not carry screenshot coordinates.
            self.screenshot()
        self._assert_frame_fresh()

        frame, w, h = self._transport.framebuffer()
        current = (int(w), int(h))
        if current[0] <= 0 or current[1] <= 0:
            raise RuntimeError(f"RDP framebuffer has invalid dimensions {current!r}")
        if self._viewport != current:
            raise RuntimeError(
                f"RDP framebuffer changed from {self._viewport!r} to {current!r}; "
                "capture and re-resolve before sending input"
            )
        if point is not None:
            x, y = point
            if not (0 <= x < current[0] and 0 <= y < current[1]):
                raise RuntimeError(
                    f"RDP input point {(x, y)!r} is outside framebuffer {current!r}"
                )
        if self._readiness_probe is not None:
            # Evaluate readiness on the current framebuffer, not merely the
            # resolver's leased image: a lock/disconnect can appear while the
            # dimensions stay unchanged.
            current_img = self._to_image(frame, current[0], current[1])
            buf = io.BytesIO()
            current_img.convert("RGB").save(buf, format="PNG")
            if not self._readiness_probe(buf.getvalue()):
                raise RuntimeError(
                    "RDP readiness probe rejected the current frame "
                    "(locked, disconnected, or unexpected session); refusing input"
                )
        # framebuffer/readiness work can block on the network; recheck at the
        # last common point before an input edge.
        self._assert_frame_fresh()

    def _assert_frame_fresh(self) -> None:
        if self._last_frame_monotonic is None:
            raise RuntimeError("no captured RDP frame lease")
        age = time.monotonic() - self._last_frame_monotonic
        if age > self._max_frame_age_s:
            raise RuntimeError(
                f"RDP frame is stale ({age:.3f}s > {self._max_frame_age_s:.3f}s); "
                "halting intentionally so the runtime can capture, re-resolve, "
                "and re-check identity"
            )

    @staticmethod
    def _to_image(frame: Union["Image.Image", bytes], w: int, h: int) -> Image.Image:
        """Coerce a transport framebuffer to a PIL image of size (w, h)."""
        if isinstance(frame, Image.Image):
            return frame
        if isinstance(frame, (bytes, bytearray)):
            data = bytes(frame)
            if w <= 0 or h <= 0:
                raise RuntimeError("framebuffer reported non-positive size")
            pixels = w * h
            if pixels and len(data) % pixels == 0:
                channels = len(data) // pixels
                mode = {3: "RGB", 4: "RGBA"}.get(channels)
                if mode is not None:
                    return Image.frombytes(mode, (w, h), data)
            raise RuntimeError(
                f"cannot interpret {len(data)} framebuffer bytes as {w}x{h} RGB/RGBA"
            )
        raise RuntimeError(f"unsupported framebuffer type: {type(frame)!r}")


# =============================================================================
# Real transport over aardwolf (lazy; behind the optional `rdp` extra).
# =============================================================================

# aardwolf virtual-key names for our normalized key tokens. Anything not here
# (a single printable character) is typed via aardwolf's send_key_char. The
# bool is is_extended (the "grey" keys — arrows, nav cluster, right-hand
# modifiers — that carry the extended-scancode flag over the RDP wire).
_AARDWOLF_VK = {
    "enter": ("VK_RETURN", False),
    "tab": ("VK_TAB", False),
    "escape": ("VK_ESCAPE", False),
    "backspace": ("VK_BACK", False),
    "delete": ("VK_DELETE", True),
    "space": ("VK_SPACE", False),
    "home": ("VK_HOME", True),
    "end": ("VK_END", True),
    "pageup": ("VK_PRIOR", True),
    "pagedown": ("VK_NEXT", True),
    "up": ("VK_UP", True),
    "down": ("VK_DOWN", True),
    "left": ("VK_LEFT", True),
    "right": ("VK_RIGHT", True),
    "ctrl": ("VK_LCONTROL", False),
    "shift": ("VK_LSHIFT", False),
    "alt": ("VK_LMENU", False),
    "meta": ("VK_LWIN", True),
}


class AardwolfTransport:
    """`RDPTransport` over the async ``aardwolf`` pure-Python RDP client.

    aardwolf is fully asynchronous; this class owns a private asyncio event
    loop on a daemon thread and marshals every operation onto it, presenting
    the synchronous :class:`RDPTransport` API the backend expects. The video
    out format is PIL at 1:1 (no downsampling), so framebuffer pixels equal
    remote-desktop pixels equal click pixels.

    Construct with :meth:`from_credentials` (host/user/pass) or pass a full
    aardwolf connection URL
    (``rdp+ntlm-password://DOMAIN\\user:password@host:port``).

    Args:
        url: aardwolf RDP connection URL.
        width: Requested remote desktop width (framebuffer width).
        height: Requested remote desktop height (framebuffer height).
        connect_timeout_s: Seconds to wait for the session + first frame.
        op_timeout_s: Per-operation timeout for input/framebuffer calls.
        keyboard_layout: Aardwolf keyboard-layout short name used to resolve
            physical shortcut scancodes (default ``enus``).
        keyboard_layout_id: RDP handshake layout id (default 1033 / en-US).
    """

    def __init__(
        self,
        url: str,
        *,
        width: int = 1280,
        height: int = 800,
        connect_timeout_s: float = 30.0,
        op_timeout_s: float = 10.0,
        keyboard_layout: str = "enus",
        keyboard_layout_id: int = 1033,
    ) -> None:
        self._url = url
        self._width = int(width)
        self._height = int(height)
        self._connect_timeout_s = connect_timeout_s
        self._op_timeout_s = op_timeout_s
        self._keyboard_layout = keyboard_layout
        self._keyboard_layout_id = int(keyboard_layout_id)
        self._physical_keyboard_layout = None
        self._loop = None
        self._thread = None
        self._conn = None
        # Last pointer position, so a wheel gesture is dispatched under the
        # cursor (where the remote OS routes it) rather than at the origin.
        self._last_pointer: Optional[tuple[int, int]] = None

    @classmethod
    def from_credentials(
        cls,
        host: str,
        username: str,
        password: str,
        *,
        domain: Optional[str] = None,
        port: int = 3389,
        auth: str = "ntlm-password",
        **kwargs: object,
    ) -> "AardwolfTransport":
        """Build a transport from plain host/user/password (the common case).

        Args:
            host: RDP host/IP.
            username: Account name.
            password: Account password.
            domain: Optional Windows domain (omit for local accounts).
            port: RDP port (default 3389).
            auth: aardwolf auth scheme (default ``ntlm-password``).
            **kwargs: Forwarded to :meth:`__init__` (width/height/timeouts).
        """
        from urllib.parse import quote

        user = f"{domain}\\{username}" if domain else username
        userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
        url = f"rdp+{auth}://{userinfo}@{host}:{port}"
        return cls(url, **kwargs)  # type: ignore[arg-type]

    # -- event-loop bridge ---------------------------------------------------

    def _ensure_loop(self) -> None:
        import asyncio
        import threading

        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="aardwolf-rdp"
        )
        self._thread.start()

    def _stop_loop(self) -> None:
        """Stop the event loop and join its thread (idempotent).

        Shared by :meth:`disconnect` and the :meth:`connect` failure path so a
        teardown never leaks the daemon thread; without the join a per-retry
        connect-then-fail loop would accumulate one live ``aardwolf-rdp`` thread
        (and its event loop) per attempt.
        """
        import threading

        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self, coro, timeout: float):
        import asyncio

        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    @staticmethod
    def _raise_embedded_error(result: object, operation: str) -> object:
        """Raise errors Aardwolf returns as ``(value, error)`` receipts.

        Several Aardwolf input methods catch their own exceptions and return
        ``(None, exc)``. Treating that tuple as success would let the backend
        report an input gesture as delivered when the wire write failed.
        Successful methods are inconsistent (``None``, ``(True, None)``, or a
        value), so only a non-null second tuple member is an error.
        """
        if isinstance(result, tuple) and len(result) == 2 and result[1] is not None:
            error = result[1]
            if isinstance(error, BaseException):
                raise error
            raise RuntimeError(f"Aardwolf {operation} failed: {error!r}")
        return result

    # -- RDPTransport --------------------------------------------------------

    def connect(self) -> None:
        """Open the RDP session and block until the first frame arrives."""
        import asyncio

        from aardwolf.commons.factory import RDPConnectionFactory
        from aardwolf.commons.iosettings import RDPIOSettings
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        self._ensure_loop()

        iosettings = RDPIOSettings()
        iosettings.video_width = self._width
        iosettings.video_height = self._height
        iosettings.video_out_format = VIDEO_FORMAT.PIL
        iosettings.video_bpp_min = 15
        iosettings.video_bpp_max = 32
        # Bind physical shortcut resolution and the RDP handshake to an
        # explicit, configurable keyboard layout. The qualification VM uses
        # the default en-US handshake id (1033).
        iosettings.client_keyboard = self._keyboard_layout
        iosettings.keyboard_layout = self._keyboard_layout_id

        async def _connect():
            factory = RDPConnectionFactory.from_url(self._url, iosettings)
            conn = factory.get_connection(iosettings)
            try:
                _, err = await conn.connect()
                if err is not None:
                    raise err
                # Drain the out-queue until the desktop buffer has real pixels,
                # so the first framebuffer() is not a blank canvas.
                deadline = asyncio.get_event_loop().time() + self._connect_timeout_s
                while not getattr(conn, "desktop_buffer_has_data", False):
                    if asyncio.get_event_loop().time() > deadline:
                        raise TimeoutError("no RDP frame within connect timeout")
                    try:
                        await asyncio.wait_for(conn.ext_out_queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                return conn
            except BaseException:
                # The session opened but never delivered a usable frame (or
                # connect() itself errored): terminate it here so a half-open
                # connection is not leaked. self._conn was never assigned, so
                # disconnect() alone would skip this teardown.
                try:
                    await conn.terminate()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
                raise

        try:
            self._conn = self._run(_connect(), self._connect_timeout_s + 5.0)
        except BaseException:
            # Connect failed: stop and join the event-loop thread so repeated
            # failing connects cannot pile up daemon threads + event loops.
            self._stop_loop()
            raise

    def disconnect(self) -> None:
        """Terminate the session and stop the event loop (idempotent)."""
        conn, loop = self._conn, self._loop
        self._conn = None
        if conn is not None and loop is not None:
            try:
                self._run(conn.terminate(), self._op_timeout_s)
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        self._stop_loop()

    def framebuffer(self) -> Framebuffer:
        """Return ``(PIL image, width, height)`` for the current desktop."""
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        if self._conn is None:
            raise RuntimeError("transport not connected")
        conn = self._conn

        async def _snapshot():
            # Aardwolf's decoder mutates its PIL desktop buffer on this same
            # event-loop thread. Deep-copying it from the caller thread can
            # tear a frame while a bitmap update is being pasted.
            snapshot = conn.get_desktop_buffer(VIDEO_FORMAT.PIL)
            # Current Aardwolf returns a deep copy, but detach explicitly on
            # the decoder thread so a future implementation cannot hand the
            # caller its still-mutable internal PIL image.
            return snapshot.copy() if isinstance(snapshot, Image.Image) else snapshot

        result = self._run(_snapshot(), self._op_timeout_s)
        img = self._raise_embedded_error(result, "framebuffer snapshot")
        if not isinstance(img, Image.Image):
            raise RuntimeError("aardwolf returned no desktop buffer")
        return img, img.width, img.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON

        btn = {
            "left": MOUSEBUTTON.MOUSEBUTTON_LEFT,
            "right": MOUSEBUTTON.MOUSEBUTTON_RIGHT,
            "middle": MOUSEBUTTON.MOUSEBUTTON_MIDDLE,
        }.get(button, MOUSEBUTTON.MOUSEBUTTON_LEFT)
        if self._conn is None:
            raise RuntimeError("transport not connected")
        # Remember where the pointer is so wheel() can dispatch under it.
        self._last_pointer = (int(x), int(y))
        result = self._run(
            self._conn.send_mouse(btn, int(x), int(y), bool(down)),
            self._op_timeout_s,
        )
        self._raise_embedded_error(result, "pointer input")

    def key(self, keysym_or_char: str, down: bool) -> None:
        if self._conn is None:
            raise RuntimeError("transport not connected")
        vk = _AARDWOLF_VK.get(keysym_or_char)
        if vk is not None:
            vk_name, is_extended = vk
            result = self._run(
                self._conn.send_key_virtualkey(vk_name, bool(down), is_extended),
                self._op_timeout_s,
            )
            self._raise_embedded_error(result, "virtual-key input")
            return
        # A single printable character: let aardwolf resolve scancode+shift.
        for ch in keysym_or_char:
            result = self._run(
                self._conn.send_key_char(ch, bool(down)),
                self._op_timeout_s,
            )
            self._raise_embedded_error(result, "character input")

    def supports_physical_key(self, keysym_or_char: str) -> bool:
        """Return whether ``physical_key`` can emit this normalized token."""
        try:
            self._physical_scancode(keysym_or_char)
            return True
        except ValueError:
            return False

    def _physical_scancode(self, keysym_or_char: str) -> int:
        """Resolve one normalized chord token without sending any input."""
        if keysym_or_char not in _AARDWOLF_VK and not (
            len(keysym_or_char) == 1
            and keysym_or_char.isascii()
            and keysym_or_char.isalnum()
        ):
            raise ValueError(f"unsupported physical RDP chord key: {keysym_or_char!r}")

        from aardwolf.keyboard import VK_MODIFIERS
        from aardwolf.keyboard.layoutmanager import KeyboardLayoutManager

        layout = self._physical_keyboard_layout
        if layout is None:
            layout = KeyboardLayoutManager().get_layout_by_shortname(
                self._keyboard_layout
            )
            self._physical_keyboard_layout = layout
        if layout is None:
            raise ValueError(
                f"unknown Aardwolf keyboard layout: {self._keyboard_layout!r}"
            )
        if keysym_or_char in _AARDWOLF_VK:
            vk_name, _legacy_extended = _AARDWOLF_VK[keysym_or_char]
            try:
                scancode = layout.vk_to_scancode(vk_name)
            except KeyError as exc:
                raise ValueError(
                    f"keyboard layout {self._keyboard_layout!r} has no {vk_name}"
                ) from exc
        else:
            try:
                scancode, modifiers = layout.char_to_scancode(keysym_or_char)
            except KeyError as exc:
                raise ValueError(
                    "keyboard layout cannot resolve physical chord key "
                    f"{keysym_or_char!r}"
                ) from exc
            if modifiers != VK_MODIFIERS(0):
                raise ValueError(
                    "physical chord key requires implicit modifiers: "
                    f"{keysym_or_char!r} -> {modifiers!r}"
                )
        return int(scancode)

    def physical_key(self, keysym_or_char: str, down: bool) -> None:
        """Send a physical scancode for a key used by ``Backend.press``.

        This path is deliberately separate from :meth:`key`: text entry keeps
        Aardwolf's Unicode events, while shortcuts require every chord member
        to be a physical key. Printable chord members are limited to ASCII
        alphanumerics with no layout modifier requirement; ambiguous symbols
        fail before any input is sent.
        """
        if self._conn is None:
            raise RuntimeError("transport not connected")
        scancode = self._physical_scancode(keysym_or_char)

        is_extended = scancode > 57000
        result = self._run(
            self._conn.send_key_scancode(scancode, bool(down), is_extended),
            self._op_timeout_s,
        )
        self._raise_embedded_error(result, "physical scancode input")

    def wheel(self, dx: int, dy: int) -> None:
        """Send a wheel gesture, dispatched under the last pointer position.

        Limitation: aardwolf's ``send_mouse`` only exposes vertical
        ``WHEEL_UP``/``WHEEL_DOWN`` (there is no horizontal ``HWHEEL`` in its
        public API), so a non-zero ``dx`` is dropped — a documented capability
        gap. A non-zero ``dx`` with ``dy == 0`` therefore does nothing; a warning
        is emitted so the drop is not silent.

        The wheel is dispatched at :attr:`_last_pointer` (the last place a
        pointer event was sent), not at the origin: Windows routes a wheel event
        to the window under the cursor, so sending it at ``(0, 0)`` would scroll
        the top-left pane instead of the content the caller just clicked into.
        """
        import warnings

        from aardwolf.commons.queuedata.constants import MOUSEBUTTON

        if self._conn is None:
            raise RuntimeError("transport not connected")
        if dx and not dy:
            warnings.warn(
                "AardwolfTransport cannot emit horizontal wheel events "
                "(aardwolf send_mouse has no HWHEEL); dropping dx="
                f"{dx}. See FreeRDPBackend.scroll docstring.",
                stacklevel=2,
            )
        if not dy:
            return
        # aardwolf wheel "steps" are wheel-delta units; approximate one notch
        # (~120 units) per ~100 px, matching WindowsBackend's notch ratio. The
        # replayer re-resolves after each scroll, so the exact ratio is not
        # load-bearing.
        btn = (
            MOUSEBUTTON.MOUSEBUTTON_WHEEL_DOWN
            if dy > 0
            else MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP
        )
        steps = max(1, round(abs(dy) / 100)) * 120
        # Dispatch under the cursor; fall back to the frame centre if no pointer
        # event has been sent yet (never the origin, which routes to top-left).
        if self._last_pointer is not None:
            x, y = self._last_pointer
        else:
            x, y = self._width // 2, self._height // 2
        result = self._run(
            self._conn.send_mouse(btn, x, y, False, steps),
            self._op_timeout_s,
        )
        self._raise_embedded_error(result, "wheel input")
