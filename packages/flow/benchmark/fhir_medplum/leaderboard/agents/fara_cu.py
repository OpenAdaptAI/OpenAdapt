"""Fara-7B (Microsoft, MIT), run locally on Apple silicon with MLX.

The prompt, the tool schema and the screenshot scaling are Fara's own
(fara_prompt.py, reproduced from github.com/microsoft/fara under MIT). As in Fara's reference agent, the model sees the
last three screenshots, gets "Current URL: ..." each turn, and samples at
temperature 0. Fara ends a task with ``terminate`` and a status; when its
reply has no ``RESULT:`` line, ``success`` is read as DONE and ``failure``
as NOT DONE (pre-registered). The model's own thoughts are the reply.
"""

from __future__ import annotations

import ast
import io
import json
import os
from typing import Any

from ..browser import Screen
from ..common import PREAMBLE, AgentRun, Meter, RunLimit
from .fara_prompt import system_prompt

MODEL_REPO = os.environ.get("FARA_MLX_REPO", "mlx-community/Fara-7B-8bit")
MAX_IMAGES = 3
NEXT = "Here is the next screenshot. Think about what to do next."
_loaded: dict[str, Any] = {}


def _model() -> tuple[Any, Any]:
    if "m" not in _loaded:
        from mlx_vlm import load

        _loaded["m"] = load(MODEL_REPO)
    return _loaded["m"]


def _parse(message: str) -> tuple[str, dict[str, Any]]:
    head, _, rest = message.partition("<tool_call>")
    body = rest.split("</tool_call>")[0].strip()
    try:
        call = json.loads(body)
    except json.JSONDecodeError:
        call = ast.literal_eval(body)
    return head.strip(), dict(call.get("arguments") or {})


def _act(screen: Screen, a: dict[str, Any], sx: float, sy: float, app_origin: str) -> bool:
    """Do one action; True when the model ended the task."""

    kind = a.get("action")
    xy = a.get("coordinate")
    pt = (float(xy[0]) * sx, float(xy[1]) * sy) if xy else None
    if kind == "terminate":
        return True
    if kind == "left_click":
        screen.click(*pt)
    elif kind == "mouse_move":
        screen.move(*pt)
    elif kind == "type":
        if pt:
            screen.click(*pt)
        if a.get("delete_existing_text"):
            screen.key(["Meta", "a"])
            screen.key(["Backspace"])
        screen.type(str(a.get("text", "")))
        if a.get("press_enter"):
            screen.key(["Enter"])
    elif kind == "key":
        screen.key([str(k) for k in a.get("keys") or []])
    elif kind == "scroll":
        px = float(a.get("pixels", 0))
        screen.scroll(*(pt or (640, 400)), 0, -px)  # Fara: positive scrolls up
    elif kind == "wait":
        screen.wait(float(a.get("time", 1)))
    elif kind == "history_back":
        screen.back()
    elif kind == "visit_url":
        url = str(a.get("url", ""))
        if not url.startswith(app_origin):
            raise ValueError("only the clinic's app can be opened in this browser")
        screen.goto(url)
    elif kind in ("pause_and_memorize_fact",):
        pass
    elif kind == "web_search":
        raise ValueError("there is no web search in this browser")
    else:
        raise ValueError(f"unsupported action {kind!r}")
    return False


def run(instruction: str, screen: Screen, meter: Meter, app_origin: str = "") -> AgentRun:
    from mlx_vlm import generate
    from PIL import Image

    model, processor = _model()
    history: list[dict[str, Any]] = []  # {"role", "text", "image"?}
    transcript: list[dict[str, Any]] = []
    thoughts = ""
    try:
        while True:
            meter.before_call()
            shot = Image.open(io.BytesIO(screen.screenshot())).convert("RGB")
            system, (w, h) = system_prompt(shot.width, shot.height)
            sx, sy = shot.width / w, shot.height / h
            img = shot.resize((w, h))
            text = (PREAMBLE + instruction) if not history else f"Current URL: {screen.url()[:100]}\n{NEXT}"
            history.append({"role": "user", "text": text, "image": img})
            # Keep the last MAX_IMAGES screenshots; the task's text always stays.
            with_img = [i for i, m in enumerate(history) if m.get("image") is not None][-MAX_IMAGES:]
            prompt = f"<|im_start|>system\n{system}<|im_end|>\n"
            images = []
            for i, m in enumerate(history):
                body = m["text"]
                if m["role"] == "user" and i in with_img:
                    body = "<|vision_start|><|image_pad|><|vision_end|>" + body
                    images.append(m["image"])
                prompt += f"<|im_start|>{m['role']}\n{body}<|im_end|>\n"
            prompt += "<|im_start|>assistant\n"
            out = generate(model, processor, prompt, image=images, max_tokens=1024, temperature=0.0, verbose=False)
            reply = getattr(out, "text", out)
            meter.record(
                0.0,
                int(getattr(out, "prompt_tokens", 0) or 0),
                int(getattr(out, "generation_tokens", 0) or 0),
            )
            history.append({"role": "assistant", "text": reply})
            entry: dict[str, Any] = {"reply": reply}
            transcript.append(entry)
            try:
                thoughts, args = _parse(reply)
            except Exception as exc:  # noqa: BLE001 - an unreadable reply
                entry["error"] = f"unparsable: {exc}"[:200]
                continue
            try:
                if _act(screen, args, sx, sy, app_origin):
                    if "RESULT:" in thoughts.upper():
                        return AgentRun(thoughts, meter, None, transcript)
                    verdict = "DONE" if args.get("status") == "success" else "NOT DONE"
                    return AgentRun(f"{thoughts}\nRESULT: {verdict}", meter, None, transcript)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{exc}"[:200]
    except RunLimit as exc:
        return AgentRun(thoughts, meter, str(exc), transcript)
