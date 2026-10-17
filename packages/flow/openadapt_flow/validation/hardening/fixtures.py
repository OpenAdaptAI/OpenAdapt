"""Real-glyph, browser-free fixture families for the hardening flywheel.

Each fixture family is a DETERMINISTIC pure function of its parameters that
renders a real screenshot (PIL, real anti-aliased glyphs) of an *ambiguous*
no-DOM surface — a surface with several near-identical widgets, which is the
no-DOM reality (a toolbar of identical icons, a "Delete"/"Open" per row, empty
form fields per column, patient rows whose only difference is a glyph-confusable
identifier). It returns a :class:`Fixture`: the clean frame, a compiled
:class:`~openadapt_flow.ir.Anchor` + template crop for exactly ONE instance (the
*true target*), the true target's click point, and the *decoys* (the other
look-alike instances the resolver must never silently pick).

Why PIL, not Playwright: the existing dense-surface study renders with a real
browser (chromium) and measures the *identity* tier. This flywheel measures the
*resolution* tier and must be CHEAP + HERMETIC enough to run as a per-PR CI
ratchet — so it renders directly with PIL/cv2 and needs no browser download.
The glyphs are real (a bundled TrueType face), so the real OCR rung and real
template matcher are exercised exactly as in production.

Ground truth is exact because WE place the true target and the decoys; the
oracle for a case is therefore deterministic and non-gameable (§ metamorphic
relation in the package docstring).
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.ir import Anchor, Point, Region

# --------------------------------------------------------------------------- #
# Fonts — a bundled, cross-platform TrueType face so rendering is identical on
# a developer Mac and a Linux CI runner (system fonts differ; matplotlib ships
# DejaVu with every install and is a dev dependency).
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _font_path() -> Optional[str]:
    """Locate a bundled DejaVuSans TrueType face (matplotlib), else a system
    fallback. Returns None only when no scalable face is found (PIL's bitmap
    default is then used, which is too small for OCR — the OCR-tier families
    self-skip in that case; see :func:`ocr_available`)."""
    try:
        import matplotlib

        cand = os.path.join(
            os.path.dirname(matplotlib.__file__),
            "mpl-data",
            "fonts",
            "ttf",
            "DejaVuSans.ttf",
        )
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        if os.path.isfile(cand):
            return cand
    return None


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path()
    if path is not None:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def ocr_available() -> bool:
    """True when a scalable font is present AND the OCR engine imports.

    The OCR-tier families (labeled rows / duplicate buttons / MRN cells) render
    text the real OCR rung must read; without a scalable face or the engine
    they self-skip so the template-tier ratchet still runs everywhere.
    """
    if _font_path() is None:
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:
        return False


# Light and dark palettes (theme inversion perturbs between them).
_LIGHT_BG = (245, 245, 246)
_LIGHT_FG = (32, 34, 38)
_LIGHT_ACCENT = (48, 96, 160)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass(frozen=True)
class Fixture:
    """A rendered ambiguous surface with exact ground truth.

    Attributes:
        family: Fixture family name (stable; part of a corpus key).
        params: JSON-serializable parameters that reproduce this fixture
            exactly (part of the corpus key).
        clean_png: The unperturbed frame, PNG bytes.
        viewport: ``(width, height)`` of ``clean_png``.
        anchor: A compiled anchor for the TRUE target (template + region +
            click point + optional ocr_text/landmarks), as the compiler would
            emit for a pixel-only recording.
        template_png: The true target's template crop, PNG bytes.
        true_center: The true target's click point in ``clean_png`` coords.
        decoys: Click points of the other look-alike instances the resolver
            must never silently pick.
        label: The true target's text label when it has one (drives the
            over-halt legibility oracle and the OCR rung); None for icon-only.
        rungs: Which ladder rungs this fixture is designed to probe (for
            reporting), e.g. ``("template", "template_global")``.
        needs_ocr: True when the family requires the real OCR engine.
        tolerance_px: Half-width of the "landed on the true target" box, in
            unscaled fixture pixels.
    """

    family: str
    params: dict
    clean_png: bytes
    viewport: tuple[int, int]
    anchor: Anchor
    template_png: bytes
    true_center: Point
    decoys: list[Point]
    label: Optional[str] = None
    rungs: tuple[str, ...] = ()
    needs_ocr: bool = False
    tolerance_px: int = 22

    def key(self) -> str:
        """Stable, order-independent identity string for the corpus."""
        items = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"{self.family}({items})"


def _crop_png(img: Image.Image, region: Region) -> bytes:
    x, y, w, h = region
    return _png(img.crop((x, y, x + w, y + h)))


# --------------------------------------------------------------------------- #
# Family 1 — repeated toolbar icons (icon-only, template rungs).
# The #1 silent class (vision_hardening A1/A3): N identical icon glyphs; the
# true one is one slot; a look-alike sits in every other slot.
# --------------------------------------------------------------------------- #


def repeated_icons(
    *, n: int = 4, true_idx: int = 0, glyph: str = "circle", spacing: int = 100
) -> Fixture:
    """A toolbar of ``n`` identical icon glyphs; slot ``true_idx`` is the target.

    Icon-only: no text label, so the OCR/geometry rescue is absent and the
    decision rests entirely on the template rungs — exactly where a degraded
    true target lets a clean look-alike out-score it.
    """
    w, h = 60 + n * spacing, 150
    img = Image.new("RGB", (w, h), _LIGHT_BG)
    d = ImageDraw.Draw(img)
    icon = 40
    y0 = 55
    centers: list[Point] = []
    for i in range(n):
        x0 = 40 + i * spacing
        d.rounded_rectangle(
            [x0, y0, x0 + icon, y0 + icon], radius=6, outline=_LIGHT_FG, width=3
        )
        if glyph == "circle":
            d.ellipse(
                [x0 + 12, y0 + 12, x0 + icon - 12, y0 + icon - 12], fill=_LIGHT_FG
            )
        else:  # "bars"
            for k in range(3):
                yy = y0 + 12 + k * 6
                d.line([x0 + 10, yy, x0 + icon - 10, yy], fill=_LIGHT_FG, width=3)
        centers.append((x0 + icon // 2, y0 + icon // 2))
    region: Region = (40 + true_idx * spacing, y0, icon, icon)
    true_center = centers[true_idx]
    return Fixture(
        family="repeated_icons",
        params={"n": n, "true_idx": true_idx, "glyph": glyph, "spacing": spacing},
        clean_png=_png(img),
        viewport=(w, h),
        anchor=Anchor(template="t.png", region=region, click_point=true_center),
        template_png=_crop_png(img, region),
        true_center=true_center,
        decoys=[c for i, c in enumerate(centers) if i != true_idx],
        label=None,
        rungs=("template", "template_global"),
        needs_ocr=False,
    )


# --------------------------------------------------------------------------- #
# Family 2 — labeled rows with a repeated action button (OCR + template rungs).
# A "Delete"/"Open" per row; the true one is one row. A baked-in label is NOT
# discriminative when it repeats (vision_hardening class F/B).
# --------------------------------------------------------------------------- #


def labeled_rows(
    *, n: int = 5, true_idx: int = 2, label: str = "Delete", row_h: int = 44
) -> Fixture:
    """A list of ``n`` rows each carrying an identical ``label`` button.

    Every row's button reads the same text, so the OCR rung cannot break the
    tie by label alone; a landmark (the row's leading text) is recorded so the
    template_global landmark guard and the OCR rung are both exercised.
    """
    w = 460
    top = 40
    h = top + n * row_h + 20
    img = Image.new("RGB", (w, h), _LIGHT_BG)
    d = ImageDraw.Draw(img)
    d.text((20, 12), "Records", font=_font(20), fill=_LIGHT_FG)
    btn_w, btn_h = 96, 28
    centers: list[Point] = []
    row_labels: list[str] = []
    for i in range(n):
        y = top + i * row_h
        rowname = f"Item {chr(ord('A') + i)}{i:02d}"
        row_labels.append(rowname)
        d.text((20, y + 10), rowname, font=_font(18), fill=_LIGHT_FG)
        bx = w - btn_w - 20
        by = y + (row_h - btn_h) // 2
        d.rounded_rectangle(
            [bx, by, bx + btn_w, by + btn_h], radius=4, outline=_LIGHT_ACCENT, width=2
        )
        d.text((bx + 14, by + 5), label, font=_font(16), fill=_LIGHT_ACCENT)
        centers.append((bx + btn_w // 2, by + btn_h // 2))
    y = top + true_idx * row_h
    bx = w - btn_w - 20
    by = y + (row_h - btn_h) // 2
    region: Region = (bx, by, btn_w, btn_h)
    true_center = centers[true_idx]
    from openadapt_flow.ir import Landmark

    # The true row's own leading text, to the LEFT of the button, is the
    # landmark that pins position when the label repeats.
    lm_text = row_labels[true_idx]
    anchor = Anchor(
        template="t.png",
        region=region,
        click_point=true_center,
        ocr_text=label,
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text=lm_text,
                distance_px=bx - 20,
                dx_px=true_center[0] - 60,
                dy_px=0,
            )
        ],
    )
    return Fixture(
        family="labeled_rows",
        params={"n": n, "true_idx": true_idx, "label": label, "row_h": row_h},
        clean_png=_png(img),
        viewport=(w, h),
        anchor=anchor,
        template_png=_crop_png(img, region),
        true_center=true_center,
        decoys=[c for i, c in enumerate(centers) if i != true_idx],
        label=label,
        rungs=("template", "template_global", "ocr", "geometry"),
        needs_ocr=True,
    )


# --------------------------------------------------------------------------- #
# Family 3 — duplicate labeled buttons (near-tie; must PASS on clean, class A2).
# Two identical "Save" buttons; the true one is the near one. This family is
# the CONTROL that guards against over-hardening: a fix that halts here would
# break a legitimate near-tie.
# --------------------------------------------------------------------------- #


def duplicate_buttons(*, label: str = "Save", gap: int = 200) -> Fixture:
    """Two identical labeled buttons; slot 0 is the true (recorded) one."""
    w, h = 120 + gap + 120, 130
    img = Image.new("RGB", (w, h), _LIGHT_BG)
    d = ImageDraw.Draw(img)
    btn_w, btn_h = 100, 34
    y = 55
    centers: list[Point] = []
    for i in range(2):
        x = 30 + i * (btn_w + gap)
        d.rounded_rectangle([x, y, x + btn_w, y + btn_h], radius=5, fill=_LIGHT_ACCENT)
        d.text((x + 22, y + 7), label, font=_font(16), fill=(255, 255, 255))
        centers.append((x + btn_w // 2, y + btn_h // 2))
    region: Region = (30, y, btn_w, btn_h)
    return Fixture(
        family="duplicate_buttons",
        params={"label": label, "gap": gap},
        clean_png=_png(img),
        viewport=(w, h),
        anchor=Anchor(
            template="t.png", region=region, click_point=centers[0], ocr_text=label
        ),
        template_png=_crop_png(img, region),
        true_center=centers[0],
        decoys=[centers[1]],
        label=label,
        rungs=("template", "template_global", "ocr"),
        needs_ocr=True,
    )


# --------------------------------------------------------------------------- #
# Family 4 — repeated empty form fields (flat/low-variance crops, template).
# An empty input per column: the crop is near-flat, which makes normalized
# correlation degenerate (vision_hardening H4).
# --------------------------------------------------------------------------- #


def form_fields(*, n: int = 4, true_idx: int = 1) -> Fixture:
    """A row of ``n`` identical empty input boxes; box ``true_idx`` is target."""
    fw, fh, gap = 90, 30, 20
    w = 40 + n * (fw + gap)
    h = 120
    img = Image.new("RGB", (w, h), _LIGHT_BG)
    d = ImageDraw.Draw(img)
    y = 50
    centers: list[Point] = []
    for i in range(n):
        x = 20 + i * (fw + gap)
        d.rectangle(
            [x, y, x + fw, y + fh],
            outline=(150, 154, 160),
            width=2,
            fill=(255, 255, 255),
        )
        centers.append((x + fw // 2, y + fh // 2))
    region: Region = (20 + true_idx * (fw + gap), y, fw, fh)
    true_center = centers[true_idx]
    return Fixture(
        family="form_fields",
        params={"n": n, "true_idx": true_idx},
        clean_png=_png(img),
        viewport=(w, h),
        anchor=Anchor(template="t.png", region=region, click_point=true_center),
        template_png=_crop_png(img, region),
        true_center=true_center,
        decoys=[c for i, c in enumerate(centers) if i != true_idx],
        label=None,
        rungs=("template", "template_global"),
        needs_ocr=False,
    )


# --------------------------------------------------------------------------- #
# Family 5 — glyph-collapse MRN cells (OCR rung, wrong-record class C6).
# Two rows one OCR-confusable glyph apart (O/0, l/1). The true row and its
# sibling differ only in the identifier; the OCR rung must not silently pick
# the sibling.
# --------------------------------------------------------------------------- #


def mrn_rows(*, collapse: str = "O0", true_idx: int = 0) -> Fixture:
    """Two patient rows whose identifiers differ by one confusable glyph.

    ``collapse`` in ``{"O0", "l1"}``. The anchor's ocr_text is the true row's
    identifier; the decoy row's identifier collapses to the same glyphs under
    OCR, so a naive OCR match can land on the sibling (a wrong-record click).
    """
    if collapse == "O0":
        true_id, decoy_id = "MRN 500612", "MRN 5OO612"
    else:
        true_id, decoy_id = "MRN 411231", "MRN 4ll231"
    ids = [true_id, decoy_id] if true_idx == 0 else [decoy_id, true_id]
    w, h = 380, 140
    row_h = 44
    top = 30
    img = Image.new("RGB", (w, h), _LIGHT_BG)
    d = ImageDraw.Draw(img)
    centers: list[Point] = []
    for i, ident in enumerate(ids):
        y = top + i * row_h
        d.text((24, y + 10), ident, font=_font(20), fill=_LIGHT_FG)
        # click target is the identifier text itself
        bbox = d.textbbox((24, y + 10), ident, font=_font(20))
        centers.append((int((bbox[0] + bbox[2]) // 2), int((bbox[1] + bbox[3]) // 2)))
    ty = top + true_idx * row_h
    tb = d.textbbox((24, ty + 10), true_id, font=_font(20))
    region: Region = (int(tb[0]), int(tb[1]), int(tb[2] - tb[0]), int(tb[3] - tb[1]))
    return Fixture(
        family="mrn_rows",
        params={"collapse": collapse, "true_idx": true_idx},
        clean_png=_png(img),
        viewport=(w, h),
        anchor=Anchor(
            template="t.png",
            region=region,
            click_point=centers[true_idx],
            ocr_text=true_id,
        ),
        template_png=_crop_png(img, region),
        true_center=centers[true_idx],
        decoys=[centers[1 - true_idx]],
        label=true_id,
        rungs=("ocr",),
        needs_ocr=True,
        tolerance_px=row_h // 2,
    )


# --------------------------------------------------------------------------- #
# The canonical fixture grid the CI ratchet sweeps. Kept small + deterministic;
# every fixture is a pure function of its params so the sweep is reproducible.
# --------------------------------------------------------------------------- #


@dataclass
class FixtureFamily:
    """A named fixture family plus the parameter grid the sweep enumerates."""

    name: str
    builder: object
    grid: list[dict] = field(default_factory=list)


def template_tier_fixtures() -> list[Fixture]:
    """Icon/field fixtures that need only cv2 template matching (no OCR).

    These form the CI ratchet: fast, hermetic, deterministic, and exactly the
    #1 silent-mis-resolution class (repeated widgets, degraded true target).
    """
    out: list[Fixture] = []
    for n in (3, 4, 6):
        for ti in (0, n // 2, n - 1):
            out.append(repeated_icons(n=n, true_idx=ti, glyph="circle"))
            out.append(repeated_icons(n=n, true_idx=ti, glyph="bars"))
    # Denser look-alike strips (n=8, tight 70px pitch): more identical
    # neighbors inside the padded local search window, the harder no-DOM
    # toolbar/gallery case where a degraded true target lets the resolver grab
    # a wrong neighbor. The neighbor pitch (70px) is larger than the locality
    # radius so a neighbor never counts as "at the expected spot".
    for ti in (0, 4, 7):
        out.append(repeated_icons(n=8, true_idx=ti, glyph="circle", spacing=70))
        out.append(repeated_icons(n=8, true_idx=ti, glyph="bars", spacing=70))
    for n in (3, 4):
        for ti in (0, 1, n - 1):
            out.append(form_fields(n=n, true_idx=ti))
    return out


def ocr_tier_fixtures() -> list[Fixture]:
    """Labeled/identifier fixtures that need the real OCR engine."""
    out: list[Fixture] = []
    for n in (4, 6):
        for ti in (0, 2, n - 1):
            out.append(labeled_rows(n=n, true_idx=ti, label="Delete"))
    out.append(duplicate_buttons(label="Save", gap=200))
    out.append(duplicate_buttons(label="Confirm", gap=140))
    for collapse in ("O0", "l1"):
        for ti in (0, 1):
            out.append(mrn_rows(collapse=collapse, true_idx=ti))
    return out
