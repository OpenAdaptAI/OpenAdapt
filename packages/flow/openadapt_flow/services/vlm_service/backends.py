"""Pluggable inference backends for the VLM service.

The service is agnostic to *how* the open VLM runs; it only needs a single text
primitive:

    generate(prompt, images, max_tokens) -> str

Endpoint logic (prompt building, image composition, veto parsing) lives in the
app layer, so swapping the backend never changes the safety semantics.

Backends
--------
* :class:`StubBackend`   -- no model; deterministic canned text. Used by CI and
  as a runnable default so the server boots on any machine (fails SAFE: its
  default answers parse to the halt direction).
* :class:`MLXBackend`    -- Apple-Silicon dev backend. Loads
  ``mlx-community/Qwen3-VL-4B-Instruct-4bit`` exactly as the PR #28 identity
  probe does (``mlx_vlm.load`` + ``apply_chat_template`` + ``generate`` at
  temperature 0). Lets this Mac serve the fleet for local testing.
* :class:`VLLMBackend`   -- production Linux GPU box. Talks to a vLLM /
  SGLang OpenAI-compatible ``/v1/chat/completions`` endpoint (GUI-Owl-1.5-8B or
  Qwen3-VL-4B), passing images as base64 ``data:`` URLs.

Select via config (``VLM_BACKEND`` / ``ServiceConfig.backend``):
``stub`` | ``mlx`` | ``vllm``.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

# Default open model ids (see .private/vlm_identity_verification_2026_07_12.md).
MLX_DEFAULT_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
VLLM_DEFAULT_MODEL = "mPLUG/GUI-Owl-1.5-8B-Instruct"


@runtime_checkable
class InferenceBackend(Protocol):
    """A loaded open VLM exposing one multimodal text-generation primitive."""

    name: str
    model: Optional[str]

    def load(self) -> "InferenceBackend":
        """Load weights / open the connection. Idempotent."""
        ...

    def is_ready(self) -> bool:
        """True once :meth:`load` has completed and the backend can serve."""
        ...

    def generate(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        """Run the model on ``prompt`` + PNG ``images``; return the raw text."""
        ...


class StubBackend:
    """Deterministic no-model backend (CI + safe default).

    Returns canned text keyed by a substring of the prompt. Defaults are the
    SAFE direction so that a server booted without a real model never fabricates
    a confident SAME / a click point / a satisfied postcondition.
    """

    name = "stub"

    def __init__(self, responses: Optional[dict[str, str]] = None) -> None:
        # Keys are matched as case-insensitive substrings of the prompt.
        self._responses = responses or {}
        self.model = "stub"
        self._ready = False

    def load(self) -> "StubBackend":
        self._ready = True
        return self

    def is_ready(self) -> bool:
        return self._ready

    def generate(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        low = prompt.lower()
        for key, val in self._responses.items():
            if key.lower() in low:
                return val
        # Safe fallbacks: never a confident SAME / a point / a YES.
        if "same or different" in low or "same sequence" in low:
            return "DIFFERENT"
        if "json object of pixel coordinates" in low:
            return '{"x": null, "y": null}'
        return "UNCERTAIN"


class MLXBackend:
    """Apple-Silicon dev backend (mlx-vlm), mirroring the PR #28 probe loader."""

    name = "mlx"

    def __init__(
        self, model: str = MLX_DEFAULT_MODEL, tmp_dir: Optional[Path] = None
    ) -> None:
        self.model = model
        self._model = None
        self._processor = None
        self._config = None
        # PHI no-retention: mlx-vlm requires image FILE paths, so crop/screenshot
        # bytes must transit disk. Use a PRIVATE, per-instance scratch dir (mode
        # 0700, not a world-readable shared /tmp path) and delete every file the
        # instant inference returns (try/finally below). See docs/deployment/
        # ON_PREM_VLM.md ("No retention of crops or VLM payloads").
        self._tmp = Path(tmp_dir) if tmp_dir else None

    def load(self) -> "MLXBackend":
        import tempfile

        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        self._model, self._processor = load(self.model)
        self._config = load_config(self.model)
        if self._tmp is None:
            self._tmp = Path(tempfile.mkdtemp(prefix="openadapt_vlm_service_"))
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    def is_ready(self) -> bool:
        return self._model is not None

    def generate(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        paths: list[str] = []
        try:
            for i, png in enumerate(images):
                p = self._tmp / f"_img_{i}_{time.time_ns()}.png"
                p.write_bytes(png)
                p.chmod(0o600)
                paths.append(str(p))
            formatted = apply_chat_template(
                self._processor, self._config, prompt, num_images=len(paths)
            )
            res = generate(
                self._model,
                self._processor,
                formatted,
                image=paths,
                max_tokens=max_tokens,
                temperature=0.0,
                verbose=False,
            )
            return res.text if hasattr(res, "text") else str(res)
        finally:
            # No-retention: delete the PHI-bearing crops even if inference
            # raised (the pre-fix code skipped cleanup on any error).
            for p in paths:
                try:
                    Path(p).unlink()
                except OSError:
                    pass


class VLLMBackend:
    """Production backend: an OpenAI-compatible vLLM / SGLang server.

    On the customer GPU box, serve the open model, e.g.::

        vllm serve mPLUG/GUI-Owl-1.5-8B-Instruct --port 8000

    and point this backend at it (``VLM_VLLM_URL=http://localhost:8000/v1``).
    Images are sent as base64 ``data:image/png;base64,...`` URLs, temperature 0.
    """

    name = "vllm"

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        model: str = VLLM_DEFAULT_MODEL,
        api_key: str = "EMPTY",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._client = None

    def load(self) -> "VLLMBackend":
        import httpx

        self._client = httpx.Client(timeout=self._timeout)
        return self

    def is_ready(self) -> bool:
        return self._client is not None

    def generate(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        if self._client is None:
            self.load()
        content: list[dict] = []
        for png in images:
            b64 = base64.standard_b64encode(png).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
        content.append({"type": "text", "text": prompt})
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def build_backend(
    backend: str, model: Optional[str] = None, **kwargs
) -> InferenceBackend:
    """Construct (but do not load) a backend by name."""
    backend = (backend or "stub").lower()
    if backend == "stub":
        return StubBackend(**kwargs)
    if backend == "mlx":
        return MLXBackend(model or MLX_DEFAULT_MODEL, **kwargs)
    if backend == "vllm":
        return VLLMBackend(model=model or VLLM_DEFAULT_MODEL, **kwargs)
    raise ValueError(f"unknown backend: {backend!r} (use stub|mlx|vllm)")


def compose_identifier_pair(png_a: bytes, png_b: bytes) -> bytes:
    """Stack two identifier crops on one A/B-labelled canvas for the comparator.

    Verbatim behaviour of ``vlm_identity_probe.compose_pair``: a single labelled
    image keeps the same/different framing unambiguous and matches how the
    validated probe presented the crops.
    """
    from PIL import Image, ImageDraw

    top = Image.open(io.BytesIO(png_a)).convert("RGB")
    bot = Image.open(io.BytesIO(png_b)).convert("RGB")
    pad, gap, lblw = 24, 40, 44
    w = max(top.width, bot.width) + 2 * pad + lblw
    h = top.height + bot.height + 2 * pad + gap
    canvas = Image.new("RGB", (w, h), (245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    canvas.paste(top, (pad + lblw, pad))
    canvas.paste(bot, (pad + lblw, pad + top.height + gap))
    draw.text((pad, pad + top.height // 2), "A", fill=(20, 20, 20))
    draw.text((pad, pad + top.height + gap + bot.height // 2), "B", fill=(20, 20, 20))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
