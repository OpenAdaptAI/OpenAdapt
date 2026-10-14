"""Directory-based demonstration library for demo-guided execution.

Stores demonstrations as sequences of (screenshot, action, metadata) on disk.
Retrieval uses perceptual hash (pHash) visual similarity alignment to find the
demo step whose screenshot is most similar to the agent's current screen state.
Falls back to sequential step index alignment when no screenshot is provided
or imagehash is not installed.

Alignment strategies are pluggable via the ``AlignmentStrategy`` protocol.
The default ``PHashAlignmentStrategy`` uses perceptual hashing.  When
``open-clip-torch`` is installed (part of ``[training]`` extras), a
``CLIPAlignmentStrategy`` and ``HybridAlignmentStrategy`` (pHash top-K +
CLIP re-rank) become available.  An ``OpenAIEmbeddingAlignment`` strategy
uses OpenAI's VLM + text-embedding-3-small for semantic matching without
requiring a local CLIP model.  Use ``create_alignment_strategy("openai")``
or pass ``alignment_strategy="openai"`` to ``DemoLibrary``.

Monotonic progress bias penalizes backward jumps in alignment to prevent
oscillating step matches.  Adaptive guidance disabling turns off demo
guidance when alignment confidence is consistently low.

Supports optional VLM-based element description enrichment: instead of
returning raw coordinate instructions like ``CLICK(0.960, 0.066)``, enriched
demos produce human-readable guidance such as *"Click the three-dot menu in
the top-right corner of Chrome at approximately (0.960, 0.066)"*.  This is
the core value of the DemoLibrary -- rich, actionable guidance for agents.

Resolution normalization ensures that demos recorded at one resolution still
provide accurate coordinate guidance when the agent operates at a different
resolution.

Demonstration libraries for GUI agents follow the retrieval-augmented
approach in the agent literature, where pre-recorded expert trajectories
are stored, indexed, and retrieved at inference time to guide agent
behavior on similar tasks.

Usage:
    from openadapt_evals.demo_library import DemoLibrary

    library = DemoLibrary("./demos")
    demo_id = library.add_demo("notepad_1", screenshots=[...], actions=[...])

    # Enrich with VLM element descriptions (optional, requires API key)
    library.enrich_demo("notepad_1", demo_id=demo_id)

    guidance = library.align_step("notepad_1", current_screenshot, step_index=2)
    print(guidance.instruction)
    # -> "Click on three-dot menu button in Chrome toolbar at approximately (0.960, 0.066)"

Prior Art:
    - AgentTrek: Li et al., "AgentTrek: Agent Trajectory Synthesis via
      Guiding Replay with Web Tutorials", arXiv 2412.09605, 2024.
      Retrieval-augmented trajectory synthesis for web agents.
    - WebAgent: Gur et al., "A Real-World WebAgent with Planning, Long
      Context Understanding, and Program Synthesis", ICLR 2024.
      Demonstration-augmented web agent architecture.
    - RCI: Kim et al., "Language Models can Solve Computer Tasks",
      NeurIPS 2023. Recursive Criticism and Improvement with
      demonstration conditioning.
    - Retrieval-augmented generation: Lewis et al., "Retrieval-Augmented
      Generation for Knowledge-Intensive NLP Tasks", NeurIPS 2020.
      General RAG paradigm applied here to agent trajectories.
"""

from __future__ import annotations

import io
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from openadapt_evals.adapters.base import BenchmarkAction

try:
    import imagehash
    from PIL import Image

    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False

try:
    import numpy as np
    import open_clip
    import torch

    _HAS_CLIP = True
except ImportError:
    _HAS_CLIP = False

try:
    import openai as _openai_module

    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False

logger = logging.getLogger(__name__)

# Default backward penalty for monotonic progress bias.  A full backward
# jump (from last step to step 0) adds this fraction to the normalized
# distance.  Configurable via ``DemoLibrary(backward_penalty=...)``.
_DEFAULT_BACKWARD_PENALTY = 0.3

# Default threshold below which alignment is considered "low confidence".
_DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.3

# Number of consecutive low-confidence alignments before guidance is
# disabled for the remainder of the episode.
_DEFAULT_MAX_CONSECUTIVE_LOW_CONFIDENCE = 3


@dataclass
class DemoStep:
    """A single step in a demonstration."""

    step_index: int
    screenshot_path: str  # relative to demo dir
    action_type: str
    action_description: str  # human-readable
    target_description: str  # what element was interacted with
    action_value: str  # text typed, key pressed, etc.
    metadata: dict[str, Any] = field(default_factory=dict)

    # Coordinates for click/drag actions (normalized 0-1)
    x: float | None = None
    y: float | None = None

    # VLM-generated element description (e.g., "three-dot menu button in
    # Chrome toolbar").  Populated by ``DemoLibrary.enrich_demo()`` or by
    # passing ``descriptions`` to ``add_demo()``.  Empty string if not
    # enriched.
    description: str = ""

    # Rich grounding target for the cascade architecture.
    # Populated by ``enrich_grounding_targets()`` or manually.
    # See docs/design/grounding_cascade_design_v3.md.
    grounding_target: Any = field(default=None, repr=False)

    # Cached perceptual hash for visual similarity alignment.
    # Not serialized to demo.json -- computed lazily by
    # ``_ensure_demo_phashes()``.
    _phash: Any = field(default=None, repr=False, compare=False)


@dataclass
class Demo:
    """A complete demonstration of a task."""

    task_id: str
    demo_id: str
    description: str  # what the demo accomplishes
    steps: list[DemoStep]
    created_at: str = ""  # ISO format
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


@dataclass
class DemoGuidance:
    """Guidance from a demonstration for the current step.

    Returned by ``DemoLibrary.align_step()`` to tell the agent what the
    demo says to do at this point in the task.
    """

    available: bool  # whether demo guidance was found
    step_index: int  # which demo step this corresponds to
    instruction: str  # human-readable guidance text
    action_type: str  # expected action type
    action_value: str  # expected action value (text, key, etc.)
    target_description: str  # what to interact with
    screenshot_path: str | None  # path to demo screenshot for this step
    next_screenshot_path: str | None  # path to demo screenshot for next step
    confidence: float  # alignment confidence 0-1
    total_demo_steps: int  # total steps in the demo
    metadata: dict[str, Any] = field(default_factory=dict)

    # Visual alignment metadata -- populated when align_step() uses
    # perceptual hash matching instead of sequential index.
    visual_alignment_used: bool = False
    visual_distance: float | None = None  # normalized distance (lower=closer)

    def to_prompt_text(self) -> str:
        """Format guidance as text suitable for injection into an agent prompt.

        When VLM-enriched descriptions are available, the instruction line
        will include the element description (e.g., *"Click on three-dot
        menu button in Chrome toolbar at approximately (0.96, 0.07)"*)
        instead of raw coordinate notation.

        NOTE: The guidance intentionally does NOT include step counts or
        position information (e.g., "step 4/4") because the planner can
        misinterpret "last step" as "task is done" and prematurely signal
        DONE. The guidance describes WHAT to do, not WHERE in the demo
        the agent currently is.
        """
        if not self.available:
            return ""

        lines = [
            "DEMONSTRATION GUIDANCE (current step hint):",
            f"  Expected action: {self.action_type}",
        ]
        if self.target_description:
            lines.append(f"  Target: {self.target_description}")
        if self.action_value:
            lines.append(f"  Value: {self.action_value}")
        # Use description (natural language) rather than
        # coordinate-based instruction when available.
        instruction = self.instruction
        if self.metadata.get("description"):
            instruction = self.metadata["description"]
        lines.append(f"  Instruction: {instruction}")
        lines.append(
            "  NOTE: Adapt if the current UI state differs from the demo. "
            "This is guidance, not a rigid script. "
            "Do NOT assume the task is complete just because this is the "
            "last guidance step -- verify the task goal is actually met."
        )
        return "\n".join(lines)


@dataclass
class AlignmentTraceEntry:
    """One step's alignment result for post-hoc analysis.

    Stored in ``DemoGuidance.metadata["alignment_trace"]`` when visual
    alignment is used.  Enables retroactive comparison of alignment
    methods and identification of failure patterns.
    """

    agent_step_index: int  # Agent's step number
    matched_demo_step: int  # Which demo step was matched
    distance: float  # Normalized distance (0-1)
    confidence: float  # Final confidence score (0-1)
    method: str  # "phash", "phash+clip", "openai", "sequential", "disabled"
    visual_alignment_used: bool
    candidates_considered: int  # How many demo steps were compared
    elapsed_ms: float  # Time for alignment computation
    backward_penalty_applied: bool = False
    guidance_disabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Alignment Strategy Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AlignmentStrategy(Protocol):
    """Protocol for pluggable alignment strategies.

    Implementations find the closest demo step to a given screenshot.
    The ``DemoLibrary`` delegates to an ``AlignmentStrategy`` during
    ``align_step()`` so that the matching algorithm can be swapped
    (pHash, CLIP, hybrid, etc.) without changing the library's API.
    """

    def find_closest_step(
        self,
        current_screenshot: bytes,
        demo: Demo,
        demo_dir: Path,
        min_step: int,
        backward_penalty: float,
    ) -> tuple[int, float, dict[str, Any]]:
        """Find the demo step whose screenshot best matches the current state.

        Args:
            current_screenshot: PNG bytes of current screen.
            demo: Demo to search.
            demo_dir: Directory containing demo screenshots.
            min_step: Last matched step index (for monotonic bias).
                Steps before this index receive a backward penalty.
                Use ``0`` to disable monotonic bias.
            backward_penalty: Penalty weight for backward jumps.
                A value of ``0.3`` means a full backward jump adds
                0.3 to the normalized distance.

        Returns:
            Tuple of ``(step_index, normalized_distance, metadata)``.
            ``normalized_distance`` is in [0, 1] range.
            ``metadata`` contains method-specific info (e.g., top-K
            candidates, raw distances).
        """
        ...


class PHashAlignmentStrategy:
    """Perceptual hash (pHash) alignment strategy.

    Uses pHash Hamming distance to find the closest demo step.
    This is the default strategy -- fast (sub-millisecond per
    comparison), zero additional dependencies beyond ``imagehash``.
    """

    def find_closest_step(
        self,
        current_screenshot: bytes,
        demo: Demo,
        demo_dir: Path,
        min_step: int,
        backward_penalty: float,
    ) -> tuple[int, float, dict[str, Any]]:
        """Find closest step using pHash Hamming distance."""
        current_img = Image.open(io.BytesIO(current_screenshot))
        current_hash = imagehash.phash(current_img)

        # Ensure all demo step pHashes are computed
        _ensure_demo_phashes(demo, demo_dir)

        total_steps = len(demo.steps)
        best_step = 0
        best_adjusted = float("inf")
        raw_distances: list[tuple[int, int, float]] = []

        for i, step in enumerate(demo.steps):
            if step._phash is None:
                continue
            raw_dist = current_hash - step._phash
            normalized = raw_dist / 64.0

            # Apply monotonic backward penalty
            adjusted = normalized
            if min_step > 0 and i < min_step and total_steps > 0:
                jump = min_step - i
                adjusted += backward_penalty * (jump / total_steps)

            raw_distances.append((i, raw_dist, adjusted))

            if adjusted < best_adjusted:
                best_adjusted = adjusted
                best_step = i

        # If no step had a valid hash, return step 0 with max distance
        if best_adjusted == float("inf"):
            return 0, 1.0, {"method": "phash", "raw_distances": []}

        # Sort by adjusted distance for top-K metadata
        raw_distances.sort(key=lambda x: x[2])
        top_k = raw_distances[:5]

        meta: dict[str, Any] = {
            "method": "phash",
            "raw_hamming_distance": next(
                (d[1] for d in raw_distances if d[0] == best_step), 64
            ),
            "top_k_candidates": [
                {"step": d[0], "raw_distance": d[1], "adjusted": round(d[2], 4)}
                for d in top_k
            ],
            "backward_penalty_applied": (
                min_step > 0 and best_step < min_step
            ),
        }

        return best_step, best_adjusted, meta


class CLIPAlignmentStrategy:
    """CLIP embedding alignment strategy.

    Uses CLIP (ViT-B/32) cosine similarity for semantic matching.
    Requires ``open-clip-torch`` (included in ``[training]`` extras).

    CLIP embeddings are cached on ``DemoStep.metadata["_clip_embedding"]``
    (runtime only, not serialized).
    """

    def __init__(
        self, model_name: str = "ViT-B-32", pretrained: str = "openai"
    ):
        if not _HAS_CLIP:
            raise ImportError(
                "open-clip-torch is required for CLIPAlignmentStrategy. "
                "Install with: pip install open-clip-torch"
            )
        self._model_name = model_name
        self._pretrained = pretrained
        self._model = None
        self._preprocess = None

    def _ensure_model(self) -> None:
        """Lazily load the CLIP model on first use."""
        if self._model is not None:
            return
        logger.info(
            "Loading CLIP model %s/%s (one-time, ~2s)...",
            self._model_name,
            self._pretrained,
        )
        self._model, _, self._preprocess = open_clip.create_model_and_transforms(
            self._model_name,
            pretrained=self._pretrained,
        )
        self._model.eval()
        logger.info("CLIP model loaded")

    def _embed_image(self, img: Image.Image) -> Any:
        """Compute CLIP embedding for a PIL Image."""
        self._ensure_model()
        preprocessed = self._preprocess(img).unsqueeze(0)
        with torch.no_grad():
            embedding = self._model.encode_image(preprocessed)
            embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        return embedding.squeeze(0).cpu().numpy()

    def _ensure_clip_embeddings(self, demo: Demo, demo_dir: Path) -> None:
        """Compute and cache CLIP embeddings for demo screenshots."""
        for step in demo.steps:
            if step.metadata.get("_clip_embedding") is not None:
                continue
            if not step.screenshot_path:
                continue
            screenshot_file = demo_dir / step.screenshot_path
            if not screenshot_file.exists():
                continue
            try:
                img = Image.open(screenshot_file).convert("RGB")
                step.metadata["_clip_embedding"] = self._embed_image(img)
            except Exception as exc:
                logger.warning(
                    "Failed to compute CLIP embedding for step %d: %s",
                    step.step_index,
                    exc,
                )

    def find_closest_step(
        self,
        current_screenshot: bytes,
        demo: Demo,
        demo_dir: Path,
        min_step: int,
        backward_penalty: float,
    ) -> tuple[int, float, dict[str, Any]]:
        """Find closest step using CLIP cosine similarity."""
        self._ensure_model()
        self._ensure_clip_embeddings(demo, demo_dir)

        current_img = Image.open(io.BytesIO(current_screenshot)).convert("RGB")
        current_emb = self._embed_image(current_img)

        total_steps = len(demo.steps)
        best_step = 0
        best_adjusted = float("inf")
        candidates: list[tuple[int, float, float]] = []

        for i, step in enumerate(demo.steps):
            emb = step.metadata.get("_clip_embedding")
            if emb is None:
                continue
            similarity = float(np.dot(current_emb, emb))
            distance = 1.0 - similarity  # cosine distance

            adjusted = distance
            if min_step > 0 and i < min_step and total_steps > 0:
                jump = min_step - i
                adjusted += backward_penalty * (jump / total_steps)

            candidates.append((i, distance, adjusted))

            if adjusted < best_adjusted:
                best_adjusted = adjusted
                best_step = i

        if best_adjusted == float("inf"):
            return 0, 1.0, {"method": "clip", "candidates": []}

        candidates.sort(key=lambda x: x[2])
        top_k = candidates[:5]

        meta: dict[str, Any] = {
            "method": "clip",
            "top_k_candidates": [
                {
                    "step": c[0],
                    "cosine_distance": round(c[1], 4),
                    "adjusted": round(c[2], 4),
                }
                for c in top_k
            ],
            "backward_penalty_applied": (min_step > 0 and best_step < min_step),
        }

        return best_step, best_adjusted, meta


class HybridAlignmentStrategy:
    """Two-stage pHash + CLIP alignment strategy.

    Stage 1: pHash filters to top-K candidates (fast, ~1ms total).
    Stage 2: CLIP re-ranks the shortlist by semantic similarity.

    Falls back to pHash-only when CLIP is not available.
    """

    def __init__(
        self,
        top_k: int = 5,
        clip_model_name: str = "ViT-B-32",
        clip_pretrained: str = "openai",
    ):
        self._top_k = top_k
        self._clip: CLIPAlignmentStrategy | None = None
        if _HAS_CLIP:
            try:
                self._clip = CLIPAlignmentStrategy(
                    model_name=clip_model_name,
                    pretrained=clip_pretrained,
                )
            except Exception as exc:
                logger.warning(
                    "CLIP initialization failed, using pHash only: %s", exc
                )

    def find_closest_step(
        self,
        current_screenshot: bytes,
        demo: Demo,
        demo_dir: Path,
        min_step: int,
        backward_penalty: float,
    ) -> tuple[int, float, dict[str, Any]]:
        """Two-stage alignment: pHash top-K then CLIP re-rank."""
        current_img = Image.open(io.BytesIO(current_screenshot))
        current_hash = imagehash.phash(current_img)

        _ensure_demo_phashes(demo, demo_dir)

        total_steps = len(demo.steps)

        # Stage 1: pHash coarse filter
        phash_candidates: list[tuple[int, int]] = []
        for i, step in enumerate(demo.steps):
            if step._phash is None:
                continue
            dist = current_hash - step._phash
            phash_candidates.append((i, dist))

        if not phash_candidates:
            return 0, 1.0, {"method": "phash+clip", "stage": "no_candidates"}

        phash_candidates.sort(key=lambda x: x[1])
        shortlist = phash_candidates[: self._top_k]

        # Stage 2: CLIP re-ranking (if available and shortlist > 1)
        if self._clip is not None and len(shortlist) > 1:
            try:
                self._clip._ensure_model()
                self._clip._ensure_clip_embeddings(demo, demo_dir)

                current_rgb = current_img.convert("RGB")
                current_emb = self._clip._embed_image(current_rgb)

                best_step = shortlist[0][0]
                best_adjusted = float("inf")
                clip_results: list[tuple[int, float, float]] = []

                for step_idx, phash_dist in shortlist:
                    step = demo.steps[step_idx]
                    emb = step.metadata.get("_clip_embedding")
                    if emb is None:
                        sim = 1.0 - (phash_dist / 64.0)
                    else:
                        sim = float(np.dot(current_emb, emb))
                    distance = 1.0 - sim

                    adjusted = distance
                    if min_step > 0 and step_idx < min_step and total_steps > 0:
                        jump = min_step - step_idx
                        adjusted += backward_penalty * (jump / total_steps)

                    clip_results.append((step_idx, distance, adjusted))
                    if adjusted < best_adjusted:
                        best_adjusted = adjusted
                        best_step = step_idx

                clip_results.sort(key=lambda x: x[2])
                meta: dict[str, Any] = {
                    "method": "phash+clip",
                    "phash_shortlist": [
                        {"step": s[0], "phash_distance": s[1]} for s in shortlist
                    ],
                    "clip_reranked": [
                        {
                            "step": c[0],
                            "clip_distance": round(c[1], 4),
                            "adjusted": round(c[2], 4),
                        }
                        for c in clip_results
                    ],
                    "backward_penalty_applied": (
                        min_step > 0 and best_step < min_step
                    ),
                }
                return best_step, best_adjusted, meta

            except Exception as exc:
                logger.warning(
                    "CLIP re-ranking failed, falling back to pHash: %s", exc
                )

        # Fallback: pHash only with monotonic bias
        best_step = shortlist[0][0]
        best_adjusted = float("inf")
        for step_idx, phash_dist in phash_candidates:
            normalized = phash_dist / 64.0
            adjusted = normalized
            if min_step > 0 and step_idx < min_step and total_steps > 0:
                jump = min_step - step_idx
                adjusted += backward_penalty * (jump / total_steps)
            if adjusted < best_adjusted:
                best_adjusted = adjusted
                best_step = step_idx

        meta = {
            "method": "phash",
            "raw_hamming_distance": next(
                (d[1] for d in phash_candidates if d[0] == best_step), 64
            ),
            "top_k_candidates": [
                {"step": s[0], "phash_distance": s[1]} for s in shortlist
            ],
            "backward_penalty_applied": (min_step > 0 and best_step < min_step),
        }
        return best_step, best_adjusted, meta


class OpenAIEmbeddingAlignment:
    """OpenAI VLM + text-embedding-3-small alignment strategy.

    Two-step pipeline per screenshot:
    1. Send the screenshot to ``gpt-4o-mini`` asking for a one-sentence
       description of the UI state.
    2. Embed that description with ``text-embedding-3-small``.
    3. Cosine similarity against pre-computed demo step embeddings.

    Demo step embeddings are pre-computed during ``enrich_demo()`` (or
    lazily on first ``find_closest_step`` call) and cached in
    ``DemoStep.metadata["_openai_embedding"]``.  The text descriptions
    are persisted in ``DemoStep.metadata["openai_ui_description"]`` and
    the embedding vectors in ``DemoStep.metadata["openai_embedding"]``
    so they survive serialization to ``demo.json``.

    Falls back to ``PHashAlignmentStrategy`` when no ``OPENAI_API_KEY``
    is set.

    Cost: ~$0.001 per screenshot ($0.0005 VLM describe + $0.00002
    embedding).  Negligible for typical demo libraries.
    """

    # Prompt sent to the VLM to describe the screenshot.
    _DESCRIBE_PROMPT = (
        "Describe the UI state visible in this screenshot in one sentence. "
        "Focus on which application is open, what dialog/page is shown, "
        "and any notable UI elements. Reply with ONLY the sentence."
    )

    def __init__(
        self,
        vlm_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
    ):
        if not _HAS_OPENAI:
            raise ImportError(
                "openai is required for OpenAIEmbeddingAlignment. "
                "Install with: pip install openai"
            )
        self._vlm_model = vlm_model
        self._embedding_model = embedding_model
        self._client: _openai_module.OpenAI | None = None

    def _ensure_client(self) -> _openai_module.OpenAI:
        """Lazily create the OpenAI client."""
        if self._client is None:
            self._client = _openai_module.OpenAI()
        return self._client

    def _describe_screenshot(self, screenshot_bytes: bytes) -> str:
        """Get a one-sentence UI description from a screenshot via VLM."""
        import base64

        client = self._ensure_client()
        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        resp = client.chat.completions.create(
            model=self._vlm_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._DESCRIBE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
            max_tokens=128,
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()

    def _embed_text(self, text: str) -> list[float]:
        """Get a text embedding vector from OpenAI."""
        client = self._ensure_client()
        resp = client.embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        return resp.data[0].embedding

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Uses numpy when available, otherwise a pure-Python fallback.
        """
        try:
            import numpy as _np

            a_arr = _np.array(a)
            b_arr = _np.array(b)
            dot = float(_np.dot(a_arr, b_arr))
            norm_a = float(_np.linalg.norm(a_arr))
            norm_b = float(_np.linalg.norm(b_arr))
        except ImportError:
            import math

            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def precompute_embeddings(self, demo: Demo, demo_dir: Path) -> int:
        """Pre-compute OpenAI embeddings for all demo steps.

        Skips steps that already have embeddings.  Descriptions and
        embedding vectors are stored in ``DemoStep.metadata`` so they
        can be persisted to ``demo.json``.

        Args:
            demo: Demo to enrich with embeddings.
            demo_dir: Directory containing demo screenshots.

        Returns:
            Number of steps that were newly embedded.
        """
        enriched = 0
        for step in demo.steps:
            # Skip if already has a persisted embedding
            if step.metadata.get("openai_embedding") is not None:
                # Also populate runtime cache
                step.metadata["_openai_embedding"] = step.metadata[
                    "openai_embedding"
                ]
                continue
            if not step.screenshot_path:
                continue
            screenshot_file = demo_dir / step.screenshot_path
            if not screenshot_file.exists():
                continue

            try:
                screenshot_bytes = screenshot_file.read_bytes()
                description = self._describe_screenshot(screenshot_bytes)
                embedding = self._embed_text(description)

                # Persist description and embedding
                step.metadata["openai_ui_description"] = description
                step.metadata["openai_embedding"] = embedding
                # Runtime cache
                step.metadata["_openai_embedding"] = embedding
                enriched += 1

                logger.info(
                    "OpenAI embedding for step %d: %s",
                    step.step_index,
                    description[:80],
                )
            except Exception as exc:
                logger.warning(
                    "Failed to compute OpenAI embedding for step %d: %s",
                    step.step_index,
                    exc,
                )
        return enriched

    def _ensure_embeddings(self, demo: Demo, demo_dir: Path) -> None:
        """Ensure all demo steps have OpenAI embeddings (lazy)."""
        needs_compute = False
        for step in demo.steps:
            if step.metadata.get("_openai_embedding") is not None:
                continue
            # Check if persisted embedding exists
            if step.metadata.get("openai_embedding") is not None:
                step.metadata["_openai_embedding"] = step.metadata[
                    "openai_embedding"
                ]
                continue
            needs_compute = True
            break

        if needs_compute:
            count = self.precompute_embeddings(demo, demo_dir)
            if count > 0:
                logger.info(
                    "Lazily computed %d OpenAI embeddings for demo %s",
                    count,
                    demo.demo_id,
                )

    def find_closest_step(
        self,
        current_screenshot: bytes,
        demo: Demo,
        demo_dir: Path,
        min_step: int,
        backward_penalty: float,
    ) -> tuple[int, float, dict[str, Any]]:
        """Find closest step using OpenAI embedding cosine similarity."""
        self._ensure_embeddings(demo, demo_dir)

        # Describe and embed the current screenshot
        try:
            description = self._describe_screenshot(current_screenshot)
            current_embedding = self._embed_text(description)
        except Exception as exc:
            logger.warning(
                "OpenAI embedding failed for current screenshot: %s", exc
            )
            return 0, 1.0, {"method": "openai", "error": str(exc)}

        total_steps = len(demo.steps)
        best_step = 0
        best_adjusted = float("inf")
        candidates: list[tuple[int, float, float]] = []

        for i, step in enumerate(demo.steps):
            emb = step.metadata.get("_openai_embedding")
            if emb is None:
                continue
            similarity = self._cosine_similarity(current_embedding, emb)
            distance = 1.0 - similarity

            adjusted = distance
            if min_step > 0 and i < min_step and total_steps > 0:
                jump = min_step - i
                adjusted += backward_penalty * (jump / total_steps)

            candidates.append((i, distance, adjusted))

            if adjusted < best_adjusted:
                best_adjusted = adjusted
                best_step = i

        if best_adjusted == float("inf"):
            return 0, 1.0, {"method": "openai", "candidates": []}

        candidates.sort(key=lambda x: x[2])
        top_k = candidates[:5]

        meta: dict[str, Any] = {
            "method": "openai",
            "current_description": description,
            "top_k_candidates": [
                {
                    "step": c[0],
                    "cosine_distance": round(c[1], 4),
                    "adjusted": round(c[2], 4),
                }
                for c in top_k
            ],
            "backward_penalty_applied": (
                min_step > 0 and best_step < min_step
            ),
        }

        return best_step, best_adjusted, meta


def create_alignment_strategy(
    name: str,
) -> AlignmentStrategy:
    """Create an alignment strategy by name.

    Convenience factory for constructing alignment strategies from
    string identifiers.  Useful for CLI flags and configuration files.

    Args:
        name: Strategy name.  One of ``"phash"``, ``"clip"``,
            ``"hybrid"``, ``"openai"``.

    Returns:
        An ``AlignmentStrategy`` instance.

    Raises:
        ValueError: If *name* is not a recognized strategy.
    """
    name = name.lower().strip()
    if name == "phash":
        return PHashAlignmentStrategy()
    elif name == "clip":
        return CLIPAlignmentStrategy()
    elif name == "hybrid":
        return HybridAlignmentStrategy()
    elif name == "openai":
        return OpenAIEmbeddingAlignment()
    else:
        raise ValueError(
            f"Unknown alignment strategy: {name!r}. "
            f"Choose from: phash, clip, hybrid, openai"
        )


# ---------------------------------------------------------------------------
# Module-level helpers for pHash computation
# ---------------------------------------------------------------------------


def _ensure_demo_phashes(demo: Demo, demo_dir: Path) -> None:
    """Compute and cache pHash for each demo step's screenshot.

    Only computes hashes for steps that don't already have one
    cached in ``step._phash``.  This means the first call per demo
    does the work and all subsequent calls are free.

    Args:
        demo: Demo whose steps need hashes.
        demo_dir: Directory containing the demo's screenshot files.
    """
    for step in demo.steps:
        if step._phash is not None:
            continue
        if not step.screenshot_path:
            continue
        screenshot_file = demo_dir / step.screenshot_path
        if not screenshot_file.exists():
            logger.debug(
                "Screenshot %s not found, skipping pHash for step %d",
                screenshot_file,
                step.step_index,
            )
            continue
        try:
            img = Image.open(screenshot_file)
            step._phash = imagehash.phash(img)
        except Exception as exc:
            logger.warning(
                "Failed to compute pHash for step %d: %s",
                step.step_index,
                exc,
            )


def _demo_to_dict(demo: Demo) -> dict[str, Any]:
    """Serialize a Demo to a dict, stripping non-serializable internal fields.

    Removes ``DemoStep._phash`` (cached ``imagehash.ImageHash`` object),
    ``DemoStep.metadata["_clip_embedding"]`` (cached numpy array), and
    ``DemoStep.metadata["_openai_embedding"]`` (runtime cache of
    persisted ``openai_embedding``).

    Note: ``openai_embedding`` and ``openai_ui_description`` are *kept*
    because they are the persisted forms (plain lists/strings) that
    should survive serialization.
    """
    data = asdict(demo)
    for step_dict in data.get("steps", []):
        step_dict.pop("_phash", None)
        # Serialize GroundingTarget to dict if present
        gt = step_dict.pop("grounding_target", None)
        if gt and isinstance(gt, dict):
            # asdict already converted it; remove empty fields
            gt = {k: v for k, v in gt.items()
                  if v is not None and v != "" and v != [] and v != ()}
            if gt:
                step_dict["grounding_target"] = gt
        if "metadata" in step_dict and isinstance(step_dict["metadata"], dict):
            step_dict["metadata"].pop("_clip_embedding", None)
            step_dict["metadata"].pop("_openai_embedding", None)
    return data


def _empty_guidance(step_index: int = 0) -> DemoGuidance:
    """Return empty guidance when no demo is available."""
    return DemoGuidance(
        available=False,
        step_index=step_index,
        instruction="",
        action_type="",
        action_value="",
        target_description="",
        screenshot_path=None,
        next_screenshot_path=None,
        confidence=0.0,
        total_demo_steps=0,
    )


class DemoLibrary:
    """Directory-based demonstration library.

    Directory structure::

        library_dir/
          {task_id}/
            {demo_id}/
              demo.json          # Demo metadata + steps
              step_000.png       # Screenshot for step 0
              step_001.png       # Screenshot for step 1
              ...

    Args:
        library_dir: Root directory for storing demos.
        alignment_strategy: Strategy for visual alignment.  Accepts an
            ``AlignmentStrategy`` instance *or* a string name
            (``"phash"``, ``"clip"``, ``"hybrid"``, ``"openai"``).
            Defaults to ``PHashAlignmentStrategy`` when *imagehash* is
            installed.  The ``"openai"`` strategy falls back to pHash
            if ``OPENAI_API_KEY`` is not set.
        backward_penalty: Penalty weight for backward jumps in monotonic
            progress tracking.  Default ``0.3``.  Set to ``0.0`` to
            disable monotonic bias.
        low_confidence_threshold: Threshold below which alignment is
            considered "low confidence".  Default ``0.3``.
        max_consecutive_low_confidence: Number of consecutive
            low-confidence alignments before guidance is disabled.
            Default ``3``.  Set to ``0`` to disable adaptive disabling.
    """

    def __init__(
        self,
        library_dir: str = "demo_library",
        alignment_strategy: AlignmentStrategy | str | None = None,
        backward_penalty: float = _DEFAULT_BACKWARD_PENALTY,
        low_confidence_threshold: float = _DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        max_consecutive_low_confidence: int = _DEFAULT_MAX_CONSECUTIVE_LOW_CONFIDENCE,
    ):
        self.library_dir = Path(library_dir)
        self.library_dir.mkdir(parents=True, exist_ok=True)

        # Alignment strategy (pluggable)
        if isinstance(alignment_strategy, str):
            try:
                self._alignment_strategy: AlignmentStrategy | None = (
                    create_alignment_strategy(alignment_strategy)
                )
            except (ImportError, ValueError) as exc:
                logger.warning(
                    "Failed to create alignment strategy %r, "
                    "falling back to pHash: %s",
                    alignment_strategy,
                    exc,
                )
                if _HAS_IMAGEHASH:
                    self._alignment_strategy = PHashAlignmentStrategy()
                else:
                    self._alignment_strategy = None
        elif alignment_strategy is not None:
            self._alignment_strategy = alignment_strategy
        elif _HAS_IMAGEHASH:
            self._alignment_strategy = PHashAlignmentStrategy()
        else:
            self._alignment_strategy = None

        # Monotonic progress tracking
        self._backward_penalty = backward_penalty
        self._last_matched_step: dict[str, int] = {}

        # Adaptive guidance disabling
        self._low_confidence_threshold = low_confidence_threshold
        self._max_consecutive_low_confidence = max_consecutive_low_confidence
        self._consecutive_low_confidence: dict[str, int] = {}
        self._guidance_disabled: dict[str, bool] = {}

    def reset_alignment_state(self, task_id: str | None = None) -> None:
        """Reset per-episode alignment tracking state.

        Should be called between episodes to reset monotonic progress
        tracking and adaptive guidance disabling.  If *task_id* is
        provided, only resets state for that task.  Otherwise resets
        all tasks.

        Args:
            task_id: Optional task to reset.  If ``None``, resets all.
        """
        if task_id is not None:
            self._last_matched_step.pop(task_id, None)
            self._consecutive_low_confidence.pop(task_id, None)
            self._guidance_disabled.pop(task_id, None)
        else:
            self._last_matched_step.clear()
            self._consecutive_low_confidence.clear()
            self._guidance_disabled.clear()

    def add_demo(
        self,
        task_id: str,
        screenshots: list[Path | bytes],
        actions: list[BenchmarkAction],
        description: str = "",
        metadata: dict[str, Any] | None = None,
        descriptions: list[str] | None = None,
        auto_enrich: bool = False,
        resolution: tuple[int, int] | None = None,
    ) -> str:
        """Record a new demonstration for a task.

        Args:
            task_id: Task identifier.
            screenshots: List of screenshot paths or raw PNG bytes,
                one per action.
            actions: List of ``BenchmarkAction`` objects from the demo.
            description: Human-readable description of the demo.
            metadata: Optional extra metadata.
            descriptions: Optional list of VLM element descriptions,
                one per step.  If provided, must match the length of
                *screenshots*/*actions*.  Each string describes the UI
                element interacted with (e.g., ``"three-dot menu button
                in Chrome toolbar"``).
            auto_enrich: If ``True`` and *descriptions* is not provided,
                automatically call ``enrich_demo()`` after saving.
                Requires a VLM API key (OPENAI_API_KEY).  Default
                ``False``.
            resolution: Optional ``(width, height)`` of the demo's
                original screen resolution.  Stored in demo metadata
                for coordinate normalization at retrieval time.  If
                ``None``, coordinates are used as-is.

        Returns:
            The generated demo_id.
        """
        if len(screenshots) != len(actions):
            raise ValueError(
                f"screenshots ({len(screenshots)}) and actions "
                f"({len(actions)}) must have the same length"
            )
        if descriptions is not None and len(descriptions) != len(actions):
            raise ValueError(
                f"descriptions ({len(descriptions)}) must match actions "
                f"({len(actions)}) length"
            )

        demo_id = uuid.uuid4().hex[:12]
        demo_dir = self.library_dir / task_id / demo_id
        demo_dir.mkdir(parents=True, exist_ok=True)

        steps: list[DemoStep] = []
        for i, (screenshot, action) in enumerate(zip(screenshots, actions)):
            # Save screenshot
            screenshot_filename = f"step_{i:03d}.png"
            screenshot_path = demo_dir / screenshot_filename

            if isinstance(screenshot, (str, Path)):
                # Copy file
                import shutil

                shutil.copy2(str(screenshot), str(screenshot_path))
            elif isinstance(screenshot, bytes):
                screenshot_path.write_bytes(screenshot)
            else:
                raise TypeError(
                    f"screenshot must be Path or bytes, got {type(screenshot)}"
                )

            # Build action description
            from openadapt_evals.agents.base import action_to_string

            action_desc = action_to_string(action)

            step_description = ""
            if descriptions is not None:
                step_description = descriptions[i]

            step = DemoStep(
                step_index=i,
                screenshot_path=screenshot_filename,
                action_type=action.type,
                action_description=action_desc,
                target_description=action.target_name or "",
                action_value=action.text or action.key or action.answer or "",
                x=action.x,
                y=action.y,
                metadata={},
                description=step_description,
            )
            steps.append(step)

        # Build demo metadata with resolution info
        demo_metadata = dict(metadata) if metadata else {}
        if resolution is not None:
            demo_metadata["resolution"] = {
                "width": resolution[0],
                "height": resolution[1],
            }

        demo = Demo(
            task_id=task_id,
            demo_id=demo_id,
            description=description or f"Demo for {task_id}",
            steps=steps,
            metadata=demo_metadata,
        )

        # Save demo.json
        demo_json_path = demo_dir / "demo.json"
        with open(demo_json_path, "w") as f:
            json.dump(_demo_to_dict(demo), f, indent=2)

        logger.info(
            "Saved demo %s for task %s (%d steps)",
            demo_id,
            task_id,
            len(steps),
        )

        # Auto-enrich with VLM descriptions if requested
        if auto_enrich and descriptions is None:
            try:
                self.enrich_demo(task_id, demo_id=demo_id)
            except Exception as exc:
                logger.warning(
                    "Auto-enrich failed for demo %s (continuing without "
                    "descriptions): %s",
                    demo_id,
                    exc,
                )

        return demo_id

    def get_demo(self, task_id: str) -> Demo | None:
        """Get the most recent demo for a task.

        If multiple demos exist for a task, returns the most recently
        created one (by filesystem modification time).

        Args:
            task_id: Task identifier.

        Returns:
            Demo object or None if no demo exists.
        """
        task_dir = self.library_dir / task_id
        if not task_dir.is_dir():
            return None

        # Find all demo directories, sorted by modification time (newest first)
        demo_dirs = sorted(
            [d for d in task_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )

        for demo_dir in demo_dirs:
            demo_json = demo_dir / "demo.json"
            if not demo_json.exists():
                continue
            try:
                with open(demo_json) as f:
                    data = json.load(f)
                raw_steps = data.pop("steps", [])
                steps = []
                for s in raw_steps:
                    # Parse grounding_target dict into GroundingTarget object
                    gt_data = s.pop("grounding_target", None)
                    step = DemoStep(**s)
                    if gt_data and isinstance(gt_data, dict):
                        from openadapt_evals.grounding import GroundingTarget
                        step.grounding_target = GroundingTarget.from_dict(gt_data)
                    steps.append(step)
                return Demo(steps=steps, **data)
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.warning(
                    "Skipping invalid demo %s: %s",
                    demo_dir.name,
                    exc,
                )

        return None

    def list_demos(self, task_id: str) -> list[str]:
        """List all demo IDs for a task.

        Args:
            task_id: Task identifier.

        Returns:
            List of demo_id strings.
        """
        task_dir = self.library_dir / task_id
        if not task_dir.is_dir():
            return []
        return [
            d.name
            for d in task_dir.iterdir()
            if d.is_dir() and (d / "demo.json").exists()
        ]

    def list_tasks(self) -> list[str]:
        """List all task IDs that have at least one demo.

        Returns:
            List of task_id strings.
        """
        if not self.library_dir.is_dir():
            return []
        return [
            d.name
            for d in self.library_dir.iterdir()
            if d.is_dir()
            and any(
                (sub / "demo.json").exists()
                for sub in d.iterdir()
                if sub.is_dir()
            )
        ]

    def align_step(
        self,
        task_id: str,
        current_screenshot: bytes | None,
        step_index: int,
        current_resolution: tuple[int, int] | None = None,
        use_visual_alignment: bool = True,
    ) -> DemoGuidance:
        """Get demo guidance for a specific step.

        When *use_visual_alignment* is ``True`` and *current_screenshot*
        is provided, alignment uses the configured ``AlignmentStrategy``
        (default: pHash) to find the demo step whose screenshot is most
        similar to the current screen state.  This is critical when the
        agent takes a different number of steps than the demo --
        sequential step index alignment breaks, but the visual state
        still matches.

        Monotonic progress bias penalizes backward jumps to prevent
        oscillating alignment.  Adaptive guidance disabling turns off
        guidance when alignment confidence is consistently low.

        Falls back to sequential ``step_index`` alignment when visual
        alignment is disabled, no screenshot is provided, or no
        alignment strategy is available.

        When VLM-enriched descriptions are available, the returned
        instruction uses the description instead of raw coordinate
        notation.  For example, instead of ``CLICK(0.960, 0.066)`` the
        instruction becomes *"Click on three-dot menu button in Chrome
        toolbar at approximately (0.960, 0.066)"*.

        If *current_resolution* is provided and the demo has stored
        resolution metadata, coordinates are proportionally normalized
        so that guidance remains accurate across different screen sizes.

        Args:
            task_id: Task identifier.
            current_screenshot: Current screenshot bytes.  When provided
                with *use_visual_alignment=True*, enables visual
                demo step matching instead of sequential index.
            step_index: Current step index in the agent's execution.
                Used as fallback when visual alignment is disabled or
                unavailable.
            current_resolution: Optional ``(width, height)`` of the
                agent's current screen.  When provided and the demo has
                stored resolution metadata, coordinates are
                proportionally normalized.
            use_visual_alignment: Whether to use visual similarity for
                step alignment.  Defaults to ``True``.  Set to
                ``False`` to force sequential index alignment (original
                behavior).

        Returns:
            DemoGuidance with the demo's recommendation for this step.
        """
        # --- Check adaptive guidance disabling ------------------------------
        if self._guidance_disabled.get(task_id, False):
            logger.debug(
                "Demo guidance disabled for task %s (low confidence)",
                task_id,
            )
            return _empty_guidance(step_index)

        demo = self.get_demo(task_id)
        if demo is None:
            return _empty_guidance(step_index)

        total_steps = len(demo.steps)
        if total_steps == 0:
            return _empty_guidance(step_index)

        # --- Visual similarity alignment ------------------------------------
        visual_alignment_used = False
        visual_distance: float | None = None
        alignment_trace: AlignmentTraceEntry | None = None
        demo_dir = self._demo_dir(task_id, demo.demo_id)

        # Check if demo has real screenshots (not placeholders)
        _has_real_screenshots = any(
            s.screenshot_path and (demo_dir / s.screenshot_path).exists()
            and (demo_dir / s.screenshot_path).stat().st_size > 100
            for s in demo.steps
        )

        if (
            use_visual_alignment
            and current_screenshot is not None
            and self._alignment_strategy is not None
            and _has_real_screenshots
        ):
            t0 = time.monotonic()

            # Get last matched step for monotonic bias
            min_step = self._last_matched_step.get(task_id, 0)

            matched_idx, distance, meta = (
                self._alignment_strategy.find_closest_step(
                    current_screenshot,
                    demo,
                    demo_dir,
                    min_step=min_step,
                    backward_penalty=self._backward_penalty,
                )
            )

            elapsed_ms = (time.monotonic() - t0) * 1000.0

            step = demo.steps[matched_idx]
            visual_alignment_used = True
            visual_distance = float(distance)
            # Confidence: normalized distance 0 -> 1.0, distance 1.0 -> 0.0
            confidence = max(0.0, 1.0 - distance)

            # Build alignment trace
            method = meta.get("method", "unknown")
            alignment_trace = AlignmentTraceEntry(
                agent_step_index=step_index,
                matched_demo_step=matched_idx,
                distance=distance,
                confidence=confidence,
                method=method,
                visual_alignment_used=True,
                candidates_considered=len(demo.steps),
                elapsed_ms=elapsed_ms,
                backward_penalty_applied=meta.get(
                    "backward_penalty_applied", False
                ),
                metadata=meta,
            )

            # Update monotonic progress tracking
            self._last_matched_step[task_id] = matched_idx

            # Update adaptive guidance disabling
            if (
                self._max_consecutive_low_confidence > 0
                and confidence < self._low_confidence_threshold
            ):
                count = (
                    self._consecutive_low_confidence.get(task_id, 0) + 1
                )
                self._consecutive_low_confidence[task_id] = count
                if count >= self._max_consecutive_low_confidence:
                    logger.warning(
                        "Disabling demo guidance for task %s after %d "
                        "consecutive low-confidence alignments "
                        "(threshold=%.2f)",
                        task_id,
                        count,
                        self._low_confidence_threshold,
                    )
                    self._guidance_disabled[task_id] = True
                    alignment_trace.guidance_disabled = True
            else:
                self._consecutive_low_confidence[task_id] = 0

            logger.info(
                "Visual alignment: step_index=%d matched demo step %d "
                "(distance=%.3f, confidence=%.2f, method=%s) for task %s",
                step_index,
                matched_idx,
                distance,
                confidence,
                method,
                task_id,
            )
        elif step_index < total_steps:
            # Sequential alignment: use step_index directly
            step = demo.steps[step_index]
            confidence = 1.0
        else:
            # Past the end of the demo -- return the last step as context
            # with low confidence
            step = demo.steps[-1]
            confidence = 0.2
            logger.info(
                "Step %d exceeds demo length %d for task %s, "
                "returning last step with low confidence",
                step_index,
                total_steps,
                task_id,
            )

        # Resolve screenshot paths
        screenshot_path = (
            str(demo_dir / step.screenshot_path)
            if step.screenshot_path
            else None
        )

        next_screenshot_path = None
        if step.step_index + 1 < total_steps:
            next_step = demo.steps[step.step_index + 1]
            next_screenshot_path = (
                str(demo_dir / next_step.screenshot_path)
                if next_step.screenshot_path
                else None
            )

        # --- Resolution normalization ----------------------------------------
        norm_x = step.x
        norm_y = step.y
        if (
            current_resolution is not None
            and norm_x is not None
            and norm_y is not None
        ):
            demo_res = demo.metadata.get("resolution")
            if demo_res:
                demo_w = demo_res.get("width")
                demo_h = demo_res.get("height")
                cur_w, cur_h = current_resolution
                if demo_w and demo_h and demo_w > 0 and demo_h > 0:
                    norm_x = step.x * cur_w / demo_w
                    norm_y = step.y * cur_h / demo_h

        # --- Build instruction with VLM description -------------------------
        instruction = _build_enriched_instruction(
            step=step,
            normalized_x=norm_x,
            normalized_y=norm_y,
        )

        # Include alignment trace in guidance metadata
        guidance_metadata: dict[str, Any] = {}
        if alignment_trace is not None:
            guidance_metadata["alignment_trace"] = asdict(alignment_trace)

        # Pass the step's natural-language description so to_prompt_text()
        # can prefer it over coordinate-based instruction text.
        if step.description:
            guidance_metadata["description"] = step.description

        return DemoGuidance(
            available=True,
            step_index=step.step_index,
            instruction=instruction,
            action_type=step.action_type,
            action_value=step.action_value,
            target_description=step.target_description,
            screenshot_path=screenshot_path,
            next_screenshot_path=next_screenshot_path,
            confidence=confidence,
            total_demo_steps=total_steps,
            metadata=guidance_metadata,
            visual_alignment_used=visual_alignment_used,
            visual_distance=visual_distance,
        )

    def get_plan_overview(self, task_id: str) -> str:
        """Return a high-level strategy overview of the full demo.

        Instead of one step at a time, this gives the planner ALL demo
        steps as a numbered plan.  The planner can then use this as a
        strategy reference while making its own decisions based on the
        actual screenshot.

        Coordinates are intentionally omitted -- the planner should
        decide WHERE to click based on what it sees, not on hardcoded
        positions from a demo recorded on a different screen.

        Returns:
            Multi-line string suitable for injection into the planner
            prompt, or empty string if no demo exists.
        """
        demo = self.get_demo(task_id)
        if demo is None or not demo.steps:
            return ""

        lines = [
            "DEMONSTRATION STRATEGY (from a successful prior execution):",
            f"This task was previously completed in {len(demo.steps)} steps:",
        ]
        for step in demo.steps:
            # Prefer description (natural language) over action_description
            label = step.description or step.action_description
            action_hint = ""
            if step.action_type == "key" and step.action_value:
                action_hint = f" [{step.action_value}]"
            lines.append(f"  {step.step_index + 1}. {label}{action_hint}")

        lines.append("")
        lines.append(
            "Use this as a STRATEGY GUIDE. Adapt to the current screen state -- "
            "skip steps that are already done, and decide click targets based on "
            "what you actually see, not memorized coordinates."
        )
        return "\n".join(lines)

    def enrich_demo(
        self,
        task_id: str,
        demo_id: str | None = None,
        model: str = "gpt-4o-mini",
        provider: str = "openai",
        crop_radius: int = 80,
    ) -> None:
        """Generate VLM element descriptions for each click step in a demo.

        For each step with a ``click`` action and a screenshot, crops a
        region around the click coordinates and sends the full screenshot
        plus the cropped region to a VLM.  The VLM responds with a short
        description of the UI element at that location (e.g., *"three-dot
        menu button in Chrome toolbar"*).

        Descriptions are stored in each ``DemoStep.description`` field
        and persisted to ``demo.json``.

        This method is idempotent -- steps that already have a non-empty
        ``description`` are skipped.

        Args:
            task_id: Task identifier.
            demo_id: Specific demo to enrich.  If ``None``, enriches the
                most recent demo for the task.
            model: VLM model name for element description calls.
            provider: VLM provider (``"openai"`` or ``"anthropic"``).
            crop_radius: Pixel radius around the click point to crop
                for the detail image sent to the VLM.

        Raises:
            ValueError: If no demo is found for the task.
        """
        if demo_id is not None:
            demo_dir = self._demo_dir(task_id, demo_id)
            demo_json = demo_dir / "demo.json"
            if not demo_json.exists():
                raise ValueError(f"No demo.json found at {demo_dir}")
            with open(demo_json) as f:
                data = json.load(f)
            steps = [DemoStep(**s) for s in data.pop("steps", [])]
            demo = Demo(steps=steps, **data)
        else:
            demo = self.get_demo(task_id)
            if demo is None:
                raise ValueError(f"No demo found for task {task_id}")
            demo_dir = self._demo_dir(task_id, demo.demo_id)

        enriched_count = 0
        for step in demo.steps:
            # Skip non-click steps and steps already enriched
            if step.action_type != "click":
                continue
            if step.description:
                continue
            if step.x is None or step.y is None:
                continue
            if not step.screenshot_path:
                continue

            screenshot_file = demo_dir / step.screenshot_path
            if not screenshot_file.exists():
                logger.warning(
                    "Screenshot %s not found, skipping enrichment for "
                    "step %d",
                    screenshot_file,
                    step.step_index,
                )
                continue

            try:
                desc = _vlm_describe_element(
                    screenshot_path=screenshot_file,
                    click_x=step.x,
                    click_y=step.y,
                    model=model,
                    provider=provider,
                    crop_radius=crop_radius,
                )
                if desc:
                    step.description = desc
                    enriched_count += 1
                    logger.info(
                        "Step %d enriched: %s",
                        step.step_index,
                        desc,
                    )
            except Exception as exc:
                logger.warning(
                    "VLM enrichment failed for step %d: %s",
                    step.step_index,
                    exc,
                )

        # --- Pre-compute OpenAI embeddings if strategy is OpenAI -----------
        openai_enriched = 0
        if isinstance(self._alignment_strategy, OpenAIEmbeddingAlignment):
            try:
                openai_enriched = (
                    self._alignment_strategy.precompute_embeddings(
                        demo, demo_dir
                    )
                )
            except Exception as exc:
                logger.warning(
                    "OpenAI embedding pre-computation failed for demo "
                    "%s: %s",
                    demo.demo_id,
                    exc,
                )

        total_enriched = enriched_count + openai_enriched
        if total_enriched > 0:
            # Persist updated demo
            demo_json_path = demo_dir / "demo.json"
            with open(demo_json_path, "w") as f:
                json.dump(_demo_to_dict(demo), f, indent=2)
            logger.info(
                "Enriched %d steps (%d VLM descriptions, %d OpenAI "
                "embeddings) in demo %s for task %s",
                total_enriched,
                enriched_count,
                openai_enriched,
                demo.demo_id,
                task_id,
            )
        else:
            logger.info(
                "No steps needed enrichment in demo %s for task %s",
                demo.demo_id,
                task_id,
            )

    def _demo_dir(self, task_id: str, demo_id: str) -> Path:
        """Get the directory for a specific demo."""
        return self.library_dir / task_id / demo_id


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

# VLM prompt for element description enrichment.
_ENRICH_PROMPT = (
    "What UI element is at the highlighted location in the screenshot? "
    "Describe it in 5-10 words (e.g., 'three-dot menu button in Chrome "
    "toolbar'). Reply with ONLY the description, no extra text."
)


def _build_enriched_instruction(
    step: DemoStep,
    normalized_x: float | None = None,
    normalized_y: float | None = None,
) -> str:
    """Build a human-readable instruction from a DemoStep.

    When the step has a natural-language ``description``, returns it
    directly — the description already contains the action verb and
    target (e.g., *"Double-click Google Chrome icon"*).

    Coordinates from manual demos (hardcoded placeholders) are omitted
    because they don't correspond to real screen positions.  For VLM-
    enriched demos with real screenshots, coordinates are included as
    approximate hints.

    For steps without descriptions, falls back to the original
    ``action_description``.

    Args:
        step: The demo step to build an instruction for.
        normalized_x: X coordinate (potentially resolution-normalized).
        normalized_y: Y coordinate (potentially resolution-normalized).

    Returns:
        Human-readable instruction string.
    """
    if step.description:
        # If the description already reads as a complete sentence/
        # instruction (starts with a verb), use it directly.
        # Only append coordinates when we have real screenshots
        # (manual demos have empty screenshot_path and fake coords).
        if step.action_type == "click" and step.screenshot_path:
            if normalized_x is not None and normalized_y is not None:
                return (
                    f"{step.description} (approximately at "
                    f"({normalized_x:.3f}, {normalized_y:.3f}))"
                )
        return step.description

    # Fallback: original action description
    return step.action_description


def _vlm_describe_element(
    screenshot_path: Path,
    click_x: float,
    click_y: float,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
    crop_radius: int = 80,
) -> str:
    """Use a VLM to describe the UI element at a click location.

    Sends the full screenshot and a cropped region around the click
    point to the VLM, which returns a short element description.

    Args:
        screenshot_path: Path to the full screenshot PNG.
        click_x: Click X coordinate (normalized 0-1).
        click_y: Click Y coordinate (normalized 0-1).
        model: VLM model name.
        provider: VLM provider.
        crop_radius: Pixel radius for the crop around the click point.

    Returns:
        Element description string, or empty string on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "Pillow is not installed -- cannot crop screenshots for "
            "VLM enrichment.  Install with: pip install Pillow"
        )
        return ""

    # Load and crop the screenshot
    img = Image.open(screenshot_path)
    img_w, img_h = img.size

    # Convert normalized coordinates to pixel coordinates
    px = int(click_x * img_w)
    py = int(click_y * img_h)

    # Crop a region around the click point (clamped to image bounds)
    x1 = max(0, px - crop_radius)
    y1 = max(0, py - crop_radius)
    x2 = min(img_w, px + crop_radius)
    y2 = min(img_h, py + crop_radius)
    crop = img.crop((x1, y1, x2, y2))

    # Draw a crosshair on the crop to highlight the exact click point
    from PIL import ImageDraw

    draw = ImageDraw.Draw(crop)
    cx = px - x1  # click point relative to crop
    cy = py - y1
    line_color = (255, 0, 0)  # red
    draw.line([(cx - 10, cy), (cx + 10, cy)], fill=line_color, width=2)
    draw.line([(cx, cy - 10), (cx, cy + 10)], fill=line_color, width=2)

    # Convert both images to bytes
    import io

    full_buf = io.BytesIO()
    img.save(full_buf, format="PNG")
    full_bytes = full_buf.getvalue()

    crop_buf = io.BytesIO()
    crop.save(crop_buf, format="PNG")
    crop_bytes = crop_buf.getvalue()

    # VLM call
    from openadapt_evals.vlm import vlm_call

    raw = vlm_call(
        _ENRICH_PROMPT,
        images=[full_bytes, crop_bytes],
        model=model,
        provider=provider,
        max_tokens=64,
        temperature=0.0,
        cost_label="demo_enrich",
    )

    # Clean up the response (strip quotes, whitespace, periods)
    desc = raw.strip().strip('"').strip("'").strip(".").strip()
    return desc
