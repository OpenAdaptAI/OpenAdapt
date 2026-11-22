"""A Claude computer-use candidate for ``openadapt-flow readiness run``.

Use it as the agent command::

    openadapt-flow readiness run --episodes episodes --out runs \\
        --app-url "$APP_URL" --egress scrubbed --spend-cap 1.00 \\
        --agent-cmd "python benchmark/readiness/claude_cu_agent.py"

It reads ``OPENADAPT_GUARD_CDP_URL``, ``OPENADAPT_GUARD_PAGE_URL`` and
``OPENADAPT_TASK`` (the readiness launcher sets them), attaches to the
guarded tab over CDP, and runs the leaderboard's Claude computer-use loop
(``benchmark/fhir_medplum/leaderboard/agents/claude_cu.py``, reused as is)
with a preamble that names no app. The Anthropic SDK reads
``ANTHROPIC_API_KEY`` and ``ANTHROPIC_BASE_URL`` from the environment, so
under ``--egress scrubbed`` every call goes through the flight proxy. It
writes ``usage.json`` (``openadapt.agent-usage/v1``); the proxy's metered
count and cost are what the scorecard uses, and this file is the
cross-check.

Hard caps per task (environment, never raised past the maximum):

``OPENADAPT_CU_MAX_STEPS``
    model calls, default 25, at most 50;
``OPENADAPT_CU_MAX_USD``
    list-price spend, default 1.00, at most 5.00;
``OPENADAPT_CU_MAX_S``
    seconds, default 600, at most 1800.

``OPENADAPT_CU_MODEL`` picks the model (default ``claude-sonnet-5-5``; it
must have a price in the loop's table, so the spend cap can hold). The
loop's own run ledger (the leaderboard's total spend file) is not touched.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

PREAMBLE = """\
You are a staff member using your organisation's web application in a web \
browser. The screen is 1280x800 pixels and you are already signed in. Use \
only this app, and change nothing the task doesn't ask for. Do the task and \
save it once.

When you have finished, or if you can't finish, stop acting and end your \
reply with one final line in exactly one of these two forms:
RESULT: DONE
RESULT: NOT DONE <the reason, in one line>

The task:
"""

DEFAULT_MODEL = "claude-sonnet-5-5"
CAPS = {
    # name: (environment variable, default, maximum)
    "steps": ("OPENADAPT_CU_MAX_STEPS", 25, 50),
    "usd": ("OPENADAPT_CU_MAX_USD", 1.0, 5.0),
    "seconds": ("OPENADAPT_CU_MAX_S", 600, 1800),
}


def caps(env: Optional[dict[str, str]] = None) -> dict[str, float]:
    """The task's caps: the environment's values, clamped to the maximums."""

    env = dict(os.environ if env is None else env)
    out: dict[str, float] = {}
    for name, (var, default, ceiling) in CAPS.items():
        try:
            value = float(env.get(var, default))
        except ValueError:
            value = float(default)
        out[name] = min(max(value, 0.0), float(ceiling))
    return out


def _loop_modules() -> tuple[Any, Any]:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from benchmark.fhir_medplum.leaderboard import common
    from benchmark.fhir_medplum.leaderboard.agents import claude_cu

    return claude_cu, common


def make_meter(label: str, limits: dict[str, float]) -> Any:
    """The loop's ``Meter`` with this task's caps and no shared ledger."""

    _, common = _loop_modules()

    class TaskMeter(common.Meter):
        def before_call(self) -> None:
            if self.calls >= int(limits["steps"]):
                raise common.RunLimit(f"step cap: {int(limits['steps'])} model calls")
            if self.usd >= limits["usd"]:
                raise common.RunLimit(f"spend cap: ${self.usd:.2f} of ${limits['usd']:.2f}")
            if time.monotonic() - self.started > limits["seconds"]:
                raise common.RunLimit(f"time cap: {int(limits['seconds'])} s")

        def record(self, usd: float, tokens_in: int, tokens_out: int, **extra: Any) -> None:
            self.calls += 1
            self.usd += usd
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            self.usage.append({"in": tokens_in, "out": tokens_out, "usd": round(usd, 6)})

    return TaskMeter(label=label)


def run_task(
    instruction: str,
    screen: Any,
    *,
    model: str = DEFAULT_MODEL,
    limits: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """One task through the reused loop; returns ``usage.json``'s content."""

    claude_cu, _ = _loop_modules()
    if model not in claude_cu.PRICES:
        raise SystemExit(f"model {model!r} has no price in the loop's table; the spend cap can't hold")
    limits = limits or caps()
    meter = make_meter(f"claude {model}", limits)
    claude_cu.PREAMBLE = PREAMBLE  # the loop prepends it; this one names no app
    try:
        result = claude_cu.run(instruction, screen, meter, model=model)
        stopped, final = result.stopped, (result.final_text or "").strip().splitlines()[-1:]
    except Exception as exc:  # noqa: BLE001 - the calls made so far are still reported
        stopped, final = f"error: {type(exc).__name__}", []
    return {
        "schema": "openadapt.agent-usage/v1",
        "calls": meter.calls,
        "input_tokens": meter.tokens_in,
        "output_tokens": meter.tokens_out,
        "model": model,
        "cost_usd": round(meter.usd, 6),
        "stopped": stopped,
        "final_line": final[0][:300] if final else None,
    }


def main() -> int:
    from playwright.sync_api import sync_playwright

    _, _ = _loop_modules()
    from benchmark.fhir_medplum.leaderboard.browser import Screen

    cdp_url = os.environ["OPENADAPT_GUARD_CDP_URL"]
    page_url = os.environ["OPENADAPT_GUARD_PAGE_URL"]
    instruction = os.environ["OPENADAPT_TASK"]
    usage_path = Path(os.environ["OPENADAPT_USAGE_PATH"])
    model = os.environ.get("OPENADAPT_CU_MODEL", DEFAULT_MODEL)
    base = page_url.split("#", 1)[0].split("?", 1)[0]
    usage: dict[str, Any] = {"schema": "openadapt.agent-usage/v1", "calls": 0,
                             "input_tokens": 0, "output_tokens": 0, "model": model,
                             "cost_usd": 0.0}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            page = next(
                (p for c in browser.contexts for p in c.pages if p.url.startswith(base)),
                None,
            )
            if page is None:
                print("the guarded tab was not found", file=sys.stderr)
                return 3
            usage = run_task(instruction, Screen(page), model=model)
    finally:
        usage_path.write_text(json.dumps(usage, indent=2, sort_keys=True))
    print(json.dumps({k: usage.get(k) for k in ("calls", "cost_usd", "stopped")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
