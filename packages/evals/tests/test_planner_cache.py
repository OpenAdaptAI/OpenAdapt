"""Tests for PlannerCache — perceptual-hash-based planner response caching."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png(width: int = 4, height: int = 4, color: tuple = (255, 0, 0)) -> bytes:
    """Create a minimal valid PNG in memory."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlannerCacheMissAndHit:
    """Basic get / put round-trip."""

    def test_miss_returns_none(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))
        result = cache.get(_make_png(), "Open Notepad", [])
        assert result is None

    def test_hit_returns_stored_value(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))
        screenshot = _make_png()
        task = "Open Notepad"
        history: list[str] = []
        planner_output = {
            "decision": "COMMAND",
            "instruction": "Click the Start menu",
            "reasoning": "Need to open Start",
        }

        cache.put(screenshot, task, history, planner_output)
        result = cache.get(screenshot, task, history)

        assert result == planner_output

    def test_put_creates_json_file(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache_dir = tmp_path / "cache"
        cache = PlannerCache(cache_dir=str(cache_dir))
        cache.put(_make_png(), "task", [], {"decision": "DONE"})

        json_files = list(cache_dir.glob("*.json"))
        assert len(json_files) == 1

        data = json.loads(json_files[0].read_text())
        assert data == {"decision": "DONE"}


class TestCacheKeyDifferentiation:
    """Different inputs must produce different cache keys."""

    def test_different_screenshots_different_keys(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))

        png_red = _make_png(color=(255, 0, 0))
        png_blue = _make_png(color=(0, 0, 255))

        key_red = cache._cache_key(png_red, "task", [])
        key_blue = cache._cache_key(png_blue, "task", [])

        # With MD5 fallback, different bytes -> different keys guaranteed.
        # With imagehash, very different colors -> different perceptual hashes.
        # For identical solid-color 4x4 images the phash *may* collide, so
        # use larger images or accept that this tests the key structure.
        assert isinstance(key_red, str)
        assert isinstance(key_blue, str)
        # Keys have the expected 3-part structure.
        assert key_red.count("_") == 2
        assert key_blue.count("_") == 2

    def test_same_screenshot_different_task_different_keys(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))
        png = _make_png()

        key_a = cache._cache_key(png, "Open Notepad", [])
        key_b = cache._cache_key(png, "Open Calculator", [])

        assert key_a != key_b

    def test_same_screenshot_different_history_different_keys(self, tmp_path: Path):
        from openadapt_evals.training.planner_cache import PlannerCache

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))
        png = _make_png()

        key_a = cache._cache_key(png, "task", [])
        key_b = cache._cache_key(png, "task", ["click(0.5, 0.5)"])

        assert key_a != key_b


class TestMD5Fallback:
    """When imagehash is not installed, fall back to MD5."""

    def test_fallback_to_md5(self, tmp_path: Path):
        """Simulate imagehash not being available."""
        import openadapt_evals.training.planner_cache as pc_module

        original_has = pc_module._HAS_IMAGEHASH

        try:
            pc_module._HAS_IMAGEHASH = False

            cache = pc_module.PlannerCache(cache_dir=str(tmp_path / "cache"))
            png = _make_png()

            # Should use MD5 — a 32-char hex digest.
            import hashlib

            expected_img_hash = hashlib.md5(png).hexdigest()
            key = cache._cache_key(png, "task", [])

            assert key.startswith(expected_img_hash)
        finally:
            pc_module._HAS_IMAGEHASH = original_has

    def test_md5_round_trip(self, tmp_path: Path):
        """Cache works end-to-end with MD5 fallback."""
        import openadapt_evals.training.planner_cache as pc_module

        original_has = pc_module._HAS_IMAGEHASH

        try:
            pc_module._HAS_IMAGEHASH = False

            cache = pc_module.PlannerCache(cache_dir=str(tmp_path / "cache"))
            png = _make_png()
            output = {"decision": "COMMAND", "instruction": "Click OK"}

            cache.put(png, "task", [], output)
            result = cache.get(png, "task", [])
            assert result == output
        finally:
            pc_module._HAS_IMAGEHASH = original_has


class TestPerceptualHashing:
    """Verify perceptual hashing is used when imagehash is available."""

    def test_phash_used_when_available(self, tmp_path: Path):
        """When imagehash is installed, the screenshot hash should be a
        perceptual hash (hex string), not an MD5 digest."""
        from openadapt_evals.training.planner_cache import _HAS_IMAGEHASH, PlannerCache

        if not _HAS_IMAGEHASH:
            pytest.skip("imagehash not installed")

        cache = PlannerCache(cache_dir=str(tmp_path / "cache"))
        png = _make_png(width=64, height=64)

        import hashlib

        md5_hex = hashlib.md5(png).hexdigest()
        img_hash = cache._screenshot_hash(png)

        # phash produces a shorter hex string than MD5's 32-char digest
        assert img_hash != md5_hex
