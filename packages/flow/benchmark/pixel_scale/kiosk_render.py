"""Render the two pixel-only kiosk fixtures at any display scale.

The real-RDP and canvas qualifications (``benchmark/rdp_ladder`` and
``benchmark/canvas_ladder``) run a Tk kiosk inside a container and read it back
through FreeRDP or noVNC. This module reproduces those two kiosk layouts,
their state machines, and their system-of-record writes in process, so the
production Recorder, compiler and Replayer can be run against them at
several display scales and text-rendering modes without a container.

A render profile models a DPI-aware remote application on a higher-density
session:

- ``scale`` multiplies every layout coordinate, widget size, border and font
  pixel size, and the frame size, the way Windows display scaling (125%,
  150%, 175%) or a HiDPI Citrix session does. Text is rasterized natively at
  the new pixel size, not resampled, so glyph shapes and hinting change the
  way they do on a real session.
- ``cleartype`` selects the text rasterizer. On: horizontal 3x oversampling
  with a five-tap filter mapped onto the RGB subpixels (the colour-fringed
  ClearType look). Off: aliased, one-bit glyphs (font smoothing off).
- ``font_scale`` enlarges the fonts alone, the negative control: the text
  grows but the layout does not move.

The surface has no DOM and no accessibility tree. The backend implements only
the base ``Backend`` protocol, so resolution runs on the pixel rungs.
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

LOGICAL_VIEWPORT = (1280, 800)
# Tk font sizes are points; the fixture display is 96 dpi at 100% scaling.
PX_PER_PT = 96 / 72

_FONT_CANDIDATES = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/DejaVuSans.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/DejaVuSans-Bold.ttf",
    ),
}


def _matplotlib_font(bold: bool) -> Optional[str]:
    try:
        import matplotlib
    except ImportError:
        return None
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf" / name
    return str(path) if path.is_file() else None


def font_path(bold: bool) -> Optional[str]:
    """DejaVu Sans (the fixture's font), or None when it is not installed."""
    for candidate in _FONT_CANDIDATES[bold]:
        if os.path.isfile(candidate):
            return candidate
    return _matplotlib_font(bold)


_FONT_CACHE: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(bold: bool, px: int) -> ImageFont.FreeTypeFont:
    key = (bold, px)
    if key not in _FONT_CACHE:
        path = font_path(bold)
        if path is None:
            _FONT_CACHE[key] = ImageFont.load_default(size=px)  # type: ignore[assignment]
        else:
            _FONT_CACHE[key] = ImageFont.truetype(path, px)
    return _FONT_CACHE[key]


def _rgb(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return (int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16))


def _shade(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    if factor >= 1:
        return tuple(int(c + (255 - c) * (factor - 1)) for c in colour)  # type: ignore[return-value]
    return tuple(int(c * factor) for c in colour)  # type: ignore[return-value]


@dataclass(frozen=True)
class RenderProfile:
    """How the remote session renders the application."""

    scale: float = 1.0
    cleartype: bool = True
    #: Multiplies font sizes only, leaving the layout where it was: a font
    #: change, not a display-scale change.
    font_scale: float = 1.0

    @property
    def label(self) -> str:
        label = (
            f"{round(self.scale * 100)}% ClearType {'on' if self.cleartype else 'off'}"
        )
        if self.font_scale != 1.0:
            label += f", fonts {round(self.font_scale * 100)}%"
        return label

    @property
    def viewport(self) -> tuple[int, int]:
        return (
            int(round(LOGICAL_VIEWPORT[0] * self.scale)),
            int(round(LOGICAL_VIEWPORT[1] * self.scale)),
        )


# ---------------------------------------------------------------- text raster


def _coverage(text: str, font_px: int, bold: bool, cleartype: bool) -> np.ndarray:
    """Per-subpixel glyph coverage ``(h, w, 3)`` in ``[0, 1]``."""
    font = _font(bold, font_px)
    ascent, descent = font.getmetrics()
    height = ascent + descent
    width = int(np.ceil(font.getlength(text)))
    if cleartype:
        width = max(width, int(np.ceil(_font(bold, font_px * 3).getlength(text) / 3)))
    width = max(1, width + 2)
    if not cleartype:
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.fontmode = "1"
        draw.text((0, 0), text, font=font, fill=255)
        cov = np.asarray(mask, dtype=np.float32) / 255.0
        return np.repeat(cov[:, :, None], 3, axis=2)
    # Horizontal 3x oversampling: rasterize at 3x, then average rows back to
    # the target height so only the horizontal axis keeps subpixel detail.
    big_font = _font(bold, font_px * 3)
    big = Image.new("L", (width * 3, height * 3), 0)
    ImageDraw.Draw(big).text((0, 0), text, font=big_font, fill=255)
    over = big.resize((width * 3, height), Image.BOX)
    cov = np.asarray(over, dtype=np.float32) / 255.0
    # ClearType-style five-tap low-pass across neighbouring subpixels.
    kernel = np.array([1, 2, 3, 2, 1], dtype=np.float32) / 9.0
    padded = np.pad(cov, ((0, 0), (2, 2)))
    filtered = sum(kernel[k] * padded[:, k : k + cov.shape[1]] for k in range(5))
    return np.clip(filtered.reshape(height, width, 3), 0.0, 1.0)


def _draw_text(
    canvas: np.ndarray,
    origin: tuple[int, int],
    text: str,
    *,
    font_px: int,
    bold: bool,
    fg: tuple[int, int, int],
    cleartype: bool,
    clip: Optional[tuple[int, int, int, int]] = None,
) -> None:
    if not text:
        return
    cov = _coverage(text, font_px, bold, cleartype)
    x, y = origin
    h, w = cov.shape[:2]
    x0, y0, x1, y1 = 0, 0, canvas.shape[1], canvas.shape[0]
    if clip is not None:
        cx, cy, cw, ch = clip
        x0, y0, x1, y1 = max(x0, cx), max(y0, cy), min(x1, cx + cw), min(y1, cy + ch)
    sx0, sy0 = max(x, x0), max(y, y0)
    sx1, sy1 = min(x + w, x1), min(y + h, y1)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    region = canvas[sy0:sy1, sx0:sx1].astype(np.float32)
    c = cov[sy0 - y : sy1 - y, sx0 - x : sx1 - x]
    fg_arr = np.array(fg, dtype=np.float32)
    canvas[sy0:sy1, sx0:sx1] = np.clip(
        region * (1.0 - c) + fg_arr * c + 0.5, 0, 255
    ).astype(np.uint8)


def text_width(text: str, pt: int, bold: bool, scale: float = 1.0) -> int:
    return int(np.ceil(_font(bold, font_px(pt, scale)).getlength(text)))


def font_px(pt: int, scale: float) -> int:
    return max(1, int(round(pt * PX_PER_PT * scale)))


def linespace(pt: int, bold: bool, scale: float = 1.0) -> int:
    ascent, descent = _font(bold, font_px(pt, scale)).getmetrics()
    return ascent + descent


# -------------------------------------------------------------------- widgets


@dataclass
class Widget:
    """A Tk widget placed at logical (100%) coordinates."""

    kind: str  # label | button | entry | icon
    name: str
    x: int
    y: int
    text: str = ""
    pt: int = 16
    bold: bool = False
    fg: str = "#101418"
    bg: Optional[str] = None
    width: Optional[int] = None  # logical px
    height: Optional[int] = None  # logical px
    chars: Optional[int] = None  # Tk ``width`` in average characters
    anchor_w: bool = False
    relief: str = "flat"
    bd: int = 0
    #: Tooltip shown while the pointer rests on the widget (hover feedback on).
    tooltip: Optional[str] = None

    def size(self) -> tuple[int, int]:
        """Logical size, from Tk's geometry rules at 100%."""
        avg = text_width("0", self.pt, self.bold)
        line = linespace(self.pt, self.bold)
        if self.kind == "label":
            return (text_width(self.text, self.pt, self.bold) + 6, line + 6)
        if self.kind == "button":
            w = self.width
            if w is None:
                chars = self.chars or len(self.text)
                w = chars * avg + 2 * (11 + 2 + 1)
            h = self.height if self.height is not None else line + 2 * (4 + 2 + 1)
            return (w, h)
        if self.kind == "entry":
            return ((self.chars or 20) * avg + 2 * (1 + 1) + 2, line + 2 * (1 + 1) + 2)
        if self.kind == "icon":
            return (self.width or 48, self.height or 44)
        raise ValueError(self.kind)


@dataclass
class KioskSpec:
    """One kiosk: background, widgets, and how a save reads its state."""

    name: str
    bg: str
    row_bg: str
    widgets: list[Widget]
    patients: list[tuple[str, str]]
    active_label: str  # widget name of the active-record banner
    active_none: str
    active_format: str
    status_label: str
    fields: list[str]
    save_button: str
    rows: list[str]
    #: Icon buttons and what they do: ``save`` or ``clear`` (empty the form).
    icon_actions: dict[str, str] = field(default_factory=dict)


def rdp_appointments_spec(
    patients: tuple[tuple[str, str], ...] = (
        ("Ada Lovelace", "MRN A1001"),
        ("Grace Hopper", "MRN B2002"),
    ),
    name: str = "rdp_appointments",
) -> KioskSpec:
    """``benchmark/rdp_ladder/fixture/kiosk_app.py`` (light theme).

    The fixture's record IDs, ``A1001`` and ``B2002``, rest on glyphs OCR
    confuses (1/l, 0/O). The identity check can never confirm them from OCR,
    only from a pixel match against the recording.
    """
    bg, fg, row_bg, btn_bg = "#f4f6f8", "#101418", "#ffffff", "#2d6fd8"
    (ada, ada_mrn), (grace, grace_mrn) = patients
    widgets = [
        Widget(
            "label",
            "title",
            50,
            34,
            "Northstar Clinic  ·  Appointment Scheduling",
            26,
            True,
            fg,
        ),
        Widget("label", "roster", 50, 116, "Patient roster", 16, True, fg),
        Widget("label", "active", 550, 130, "Active record: (none)", 16, True, fg),
        Widget("label", "status", 550, 660, "", 16, True, "#1a7f37"),
        Widget(
            "button",
            "row0",
            50,
            166,
            f"{ada}    {ada_mrn}",
            18,
            False,
            fg,
            row_bg,
            430,
            54,
            anchor_w=True,
            relief="raised",
            bd=2,
        ),
        Widget(
            "button",
            "row1",
            50,
            236,
            f"{grace}    {grace_mrn}",
            18,
            False,
            fg,
            row_bg,
            430,
            54,
            anchor_w=True,
            relief="raised",
            bd=2,
        ),
        Widget("label", "slot_label", 550, 210, "Appointment slot", 16, True, fg),
        Widget(
            "entry",
            "slot",
            550,
            250,
            "",
            18,
            False,
            fg,
            row_bg,
            chars=35,
            relief="sunken",
            bd=1,
        ),
        Widget("label", "visit_label", 550, 330, "Visit type", 16, True, fg),
        Widget(
            "entry",
            "visit_type",
            550,
            370,
            "",
            18,
            False,
            fg,
            row_bg,
            chars=35,
            relief="sunken",
            bd=1,
        ),
        Widget("label", "request_label", 550, 450, "Request ID", 16, True, fg),
        Widget(
            "entry",
            "request_id",
            550,
            490,
            "",
            18,
            False,
            fg,
            row_bg,
            chars=35,
            relief="sunken",
            bd=1,
        ),
        Widget(
            "button",
            "save",
            550,
            570,
            "Save appointment",
            18,
            True,
            "#ffffff",
            btn_bg,
            chars=20,
            relief="raised",
            bd=2,
        ),
    ]
    return KioskSpec(
        name=name,
        bg=bg,
        row_bg=row_bg,
        widgets=widgets,
        patients=list(patients),
        active_label="active",
        active_none="Active record: (none)",
        active_format="Active record: {name}  {mrn}",
        status_label="status",
        fields=["slot", "visit_type", "request_id"],
        save_button="save",
        rows=["row0", "row1"],
    )


def canvas_notes_spec() -> KioskSpec:
    """``benchmark/canvas_ladder/fixture/kiosk_app.py`` (light theme)."""
    bg, fg, row_bg, btn_bg = "#f4f6f8", "#101418", "#ffffff", "#2d6fd8"
    widgets = [
        Widget(
            "label",
            "title",
            60,
            40,
            "OpenAdapt Clinic Fixture  -  Patient Notes",
            26,
            True,
            fg,
        ),
        Widget("label", "roster", 60, 120, "Roster", 16, True, fg),
        Widget("label", "active", 60, 630, "Active: (none)", 16, False, fg),
        Widget("label", "status", 60, 690, "", 16, True, "#1a7f37"),
        Widget(
            "button",
            "row0",
            60,
            170,
            "Ada Lovelace    MRN 24682",
            18,
            False,
            fg,
            row_bg,
            chars=34,
            anchor_w=True,
            relief="raised",
            bd=2,
        ),
        Widget(
            "button",
            "row1",
            60,
            240,
            "Grace Hopper    MRN 73594",
            18,
            False,
            fg,
            row_bg,
            chars=34,
            anchor_w=True,
            relief="raised",
            bd=2,
        ),
        Widget("label", "note_label", 60, 530, "Clinical note", 16, True, fg),
        Widget(
            "entry",
            "note",
            60,
            570,
            "",
            18,
            False,
            fg,
            row_bg,
            chars=44,
            relief="sunken",
            bd=1,
        ),
        Widget(
            "button",
            "save",
            760,
            626,
            "Save Note",
            18,
            True,
            "#ffffff",
            btn_bg,
            chars=16,
            relief="raised",
            bd=2,
        ),
    ]
    return KioskSpec(
        name="canvas_notes",
        bg=bg,
        row_bg=row_bg,
        widgets=widgets,
        patients=[("Ada Lovelace", "MRN 24682"), ("Grace Hopper", "MRN 73594")],
        active_label="active",
        active_none="Active: (none)",
        active_format="Active: {name}  {mrn}",
        status_label="status",
        fields=["note"],
        save_button="save",
        rows=["row0", "row1"],
    )


def rdp_appointments_unambiguous_spec() -> KioskSpec:
    """The appointment kiosk with record IDs free of OCR-confusable glyphs,
    as the canvas fixture's are."""
    return rdp_appointments_spec(
        (("Ada Lovelace", "MRN A3467"), ("Grace Hopper", "MRN B7346")),
        name="rdp_appointments_unambiguous_ids",
    )


def rdp_appointments_icons_spec(swapped: bool = False) -> KioskSpec:
    """The appointment kiosk with its Save button turned into a toolbar icon.

    Two icons draw the same glyph and differ only by their tooltip:
    ``Save appointment`` and ``Clear form``. ``swapped`` exchanges their
    places, as a toolbar reorder between versions would. Record IDs are free
    of OCR-confusable glyphs, so identity is not what stops a run here.
    """
    spec = rdp_appointments_spec(
        (("Ada Lovelace", "MRN A3467"), ("Grace Hopper", "MRN B7346")),
        name="rdp_appointments_icons_swapped" if swapped else "rdp_appointments_icons",
    )
    btn_bg = "#2d6fd8"
    save_x, clear_x = (750, 550) if swapped else (550, 750)
    widgets = [w for w in spec.widgets if w.name != "save"]
    for name, x, tip in (
        ("save", save_x, "Save appointment"),
        ("clear", clear_x, "Clear form"),
    ):
        widgets.append(
            Widget(
                "icon",
                name,
                x,
                570,
                bg=btn_bg,
                width=56,
                height=48,
                relief="raised",
                bd=2,
                tooltip=tip,
            )
        )
    spec.widgets = widgets
    spec.icon_actions = {"save": "save", "clear": "clear"}
    return spec


SPECS: dict[str, Callable[[], KioskSpec]] = {
    "rdp_appointments": rdp_appointments_spec,
    "rdp_appointments_unambiguous_ids": rdp_appointments_unambiguous_spec,
    "canvas_notes": canvas_notes_spec,
}


@dataclass
class KioskState:
    active: Optional[int] = None
    focus: Optional[str] = None
    values: dict[str, str] = field(default_factory=dict)
    status: str = ""
    status_fg: str = "#1a7f37"
    saved: list[dict[str, str]] = field(default_factory=list)


class ScaledKiosk:
    """Kiosk state machine plus a renderer for one :class:`RenderProfile`."""

    def __init__(
        self, spec: KioskSpec, profile: RenderProfile, *, hover_feedback: bool = False
    ) -> None:
        self.spec = spec
        self.profile = profile
        self.state = KioskState(values={name: "" for name in spec.fields})
        self._by_name = {w.name: w for w in spec.widgets}
        # Tk draws a button under the pointer in its active colour and the
        # application may show a tooltip. Off by default: the display-scale
        # sweep was measured without it.
        self.hover_feedback = hover_feedback
        self.pointer: Optional[tuple[int, int]] = None

    # -- geometry --------------------------------------------------------
    def logical_rect(self, name: str) -> tuple[int, int, int, int]:
        w = self._by_name[name]
        width, height = w.size()
        return (w.x, w.y, width, height)

    def _px(self, value: float) -> int:
        return int(round(value * self.profile.scale))

    def _rect(self, name: str) -> tuple[int, int, int, int]:
        x, y, w, h = self.logical_rect(name)
        return (
            self._px(x),
            self._px(y),
            self._px(x + w) - self._px(x),
            self._px(y + h) - self._px(y),
        )

    def logical_center(self, name: str) -> tuple[int, int]:
        x, y, w, h = self.logical_rect(name)
        return (x + w // 2, y + h // 2)

    # -- interaction -----------------------------------------------------
    def _hit(self, x: int, y: int) -> Optional[str]:
        for w in self.spec.widgets:
            if w.kind not in ("button", "entry", "icon"):
                continue
            rx, ry, rw, rh = self._rect(w.name)
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return w.name
        return None

    def click(self, x: int, y: int) -> None:
        name = self._hit(int(x), int(y))
        if name is None:
            return
        if name in self.spec.rows:
            self.state.active = self.spec.rows.index(name)
        elif name in self.spec.fields:
            self.state.focus = name
        elif self.spec.icon_actions.get(name) == "clear":
            self.state.values = {k: "" for k in self.state.values}
            self.state.status = "Form cleared"
            self.state.status_fg = "#b42318"
        elif name == self.spec.save_button:
            self.save()

    def type_text(self, text: str) -> None:
        if self.state.focus is not None:
            self.state.values[self.state.focus] += text

    def save(self) -> None:
        values = {k: v.strip() for k, v in self.state.values.items()}
        if self.state.active is None or not all(values.values()):
            self.state.status = "Refused: select a patient and complete all fields"
            self.state.status_fg = "#b42318"
            return
        name, mrn = self.spec.patients[self.state.active]
        self.state.saved.append({"patient_name": name, "patient_mrn": mrn, **values})
        self.state.status = f"Saved for {name}"
        self.state.status_fg = "#1a7f37"

    def select_patient(self, index: int) -> None:
        self.state.active = index

    # -- rendering -------------------------------------------------------
    def _text_for(self, w: Widget) -> str:
        if w.name == self.spec.active_label:
            if self.state.active is None:
                return self.spec.active_none
            name, mrn = self.spec.patients[self.state.active]
            return self.spec.active_format.format(name=name, mrn=mrn)
        if w.name == self.spec.status_label:
            return self.state.status
        if w.kind == "entry":
            return self.state.values.get(w.name, "")
        return w.text

    def render(self) -> Image.Image:
        s = self.profile.scale
        fs = s * self.profile.font_scale
        vw, vh = self.profile.viewport
        canvas = np.empty((vh, vw, 3), dtype=np.uint8)
        canvas[:, :] = _rgb(self.spec.bg)
        for w in self.spec.widgets:
            rx, ry, rw, rh = self._rect(w.name)
            text = self._text_for(w)
            fg = _rgb(
                self.state.status_fg if w.name == self.spec.status_label else w.fg
            )
            bg = _rgb(w.bg) if w.bg else _rgb(self.spec.bg)
            if w.kind in ("button", "icon") and self._under_pointer(w.name):
                bg = _shade(bg, 1.18)  # Tk activebackground
            if w.kind != "label":
                canvas[ry : ry + rh, rx : rx + rw] = bg
                relief = w.relief
                bd = w.bd
                if (
                    w.name in self.spec.rows
                    and self.state.active == self.spec.rows.index(w.name)
                ):
                    relief, bd = "sunken", 4
                self._bevel(canvas, (rx, ry, rw, rh), bg, relief, max(1, self._px(bd)))
            if w.kind == "icon":
                self._glyph(canvas, (rx, ry, rw, rh))
                continue
            pad = self._px(3)
            line = linespace(w.pt, w.bold, fs)
            if w.kind == "label":
                origin = (rx + pad, ry + pad)
            elif w.kind == "entry":
                origin = (rx + self._px(3), ry + (rh - line) // 2)
            else:
                tw = text_width(text, w.pt, w.bold, fs)
                tx = rx + self._px(16) if w.anchor_w else rx + (rw - tw) // 2
                origin = (tx, ry + (rh - line) // 2)
            _draw_text(
                canvas,
                origin,
                text,
                font_px=font_px(w.pt, fs),
                bold=w.bold,
                fg=fg,
                cleartype=self.profile.cleartype,
                clip=(rx, ry, rw, rh) if w.kind != "label" else None,
            )
            if w.kind == "entry" and self.state.focus == w.name:
                cx = origin[0] + text_width(text, w.pt, w.bold, fs) + 1
                cw = max(1, self._px(2))
                canvas[origin[1] : origin[1] + line, cx : cx + cw] = fg
        self._tooltip(canvas)
        return Image.fromarray(canvas, "RGB")

    def _under_pointer(self, name: str) -> bool:
        if not self.hover_feedback or self.pointer is None:
            return False
        return self._hit(*self.pointer) == name

    def _glyph(self, canvas: np.ndarray, rect: tuple[int, int, int, int]) -> None:
        """The same white disc-and-bar glyph on every icon."""
        x, y, w, h = rect
        cx, cy = x + w // 2, y + h // 2
        r = max(3, min(w, h) // 4)
        yy, xx = np.ogrid[: canvas.shape[0], : canvas.shape[1]]
        disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
        canvas[disc] = (255, 255, 255)
        bar = max(1, r // 3)
        canvas[cy - bar : cy + bar, cx - 2 * r : cx + 2 * r] = (255, 255, 255)

    def _tooltip(self, canvas: np.ndarray) -> None:
        if not self.hover_feedback or self.pointer is None:
            return
        name = self._hit(*self.pointer)
        widget = self._by_name.get(name) if name else None
        if widget is None or not widget.tooltip:
            return
        fs = self.profile.scale * self.profile.font_scale
        px, py = self.pointer
        tx, ty = px + self._px(12), py + self._px(20)
        tw = text_width(widget.tooltip, 11, False, fs) + self._px(8)
        th = linespace(11, False, fs) + self._px(6)
        vh, vw = canvas.shape[:2]
        tx, ty = min(tx, vw - tw - 1), min(ty, vh - th - 1)
        canvas[ty : ty + th, tx : tx + tw] = (255, 255, 225)
        canvas[ty, tx : tx + tw] = canvas[ty + th - 1, tx : tx + tw] = (0, 0, 0)
        canvas[ty : ty + th, tx] = canvas[ty : ty + th, tx + tw - 1] = (0, 0, 0)
        _draw_text(
            canvas,
            (tx + self._px(4), ty + self._px(3)),
            widget.tooltip,
            font_px=font_px(11, fs),
            bold=False,
            fg=(0, 0, 0),
            cleartype=self.profile.cleartype,
            clip=(tx, ty, tw, th),
        )

    @staticmethod
    def _bevel(
        canvas: np.ndarray,
        rect: tuple[int, int, int, int],
        bg: tuple[int, int, int],
        relief: str,
        bd: int,
    ) -> None:
        if relief == "flat":
            return
        x, y, w, h = rect
        light, dark = _shade(bg, 1.6), _shade(bg, 0.55)
        top, bottom = (light, dark) if relief == "raised" else (dark, light)
        for i in range(bd):
            canvas[y + i, x + i : x + w - i] = top
            canvas[y + i : y + h - i, x + i] = top
            canvas[y + h - 1 - i, x + i : x + w - i] = bottom
            canvas[y + i : y + h - i, x + w - 1 - i] = bottom

    def screenshot_png(self) -> bytes:
        buffer = io.BytesIO()
        self.render().save(buffer, format="PNG")
        return buffer.getvalue()


class KioskBackend:
    """Pixel-only ``Backend`` over a :class:`ScaledKiosk`.

    It also implements the guarded-coordinate seam: a consequential click is
    delivered only when the frame is byte-identical to the one identity was
    verified on. ``after_type`` runs after each typed string; the wrong-record
    campaign uses it to switch the active record before the save.
    """

    def __init__(
        self,
        kiosk: ScaledKiosk,
        *,
        after_type: Optional[Callable[[ScaledKiosk, str], None]] = None,
    ) -> None:
        self.kiosk = kiosk
        self._after_type = after_type
        self.clicks: list[tuple[int, int]] = []
        self._guarded: Optional[tuple[int, int]] = None

    @property
    def viewport(self) -> tuple[int, int]:
        return self.kiosk.profile.viewport

    def screenshot(self) -> bytes:
        return self.kiosk.screenshot_png()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.clicks.append((int(x), int(y)))
        self.kiosk.pointer = (int(x), int(y))
        self.kiosk.click(int(x), int(y))

    def hover(self, x: int, y: int) -> None:
        """Rest the pointer on (x, y); with hover feedback on, the kiosk draws
        the control's active colour and tooltip."""
        self.kiosk.pointer = (int(x), int(y))

    def type_text(self, text: str) -> None:
        self.kiosk.type_text(text)
        if self._after_type is not None:
            self._after_type(self.kiosk, text)

    def press(self, key: str) -> None:
        return None

    def scroll(self, dx: int, dy: int) -> None:
        return None

    # -- guarded coordinate delivery ---------------------------------------
    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        self._guarded = (int(x), int(y))

    def cancel_guarded_coordinate(self) -> None:
        self._guarded = None

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ):
        from datetime import datetime, timezone

        from openadapt_flow.ir import ActionDeliveryReceipt

        if self._guarded != (int(x), int(y)):
            raise RuntimeError("guarded coordinate target was not armed")
        if hashlib.sha256(self.screenshot()).hexdigest() != expected_frame_sha256:
            raise RuntimeError("guarded coordinate frame changed")
        self._guarded = None
        self.click(x, y, double=double)
        return ActionDeliveryReceipt(
            receipt_id="kiosk-guarded-click",
            operation="guarded_coordinate_click",
            native=False,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )
