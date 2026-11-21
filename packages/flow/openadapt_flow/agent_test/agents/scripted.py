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

import re
import time
from typing import Any

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
    steps = app.readback.get("steps") or []
    if not steps:
        return AgentResult("RESULT: NOT DONE the app pack has no readback")
    time.sleep(float(app.readback.get("settle_s", 2)))
    try:
        problem = check_readback(page, ctx.base_url, steps, ctx.values)
    except Exception as exc:  # noqa: BLE001
        return AgentResult(f"RESULT: NOT DONE the read-back failed: {exc}"[:300])
    if problem:
        return AgentResult(f"RESULT: NOT DONE read-back: {problem}"[:300])
    return AgentResult(f"RESULT: DONE read back {page.url}")


def check_readback(
    page: Any, base: str, steps: list[dict[str, Any]], values: dict[str, Any]
) -> str:
    """Run read-back steps; the first failed check, or "" when every check holds.

    Check steps: ``expect_text`` (each string on the page, ignoring case),
    ``expect_digits`` (each value's digits in the page's digits, for phones
    an app formats), ``expect_regex`` with ``equals`` (the pattern's first
    group, such as a list's row count).
    """

    def text() -> str:
        # The page's text and the values of its form controls (an edit form
        # shows the saved record in its inputs).
        vals = page.evaluate(
            "() => Array.from(document.querySelectorAll('input, select, textarea')).map(e => e.value).join(' ')"
        )
        return page.inner_text("body") + " " + str(vals)

    for raw in steps:
        st = fill(raw, values)
        if "expect_text" in st:
            low = text().casefold()
            missing = [t for t in st["expect_text"] if str(t).casefold() not in low]
            if missing:
                return f"missing {missing}"
        elif "expect_digits" in st:
            digits = re.sub(r"\D", "", text())
            missing = [
                t
                for t in st["expect_digits"]
                if re.sub(r"\D", "", str(t)) not in digits
            ]
            if missing:
                return f"missing {missing}"
        elif "expect_regex" in st:
            m = re.search(st["expect_regex"], page.inner_text("body"))
            got = m.group(1) if m else None
            if got is None or str(got) != str(st["equals"]):
                return f"{st['expect_regex']} gave {got}, not {st['equals']}"
        else:
            run_steps(page, base, [st], {})
    return ""
