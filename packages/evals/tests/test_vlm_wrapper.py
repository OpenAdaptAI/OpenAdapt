"""Tests for VLM model patching — multimodal TRL compatibility.

Tests both the new patch_model_for_trl() function (direct patching)
and the legacy VLMModelWrapper class (backward compat).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openadapt_evals.training.vlm_wrapper import VLMModelWrapper, patch_model_for_trl


class _FakeModel:
    """Minimal model with a forward() method that records kwargs."""

    def __init__(self):
        self.last_forward_kwargs = {}
        self.config = MagicMock(name="config")
        self._params = [MagicMock(name="param")]

    def forward(self, input_ids=None, **kwargs):
        self.last_forward_kwargs = {"input_ids": input_ids, **kwargs}

        class Out:
            logits = "fake_logits"
        return Out()

    def __call__(self, input_ids=None, **kwargs):
        return self.forward(input_ids=input_ids, **kwargs)

    def generate(self, **kwargs):
        return "generated_text"

    def parameters(self):
        return self._params


# ---------------------------------------------------------------------------
# patch_model_for_trl tests (the primary API)
# ---------------------------------------------------------------------------


class TestPatchModelForTRL:

    def test_injects_cached_pixel_values(self):
        """Patched forward injects cached vision inputs."""
        model = _FakeModel()
        cache_fn = patch_model_for_trl(model)

        cache_fn({"pixel_values": "fake_pv", "image_grid_thw": "fake_thw"})
        model.forward(input_ids="fake_ids")

        assert model.last_forward_kwargs["pixel_values"] == "fake_pv"
        assert model.last_forward_kwargs["image_grid_thw"] == "fake_thw"

    def test_does_not_override_existing_pixel_values(self):
        """Explicit pixel_values are not overridden by cache."""
        model = _FakeModel()
        cache_fn = patch_model_for_trl(model)

        cache_fn({"pixel_values": "cached_pv"})
        model.forward(input_ids="fake_ids", pixel_values="explicit_pv")

        assert model.last_forward_kwargs["pixel_values"] == "explicit_pv"

    def test_warns_without_cache(self, caplog):
        """Forward without cache logs a warning."""
        model = _FakeModel()
        patch_model_for_trl(model)

        import logging
        with caplog.at_level(logging.WARNING):
            model.forward(input_ids="fake_ids")

        assert "no cache" in caplog.text.lower() or "blind" in caplog.text.lower()

    def test_cache_overwrites(self):
        """New cache replaces old cache."""
        model = _FakeModel()
        cache_fn = patch_model_for_trl(model)

        cache_fn({"pixel_values": "old"})
        cache_fn({"pixel_values": "new"})
        model.forward(input_ids="ids")

        assert model.last_forward_kwargs["pixel_values"] == "new"

    def test_ignores_non_vision_keys(self):
        """Only pixel_values and image_grid_thw are cached."""
        model = _FakeModel()
        cache_fn = patch_model_for_trl(model)

        cache_fn({"pixel_values": "pv", "input_ids": "should_not_cache"})
        model.forward(input_ids="ids")

        assert model.last_forward_kwargs["pixel_values"] == "pv"
        assert model.last_forward_kwargs.get("input_ids") == "ids"

    def test_call_also_injects(self):
        """model() (via __call__) also gets injection."""
        model = _FakeModel()
        cache_fn = patch_model_for_trl(model)

        cache_fn({"pixel_values": "pv"})
        model(input_ids="ids")

        assert model.last_forward_kwargs["pixel_values"] == "pv"

    def test_generate_unaffected(self):
        """generate() is not intercepted (uses pixel_values from caller)."""
        model = _FakeModel()
        patch_model_for_trl(model)

        result = model.generate(input_ids="test")
        assert result == "generated_text"

    def test_attributes_preserved(self):
        """Model attributes remain accessible after patching."""
        model = _FakeModel()
        patch_model_for_trl(model)

        assert model.config is not None
        assert model.parameters() == model._params

    def test_peft_attributes_accessible(self):
        """PEFT attributes remain accessible after patching."""
        model = _FakeModel()
        model.peft_config = {"default": "lora"}
        model.active_adapter = "default"
        patch_model_for_trl(model)

        assert model.peft_config == {"default": "lora"}
        assert model.active_adapter == "default"

    def test_isinstance_preserved(self):
        """isinstance checks still work after patching."""
        model = _FakeModel()
        patch_model_for_trl(model)

        assert isinstance(model, _FakeModel)


# ---------------------------------------------------------------------------
# VLMModelWrapper legacy tests
# ---------------------------------------------------------------------------


class TestVLMModelWrapperLegacy:

    def test_cache_and_forward(self):
        """Legacy wrapper caches and delegates forward."""
        model = _FakeModel()
        wrapper = VLMModelWrapper(model)

        wrapper.cache_vision_inputs({"pixel_values": "pv"})
        # The patch is on the model, so calling model.forward injects
        wrapper.forward(input_ids="ids")

        assert model.last_forward_kwargs["pixel_values"] == "pv"

    def test_attribute_delegation(self):
        """Wrapper delegates attributes to model."""
        model = _FakeModel()
        wrapper = VLMModelWrapper(model)

        assert wrapper.config == model.config

    def test_generate_delegation(self):
        """Wrapper delegates generate to model."""
        model = _FakeModel()
        wrapper = VLMModelWrapper(model)

        assert wrapper.generate(input_ids="x") == "generated_text"


# ---------------------------------------------------------------------------
# Real e2e tests (requires torch — skipped in CI)
# ---------------------------------------------------------------------------


@pytest.mark.heavy
class TestPatchModelE2E:

    @staticmethod
    def _make_tiny_vlm():
        pytest.importorskip("torch")
        import torch.nn as nn

        class TinyVLM(nn.Module):
            def __init__(self):
                super().__init__()
                self.embed = nn.Embedding(100, 16)
                self.vision_proj = nn.Linear(3, 16)
                self.head = nn.Linear(16, 100)

            def forward(self, input_ids, pixel_values=None, **kwargs):
                h = self.embed(input_ids)
                if pixel_values is not None:
                    vis = self.vision_proj(pixel_values.float().mean(dim=(-2, -1)))
                    if vis.dim() == 2:
                        vis = vis.unsqueeze(1)
                    h = h + vis[:, :h.shape[1], :]
                class Out:
                    pass
                out = Out()
                out.logits = self.head(h)
                return out

        return TinyVLM()

    def test_patched_forward_changes_logits(self):
        """Cached pixel_values change logits via patched forward."""
        torch = pytest.importorskip("torch")

        model = self._make_tiny_vlm()
        cache_fn = patch_model_for_trl(model)

        input_ids = torch.tensor([[1, 2, 3, 4, 5]])

        # Blind forward
        out_blind = model(input_ids=input_ids)
        logits_blind = out_blind.logits.detach().clone()

        # Cache vision + forward
        cache_fn({"pixel_values": torch.randn(1, 3, 10, 10)})
        out_vision = model(input_ids=input_ids)
        logits_vision = out_vision.logits.detach()

        assert not torch.allclose(logits_blind, logits_vision, atol=1e-6), (
            "Patched forward must produce different logits with pixel_values."
        )

    def test_cache_survives_multiple_calls(self):
        """Cache persists across multiple forward calls."""
        torch = pytest.importorskip("torch")

        model = self._make_tiny_vlm()
        cache_fn = patch_model_for_trl(model)

        input_ids = torch.tensor([[1, 2, 3]])
        cache_fn({"pixel_values": torch.randn(1, 3, 10, 10)})

        out1 = model(input_ids=input_ids)
        out2 = model(input_ids=input_ids)
        assert torch.allclose(out1.logits, out2.logits, atol=1e-6)
