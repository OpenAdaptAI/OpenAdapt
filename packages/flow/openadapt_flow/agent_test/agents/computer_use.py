"""Claude computer use and OpenAI computer use, on the scenario's screen.

Both loops are the write leaderboard's adapters
(``benchmark/fhir_medplum/leaderboard/agents``), with the model and its
price as parameters and this harness's budget.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from openadapt_flow.agent_test.agents.common import (
    AgentContext,
    AgentResult,
    RunLimit,
    call_usd,
)

SCROLL_PX = 120


def _img(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode(),
        },
    }


def _claude_act(screen: Any, a: dict[str, Any]) -> None:
    kind = a.get("action")
    xy = a.get("coordinate")
    mods = (
        [m for m in str(a.get("text") or "").split("+") if m]
        if kind and kind.endswith("click")
        else []
    )
    if kind in ("screenshot", "cursor_position", "zoom"):
        return
    if kind == "wait":
        screen.wait(float(a.get("duration", 1.0)))
    elif kind == "mouse_move":
        screen.move(*xy)
    elif kind in (
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
    ):
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


def run_claude(ctx: AgentContext, model: str, prices: dict[str, float]) -> AgentResult:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    screen, meter = ctx.screen, ctx.meter
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": ctx.instruction},
                _img(screen.screenshot()),
            ],
        }
    ]
    transcript: list[dict[str, Any]] = []
    try:
        while True:
            meter.before_call()
            r = client.messages.create(
                model=model,
                max_tokens=8000,
                tools=[{"type": "computer_toolset_20260801"}],
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            u = r.usage
            cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            tin = int(u.input_tokens) + cached + written
            meter.record(
                call_usd(prices, tin, int(u.output_tokens), cached, written),
                tin,
                int(u.output_tokens),
            )
            calls = [b for b in r.content if b.type == "tool_use"]
            text = " ".join(b.text for b in r.content if b.type == "text")
            transcript.append(
                {
                    "text": text,
                    "actions": [b.input for b in calls],
                    "stop": r.stop_reason,
                }
            )
            messages.append({"role": "assistant", "content": r.content})
            if r.stop_reason == "refusal":
                return AgentResult(text, stopped="refusal", transcript=transcript)
            if not calls:
                return AgentResult(text, transcript=transcript)
            results = []
            for b in calls:
                res: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "toolset_name": "computer",
                }
                try:
                    _claude_act(screen, {"action": b.name, **dict(b.input)})
                except Exception as exc:  # noqa: BLE001 - the model hears about it
                    res["content"] = f"Error: {exc}"[:300]
                    res["is_error"] = True
                    results.append(res)
                    continue
                res["content"] = (
                    [_img(screen.screenshot())]
                    if b is calls[-1] or b.name == "screenshot"
                    else "OK"
                )
                results.append(res)
            messages.append({"role": "user", "content": results})
    except RunLimit as exc:
        return AgentResult("", stopped=str(exc), transcript=transcript)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _openai_act(screen: Any, a: Any) -> None:
    kind = _get(a, "type")
    mods = list(_get(a, "keys") or []) if kind != "keypress" else []
    if kind == "screenshot":
        return
    if kind == "wait":
        screen.wait(float(_get(a, "ms") or 1000) / 1000)
    elif kind == "move":
        screen.move(_get(a, "x"), _get(a, "y"))
    elif kind in ("click", "double_click"):
        button = str(_get(a, "button", "left") or "left")
        if button in ("back", "forward"):
            if button == "back":
                screen.back()
            return
        button = {"wheel": "middle"}.get(button, button)
        screen.click(
            _get(a, "x"),
            _get(a, "y"),
            button=button,
            count=2 if kind == "double_click" else 1,
            modifiers=mods,
        )
    elif kind == "drag":
        screen.drag([(_get(p, "x"), _get(p, "y")) for p in _get(a, "path") or []])
    elif kind == "keypress":
        screen.key(list(_get(a, "keys") or []))
    elif kind == "type":
        screen.type(str(_get(a, "text", "")))
    elif kind == "scroll":
        screen.scroll(
            _get(a, "x", 640),
            _get(a, "y", 400),
            _get(a, "scroll_x", 0) or 0,
            _get(a, "scroll_y", 0) or 0,
        )
    else:
        raise ValueError(f"unsupported action {kind!r}")


def _shot_url(screen: Any) -> str:
    return "data:image/png;base64," + base64.b64encode(screen.screenshot()).decode()


def _dump(c: Any) -> Any:
    try:
        return c.model_dump(exclude_none=True)
    except AttributeError:
        return c


def run_openai(ctx: AgentContext, model: str, prices: dict[str, float]) -> AgentResult:
    from openai import OpenAI

    client = OpenAI(max_retries=4)
    screen, meter = ctx.screen, ctx.meter
    history: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": ctx.instruction},
                {
                    "type": "input_image",
                    "image_url": _shot_url(screen),
                    "detail": "original",
                },
            ],
        }
    ]
    transcript: list[dict[str, Any]] = []
    try:
        while True:
            meter.before_call()
            # No previous_response_id: the whole conversation is sent each turn,
            # so nothing depends on the provider keeping it.
            r = client.responses.create(
                model=model,
                tools=[{"type": "computer"}],
                input=history,
                truncation="auto",
                store=False,
                include=["reasoning.encrypted_content"],
            )
            history.extend(_dump(o) for o in r.output)
            u = r.usage
            cached = int(_get(_get(u, "input_tokens_details"), "cached_tokens", 0) or 0)
            meter.record(
                call_usd(prices, int(u.input_tokens), int(u.output_tokens), cached),
                int(u.input_tokens),
                int(u.output_tokens),
            )
            calls = [o for o in r.output if _get(o, "type") == "computer_call"]
            text = " ".join(
                _get(c, "text", "")
                for o in r.output
                if _get(o, "type") == "message"
                for c in _get(o, "content") or []
                if _get(c, "type") == "output_text"
            )
            refusal = any(
                _get(c, "type") == "refusal"
                for o in r.output
                if _get(o, "type") == "message"
                for c in _get(o, "content") or []
            )
            transcript.append({"text": text, "calls": [_dump(c) for c in calls]})
            if refusal:
                return AgentResult(text, stopped="refusal", transcript=transcript)
            if not calls:
                return AgentResult(text, transcript=transcript)
            for c in calls:
                actions = _get(c, "actions") or (
                    [_get(c, "action")] if _get(c, "action") else []
                )
                err = ""
                for a in actions:
                    try:
                        _openai_act(screen, a)
                    except Exception as exc:  # noqa: BLE001
                        err = f"{exc}"[:200]
                        break
                item: dict[str, Any] = {
                    "type": "computer_call_output",
                    "call_id": _get(c, "call_id"),
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": _shot_url(screen),
                        "detail": "original",
                    },
                }
                checks = _get(c, "pending_safety_checks") or []
                if checks:
                    # The harness plays the operator who gave the task and
                    # approves; every approval is logged in the transcript.
                    item["acknowledged_safety_checks"] = [
                        {
                            "id": _get(s, "id"),
                            "code": _get(s, "code"),
                            "message": _get(s, "message"),
                        }
                        for s in checks
                    ]
                    transcript[-1].setdefault("approved_checks", []).extend(
                        item["acknowledged_safety_checks"]
                    )
                if err:
                    transcript[-1]["error"] = err
                history.append(item)
    except RunLimit as exc:
        return AgentResult("", stopped=str(exc), transcript=transcript)
