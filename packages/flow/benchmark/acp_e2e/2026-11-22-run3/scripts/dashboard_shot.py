"""Screenshot the Cloud dashboard pages from the local mock-mode dev server, headless,
and save each page's visible text. Usage: dashboard_shot.py ORIGIN OUTDIR"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
origin, out = sys.argv[1], Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_page(viewport={"width": 1280, "height": 900})
    for name in ("ledger", "readiness"):
        r = p.goto(f"{origin}/dashboard/{name}", wait_until="load", timeout=180000)
        p.wait_for_timeout(4000)
        p.screenshot(path=str(out / f"dashboard-{name}.png"), full_page=True)
        (out / f"dashboard-{name}.txt").write_text(f"HTTP {r.status if r else None} {p.url}\n" + p.inner_text("body"))
        print(name, r.status if r else None, p.url)
    b.close()
