"""Shared VLM call utilities.

Provides a unified interface for calling vision-language models across
different providers (OpenAI, Anthropic) and via consilium multi-model
council.  Also includes general-purpose JSON extraction from LLM output.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

_DEFAULT_TIMEOUT = 120  # seconds — prevents indefinite hangs on API calls


try:
    from openadapt_evals.integrations.weave_integration import weave_op
except ImportError:
    weave_op = lambda fn: fn  # noqa: E731


@weave_op
def vlm_call(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    system: str = "",
    model: str = "gpt-4.1-mini",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout: int = _DEFAULT_TIMEOUT,
    use_council: bool = False,
    provider: str = "openai",
    cost_label: str = "",
) -> str:
    """Send a VLM query with optional images.

    When *use_council* is True, queries multiple LLMs via consilium in
    Stage-1-only mode (``skip_review``) for fast multi-model consensus.
    Falls back to a single-model call via *provider* if consilium is
    unavailable or errors.

    Args:
        prompt: Text prompt to send to the VLM.
        images: Optional list of raw PNG image bytes.
        system: Optional system prompt.
        model: Model name.
        max_tokens: Maximum response tokens.
        temperature: Sampling temperature.
        timeout: Request timeout in seconds.
        use_council: Whether to use consilium multi-model council.
        provider: VLM provider for direct calls (``"openai"`` or
            ``"anthropic"``).
        cost_label: Optional label for cost tracking (e.g., ``"planner"``,
            ``"grounder"``, ``"vlm_judge"``). Helps categorize costs in
            the summary.

    Returns:
        Model response text.
    """
    if use_council:
        try:
            from consilium import council_query

            result = council_query(
                prompt,
                images=images,
                system=system or None,
                skip_review=True,
                budget=0.50,
            )
            return result["final_answer"]
        except ImportError:
            print("  (consilium not installed -- falling back to single-model)")
        except Exception as e:
            print(f"  (consilium failed: {e} -- falling back to single-model)")

    if provider == "openai":
        return _vlm_call_openai(
            prompt,
            images=images,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            cost_label=cost_label,
        )
    elif provider in ("anthropic", "claude"):
        return _vlm_call_anthropic(
            prompt,
            images=images,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            cost_label=cost_label,
        )
    else:
        raise ValueError(f"Unsupported provider: {provider}")


def _vlm_call_openai(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    system: str = "",
    model: str = "gpt-4.1-mini",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout: int = _DEFAULT_TIMEOUT,
    cost_label: str = "",
) -> str:
    """Single-model OpenAI call."""
    import openai

    content: list[dict] = [{"type": "text", "text": prompt}]
    if images:
        for img_bytes in images:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
            })

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    client = openai.OpenAI(timeout=timeout)
    # GPT-5.x models require max_completion_tokens instead of max_tokens
    token_param = (
        {"max_completion_tokens": max_tokens}
        if model.startswith(("gpt-5", "o1", "o3", "o4"))
        else {"max_tokens": max_tokens}
    )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        **token_param,
    )

    # Track cost from response usage
    _track_response_cost(
        model=model,
        input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        label=cost_label,
    )

    return resp.choices[0].message.content


def _vlm_call_anthropic(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    system: str = "",
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout: int = _DEFAULT_TIMEOUT,
    cost_label: str = "",
) -> str:
    """Single-model Anthropic call."""
    import anthropic

    content: list[dict] = [{"type": "text", "text": prompt}]
    if images:
        for img_bytes in images:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })

    client = anthropic.Anthropic(timeout=timeout)
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)

    # Track cost from response usage
    _track_response_cost(
        model=model,
        input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
        label=cost_label,
    )

    return resp.content[0].text


# ---------------------------------------------------------------------------
# Cost tracking helper
# ---------------------------------------------------------------------------


def _track_response_cost(
    model: str, input_tokens: int, output_tokens: int, label: str,
) -> None:
    """Report token usage to the global cost tracker.

    Silently does nothing if tracking fails (never interrupts API calls).
    """
    if input_tokens == 0 and output_tokens == 0:
        return
    try:
        from openadapt_evals.cost_tracker import get_cost_tracker

        get_cost_tracker().track_api_call(
            model, input_tokens, output_tokens, label=label,
        )
    except Exception:
        pass  # Cost tracking must never break API calls


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def extract_json(text: str):
    """Extract a JSON array or object from LLM output.

    Handles common cases:

    - Pure JSON
    - JSON wrapped in ``\\`\\`\\`json ... \\`\\`\\``` fences
    - Preamble text before the JSON / fence
    - Trailing commentary after the JSON
    """
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding the first [ ... ] or { ... } in the text
    for opener, closer in [("[", "]"), ("{", "}")]:
        start = text.find(opener)
        if start == -1:
            continue
        # Walk from the end to find the matching closer
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def image_bytes_from_path(path: str | Path) -> bytes:
    """Read raw image bytes from a file path."""
    return Path(path).read_bytes()
