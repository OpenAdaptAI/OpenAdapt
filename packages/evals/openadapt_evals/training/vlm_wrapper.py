"""VLM model patching for TRL compatibility.

TRL's GRPOTrainer was designed for text-only LLMs. It unwraps models
via Accelerate, which strips any external wrapper class. The fix:
patch the model's forward() method directly on the instance. This
survives unwrapping because it's on the model object, not a wrapper.

Two functions:
- ``patch_model_for_trl(model)``: patches model.forward to inject
  cached pixel_values. Returns a ``cache_vision_inputs`` callable.
- ``VLMModelWrapper``: legacy wrapper class (kept for backward compat,
  delegates to patch_model_for_trl internally).

Usage:
    from openadapt_evals.training.vlm_wrapper import patch_model_for_trl

    cache_fn = patch_model_for_trl(model)

    # During rollout generation:
    inputs = processor(text=..., images=[img], return_tensors="pt")
    cache_fn(inputs)  # cache pixel_values
    outputs = model.generate(**inputs, ...)  # model sees image ✓

    # During TRL's training forward pass:
    # TRL calls model.forward(input_ids=...) → patched forward injects
    # cached pixel_values automatically. Model sees image ✓
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def patch_model_for_trl(model: Any) -> Callable[[dict[str, Any]], None]:
    """Patch a VLM's forward() to auto-inject cached vision inputs.

    This patches the model instance directly (not a wrapper class),
    so it survives TRL/Accelerate unwrapping.

    Args:
        model: A HuggingFace VLM (may be a PeftModel).

    Returns:
        A ``cache_vision_inputs(inputs_dict)`` function. Call this during
        rollout generation to cache pixel_values for the training forward.
    """
    # Mutable state shared between cache_fn and patched forward
    _cache: dict[str, Any] = {}
    _logged_inject = [False]
    _logged_miss = [False]

    original_forward = model.forward

    def _patched_forward(input_ids: Any = None, **kwargs: Any) -> Any:
        """Forward with automatic vision input injection."""
        if "pixel_values" not in kwargs and _cache:
            for key, val in _cache.items():
                if key not in kwargs:
                    if hasattr(val, "to") and hasattr(input_ids, "device"):
                        kwargs[key] = val.to(input_ids.device)
                    else:
                        kwargs[key] = val
            if not _logged_inject[0]:
                _logged_inject[0] = True
                logger.info(
                    "VLM forward patch: injecting cached vision inputs "
                    "(keys=%s). TRL called forward() without pixel_values.",
                    list(_cache.keys()),
                )
        elif "pixel_values" not in kwargs and not _cache:
            if not _logged_miss[0]:
                _logged_miss[0] = True
                logger.warning(
                    "VLM forward patch: forward() called without pixel_values "
                    "and no cache. Model is blind. Call cache_fn() first.",
                )
        return original_forward(input_ids=input_ids, **kwargs)

    # Patch the model instance
    model.forward = _patched_forward

    # Also patch generate() — TRL calls model.generate(input_ids=...)
    # without pixel_values. HF's generate() calls forward() internally,
    # but pixel_values must also be in the generate() kwargs so HF can
    # pass them through prepare_inputs_for_generation() → forward().
    _logged_gen_inject = [False]
    original_generate = model.generate

    def _patched_generate(**kwargs: Any) -> Any:
        """Generate with automatic vision input injection."""
        if "pixel_values" not in kwargs and _cache:
            for key, val in _cache.items():
                if key not in kwargs:
                    input_ids = kwargs.get("input_ids")
                    if hasattr(val, "to") and input_ids is not None and hasattr(input_ids, "device"):
                        kwargs[key] = val.to(input_ids.device)
                    else:
                        kwargs[key] = val
            if not _logged_gen_inject[0]:
                _logged_gen_inject[0] = True
                logger.info(
                    "VLM generate patch: injecting cached vision inputs "
                    "(keys=%s). TRL called generate() without pixel_values.",
                    list(_cache.keys()),
                )
        return original_generate(**kwargs)

    model.generate = _patched_generate

    logger.info(
        "VLM patches installed on %s (forward + generate). Vision inputs "
        "will be auto-injected during all TRL model calls.",
        type(model).__name__,
    )

    def cache_vision_inputs(inputs: dict[str, Any]) -> None:
        """Cache vision tensors for injection into forward passes.

        Args:
            inputs: Dict from processor(text=..., images=...) or a dict
                with pixel_values and optionally image_grid_thw.
        """
        _cache.clear()
        for key in ("pixel_values", "image_grid_thw"):
            if key in inputs:
                val = inputs[key]
                if hasattr(val, "detach"):
                    _cache[key] = val.detach().clone()
                else:
                    _cache[key] = val
        if _cache:
            logger.debug("Cached vision inputs: keys=%s", list(_cache.keys()))

    return cache_vision_inputs


class VLMModelWrapper:
    """Legacy wrapper — delegates to patch_model_for_trl internally.

    Kept for backward compatibility with existing code that creates
    VLMModelWrapper(model). New code should use patch_model_for_trl()
    directly and pass the original model to TRL.
    """

    def __init__(self, model: Any):
        object.__setattr__(self, "_vlm_model", model)
        object.__setattr__(self, "_cache_fn", patch_model_for_trl(model))
        object.__setattr__(self, "_vision_cache", None)
        object.__setattr__(self, "_cache_hits", 0)
        object.__setattr__(self, "_cache_misses", 0)

        # PEFT isinstance compatibility
        try:
            from peft import PeftModel
            if isinstance(model, PeftModel):
                combined = type(
                    "VLMPeftModelWrapper",
                    (VLMModelWrapper, type(model)),
                    {
                        "forward": VLMModelWrapper.forward,
                        "generate": VLMModelWrapper.generate,
                        "__call__": VLMModelWrapper.__call__,
                        "cache_vision_inputs": VLMModelWrapper.cache_vision_inputs,
                        "__getattr__": VLMModelWrapper.__getattr__,
                    },
                )
                object.__setattr__(self, "__class__", combined)
        except (ImportError, Exception):
            pass

    def cache_vision_inputs(self, inputs: dict[str, Any]) -> None:
        cache_fn = object.__getattribute__(self, "_cache_fn")
        cache_fn(inputs)

    def forward(self, input_ids: Any = None, **kwargs: Any) -> Any:
        model = object.__getattribute__(self, "_vlm_model")
        return model.forward(input_ids=input_ids, **kwargs)

    def generate(self, **kwargs: Any) -> Any:
        model = object.__getattribute__(self, "_vlm_model")
        return model.generate(**kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.forward(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        model = object.__getattribute__(self, "_vlm_model")
        return getattr(model, name)

    def __setattr__(self, name: str, value: Any) -> None:
        model = object.__getattribute__(self, "_vlm_model")
        setattr(model, name, value)
