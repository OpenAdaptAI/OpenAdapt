"""OpenAI computer use (Responses API, ``computer`` tool)."""

from __future__ import annotations

import base64
from typing import Any

from ..browser import Screen
from ..common import PREAMBLE, AgentRun, Meter, RunLimit, price_usd

PRICES = {  # USD per million tokens, list, 2026-11-02
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cached_input": 0.2},
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _act(screen: Screen, a: Any) -> None:
    kind = _get(a, "type")
    mods = list(_get(a, "keys") or []) if kind != "keypress" else []
    if kind == "screenshot":
        return
    if kind == "wait":
        screen.wait(float(_get(a, "ms", 1000) or 1000) / 1000 if _get(a, "ms") else 1.0)
    elif kind == "move":
        screen.move(_get(a, "x"), _get(a, "y"))
    elif kind in ("click", "double_click"):
        button = str(_get(a, "button", "left") or "left")
        if button in ("back", "forward"):
            screen.back() if button == "back" else None
            return
        button = {"wheel": "middle"}.get(button, button)
        screen.click(
            _get(a, "x"), _get(a, "y"), button=button,
            count=2 if kind == "double_click" else 1, modifiers=mods,
        )
    elif kind == "drag":
        screen.drag([(_get(p, "x"), _get(p, "y")) for p in _get(a, "path") or []])
    elif kind == "keypress":
        screen.key(list(_get(a, "keys") or []))
    elif kind == "type":
        screen.type(str(_get(a, "text", "")))
    elif kind == "scroll":
        screen.scroll(_get(a, "x", 640), _get(a, "y", 400), _get(a, "scroll_x", 0) or 0, _get(a, "scroll_y", 0) or 0)
    else:
        raise ValueError(f"unsupported action {kind!r}")


def _shot(screen: Screen) -> dict[str, Any]:
    return {
        "type": "computer_screenshot",
        "image_url": "data:image/png;base64," + base64.b64encode(screen.screenshot()).decode(),
        "detail": "original",
    }


def run(instruction: str, screen: Screen, meter: Meter, model: str = "gpt-6.1-sol") -> AgentRun:
    from openai import OpenAI

    client = OpenAI(max_retries=4)
    first = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": PREAMBLE + instruction},
                {"type": "input_image", "image_url": _shot(screen)["image_url"], "detail": "original"},
            ],
        }
    ]
    transcript: list[dict[str, Any]] = []
    # The organisation has zero data retention: no previous_response_id, so
    # the whole conversation is sent each turn (reasoning as encrypted items).
    history: list[Any] = list(first)
    try:
        while True:
            meter.before_call()
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
                price_usd(PRICES[model], int(u.input_tokens), int(u.output_tokens), cached),
                int(u.input_tokens), int(u.output_tokens), cached=cached,
            )
            calls = [o for o in r.output if _get(o, "type") == "computer_call"]
            text = " ".join(
                _get(c, "text", "")
                for o in r.output if _get(o, "type") == "message"
                for c in _get(o, "content") or [] if _get(c, "type") == "output_text"
            )
            refusal = any(
                _get(c, "type") == "refusal"
                for o in r.output if _get(o, "type") == "message"
                for c in _get(o, "content") or []
            )
            transcript.append({"text": text, "calls": [_dump(c) for c in calls]})
            if refusal:
                return AgentRun(text, meter, "refusal", transcript)
            if not calls:
                return AgentRun(text, meter, None, transcript)
            for c in calls:
                actions = _get(c, "actions") or ([_get(c, "action")] if _get(c, "action") else [])
                err = ""
                for a in actions:
                    try:
                        _act(screen, a)
                    except Exception as exc:  # noqa: BLE001
                        err = f"{exc}"[:200]
                        break
                item: dict[str, Any] = {"type": "computer_call_output", "call_id": _get(c, "call_id"), "output": _shot(screen)}
                checks = _get(c, "pending_safety_checks") or []
                if checks:
                    # Pre-registered: the harness plays the operator who gave
                    # the task and approves; every approval is logged.
                    item["acknowledged_safety_checks"] = [
                        {"id": _get(s, "id"), "code": _get(s, "code"), "message": _get(s, "message")} for s in checks
                    ]
                    transcript[-1].setdefault("approved_checks", []).extend(item["acknowledged_safety_checks"])
                if err:
                    transcript[-1]["error"] = err
                history.append(item)
    except RunLimit as exc:
        return AgentRun("", meter, str(exc), transcript)


def _dump(c: Any) -> Any:
    try:
        return c.model_dump(exclude_none=True)
    except AttributeError:
        return c
