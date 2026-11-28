"""Scale-normalized surrounding OCR-word constellation grounder (Arm B).

Records the spatial constellation of nearby OCR word tokens around a
demonstrated target in coordinates normalized by the frame's median OCR token
height. Normalizing by median token height makes the recorded spatial layout
invariant to display DPI scaling (75% to 200%), window translation, and
dark/light theme changes.

At replay time the grounder handles two cases where pixel templates and
label-only OCR fail or abstain:

1. **Duplicate look-alike targets** (for example, multiple "Date" fields under
   "Subscriber" vs "Patient", or multiple "Delete" buttons in a table): scores
   each candidate box against the recorded surrounding word constellation and
   picks the winner only when it clears both a minimum score and a strict tie
   margin over the runner-up.
2. **Blank input fields and text-less targets**: back-projects candidate target
   positions from matched surrounding OCR words using the recorded
   scale-normalized offsets, clusters votes into distinct hypotheses, and
   applies the same minimum-score and tie-margin safety gates.

Safety rule: when the best candidate scores below ``min_score``, or when the
top two distinct candidates fall within ``tie_margin``, the grounder returns
``None`` (abstain so the caller halts safely). It never guesses on a tie.
"""

from __future__ import annotations

import difflib
import io
import math
import re
import statistics
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel

from openadapt_flow.ir import Point, Region
from openadapt_flow.runtime.grounder import GrounderMatch

#: Minimum weighted constellation score in [0, 1] required to accept a match.
DEFAULT_MIN_SCORE = 0.45
#: Minimum gap between the top two distinct candidates. Smaller gaps abstain.
DEFAULT_TIE_MARGIN = 0.12
#: Base Gaussian spatial decay width in units of median token height.
DEFAULT_SIGMA_NORM = 1.25
#: Maximum spatial error (in median token heights) before a token scores 0.
DEFAULT_MAX_ERR_NORM = 3.5
#: Maximum radius (in median token heights) around the target when capturing.
DEFAULT_MAX_RADIUS_NORM = 18.0
#: Maximum surrounding tokens stored per target constellation.
DEFAULT_MAX_TOKENS = 24
#: Minimum fuzzy ratio for alphabetic tokens of length >= 4.
_FUZZY_MIN_RATIO = 0.85
#: Overlap threshold for suppressing duplicate proposals of the same control.
_OVERLAP_MERGE_THRESHOLD = 0.25

#: Maximum horizontal gap (in median token heights) from the target's left edge
#: to its immediate left-hand label token.
_IMMEDIATE_LEFT_MAX_GAP_NORM = 3.5
#: Maximum vertical gap (in median token heights) above the target's top edge
#: to a stacked immediate label token.
_IMMEDIATE_ABOVE_MAX_GAP_NORM = 2.5
#: Maximum inter-word gap (in median token heights) within one label phrase.
_IMMEDIATE_PHRASE_GAP_NORM = 2.0
#: Maximum word count chained into one immediate label phrase.
_IMMEDIATE_MAX_WORDS = 4
#: Minimum average score across recorded immediate label tokens.
_IMMEDIATE_MIN_SCORE = 0.45
#: Tight spatial error limits (in median token heights) for immediate labels.
_IMMEDIATE_MAX_DX_ERR_NORM = 2.25
_IMMEDIATE_MAX_DY_ERR_NORM = 1.25

#: Same-row horizontal band geometry and weighting for table/list row actions.
_ROW_BAND_DY_NORM = 0.9
_ROW_BAND_MAX_RADIUS_NORM = 28.0
_ROW_BAND_SAME_BOOST = 3.0
_ROW_BAND_OTHER_DECAY = 0.25
_ROW_BAND_SIBLING_MIN_DY_NORM = 1.1
_ROW_BAND_SIBLING_MAX_DY_NORM = 5.5

#: Static-context pruning thresholds and fallback radius for ephemeral tables.
_MIN_STATIC_TOKENS = 4
_STATIC_FALLBACK_RADIUS_NORM = 32.0

#: Checkbox/radio/bullet OCR glyph patterns stripped around alphabetic labels.
_WIDGET_BRACKET_RE = re.compile(
    r"^(?:\[\s*[xXvV\u2713\u2714\u2022*]?\s*\]|\(\s*[xXvV\u2022*oO0]?\s*\))$"
)
_WIDGET_LEADING_BRACKET_RE = re.compile(
    r"^\s*(?:\[\s*[xXvV\u2713\u2714\u2022*]?\s*\]|\(\s*[xXvV\u2022*oO0]?\s*\))\s*"
)
_WIDGET_TRAILING_BRACKET_RE = re.compile(
    r"\s*(?:\[\s*[xXvV\u2713\u2714\u2022*]?\s*\]|\(\s*[xXvV\u2022*oO0]?\s*\))\s*$"
)
_WIDGET_SINGLE_GLYPH_RE = re.compile(
    r"^[oO0vV<>\u2022\u25a1\u25a0\u25cb\u25cf\u2610\u2611\u2713\u2714\u2717\u2715]$"
)
_WIDGET_LEADING_GLYPH_RE = re.compile(
    r"^\s*[oO0vV<>\u2022\u25a1\u25a0\u25cb\u25cf\u2610\u2611\u2713\u2714\u2717\u2715]"
    r"\s+(?=[A-Za-z]{2})"
)
_WIDGET_TRAILING_GLYPH_RE = re.compile(
    r"(?<=[A-Za-z]{2})\s+"
    r"[oO0vV<>\u2022\u25a1\u25a0\u25cb\u25cf\u2610\u2611\u2713\u2714\u2717\u2715]\s*$"
)

#: Ephemeral/dynamic value formats (dates, timestamps, counts, row IDs).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLASH_DATE_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
_CLOCK_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_PURE_INT_RE = re.compile(r"^\d+$")
_ROW_ID_VALUE_RE = re.compile(r"^(?:\d{4,}|[a-z]{2,5}[-_]\d{4,})$")

#: Recording thresholds identifying a blank/low-ink input field interior.
_BLANK_REC_MAX_EDGE_DENSITY = 0.03
_BLANK_REC_MAX_INK_DENSITY = 0.04
_BLANK_REC_MAX_LUMA_STD = 12.0

#: Live thresholds flagging a recorded blank input box as occluded by a popup.
_OCCLUDED_LIVE_MIN_EDGE_DENSITY = 0.07
_OCCLUDED_LIVE_MIN_INK_DENSITY = 0.08
_OCCLUDED_LIVE_MIN_LUMA_STD = 28.0
_OCCLUDED_LIVE_STD_INK_MIN = 0.12
_OCCLUDED_LIVE_CONTRAST_JUMP = 75.0
_OCCLUDED_LIVE_MIN_CONTRAST = 90.0

OcrFn = Callable[[bytes], Sequence[Any]]


class OcrToken(BaseModel):
    """One word-level OCR token in full-frame pixel coordinates."""

    text: str
    norm_text: str
    cx: float
    cy: float
    w: float
    h: float
    confidence: float = 1.0

    @property
    def region(self) -> Region:
        x0 = max(0, int(round(self.cx - self.w / 2.0)))
        y0 = max(0, int(round(self.cy - self.h / 2.0)))
        return (x0, y0, max(1, int(round(self.w))), max(1, int(round(self.h))))


class ConstellationToken(BaseModel):
    """A surrounding OCR word stored relative to the target center.

    ``dx_norm`` and ``dy_norm`` are measured in units of the recorded frame's
    median OCR token height: ``(token_center - target_center) / median_h``.
    """

    text: str
    norm_text: str
    dx_norm: float
    dy_norm: float
    weight: float
    is_immediate_label: bool = False


class TargetConstellation(BaseModel):
    """Scale-normalized surrounding OCR-word constellation for one target."""

    tokens: list[ConstellationToken]
    median_token_height: float
    target_w_norm: float
    target_h_norm: float
    label: Optional[str] = None
    intent: Optional[str] = None
    target_is_blank: bool = False
    target_edge_density: Optional[float] = None
    target_ink_density: Optional[float] = None
    target_luma_std: Optional[float] = None
    target_frame_contrast: Optional[float] = None
    row_band_mode: bool = False


class CandidateScore(BaseModel):
    """Constellation match result for a single candidate region."""

    region: Region
    point: Point
    score: float
    matched_tokens: int
    total_tokens: int


def strip_widget_glyph_noise(text: str) -> str:
    """Strip leading/trailing checkbox/radio OCR glyph noise from a label.

    Removes bracketed markers (``[ ]``, ``[x]``, ``( )``, ``(*)``) and 1-char
    glyph prefixes/suffixes (``o ``, ``O ``, ``0 ``, ``v ``, ``< ``, ``> ``,
    ``• ``) only when separated by whitespace from an alphabetic word of at
    least 2 letters. Genuine numeric or alphanumeric IDs (such as
    ``MRN-482910``, ``500612``, or ``5OO612``) are never altered.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""
    if _WIDGET_BRACKET_RE.match(cleaned):
        return ""
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _WIDGET_LEADING_BRACKET_RE.sub("", cleaned)
        cleaned = _WIDGET_TRAILING_BRACKET_RE.sub("", cleaned)
        cleaned = _WIDGET_LEADING_GLYPH_RE.sub("", cleaned)
        cleaned = _WIDGET_TRAILING_GLYPH_RE.sub("", cleaned)
        cleaned = cleaned.strip()
    return cleaned


def normalize_token_text(text: str) -> str:
    """Lowercase and strip widget glyph noise and surrounding punctuation."""
    stripped = strip_widget_glyph_noise(text)
    if not stripped:
        return ""
    cleaned = re.sub(r"^[^0-9a-zA-Z]+|[^0-9a-zA-Z]+$", "", stripped)
    return cleaned.lower()


def _trim_line_widget_glyph_matches(
    matches: list[re.Match[str]],
) -> list[re.Match[str]]:
    """Drop leading/trailing widget glyph matches while keeping line offsets."""
    trimmed = list(matches)
    changed = True
    while changed and trimmed:
        changed = False
        first = trimmed[0].group(0)
        if _WIDGET_BRACKET_RE.match(first):
            trimmed.pop(0)
            changed = True
            continue
        if len(trimmed) >= 2:
            pair = f"{first} {trimmed[1].group(0)}"
            if _WIDGET_BRACKET_RE.match(pair):
                trimmed = trimmed[2:]
                changed = True
                continue
            second = trimmed[1].group(0)
            if _WIDGET_SINGLE_GLYPH_RE.match(first) and re.match(
                r"^[A-Za-z]{2}", second
            ):
                trimmed.pop(0)
                changed = True
                continue

        if not trimmed:
            break
        last = trimmed[-1].group(0)
        if _WIDGET_BRACKET_RE.match(last):
            trimmed.pop()
            changed = True
            continue
        if len(trimmed) >= 2:
            pair = f"{trimmed[-2].group(0)} {last}"
            if _WIDGET_BRACKET_RE.match(pair):
                trimmed = trimmed[:-2]
                changed = True
                continue
            prev = trimmed[-2].group(0)
            if _WIDGET_SINGLE_GLYPH_RE.match(last) and re.search(r"[A-Za-z]{2}$", prev):
                trimmed.pop()
                changed = True
                continue
    return trimmed


def _default_ocr(screen_png: bytes) -> Sequence[Any]:
    from openadapt_flow.vision.ocr import ocr

    return ocr(screen_png)


def extract_ocr_tokens(
    screen_png: bytes,
    *,
    ocr_fn: Optional[OcrFn] = None,
) -> list[OcrToken]:
    """Run OCR on ``screen_png`` and split lines into word-level tokens.

    Never raises on undecodable bytes or OCR errors; returns an empty list so
    callers abstain safely.
    """
    runner = ocr_fn or _default_ocr
    try:
        lines = runner(screen_png)
    except Exception:  # noqa: BLE001 - safe abstention on OCR or decode error
        return []
    tokens: list[OcrToken] = []
    for line in lines:
        raw_text = getattr(line, "text", "") or ""
        region = getattr(line, "region", None)
        if not raw_text.strip() or region is None:
            continue
        if not strip_widget_glyph_noise(raw_text):
            continue
        x, y, w, h = (
            float(region[0]),
            float(region[1]),
            float(region[2]),
            float(region[3]),
        )
        if w <= 0 or h <= 0:
            continue
        conf = float(getattr(line, "confidence", 1.0) or 1.0)
        line_len = len(raw_text)
        word_matches = _trim_line_widget_glyph_matches(
            list(re.finditer(r"\S+", raw_text))
        )
        for match in word_matches:
            word = match.group(0)
            # RapidOCR frequently drops spaces between adjacent TitleCase words
            # at 1.0x scale (e.g. "SubscriberSection"). Split camelCase
            # boundaries while preserving alphanumeric codes and horizontal spans.
            parts = re.split(r"(?<=[a-z])(?=[A-Z])", word)
            cursor = match.start()
            sub_spans: list[tuple[int, int, str]] = []
            for part in parts:
                end = cursor + len(part)
                sub_spans.append((cursor, end, part))
                cursor = end
            for s_idx, e_idx, sub_word in sub_spans:
                norm = normalize_token_text(sub_word)
                if not norm:
                    continue
                start_frac = s_idx / line_len
                end_frac = e_idx / line_len
                wx0 = x + w * start_frac
                wx1 = x + w * end_frac
                ww = max(1.0, wx1 - wx0)
                tokens.append(
                    OcrToken(
                        text=sub_word,
                        norm_text=norm,
                        cx=(wx0 + wx1) / 2.0,
                        cy=y + h / 2.0,
                        w=ww,
                        h=h,
                        confidence=conf,
                    )
                )
    return tokens


def _median_token_height(tokens: Sequence[OcrToken]) -> float:
    heights = [t.h for t in tokens if t.h > 0]
    if not heights:
        return 16.0
    return float(statistics.median(heights))


def _point_in_region(px: float, py: float, region: Region, margin: float = 1.0) -> bool:
    x, y, w, h = region
    return (x - margin) <= px <= (x + w + margin) and (y - margin) <= py <= (
        y + h + margin
    )


def _region_overlap(first: Region, second: Region) -> float:
    """Intersection over the smaller box's area."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    if ix == 0 or iy == 0:
        return 0.0
    smaller = min(aw * ah, bw * bh)
    return float(ix * iy) / float(smaller) if smaller > 0 else 0.0


def _token_text_similarity(expected: str, observed: str) -> float:
    """Return text similarity in [0, 1] without collapsing numeric IDs."""
    if expected == observed:
        return 1.0
    if len(expected) < 4 or len(observed) < 4:
        return 0.0
    if any(ch.isdigit() for ch in expected) or any(ch.isdigit() for ch in observed):
        return 0.0
    ratio = difflib.SequenceMatcher(None, expected, observed).ratio()
    if ratio >= _FUZZY_MIN_RATIO:
        return 0.85 * ratio
    return 0.0


class _InteriorStats:
    __slots__ = ("edge_density", "ink_density", "luma_std", "frame_contrast")

    def __init__(
        self,
        edge_density: float,
        ink_density: float,
        luma_std: float,
        frame_contrast: float,
    ) -> None:
        self.edge_density = edge_density
        self.ink_density = ink_density
        self.luma_std = luma_std
        self.frame_contrast = frame_contrast


def _analyze_region_interior(
    screen_png: bytes,
    region: Region,
) -> Optional[_InteriorStats]:
    """Measure interior edge density, ink density, luminance std, and contrast.

    Returns ``None`` when ``screen_png`` is not a decodable image, Pillow is
    unavailable, or the inset interior is smaller than 4x4 pixels.
    """
    if not screen_png or len(screen_png) < 16:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(screen_png)) as raw:
            gray = raw.convert("L")
    except Exception:  # noqa: BLE001
        return None

    img_w, img_h = gray.size
    x, y, w, h = region
    inset_x = max(2, min(int(round(w * 0.15)), 10))
    inset_y = max(2, min(int(round(h * 0.22)), 10))
    ix0 = max(0, x + inset_x)
    iy0 = max(0, y + inset_y)
    ix1 = min(img_w, x + w - inset_x)
    iy1 = min(img_h, y + h - inset_y)
    iw = ix1 - ix0
    ih = iy1 - iy0
    if iw < 4 or ih < 4:
        return None

    crop = gray.crop((ix0, iy0, ix1, iy1))
    data = crop.tobytes()
    n = len(data)
    if n == 0:
        return None

    hist = crop.histogram()
    mean_luma = sum(val * count for val, count in enumerate(hist)) / float(n)
    var_luma = sum(
        ((val - mean_luma) ** 2) * count for val, count in enumerate(hist)
    ) / float(n)
    luma_std = math.sqrt(var_luma)

    half = n // 2
    acc = 0
    med_luma = 0
    for val, count in enumerate(hist):
        acc += count
        if acc > half:
            med_luma = val
            break

    ink_count = sum(
        count for val, count in enumerate(hist) if abs(val - med_luma) >= 24
    )
    ink_density = ink_count / float(n)

    edge_count = 0
    edge_total = (ih - 1) * (iw - 1)
    if edge_total > 0:
        for row in range(1, ih):
            row_off = row * iw
            prev_off = row_off - iw
            for col in range(1, iw):
                px = data[row_off + col]
                if (
                    abs(px - data[row_off + col - 1]) >= 24
                    or abs(px - data[prev_off + col]) >= 24
                ):
                    edge_count += 1
        edge_density = edge_count / float(edge_total)
    else:
        edge_density = 0.0

    thumb = gray.resize((64, 64))
    thist = thumb.histogram()
    thalf = (64 * 64) // 2
    tacc = 0
    frame_med = 0
    for val, count in enumerate(thist):
        tacc += count
        if tacc > thalf:
            frame_med = val
            break

    return _InteriorStats(
        edge_density=edge_density,
        ink_density=ink_density,
        luma_std=luma_std,
        frame_contrast=abs(float(med_luma) - float(frame_med)),
    )


def _is_blank_target_occluded(
    screen_png: bytes,
    region: Region,
    constellation: TargetConstellation,
) -> bool:
    """Return True when a recorded blank input field appears occluded."""
    if not constellation.target_is_blank:
        return False
    stats = _analyze_region_interior(screen_png, region)
    if stats is None:
        return False
    if (
        stats.edge_density >= _OCCLUDED_LIVE_MIN_EDGE_DENSITY
        and stats.ink_density >= _OCCLUDED_LIVE_MIN_INK_DENSITY
    ):
        return True
    if (
        stats.luma_std >= _OCCLUDED_LIVE_MIN_LUMA_STD
        and stats.ink_density >= _OCCLUDED_LIVE_STD_INK_MIN
    ):
        return True
    if (
        constellation.target_frame_contrast is not None
        and stats.frame_contrast >= _OCCLUDED_LIVE_MIN_CONTRAST
        and (stats.frame_contrast - constellation.target_frame_contrast)
        >= _OCCLUDED_LIVE_CONTRAST_JUMP
    ):
        return True
    return False


def _find_immediate_label_indices(
    outside_tokens: Sequence[OcrToken],
    target_region: Region,
    med_h: float,
    *,
    target_label: Optional[str] = None,
) -> set[int]:
    """Identify indices of immediate adjacent label token(s) for ``target_region``.

    Checks directly to the left of ``target_region`` first (standard horizontal
    form layout), then immediately above (stacked label layout). Chains
    contiguous words on the same line so multi-word field labels like
    ``"Date of Birth"`` are tracked as one immediate label phrase.
    """
    rx, ry, rw, rh = target_region
    x0, y0, x1, y1 = float(rx), float(ry), float(rx + rw), float(ry + rh)
    label_norm = normalize_token_text(target_label) if target_label else ""

    # 1. Immediate left-hand label (same horizontal band).
    left_candidates: list[tuple[float, int, OcrToken]] = []
    for idx, tok in enumerate(outside_tokens):
        if tok.cy < y0 - 0.6 * med_h or tok.cy > y1 + 0.6 * med_h:
            continue
        tok_right = tok.cx + tok.w / 2.0
        if tok.cx >= x0 or tok_right > x0 + 0.5 * med_h:
            continue
        gap_left = max(0.0, x0 - tok_right)
        if gap_left <= _IMMEDIATE_LEFT_MAX_GAP_NORM * med_h:
            left_candidates.append((gap_left, idx, tok))

    if left_candidates:
        left_candidates.sort(key=lambda item: item[0])
        _, anchor_idx, anchor = left_candidates[0]
        selected = {anchor_idx}
        cur_left = anchor.cx - anchor.w / 2.0
        same_line_left = [
            (idx, tok)
            for idx, tok in enumerate(outside_tokens)
            if idx != anchor_idx
            and abs(tok.cy - anchor.cy) <= 0.6 * med_h
            and (tok.cx + tok.w / 2.0) <= cur_left + 0.25 * med_h
        ]
        same_line_left.sort(key=lambda item: -(item[1].cx + item[1].w / 2.0))
        for idx, tok in same_line_left:
            if len(selected) >= _IMMEDIATE_MAX_WORDS:
                break
            tok_right = tok.cx + tok.w / 2.0
            if cur_left - tok_right <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
                selected.add(idx)
                cur_left = min(cur_left, tok.cx - tok.w / 2.0)
            else:
                break
        return selected

    # 2. Immediate above label (stacked label layout).
    # Skip stacked-above tokens that repeat the target's own label text: a
    # repeated "Delete" button or "Unassigned" dropdown directly above is a
    # sibling row control, not a stacked field label for this target.
    above_candidates: list[tuple[float, float, int, OcrToken]] = []
    target_cx = (x0 + x1) / 2.0
    for idx, tok in enumerate(outside_tokens):
        if label_norm and tok.norm_text == label_norm:
            continue
        tok_left = tok.cx - tok.w / 2.0
        tok_right = tok.cx + tok.w / 2.0
        tok_bottom = tok.cy + tok.h / 2.0
        if tok.cy >= y0 or tok_bottom > y0 + 0.5 * med_h:
            continue
        gap_above = max(0.0, y0 - tok_bottom)
        if gap_above > _IMMEDIATE_ABOVE_MAX_GAP_NORM * med_h:
            continue
        if tok_right < x0 - 1.5 * med_h or tok_left > x1 + 1.5 * med_h:
            continue
        above_candidates.append((gap_above, abs(tok.cx - target_cx), idx, tok))

    if above_candidates:
        above_candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, anchor_idx, anchor = above_candidates[0]
        selected = {anchor_idx}
        same_line = [
            (idx, tok)
            for idx, tok in enumerate(outside_tokens)
            if idx != anchor_idx
            and abs(tok.cy - anchor.cy) <= 0.6 * med_h
            and (tok.cx + tok.w / 2.0) >= x0 - 2.5 * med_h
            and (tok.cx - tok.w / 2.0) <= x1 + 2.5 * med_h
        ]
        # Chain leftward then rightward from anchor.
        cur_left = anchor.cx - anchor.w / 2.0
        for idx, tok in sorted(
            (item for item in same_line if item[1].cx < anchor.cx),
            key=lambda item: -(item[1].cx + item[1].w / 2.0),
        ):
            if len(selected) >= _IMMEDIATE_MAX_WORDS:
                break
            if cur_left - (tok.cx + tok.w / 2.0) <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
                selected.add(idx)
                cur_left = min(cur_left, tok.cx - tok.w / 2.0)
            else:
                break
        cur_right = anchor.cx + anchor.w / 2.0
        for idx, tok in sorted(
            (item for item in same_line if item[1].cx > anchor.cx),
            key=lambda item: item[1].cx - item[1].w / 2.0,
        ):
            if len(selected) >= _IMMEDIATE_MAX_WORDS:
                break
            if (tok.cx - tok.w / 2.0) - cur_right <= _IMMEDIATE_PHRASE_GAP_NORM * med_h:
                selected.add(idx)
                cur_right = max(cur_right, tok.cx + tok.w / 2.0)
            else:
                break
        return selected

    return set()


def _immediate_text_similarity(expected: str, observed: str) -> float:
    """Text similarity for immediate labels, tolerating glued OCR words.

    RapidOCR occasionally drops spaces between adjacent words across themes or
    JPEG compression (for example, ``"Visit type"`` -> ``"Visittype"``). Allows
    alphabetic-only substring matches (length >= 4, no digits) while keeping
    numeric IDs exact.
    """
    base = _token_text_similarity(expected, observed)
    if base > 0.0:
        return base
    if any(ch.isdigit() for ch in expected) or any(ch.isdigit() for ch in observed):
        return 0.0
    exp_alpha = re.sub(r"[^a-z]", "", expected)
    obs_alpha = re.sub(r"[^a-z]", "", observed)
    if min(len(exp_alpha), len(obs_alpha)) >= 4 and (
        exp_alpha in obs_alpha or obs_alpha in exp_alpha
    ):
        return 0.8
    return 0.0


def _immediate_labels_supported(
    cx: float,
    cy: float,
    live_tokens: Sequence[OcrToken],
    live_med_h: float,
    constellation: TargetConstellation,
    *,
    sigma_norm: float = DEFAULT_SIGMA_NORM,
) -> bool:
    """Verify recorded immediate adjacent label(s) exist near ``(cx, cy)``.

    Distant header or banner tokens must never outvote a missing or shifted
    immediate field label.
    """
    imm_tokens = [ct for ct in constellation.tokens if ct.is_immediate_label]
    if not imm_tokens:
        return True

    score_sum = 0.0
    matched = 0
    for ct in imm_tokens:
        ex = cx + ct.dx_norm * live_med_h
        ey = cy + ct.dy_norm * live_med_h
        max_dx_err = _IMMEDIATE_MAX_DX_ERR_NORM + 0.15 * abs(ct.dx_norm)
        max_dy_err = _IMMEDIATE_MAX_DY_ERR_NORM + 0.08 * abs(ct.dy_norm)
        eff_sigma_x = sigma_norm * (1.0 + 0.08 * abs(ct.dx_norm))
        eff_sigma_y = min(sigma_norm, 1.1) * (1.0 + 0.05 * abs(ct.dy_norm))
        best_token_score = 0.0
        for lt in live_tokens:
            text_sim = _immediate_text_similarity(ct.norm_text, lt.norm_text)
            if text_sim <= 0.0:
                continue
            dx_err = abs(lt.cx - ex) / live_med_h
            dy_err = abs(lt.cy - ey) / live_med_h
            if dx_err > max_dx_err or dy_err > max_dy_err:
                continue
            spatial = math.exp(
                -0.5 * ((dx_err / eff_sigma_x) ** 2 + (dy_err / eff_sigma_y) ** 2)
            )
            combined = text_sim * spatial
            if combined > best_token_score:
                best_token_score = combined
        if any(ch.isdigit() for ch in ct.norm_text) and best_token_score < 0.35:
            return False
        if best_token_score >= 0.35:
            matched += 1
        score_sum += best_token_score

    avg_score = score_sum / float(len(imm_tokens))
    min_matched = max(1, math.ceil(0.5 * len(imm_tokens)))
    return matched >= min_matched and avg_score >= _IMMEDIATE_MIN_SCORE


def _is_dynamic_value_token(norm_text: str) -> bool:
    """Return True for dates, clock times, and standalone integer counts."""
    return bool(
        _ISO_DATE_RE.match(norm_text)
        or _SLASH_DATE_RE.match(norm_text)
        or _CLOCK_TIME_RE.match(norm_text)
        or _PURE_INT_RE.match(norm_text)
    )


def _is_table_row_dynamic_cell(norm_text: str) -> bool:
    """Return True for date, time, or ID values typical of dynamic table rows."""
    return bool(
        _ISO_DATE_RE.match(norm_text)
        or _SLASH_DATE_RE.match(norm_text)
        or _CLOCK_TIME_RE.match(norm_text)
        or _ROW_ID_VALUE_RE.match(norm_text)
    )


def _tokenize_entity_values(entity_values: Sequence[str]) -> set[str]:
    """Normalize recorded dossier/entity strings into a set of word tokens."""
    out: set[str] = set()
    for val in entity_values:
        if not val:
            continue
        for match in re.finditer(r"\S+", val):
            for part in re.split(r"(?<=[a-z])(?=[A-Z])", match.group(0)):
                norm = normalize_token_text(part)
                if norm:
                    out.add(norm)
    return out


def _group_tokens_by_row(
    tokens: Sequence[OcrToken], med_h: float
) -> list[tuple[float, list[int]]]:
    """Cluster token indices into horizontal text rows."""
    band_px = _ROW_BAND_DY_NORM * med_h
    rows: list[tuple[float, list[int]]] = []
    for idx in sorted(range(len(tokens)), key=lambda i: tokens[i].cy):
        cy = tokens[idx].cy
        placed = False
        for r_idx, (row_cy, members) in enumerate(rows):
            if abs(cy - row_cy) <= band_px:
                members.append(idx)
                new_cy = sum(tokens[m].cy for m in members) / float(len(members))
                rows[r_idx] = (new_cy, members)
                placed = True
                break
        if not placed:
            rows.append((cy, [idx]))
    return rows


def _detect_row_band_side(
    outside_tokens: Sequence[OcrToken],
    tx: float,
    ty: float,
    med_h: float,
    *,
    target_is_blank: bool,
    max_row_radius_norm: float = _ROW_BAND_MAX_RADIUS_NORM,
) -> Optional[str]:
    """Detect whether a non-blank target sits on a repeating table/list row.

    Returns ``"left"`` when the target has same-row label tokens to its left
    and at least one vertically adjacent column-aligned sibling row within
    standard table row pitch.
    """
    if target_is_blank:
        return None
    left_same_row = [
        tok
        for tok in outside_tokens
        if abs(tok.cy - ty) <= _ROW_BAND_DY_NORM * med_h
        and -max_row_radius_norm <= (tok.cx - tx) / med_h <= -0.5
    ]
    if not left_same_row:
        return None
    align_tol = 1.5 * med_h
    for tok in outside_tokens:
        dy_norm = abs(tok.cy - ty) / med_h
        if not (
            _ROW_BAND_SIBLING_MIN_DY_NORM <= dy_norm <= _ROW_BAND_SIBLING_MAX_DY_NORM
        ):
            continue
        tok_left = tok.cx - tok.w / 2.0
        for row_tok in left_same_row:
            row_left = row_tok.cx - row_tok.w / 2.0
            if (
                abs(tok.cx - row_tok.cx) <= align_tol
                or abs(tok_left - row_left) <= align_tol
            ):
                return "left"
    return None


def _find_dynamic_token_indices(
    all_tokens: Sequence[OcrToken],
    *,
    ty: float,
    med_h: float,
    row_band_active: bool,
    exclude_dynamic_tokens: bool,
    entity_values: Optional[Sequence[str]],
) -> set[int]:
    """Identify indices of ephemeral/dynamic tokens to exclude at capture time.

    Protects the target's own horizontal band when ``row_band_active`` is True
    (row-action targets rely on their own row's patient name, MRN, or time).
    """
    dynamic: set[int] = set()
    same_row_band_px = _ROW_BAND_DY_NORM * med_h

    def _is_own_row(tok: OcrToken) -> bool:
        return abs(tok.cy - ty) <= same_row_band_px

    if entity_values:
        explicit_norms = _tokenize_entity_values(entity_values)
        for idx, tok in enumerate(all_tokens):
            if row_band_active and _is_own_row(tok):
                continue
            if tok.norm_text in explicit_norms:
                dynamic.add(idx)

    if not exclude_dynamic_tokens:
        return dynamic

    # 1. Direct dynamic value tokens (dates, clock times, standalone counts)
    # strictly outside the target's own horizontal band.
    for idx, tok in enumerate(all_tokens):
        if _is_own_row(tok):
            continue
        if _is_dynamic_value_token(tok.norm_text):
            dynamic.add(idx)

    # 2. Structural repeating data-table rows: >= 2 horizontal rows sharing a
    # column-aligned dynamic cell (date, time, or 4+ digit ID). Skip summary
    # or form header rows containing colon-terminated field labels (e.g. "Patient:").
    rows = _group_tokens_by_row(all_tokens, med_h)
    dyn_cells_by_row: list[tuple[int, float, list[OcrToken]]] = []
    for r_idx, (row_cy, members) in enumerate(rows):
        if any(all_tokens[m].text.strip().endswith(":") for m in members):
            continue
        cells = [
            all_tokens[m]
            for m in members
            if _is_table_row_dynamic_cell(all_tokens[m].norm_text)
        ]
        if cells:
            dyn_cells_by_row.append((r_idx, row_cy, cells))

    col_tol = 1.5 * med_h
    data_row_indices: set[int] = set()
    for i in range(len(dyn_cells_by_row)):
        ri, _, cells_i = dyn_cells_by_row[i]
        for j in range(i + 1, len(dyn_cells_by_row)):
            rj, _, cells_j = dyn_cells_by_row[j]
            if any(abs(ci.cx - cj.cx) <= col_tol for ci in cells_i for cj in cells_j):
                data_row_indices.add(ri)
                data_row_indices.add(rj)

    if data_row_indices:
        # Collect TitleCase entity words appearing inside detected data rows
        # (excluding colon-suffixed field labels like "Patient:").
        table_entity_norms: set[str] = set()
        for r_idx in data_row_indices:
            row_cy, members = rows[r_idx]
            is_target_row = abs(row_cy - ty) <= same_row_band_px
            for m in members:
                tok = all_tokens[m]
                if not is_target_row:
                    dynamic.add(m)
                raw = tok.text.strip()
                if (
                    len(tok.norm_text) >= 3
                    and raw
                    and raw[0].isupper()
                    and not raw.endswith(":")
                    and tok.norm_text.isalpha()
                ):
                    table_entity_norms.add(tok.norm_text)

        # Propagate detected table entity names onto summary/banner rows that
        # also carry a dynamic value (for example: "Patient: James Alvarez (1981-05-21)"),
        # leaving static labels ("Patient:", "Clinical", "note:") intact.
        if table_entity_norms:
            for r_idx, (row_cy, members) in enumerate(rows):
                if r_idx in data_row_indices:
                    continue
                if abs(row_cy - ty) <= same_row_band_px:
                    continue
                if not any(
                    _is_table_row_dynamic_cell(all_tokens[m].norm_text) for m in members
                ):
                    continue
                for m in members:
                    tok = all_tokens[m]
                    if (
                        tok.norm_text in table_entity_norms
                        and not tok.text.strip().endswith(":")
                    ):
                        dynamic.add(m)

    return dynamic


def capture_constellation(
    screen_png: bytes,
    target_region: Region,
    *,
    target_point: Optional[Point] = None,
    label: Optional[str] = None,
    intent: Optional[str] = None,
    max_radius_norm: float = DEFAULT_MAX_RADIUS_NORM,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    row_band_mode: bool | str = "auto",
    exclude_dynamic_tokens: bool = True,
    entity_values: Optional[Sequence[str]] = None,
    ocr_fn: Optional[OcrFn] = None,
) -> TargetConstellation:
    """Record nearby OCR words around ``target_region`` in scale-normalized units.

    Tokens whose centers fall inside ``target_region`` are excluded so the
    constellation captures surrounding context rather than self-matching a
    repeated button or field label. Immediate adjacent label tokens (left or
    above ``target_region``) are flagged so distant header tokens cannot
    outvote them at match time. When ``target_region`` is a blank/low-ink input
    box, its interior pixel statistics are recorded to detect live occlusion.

    When ``row_band_mode`` is ``True`` (or ``"auto"`` detects a repeating
    table/list row action), same-row horizontal tokens (``|dy| <= 0.9 * med_h``)
    receive higher weight and an extended horizontal capture radius so
    reordering rows preserves match score.

    When ``exclude_dynamic_tokens`` is ``True`` or ``entity_values`` are
    provided, ephemeral off-row tokens (dates, timestamps, counts, repeating
    table-body data rows, and explicit dossier values) are pruned so static
    controls do not depend on changing data rows.
    """
    if row_band_mode not in (True, False, "auto"):
        raise ValueError(
            f"row_band_mode must be True, False, or 'auto', got {row_band_mode!r}"
        )

    tokens = extract_ocr_tokens(screen_png, ocr_fn=ocr_fn)
    med_h = _median_token_height(tokens)
    tx, ty = (
        (float(target_point[0]), float(target_point[1]))
        if target_point is not None
        else (
            target_region[0] + target_region[2] / 2.0,
            target_region[1] + target_region[3] / 2.0,
        )
    )

    has_inner_tokens = False
    all_outside: list[tuple[int, float, OcrToken]] = []
    for tok_idx, tok in enumerate(tokens):
        if _point_in_region(tok.cx, tok.cy, target_region):
            has_inner_tokens = True
            continue
        dx_norm = (tok.cx - tx) / med_h
        dy_norm = (tok.cy - ty) / med_h
        dist_norm = math.hypot(dx_norm, dy_norm)
        all_outside.append((tok_idx, dist_norm, tok))

    target_is_blank = False
    target_edge_density: Optional[float] = None
    target_ink_density: Optional[float] = None
    target_luma_std: Optional[float] = None
    target_frame_contrast: Optional[float] = None
    target_w_norm = max(0.25, float(target_region[2]) / med_h)
    target_h_norm = max(0.25, float(target_region[3]) / med_h)
    # Blank-interior occlusion checks apply to wide text/form inputs, not tiny
    # square checkboxes or radio buttons whose interiors legitimately toggle ink.
    is_input_like_geometry = (
        target_w_norm >= 2.0 and target_region[2] >= target_region[3] * 1.4
    )
    if not has_inner_tokens and not (label or "").strip() and is_input_like_geometry:
        stats = _analyze_region_interior(screen_png, target_region)
        if stats is not None:
            target_edge_density = round(stats.edge_density, 5)
            target_ink_density = round(stats.ink_density, 5)
            target_luma_std = round(stats.luma_std, 3)
            target_frame_contrast = round(stats.frame_contrast, 3)
            if (
                stats.edge_density <= _BLANK_REC_MAX_EDGE_DENSITY
                and stats.ink_density <= _BLANK_REC_MAX_INK_DENSITY
                and stats.luma_std <= _BLANK_REC_MAX_LUMA_STD
            ):
                target_is_blank = True

    all_outside_tokens = [item[2] for item in all_outside]
    max_row_radius_norm = max(max_radius_norm, _ROW_BAND_MAX_RADIUS_NORM)
    if row_band_mode is True:
        row_side: Optional[str] = "both"
    elif row_band_mode is False:
        row_side = None
    else:
        row_side = _detect_row_band_side(
            all_outside_tokens,
            tx,
            ty,
            med_h,
            target_is_blank=target_is_blank,
            max_row_radius_norm=max_row_radius_norm,
        )
    row_band_active = row_side is not None

    def _is_same_row_band(tok: OcrToken) -> bool:
        if not row_band_active:
            return False
        if abs(tok.cy - ty) > _ROW_BAND_DY_NORM * med_h:
            return False
        if row_side == "left":
            return (tok.cx - tx) < 0.0
        return True

    def _in_capture_radius(dist_norm: float, tok: OcrToken, radius_norm: float) -> bool:
        if dist_norm <= radius_norm:
            return True
        if _is_same_row_band(tok) and dist_norm <= max(
            radius_norm, max_row_radius_norm
        ):
            return True
        return False

    initial_outside = [
        (tok_idx, dist_norm, tok)
        for tok_idx, dist_norm, tok in all_outside
        if _in_capture_radius(dist_norm, tok, max_radius_norm)
    ]
    initial_tokens = [item[2] for item in initial_outside]
    imm_local_indices = _find_immediate_label_indices(
        initial_tokens,
        target_region,
        med_h,
        target_label=label,
    )
    imm_global_indices = {initial_outside[i][0] for i in imm_local_indices}

    dynamic_indices = _find_dynamic_token_indices(
        tokens,
        ty=ty,
        med_h=med_h,
        row_band_active=row_band_active,
        exclude_dynamic_tokens=exclude_dynamic_tokens,
        entity_values=entity_values,
    )
    # Immediate labels are never pruned by automatic heuristics; explicit
    # entity_values still respect immediate labels to prevent unsafe drift.
    dynamic_indices -= imm_global_indices

    filtered = [item for item in initial_outside if item[0] not in dynamic_indices]
    pruned_any = len(filtered) < len(initial_outside)
    if pruned_any and len(filtered) < _MIN_STATIC_TOKENS:
        expanded_radius = max(max_radius_norm * 1.8, _STATIC_FALLBACK_RADIUS_NORM)
        expanded = [
            item
            for item in all_outside
            if item[0] not in dynamic_indices
            and _in_capture_radius(item[1], item[2], expanded_radius)
        ]
        if expanded:
            filtered = expanded
    if not filtered:
        # Never produce an empty constellation when unpruned context exists.
        filtered = initial_outside

    nearby: list[tuple[int, float, ConstellationToken]] = []
    for tok_idx, dist_norm, tok in filtered:
        dx_norm = (tok.cx - tx) / med_h
        dy_norm = (tok.cy - ty) / med_h
        weight = 1.0 / (1.0 + dist_norm / 6.0)
        is_imm = tok_idx in imm_global_indices
        same_row = _is_same_row_band(tok)
        if row_band_active:
            weight *= _ROW_BAND_SAME_BOOST if same_row else _ROW_BAND_OTHER_DECAY
        priority = 0 if is_imm else (1 if same_row else 2)
        nearby.append(
            (
                priority,
                dist_norm,
                ConstellationToken(
                    text=tok.text,
                    norm_text=tok.norm_text,
                    dx_norm=dx_norm,
                    dy_norm=dy_norm,
                    weight=weight,
                    is_immediate_label=is_imm,
                ),
            )
        )
    # Keep immediate label tokens first, same-row tokens second, then by proximity.
    nearby.sort(key=lambda item: (item[0], item[1]))
    kept = [item[2] for item in nearby[:max_tokens]]

    return TargetConstellation(
        tokens=kept,
        median_token_height=med_h,
        target_w_norm=target_w_norm,
        target_h_norm=target_h_norm,
        label=label,
        intent=intent,
        target_is_blank=target_is_blank,
        target_edge_density=target_edge_density,
        target_ink_density=target_ink_density,
        target_luma_std=target_luma_std,
        target_frame_contrast=target_frame_contrast,
        row_band_mode=row_band_active,
    )


def _score_point(
    cx: float,
    cy: float,
    region: Region,
    live_tokens: Sequence[OcrToken],
    live_med_h: float,
    constellation: TargetConstellation,
    *,
    sigma_norm: float = DEFAULT_SIGMA_NORM,
    max_err_norm: float = DEFAULT_MAX_ERR_NORM,
) -> CandidateScore:
    """Score one candidate center against the recorded constellation."""
    total_weight = sum(ct.weight for ct in constellation.tokens)
    if total_weight <= 0.0:
        return CandidateScore(
            region=region,
            point=(int(round(cx)), int(round(cy))),
            score=0.0,
            matched_tokens=0,
            total_tokens=0,
        )

    weighted_sum = 0.0
    matched_count = 0
    for ct in constellation.tokens:
        ex = cx + ct.dx_norm * live_med_h
        ey = cy + ct.dy_norm * live_med_h
        eff_sigma_x = sigma_norm * (1.0 + 0.08 * abs(ct.dx_norm))
        eff_max_dx = max_err_norm * (1.0 + 0.08 * abs(ct.dx_norm))
        if constellation.row_band_mode and abs(ct.dy_norm) <= _ROW_BAND_DY_NORM:
            # Keep vertical tolerance tight for same-row tokens so row labels
            # never grant credit to adjacent sibling rows above or below.
            eff_sigma_y = min(sigma_norm, 0.85)
            eff_max_dy = min(max_err_norm, 1.4)
        else:
            eff_sigma_y = sigma_norm * (1.0 + 0.08 * abs(ct.dy_norm))
            eff_max_dy = max_err_norm * (1.0 + 0.08 * abs(ct.dy_norm))
        best_token_score = 0.0
        for lt in live_tokens:
            text_sim = _token_text_similarity(ct.norm_text, lt.norm_text)
            if text_sim <= 0.0:
                continue
            dx_err = abs(lt.cx - ex) / live_med_h
            dy_err = abs(lt.cy - ey) / live_med_h
            if dx_err > eff_max_dx or dy_err > eff_max_dy:
                continue
            spatial = math.exp(
                -0.5 * ((dx_err / eff_sigma_x) ** 2 + (dy_err / eff_sigma_y) ** 2)
            )
            combined = text_sim * spatial
            if combined > best_token_score:
                best_token_score = combined
        if best_token_score > 0.0:
            matched_count += 1
            weighted_sum += ct.weight * best_token_score

    return CandidateScore(
        region=region,
        point=(int(round(cx)), int(round(cy))),
        score=weighted_sum / total_weight,
        matched_tokens=matched_count,
        total_tokens=len(constellation.tokens),
    )


def _dedupe_overlapping(scored: Sequence[CandidateScore]) -> list[CandidateScore]:
    """Keep the highest-scoring box among overlapping proposals of one control."""
    kept: list[CandidateScore] = []
    for cand in sorted(scored, key=lambda c: -c.score):
        if any(
            _region_overlap(cand.region, other.region) > _OVERLAP_MERGE_THRESHOLD
            for other in kept
        ):
            continue
        kept.append(cand)
    return kept


def disambiguate_candidates(
    screen_png: bytes,
    candidates: Sequence[Region],
    constellation: TargetConstellation,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    tie_margin: float = DEFAULT_TIE_MARGIN,
    sigma_norm: float = DEFAULT_SIGMA_NORM,
    max_err_norm: float = DEFAULT_MAX_ERR_NORM,
    merge_overlapping: bool = True,
    ocr_fn: Optional[OcrFn] = None,
    _live_tokens: Optional[Sequence[OcrToken]] = None,
    _candidate_unit_heights: Optional[Sequence[float]] = None,
) -> Optional[CandidateScore]:
    """Pick the single candidate matching ``constellation``, or abstain (None).

    Returns ``None`` whenever:
    - no candidates or constellation tokens are provided,
    - OCR produces no live tokens,
    - a candidate fails the recorded immediate adjacent label check,
    - the best candidate scores below ``min_score``,
    - the second-best distinct candidate is within ``tie_margin`` of the best, or
    - a recorded blank input box is occluded on the live frame.
    """
    if not candidates or not constellation.tokens:
        return None
    live_tokens = (
        list(_live_tokens)
        if _live_tokens is not None
        else extract_ocr_tokens(screen_png, ocr_fn=ocr_fn)
    )
    if not live_tokens:
        return None
    live_med_h = _median_token_height(live_tokens)

    scored: list[CandidateScore] = []
    for idx, region in enumerate(candidates):
        cx = region[0] + region[2] / 2.0
        cy = region[1] + region[3] / 2.0
        unit_h = (
            _candidate_unit_heights[idx]
            if _candidate_unit_heights is not None
            and idx < len(_candidate_unit_heights)
            else live_med_h
        )
        if not _immediate_labels_supported(
            cx,
            cy,
            live_tokens,
            unit_h,
            constellation,
            sigma_norm=sigma_norm,
        ):
            continue
        scored.append(
            _score_point(
                cx,
                cy,
                region,
                live_tokens,
                unit_h,
                constellation,
                sigma_norm=sigma_norm,
                max_err_norm=max_err_norm,
            )
        )
    if not scored:
        return None
    ranked = (
        _dedupe_overlapping(scored)
        if merge_overlapping
        else sorted(scored, key=lambda c: -c.score)
    )
    if not ranked:
        return None
    best = ranked[0]
    if best.score < min_score or best.matched_tokens < 1:
        return None
    if len(ranked) >= 2 and (best.score - ranked[1].score) < tie_margin:
        return None
    if _is_blank_target_occluded(screen_png, best.region, constellation):
        return None
    return best


def _label_candidate_regions(
    live_tokens: Sequence[OcrToken],
    label: str,
) -> list[Region]:
    """Find candidate boxes on the live frame matching ``label``."""
    words = [normalize_token_text(w) for w in re.split(r"\s+", label.strip())]
    words = [w for w in words if w]
    if not words:
        return []
    if len(words) == 1:
        needle = words[0]
        return [
            tok.region
            for tok in live_tokens
            if _token_text_similarity(needle, tok.norm_text) > 0.0
        ]
    # Multi-word label: match contiguous tokens on the same horizontal row.
    med_h = _median_token_height(live_tokens)
    ordered = sorted(live_tokens, key=lambda t: (round(t.cy / max(med_h, 1.0)), t.cx))
    regions: list[Region] = []
    span = len(words)
    for idx in range(len(ordered) - span + 1):
        group = ordered[idx : idx + span]
        if any(abs(g.cy - group[0].cy) > med_h * 0.9 for g in group[1:]):
            continue
        if all(
            _token_text_similarity(w, g.norm_text) > 0.0 for w, g in zip(words, group)
        ):
            x0 = min(g.cx - g.w / 2.0 for g in group)
            y0 = min(g.cy - g.h / 2.0 for g in group)
            x1 = max(g.cx + g.w / 2.0 for g in group)
            y1 = max(g.cy + g.h / 2.0 for g in group)
            regions.append(
                (
                    max(0, int(round(x0))),
                    max(0, int(round(y0))),
                    max(1, int(round(x1 - x0))),
                    max(1, int(round(y1 - y0))),
                )
            )
    return regions


def _refined_unit_height(
    pairs: Sequence[tuple[ConstellationToken, OcrToken, float]],
    fallback_med_h: float,
) -> float:
    """Refine the live pixel-per-unit-height scale from inter-token distances.

    OCR box heights are quantized to integers, which can introduce 3% to 6%
    scale error when projecting over wide layouts. Pairwise distances between
    matched constellation tokens recover sub-pixel scale accuracy when the
    layout scaled uniformly.
    """
    if len(pairs) < 2:
        return fallback_med_h
    ratios: list[float] = []
    # Deduplicate by constellation token index (keep highest-similarity live match)
    limit = min(len(pairs), 12)
    for i in range(limit):
        ct_i, lt_i, _ = pairs[i]
        for j in range(i + 1, limit):
            ct_j, lt_j, _ = pairs[j]
            rec_dist = math.hypot(
                ct_i.dx_norm - ct_j.dx_norm, ct_i.dy_norm - ct_j.dy_norm
            )
            if rec_dist < 2.0:
                continue
            live_dist = math.hypot(lt_i.cx - lt_j.cx, lt_i.cy - lt_j.cy)
            if live_dist <= 0.0:
                continue
            est = live_dist / rec_dist
            # Reject wild outliers from accidental text collisions across panels.
            if 0.6 * fallback_med_h <= est <= 1.6 * fallback_med_h:
                ratios.append(est)
    if not ratios:
        return fallback_med_h
    return float(statistics.median(ratios))


def _vote_candidate_regions_with_scale(
    live_tokens: Sequence[OcrToken],
    constellation: TargetConstellation,
    *,
    cluster_radius_norm: float = 1.5,
    max_spread_share: float = 0.35,
) -> list[tuple[Region, float]]:
    """Generate ``(region, refined_unit_h)`` hypotheses via back-projection.

    Every live token matching a recorded constellation word votes for the
    target center implied by the recorded scale-normalized offset. Votes within
    ``cluster_radius_norm`` median token heights merge into one hypothesis.
    Clusters whose vote spread exceeds ``max_spread_share`` of the predicted
    target box width or height are rejected so anisotropic reflow abstains
    rather than clicking beside a small control.
    """
    if not live_tokens or not constellation.tokens:
        return []
    live_med_h = _median_token_height(live_tokens)
    votes: list[tuple[float, float, float, int, ConstellationToken, OcrToken]] = []
    for ct_idx, ct in enumerate(constellation.tokens):
        for lt in live_tokens:
            sim = _token_text_similarity(ct.norm_text, lt.norm_text)
            if sim <= 0.0:
                continue
            pred_cx = lt.cx - ct.dx_norm * live_med_h
            pred_cy = lt.cy - ct.dy_norm * live_med_h
            votes.append((pred_cx, pred_cy, ct.weight * sim, ct_idx, ct, lt))
    if not votes:
        return []

    radius_px = cluster_radius_norm * live_med_h
    clusters: list[
        list[tuple[float, float, float, int, ConstellationToken, OcrToken]]
    ] = []
    for item in sorted(votes, key=lambda v: -v[2]):
        vx, vy = item[0], item[1]
        placed = False
        for cluster in clusters:
            tw = sum(c[2] for c in cluster)
            mcx = sum(c[0] * c[2] for c in cluster) / tw
            mcy = sum(c[1] * c[2] for c in cluster) / tw
            if math.hypot(vx - mcx, vy - mcy) <= radius_px:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    results: list[tuple[Region, float]] = []
    min_votes = 2 if len(constellation.tokens) >= 2 else 1
    for cluster in clusters:
        if len(cluster) < min_votes:
            continue
        # Keep at most one (closest-to-median) live match per recorded token so
        # cross-row collisions of repeated words (like "Row") do not distort RMS.
        anchor_cx = float(statistics.median(c[0] for c in cluster))
        anchor_cy = float(statistics.median(c[1] for c in cluster))
        best_by_ct: dict[
            int, tuple[float, float, float, int, ConstellationToken, OcrToken]
        ] = {}
        for item in cluster:
            ct_idx = item[3]
            prev = best_by_ct.get(ct_idx)
            if prev is None:
                best_by_ct[ct_idx] = item
                continue
            d_new = math.hypot(item[0] - anchor_cx, item[1] - anchor_cy)
            d_prev = math.hypot(prev[0] - anchor_cx, prev[1] - anchor_cy)
            if (d_new, -item[2]) < (d_prev, -prev[2]):
                best_by_ct[ct_idx] = item
        deduped = list(best_by_ct.values())
        if len(deduped) < min_votes:
            continue
        pairs = [(c[4], c[5], c[2]) for c in deduped]
        unit_h = _refined_unit_height(pairs, live_med_h)
        # If inter-token spacing scale contradicts OCR glyph height scale by
        # more than 22%, the layout reflowed anisotropically; abstain safely.
        if abs(unit_h - live_med_h) / max(live_med_h, 1.0) > 0.22:
            continue
        refined = [
            (lt.cx - ct.dx_norm * unit_h, lt.cy - ct.dy_norm * unit_h, weight)
            for ct, lt, weight in pairs
        ]
        tw = sum(r[2] for r in refined)
        mcx = sum(r[0] * r[2] for r in refined) / tw
        mcy = sum(r[1] * r[2] for r in refined) / tw
        box_w = max(4, int(round(constellation.target_w_norm * unit_h)))
        box_h = max(4, int(round(constellation.target_h_norm * unit_h)))
        rms_x = math.sqrt(sum(r[2] * (r[0] - mcx) ** 2 for r in refined) / tw)
        rms_y = math.sqrt(sum(r[2] * (r[1] - mcy) ** 2 for r in refined) / tw)
        if rms_x > max_spread_share * box_w or rms_y > max_spread_share * box_h:
            continue
        x0 = max(0, int(round(mcx - box_w / 2.0)))
        y0 = max(0, int(round(mcy - box_h / 2.0)))
        results.append(((x0, y0, box_w, box_h), unit_h))
    return results


def _vote_candidate_regions(
    live_tokens: Sequence[OcrToken],
    constellation: TargetConstellation,
    *,
    cluster_radius_norm: float = 1.5,
    max_spread_share: float = 0.35,
) -> list[Region]:
    """Generate candidate regions for blank/unlabeled targets via back-projection."""
    return [
        region
        for region, _ in _vote_candidate_regions_with_scale(
            live_tokens,
            constellation,
            cluster_radius_norm=cluster_radius_norm,
            max_spread_share=max_spread_share,
        )
    ]


class ConstellationGrounder:
    """Grounder conforming to :class:`~openadapt_flow.runtime.grounder.Grounder`.

    Resolves targets using scale-normalized surrounding OCR-word constellations.
    Runs locally in-process (``MAY_EGRESS = False``) and abstains (returns
    ``None``) whenever the top score is below ``min_score`` or the top two
    distinct candidates fall within ``tie_margin``.
    """

    MAY_EGRESS = False

    def __init__(
        self,
        constellations: Optional[Mapping[str, TargetConstellation]] = None,
        *,
        default_constellation: Optional[TargetConstellation] = None,
        min_score: float = DEFAULT_MIN_SCORE,
        tie_margin: float = DEFAULT_TIE_MARGIN,
        sigma_norm: float = DEFAULT_SIGMA_NORM,
        max_err_norm: float = DEFAULT_MAX_ERR_NORM,
        ocr_fn: Optional[OcrFn] = None,
    ) -> None:
        self._constellations: dict[str, TargetConstellation] = dict(
            constellations or {}
        )
        self._default = default_constellation
        self._min_score = min_score
        self._tie_margin = tie_margin
        self._sigma_norm = sigma_norm
        self._max_err_norm = max_err_norm
        self._ocr_fn = ocr_fn

    def register(self, key: str, constellation: TargetConstellation) -> None:
        """Register or replace a constellation keyed by step intent or label."""
        self._constellations[key] = constellation

    def _resolve_constellation(
        self, intent: str, ocr_text: Optional[str]
    ) -> Optional[TargetConstellation]:
        if intent and intent in self._constellations:
            return self._constellations[intent]
        if ocr_text and ocr_text in self._constellations:
            return self._constellations[ocr_text]
        return self._default

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Locate the target on ``screen_png`` using its recorded constellation.

        Returns ``None`` (abstain) when no constellation is registered, OCR
        yields no tokens, no candidate clears ``min_score``, or the top two
        distinct candidates are within ``tie_margin``.
        """
        constellation = self._resolve_constellation(intent, ocr_text)
        if constellation is None or not constellation.tokens:
            return None
        live_tokens = extract_ocr_tokens(screen_png, ocr_fn=self._ocr_fn)
        if not live_tokens:
            return None

        label = (ocr_text or "").strip() or (constellation.label or "").strip()
        candidate_unit_heights: Optional[list[float]] = None
        if label:
            candidates = _label_candidate_regions(live_tokens, label)
        else:
            voted = _vote_candidate_regions_with_scale(live_tokens, constellation)
            candidates = [r for r, _ in voted]
            candidate_unit_heights = [u for _, u in voted]
        if not candidates:
            return None

        winner = disambiguate_candidates(
            screen_png,
            candidates,
            constellation,
            min_score=self._min_score,
            tie_margin=self._tie_margin,
            sigma_norm=self._sigma_norm,
            max_err_norm=self._max_err_norm,
            ocr_fn=self._ocr_fn,
            _live_tokens=live_tokens,
            _candidate_unit_heights=candidate_unit_heights,
        )
        if winner is None:
            return None
        return GrounderMatch(
            point=winner.point,
            region=winner.region,
            confidence=round(float(winner.score), 4),
        )
