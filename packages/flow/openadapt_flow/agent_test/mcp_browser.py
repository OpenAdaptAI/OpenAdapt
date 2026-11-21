"""A small stdio MCP server that drives the scenario's signed-in browser page.

    python -m openadapt_flow.agent_test.mcp_browser --cdp http://127.0.0.1:PORT --base http://127.0.0.1:APP

`openadapt test` writes a client config for it (``OPENADAPT_TEST_MCP_CONFIG``)
so a coding agent (Claude Code, or any MCP client) can do the scenario's
task through tools instead of pixels. It attaches to the harness's own
browser over the DevTools Protocol, so every save still goes through the
front door. It refuses to open another origin. It speaks MCP's stdio
transport (newline-delimited JSON-RPC 2.0) with no SDK dependency.

Tools: ``page`` (the page's text and its form controls with selectors),
``goto``, ``fill``, ``select``, ``click``, ``press``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from openadapt_flow.agent_test.browser import chord, off_app

_CONTROLS_JS = """
() => {
  const out = [];
  const sel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    return null;
  };
  const label = (el) => {
    if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim();
  };
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  for (const el of document.querySelectorAll('input, select, textarea, button, a[href]')) {
    if (!vis(el) || el.type === 'hidden') continue;
    const s = sel(el);
    const text = (el.innerText || el.value || '').trim().slice(0, 60);
    const item = {tag: el.tagName.toLowerCase(), type: el.type || '', selector: s, label: label(el).slice(0, 60), text};
    if (el.tagName === 'SELECT') item.options = Array.from(el.options).slice(0, 80).map(o => o.value + '=' + o.text.trim());
    if (!s && !text) continue;
    out.push(item);
    if (out.length >= 250) break;
  }
  return out;
}
"""

TOOLS = [
    {
        "name": "page",
        "description": "The current page: its URL, its visible text (cut to 6000 characters), and its visible form controls, buttons and links with a CSS selector for each.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "goto",
        "description": "Open a page of the app. Takes a path such as /contact/list.php or a full URL on the app's origin.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into the input or textarea at a CSS selector (replacing its value).",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option (by value or label) in the select at a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
        },
    },
    {
        "name": "click",
        "description": "Click the element at a CSS selector, or (with text) the first visible element showing that text.",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "text": {"type": "string"}},
        },
    },
    {
        "name": "press",
        "description": "Press a key or chord on the focused element, such as Enter or Control+A.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]


class Driver:
    def __init__(self, cdp: str, base: str) -> None:
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_readiness import require_rendering_ready

        self.base = base.rstrip("/")
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(cdp)
        self.off = off_app([self.base])
        try:
            # The selected page must render before any tool acts on it.
            require_rendering_ready(self.browser, attached_page=self.page)
        except Exception:
            self._pw.stop()
            raise

    @property
    def page(self) -> Any:
        pages = [p for c in self.browser.contexts for p in c.pages]
        mine = [p for p in pages if p.url.startswith(self.base)]
        if not (mine or pages):
            raise RuntimeError("no page is open")
        return (mine or pages)[-1]

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=15000)
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001
            pass

    def call(self, name: str, args: dict[str, Any]) -> str:
        page = self.page
        if name == "page":
            text = page.inner_text("body")[:6000]
            controls = page.evaluate(_CONTROLS_JS)
            # Frames (some apps draw their pages in iframes) are listed too.
            frames = []
            for fr in page.frames[1:12]:
                try:
                    frames.append(
                        {
                            "frame_url": fr.url,
                            "text": fr.inner_text("body")[:2500],
                            "controls": fr.evaluate(_CONTROLS_JS)[:120],
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue
            return json.dumps(
                {"url": page.url, "text": text, "controls": controls, "frames": frames},
                indent=0,
            )[:60000]
        if name == "goto":
            url = str(args["url"])
            url = url if url.startswith("http") else self.base + "/" + url.lstrip("/")
            if self.off(url):
                raise ValueError("only the app's own pages can be opened")
            page.goto(url)
            self.settle()
            return f"opened {page.url}"
        target = self._target(page, str(args.get("selector") or ""))
        if name == "fill":
            target.fill(str(args["selector"]), str(args["value"]), timeout=10000)
            return "filled"
        if name == "select":
            v = str(args["value"])
            try:
                target.select_option(str(args["selector"]), v, timeout=10000)
            except Exception:  # noqa: BLE001 - by label
                target.select_option(str(args["selector"]), label=v, timeout=10000)
            return "selected"
        if name == "click":
            if args.get("selector"):
                target.click(str(args["selector"]), timeout=10000)
            elif args.get("text"):
                target.get_by_text(str(args["text"]), exact=False).first.click(
                    timeout=10000
                )
            else:
                raise ValueError("click needs a selector or text")
            self.settle()
            return f"clicked; now on {self.page.url}"
        if name == "press":
            page.keyboard.press(chord(str(args["key"]).split("+")))
            self.settle()
            return "pressed"
        raise ValueError(f"unknown tool {name!r}")

    def _target(self, page: Any, selector: str) -> Any:
        """The page, or the first frame holding ``selector``."""

        if not selector:
            return page
        try:
            if page.locator(selector).count():
                return page
        except Exception:  # noqa: BLE001
            return page
        for fr in page.frames[1:]:
            try:
                if fr.locator(selector).count():
                    return fr
            except Exception:  # noqa: BLE001
                continue
        return page


def serve(cdp: str, base: str) -> None:
    driver: Optional[Driver] = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method", "")
        if mid is None:
            continue  # a notification
        result: Any
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": (msg.get("params") or {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "openadapt_browser", "version": "1.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                if driver is None:
                    driver = Driver(cdp, base)
                try:
                    text = driver.call(
                        str(params.get("name")), dict(params.get("arguments") or {})
                    )
                    result = {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    }
                except Exception as exc:  # noqa: BLE001 - the agent hears about it
                    result = {
                        "content": [{"type": "text", "text": f"Error: {exc}"[:500]}],
                        "isError": True,
                    }
            else:
                raise KeyError(method)
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except KeyError:
            reply = {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="openadapt_flow.agent_test.mcp_browser", allow_abbrev=False
    )
    ap.add_argument("--cdp", required=True)
    ap.add_argument("--base", required=True)
    a = ap.parse_args(argv)
    serve(a.cdp, a.base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
