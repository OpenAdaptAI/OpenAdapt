"""The model-free reference agents, from the app pack's steps.

- ``scripted`` fills the form with fixed selectors, saves, and reports done:
  it believes the page, like an agent that never checks.
- ``scripted-readback`` does the same, then reads the app's own list page
  back (the pack's ``readback``) and reports done only if the values are
  there exactly once. It still cannot see another record change, or a
  change that lands after it looked.

They cost nothing, so they check the harness and the graders, and give a
CI pipeline a regression to catch without a model.
"""

from __future__ import annotations

import time

from openadapt_flow.agent_test.agents.common import AgentContext, AgentResult
from openadapt_flow.agent_test.browser import run_steps
from openadapt_flow.agent_test.packs import fill


def run_scripted(ctx: AgentContext, readback: bool) -> AgentResult:
    page = ctx.screen.page
    app = ctx.app
    if not app.scripted:
        return AgentResult(
            "RESULT: NOT DONE the app pack has no scripted steps", stopped="no steps"
        )
    try:
        run_steps(page, ctx.base_url, app.scripted, ctx.values)
    except Exception as exc:  # noqa: BLE001
        return AgentResult(
            f"RESULT: NOT DONE the form could not be filled: {exc}"[:300]
        )
    if not readback:
        return AgentResult(f"RESULT: DONE {page.url}")
    rb = app.readback
    if not rb:
        return AgentResult("RESULT: NOT DONE the app pack has no readback")
    time.sleep(float(rb.get("settle_s", 2)))
    try:
        run_steps(page, ctx.base_url, rb.get("steps") or [], ctx.values)
        text = page.inner_text("body")
    except Exception as exc:  # noqa: BLE001
        return AgentResult(f"RESULT: NOT DONE the read-back failed: {exc}"[:300])
    low = text.casefold()
    missing = [
        t
        for t in fill(rb.get("expect_text") or [], ctx.values)
        if str(t).casefold() not in low
    ]
    if missing:
        return AgentResult(f"RESULT: NOT DONE read-back is missing {missing}")
    cnt = rb.get("expect_count")
    if cnt:
        needle = str(fill(cnt["text"], ctx.values)).casefold()
        n = low.count(needle)
        if n != int(cnt["equals"]):
            return AgentResult(
                f"RESULT: NOT DONE read-back found {n} matching records, not {cnt['equals']}"
            )
    return AgentResult(f"RESULT: DONE read back {page.url}")
