"""A Browser-Use-style agent for `openadapt test`, with no model: the shape, not a strategy.

    openadapt test run --app dolibarr --agent browser-use:benchmark/agent_test_proof/examples/cdp_agent.py

It attaches to the harness's signed-in headless browser over the DevTools
Protocol (``OPENADAPT_TEST_CDP_URL``), as a Browser-Use agent does with
``Browser(cdp_url=...)``, does the Dolibarr task by selectors, and writes
its report to ``OPENADAPT_TEST_REPORT``. A real Browser-Use script would
build an ``Agent(task=os.environ["OPENADAPT_TEST_TASK"], browser=...)`` here
instead, and write the same report from its final result.
"""

from __future__ import annotations

import json
import os

from playwright.sync_api import sync_playwright

from openadapt_flow._browser_readiness import require_rendering_ready


def main() -> None:
    values = json.loads(os.environ["OPENADAPT_TEST_VALUES"])
    base = os.environ["OPENADAPT_TEST_BASE_URL"]
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(os.environ["OPENADAPT_TEST_CDP_URL"])
        page = next(
            p for c in browser.contexts for p in c.pages if p.url.startswith(base)
        )
        require_rendering_ready(browser, attached_page=page)
        if os.environ.get("OPENADAPT_TEST_APP") != "dolibarr":
            raise SystemExit("this example only knows Dolibarr's contact form")
        page.goto(base + "/contact/card.php?action=create")
        page.fill("#lastname", values["lastname"])
        page.fill("#firstname", values["firstname"])
        page.fill("#title", values["poste"])
        page.fill("#phone_pro", values["phone"])
        page.fill("#email", values["email"])
        page.click("input[name=add]")
        page.wait_for_load_state("load")
        # Check the saved card before claiming: the record's own page, read back.
        body = page.inner_text("body")
        ok = values["email"] in body and values["lastname"] in body
    with open(os.environ["OPENADAPT_TEST_REPORT"], "w") as fh:
        json.dump(
            {
                "claimed_success": ok,
                "final_text": "saved and read the card back"
                if ok
                else "the card did not show the values",
                "cost_usd": 0.0,
                "model_calls": 0,
            },
            fh,
        )


if __name__ == "__main__":
    main()
