"""Dev-case probe: what the form layer's surface sees on each portal's first page.

Signs in with the portal's synthetic test login, opens the entry page for the
dev case, and prints the controls the PlaywrightFormSurface snapshot reports.
Reads nothing from the answer key.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # packages/flow

from benchmark.form_tx_heldout.portals import PORTALS  # noqa: E402
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.types import UIProfileSpec  # noqa: E402


def post(url: str, body: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req).read()


def sign_in(page, username: str, password: str) -> None:
    """Operator sign-in on a plain login page (CSS locators pierce shadow DOM)."""
    frame = page
    for candidate in [page.main_frame, *page.frames]:
        if candidate.locator("input[type=password]").count():
            frame = candidate
            break
    frame.locator("input[type=text], input[type=email], input:not([type])").first.fill(
        username
    )
    frame.locator("input[type=password]").first.fill(password)
    frame.locator("input[type=password]").first.press("Enter")
    page.wait_for_load_state()
    page.wait_for_timeout(500)


def main() -> None:
    from playwright.sync_api import sync_playwright

    only = sys.argv[1:] or list(PORTALS)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for pid in only:
            spec = PORTALS[pid]
            server, base = spec.start(host="127.0.0.1", port=0)
            try:
                post(f"{base}/__oracle/load_case", {"case_id": spec.dev_case})
                page = browser.new_page()
                login = spec.login
                if login.get("required_at_start") and login.get("path"):
                    page.goto(base + login["path"])
                    sign_in(page, login["username"], login["password"])
                page.goto(base + spec.entry_path)
                page.wait_for_load_state()
                surface = PlaywrightFormSurface(page, UIProfileSpec(profile_id="probe"))
                snap = surface.snapshot()
                print(
                    f"== {pid} url={snap.url_origin_and_path} controls={len(snap.controls)} frames={len(page.frames)}"
                )
                for c in snap.controls:
                    print(
                        f"   {c.tag}/{c.input_type or c.role} label={c.label_text!r} "
                        f"name={c.name_attr!r} sel={c.dom_selector!r} vis={c.visible} val={c.current_value!r}"
                    )
                page.close()
            finally:
                server.shutdown()
        browser.close()


if __name__ == "__main__":
    main()
