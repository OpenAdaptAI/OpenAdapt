"""Driver for the Pinecrest Assurance iframe workspace."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import DriverError, Source, find_option, fmt_plain, norm

NAIVE_LABELS: dict[str, Any] = {
    "Street address": "property.address_line1",
    "City": "property.city",
    "ZIP code": "property.zip",
    "Year built": "property.year_built",
    "Living area": "property.square_feet",
    "Living area (sq ft)": "property.square_feet",
    "Construction type": "property.construction_type",
    "Stories": "property.stories",
    "Year roof was installed": "property.roof_year",
    "Roof material": "property.roof_material",
    "Primary heating": "property.heating_type",
    "Claims in the last 5 years": "history.prior_claims_5yr",
    "Monitored burglar alarm": "property.burglar_alarm",
    "Finish level": "property.finish_level",
    "Deductible": "coverage.deductible",
    "Policy start date": "coverage.effective_date",
}

REFERENCE_LABELS: dict[str, Any] = {
    "Street address": "street",
    "City": lambda s: f"{s.expected['city']}, {s.expected['state']}",
    "ZIP code": "zip",
    "Year built": "year_built",
    "Living area": "living_area_sqft",
    "Area unit": lambda s: "sq ft",
    "Living area (sq ft)": "living_area_sqft",
    "Construction type": "construction",
    "Stories": "stories",
    "Year roof was installed": "roof_year",
    "Roof material": "roof_material",
    "Primary heating": "heating",
    "Was the wood stove installed by a licensed professional?": "stove_installed",
    "Claims in the last 5 years": "prior_claims",
    "Monitored burglar alarm": ("burglar_alarm", fmt_plain),
    "Finish level": "intake:property.finish_level",
    "Dwelling limit": "dwelling_limit",
    "Deductible": "deductible",
    "Policy start date": "effective_date",
}

SCAN = """root => {
  const out = [];
  root.querySelectorAll('.pc-row').forEach(row => {
    row.querySelectorAll('[data-f],[data-combo]').forEach(c => {
      const key = c.dataset.f || c.dataset.combo;
      if (out.some(o => o.key === key)) return;
      let label = '';
      if (c.type === 'radio') {
        const g = c.closest('[role=radiogroup]');
        label = document.getElementById(g.getAttribute('aria-labelledby')).textContent;
      } else {
        const l = root.ownerDocument.querySelector('label[for="' + c.id + '"]');
        label = l ? l.textContent : '';
      }
      out.push({key, label: label.trim(), type: c.getAttribute('role') === 'combobox' ? 'combo' : (c.tagName === 'SELECT' ? 'select' : c.type)});
    });
  });
  return out;
}"""


def _frame(page: Any) -> Any:
    page.wait_for_selector("#quote-frame", timeout=15000)
    fr = page.query_selector("#quote-frame").content_frame()
    fr.wait_for_selector("#root h2", timeout=15000)
    return fr


def _sel(key: str) -> str:
    return f"#root [data-f='{key}']" if key != "city" else "#root [data-combo='city']"


def _blur(fr: Any, key: str) -> None:
    fr.evaluate("s => { const e = document.querySelector(s); if (e) e.blur(); }", _sel(key))
    fr.wait_for_timeout(60)


def _fill_dialog(fr: Any, src: Source) -> None:
    rows = fr.locator("#pc-est-body .pc-row")
    for i in range(rows.count()):
        row = rows.nth(i)
        label = row.locator("label").inner_text().strip()
        src.note(label)
        want = src.get(label)
        if row.locator("select").count():
            s = row.locator("select")
            opts = s.evaluate("s => Array.from(s.options).map(o => [o.value || o.text, o.text])")
            opts = [(v, t) for v, t in opts if t != "Choose..."]
            if want is not None:
                hit = find_option(src, want, opts)
                if hit is None and src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
                if hit is not None:
                    s.select_option(label=dict(opts)[hit])
            elif src.fallback_first and s.input_value() == "":
                s.select_option(label=opts[0][1])
        elif want is not None:
            row.locator("input").fill(want)


def _estimator(fr: Any, src: Source, label: str) -> None:
    fr.click("#pc-calc")
    fr.wait_for_selector("#pc-est[open]")
    _fill_dialog(fr, src)
    fr.click("#pc-est-go")
    fr.wait_for_selector("#pc-estimate", timeout=8000)
    displayed = int(fr.inner_text("#pc-estimate").replace("$", "").replace(",", ""))
    fr.click("#pc-est-apply")
    fr.wait_for_timeout(100)
    current = fr.input_value(_sel("dwelling_limit"))
    final = src.estimator(label, displayed, current)
    if final is not None:
        fr.fill(_sel("dwelling_limit"), final)
        _blur(fr, "dwelling_limit")


def fill_step(fr: Any, src: Source) -> None:
    done: set[str] = set()
    for _ in range(40):
        ctrls = fr.evaluate(SCAN, fr.query_selector("#root"))
        nxt = next((c for c in ctrls if c["key"] not in done), None)
        if nxt is None:
            return
        key, label, typ = nxt["key"], nxt["label"], nxt["type"]
        done.add(key)
        src.note(label)
        sel = _sel(key)
        if key == "dwelling_limit":
            _estimator(fr, src, label)
            continue
        want = src.get(label)
        if typ == "radio":
            radios = fr.locator(sel)
            opts = radios.evaluate_all("rs => rs.map(r => [r.value, r.parentElement.textContent.trim(), r.checked])")
            idx = None
            if want is not None:
                hit = find_option(src, want, [(v, t) for v, t, _c in opts])
                if hit is None and src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
                idx = next((j for j, (v, _t, _c) in enumerate(opts) if v == hit), None)
            elif src.fallback_first and not any(c for _v, _t, c in opts):
                idx = 0
            if idx is not None and not opts[idx][2]:
                radios.nth(idx).click()
                fr.wait_for_timeout(80)
        elif typ == "select":
            opts = fr.locator(sel).evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
            if want is not None:
                hit = find_option(src, want, opts)
                if hit is None and src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
                if hit is not None and fr.input_value(sel) != hit:
                    fr.select_option(sel, value=hit)
                    fr.wait_for_timeout(60)
            elif src.fallback_first and fr.input_value(sel) == "":
                fr.select_option(sel, value=next(v for v, _t in opts if v))
        elif typ == "checkbox":
            if want is not None:
                on = norm(want) in ("yes", "true", "y", "1")
                if fr.is_checked(sel) != on:
                    fr.click(sel)
                    fr.wait_for_timeout(80)
        elif typ == "combo":
            if want is None or norm(fr.input_value(sel)) == norm(want):
                continue
            query = want.split(",")[0].strip()
            fr.fill(sel, query)
            lb = fr.get_attribute(sel, "aria-controls")
            fr.wait_for_selector(f"#{lb} li", timeout=8000)
            opts = fr.locator(f"#{lb} li[role=option]")
            texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
            idx = src.pick(label, texts, want)
            opts.nth(idx).click()
            fr.wait_for_timeout(100)
        else:
            if want is not None and fr.input_value(sel) != want:
                fr.fill(sel, want)
                _blur(fr, key)


def _h2(fr: Any) -> str:
    return fr.inner_text("#root h2").strip()


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/portal")
    stuck = 0
    for _ in range(40):
        page.wait_for_load_state()
        if urlsplit(page.url).path == "/login":
            if "expired=1" in page.url:
                src.events.append("session_expired")
            page.fill("#u", src.creds["username"])
            page.fill("#p", src.creds["password"])
            page.click("button[type=submit]")
            page.wait_for_load_state()
            continue
        fr = _frame(page)
        title = _h2(fr)
        if title == "Quote request received":
            return "submitted"
        if title == "Session ended":
            fr.click("#pc-relogin")
            page.wait_for_url("**/login**")
            continue
        if title == "Review":
            with page.expect_response(lambda r: "/app/api/submit" in r.url):
                fr.click("#pc-submit")
            fr.wait_for_function(
                """() => { const h = document.querySelector('#root h2'); const e = document.getElementById('pc-submit-err');
                           return (h && h.textContent.trim() === 'Quote request received') || (e && e.textContent.trim()); }""",
                timeout=10000,
            )
            if fr.locator("#pc-submit-err").count():
                return "stuck"
            continue
        if fr.locator(".pc-err").count():
            stuck += 1
            if stuck >= 3:
                return "stuck"
        fill_step(fr, src)
        with page.expect_response(lambda r: "/app/api/save" in r.url) as resp:
            fr.click("#pc-next")
        fr.wait_for_timeout(250)
        if resp.value.status == 200:
            fr.wait_for_function("b => { const h = document.querySelector('#root h2'); return h && h.textContent.trim() !== b; }",
                                 arg=title, timeout=10000)
    return "stuck"
