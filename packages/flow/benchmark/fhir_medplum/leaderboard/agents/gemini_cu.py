"""Gemini computer use (Gemini API Interactions, ``computer_use`` tool, browser)."""

from __future__ import annotations

import base64
import json
from typing import Any

from ..browser import VIEWPORT, Screen
from ..common import PREAMBLE, AgentRun, Meter, RunLimit, price_usd

PRICES = {  # USD per million tokens, list, 2026-11-02 (promotional to 2026-12-31)
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cached_input": 0.075},
}
W, H = VIEWPORT["width"], VIEWPORT["height"]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _px(a: dict[str, Any], kx: str = "x", ky: str = "y") -> tuple[float, float]:
    return int(a[kx]) / 1000 * W, int(a[ky]) / 1000 * H


def _act(screen: Screen, name: str, a: dict[str, Any], app_origin: str) -> None:
    if name in ("take_screenshot",):
        return
    if name in ("click", "double_click", "triple_click", "middle_click", "right_click"):
        button = {"middle_click": "middle", "right_click": "right"}.get(name, "left")
        count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        screen.click(*_px(a), button=button, count=count)
    elif name == "move":
        screen.move(*_px(a))
    elif name == "type":
        screen.type(str(a.get("text", "")))
        if a.get("press_enter"):
            screen.key(["Enter"])
    elif name == "drag_and_drop":
        screen.drag([_px(a, "start_x", "start_y"), _px(a, "end_x", "end_y")])
    elif name == "wait":
        screen.wait(float(a.get("seconds", 1)))
    elif name == "press_key":
        screen.key([str(a["key"])])
    elif name == "hotkey":
        screen.key([str(k) for k in a.get("keys") or []])
    elif name == "scroll":
        mag = float(a.get("magnitude_in_pixels", 300))
        dx, dy = {"down": (0, mag), "up": (0, -mag), "right": (mag, 0), "left": (-mag, 0)}[a["direction"]]
        x, y = _px(a) if "x" in a else (W / 2, H / 2)
        screen.scroll(x, y, dx, dy)
    elif name == "go_back":
        screen.back()
    elif name == "navigate":
        url = str(a.get("url", ""))
        if not url.startswith(app_origin):
            raise ValueError("only the clinic's app can be opened in this browser")
        screen.goto(url)
    else:
        raise ValueError(f"unsupported action {name!r}")


def run(instruction: str, screen: Screen, meter: Meter, model: str = "gemini-3.8-flash", app_origin: str = "") -> AgentRun:
    from google import genai

    client = genai.Client()
    tools = [{"type": "computer_use", "environment": "browser"}]
    shot = base64.b64encode(screen.screenshot()).decode()
    pending: Any = [
        {"type": "text", "text": PREAMBLE + instruction},
        {"type": "image", "data": shot, "mime_type": "image/png"},
    ]
    prev = None
    transcript: list[dict[str, Any]] = []
    try:
        while True:
            meter.before_call()
            kw: dict[str, Any] = {"model": model, "input": pending, "tools": tools}
            if prev:
                kw["previous_interaction_id"] = prev
            r = client.interactions.create(**kw)
            prev = _get(r, "id")
            u = _get(r, "usage") or {}
            tin = int(_get(u, "total_input_tokens", 0) or _get(u, "input_tokens", 0) or 0)
            tout = int(_get(u, "total_output_tokens", 0) or _get(u, "output_tokens", 0) or 0)
            cached = int(_get(u, "total_cached_tokens", 0) or 0)
            meter.record(price_usd(PRICES[model], tin, tout, cached), tin, tout, cached=cached)
            outputs = _get(r, "outputs") or []
            calls = [o for o in outputs if _get(o, "type") == "function_call"]
            text = " ".join(str(_get(o, "text", "")) for o in outputs if _get(o, "type") == "text")
            transcript.append({"text": text, "calls": [{"name": _get(c, "name"), "args": _get(c, "arguments")} for c in calls]})
            if not calls:
                return AgentRun(text, meter, None, transcript)
            pending = []
            for c in calls:
                name, args = _get(c, "name"), dict(_get(c, "arguments") or {})
                result: dict[str, Any] = {}
                sd = args.pop("safety_decision", None)
                if sd and _get(sd, "decision") == "require_confirmation":
                    # Pre-registered: the harness plays the operator who gave
                    # the task and approves; every approval is logged.
                    result["safety_acknowledgement"] = True
                    transcript[-1].setdefault("approved_checks", []).append(sd)
                try:
                    _act(screen, name, args, app_origin)
                except Exception as exc:  # noqa: BLE001
                    result["error"] = f"{exc}"[:200]
                pending.append(
                    {
                        "type": "function_result",
                        "name": name,
                        "call_id": _get(c, "id") or _get(c, "call_id"),
                        "result": [
                            {"type": "text", "text": json.dumps({"url": screen.url(), **result})},
                            {"type": "image", "data": base64.b64encode(screen.screenshot()).decode(), "mime_type": "image/png"},
                        ],
                    }
                )
    except RunLimit as exc:
        return AgentRun("", meter, str(exc), transcript)
