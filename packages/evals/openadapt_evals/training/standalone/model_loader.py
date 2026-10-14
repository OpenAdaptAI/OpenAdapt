"""HuggingFace + PEFT model loading for standalone GRPO. No openadapt-ml imports."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_model_and_processor(
    model_name: str,
    *,
    load_in_4bit: bool = True,
    lora_r: int = 16,
    lora_alpha: int = 32,
    lora_checkpoint: str | None = None,
) -> tuple[Any, Any]:
    """Load VLM with LoRA. Returns (model, processor)."""
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoProcessor

    try:
        from transformers import AutoModelForImageTextToText as AutoVLM
    except ImportError:
        from transformers import AutoModelForVision2Seq as AutoVLM

    processor = AutoProcessor.from_pretrained(model_name)
    load_kwargs: dict[str, Any] = {"torch_dtype": torch.bfloat16, "device_map": "auto"}
    if load_in_4bit:
        from transformers import BitsAndBytesConfig

        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_quant_type="nf4",
        )
    model = AutoVLM.from_pretrained(model_name, **load_kwargs)

    if lora_checkpoint:
        logger.info("Loading existing LoRA from %s", lora_checkpoint)
        model = PeftModel.from_pretrained(model, lora_checkpoint, is_trainable=True)
    else:
        lora_config = LoraConfig(
            r=lora_r, lora_alpha=lora_alpha,
            target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora_config)

    model.print_trainable_parameters()
    return model, processor
