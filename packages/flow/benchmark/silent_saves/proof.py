"""Side-by-side images: what the agent reported, and what the record held.

    python -m silent_saves.proof --extract   # from the claims run's local cache
    python -m silent_saves.proof             # render proof/*.png ($0, no model)

The images use the scoreboard's claims run ``mockmed/claims/2026-10-19-b``:

* the agent's final message, verbatim, and the run's ground truth from
  ``scoreboard/raw/mockmed/claims/2026-10-19-b/items.csv``;
* the store before the agent started, and verify-run's verdict, from each
  item's verify-run baseline and receipt. ``--extract`` copies only the rows,
  the read times and the verdicts into ``proof/evidence.json``; the baseline's
  salt stays out.

The claims run kept no screenshots. The screen panel is MockMed re-rendered
locally with the same fault and note: the page the agent was shown, not the
agent's own capture. Each image says so.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
from pathlib import Path
from typing import Any

from .app import MockMed

HERE = Path(__file__).resolve().parent
PROOF = HERE / "proof"
EVIDENCE = PROOF / "evidence.json"
RUN = "2026-10-19-b"
ROWS = HERE.parent / "scoreboard" / "raw" / "mockmed" / "claims" / RUN / "items.csv"
CACHE = (
    Path(
        os.environ.get(
            "SCOREBOARD_CACHE", Path.home() / ".cache" / "openadapt-scoreboard"
        )
    )
    / "mockmed"
    / "claims"
    / RUN
)
#: One item per fault kind that a picture explains well.
ITEMS = (31, 11, 21, 30)
#: What the store held afterwards, in words, by the run's ground truth.
AFTER = {
    ("optimistic", "missing"): "Nothing was saved. The server rejected the save; "
    "the page had already shown it as saved.",
    ("partial", "wrong"): "An encounter was saved without the note.",
    ("wrong_record", "wrong"): "The note was saved on another patient.",
    ("stale", "collateral_loss"): "The note was saved, and another clinician's "
    "earlier encounter (the penicillin allergy) was deleted.",
}


def extract() -> None:
    rows = {int(r["item"]): r for r in csv.DictReader(ROWS.open())}
    out = []
    for i in ITEMS:
        r = rows[i]
        base = json.loads((CACHE / f"item-{i:03d}" / "baseline.json").read_text())
        receipt = json.loads(
            (CACHE / f"item-{i:03d}" / "share" / "effect-receipt.json").read_text()
        )
        rb = receipt["evidence"]["readback"]
        out.append(
            {
                "item": i,
                "fault": r["fault"],
                "note": json.loads(
                    (CACHE / f"item-{i:03d}" / "params.json").read_text()
                )["note"],
                "agent_claimed_success": r["claimed_success"] == "True",
                "agent_final_text": r["final_text"],
                "ground_truth": r["ground_truth"],
                "before_read_at": base["state"]["captured_at"],
                "before_records": base["state"]["records"],
                "verify_read_at": rb["read_at"],
                "verify_effect_verdict": receipt["receipt"]["effect_verdict"],
                "verify_outcome": receipt["receipt"]["outcome"],
                "verify_observed": rb["effects"][0]["observed_effect"],
                "verify_observed_count": rb["effects"][0]["observed_count"],
            }
        )
    PROOF.mkdir(exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {"source": f"scoreboard claims run mockmed/{RUN}", "items": out}, indent=2
        )
        + "\n"
    )


def _screen(page: Any, app: MockMed, fault: str, note: str) -> bytes:
    app.prepare(fault)
    page.goto(app.url, wait_until="networkidle")
    page.locator(".open-btn").first.click()
    page.locator("#new-encounter").click()
    page.locator("#type-triage").click()
    page.locator("#note").fill(note)
    page.locator("#save-encounter").click()
    page.locator("#saved-banner").wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(1200)
    return page.screenshot()


def _records(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='empty'>No encounters.</p>"
    cells = "".join(
        f"<tr><td>{html.escape(str(r['patient_id']))}</td><td>{html.escape(str(r['type']))}</td>"
        f"<td>{html.escape(str(r['note']))}</td></tr>"
        for r in rows
    )
    return f"<table><tr><th>Patient</th><th>Type</th><th>Note</th></tr>{cells}</table>"


PAGE = """<!doctype html><meta charset="utf-8"><style>
body {{ margin: 0; font: 15px/1.4 -apple-system, "Segoe UI", sans-serif; color: #1a1a1a; background: #fff; width: 1400px; }}
header {{ padding: 18px 28px 6px; }}
h1 {{ font-size: 22px; margin: 0; }}
.sub {{ color: #555; margin-top: 4px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding: 14px 28px 24px; }}
.panel {{ border: 1px solid #d0d0d0; border-radius: 8px; padding: 14px 16px; }}
.panel h2 {{ font-size: 16px; margin: 0 0 8px; }}
.said {{ background: #eef6ee; border-left: 4px solid #2e7d32; padding: 10px 12px; margin: 0 0 12px; }}
.held {{ background: #fdecea; border-left: 4px solid #c62828; padding: 10px 12px; margin: 10px 0; }}
img {{ width: 100%; border: 1px solid #ddd; border-radius: 4px; }}
.cap {{ color: #666; font-size: 12px; margin-top: 4px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
td, th {{ border: 1px solid #ddd; padding: 4px 6px; text-align: left; }}
.empty {{ color: #666; margin: 4px 0; }}
.label {{ font-weight: 600; margin-top: 10px; }}
.src {{ color: #666; font-size: 12px; padding: 0 28px 16px; }}
</style>
<header><h1>Item {item} ({fault}): the agent reported a save; the record disagrees</h1>
<div class="sub">silent-saves fault set 1.0.0 on MockMed. Agent: Claude Sonnet 5.5 computer use, working alone.</div></header>
<div class="cols">
<div class="panel"><h2>What the agent reported</h2>
<div class="said">{final_text}</div>
<img src="data:image/png;base64,{shot}">
<div class="cap">MockMed's screen after the save under this fault, with this note, re-rendered locally.
The claims run kept no screenshots, so this is the page the agent was shown, not its own capture.</div></div>
<div class="panel"><h2>What the system of record held</h2>
<div class="label">Before the agent started ({before_at})</div>{before}
<div class="label">After</div>
<div class="held">{after}</div>
<div class="label">OpenAdapt read-back (verify-run, {verify_at})</div>
<div>Effect {verdict}: {observed_count} matching Triage encounter(s) found, observed <code>{observed}</code>.</div></div>
</div>
<div class="src">Source: scoreboard claims run mockmed/{run}, item {item} (items.csv, verify-run baseline and receipt).
The store's "after" is the run's ground truth, compared from the store before and after.</div>
"""


def render() -> list[Path]:
    from playwright.sync_api import sync_playwright

    doc = json.loads(EVIDENCE.read_text())
    app = MockMed()
    written = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 600})
            for ev in doc["items"]:
                # MockMed keeps saved encounters in page memory: a fresh
                # context per item, or earlier items' notes show up.
                ctx = browser.new_context(viewport={"width": 1100, "height": 560})
                shot = _screen(ctx.new_page(), app, ev["fault"], ev["note"])
                ctx.close()
                page.set_content(
                    PAGE.format(
                        item=ev["item"],
                        fault=html.escape(ev["fault"]),
                        final_text=html.escape(ev["agent_final_text"]),
                        shot=base64.b64encode(shot).decode(),
                        before_at=html.escape(
                            ev["before_read_at"][:19].replace("T", " ") + " UTC"
                        ),
                        before=_records(ev["before_records"]),
                        after=html.escape(AFTER[(ev["fault"], ev["ground_truth"])]),
                        verify_at=html.escape(
                            ev["verify_read_at"].replace("T", " ").replace("Z", " UTC")
                        ),
                        verdict=html.escape(ev["verify_effect_verdict"]),
                        observed_count=ev["verify_observed_count"],
                        observed=html.escape(ev["verify_observed"]),
                        run=RUN,
                    )
                )
                path = PROOF / f"item-{ev['item']:03d}-{ev['fault']}.png"
                page.screenshot(path=str(path), full_page=True)
                written.append(path)
            browser.close()
    finally:
        app.close()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extract", action="store_true")
    args = parser.parse_args(argv)
    if args.extract:
        extract()
        print(EVIDENCE)
        return 0
    for path in render():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
