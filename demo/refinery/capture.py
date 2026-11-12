"""Replay the recorded MockMed workflow once and capture what it did.

The source is a recording we own: ``tests/fixtures/jit/playwright_mcp_add_note.json``
in ``packages/flow``, one Playwright MCP run that adds a Triage note in
MockMed (synthetic data). This step replays those actions in headless
Chromium against MockMed's fault server with ``?fault=ok`` and keeps two
things: every request the page sent to the app's API (the wire), and the
system of record before and after (``GET /api/db``).

    python demo/refinery/capture.py

Writes ``results/capture.json``. It is the only step that needs a browser
(``python -m playwright install chromium-headless-shell``); the pipeline and
the tests start from the committed capture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RECORDING = REPO / "packages/flow/tests/fixtures/jit/playwright_mcp_add_note.json"
OUT = HERE / "results" / "capture.json"


def _db(base: str) -> list[dict]:
    with urllib.request.urlopen(base + "api/db", timeout=10) as resp:
        return json.loads(resp.read())["records"]


def _replay(page, actions: list[dict]) -> None:
    for action in actions:
        name, args = action["name"], action.get("arguments", {})
        if name == "browser_snapshot":
            continue
        if name == "browser_click":
            page.click(args["target"])
        elif name == "browser_type":
            page.fill(args["target"], args["text"])
        else:
            raise ValueError(f"unsupported recorded action {name!r}")
    page.wait_for_load_state("networkidle")


def capture(recording: Path = RECORDING) -> dict:
    from openadapt_flow.mockmed.fault_server import serve
    from playwright.sync_api import sync_playwright

    raw = recording.read_bytes()
    rec = json.loads(raw)
    base, _db_handle, stop = serve()
    wire: list[dict] = []
    try:
        urllib.request.urlopen(
            urllib.request.Request(base + "api/reset", data=b"{}", method="POST"),
            timeout=10,
        ).close()
        before = _db(base)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def on_request(req) -> None:
                    url = urlparse(req.url)
                    if not url.path.startswith("/api/"):
                        return  # static files of the app itself
                    body = req.post_data
                    wire.append(
                        {
                            "method": req.method,
                            "path": url.path,
                            "query": url.query,
                            "body": json.loads(body) if body else None,
                        }
                    )

                page.on("request", on_request)
                # The recording starts at the task list (sign-in is setup).
                page.goto(base + "?fault=ok#tasks")
                _replay(page, rec["actions"])
            finally:
                browser.close()
        after = _db(base)
    finally:
        stop()
    return {
        "recording": str(recording.relative_to(REPO)),
        "recording_sha256": hashlib.sha256(raw).hexdigest(),
        "recording_inputs": rec.get("inputs", {}),
        "recorded_actions": len(rec["actions"]),
        "app": "MockMed (openadapt_flow.mockmed.fault_server, ?fault=ok#tasks)",
        "wire": wire,
        "store_before": before,
        "store_after": after,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    result = capture()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"{len(result['wire'])} API request(s); wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
