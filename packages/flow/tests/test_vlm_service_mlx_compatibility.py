"""Optional real MLX boundary; tiny random weights, no pretrained download.

Run on Apple Silicon with the service-mlx extra. This checks dependency and
image/generation API compatibility, not model quality or workflow admission.
"""

from __future__ import annotations

import io
import json
import platform
from pathlib import Path

import pytest
from PIL import Image, UnidentifiedImageError

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="the MLX service backend requires Apple Silicon",
)


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mx = pytest.importorskip("mlx.core", exc_type=ImportError)
    pytest.importorskip("mlx_vlm", exc_type=ImportError)
    from mlx.utils import tree_flatten
    from mlx_vlm.models.qwen3_vl import Model, ModelConfig
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast

    base = tmp_path_factory.mktemp("tiny-qwen3-vl")
    tokens = [
        "[UNK]",
        "[PAD]",
        "[EOS]",
        "<|image_pad|>",
        "<|video_pad|>",
        "<|vision_start|>",
        "<|vision_end|>",
    ] + [f"word{i}" for i in range(25)]
    raw = Tokenizer(
        models.WordLevel({t: i for i, t in enumerate(tokens)}, unk_token="[UNK]")
    )
    raw.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=raw,
        unk_token="[UNK]",
        pad_token="[PAD]",
        eos_token="[EOS]",
        additional_special_tokens=tokens[3:7],
    )
    tokenizer.chat_template = "{% for m in messages %}{% for c in m['content'] %}{% if c['type'] == 'image' %}<|vision_start|><|image_pad|><|vision_end|>{% elif c['type'] == 'text' %}{{ c['text'] }}{% endif %}{% endfor %}{% endfor %}"
    tokenizer.save_pretrained(base)
    config = {
        "model_type": "qwen3_vl",
        "architectures": ["Qwen3VLForConditionalGeneration"],
        "image_token_id": 3,
        "video_token_id": 4,
        "vision_start_token_id": 5,
        "vision_end_token_id": 6,
        "vision_token_id": 3,
        "eos_token_id": [2],
        "vocab_size": 32,
        "text_config": {
            "model_type": "qwen3_vl_text",
            "num_hidden_layers": 1,
            "hidden_size": 24,
            "intermediate_size": 48,
            "num_attention_heads": 2,
            "num_key_value_heads": 2,
            "rms_norm_eps": 1e-6,
            "vocab_size": 32,
            "head_dim": 12,
            "rope_theta": 10000.0,
            "max_position_embeddings": 512,
            "rope_scaling": {"type": "default", "mrope_section": [2, 2, 2]},
            "tie_word_embeddings": True,
        },
        "vision_config": {
            "model_type": "qwen3_vl",
            "depth": 1,
            "hidden_size": 16,
            "intermediate_size": 32,
            "out_hidden_size": 24,
            "num_heads": 2,
            "patch_size": 16,
            "spatial_patch_size": 16,
            "spatial_merge_size": 2,
            "temporal_patch_size": 2,
            "num_position_embeddings": 16,
            "deepstack_visual_indexes": [],
            "fullatt_block_indexes": [0],
        },
    }
    (base / "config.json").write_text(json.dumps(config))
    (base / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "image_processor_type": "Qwen3VLImageProcessor",
                "patch_size": 16,
                "temporal_patch_size": 2,
                "merge_size": 2,
                "min_pixels": 1024,
                "max_pixels": 1024,
            }
        )
    )
    (base / "processor_config.json").write_text(
        json.dumps({"processor_class": "Qwen3VLProcessor"})
    )
    mx.random.seed(17)
    model = Model(ModelConfig.from_dict(config))
    weights = dict(tree_flatten(model.parameters()))
    mx.eval(weights)
    mx.save_safetensors(str(base / "model.safetensors"), weights)

    return base


@pytest.mark.parametrize("image_count", [1, 2])
def test_real_mlx_load_process_generate_and_cleanup(
    tiny_model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_count: int
) -> None:
    from openadapt_flow.services.vlm_service.backends import MLXBackend

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    scratch = tmp_path / "scratch"
    backend = MLXBackend(str(tiny_model), tmp_dir=scratch).load()
    assert backend.is_ready()
    assert scratch.stat().st_mode & 0o777 == 0o700
    png = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(png, format="PNG")
    result = backend.generate("word1", [png.getvalue()] * image_count, max_tokens=2)
    assert isinstance(result, str)
    assert result.strip()
    assert list(scratch.iterdir()) == []
    # Exercise the real processor's refusal and the backend's finally path.
    with pytest.raises(ValueError, match="Failed to load image") as error:
        backend.generate("word1", [b"invalid PNG"], max_tokens=2)
    assert isinstance(error.value.__cause__, UnidentifiedImageError)
    assert list(scratch.iterdir()) == []
