"""A Claude computer-use agent driving the OpenEnv environment on screenshots.

The agent sees only screenshots of the 1280x800 browser and acts with
Anthropic's computer toolset; each action becomes one environment step
(click, type, press, scroll, hover, wait). When it stops calling tools, its
last ``RESULT:`` line is the claim and its whole final reply is the message
the grader checks answers against.

No server-side model fallback is enabled: a refusal is recorded as the
model's own outcome, so the pass rate is the named model's.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

PRICES = {  # USD per million tokens, list, 2026-11-03
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
# Per-episode caps; long tasks raise them through the environment.
MODEL_CALL_CAP = int(os.environ.get("DOLIBARR_AGENT_MAX_CALLS", "70"))
RUN_USD_CAP = float(os.environ.get("DOLIBARR_AGENT_MAX_USD", "1.50"))
SCROLL_PX = 120

PREAMBLE = """\
You work in the back office of Synthetic Supplies, an office-supply \
wholesaler. You are using Dolibarr, the company's ERP, in a web browser. The \
screen is 1280x800 pixels and you are already signed in. Use only this app, \
and change nothing the task does not ask for.

When you have finished, or if you cannot finish, stop acting and reply. If \
the task asks a question, put your answer alone on one line that starts with \
ANSWER: (for example "ANSWER: 42.50"). End your reply with one final line in \
exactly one of these two forms:
RESULT: DONE
RESULT: NOT DONE <the reason, in one line>
Write RESULT: DONE only if the task is now done exactly as asked.

The task:
"""

_RESULT = re.compile(r"^\W*RESULT\W*:?\s*(NOT\s+DONE|DONE)\b", re.I)


def parse_claim(text: str) -> Optional[str]:
    for ln in reversed([ln.strip() for ln in (text or "").splitlines() if ln.strip()]):
        m = _RESULT.match(ln)
        if m:
            return "success" if m.group(1).upper() == "DONE" else "failure"
    return None


class SpendCapReached(RuntimeError):
    pass


class Ledger:
    """A file-locked running total of list-price spend, shared by every run."""

    def __init__(self, path: Path, max_usd: float) -> None:
        self.path, self.max_usd = path, max_usd
        path.parent.mkdir(parents=True, exist_ok=True)

    def _update(self, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read()
            data = json.loads(raw) if raw.strip() else {"total_usd": 0.0, "by_model": {}}
            fn(data)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data, indent=1))
            return data

    def check(self) -> None:
        total = self._update(lambda d: None)["total_usd"]
        if self.max_usd and total + RUN_USD_CAP > self.max_usd:
            raise SpendCapReached(f"ledger at ${total:.2f}; one more run could pass ${self.max_usd:.2f}")

    def add(self, model: str, usd: float) -> None:
        def f(d: dict[str, Any]) -> None:
            d["total_usd"] = round(d["total_usd"] + usd, 6)
            d["by_model"][model] = round(d["by_model"].get(model, 0.0) + usd, 6)

        self._update(f)


def _img(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def _key(text: str) -> str:
    from benchmark.fhir_medplum.leaderboard.browser import chord

    # Chromium on macOS treats Control+A as "start of line", not select-all,
    # so a model's ctrl shortcut uses Meta there and Control elsewhere.
    return chord(text.split("+")).replace("Control+", "ControlOrMeta+")


def to_env(a: dict[str, Any]) -> list[dict[str, Any]]:
    """One computer-toolset action as environment actions ([] = just look)."""

    kind = a.get("action")
    xy = a.get("coordinate")
    if kind in ("screenshot", "cursor_position", "zoom"):
        return []
    if kind in ("left_click", "double_click", "triple_click"):
        if not xy:
            raise ValueError("click needs a coordinate")
        n = {"double_click": 2, "triple_click": 3}.get(kind, 1)
        return [{"kind": "click", "x": int(xy[0]), "y": int(xy[1]), "count": n}]
    if kind == "mouse_move":
        return [{"kind": "hover", "x": int(xy[0]), "y": int(xy[1])}]
    if kind == "type":
        return [{"kind": "type", "text": str(a.get("text", ""))}]
    if kind == "key":
        # A held or repeated key is one step, as it is one action.
        reps = max(1, min(int(a.get("repeat") or 1), 50))
        return [{"kind": "press", "text": _key(c), "count": reps} for c in str(a.get("text", "")).split()]
    if kind == "scroll":
        n = int(a.get("scroll_amount", 3)) * SCROLL_PX
        dx, dy = {"down": (0, n), "up": (0, -n), "right": (n, 0), "left": (-n, 0)}[str(a.get("scroll_direction"))]
        x, y = xy if xy else (640, 400)
        return [{"kind": "scroll", "x": int(x), "y": int(y), "dx": dx, "dy": dy}]
    if kind == "wait":
        return [{"kind": "wait", "seconds": float(a.get("duration", 1.0))}]
    raise ValueError(f"action {kind!r} is not available in this environment")


def claude_policy(model: str, effort: str, *, ledger: Path, max_usd: float) -> Callable[..., Any]:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    book = Ledger(ledger, max_usd)
    price = PRICES[model]

    def run(env: Any, task: Any, obs: Any) -> tuple[Any, dict[str, Any]]:
        from openadapt_flow.rl_env.environment import WebAction

        book.check()
        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            {"type": "text", "text": PREAMBLE + obs.instruction}, _img(obs.screenshot_png_b64)]}]
        transcript: list[dict[str, Any]] = []
        usd = 0.0
        tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        calls = 0
        final_text, stop, refusal = "", None, False
        t0 = time.monotonic()
        while True:
            if calls >= MODEL_CALL_CAP:
                stop = f"model call cap {MODEL_CALL_CAP}"
                break
            if usd >= RUN_USD_CAP:
                stop = f"run spend cap ${RUN_USD_CAP}"
                break
            calls += 1
            r = client.messages.create(
                model=model,
                max_tokens=8000,
                tools=[{"type": "computer_toolset_20260801"}],
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            u = r.usage
            cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            step_usd = (u.input_tokens * price["input"] + u.output_tokens * price["output"]
                        + cr * price["cache_read"] + cw * price["cache_write"]) / 1e6
            usd += step_usd
            book.add(model, step_usd)
            for k, v in (("input", u.input_tokens), ("output", u.output_tokens), ("cache_read", cr), ("cache_write", cw)):
                tokens[k] += int(v)
            uses = [b for b in r.content if b.type == "tool_use"]
            text = " ".join(b.text for b in r.content if b.type == "text")
            transcript.append({"text": text, "actions": [dict(b.input) | {"_name": b.name} for b in uses],
                               "stop": r.stop_reason})
            messages.append({"role": "assistant", "content": r.content})
            if r.stop_reason == "refusal":
                refusal, final_text = True, text
                break
            if not uses:
                final_text = text
                break
            results = []
            for b in uses:
                res: dict[str, Any] = {"type": "tool_result", "tool_use_id": b.id, "toolset_name": "computer"}
                try:
                    steps = to_env({"action": b.name, **dict(b.input)})
                except Exception as exc:  # noqa: BLE001 - the model hears about it
                    res.update(content=f"Error: {exc}"[:300], is_error=True)
                    results.append(res)
                    continue
                for s in steps:
                    obs = env.step(WebAction(**s))
                    if obs.done:
                        break
                if obs.done:
                    stop = "environment step limit"
                    res["content"] = "The episode ended: step limit reached."
                    results.append(res)
                    break
                if obs.last_action_error:
                    shot = [_img(obs.screenshot_png_b64)] if obs.screenshot_png_b64 else []
                    res.update(content=[{"type": "text", "text": f"Error: {obs.last_action_error}"[:300]}, *shot],
                               is_error=True)
                elif obs.screenshot_png_b64:
                    res["content"] = [_img(obs.screenshot_png_b64)]
                else:  # a step that produced no screenshot (the page was still loading)
                    res["content"] = "No screenshot was available after this action; take one."
                results.append(res)
            if obs.done:
                break
            messages.append({"role": "user", "content": results})
        claim = None if (refusal or stop) else parse_claim(final_text)
        if not obs.done:
            obs = env.step(WebAction(kind="done", claim=claim, message=final_text))
        return obs, {
            "model": model,
            "effort": effort,
            "claim_parsed": claim,
            "final_text": final_text[-2000:],
            "stop": stop,
            "refusal": refusal,
            "model_calls": calls,
            "usd": round(usd, 4),
            "tokens": tokens,
            "agent_s": round(time.monotonic() - t0, 1),
            "transcript": transcript,
        }

    return run
