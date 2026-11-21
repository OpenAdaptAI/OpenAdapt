"""Dev-case exploration: sign in, open the entry page, and print what the
surface sees in every frame. Optionally run steps given as JSON: a list of
["fill", frame_url_substring, selector, value] / ["select", ...] /
["click", frame_url_substring, selector] / ["snap"]. Dev case only."""
from __future__ import annotations
import json, sys, urllib.request
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from benchmark.form_tx_heldout2.portals import PORTALS  # noqa
from benchmark.form_tx_heldout_run.run import sign_in  # noqa
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa
from openadapt_flow.form_tx.types import UIProfileSpec  # noqa

def post(url, body):
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})).read()

def dump(page, tag):
    for fr in page.frames:
        try:
            s = PlaywrightFormSurface(fr, UIProfileSpec(profile_id="probe", max_settle_ms=6000))
            snap = s.snapshot(); s._detach()
        except Exception as e:
            print(f"## {tag} {fr.url} ERROR {e!r}"[:300]); continue
        print(f"## {tag} frame={fr.url} title={snap.title!r} banners={list(snap.banner_errors)} modals={[m.message_text[:80] for m in snap.active_modals]}")
        for c in snap.controls:
            d = {k: v for k, v in c.model_dump().items() if k in ("tag","input_type","role","name_attr","label_text","section_text","current_value","checked","dom_selector","required","readonly","group_label","radio_value") and v not in ("", None, False)}
            if c.options: d["options"] = [(o.value, o.label) for o in c.options][:12]
            print("  ", d)
        for p in snap.text_pairs: print("   TP", p.label, "=", p.value)

def main():
    pid = sys.argv[1]; steps = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
    from playwright.sync_api import sync_playwright
    spec = PORTALS[pid]
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        server, base = spec.start(host="127.0.0.1", port=0)
        try:
            post(f"{base}/__oracle/load_case", {"case_id": spec.dev_case})
            page = b.new_page()
            lg = spec.login
            if lg.get("required_at_start") and lg.get("path"):
                page.goto(base + lg["path"]); sign_in(page, lg["username"], lg["password"])
            page.goto(base + spec.entry_path); page.wait_for_load_state(); page.wait_for_timeout(500)
            dump(page, "start")
            main = page
            for i, st in enumerate(steps):
                if st[0] == "snap":
                    dump(page, f"step{i}"); continue
                if st[0] == "main":
                    page = main; continue
                if st[0] == "text":
                    for f in page.frames:
                        try: print(f"## TEXT {f.url}\n" + f.locator("body").inner_text()[:3000])
                        except Exception as e: print("text err", e)
                    continue
                if st[0] == "popup_click":
                    fr = next(f for f in page.frames if st[1] in f.url)
                    with page.context.expect_page() as info:
                        fr.locator(st[2]).first.click()
                    page = info.value; page.wait_for_load_state(); page.wait_for_timeout(500)
                    continue
                fr = next(f for f in page.frames if st[1] in f.url)
                loc = fr.locator(st[2]).first
                if st[0] == "fill": loc.fill(str(st[3]))
                elif st[0] == "select": loc.select_option(label=str(st[3]))
                elif st[0] == "check": loc.check()
                elif st[0] == "click": loc.click()
                elif st[0] == "press": loc.press(st[3])
                page.wait_for_timeout(700)
            dump(page, "end")
        finally:
            server.shutdown(); b.close()

if __name__ == "__main__":
    main()
