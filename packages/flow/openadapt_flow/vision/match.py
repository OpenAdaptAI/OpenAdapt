"""Multi-scale grayscale and structural-edge template matching.

Implements the `find_template` rung of the resolution ladder using
``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED`` over a small scale ladder.
``find_structural_template`` applies the same matcher to Canny edge maps for
postconditions whose recorded invariant is layout/content rather than palette.
All returned coordinates are in *screen* (full-frame) pixel space, even when
a ``search_region`` restricts the search.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from pydantic import BaseModel

from openadapt_flow.ir import Point, Region
from openadapt_flow.vision import imgops
from openadapt_flow.vision.imgops import cv2


class Match(BaseModel):
    """A located target on screen.

    Attributes:
        point: Click/center point in screen coordinates.
        region: Matched region ``(x, y, w, h)`` in screen coordinates.
        confidence: Match confidence in ``[0, 1]``.
    """

    point: Point
    region: Region
    confidence: float


def _decode_gray(png: bytes) -> np.ndarray:
    """Decode PNG bytes to a grayscale ``uint8`` image.

    Args:
        png: PNG-encoded image bytes.

    Returns:
        2-D grayscale image array.

    Raises:
        ValueError: If the bytes cannot be decoded as an image.
    """
    img = imgops.decode_gray(png)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    return img


def _clamp_region(region: Region, width: int, height: int) -> Optional[Region]:
    """Clamp ``region`` to image bounds; return None if empty after clamping."""
    x, y, w, h = region
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


# --- Locality / uniqueness gate (target-resolution only) -------------------
# When a caller passes ``prefer_near`` it is asserting an EXPECTED location for
# the target (the resolution ladder passes the recorded anchor origin). On a
# pixel-only substrate (RDP/Citrix) the frame frequently contains several
# near-identical widgets (an edit pencil per row, a gear per toolbar item, an
# empty form field per column). ``TM_CCOEFF_NORMED`` scores every instance ~1.0,
# so the raw arg-max returns an ARBITRARY instance -- and when the TRUE target is
# degraded (compression, a cursor/tooltip occluding it, sub-pixel jitter) a
# CLEANER look-alike out-scores it and is clicked SILENTLY (the wrong-row / wrong-
# icon class -- the most dangerous failure because nothing downstream on an
# unarmed step catches it). The gate makes two changes, both keyed on
# ``prefer_near`` so postcondition matching (which passes none) is untouched:
#   1. LOCALITY -- prefer the peak nearest the expected location over a cleaner
#      look-alike elsewhere, so a degraded/occluded true target still wins.
#   2. UNIQUENESS -- when >= 2 peaks clear ``threshold`` and NONE sits where
#      expected, the target is not uniquely present; return None so the ladder
#      falls through / halts rather than guess. A single peak far from expected
#      is a legitimately MOVED unique target and is kept.
# Radius (px) around the expected point that counts as "at the expected spot".
# Floored so small icons still admit a few px of cross-render drift; otherwise
# it scales with the smaller template dimension (~ the target's own size).
LOCALITY_MIN_PX = 48
# Pixels around the expected origin searched directly, so a target that sits
# where it was recorded is found even when NMS kept another point of its ridge.
EXPECTED_SPOT_JITTER_PX = 4
# Bound on the number of peaks enumerated per frame (cost guard).
MAX_PEAKS = 16
# Correlation score at/above which a SECOND, spatially-distinct response on the
# GLOBAL rung is treated as a degraded sibling of the same repeated widget
# (evidence the surface is ambiguous), not background noise. Chosen well above
# flat-background/text correlation (~0.1-0.5) yet below a same-theme redraw of
# an occluded/blurred/compressed look-alike (~0.75-0.95): a genuinely UNIQUE
# moved target produces nothing else this high, so it is still kept; a repeated
# widget whose true instance dimmed just under the accept threshold does, so the
# lone crisp decoy is refused. A refusal here falls through to the ocr/geometry/
# grounder rungs (stronger POSITION evidence), never to a silent click.
AMBIGUITY_SUSPICION_SCORE = 0.7
# A neighbouring copy of a widget cannot overlap it, so a match shifted by less
# than this share of the widget's width (horizontally) or height (vertically)
# is the recorded widget itself, redrawn with jitter or a small reflow. Shifted
# farther, inside the locality radius, it may be the next row's copy.
SAME_SPOT_SHARE = 0.75
SAME_SPOT_MIN_PX = 6


def moved_off_spot(dx: float, dy: float, w: int, h: int) -> bool:
    """Whether a match ``(dx, dy)`` from the recorded spot could be another
    copy of a ``w`` x ``h`` widget rather than the widget itself."""
    return abs(dx) >= max(SAME_SPOT_MIN_PX, SAME_SPOT_SHARE * w) or abs(dy) >= max(
        SAME_SPOT_MIN_PX, SAME_SPOT_SHARE * h
    )


def _repeated_on_frame(
    screen: np.ndarray, template: np.ndarray, tw: int, th: int
) -> bool:
    """Whether two or more distinct places on the whole frame look like the
    template (at the matched size), i.e. the widget is repeated."""
    sh, sw = screen.shape[:2]
    if tw > sw or th > sh:
        return False
    scaled = (
        template
        if template.shape[1] == tw and template.shape[0] == th
        else imgops.resize_area(template, tw, th)
    )
    result = imgops.match_template_ccoeff_normed(screen, scaled)
    return len(_peaks_above(result, AMBIGUITY_SUSPICION_SCORE, tw, th)) >= 2


def _peaks_above(
    result: np.ndarray, threshold: float, tw: int, th: int
) -> list[tuple[float, int, int]]:
    """Greedy non-max-suppressed peaks ``(score, x, y)`` at/above ``threshold``.

    ``x``/``y`` are the match top-left in the result map's (haystack) coords.
    A template-sized-ish window is suppressed around each accepted peak so
    repeated UI structure yields ONE peak per instance; capped at
    :data:`MAX_PEAKS` to bound cost on pathological frames.
    """
    work = result.copy()
    sx = max(1, tw // 2)
    sy = max(1, th // 2)
    peaks: list[tuple[float, int, int]] = []
    while len(peaks) < MAX_PEAKS:
        _, max_val, _, max_loc = imgops.min_max_loc(work)
        if max_val < threshold:
            break
        x, y = int(max_loc[0]), int(max_loc[1])
        peaks.append((float(max_val), x, y))
        work[max(0, y - sy) : y + sy + 1, max(0, x - sx) : x + sx + 1] = -1.0
    return peaks


# A flat/near-flat edge crop has no discriminative structure and makes
# normalized correlation degenerate.  Refuse that evidence and let the
# caller's independent hash/semantic checks decide.
STRUCTURAL_MIN_EDGE_PIXELS = 16
DEFAULT_TEMPLATE_SCALES: tuple[float, ...] = (0.85, 1.0, 1.18)


def _find_template_arrays(
    screen: np.ndarray,
    template: np.ndarray,
    *,
    search_region: Region | None = None,
    scales: tuple[float, ...] = DEFAULT_TEMPLATE_SCALES,
    threshold: float = 0.82,
    prefer_near: Point | None = None,
) -> Match | None:
    """Run the shared multi-scale matcher over two single-channel arrays."""
    sh, sw = screen.shape[:2]

    off_x, off_y = 0, 0
    haystack = screen
    if search_region is not None:
        clamped = _clamp_region(search_region, sw, sh)
        if clamped is None:
            return None
        off_x, off_y, rw, rh = clamped
        haystack = screen[off_y : off_y + rh, off_x : off_x + rw]

    hh, hw = haystack.shape[:2]
    th0, tw0 = template.shape[:2]

    # score, x, y, w, h, result map of the winning scale
    best: tuple[float, int, int, int, int, np.ndarray] | None = None
    for scale in scales:
        tw = max(1, int(round(tw0 * scale)))
        th = max(1, int(round(th0 * scale)))
        if tw > hw or th > hh:
            continue  # template larger than search area at this scale
        if scale == 1.0:
            scaled = template
        else:
            scaled = imgops.resize_area(template, tw, th)
        result = imgops.match_template_ccoeff_normed(haystack, scaled)
        _, max_val, _, max_loc = imgops.min_max_loc(result)
        if best is None or max_val > best[0]:
            best = (float(max_val), max_loc[0], max_loc[1], tw, th, result)

    if best is None or best[0] < threshold:
        return None

    score, mx, my, mw, mh, result = best
    if prefer_near is not None:
        # Locality + uniqueness gate (see the constants block above). Enumerate
        # the spatially distinct peaks that clear ``threshold``, prefer the one
        # nearest the expected location, and REFUSE (None) an ambiguous frame
        # where >= 2 look-alikes clear threshold and none sits where expected.
        peaks = _peaks_above(result, threshold, mw, mh)
        # ``prefer_near`` is the recorded target ORIGIN (top-left) in screen
        # coords (the ladder passes ``anchor.region[0:2]``). Compare peak
        # centers against the EXPECTED center so the locality radius is measured
        # consistently -- adding ``mw//2, mh//2`` to only the peak side would
        # bias every true peak by ~half a template, pushing wider widgets past
        # the radius and spuriously tripping the uniqueness halt on unchanged
        # surfaces.
        px = prefer_near[0] - off_x + mw // 2
        py = prefer_near[1] - off_y + mh // 2
        radius = max(LOCALITY_MIN_PX, min(mw, mh))

        def _center_dist(peak: tuple[float, int, int]) -> float:
            return math.hypot(peak[1] + mw // 2 - px, peak[2] + mh // 2 - py)

        # A self-similar template (a plain bar, a divider, a row stripe) gives
        # a RIDGE of equal scores, and non-max suppression keeps an arbitrary
        # point on each ridge: the true ridge's kept peak can sit farther from
        # the expected spot than a look-alike ridge's, and the look-alike wins.
        # Also consider the best score within a few pixels of the expected
        # origin itself.
        ex, ey = px - mw // 2, py - mh // 2
        j = EXPECTED_SPOT_JITTER_PX
        y0, y1 = max(0, ey - j), min(result.shape[0], ey + j + 1)
        x0, x1 = max(0, ex - j), min(result.shape[1], ex + j + 1)
        if y0 < y1 and x0 < x1:
            _, spot_val, _, spot_loc = imgops.min_max_loc(result[y0:y1, x0:x1])
            if spot_val >= threshold:
                peaks = [*peaks, (float(spot_val), x0 + spot_loc[0], y0 + spot_loc[1])]
        near = [p for p in peaks if _center_dist(p) <= radius]
        if near:
            # A degraded/occluded TRUE target at the expected spot beats a
            # cleaner look-alike elsewhere.
            score, mx, my = min(near, key=_center_dist)
            # The locality radius is wider than the pitch of a dense list
            # (48 px against 30-40 px rows), so the nearest peak can be the
            # NEXT row's identical widget when the target itself moved away.
            # A peak off the recorded spot is kept only when nothing else on
            # the whole frame looks like the template.
            if moved_off_spot(
                mx + mw // 2 - px, my + mh // 2 - py, mw, mh
            ) and _repeated_on_frame(screen, template, mw, mh):
                return None
        elif len(peaks) >= 2:
            # Multiple look-alikes, none where expected: not uniquely present.
            return None
        elif search_region is not None:
            # LOCAL-rung tightening. ``search_region`` set means the caller is
            # confirming the target NEAR where it was recorded (the resolution
            # ladder's local ``template`` rung, seeded with the recorded region
            # padded by ``search_pad``). No peak sits near the expected spot, so
            # the single peak that cleared threshold is a NEIGHBORING repeated
            # widget the padded window happened to include (the target's own
            # cell is degraded/unpainted -- a partial render / latency frame).
            # Committing to a neighbor is the wrong-row/wrong-icon silent-wrong;
            # refuse and let the GLOBAL rung's full-frame uniqueness gate decide
            # (it halts when >= 2 identical widgets exist and none is where
            # expected). A legitimately MOVED unique target is unaffected: it
            # resolves on the global rung, which keeps a lone far peak.
            return None
        elif len(_peaks_above(result, AMBIGUITY_SUSPICION_SCORE, mw, mh)) >= 2:
            # GLOBAL rung, one peak clears threshold far from expected, but a
            # SECOND spatially-distinct location is elevated just BELOW threshold
            # -- a DEGRADED sibling of the same repeated widget (a look-alike the
            # perturbation blurred/occluded/compressed under the accept bar,
            # while one instance stayed crisp). The surface therefore has
            # repeated widgets and the lone crisp peak is NOT uniquely the
            # target; refuse rather than click it. A genuinely moved UNIQUE
            # target has no such second response (nothing else on the frame
            # resembles it), so it is kept by the branch below. This closes the
            # asymmetric-degradation silent-wrong (the true target dimmed just
            # under threshold while a decoy stayed sharp) without weakening the
            # moved-unique-target path.
            return None
        # else: search_region is None -> the GLOBAL rung, exactly one elevated
        # response anywhere on the frame: a legitimately moved UNIQUE target;
        # keep the arg-max match unchanged.

    rx, ry = off_x + mx, off_y + my
    region: Region = (rx, ry, mw, mh)
    point: Point = (rx + mw // 2, ry + mh // 2)
    return Match(point=point, region=region, confidence=score)


def find_template(
    screen_png: bytes,
    template_png: bytes,
    *,
    search_region: Region | None = None,
    scales: tuple[float, ...] = DEFAULT_TEMPLATE_SCALES,
    threshold: float = 0.82,
    prefer_near: Point | None = None,
) -> Match | None:
    """Locate a grayscale template crop via multi-scale matching.

    ``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED`` is run at each scale (the
    *template* is resized); the best-scoring location wins if it clears
    ``threshold``. Scales at which the resized template would not fit inside
    the search image are skipped.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        template_png: Template crop as PNG bytes.
        search_region: Optional ``(x, y, w, h)`` sub-region of the screen to
            search within (clamped to screen bounds). Returned coordinates
            are still full-screen coordinates.
        scales: Multiplicative scale factors applied to the template.
        threshold: Minimum ``TM_CCOEFF_NORMED`` score to accept a match.
        prefer_near: Optional expected match origin ``(x, y)`` in screen
            coordinates. Enables the LOCALITY + UNIQUENESS gate (see the
            ``LOCALITY_MIN_PX`` constants block): among the peaks that clear
            ``threshold``, the one nearest this point wins — so a degraded or
            occluded TRUE target at the expected spot is chosen over a cleaner
            look-alike elsewhere — and an AMBIGUOUS frame (>= 2 peaks clear
            threshold, none near the expected spot) returns ``None`` so the
            caller can fall through / halt instead of clicking an arbitrary
            look-alike. A single peak far from the expected point is treated as
            a legitimately moved unique target and is kept.

    Returns:
        The best :class:`Match` in screen coordinates, or ``None`` if no scale
        produced a score at or above ``threshold`` — or, when ``prefer_near``
        is set, if the target is not uniquely present where expected.
    """
    return _find_template_arrays(
        _decode_gray(screen_png),
        _decode_gray(template_png),
        search_region=search_region,
        scales=scales,
        threshold=threshold,
        prefer_near=prefer_near,
    )


# A crop that matches itself shifted by a quarter of its width or height at or
# above this correlation (a blank band, a rule, a repeating pattern) matches
# along that axis anywhere: it cannot tell one place from another. Identifier
# crops of real records measured at most ~0.65.
SELF_SIMILAR_SHIFT_SCORE = 0.9
# Below this grey-level spread a crop is flat.
FLAT_CROP_STD = 2.0


# --- Control outline (geometry corroboration) -------------------------------
# The landmark geometry rung predicts a click from a label's recorded offset.
# OCR can read a label together with the glyph beside it, which moves the
# label's centre and the prediction with it. The prediction is kept only when
# the recorded crop's outline is found once near it. The outline is the edge
# strength of the crop's border ring: it does not depend on light or dark
# theme, and a checked or unchecked fill in the centre does not change it.
#: Normalised correlation of the outline at or above which a place shows the
#: recorded control.
OUTLINE_SCORE = 0.9
#: Share of the crop's height and width, on each side, kept as the border ring.
OUTLINE_RING_SHARE = 0.25


def _edge_strength(gray: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(gray.astype(np.float32))
    return np.hypot(gx, gy)


def outline_matches_near(
    screen_png: bytes,
    template_png: bytes,
    origin: Point,
    radius: tuple[int, int],
) -> list[tuple[float, int, int]]:
    """Places near ``origin`` (the crop's expected top-left) where the crop's
    outline appears: ``(score, x, y)`` top-left corners at or above
    :data:`OUTLINE_SCORE`, one per place, best first."""
    try:
        screen = _edge_strength(_decode_gray(screen_png))
        template = _edge_strength(_decode_gray(template_png))
    except ValueError:
        return []  # an unreadable image shows no outline
    th, tw = template.shape
    mask = np.ones((th, tw), dtype=bool)
    r0, r1 = int(th * OUTLINE_RING_SHARE), int(np.ceil(th * (1 - OUTLINE_RING_SHARE)))
    c0, c1 = int(tw * OUTLINE_RING_SHARE), int(np.ceil(tw * (1 - OUTLINE_RING_SHARE)))
    mask[r0:r1, c0:c1] = False
    t = template[mask]
    t = t - t.mean()
    t_norm = float(np.sqrt((t * t).sum()))
    if t_norm < 1e-6:
        return []
    sh, sw = screen.shape
    found: list[tuple[float, int, int]] = []
    for y in range(
        max(0, origin[1] - radius[1]), min(sh - th, origin[1] + radius[1]) + 1
    ):
        for x in range(
            max(0, origin[0] - radius[0]), min(sw - tw, origin[0] + radius[0]) + 1
        ):
            w = screen[y : y + th, x : x + tw][mask]
            w = w - w.mean()
            w_norm = float(np.sqrt((w * w).sum()))
            if w_norm < 1e-6:
                continue
            score = float((w * t).sum()) / (w_norm * t_norm)
            if score >= OUTLINE_SCORE:
                found.append((score, x, y))
    found.sort(key=lambda f: -f[0])
    places: list[tuple[float, int, int]] = []
    for score, x, y in found:
        if all(abs(x - px) > tw // 2 or abs(y - py) > th // 2 for _s, px, py in places):
            places.append((score, x, y))
    return places


def crop_is_featureless(template_png: bytes) -> bool:
    """Whether a crop has too little structure to tell one place from another.

    True for a flat crop, and for one that matches itself shifted by a quarter
    of its width or height (a background band, a ruled line, a repeat), so a
    template match finds it at every offset along that axis.
    """
    crop = _decode_gray(template_png).astype(np.float64)
    if crop.size == 0 or float(crop.std()) < FLAT_CROP_STD:
        return True
    height, width = crop.shape[:2]

    def shifted_score(a: np.ndarray, b: np.ndarray) -> float:
        a = a - a.mean()
        b = b - b.mean()
        energy_a = float((a * a).sum())
        energy_b = float((b * b).sum())
        if energy_a == 0 and energy_b == 0:
            return 1.0  # both windows flat: nothing tells places apart
        if energy_a == 0 or energy_b == 0:
            # Content confined to one edge quarter (a short label in a wide
            # cell): the shifted windows are not alike.
            return 0.0
        return float((a * b).sum()) / math.sqrt(energy_a * energy_b)

    dx = max(1, width // 4)
    dy = max(1, height // 4)
    horizontal = shifted_score(crop[:, dx:], crop[:, :-dx]) if width > dx else 1.0
    vertical = shifted_score(crop[dy:, :], crop[:-dy, :]) if height > dy else 1.0
    return max(horizontal, vertical) >= SELF_SIMILAR_SHIFT_SCORE


def template_peaks(
    screen_png: bytes, template_png: bytes, *, threshold: float = 0.6
) -> Optional[list[tuple[int, int]]]:
    """Every spatially distinct place ``template`` matches, at its own scale.

    Returns the top-left ``(x, y)`` of each non-max-suppressed peak at or above
    ``threshold``, strongest first, for a caller that must know every place a
    crop could be (the pixel identity census). None when the frame holds at
    least :data:`MAX_PEAKS` peaks, so the list may be incomplete, or when the
    template does not fit the frame.
    """
    screen = _decode_gray(screen_png)
    template = _decode_gray(template_png)
    th, tw = template.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return None
    result = imgops.match_template_ccoeff_normed(screen, template)
    peaks = _peaks_above(result, threshold, tw, th)
    if len(peaks) >= MAX_PEAKS:
        return None
    return [(x, y) for _score, x, y in peaks]


def find_structural_template(
    screen_png: bytes,
    template_png: bytes,
    *,
    search_region: Region | None = None,
    scales: tuple[float, ...] = (0.85, 1.0, 1.18),
    threshold: float = 0.8,
    prefer_near: Point | None = None,
) -> Match | None:
    """Locate recorded structure while ignoring foreground/background palette.

    Both images are reduced to Canny edge maps before the same localized,
    multi-scale normalized-correlation matcher used by :func:`find_template`.
    A light/dark theme inversion therefore keeps borders and glyph geometry,
    while a modal, missing panel, or changed layout alters the edge map and
    remains a failed match. This is intended for ``REGION_STABLE`` outcome
    checks; target resolution continues to use the stricter grayscale matcher.
    """
    screen = cv2.Canny(_decode_gray(screen_png), 50, 150)
    template = cv2.Canny(_decode_gray(template_png), 50, 150)
    if int(np.count_nonzero(template)) < STRUCTURAL_MIN_EDGE_PIXELS:
        return None
    return _find_template_arrays(
        screen,
        template,
        search_region=search_region,
        scales=scales,
        threshold=threshold,
        prefer_near=prefer_near,
    )


# pixels_changed: threshold below which a per-pixel grayscale difference is
# attributed to encoder noise, and the minimum count of above-threshold
# pixels for the region to count as visibly changed. Headless screenshots of
# a static screen are byte-identical, so both bars are deliberately low —
# the check answers "did ANYTHING render?", not "what changed?".
CHANGE_THRESHOLD = 20
CHANGE_MIN_PIXELS = 4


def pixels_changed(
    before_png: bytes,
    after_png: bytes,
    *,
    region: Region | None = None,
    threshold: int = CHANGE_THRESHOLD,
    min_pixels: int = CHANGE_MIN_PIXELS,
) -> bool:
    """True when two frames differ visibly (optionally within ``region``).

    Used by the replayer's typed-input verification: any visible change in
    the field region distinguishes "keystrokes landed somewhere visible"
    from "keystrokes fell on a non-rendering target" (e.g. ``<body>`` after
    focus theft).

    Args:
        before_png: Frame before the action, PNG bytes.
        after_png: Frame after the action, PNG bytes.
        region: Optional (x, y, w, h) to restrict the comparison to.
        threshold: Per-pixel grayscale delta above which a pixel counts.
        min_pixels: How many counting pixels make the frames "changed".

    Returns:
        True when the frames differ visibly (mismatched dimensions count as
        changed).
    """
    before = _decode_gray(before_png)
    after = _decode_gray(after_png)
    if before.shape != after.shape:
        return True
    if region is not None:
        clamped = _clamp_region(region, before.shape[1], before.shape[0])
        if clamped is None:
            return False
        x, y, w, h = clamped
        before = before[y : y + h, x : x + w]
        after = after[y : y + h, x : x + w]
    diff = imgops.absdiff(before, after)
    return int(np.count_nonzero(diff > threshold)) >= min_pixels
