import sys
from playwright.sync_api import sync_playwright
src, dst = sys.argv[1], sys.argv[2]
w = int(sys.argv[3]) if len(sys.argv) > 3 else 1100
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_page(viewport={"width": w, "height": 900})
    p.goto(src if "://" in src else "file://" + src); p.wait_for_timeout(500)
    p.screenshot(path=dst, full_page=True); b.close()
