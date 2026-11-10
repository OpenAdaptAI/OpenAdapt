"""Claude computer use (Anthropic Messages API, ``computer_toolset_20260801``)."""

from __future__ import annotations

import base64
import os
from typing import Any

from ..browser import Screen
from ..common import PREAMBLE, AgentRun, Meter, RunLimit, price_usd

PRICES = {  # USD per million tokens, list, 2026-11-02
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cached_input": 0.2, "cache_write": 2.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cached_input": 0.2, "cache_write": 5.0},
}
SCROLL_PX = 120


def _img(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
    }


def _act(screen: Screen, a: dict[str, Any]) -> None:
    kind = a.get("action")
    xy = a.get("coordinate")
    mods = [m for m in str(a.get("text") or "").split("+") if m] if kind and kind.endswith("click") else []
    if kind in ("screenshot", "cursor_position", "zoom"):
        return
    if kind == "wait":
        screen.wait(float(a.get("duration", 1.0)))
    elif kind == "mouse_move":
        screen.move(*xy)
    elif kind in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        if xy is None:
            raise ValueError("click needs a coordinate")
        button = {"right_click": "right", "middle_click": "middle"}.get(kind, "left")
        count = {"double_click": 2, "triple_click": 3}.get(kind, 1)
        screen.click(*xy, button=button, count=count, modifiers=mods)
    elif kind == "left_click_drag":
        screen.drag([tuple(a["start_coordinate"]), tuple(xy)])
    elif kind == "type":
        screen.type(str(a.get("text", "")))
    elif kind == "key":
        for combo in str(a.get("text", "")).split():
            for _ in range(max(1, min(int(a.get("repeat") or 1), 50))):
                screen.key(combo.split("+"))
    elif kind == "scroll":
        n = int(a.get("scroll_amount", 3)) * SCROLL_PX
        dx, dy = {"down": (0, n), "up": (0, -n), "right": (n, 0), "left": (-n, 0)}[
            str(a.get("scroll_direction"))
        ]
        x, y = xy if xy else (640, 400)
        screen.scroll(x, y, dx, dy)
    else:
        raise ValueError(f"unsupported action {kind!r}")


def run(instruction: str, screen: Screen, meter: Meter, model: str = "claude-sonnet-5-5") -> AgentRun:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    tools = [{"type": "computer_toolset_20260801"}]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": PREAMBLE + instruction}, _img(screen.screenshot())]}
    ]
    transcript: list[dict[str, Any]] = []
    try:
        while True:
            meter.before_call()
            r = client.messages.create(
                model=model,
                max_tokens=8000,
                tools=tools,
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            u = r.usage
            cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            tin = int(u.input_tokens) + cached + written
            p = PRICES[model]
            usd = price_usd(p, tin - written, int(u.output_tokens), cached) + written * p["cache_write"] / 1e6
            meter.record(usd, tin, int(u.output_tokens), cached=cached)
            calls = [b for b in r.content if b.type == "tool_use"]
            text = " ".join(b.text for b in r.content if b.type == "text")
            transcript.append({"text": text, "actions": [b.input for b in calls], "stop": r.stop_reason})
            messages.append({"role": "assistant", "content": r.content})
            if r.stop_reason == "refusal":
                return AgentRun(text, meter, "refusal", transcript)
            if not calls:
                return AgentRun(text, meter, None, transcript)
            results = []
            for b in calls:
                res: dict[str, Any] = {"type": "tool_result", "tool_use_id": b.id, "toolset_name": "computer"}
                try:
                    _act(screen, {"action": b.name, **dict(b.input)})
                except Exception as exc:  # noqa: BLE001 - the model hears about it
                    res["content"] = f"Error: {exc}"[:300]
                    res["is_error"] = True
                    results.append(res)
                    continue
                res["content"] = [_img(screen.screenshot())] if b is calls[-1] or b.name == "screenshot" else "OK"
                results.append(res)
            messages.append({"role": "user", "content": results})
    except RunLimit as exc:
        return AgentRun("", meter, str(exc), transcript)
