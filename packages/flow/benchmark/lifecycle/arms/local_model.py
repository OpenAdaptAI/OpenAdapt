"""A local Ollama model behind the Anthropic SDK's ``messages.create`` shape.

Stand-in only: it lets the agent arms run end to end on the real fixture when
no frontier model key is available. Every ledger row names the model
(``ollama:<name>``), and the report labels these arms as local stand-ins, not
the frontier agent the comparison is about.

Translation, Anthropic messages -> Ollama ``/api/chat`` (native, ``think``
off where the model allows it):

- ``system`` -> a system message; text and image blocks -> one user message
  (images base64); ``tool_result`` blocks -> ``tool`` messages, and any images
  in them -> a following user message (Ollama takes images on user turns);
- assistant ``text``/``tool_use`` blocks -> one assistant message with
  ``tool_calls``;
- custom tools -> function tools; the computer use toolset -> one function
  ``computer`` whose ``action`` argument becomes the tool_use block's name,
  which is how the toolset reports actions.

Usage comes from Ollama's ``prompt_eval_count`` and ``eval_count``. Local
calls cost $0 in model fees; their time is in the item's wall and compute.
"""

from __future__ import annotations

import itertools
import json
import os
from types import SimpleNamespace
from typing import Any, Optional

OLLAMA_URL = os.environ.get("LIFECYCLE_OLLAMA_URL", "http://127.0.0.1:11434")
PREFIX = "ollama:"
_ids = itertools.count(1)

COMPUTER_FUNCTION = {
    "type": "function",
    "function": {
        "name": "computer",
        "description": (
            "Act on the 1280x800 screen. coordinate is [x, y] in screen pixels. "
            "Actions: screenshot, left_click, double_click, type (text), key (text, "
            "e.g. Enter or ctrl+a), scroll (coordinate, scroll_direction, scroll_amount)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "left_click", "double_click", "type", "key", "scroll"],
                },
                "coordinate": {"type": "array", "items": {"type": "integer"}},
                "text": {"type": "string"},
                "scroll_direction": {"type": "string", "enum": ["up", "down"]},
                "scroll_amount": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
}


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _tools(tools: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        if str(t.get("type", "")).startswith("computer_"):
            out.append(COMPUTER_FUNCTION)
        else:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                    },
                }
            )
    return out


def _messages(system: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system if isinstance(system, str) else str(system)})
    names: dict[str, str] = {}
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text, calls = [], []
            for b in content:
                t = _get(b, "type")
                if t == "text":
                    text.append(_get(b, "text", ""))
                elif t == "tool_use":
                    names[_get(b, "id")] = _get(b, "name")
                    args = dict(_get(b, "input", {}) or {})
                    name = _get(b, "name")
                    if name not in TOOL_NAMES_SEEN and name in COMPUTER_ACTIONS:
                        args = {"action": name, **args}
                        name = "computer"
                    calls.append({"function": {"name": name, "arguments": args}})
            msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts, images, after_images = [], [], []
        for b in content:
            t = _get(b, "type")
            if t == "text":
                texts.append(_get(b, "text", ""))
            elif t == "image":
                images.append(_get(b, "source")["data"])
            elif t == "tool_result":
                c = _get(b, "content", "")
                if isinstance(c, list):
                    parts = []
                    for x in c:
                        if _get(x, "type") == "image":
                            after_images.append(_get(x, "source")["data"])
                        elif _get(x, "type") == "text":
                            parts.append(_get(x, "text", ""))
                    c = "\n".join(parts) or "(see the screenshot that follows)"
                out.append({"role": "tool", "content": str(c), "tool_name": names.get(_get(b, "tool_use_id"), "")})
        if texts or images:
            msg = {"role": "user", "content": "\n".join(texts)}
            if images:
                msg["images"] = images
            out.append(msg)
        if after_images:
            out.append({"role": "user", "content": "The screen now:", "images": after_images[-1:]})
    return out


COMPUTER_ACTIONS = {"screenshot", "left_click", "double_click", "type", "key", "scroll"}
TOOL_NAMES_SEEN: set[str] = set()


class _Messages:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def create(self, *, model: str, max_tokens: int = 4096, system: Any = None,
               tools: Optional[list[dict[str, Any]]] = None, messages: list[dict[str, Any]],
               **_ignored: Any) -> Any:
        import httpx

        name = model[len(PREFIX):] if model.startswith(PREFIX) else model
        TOOL_NAMES_SEEN.clear()
        TOOL_NAMES_SEEN.update(t["name"] for t in tools or [] if "name" in t)
        body = {
            "model": name,
            "stream": False,
            "think": False,
            "messages": _messages(system, messages),
            "tools": _tools(tools),
            "options": {"num_predict": min(int(max_tokens), 2048), "num_ctx": 32768, "temperature": 0},
        }
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message") or {}
        blocks: list[Any] = []
        if msg.get("content"):
            blocks.append(SimpleNamespace(type="text", text=msg["content"]))
        is_computer = any(str(t.get("type", "")).startswith("computer_") for t in tools or [])
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            fname = fn.get("name", "")
            if is_computer and fname == "computer":
                args = dict(args)
                fname = str(args.pop("action", "screenshot"))
                if fname == "scroll":
                    args.setdefault("scroll_direction", "down")
                    args.setdefault("scroll_amount", 3)
            blocks.append(SimpleNamespace(type="tool_use", id=f"local_{next(_ids)}", name=fname, input=args))
        usage = SimpleNamespace(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else (
            "max_tokens" if data.get("done_reason") == "length" else "end_turn")
        return SimpleNamespace(content=blocks, usage=usage, stop_reason=stop, model=model)

    def stream(self, **kw: Any) -> Any:
        resp = self.create(**kw)

        class _S:
            def __enter__(self) -> "_S":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def get_final_message(self) -> Any:
                return resp

        return _S()


class LocalClient:
    def __init__(self, timeout_s: float = 900.0) -> None:
        self.messages = _Messages(timeout_s)


def is_local(model: str) -> bool:
    return model.startswith(PREFIX)
