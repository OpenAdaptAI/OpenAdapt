"""A scripted computer-use agent loop on MockMed, for the flight e2e test.

It is the buyer's own loop: it calls the model API at ANTHROPIC_BASE_URL
(the flight proxy), sends the screen as a base64 screenshot each turn, and
performs each ``computer`` tool_use through its own Playwright executor. The
only flight-specific line is the executor swap to ``GatedComputer``.

Usage: python mockmed_agent.py MOCKMED_URL RESULT_JSON
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from openadapt_flow.flight.computer import GatedComputer, HeldResult

#: A synthetic patient name; it must never reach the model provider.
PLANTED = "Zyxwv Qtest"

LABEL_JS = """([x, y]) => {
  let el = document.elementFromPoint(x, y);
  while (el && !['BUTTON','A','INPUT','TEXTAREA','SELECT'].includes(el.tagName)) {
    el = el.parentElement;
  }
  if (!el) return null;
  const role = {BUTTON: 'button', A: 'link', INPUT: 'textbox', TEXTAREA: 'textarea',
                SELECT: 'combobox'}[el.tagName];
  let name = el.getAttribute('aria-label') || '';
  if (!name && el.labels && el.labels.length) name = el.labels[0].innerText;
  if (!name) name = el.innerText || el.placeholder || el.id || '';
  return (role + ' ' + name).trim();
}"""


class PlaywrightComputer:
    """The agent's own executor (one of many a buyer might have)."""

    def __init__(self, page: Any) -> None:
        self.page = page

    def _shot(self) -> dict[str, str]:
        return {"base64_image": base64.b64encode(self.page.screenshot()).decode()}

    def __call__(
        self,
        action: str,
        coordinate: Optional[list[int]] = None,
        text: Optional[str] = None,
        **_: Any,
    ) -> dict[str, str]:
        if action == "left_click" and coordinate:
            self.page.mouse.click(coordinate[0], coordinate[1])
        elif action == "type" and text is not None:
            self.page.keyboard.type(text)
        self.page.wait_for_timeout(150)
        return self._shot()

    def label_at(self, x: int, y: int) -> Optional[str]:
        return self.page.evaluate(LABEL_JS, [x, y])


def call_model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 512,
        "system": f"You are charting for patient {PLANTED}.",
        "tools": [
            {
                "type": "computer_20250124",
                "name": "computer",
                "display_width_px": 1280,
                "display_height_px": 800,
            }
        ],
        "messages": messages,
    }
    request = urllib.request.Request(
        os.environ["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/messages",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": "sk-fake-e2e",
            "anthropic-version": "2023-06-01",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        return json.loads(response.read())


def main(url: str, out: str) -> int:
    held: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url)
        # The agent's own sign-in (MockMed's synthetic demo account).
        page.fill("#username", "nurse.demo")
        page.fill("#password", "mockmed-demo-pass")
        page.click("#signin")
        page.wait_for_selector(".open-btn")
        computer = GatedComputer(PlaywrightComputer(page))  # the one-line swap
        first = computer(action="screenshot")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Open the referral for {PLANTED} and chart a triage note.",
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": first["base64_image"],
                        },
                    },
                ],
            }
        ]
        for _turn in range(12):
            reply = call_model(messages)
            messages.append({"role": "assistant", "content": reply["content"]})
            results = []
            for block in reply["content"]:
                if block.get("type") != "tool_use":
                    continue
                result = computer(**block["input"])
                if isinstance(result, HeldResult):
                    held.append(result.error)
                    content: list[dict[str, Any]] = [
                        {"type": "text", "text": result.error}
                    ]
                else:
                    content = [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": result["base64_image"],
                            },
                        }
                    ]
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": content,
                    }
                )
            if reply.get("stop_reason") != "tool_use":
                break
            messages.append({"role": "user", "content": results})
        saved = page.locator("#saved-banner").count() > 0
        browser.close()
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"saved": saved, "held": held}, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
