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


class TargetConstellation(BaseModel):
    """Scale-normalized surrounding OCR-word constellation for one target."""

    tokens: list[ConstellationToken]
    median_token_height: float
    target_w_norm: float
    target_h_norm: float
    label: Optional[str] = None
    intent: Optional[str] = None


class CandidateScore(BaseModel):
    """Constellation match result for a single candidate region."""

    region: Region
    point: Point
    score: float
    matched_tokens: int
    total_tokens: int


def normalize_token_text(text: str) -> str:
    """Lowercase and strip surrounding punctuation from a word token."""
    cleaned = re.sub(r"^[^0-9a-zA-Z]+|[^0-9a-zA-Z]+$", "", text.strip())
    return cleaned.lower()


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
        for match in re.finditer(r"\S+", raw_text):
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


def capture_constellation(
    screen_png: bytes,
    target_region: Region,
    *,
    target_point: Optional[Point] = None,
    label: Optional[str] = None,
    intent: Optional[str] = None,
    max_radius_norm: float = DEFAULT_MAX_RADIUS_NORM,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    ocr_fn: Optional[OcrFn] = None,
) -> TargetConstellation:
    """Record nearby OCR words around ``target_region`` in scale-normalized units.

    Tokens whose centers fall inside ``target_region`` are excluded so the
    constellation captures surrounding context rather than self-matching a
    repeated button or field label.
    """
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
    nearby: list[tuple[float, ConstellationToken]] = []
    for tok in tokens:
        if _point_in_region(tok.cx, tok.cy, target_region):
            continue
        dx_norm = (tok.cx - tx) / med_h
        dy_norm = (tok.cy - ty) / med_h
        dist_norm = math.hypot(dx_norm, dy_norm)
        if dist_norm > max_radius_norm:
            continue
        weight = 1.0 / (1.0 + dist_norm / 6.0)
        nearby.append(
            (
                dist_norm,
                ConstellationToken(
                    text=tok.text,
                    norm_text=tok.norm_text,
                    dx_norm=dx_norm,
                    dy_norm=dy_norm,
                    weight=weight,
                ),
            )
        )
    nearby.sort(key=lambda item: item[0])
    kept = [item[1] for item in nearby[:max_tokens]]
    return TargetConstellation(
        tokens=kept,
        median_token_height=med_h,
        target_w_norm=max(0.25, float(target_region[2]) / med_h),
        target_h_norm=max(0.25, float(target_region[3]) / med_h),
        label=label,
        intent=intent,
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
        dist_norm = math.hypot(ct.dx_norm, ct.dy_norm)
        eff_sigma = sigma_norm * (1.0 + 0.08 * dist_norm)
        eff_max_err = max_err_norm * (1.0 + 0.08 * dist_norm)
        best_token_score = 0.0
        for lt in live_tokens:
            text_sim = _token_text_similarity(ct.norm_text, lt.norm_text)
            if text_sim <= 0.0:
                continue
            err_norm = math.hypot(lt.cx - ex, lt.cy - ey) / live_med_h
            if err_norm > eff_max_err:
                continue
            spatial = math.exp(-0.5 * (err_norm / eff_sigma) ** 2)
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
) -> Optional[CandidateScore]:
    """Pick the single candidate matching ``constellation``, or abstain (None).

    Returns ``None`` whenever:
    - no candidates or constellation tokens are provided,
    - OCR produces no live tokens,
    - the best candidate scores below ``min_score``, or
    - the second-best distinct candidate is within ``tie_margin`` of the best.
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

    scored = [
        _score_point(
            region[0] + region[2] / 2.0,
            region[1] + region[3] / 2.0,
            region,
            live_tokens,
            live_med_h,
            constellation,
            sigma_norm=sigma_norm,
            max_err_norm=max_err_norm,
        )
        for region in candidates
    ]
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


def _vote_candidate_regions(
    live_tokens: Sequence[OcrToken],
    constellation: TargetConstellation,
    *,
    cluster_radius_norm: float = 1.5,
    max_spread_share: float = 0.35,
) -> list[Region]:
    """Generate candidate regions for blank/unlabeled targets via back-projection.

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
    votes: list[tuple[float, float, float, ConstellationToken, OcrToken]] = []
    for ct in constellation.tokens:
        for lt in live_tokens:
            sim = _token_text_similarity(ct.norm_text, lt.norm_text)
            if sim <= 0.0:
                continue
            pred_cx = lt.cx - ct.dx_norm * live_med_h
            pred_cy = lt.cy - ct.dy_norm * live_med_h
            votes.append((pred_cx, pred_cy, ct.weight * sim, ct, lt))
    if not votes:
        return []

    radius_px = cluster_radius_norm * live_med_h
    clusters: list[list[tuple[float, float, float, ConstellationToken, OcrToken]]] = []
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

    regions: list[Region] = []
    min_votes = 2 if len(constellation.tokens) >= 2 else 1
    for cluster in clusters:
        if len(cluster) < min_votes:
            continue
        pairs = [(c[3], c[4], c[2]) for c in cluster]
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
        regions.append((x0, y0, box_w, box_h))
    return regions


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
        if label:
            candidates = _label_candidate_regions(live_tokens, label)
        else:
            candidates = _vote_candidate_regions(live_tokens, constellation)
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
        )
        if winner is None:
            return None
        return GrounderMatch(
            point=winner.point,
            region=winner.region,
            confidence=round(float(winner.score), 4),
        )
