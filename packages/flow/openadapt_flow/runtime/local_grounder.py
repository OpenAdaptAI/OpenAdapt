"""Local grounding model for pixel surfaces (Citrix, RDP, VNC, canvas).

The template rungs compare the recorded crop with the live frame pixel for
pixel. That breaks when the remote session draws the same control at another
display scale, zoom, font size or theme, or through a lossy codec. This rung
finds the control again with a small vision model that runs in process:

1. **Candidates.** The recorded crop and the live frame are reduced to edge
   strength, which does not depend on colour or on light-on-dark versus
   dark-on-light. The crop is matched at a range of sizes (0.75x to 2.1x), and
   every distinct peak becomes a candidate box.
2. **Model scores.** Each candidate is cropped from the live frame and
   embedded with DINOv2-small (Apache-2.0, 22M parameters, ONNX on the
   onnxruntime Flow already ships with). Its patch tokens are compared, place
   by place, with the recorded crop's, and with a luminance-inverted copy of
   it so a dark theme still compares like with like.
3. **Count, never pick.** Every candidate that scores at or above
   :data:`ACCEPT_SCORE`, or within :data:`LOOKALIKE_MARGIN` of the best, is a
   look-alike. The resolver keeps a look-alike only when the step's retained
   landmarks place the target there; two or more left over halt the step.

The model only locates. It never decides identity: the resolver still
corroborates the target's own text with OCR, the identity check still reads
the record, and the look-alike census still counts rows. The rung is below
OCR in the ladder, so an irreversible step it resolves halts at the risk gate.

The weights are not part of the wheel. :meth:`LocalGroundingModel.load`
reads them from ``OPENADAPT_FLOW_MODEL_DIR`` (default
``~/.cache/openadapt/models``), downloads them once from the pinned Hugging
Face revision when they are missing, and refuses any file whose SHA-256
differs from :data:`WEIGHTS`. Queries make no network call.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

from openadapt_flow.ir import Region

#: The pinned weights: the int8 ONNX export of ``facebook/dinov2-small``
#: (Apache-2.0) published as ``onnx-community/dinov2-small``. Revision and
#: digest are fixed; a changed file is refused, never loaded. int8 scores 12
#: candidates in about 55 ms on 4 CPU threads, 2.5x faster than float32.
WEIGHTS = {
    "name": "dinov2-small",
    "license": "Apache-2.0",
    "source": "facebook/dinov2-small",
    "url": (
        "https://huggingface.co/onnx-community/dinov2-small/resolve/"
        "8b1f705a3a7f6f062f6bdd21986c1583d3ef105d/onnx/model_int8.onnx"
    ),
    "sha256": "dfce54a839b491f395c516350ebb4a78f947e9170a6beac0f2bc5638e0f09d61",
    "bytes": 24446700,
    "filename": "dinov2-small-8b1f705a-model_int8.onnx",
}

#: Model input side. 112 px is 8x8 patches of 14 px: enough to keep an
#: icon's layout, small enough to embed 24 candidates in one batch.
INPUT_SIDE = 112
PATCH = 14
#: Sizes, relative to the recorded crop, at which candidates are sought.
SCALES = (0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.5, 1.75, 2.0)
#: Peaks kept per scale before candidates from all scales are merged.
PEAKS_PER_SCALE = 6
#: Edge-correlation floor for a candidate peak. Low on purpose: the model,
#: not the edge match, decides what counts as the target.
EDGE_FLOOR = 0.3
MAX_CANDIDATES = 12
#: The edge search runs at a size where the crop's short side is this many
#: pixels; finer search costs time and adds nothing the model does not.
SEARCH_SHORT_SIDE = 20
#: Patch-wise cosine similarity at or above which a candidate looks like the
#: recorded target. Set on the benchmark in ``benchmark/pixel_grounding``:
#: the true target scored 0.80 or more under every rendering condition there.
ACCEPT_SCORE = 0.78
#: A candidate within this margin of the best one is counted as a look-alike
#: even below ACCEPT_SCORE, so a near tie is never resolved by ranking.
LOOKALIKE_MARGIN = 0.06

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class WeightsUnavailable(RuntimeError):
    """The pinned weights are missing and could not be fetched and verified."""


@dataclass(frozen=True)
class Candidate:
    """One place on the live frame that looks like the recorded target."""

    region: Region
    scale: float
    score: float

    @property
    def point(self) -> tuple[int, int]:
        x, y, w, h = self.region
        return (x + w // 2, y + h // 2)


def model_dir() -> Path:
    configured = os.environ.get("OPENADAPT_FLOW_MODEL_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "openadapt" / "models"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_weights(*, download: bool = True) -> Path:
    """Return the verified weights file, fetching it once if allowed.

    Raises:
        WeightsUnavailable: the file is missing and ``download`` is False, the
            download failed, or the file's digest is not the pinned one.
    """
    directory = model_dir()
    path = directory / WEIGHTS["filename"]
    if path.is_file():
        if _sha256(path) != WEIGHTS["sha256"]:
            raise WeightsUnavailable(
                f"{path} does not match the pinned SHA-256; delete it to re-fetch"
            )
        return path
    if not download:
        raise WeightsUnavailable(
            f"grounding model weights not found at {path}; place the pinned "
            f"file there (SHA-256 {WEIGHTS['sha256']}) or allow the download"
        )
    import httpx

    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".part")
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    try:
        with (
            os.fdopen(fd, "wb") as out,
            httpx.stream(
                "GET", WEIGHTS["url"], follow_redirects=True, timeout=60.0
            ) as response,
        ):
            response.raise_for_status()
            for block in response.iter_bytes(1 << 20):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != WEIGHTS["sha256"]:
            raise WeightsUnavailable(
                "downloaded grounding model weights do not match the pinned "
                "SHA-256; refusing to load them"
            )
        os.replace(tmp, path)
    except httpx.HTTPError as exc:
        raise WeightsUnavailable(f"could not download grounding model weights: {exc}")
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.float32)


def _edges(gray: np.ndarray) -> np.ndarray:
    import cv2

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _nms(
    boxes: list[tuple[float, Region, float]], limit: int
) -> list[tuple[float, Region, float]]:
    """Greedy non-maximum suppression on (score, region, scale) by overlap."""
    kept: list[tuple[float, Region, float]] = []
    for score, region, scale in sorted(boxes, key=lambda b: -b[0]):
        x, y, w, h = region
        cx, cy = x + w / 2, y + h / 2
        clash = False
        for _s, (kx, ky, kw, kh), _sc in kept:
            if (
                abs(cx - (kx + kw / 2)) < max(w, kw) * 0.5
                and abs(cy - (ky + kh / 2)) < max(h, kh) * 0.5
            ):
                clash = True
                break
        if not clash:
            kept.append((score, region, scale))
            if len(kept) >= limit:
                break
    return kept


def edge_candidates(
    frame: Image.Image,
    template: Image.Image,
    *,
    scales: tuple[float, ...] = SCALES,
    floor: float = EDGE_FLOOR,
    limit: int = MAX_CANDIDATES,
) -> list[tuple[float, Region, float]]:
    """Places where the template's edges recur, at any of ``scales``.

    The search runs on a reduced copy of both images, sized so the template's
    short side is about :data:`SEARCH_SHORT_SIDE` px; boxes are mapped back to
    full-frame pixels. The model scores full-resolution crops afterwards.
    """
    import cv2

    tw, th = template.size
    factor = max(1.0, min(tw, th) / SEARCH_SHORT_SIDE)
    frame_gray = _gray(frame)
    template_gray = _gray(template)
    if factor > 1.0:
        frame_gray = cv2.resize(
            frame_gray,
            (int(round(frame.size[0] / factor)), int(round(frame.size[1] / factor))),
            interpolation=cv2.INTER_AREA,
        )
    frame_edges = _edges(frame_gray)
    fh, fw = frame_edges.shape
    found: list[tuple[float, Region, float]] = []
    for scale in scales:
        w, h = int(round(tw * scale / factor)), int(round(th * scale / factor))
        if w < 6 or h < 6 or w >= fw or h >= fh:
            continue
        resized = cv2.resize(template_gray, (w, h), interpolation=cv2.INTER_AREA)
        tmpl_edges = _edges(resized)
        if float(tmpl_edges.std()) < 1e-3:
            continue
        result = cv2.matchTemplate(frame_edges, tmpl_edges, cv2.TM_CCOEFF_NORMED)
        # The strongest peaks at this scale, each suppressing its own
        # neighbourhood before the next is taken.
        for _ in range(PEAKS_PER_SCALE):
            _min, peak, _minloc, (x, y) = cv2.minMaxLoc(result)
            if peak < floor:
                break
            result[
                max(0, y - h // 2) : y + h // 2 + 1, max(0, x - w // 2) : x + w // 2 + 1
            ] = -1.0
            region = (
                int(round(x * factor)),
                int(round(y * factor)),
                int(round(tw * scale)),
                int(round(th * scale)),
            )
            found.append((float(peak), region, scale))
    return _nms(found, limit)


class LocalGroundingModel:
    """Scale-, zoom-, font- and theme-tolerant locator for a recorded crop.

    Construct with :meth:`load`. Thread-safe for concurrent ``locate_anchor``
    calls: onnxruntime sessions are; the crop cache is guarded by a lock.
    """

    MAY_EGRESS = False

    def __init__(self, session: Any, weights_path: Path) -> None:
        self._session = session
        self._weights_path = weights_path
        self._input = session.get_inputs()[0].name
        self._lock = threading.Lock()
        self._template_cache: dict[str, np.ndarray] = {}
        self.last_elapsed_ms: float = 0.0

    @classmethod
    def load(
        cls, *, download: bool = True, threads: Optional[int] = None
    ) -> "LocalGroundingModel":
        """Load the pinned weights (fetching them once when ``download``)."""
        import onnxruntime as ort

        path = ensure_weights(download=download)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads or min(4, os.cpu_count() or 1)
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        return cls(session, path)

    def describe(self) -> dict[str, Any]:
        return {
            "rung": "grounder",
            "model": WEIGHTS["name"],
            "source": WEIGHTS["source"],
            "license": WEIGHTS["license"],
            "sha256": WEIGHTS["sha256"],
            "input_side": INPUT_SIDE,
            "accept_score": ACCEPT_SCORE,
            "lookalike_margin": LOOKALIKE_MARGIN,
        }

    # -- model -----------------------------------------------------------
    def _embed(self, crops: list[Image.Image]) -> np.ndarray:
        """Patch tokens, L2-normalised: (n, 64, 384)."""
        batch = np.stack(
            [
                (
                    np.asarray(
                        c.convert("RGB").resize(
                            (INPUT_SIDE, INPUT_SIDE), Image.BICUBIC
                        ),
                        dtype=np.float32,
                    )
                    / 255.0
                    - _IMAGENET_MEAN
                )
                / _IMAGENET_STD
                for c in crops
            ]
        ).transpose(0, 3, 1, 2)
        out = self._session.run(None, {self._input: batch.astype(np.float32)})[0]
        patches = out[:, 1:, :]
        norms = np.linalg.norm(patches, axis=-1, keepdims=True)
        return patches / np.maximum(norms, 1e-6)

    def _template_tokens(self, template_png: bytes) -> tuple[np.ndarray, np.ndarray]:
        """The crop's patch tokens (and its inverse's), and per-patch weights.

        A patch weighs by the edge content under it, so flat background,
        which looks alike everywhere, does not make every place look like
        the target.
        """
        key = hashlib.sha256(template_png).hexdigest()
        with self._lock:
            cached = self._template_cache.get(key)
        if cached is not None:
            return cached
        image = Image.open(io.BytesIO(template_png)).convert("RGB")
        inverted = Image.fromarray(255 - np.asarray(image))
        tokens = self._embed([image, inverted])
        grid = INPUT_SIDE // PATCH
        energy = _edges(
            np.asarray(
                image.convert("L").resize((INPUT_SIDE, INPUT_SIDE), Image.BICUBIC),
                dtype=np.float32,
            )
        )
        pooled = energy.reshape(grid, PATCH, grid, PATCH).mean(axis=(1, 3)).reshape(-1)
        weights = pooled / max(float(pooled.max()), 1e-6)
        weights = np.maximum(weights, 0.02)
        weights = (weights / weights.sum()).astype(np.float32)
        entry = (tokens, weights)
        with self._lock:
            self._template_cache[key] = entry
        return entry

    # -- grounding -------------------------------------------------------
    def candidates(self, screen_png: bytes, template_png: bytes) -> list[Candidate]:
        """Score every edge candidate against the recorded crop, best first."""
        started = time.monotonic()
        frame = Image.open(io.BytesIO(screen_png)).convert("RGB")
        template = Image.open(io.BytesIO(template_png)).convert("RGB")
        proposals = edge_candidates(frame, template)
        if not proposals:
            self.last_elapsed_ms = (time.monotonic() - started) * 1000.0
            return []
        reference, weights = self._template_tokens(template_png)
        crops = [
            frame.crop((x, y, x + w, y + h)) for _s, (x, y, w, h), _sc in proposals
        ]
        tokens = self._embed(crops)
        # Place-by-place cosine weighted by the crop's content, against the
        # crop and its inverse; the better of the two counts.
        per_patch = np.einsum("npd,tpd->ntp", tokens, reference)
        scores = (per_patch * weights).sum(axis=-1).max(axis=-1)
        found = [
            Candidate(region=region, scale=scale, score=float(score))
            for (_s, region, scale), score in zip(proposals, scores.tolist())
        ]
        found.sort(key=lambda c: -c.score)
        self.last_elapsed_ms = (time.monotonic() - started) * 1000.0
        return found

    def locate_anchor(
        self,
        screen_png: bytes,
        template_png: bytes,
    ) -> list[Candidate]:
        """Every look-alike of the recorded target on the frame, best first.

        Empty when nothing scores like the target. More than one entry means
        the frame shows look-alikes and the caller must tell them apart with
        independent evidence or refuse.
        """
        scored = self.candidates(screen_png, template_png)
        if not scored or scored[0].score < ACCEPT_SCORE:
            return []
        best = scored[0].score
        return [
            c
            for c in scored
            if c.score >= ACCEPT_SCORE or c.score >= best - LOOKALIKE_MARGIN
        ]

    # The plain Grounder protocol: this rung needs the recorded crop, which
    # ``locate`` does not carry, so it abstains there.
    def locate(
        self, screen_png: bytes, intent: str, ocr_text: Optional[str] = None
    ) -> None:
        return None
