"""Tests for observe_pil() convenience method."""

from __future__ import annotations

import pytest

from openadapt_evals.adapters.rl_env import RLEnvironment
from openadapt_evals.adapters.waa.mock import WAAMockAdapter


def _make_env() -> RLEnvironment:
    adapter = WAAMockAdapter(num_tasks=3)
    task_id = adapter.list_tasks()[0].task_id
    return RLEnvironment(adapter, default_task_id=task_id)


class TestObservePil:
    def test_returns_pil_image(self):
        from PIL import Image

        env = _make_env()
        env.reset()
        img = env.observe_pil()
        assert isinstance(img, Image.Image)
        assert img.size == (1920, 1200)

    def test_does_not_advance_step_count(self):
        env = _make_env()
        env.reset()
        before = env._step_count
        env.observe_pil()
        assert env._step_count == before

    def test_raises_without_reset(self):
        env = _make_env()
        with pytest.raises(RuntimeError):
            env.observe_pil()

    def test_image_mode_is_rgb(self):
        env = _make_env()
        env.reset()
        img = env.observe_pil()
        assert img.mode == "RGB"

    def test_fallback_without_adapter_observe_pil(self):
        """RLEnvironment falls back when adapter lacks observe_pil()."""
        from PIL import Image

        adapter = WAAMockAdapter(num_tasks=3)
        assert not hasattr(adapter, "observe_pil")
        task_id = adapter.list_tasks()[0].task_id
        env = RLEnvironment(adapter, default_task_id=task_id)
        env.reset()
        img = env.observe_pil()
        assert isinstance(img, Image.Image)
