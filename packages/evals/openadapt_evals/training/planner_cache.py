"""Cache planner API responses to reduce costs during GRPO training.

Uses perceptual hashing (pHash) so visually similar screenshots produce
cache hits even when pixel values differ slightly (e.g. anti-aliasing,
compression artifacts, minor cursor movement).

Falls back to MD5 when the ``imagehash`` library is not installed.

Usage::

    cache = PlannerCache(cache_dir=".planner_cache", hash_threshold=8)

    # Check cache before calling the planner API
    cached = cache.get(screenshot_bytes, task_instruction, action_history)
    if cached is not None:
        return cached

    # On miss, call the API and store the result
    result = call_planner_api(...)
    cache.put(screenshot_bytes, task_instruction, action_history, result)
"""

from __future__ import annotations

import hashlib
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import imagehash
    from PIL import Image

    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False


class PlannerCache:
    """Cache planner responses keyed by perceptual screenshot hash + task + history."""

    def __init__(self, cache_dir: str = ".planner_cache", hash_threshold: int = 8):
        """Initialise the cache.

        Args:
            cache_dir: Directory where cached JSON files are stored.
            hash_threshold: Maximum Hamming distance for perceptual hash
                matches.  Only used with ``imagehash``; ignored when
                falling back to MD5.
        """
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._hash_threshold = hash_threshold

        if _HAS_IMAGEHASH:
            logger.info("PlannerCache using perceptual hashing (threshold=%d)", hash_threshold)
        else:
            logger.info("PlannerCache using MD5 fallback (imagehash not installed)")

    # -- public API ----------------------------------------------------------

    def get(
        self,
        screenshot_bytes: bytes,
        task_instruction: str,
        action_history: list[str],
    ) -> dict[str, Any] | None:
        """Return cached planner output or ``None`` on miss."""
        key = self._cache_key(screenshot_bytes, task_instruction, action_history)
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            logger.debug("Cache MISS: %s", key)
            return None
        try:
            data = json.loads(path.read_text())
            logger.info("Cache HIT: %s", key)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cache read error for %s: %s", key, exc)
            return None

    def put(
        self,
        screenshot_bytes: bytes,
        task_instruction: str,
        action_history: list[str],
        planner_output: dict[str, Any],
    ) -> None:
        """Store planner output in cache."""
        key = self._cache_key(screenshot_bytes, task_instruction, action_history)
        path = self._cache_dir / f"{key}.json"
        try:
            path.write_text(json.dumps(planner_output, indent=2))
            logger.debug("Cache STORE: %s", key)
        except OSError as exc:
            logger.warning("Cache write error for %s: %s", key, exc)

    # -- internals -----------------------------------------------------------

    def _cache_key(
        self,
        screenshot_bytes: bytes,
        task_instruction: str,
        action_history: list[str],
    ) -> str:
        """Compute cache key from perceptual hash + task + history hash."""
        img_hash = self._screenshot_hash(screenshot_bytes)
        task_hash = hashlib.md5(task_instruction.encode()).hexdigest()[:12]
        history_hash = hashlib.md5(
            json.dumps(action_history).encode()
        ).hexdigest()[:12]
        return f"{img_hash}_{task_hash}_{history_hash}"

    @staticmethod
    def _screenshot_hash(screenshot_bytes: bytes) -> str:
        """Return a perceptual hash string, or MD5 hex digest as fallback."""
        if _HAS_IMAGEHASH:
            img = Image.open(BytesIO(screenshot_bytes))
            return str(imagehash.phash(img))
        return hashlib.md5(screenshot_bytes).hexdigest()
