import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.types import UIProfileSpec
HTML = """<html><body><form name='step' method='post'><table class='frm'>
<tr><td class='lbl'>Swimming Pool on Premises?</td><td><input type='radio' name='pool' value='Y' onclick='hlToggle()'>Yes <input type='radio' name='pool' value='N' onclick='hlToggle()'>No</td></tr>
</table><input type=submit value='Next'></form></body></html>"""
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True); p = b.new_page(); p.set_content(HTML)
    s = PlaywrightFormSurface(p, UIProfileSpec(profile_id="x"))
    for c in s.snapshot().controls: print(c.input_type, repr(c.label_text), c.current_value, c.radio_value, c.options)
    b.close()
