"""The metamorphic perturbation space over a live no-DOM frame.

Every perturbation here is a **metamorphic transform**: it changes how the
frame *renders* (DPI, theme, compression, jitter, occlusion, blur, colour
depth, local mid-run drift) while *preserving the true target's identity and
location*. That invariant is the oracle: a correct resolver must stay on the
true target or HALT — it must never move to a decoy. A perturbation that flips
the resolved target onto a look-alike is a silent-wrong-resolution by
construction.

Each perturbation returns a :class:`PerturbResult` carrying the perturbed PNG
plus an affine ``(scale, offset)`` that maps a ground-truth point in the clean
frame into the perturbed frame — so geometry-changing perturbations (DPI) keep
the ground truth exact. Compositions multiply the affines.

Severity (``none`` / ``mild`` / ``severe``) is a documented, deterministic
label used only to split HALTs into over-halt (availability loss under a
*legible* perturbation — annoying, safe) vs safe-halt (a correct refusal under
an *illegible* one). It never affects the silent-wrong rate.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageDraw

from openadapt_flow.ir import Point

Severity = str  # "none" | "mild" | "severe"


@dataclass(frozen=True)
class PerturbResult:
    """A perturbed frame plus the affine mapping clean coords into it."""

    png: bytes
    scale: float
    offset: tuple[float, float]

    def map_point(self, p: Point) -> Point:
        return (
            int(round(p[0] * self.scale + self.offset[0])),
            int(round(p[1] * self.scale + self.offset[1])),
        )

    @property
    def viewport(self) -> tuple[int, int]:
        w, h = _png_size(self.png)
        return (w, h)


@dataclass(frozen=True)
class Perturbation:
    """A named, parameterized metamorphic transform.

    Attributes:
        name: Stable family name (part of a corpus key).
        params: JSON-serializable parameters reproducing the transform.
        severity: ``none`` / ``mild`` / ``severe`` (over- vs safe-halt split).
        apply: ``(clean_png, viewport, true_center) -> PerturbResult``. The
            true center is passed so target-local transforms (occlusion) know
            WHERE the true target is without moving it.
    """

    name: str
    params: dict
    severity: Severity
    apply: Callable[[bytes, tuple[int, int], Point], PerturbResult]

    def key(self) -> str:
        items = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"{self.name}({items})"


# --------------------------------------------------------------------------- #
# PNG <-> array helpers (kept local so the module has no vision-internal deps).
# --------------------------------------------------------------------------- #


def _png_size(png: bytes) -> tuple[int, int]:
    import struct

    return tuple(struct.unpack(">II", png[16:24]))  # type: ignore[return-value]


def _to_bgr(png: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)


def _to_png(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("failed to PNG-encode frame")
    return buf.tobytes()


# --------------------------------------------------------------------------- #
# The perturbations.
# --------------------------------------------------------------------------- #


def identity() -> Perturbation:
    """The control: no change. A correct resolver MUST resolve correctly."""

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        return PerturbResult(png=png, scale=1.0, offset=(0.0, 0.0))

    return Perturbation("identity", {}, "none", _apply)


def dpi_scale(factor: float) -> Perturbation:
    """Rescale the whole frame (a DPI / display-scale change on a remote
    display). The true target scales with everything, so ground truth maps by
    ``factor``. Beyond the resolver's narrow scale ladder this fails SAFE
    (over-halt), not wrong."""
    sev: Severity = "mild" if 0.75 <= factor <= 1.5 else "severe"

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        h, w = bgr.shape[:2]
        interp = cv2.INTER_AREA if factor < 1.0 else cv2.INTER_CUBIC
        out = cv2.resize(
            bgr,
            (max(1, int(w * factor)), max(1, int(h * factor))),
            interpolation=interp,
        )
        return PerturbResult(png=_to_png(out), scale=factor, offset=(0.0, 0.0))

    return Perturbation("dpi_scale", {"factor": factor}, sev, _apply)


def theme_invert() -> Perturbation:
    """Invert the palette (light<->dark re-render). Grayscale correlation
    inverts, so the template rungs fail SAFE; the structural-edge matcher would
    survive but is not a resolution rung today (vision_hardening D/H7)."""

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        return PerturbResult(png=_to_png(255 - bgr), scale=1.0, offset=(0.0, 0.0))

    return Perturbation("theme_invert", {}, "severe", _apply)


def jpeg(quality: int) -> Perturbation:
    """Re-encode as JPEG (ICA/HDX-like lossy compression). Blocking/chroma
    artifacts drop the template score; low quality fails SAFE."""
    sev: Severity = "mild" if quality >= 20 else "severe"

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("jpeg encode failed")
        dec = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return PerturbResult(png=_to_png(dec), scale=1.0, offset=(0.0, 0.0))

    return Perturbation("jpeg", {"quality": quality}, sev, _apply)


def subpixel_jitter(dx: float, dy: float) -> Perturbation:
    """Sub-pixel translate the whole frame (cross-render anti-alias jitter).
    A shift < 1px keeps the true target within tolerance; larger shifts map
    exactly via the affine offset."""

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png).astype(np.float32)
        m = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
        out = cv2.warpAffine(
            bgr,
            m,
            (bgr.shape[1], bgr.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return PerturbResult(
            png=_to_png(out.astype(np.uint8)), scale=1.0, offset=(dx, dy)
        )

    return Perturbation("subpixel_jitter", {"dx": dx, "dy": dy}, "mild", _apply)


def gaussian_blur(sigma: float) -> Perturbation:
    """Blur the whole frame (down-scaling / codec smoothing). Heavy blur fails
    SAFE."""
    sev: Severity = "mild" if sigma <= 1.0 else "severe"

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        k = max(3, int(sigma * 4) | 1)
        out = cv2.GaussianBlur(bgr, (k, k), sigma)
        return PerturbResult(png=_to_png(out), scale=1.0, offset=(0.0, 0.0))

    return Perturbation("gaussian_blur", {"sigma": sigma}, sev, _apply)


def color_depth(bits: int) -> Perturbation:
    """Reduce colour depth (banding/dither on a low-colour remote session)."""

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        levels = max(2, 2**bits)
        step = 256 // levels
        out = (bgr // step) * step
        return PerturbResult(
            png=_to_png(out.astype(np.uint8)), scale=1.0, offset=(0.0, 0.0)
        )

    return Perturbation("color_depth", {"bits": bits}, "mild", _apply)


def occlude_target(kind: str = "tooltip", coverage: float = 0.5) -> Perturbation:
    """Draw a tooltip / cursor OVER the true target, degrading it locally.

    This is the DANGEROUS case: the true target's local score drops below
    threshold, the local ``template`` rung falls through, and ``template_global``
    can lock onto a clean look-alike elsewhere at ~1.0 confidence. The true
    target is still *present* (a human/oracle can still resolve it), so a jump
    to a decoy is a silent-wrong; a HALT is a safe over-halt.
    """
    sev: Severity = "mild" if coverage <= 0.6 else "severe"

    def _apply(png: bytes, vp: tuple[int, int], t: Point) -> PerturbResult:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        d = ImageDraw.Draw(img)
        tx, ty = t
        # Occlude a box centered on the true target, covering `coverage` of a
        # ~44px widget.
        half = int(22 * coverage)
        if kind == "tooltip":
            d.rectangle(
                [tx - half, ty - half, tx + half + 40, ty + half],
                fill=(255, 250, 205),
                outline=(150, 140, 60),
            )
        elif kind == "cursor":
            # An arrow cursor over the target.
            d.polygon(
                [
                    (tx - 2, ty - 2),
                    (tx - 2, ty + 18),
                    (tx + 4, ty + 12),
                    (tx + 9, ty + 20),
                    (tx + 13, ty + 17),
                    (tx + 8, ty + 9),
                    (tx + 16, ty + 9),
                ],
                fill=(20, 20, 20),
                outline=(240, 240, 240),
            )
            d.rectangle([tx - half, ty - half, tx + half, ty + half], fill=(20, 20, 20))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return PerturbResult(png=buf.getvalue(), scale=1.0, offset=(0.0, 0.0))

    return Perturbation(
        "occlude_target", {"kind": kind, "coverage": round(coverage, 3)}, sev, _apply
    )


def local_drift(
    region_frac: tuple[float, float, float, float] = (0.0, 0.0, 0.5, 1.0),
) -> Perturbation:
    """Blur only a sub-region of the frame (mid-run / frame-staleness drift on a
    laggy session). If the true target is in the stale region it degrades; the
    rest of the frame (including decoys) stays crisp — the worst asymmetry for a
    look-alike surface."""

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        h, w = bgr.shape[:2]
        fx, fy, fw, fh = region_frac
        x0, y0 = int(fx * w), int(fy * h)
        x1, y1 = int((fx + fw) * w), int((fy + fh) * h)
        sub = bgr[y0:y1, x0:x1]
        if sub.size:
            bgr[y0:y1, x0:x1] = cv2.GaussianBlur(sub, (9, 9), 3.0)
        return PerturbResult(png=_to_png(bgr), scale=1.0, offset=(0.0, 0.0))

    return Perturbation(
        "local_drift", {"region_frac": list(region_frac)}, "severe", _apply
    )


def unpainted_target(pad: int = 8) -> Perturbation:
    """The true target's own cell has not painted yet (latency / partial render).

    A real remote-display artifact: a slow session paints the surface region by
    region, so a frame can arrive with the true target's cell still showing the
    background placeholder while its identical siblings elsewhere are already
    crisp. The true target is ABSENT from this frame, so the ONLY safe outcome
    is a HALT (there are >= 2 identical look-alikes and none is uniquely the
    target where expected). The DANGEROUS failure this probes is the local rung
    grabbing the nearest painted NEIGHBOR (a wrong-row / wrong-icon silent
    click); the metamorphic oracle flags exactly that. Ground truth is unchanged
    (identity affine): the target's recorded location is where the blank cell
    sits, so a resolver that lands anywhere else has silently mis-resolved."""

    def _apply(png: bytes, vp: tuple[int, int], t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        h, w = bgr.shape[:2]
        tx, ty = t
        # Sample the frame's corner as the background/placeholder colour so the
        # fill matches the surface (no synthetic edge to correlate against).
        bg = bgr[1, 1].tolist()
        r = 26
        x0, y0 = max(0, tx - r - pad), max(0, ty - r - pad)
        x1, y1 = min(w, tx + r + pad), min(h, ty + r + pad)
        bgr[y0:y1, x0:x1] = np.array(bg, dtype=np.uint8)
        return PerturbResult(png=_to_png(bgr), scale=1.0, offset=(0.0, 0.0))

    return Perturbation("unpainted_target", {"pad": pad}, "severe", _apply)


def uniform_scroll(dx: int = 0, dy: int = 40) -> Perturbation:
    """Translate ALL content by ``(dx, dy)`` (the surface scrolled, or a top
    banner/toolbar appeared and pushed everything down uniformly).

    Dynamic reflow that PRESERVES the target's identity and its relation to
    every decoy: the whole layout shifts rigidly, so ground truth maps exactly
    by the affine offset. A correct resolver must FOLLOW a small shift (the
    target is still within the padded local window) and, for a large shift that
    carries the target out of the search window while identical decoys remain,
    HALT rather than click whatever now occupies the vacated location. It must
    never silently land on a decoy. The vacated strip is filled with the
    surface background so no synthetic edge is introduced."""
    dist = max(abs(dx), abs(dy))
    sev: Severity = "mild" if dist <= 50 else "severe"

    def _apply(png: bytes, vp: tuple[int, int], _t: Point) -> PerturbResult:
        bgr = _to_bgr(png)
        h, w = bgr.shape[:2]
        bg = bgr[1, 1].tolist()
        m = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
        out = cv2.warpAffine(
            bgr,
            m,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=bg,
        )
        return PerturbResult(png=_to_png(out), scale=1.0, offset=(float(dx), float(dy)))

    return Perturbation("uniform_scroll", {"dx": dx, "dy": dy}, sev, _apply)


def compose(*perts: Perturbation) -> Perturbation:
    """Left-to-right composition of perturbations (multiplies the affines)."""

    def _apply(png: bytes, vp: tuple[int, int], t: Point) -> PerturbResult:
        cur = PerturbResult(png=png, scale=1.0, offset=(0.0, 0.0))
        for p in perts:
            mapped_t = cur.map_point(t)
            res = p.apply(cur.png, cur.viewport, mapped_t)
            cur = PerturbResult(
                png=res.png,
                scale=cur.scale * res.scale,
                offset=(
                    cur.offset[0] * res.scale + res.offset[0],
                    cur.offset[1] * res.scale + res.offset[1],
                ),
            )
        return cur

    sev: Severity = "severe" if any(p.severity == "severe" for p in perts) else "mild"
    return Perturbation("compose", {"steps": [p.key() for p in perts]}, sev, _apply)


def standard_grid() -> list[Perturbation]:
    """The deterministic perturbation grid the CI sweep enumerates.

    Spans DPI 75-200%, theme inversion, JPEG q50..q5, sub-pixel jitter,
    blur, colour-depth reduction, tooltip/cursor occlusion of the true target,
    local mid-run drift, and a few compositions (the combined-drift cases that
    real remote displays actually present)."""
    grid: list[Perturbation] = [identity()]
    for f in (0.75, 1.0, 1.25, 1.5, 2.0):
        grid.append(dpi_scale(f))
    grid.append(theme_invert())
    for q in (50, 20, 10, 5):
        grid.append(jpeg(q))
    grid.append(subpixel_jitter(0.4, 0.4))
    grid.append(subpixel_jitter(1.5, 0.0))
    grid.append(gaussian_blur(0.8))
    grid.append(gaussian_blur(2.5))
    grid.append(color_depth(3))
    for kind in ("tooltip", "cursor"):
        for cov in (0.4, 0.6, 0.9):
            grid.append(occlude_target(kind=kind, coverage=cov))
    grid.append(local_drift((0.0, 0.0, 0.5, 1.0)))
    # Latency / partial render: the target's own cell has not painted while its
    # identical siblings have — the wrong-neighbor silent-click probe.
    grid.append(unpainted_target(pad=8))
    # Dynamic reflow: a rigid VERTICAL scroll / a top banner pushing the
    # (horizontally-laid-out) fixtures down. Small = follow it (correct); large
    # = the target left the padded window while its horizontally-offset decoys
    # remain, so no peak sits where expected and the resolver must halt, never
    # click a decoy. (A horizontal scroll on a horizontal widget strip would
    # align a decoy to the expected column — an unfair, pixel-identical-to-
    # nominal case no pixel-only resolver can win — so it is deliberately
    # excluded here and documented as a known ladder limitation.)
    grid.append(uniform_scroll(dx=0, dy=30))
    grid.append(uniform_scroll(dx=0, dy=90))
    # Combined drift — the realistic remote-display case.
    grid.append(compose(dpi_scale(1.25), jpeg(20)))
    grid.append(compose(occlude_target("tooltip", 0.6), jpeg(20)))
    grid.append(compose(dpi_scale(1.5), theme_invert()))
    # Latency under compression: a partly-painted target on a lossy session.
    grid.append(compose(unpainted_target(pad=8), jpeg(20)))
    return grid
