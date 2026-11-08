"""Replay a pixel-only workflow on a session that renders at another scale.

A remote desktop that switches from 100% to 125% display scaling, a HiDPI
Citrix client, or a zoomed canvas draws the same application with every
coordinate, widget and glyph multiplied by one factor. The recorded templates,
landmark offsets, identity regions and postcondition crops are in the
recorded geometry, so every pixel rung misses and the layout-drift check
stops the run at its first step.

This module estimates that factor once, from a recorded landmark set, and
then gives the runtime a view of the session in the recorded geometry:

1. At compile time, :func:`scale_landmark_set` keeps the text lines of each
   click step's frame that appear exactly once and are not parameter values,
   as salted hashes of their OCR-canonical text with their centres. No label
   text is stored. Older bundles fall back to the anchor landmark labels the
   compiler already keeps (:func:`anchor_landmarks`).
2. :func:`match_landmarks` reads the live frame once and pairs each recorded
   label with the one live line that reads the same; repeated or missing
   labels are skipped.
3. :func:`estimate_render_transform` fits ``live = scale * recorded +
   offset``. The fit must rest on at least :data:`MIN_LANDMARKS` labels that
   agree within :data:`RESIDUAL_TOLERANCE` of the frame, span the frame in
   both axes, scale both axes by the same factor, and leave the whole recorded
   frame inside the live frame. Otherwise there is no estimate and the run
   keeps its recorded geometry, where the layout-drift check stops it.
4. :func:`render_scaled_backend` resamples each live frame to the recorded
   frame size and maps every coordinate the runtime sends back to the live
   session. It exposes only the backend methods whose coordinates it can map;
   any other capability is hidden, so the runtime takes its conservative path
   rather than sending recorded-geometry coordinates to a scaled session.

Nothing downstream is relaxed. Template, OCR and geometry resolution, the
identity check, postconditions, effect verification and the per-step
layout-drift check all run unchanged on the normalized frame. If identity
cannot be confirmed there, the step halts exactly as it would at 100%.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import math
import statistics
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from openadapt_flow.vision.ocr import normalize_text

Point = tuple[int, int]
Region = tuple[int, int, int, int]
Pair = tuple[tuple[float, float], tuple[float, float]]

#: Fewest agreeing labels a scale estimate may rest on.
MIN_LANDMARKS = 3
#: Fraction of the located labels that must agree with the fit.
MIN_INLIER_FRACTION = 0.6
#: Allowed distance between a label's live position and the fitted position,
#: in recorded pixels, as a fraction of the recorded frame diagonal (about
#: 15 px at 1280x800).
RESIDUAL_TOLERANCE = 0.01
#: The agreeing labels must span at least this fraction of the recorded frame
#: in each axis, so neither axis's scale is extrapolated from a cluster.
MIN_SPAN_FRACTION = 0.15
#: Largest allowed relative difference between the two axes' scale factors.
#: Display scaling multiplies both axes by the same factor; a reflow doesn't.
MAX_AXIS_DISAGREEMENT = 0.03
#: Scales this module will normalize.
MIN_SCALE, MAX_SCALE = 0.5, 3.0
#: Relative agreement needed to snap a fit to the frame-size ratio.
SNAP_TOLERANCE = 0.01
#: A fit this close to the recorded geometry is the recorded geometry.
IDENTITY_SCALE_EPSILON = 0.02
IDENTITY_OFFSET_PX = 2.0
#: Minimum similarity for a live OCR line to count as an anchor landmark
#: label. The same floor the resolver uses for fuzzy landmarks.
LABEL_MIN_RATIO = 0.9
#: Two recorded positions of one label within this distance are one label.
SAME_LABEL_PX = 6
#: Compile-time limits for the hashed landmark set.
MIN_LABEL_CHARS = 4
MIN_LABEL_CONFIDENCE = 0.8
MAX_LABELS_PER_STEP = 32


@dataclass(frozen=True)
class RecordedLandmark:
    """A recorded label: plain text (anchor landmark) or salted hash."""

    point: tuple[float, float]
    text: Optional[str] = None
    digest: Optional[str] = None
    size: Optional[tuple[float, float]] = None


@dataclass(frozen=True)
class LandmarkMatch:
    """One recorded label and the one live line that reads the same."""

    recorded: tuple[float, float]
    live: tuple[float, float]
    recorded_size: Optional[tuple[float, float]] = None
    live_size: Optional[tuple[float, float]] = None


def _canonical(text: str) -> str:
    from openadapt_flow.runtime.identity import ocr_canonical, squash

    return ocr_canonical("".join(ch for ch in squash(text) if ch.isalnum()))


def _digest(salt_hex: str, text: str) -> str:
    from openadapt_flow.runtime.identity_template import _hash, _salt_bytes

    return _hash(_salt_bytes(salt_hex), _canonical(text))


def new_salt_hex() -> str:
    from openadapt_flow.runtime.identity_template import new_salt_hex as _new

    return _new()


def scale_landmark_set(
    lines: Sequence[Any],
    *,
    salt_hex: str,
    exclude_texts: Iterable[str] = (),
) -> list[list[Any]]:
    """``[[digest, cx, cy, w, h], ...]`` for the frame's unique text lines.

    A line qualifies when it has at least :data:`MIN_LABEL_CHARS` letters or
    digits, OCR read it with confidence at least
    :data:`MIN_LABEL_CONFIDENCE`, its canonical text appears once in the
    frame, and it contains no parameter value (``exclude_texts``).
    """
    excluded = [_canonical(t) for t in exclude_texts if _canonical(t)]
    keyed = []
    for line in lines:
        key = _canonical(line.text)
        if len(key) < MIN_LABEL_CHARS or line.confidence < MIN_LABEL_CONFIDENCE:
            continue
        if any(value in key for value in excluded):
            continue
        keyed.append((key, line))
    counts = Counter(key for key, _ in keyed)
    out = []
    for key, line in keyed:
        if counts[key] != 1:
            continue
        x, y, w, h = line.region
        out.append(
            [
                _digest(salt_hex, line.text),
                round(x + w / 2, 1),
                round(y + h / 2, 1),
                w,
                h,
            ]
        )
        if len(out) >= MAX_LABELS_PER_STEP:
            break
    return out


def hashed_landmarks(entries: Sequence[Sequence[Any]]) -> list[RecordedLandmark]:
    return [
        RecordedLandmark(
            point=(float(e[1]), float(e[2])),
            digest=str(e[0]),
            size=(float(e[3]), float(e[4])) if len(e) >= 5 else None,
        )
        for e in entries
    ]


def anchor_landmarks(steps: Sequence[Any]) -> list[RecordedLandmark]:
    """Unique, unmoved anchor landmark labels with their recorded positions.

    A landmark's recorded position is the anchor's click point minus its
    retained offset. A label recorded at different positions in different
    steps moved during the recording and is dropped.
    """
    seen: dict[str, list[tuple[float, float]]] = {}
    texts: dict[str, str] = {}
    for step in steps:
        anchor = getattr(step, "anchor", None)
        if anchor is None:
            continue
        cx, cy = anchor.click_point
        for landmark in anchor.landmarks:
            if landmark.dx_px is None or landmark.dy_px is None:
                continue
            key = normalize_text(landmark.ocr_text)
            if len(key) < 3:
                continue
            seen.setdefault(key, []).append(
                (float(cx - landmark.dx_px), float(cy - landmark.dy_px))
            )
            texts.setdefault(key, landmark.ocr_text)
    out: list[RecordedLandmark] = []
    for key, points in seen.items():
        x0, y0 = points[0]
        if any(math.hypot(x - x0, y - y0) > SAME_LABEL_PX for x, y in points[1:]):
            continue
        out.append(
            RecordedLandmark(
                point=(
                    statistics.fmean(p[0] for p in points),
                    statistics.fmean(p[1] for p in points),
                ),
                text=texts[key],
            )
        )
    return out


def match_landmarks(
    landmarks: Sequence[RecordedLandmark],
    live_lines: Sequence[Any],
    *,
    salt_hex: Optional[str] = None,
) -> list[LandmarkMatch]:
    """Pair each recorded label with the one live line that reads the same.

    Hashed labels match on the salted hash of their OCR-canonical text; anchor
    labels match fuzzily, as the resolver matches landmarks. A label read on
    no live line or on more than one is skipped.
    """
    by_digest: dict[str, list[Any]] = {}
    if salt_hex is not None:
        for line in live_lines:
            if len(_canonical(line.text)) < MIN_LABEL_CHARS:
                continue
            by_digest.setdefault(_digest(salt_hex, line.text), []).append(line)
    matches: list[LandmarkMatch] = []
    for landmark in landmarks:
        hits: list[Any] = []
        if landmark.digest is not None:
            hits = by_digest.get(landmark.digest, [])
        elif landmark.text is not None:
            target = normalize_text(landmark.text)
            hits = [
                line
                for line in live_lines
                if difflib.SequenceMatcher(
                    None, normalize_text(line.text), target
                ).ratio()
                >= LABEL_MIN_RATIO
            ]
        if len(hits) != 1:
            continue
        x, y, w, h = hits[0].region
        if any(
            math.hypot(
                landmark.point[0] - m.recorded[0], landmark.point[1] - m.recorded[1]
            )
            <= SAME_LABEL_PX
            for m in matches
        ):
            continue  # one label found twice, through both landmark sets
        matches.append(
            LandmarkMatch(
                recorded=landmark.point,
                live=(x + w / 2.0, y + h / 2.0),
                recorded_size=landmark.size,
                live_size=(float(w), float(h)),
            )
        )
    return matches


@dataclass(frozen=True)
class RenderTransform:
    """``live = scale * recorded + offset`` for one session."""

    scale: float
    offset: tuple[float, float]
    recorded_viewport: tuple[int, int]
    live_viewport: tuple[int, int]
    landmarks_used: int
    landmarks_located: int
    max_residual_px: float

    def to_live(self, x: float, y: float) -> Point:
        return (
            int(round(self.scale * x + self.offset[0])),
            int(round(self.scale * y + self.offset[1])),
        )

    def region_to_live(self, region: Sequence[int]) -> Region:
        x, y, w, h = (int(v) for v in region)
        x0, y0 = self.to_live(x, y)
        x1, y1 = self.to_live(x + w, y + h)
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def region_to_recorded(self, region: Sequence[int]) -> Region:
        x, y, w, h = (float(v) for v in region)
        s = self.scale
        x0 = (x - self.offset[0]) / s
        y0 = (y - self.offset[1]) / s
        return (
            int(round(x0)),
            int(round(y0)),
            max(1, int(round(w / s))),
            max(1, int(round(h / s))),
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "scale": round(self.scale, 4),
            "offset": [round(self.offset[0], 2), round(self.offset[1], 2)],
            "recorded_viewport": list(self.recorded_viewport),
            "live_viewport": list(self.live_viewport),
            "landmarks_used": self.landmarks_used,
            "landmarks_located": self.landmarks_located,
            "max_residual_px": round(self.max_residual_px, 2),
        }

    def normalize_png(self, png: bytes) -> bytes:
        """Resample a live frame into the recorded geometry."""
        from PIL import Image

        from openadapt_flow.vision.imgops import cv2

        with Image.open(io.BytesIO(png)) as img:
            rgb = np.asarray(img.convert("RGB"))
        lh, lw = rgb.shape[:2]
        if (lw, lh) != self.live_viewport:
            raise RenderScaleError(
                f"live frame is {lw}x{lh}; the scale estimate was made on "
                f"{self.live_viewport[0]}x{self.live_viewport[1]}"
            )
        rw, rh = self.recorded_viewport
        x0, y0 = self.offset
        ix0, iy0 = int(round(x0)), int(round(y0))
        ix1 = int(round(x0 + rw * self.scale))
        iy1 = int(round(y0 + rh * self.scale))
        # The estimate allows the recorded frame to overhang the live frame
        # by its residual tolerance; replicate the edge rather than stretch.
        pad = max(0, -ix0, -iy0, ix1 - lw, iy1 - lh)
        if pad:
            rgb = cv2.copyMakeBorder(rgb, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        crop = rgb[iy0 + pad : iy1 + pad, ix0 + pad : ix1 + pad]
        interpolation = cv2.INTER_AREA if self.scale >= 1.0 else cv2.INTER_CUBIC
        out = cv2.resize(crop, (rw, rh), interpolation=interpolation)
        buffer = io.BytesIO()
        Image.fromarray(out, "RGB").save(buffer, format="PNG")
        return buffer.getvalue()


class RenderScaleError(RuntimeError):
    """The session no longer matches the scale estimate; refuse to act."""


def _fit(pairs: Sequence[Pair]) -> tuple[float, float, float]:
    """Least-squares uniform scale and offset for ``live = s * rec + t``."""
    rec = np.array([p[0] for p in pairs], dtype=float)
    live = np.array([p[1] for p in pairs], dtype=float)
    dr = rec - rec.mean(axis=0)
    dl = live - live.mean(axis=0)
    denom = float((dr**2).sum())
    if denom <= 0:
        return 1.0, 0.0, 0.0
    s = float((dr * dl).sum()) / denom
    t = live.mean(axis=0) - s * rec.mean(axis=0)
    return s, float(t[0]), float(t[1])


def _axis_scale(rec: np.ndarray, live: np.ndarray) -> Optional[float]:
    d = rec - rec.mean()
    denom = float((d**2).sum())
    if denom <= 0:
        return None
    return float((d * (live - live.mean())).sum()) / denom


def estimate_render_transform(
    matches: Sequence[LandmarkMatch],
    *,
    recorded_viewport: tuple[int, int],
    live_viewport: tuple[int, int],
) -> tuple[Optional[RenderTransform], str]:
    """Fit the session's render transform, or explain why there is none.

    Returns ``(transform, reason)``. ``transform`` is None when the matches do
    not establish one scale, and ``reason`` names the requirement that
    failed. A fit equal to the recorded geometry also returns None, with
    reason ``"recorded geometry"``.

    When the fit agrees with the ratio of live to recorded frame size and
    places the recorded frame at the origin, it is snapped to that ratio:
    a whole-session scale change, which is the common case.
    """
    pairs: list[Pair] = [(m.recorded, m.live) for m in matches]
    located = len(pairs)
    if located < MIN_LANDMARKS:
        return None, f"located {located} recorded labels, need {MIN_LANDMARKS}"
    rw, rh = recorded_viewport
    tolerance = RESIDUAL_TOLERANCE * math.hypot(rw, rh)

    # Robust start: the median of pairwise distance ratios, then the median
    # offset at that scale, so one moved or misread label cannot steer it.
    ratios = []
    for i in range(located):
        for j in range(i + 1, located):
            (ra, la), (rb, lb) = pairs[i], pairs[j]
            dr = math.hypot(ra[0] - rb[0], ra[1] - rb[1])
            if dr >= MIN_SPAN_FRACTION * min(rw, rh):
                ratios.append(math.hypot(la[0] - lb[0], la[1] - lb[1]) / dr)
    if not ratios:
        return None, "recorded labels are too close together to measure scale"
    s0 = statistics.median(ratios)
    tx0 = statistics.median(lv[0] - s0 * r[0] for r, lv in pairs)
    ty0 = statistics.median(lv[1] - s0 * r[1] for r, lv in pairs)

    def residual(pair: Pair, s: float, tx: float, ty: float) -> float:
        r, lv = pair
        return math.hypot(lv[0] - (s * r[0] + tx), lv[1] - (s * r[1] + ty)) / max(
            s, 1e-6
        )

    inliers = [p for p in pairs if residual(p, s0, tx0, ty0) <= tolerance]
    if len(inliers) < MIN_LANDMARKS or len(inliers) < MIN_INLIER_FRACTION * located:
        return None, f"{len(inliers)} of {located} labels agree on one scale"
    s, tx, ty = _fit(inliers)
    max_res = max(residual(p, s, tx, ty) for p in inliers)
    if max_res > tolerance:
        return None, f"labels disagree by {max_res:.1f}px after the fit"

    rec = np.array([p[0] for p in inliers], dtype=float)
    live = np.array([p[1] for p in inliers], dtype=float)
    span_x = float(rec[:, 0].max() - rec[:, 0].min())
    span_y = float(rec[:, 1].max() - rec[:, 1].min())
    if span_x < MIN_SPAN_FRACTION * rw or span_y < MIN_SPAN_FRACTION * rh:
        return None, (
            f"agreeing labels span {span_x:.0f}x{span_y:.0f}px; need "
            f"{MIN_SPAN_FRACTION:.0%} of the frame in each axis"
        )
    sx = _axis_scale(rec[:, 0], live[:, 0])
    sy = _axis_scale(rec[:, 1], live[:, 1])
    if sx is None or sy is None or abs(sx - sy) > MAX_AXIS_DISAGREEMENT * s:
        return None, "the axes scale differently: not a display-scale change"
    if not (MIN_SCALE <= s <= MAX_SCALE):
        return None, f"scale {s:.3f} is outside {MIN_SCALE}-{MAX_SCALE}"
    lw, lh = live_viewport
    ratio_x, ratio_y = lw / rw, lh / rh
    if (
        abs(ratio_x - ratio_y) <= SNAP_TOLERANCE * ratio_x
        and abs(s - ratio_x) <= SNAP_TOLERANCE * ratio_x
        and math.hypot(tx, ty) <= tolerance * s
    ):
        snapped = (ratio_x + ratio_y) / 2.0
        snapped_res = max(residual(p, snapped, 0.0, 0.0) for p in inliers)
        if snapped_res <= tolerance:
            s, tx, ty, max_res = snapped, 0.0, 0.0, snapped_res
    slack = tolerance * s
    if (
        tx < -slack
        or ty < -slack
        or tx + s * rw > lw + slack
        or ty + s * rh > lh + slack
    ):
        return None, "the recorded frame does not fit inside the live frame"
    if (
        abs(s - 1.0) <= IDENTITY_SCALE_EPSILON
        and math.hypot(tx, ty) <= IDENTITY_OFFSET_PX
    ):
        return None, "recorded geometry"
    return (
        RenderTransform(
            scale=s,
            offset=(tx, ty),
            recorded_viewport=(int(rw), int(rh)),
            live_viewport=(int(lw), int(lh)),
            landmarks_used=len(inliers),
            landmarks_located=located,
            max_residual_px=max_res,
        ),
        "estimated",
    )


def check_normalized_frame(
    entries: Sequence[Sequence[Any]],
    live_lines: Sequence[Any],
    *,
    salt_hex: str,
    text_height_ratio: float,
    landmark_px: float,
) -> Optional[list[str]]:
    """Compare a normalized frame with one step's recorded labels.

    Returns None when the step has too few recorded labels with sizes to
    judge (the caller falls back to the whole-frame fingerprint check), an
    empty list when the frame matches, and drift details otherwise.

    The frame matches when at least :data:`MIN_LANDMARKS` recorded labels,
    and :data:`MIN_INLIER_FRACTION` of those read, sit within
    ``landmark_px`` of their recorded centres, and the same labels are as wide
    as they were recorded, within ``text_height_ratio``. The same text set in
    a larger size or another face is wider or narrower, so width carries the
    font test. OCR box heights are not used: they move in steps of 2-3 px
    (about 10%) between renders of the same text, while widths of the same
    text stay within about 1%.
    """
    landmarks = [
        landmark for landmark in hashed_landmarks(entries) if landmark.size is not None
    ]
    if len(landmarks) < MIN_LANDMARKS:
        return None
    matches = match_landmarks(landmarks, live_lines, salt_hex=salt_hex)
    if len(matches) < MIN_LANDMARKS:
        return [f"read {len(matches)} of {len(landmarks)} recorded labels"]
    placed = [
        m
        for m in matches
        if math.hypot(m.live[0] - m.recorded[0], m.live[1] - m.recorded[1])
        <= landmark_px
    ]
    details: list[str] = []
    if len(placed) < MIN_LANDMARKS or len(placed) < MIN_INLIER_FRACTION * len(matches):
        details.append(
            f"{len(placed)} of {len(matches)} labels are where they were recorded"
        )
        return details
    widths = [
        m.live_size[0] / m.recorded_size[0]
        for m in placed
        if m.recorded_size and m.live_size and m.recorded_size[0] > 0
    ]
    if widths:
        width = statistics.median(widths)
        if abs(width - 1.0) > text_height_ratio:
            details.append(f"text is {width:.2f}x the recorded width")
    return details


class RenderScaledBackend:
    """The live session, seen in the recorded geometry.

    Build one with :func:`render_scaled_backend`. Frames are resampled to the
    recorded size and coordinates are mapped back to the session. A guarded
    action names the normalized frame the runtime verified; the wrapper
    forwards the hash of the live frame that produced it, so the inner
    backend's exact-frame lease still applies, and it refuses a frame it did
    not produce.
    """

    _FRAME_MEMORY = 64

    def __init__(self, inner: Any, transform: RenderTransform) -> None:
        self._inner = inner
        self._transform = transform
        self._raw_sha: "OrderedDict[str, str]" = OrderedDict()

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def transform(self) -> RenderTransform:
        return self._transform

    @property
    def viewport(self) -> tuple[int, int]:
        return self._transform.recorded_viewport

    def normalize(self, png: bytes) -> bytes:
        """Normalize one live frame and remember which live frame it was.

        OCR of the normalized frame reads the live frame (see
        :func:`openadapt_flow.vision.ocr.register_frame_source`): resampling
        merges the gaps between words, and the identity check reads words.
        """
        from openadapt_flow.vision.ocr import register_frame_source

        out = self._transform.normalize_png(png)
        self._raw_sha[hashlib.sha256(out).hexdigest()] = hashlib.sha256(png).hexdigest()
        while len(self._raw_sha) > self._FRAME_MEMORY:
            self._raw_sha.popitem(last=False)
        register_frame_source(
            out,
            png,
            to_source=self._transform.region_to_live,
            from_source=self._transform.region_to_recorded,
            derived_size=self._transform.recorded_viewport,
        )
        return out

    def release_frame_sources(self) -> None:
        """Stop OCR of this view's frames from reading the live frames."""
        from openadapt_flow.vision.ocr import forget_frame_sources

        forget_frame_sources(list(self._raw_sha))

    def _raw_hash(self, expected_frame_sha256: str) -> str:
        raw = self._raw_sha.get(expected_frame_sha256)
        if raw is None:
            raise RenderScaleError(
                "guarded action names a frame the scaled view did not produce"
            )
        return raw

    def screenshot(self) -> bytes:
        return self.normalize(self._inner.screenshot())

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self._inner.click(*self._transform.to_live(x, y), double=double)

    def scroll(self, dx: int, dy: int) -> None:
        s = self._transform.scale
        self._inner.scroll(int(round(dx * s)), int(round(dy * s)))

    def type_text(self, text: str) -> None:
        self._inner.type_text(text)

    def press(self, key: str) -> None:
        self._inner.press(key)


def _map_hash(self: RenderScaledBackend, kwargs: dict[str, Any]) -> None:
    if "expected_frame_sha256" in kwargs:
        kwargs["expected_frame_sha256"] = self._raw_hash(
            kwargs["expected_frame_sha256"]
        )
    if kwargs.get("target_point") is not None:
        kwargs["target_point"] = self._transform.to_live(*kwargs["target_point"])


def _point_method(name: str) -> Callable[..., Any]:
    def method(
        self: RenderScaledBackend, x: int, y: int, *args: Any, **kwargs: Any
    ) -> Any:
        _map_hash(self, kwargs)
        return getattr(self._inner, name)(
            *self._transform.to_live(x, y), *args, **kwargs
        )

    return method


def _drag_method(name: str) -> Callable[..., Any]:
    def method(
        self: RenderScaledBackend,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        _map_hash(self, kwargs)
        start = self._transform.to_live(x, y)
        end = self._transform.to_live(end_x, end_y)
        return getattr(self._inner, name)(*start, *end, *args, **kwargs)

    return method


def _frame_method(name: str) -> Callable[..., bytes]:
    def method(self: RenderScaledBackend, *args: Any, **kwargs: Any) -> bytes:
        return self.normalize(getattr(self._inner, name)(*args, **kwargs))

    return method


def _hash_method(name: str) -> Callable[..., Any]:
    def method(self: RenderScaledBackend, *args: Any, **kwargs: Any) -> Any:
        _map_hash(self, kwargs)
        return getattr(self._inner, name)(*args, **kwargs)

    return method


def _regions_method(name: str) -> Callable[..., Any]:
    def method(
        self: RenderScaledBackend,
        *,
        protected_regions: tuple[tuple[int, int, int, int], ...],
    ) -> Any:
        return getattr(self._inner, name)(
            protected_regions=tuple(
                self._transform.region_to_live(region) for region in protected_regions
            )
        )

    return method


def _dialog_method(name: str) -> Callable[..., Any]:
    """Probe the live dialog at the live point; return its box in the view."""

    def method(self: RenderScaledBackend, x: int, y: int) -> Any:
        status, box = getattr(self._inner, name)(*self._transform.to_live(x, y))
        if status != "dialog" or box is None:
            return status, None
        return status, self._transform.region_to_recorded(box)

    return method


def _passthrough(name: str) -> property:
    return property(lambda self: getattr(self._inner, name))


#: Inner-backend members the scaled view carries, and how. Anything else the
#: inner backend offers (structural location, DOM element leases, readback at
#: a point) is not exposed while the session is scaled.
_MEMBERS: dict[str, Callable[[str], Any]] = {
    "right_click": _point_method,
    "click_guarded": _point_method,
    "right_click_guarded": _point_method,
    "arm_guarded_coordinate": _point_method,
    "act_guarded_coordinate": _point_method,
    "arm_guarded_keyboard": _point_method,
    "prepare_pointer_actuation": _point_method,
    "hover": _point_method,
    "drag": _drag_method,
    "drag_guarded": _drag_method,
    "acquire_actuation_frame": _frame_method,
    "guarded_keyboard_frame": _frame_method,
    "wait_first_frame": _frame_method,
    "press_guarded": _hash_method,
    "type_text_guarded": _hash_method,
    "select_option": _hash_method,
    "select_option_guarded": _hash_method,
    "arm_remote_frame_contract": _regions_method,
    "dialog_region_at": _dialog_method,
    "take_dialogs": _passthrough,
    "arm_dialog": _passthrough,
    "transport": _passthrough,
    "set_qualification_input_guard": _passthrough,
    "qualification_environment_identity": _passthrough,
    "application_identity": _passthrough,
    "application_version_identity": _passthrough,
    "workflow_state_identity": _passthrough,
    "session_identity": _passthrough,
    "reset_fresh_actuation_state": _passthrough,
    "cancel_guarded_coordinate": _passthrough,
    "cancel_guarded_keyboard": _passthrough,
    "close": _passthrough,
}


def render_scaled_backend(
    inner: Any, transform: RenderTransform
) -> RenderScaledBackend:
    """A :class:`RenderScaledBackend` exposing what ``inner`` supports.

    Members are real class attributes, not ``__getattr__`` lookups, because
    runtime-checkable protocol checks read attributes statically.
    """
    members = {
        name: factory(name)
        for name, factory in _MEMBERS.items()
        if hasattr(inner, name)
    }
    cls = type(f"RenderScaled{type(inner).__name__}", (RenderScaledBackend,), members)
    return cls(inner, transform)
