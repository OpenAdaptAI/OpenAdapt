"""Tests for vision-safe loss computation in the standalone GRPO trainer.

These tests verify the fix for the Qwen3 vision merge attention mask crash.
The root cause: manually concatenating action_ids onto prompt input_ids
created inconsistent input that the model's vision merge couldn't handle.
The fix: process prompt + action as a single string through the processor.

No GPU, no model weights, no API keys required.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.heavy
torch = pytest.importorskip("torch")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tiny_png() -> bytes:
    """A minimal valid PNG image (10x10 red)."""
    from PIL import Image
    img = Image.new("RGB", (10, 10), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def mock_processor():
    """A mock processor that behaves like a Qwen VLM processor.

    Returns input_ids of predictable lengths so we can verify
    the action slicing math.
    """
    processor = MagicMock()

    # Tokenizer: every 4 characters = 1 token (deterministic, length-based)
    # This approximates BPE behavior better than splitting on spaces
    tokenizer = MagicMock()

    def _to_ids(text):
        n = max(1, len(text) // 4) if text else 0
        return list(range(100, 100 + n))

    def encode(text, add_special_tokens=False, return_tensors=None):
        ids = _to_ids(text)
        if return_tensors == "pt":
            return {"input_ids": torch.tensor([ids]) if ids else torch.zeros(1, 0, dtype=torch.long)}
        return ids

    tokenizer.side_effect = encode
    tokenizer.encode = lambda text, **kw: encode(text, **kw)
    processor.tokenizer = tokenizer

    def process(text=None, images=None, return_tensors=None):
        """Simulate processor: tokenize text, add vision tensors."""
        t = text[0] if isinstance(text, list) else text
        ids_list = _to_ids(t)
        ids = torch.tensor([ids_list]) if ids_list else torch.zeros(1, 0, dtype=torch.long)
        result = {
            "input_ids": ids,
            "attention_mask": torch.ones_like(ids),
        }
        if images is not None:
            # Simulate vision tensors — their exact shape doesn't matter,
            # what matters is they're CONSISTENT with the input_ids
            result["pixel_values"] = torch.randn(1, 3, 10, 10)
            result["image_grid_thw"] = torch.tensor([[1, 10, 10]])
        return result

    processor.side_effect = process
    processor.apply_chat_template = MagicMock(
        side_effect=lambda msgs, **kw: "prompt tokens here"
    )

    return processor


# ---------------------------------------------------------------------------
# Test 1: Processor consistency (unified vs manual concat)
# ---------------------------------------------------------------------------


class TestProcessorConsistency:
    """Verify processor(prompt+action) produces consistent inputs."""

    def test_unified_includes_action_tokens(self, mock_processor):
        """Full text through processor includes both prompt and action."""
        prompt = "You are a GUI automation agent. Given a screenshot and a goal, predict the next action."
        action = "Thought: I need to click the button.\nAction: CLICK(x=0.50, y=0.30)"
        full_text = prompt + action

        prompt_inputs = mock_processor(text=[prompt], images=["img"])
        full_inputs = mock_processor(text=[full_text], images=["img"])

        prompt_len = prompt_inputs["input_ids"].shape[1]
        full_len = full_inputs["input_ids"].shape[1]

        # Full text should be longer than prompt alone
        assert full_len > prompt_len, (
            f"Full input ({full_len}) should be longer than prompt ({prompt_len})"
        )

    def test_unified_has_consistent_vision_tensors(self, mock_processor):
        """Processor output has vision tensors consistent with input_ids."""
        full_text = "prompt tokens here CLICK(x=0.5,y=0.3)"
        inputs = mock_processor(text=[full_text], images=["img"])

        assert "input_ids" in inputs
        assert "pixel_values" in inputs
        assert "attention_mask" in inputs
        # Attention mask matches input_ids length
        assert inputs["attention_mask"].shape == inputs["input_ids"].shape

    def test_manual_concat_would_be_inconsistent(self, mock_processor):
        """Prove the old approach creates inconsistent inputs.

        The old code did:
          prompt_inputs = processor(prompt, image)
          full_ids = cat(prompt_inputs["input_ids"], action_ids)
          full_inputs = {**prompt_inputs, "input_ids": full_ids}

        This makes input_ids longer but pixel_values stay prompt-sized.
        The model's vision merge sees the mismatch.
        """
        prompt = "prompt tokens here"

        # Old approach: process prompt only
        prompt_inputs = mock_processor(text=[prompt], images=["img"])
        prompt_ids = prompt_inputs["input_ids"]

        # Manually add action tokens
        action_ids = torch.tensor([[200]])  # one action token
        old_full_ids = torch.cat([prompt_ids, action_ids], dim=1)

        # The inconsistency: input_ids is now longer than what the
        # processor produced pixel_values/attention_mask for
        assert old_full_ids.shape[1] > prompt_inputs["attention_mask"].shape[1], (
            "Manual concat makes input_ids longer than attention_mask — "
            "this is the root cause of the vision merge crash"
        )


# ---------------------------------------------------------------------------
# Test 2: Action logit slicing math
# ---------------------------------------------------------------------------


class TestActionLogitSlicing:
    """Verify the math for extracting action log-probs from output logits."""

    def test_slice_last_n_action_tokens(self):
        """Action logits are the last n_action positions in output."""
        vocab_size = 100
        seq_len = 20
        n_action = 3

        # Synthetic logits: shape (1, seq_len, vocab_size)
        logits = torch.randn(1, seq_len, vocab_size)

        # The trainer slices: logits[:, seq_len - n_action - 1 : seq_len - 1, :]
        al = logits[:, seq_len - n_action - 1: seq_len - 1, :]

        assert al.shape == (1, n_action, vocab_size), (
            f"Expected (1, {n_action}, {vocab_size}), got {al.shape}"
        )

    def test_gather_correct_token_logprobs(self):
        """Gathering log-probs for specific token IDs works correctly."""
        vocab_size = 10
        n_action = 3

        # Logits where token 5 has the highest score at each position
        logits = torch.zeros(1, n_action, vocab_size)
        logits[0, :, 5] = 10.0  # token 5 is strongly preferred

        lp = torch.nn.functional.log_softmax(logits, dim=-1)
        action_ids = torch.tensor([[5, 5, 5]])  # all token 5

        tlp = lp.gather(2, action_ids.unsqueeze(-1)).squeeze(-1)

        # Log-prob of the most likely token should be close to 0
        assert tlp.sum().item() > -1.0, (
            f"Log-prob sum should be near 0 for the most likely tokens, "
            f"got {tlp.sum().item()}"
        )

    def test_different_sequence_lengths_same_result(self):
        """Slicing from the end works regardless of total sequence length.

        This is the key property: after vision merge, seq_len may differ
        from input_ids length. Slicing from the END (not from prompt_len)
        always gets the right tokens.
        """
        vocab_size = 50
        n_action = 2

        # Same action logits at the end, different total lengths
        for seq_len in [10, 15, 20, 50]:
            logits = torch.randn(1, seq_len, vocab_size)
            # Put a known pattern at the end
            logits[0, -3, :] = 0.0  # position before action
            logits[0, -3, 42] = 99.0  # token 42 at this position

            al = logits[:, seq_len - n_action - 1: seq_len - 1, :]
            assert al.shape == (1, n_action, vocab_size)
            # First action position should strongly prefer token 42
            assert al[0, 0, 42].item() == 99.0


# ---------------------------------------------------------------------------
# Test 3: Synthetic vision-merging model (reproduces Qwen crash)
# ---------------------------------------------------------------------------


class TestVisionMergeCrash:
    """Reproduce the Qwen vision merge crash with a synthetic model.

    Qwen2.5-VL and Qwen3.5-VL replace image placeholder tokens with
    visual features of a DIFFERENT count, changing internal sequence
    length. If attention_mask is sized for pre-merge input_ids, crash.
    """

    @staticmethod
    def _make_vision_merge_model(vocab_size=200, placeholder_id=50, n_visual_features=7):
        import torch.nn as nn

        class VisionMergeModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.embed = nn.Embedding(vocab_size, 16)
                self.visual_embed = nn.Parameter(torch.randn(n_visual_features, 16))
                self.head = nn.Linear(16, vocab_size)
                self._ph = placeholder_id
                self._nv = n_visual_features

            def forward(self, input_ids, attention_mask=None, pixel_values=None, **kw):
                h = self.embed(input_ids)
                if pixel_values is not None:
                    mask = input_ids[0] == self._ph
                    n_ph = mask.sum().item()
                    if n_ph > 0:
                        keep = h[:, ~mask, :]
                        vis = self.visual_embed.unsqueeze(0)
                        idx = mask.nonzero(as_tuple=True)[0][0].item()
                        h = torch.cat([keep[:, :idx, :], vis, keep[:, idx:, :]], dim=1)
                if attention_mask is not None and attention_mask.shape[1] != h.shape[1]:
                    raise IndexError(
                        f"The shape of the mask [{attention_mask.shape[1]}] at index 0 "
                        f"does not match the shape of the indexed tensor [{h.shape[1]}] at index 0"
                    )

                class Out:
                    pass
                out = Out()
                out.logits = self.head(h)
                return out

        return VisionMergeModel()

    def test_manual_concat_crashes(self):
        """OLD approach: cat(prompt_ids, action_ids) + mask → crashes."""
        model = self._make_vision_merge_model()
        prompt_ids = torch.tensor([[10, 50, 50, 50, 20, 30]])
        action_ids = torch.tensor([[40, 41]])
        full_ids = torch.cat([prompt_ids, action_ids], dim=1)
        mask = torch.ones_like(full_ids)
        pv = torch.randn(1, 3, 10, 10)

        with pytest.raises(IndexError, match="shape of the mask"):
            model(input_ids=full_ids, attention_mask=mask, pixel_values=pv)

    def test_unified_processor_works(self):
        """NEW approach: no explicit mask → model handles merge."""
        model = self._make_vision_merge_model()
        full_ids = torch.tensor([[10, 50, 50, 50, 20, 30, 40, 41]])
        pv = torch.randn(1, 3, 10, 10)

        out = model(input_ids=full_ids, pixel_values=pv)
        # 8 - 3 placeholders + 7 features = 12
        assert out.logits.shape[1] == 12

    def test_no_vision_no_merge(self):
        """Without pixel_values, no merge, mask matches."""
        model = self._make_vision_merge_model()
        ids = torch.tensor([[10, 50, 50, 50, 20]])
        out = model(input_ids=ids, attention_mask=torch.ones_like(ids))
        assert out.logits.shape[1] == 5

    def test_exclude_strips_vision(self):
        """Exclude mode: no pixel_values passed, mask is safe."""
        model = self._make_vision_merge_model()
        ids = torch.tensor([[10, 50, 50, 50, 20, 30, 40, 41]])
        out = model(input_ids=ids, attention_mask=torch.ones_like(ids))
        assert out.logits.shape[1] == 8


# ---------------------------------------------------------------------------
# Test 4: _compute_rollout_loss integration
# ---------------------------------------------------------------------------


class TestComputeRolloutLossIntegration:
    """Test _compute_rollout_loss with a real tiny model (no mocks)."""

    @staticmethod
    def _make_tiny_model(vocab_size=200):
        """Real nn.Module — avoids MagicMock leaking into torch ops."""
        import torch.nn as nn

        class TinyVLM(nn.Module):
            def __init__(self):
                super().__init__()
                self.embed = nn.Embedding(vocab_size, 16)
                self.head = nn.Linear(16, vocab_size)

            def forward(self, input_ids, **kwargs):
                h = self.embed(input_ids)

                class Out:
                    pass

                out = Out()
                out.logits = self.head(h)
                return out

        return TinyVLM()

    def test_runs_without_crash(self, mock_processor, tiny_png):
        """The full loss computation runs end-to-end without error."""
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.standalone.trainer import GRPOTrainer
        from openadapt_evals.training.standalone.waa_direct import Rollout, RolloutStep

        config = TrainingConfig(vision_loss_mode="include")
        trainer = GRPOTrainer(config)
        trainer._processor = mock_processor
        trainer._config = config
        trainer._model = self._make_tiny_model()

        step = RolloutStep(
            screenshot=tiny_png,
            action=MagicMock(type="click", x=0.5, y=0.3),
            raw_text="CLICK(x=0.50, y=0.30)",
            reward=0.0,
        )
        rollout = Rollout(
            task_id="test", instruction="Click the button",
            steps=[step], reward=1.0,
        )

        loss = trainer._compute_rollout_loss(rollout, advantage=1.0, scale=1.0)
        assert isinstance(loss, float)
        assert loss != 0.0, "Loss should be non-zero with advantage=1.0"

    def test_exclude_mode_strips_vision_keys(self, mock_processor, tiny_png):
        """In exclude mode, vision tensors are not passed to the model."""
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.standalone.trainer import GRPOTrainer
        from openadapt_evals.training.standalone.waa_direct import Rollout, RolloutStep

        config = TrainingConfig(vision_loss_mode="exclude")
        trainer = GRPOTrainer(config)
        trainer._processor = mock_processor
        trainer._config = config

        model = self._make_tiny_model()
        captured = {}
        orig_forward = model.forward

        def spy_forward(input_ids, **kwargs):
            captured.update(kwargs)
            captured["input_ids_shape"] = input_ids.shape
            return orig_forward(input_ids, **kwargs)

        model.forward = spy_forward
        trainer._model = model

        step = RolloutStep(
            screenshot=tiny_png,
            action=MagicMock(type="click", x=0.5, y=0.3),
            raw_text="CLICK(x=0.50, y=0.30)", reward=0.0,
        )
        rollout = Rollout(
            task_id="test", instruction="Click the button",
            steps=[step], reward=1.0,
        )

        trainer._compute_rollout_loss(rollout, advantage=1.0, scale=1.0)

        assert "pixel_values" not in captured, "exclude mode should strip pixel_values"
        assert "image_grid_thw" not in captured, "exclude mode should strip image_grid_thw"

    def test_training_step_diagnostics(self, mock_processor, tiny_png):
        """Training step with reward variance produces diagnostic metrics.

        With 2 rollouts and rewards [0, 1], advantages are symmetric
        [-1, +1]. Per-rollout gradients may cancel (grad_norm≈0) because
        both rollouts saw the same input. This is expected with N=2.
        With more rollouts (N≥4), gradients would be asymmetric.

        The key assertion: loss_abs > 0 (each rollout contributes
        non-zero loss even though the sum cancels).
        """
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.standalone.trainer import GRPOTrainer
        from openadapt_evals.training.standalone.waa_direct import Rollout, RolloutStep

        config = TrainingConfig(vision_loss_mode="exclude")
        trainer = GRPOTrainer(config)
        trainer._processor = mock_processor
        trainer._config = config
        model = self._make_tiny_model()
        trainer._model = model
        trainer._optimizer = torch.optim.AdamW(
            [p for p in model.parameters() if p.requires_grad], lr=1e-3,
        )

        def make_rollout(reward):
            step = RolloutStep(
                screenshot=tiny_png,
                action=MagicMock(type="click", x=0.5, y=0.3),
                raw_text="CLICK(x=0.50, y=0.30)", reward=0.0,
            )
            return Rollout(
                task_id="test", instruction="Click the button",
                steps=[step], reward=reward,
            )

        rollouts = [make_rollout(0.0), make_rollout(1.0)]
        metrics = trainer._training_step(rollouts)

        # With 2 symmetric rollouts, grad_norm may be ≈0 (gradients cancel).
        # But absolute loss must be > 0 (each rollout contributes non-zero loss).
        assert metrics["loss_abs"] > 0, (
            f"|loss| should be > 0, got {metrics['loss_abs']}"
        )
        assert not metrics["skipped"], "Should not skip with reward variance"
        assert len(metrics["advantages"]) == 2, "Should have 2 advantage values"
        # Advantages should be symmetric: one positive, one negative
        advs = metrics["advantages"]
        assert advs[0] * advs[1] < 0, f"Advantages should have opposite signs: {advs}"
