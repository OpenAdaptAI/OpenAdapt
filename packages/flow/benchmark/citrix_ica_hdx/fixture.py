#!/usr/bin/env python3
"""Deterministic synthetic ICA/HDX stand-in fixture (NOT real Citrix ICA/HDX).

This module renders a *synthetic* clinical "MockMed" application frame and
presents it to the UNMODIFIED
:class:`~openadapt_flow.backends.citrix_workspace.CitrixWorkspaceBackend`
through its real :class:`~openadapt_flow.backends.remote_display.WindowClient`
seam. Every ICA/HDX degradation in the Section 10 condition matrix is reproduced
here *deterministically and in-process* -- no Docker, no network, no Playwright,
no live protocol -- so the backend's fail-loud safety gates (frame-freshness
lease, focus/occlusion binding, DPI/scale consistency, input-trust, one-shot
actuation lease, readiness/identity re-resolution) can be qualified against the
whole matrix while CI stays green.

HONEST LABEL (non-negotiable): this is a **DETERMINISTIC STAND-IN** -- a
synthetic fixture that *reproduces* ICA/HDX conditions. It is **NOT** real Citrix
ICA/HDX. It does not exercise HDX codecs, ICA compression, or the real
Workspace-client transport. No result here is real-protocol acceptance; the
real-ICA lane remains the customer-environment (Accuro) release gate documented
in ``benchmark/citrix_workspace/README.md``.

The frame the ``SyntheticIcaWindowClient`` returns is a real PNG that the real
backend decodes, hashes, DPI-maps, occlusion-checks, and readiness/identity
probes; the synthetic app also *interprets delivered input* (roster select, note
focus, typing, Save) and commits the write to an out-of-band :class:`FaultDB`
oracle. Effect verification reads that oracle, never the on-screen banner.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from openadapt_flow.backends.remote_display import RemoteDisplayError, WindowInfo

# -- fixed synthetic geometry (pixel space of the captured frame) --------------
# Window bounds are screen POINTS; the captured frame is pixels. scale 2.0 (a
# common Citrix/Retina HiDPI factor) exercises the backend's pixel<->point map.
FRAME_PX: tuple[int, int] = (600, 400)
WINDOW_BOUNDS: tuple[float, float, float, float] = (40.0, 24.0, 300.0, 200.0)
SCALE: float = 2.0
WINDOW_ID: int = 4101
WINDOW_PID: int = 9200

# Target rungs (pixel coordinates on the captured frame).
ROSTER_ROW: tuple[int, int] = (150, 96)  # "Ada Lovelace  MRN A1001"
NOTE_FIELD: tuple[int, int] = (170, 300)  # clinical-note entry box
SAVE_BUTTON: tuple[int, int] = (470, 300)  # "Save Note" (irreversible write)

EXPECTED_MRN: str = "A1001"
NOTE_VALUE: str = "followup in two weeks"

# On-frame marker swatches (deterministic pixel probes stand in for OCR markers;
# the real backend accepts an injected pixel predicate exactly this way).
_READY_SWATCH = (8, 8, 40, 24)  # green when in-app, red when locked
_APP_SWATCH = (48, 8, 80, 24)  # application-identity swatch
_STATE_SWATCH = (88, 8, 120, 24)  # workflow-state swatch
_CLOCK_REGION = (520, 4, 596, 26)  # VOLATILE remote chrome (a clock)

_READY_GREEN = (0, 170, 0)
_LOCK_RED = (185, 0, 0)
_APP_BLUE = (0, 70, 190)
_STATE_TEAL = (0, 150, 150)
_TOL = 42  # per-channel mean tolerance for probes


# -- out-of-band effect oracle -------------------------------------------------
@dataclass
class FaultDB:
    """Synthetic system-of-record. The app commits here on Save; the harness
    reads it OUT OF BAND (never from the screen) to verify the real effect."""

    records: list[tuple[str, str]] = field(default_factory=list)
    rejected_writes: int = 0

    def commit(self, mrn: str, note: str) -> None:
        self.records.append((mrn, note))

    def last(self) -> Optional[tuple[str, str]]:
        return self.records[-1] if self.records else None

    def write_count(self) -> int:
        return len(self.records)


def _mean(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, ...]:
    region = img.crop(box)
    hist = region.resize((1, 1), Image.BILINEAR)
    return hist.getpixel((0, 0))


def _near(observed: tuple[float, ...], target: tuple[int, int, int]) -> bool:
    return all(abs(float(o) - t) <= _TOL for o, t in zip(observed, target))


def readiness_probe(png: bytes) -> bool:
    """True iff the fresh frame is the in-app (unlocked, launched) surface."""
    with Image.open(io.BytesIO(png)) as im:
        return _near(_mean(im.convert("RGB"), _READY_SWATCH), _READY_GREEN)


def application_marker_probe(png: bytes) -> bool:
    with Image.open(io.BytesIO(png)) as im:
        return _near(_mean(im.convert("RGB"), _APP_SWATCH), _APP_BLUE)


def workflow_state_marker_probe(png: bytes) -> bool:
    with Image.open(io.BytesIO(png)) as im:
        return _near(_mean(im.convert("RGB"), _STATE_SWATCH), _STATE_TEAL)


@dataclass
class AppState:
    """Mutable synthetic-app + session state the renderer and client read."""

    ready: bool = True  # in-app (True) vs lock/login/spinner (False)
    selected_mrn: Optional[str] = None
    note_buffer: str = ""
    note_focused: bool = False
    committed: bool = False
    overlay: bool = False  # an unexpected dialog/overlay is painted
    clock: int = 0  # volatile chrome value (only ticks on demand)
    degrade: float = 0.0  # 0.0 none .. 1.0 severe codec/compression
    theme_invert: bool = False
    identity_broken: bool = False  # application-identity marker unverifiable
    write_rejected: bool = False  # system of record rejects the Save (banner lies)


def render_frame(state: AppState) -> bytes:
    """Render the synthetic MockMed frame deterministically to PNG bytes."""
    w, h = FRAME_PX
    img = Image.new("RGB", (w, h), (245, 245, 248))
    d = ImageDraw.Draw(img)

    # top status bar + marker swatches
    d.rectangle((0, 0, w, 30), fill=(225, 228, 235))
    d.rectangle(_READY_SWATCH, fill=_READY_GREEN if state.ready else _LOCK_RED)
    d.rectangle(
        _APP_SWATCH,
        fill=_APP_BLUE
        if (state.ready and not state.identity_broken)
        else (120, 120, 120),
    )
    d.rectangle(
        _STATE_SWATCH,
        fill=_STATE_TEAL if (state.ready and state.note_focused) else (120, 120, 120),
    )
    # volatile clock (remote chrome) -- legitimately masked, never a target
    d.rectangle(_CLOCK_REGION, fill=(235, 235, 235))
    d.text((524, 8), f"{state.clock:04d}", fill=(90, 90, 90))

    if state.ready:
        # roster row
        row_fill = (
            (210, 232, 255) if state.selected_mrn == EXPECTED_MRN else (255, 255, 255)
        )
        d.rectangle((40, 78, 560, 116), fill=row_fill, outline=(80, 80, 80))
        d.text((52, 90), f"Ada Lovelace   MRN {EXPECTED_MRN}", fill=(0, 0, 0))
        # note field
        d.rectangle((40, 280, 300, 320), fill=(255, 255, 255), outline=(80, 80, 80))
        d.text((48, 294), state.note_buffer[:34], fill=(0, 0, 0))
        # save button
        d.rectangle((420, 280, 520, 320), fill=(60, 130, 60), outline=(20, 60, 20))
        d.text((432, 294), "Save Note", fill=(255, 255, 255))
        if state.committed:
            # an optimistic on-screen banner -- deliberately NOT the oracle
            d.text((330, 340), "Saved", fill=(0, 120, 0))
    else:
        d.rectangle((150, 150, 450, 250), fill=(60, 60, 60))
        d.text((190, 195), "Session Locked", fill=(240, 240, 240))

    if state.overlay:
        d.rectangle((120, 120, 480, 260), fill=(250, 244, 190), outline=(120, 90, 0))
        d.text((150, 180), "Unexpected dialog", fill=(60, 40, 0))

    if state.theme_invert:
        img = ImageOps.invert(img)

    if state.degrade > 0.0:
        # reproduce compression/codec artifacts: downscale round-trip + blur +
        # JPEG. Severity scales the illegibility. This is a *synthetic* artifact
        # generator, not a real HDX/Thinwire bitstream.
        factor = max(0.06, 1.0 - state.degrade)
        small = img.resize(
            (max(1, int(w * factor)), max(1, int(h * factor))), Image.BILINEAR
        ).resize((w, h), Image.BILINEAR)
        if state.degrade >= 0.6:
            small = small.filter(ImageFilter.GaussianBlur(2.0 * state.degrade))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=max(4, int(90 * factor)))
        img = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _session_digest(token: str) -> str:
    return hashlib.sha256(("oa-ica-standin\x00" + token).encode()).hexdigest()


class SyntheticIcaWindowClient:
    """A real :class:`WindowClient` backed by the deterministic synthetic frame.

    It is NOT a mock of the backend: the backend under test is unmodified. Only
    this host-window seam is synthetic. It renders a real PNG the backend
    decodes/hashes/DPI-maps, reports window geometry/focus/occlusion the backend
    binds against, and -- crucially -- *interprets delivered OS input* the way
    the remote app would (roster select, note focus, typing, Save->oracle
    commit), so a delivered click has a real, independently observable effect.
    """

    def __init__(self, oracle: FaultDB, *, state: Optional[AppState] = None) -> None:
        self.oracle = oracle
        self.state = state or AppState()
        self.trusted = True
        self.frontmost = True
        self.session_token = "sess-A"
        self.window = WindowInfo(
            window_id=WINDOW_ID,
            owner="Citrix Viewer",
            title="MockMed",
            pid=WINDOW_PID,
            bounds=WINDOW_BOUNDS,
            on_screen=True,
        )
        self.windows = [self.window]
        self.key_window_override: Optional[int] = None
        self.hit_window_override: Optional[int] = None
        self.paste_blocked = False  # Citrix clipboard restriction
        self.unmapped_keys: set[str] = set()
        self.aniso = False  # uncalibrated anisotropic DPI
        self.hover_unsettle_frames = 0  # delayed remote hover-paint frames
        self._settle_countdown = 0
        self.calls: list[tuple] = []

    # -- session identity (reconnect/roaming) --------------------------------
    def session_observer(self) -> Optional[str]:
        return _session_digest(self.session_token)

    # -- WindowClient protocol ----------------------------------------------
    def input_trusted(self) -> bool:
        return self.trusted

    def frontmost_pid(self) -> Optional[int]:
        return self.window.pid if self.frontmost else 7

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        return [
            w
            for w in self.windows
            if w.owner.casefold() == owner.casefold()
            and (title is None or w.title.casefold() == title.casefold())
        ]

    def key_window_id(self, pid: int) -> Optional[int]:
        if not self.frontmost:
            return None
        if self.key_window_override is not None:
            return self.key_window_override
        return self.window.window_id if pid == self.window.pid else None

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        if self.hit_window_override is not None:
            return self.hit_window_override
        return self.window.window_id

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        # Delayed remote hover paint: while unsettled, advance the volatile
        # chrome so consecutive frames differ (the backend's settle gate must
        # wait for byte-stable frames before arming a lease).
        if self._settle_countdown > 0:
            self.state.clock += 1
            self._settle_countdown -= 1
        png = render_frame(self.state)
        if self.aniso:
            # Report captured pixels whose x/y scale disagrees with the window
            # bounds -> the backend must refuse uncalibrated input.
            with Image.open(io.BytesIO(png)) as im:
                skewed = im.convert("RGB").resize((560, 400), Image.BILINEAR)
            buf = io.BytesIO()
            skewed.save(buf, format="PNG")
            return buf.getvalue(), 560, 400
        return png, FRAME_PX[0], FRAME_PX[1]

    def activate(self, pid: int) -> None:
        self.calls.append(("activate", pid))

    def _pixel_from_screen(self, sx: float, sy: float) -> tuple[float, float]:
        ox, oy = self.window.bounds[0], self.window.bounds[1]
        return (sx - ox) * SCALE, (sy - oy) * SCALE

    def _in(self, px: float, py: float, box: tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = box
        return x0 <= px <= x1 and y0 <= py <= y1

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int = 1
    ) -> None:
        self.calls.append(("mouse", button, down))
        if down or not self.state.ready:
            return
        px, py = self._pixel_from_screen(x, y)
        if self._in(px, py, (40, 78, 560, 116)):
            self.state.selected_mrn = EXPECTED_MRN
        elif self._in(px, py, (40, 280, 300, 320)):
            self.state.note_focused = True
        elif self._in(px, py, (420, 280, 520, 320)):
            # the irreversible write. The on-screen banner is optimistic; only a
            # non-rejected write reaches the out-of-band oracle.
            if self.state.selected_mrn is not None:
                self.state.committed = True
                if self.state.write_rejected:
                    self.oracle.rejected_writes += 1
                else:
                    self.oracle.commit(self.state.selected_mrn, self.state.note_buffer)

    def mouse_move(self, x: float, y: float) -> None:
        self.calls.append(("move",))
        # Each new pointer positioning restarts the settle countdown, so the
        # backend must re-observe stable frames after the move it just made.
        self._settle_countdown = self.hover_unsettle_frames

    def type_chars(self, text: str) -> None:
        self.calls.append(("type", text))
        if self.state.ready and self.state.note_focused:
            self.state.note_buffer += text

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        self.calls.append(("key", keycode, down, tuple(flags)))
        if self.paste_blocked and "control" in flags:
            # Citrix policy disables the clipboard channel: fail LOUD rather
            # than let a no-op paste look like a completed action.
            raise RemoteDisplayError(
                "clipboard redirection is disabled by session policy; paste "
                "was not delivered (refusing to let a no-op look like success)"
            )

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        if token in self.unmapped_keys:
            return None
        table = {"enter": (0x24, False), "tab": (0x30, False), "v": (0x09, False)}
        return table.get(token.lower())


# -- reviewable volatile-mask contract ----------------------------------------
Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class VolatileMaskSpec:
    """A reviewable declaration of which frame regions are volatile (may be
    masked from continuity comparison) and which are PROTECTED and must never be
    masked: target, actionability, identity, workflow-state, effect-relevant.

    The backend's default is deliberately conservative (full-frame decoded-RGB
    continuity), so any real relaxation must be reviewed. This spec makes the
    relaxation auditable and lets a check prove no protected region is covered.
    """

    volatile: dict[str, Rect]
    protected: dict[str, Rect]


def default_mask_spec() -> VolatileMaskSpec:
    return VolatileMaskSpec(
        volatile={"remote_clock_chrome": _CLOCK_REGION},
        protected={
            "target_roster_row": (40, 78, 560, 116),
            "target_note_field": (40, 280, 300, 320),
            "target_save_button": (420, 280, 520, 320),
            "actionability_save_label": (432, 294, 500, 306),
            "identity_readiness": _READY_SWATCH,
            "identity_application": _APP_SWATCH,
            "workflow_state": _STATE_SWATCH,
            "effect_saved_banner": (330, 336, 400, 352),
        },
    )


def _overlaps(a: Rect, b: Rect) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def check_masks_reviewable(spec: VolatileMaskSpec) -> list[str]:
    """Return a list of violations: any volatile mask overlapping a protected
    region. Empty list == the mask spec is reviewable and safe. Volatile masks
    must NEVER cover a target, actionability, identity, workflow-state, or
    effect-relevant region."""
    violations: list[str] = []
    for vname, vrect in spec.volatile.items():
        for pname, prect in spec.protected.items():
            if _overlaps(vrect, prect):
                violations.append(f"volatile mask {vname!r} covers protected {pname!r}")
    return violations
