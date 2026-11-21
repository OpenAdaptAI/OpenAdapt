"""Driver for the Tidewater Bay shadow-DOM quick quote."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import DriverError, Source, find_option, fmt_date, norm

NAIVE_LABELS: dict[str, Any] = {
    "First name": "applicant.first_name",
    "Last name": "applicant.last_name",
    "Date of birth (MM/DD/YYYY)": ("applicant.date_of_birth", fmt_date),
    "Email": "applicant.email",
    "Street address": "property.address_line1",
    "Apt / unit": "property.address_line2",
    "City": "property.city",
    "State": "property.state",
    "ZIP": "property.zip",
    "Year built": "property.year_built",
    "Square footage": "property.square_feet",
    "Roof updated (year)": "property.roof_year",
    "Occupancy": "property.occupancy",
    "Losses in the last 3 years": "history.prior_claims_3yr",
    "Vehicle year": "vehicle.year",
    "Make": "vehicle.make",
    "Model": "vehicle.model",
    "Drivers in household": "household.driver_count",
}

REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth (MM/DD/YYYY)": ("dob", fmt_date),
    "Email": "email",
    "Street address": "street",
    "Apt / unit": "unit",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "Year built": "year_built",
    "Square footage": "sqft",
    "Roof updated (year)": "roof_year",
    "Occupancy": "occupancy",
    "Is the home rented to others for short stays (under 30 days)?": "short_term_rental",
    "Losses in the last 3 years": "losses_3yr",
    "Vehicle year": "vehicle_year",
    "Make": "make",
    "Model": "model",
    "Drivers in household": "driver_count",
}

SCAN = """() => Array.from(document.querySelectorAll('tg-field,tg-choice,tg-typeahead'))
  .filter(e => !e.closest('[hidden]'))
  .map(e => ({tag: e.tagName.toLowerCase(), name: e.getAttribute('name'),
              label: (e.shadowRoot.querySelector('label,.q') || {textContent: ''}).textContent.trim(),
              select: !!e.shadowRoot.querySelector('select')}))"""


def fill_page(page: Any, src: Source) -> None:
    done: set[str] = set()
    for _ in range(40):
        c = next((x for x in page.evaluate(SCAN) if x["name"] not in done), None)
        if c is None:
            return
        done.add(c["name"])
        fill_one(page, src, c)


def fill_one(page: Any, src: Source, c: dict[str, Any]) -> None:
    tag, name, label = c["tag"], c["name"], c["label"]
    src.note(label)
    host = f"{tag}[name='{name}']"
    want = src.get(label)
    if tag == "tg-choice":
        radios = page.locator(f"{host} input[type=radio]")
        opts = radios.evaluate_all("rs => rs.map(r => [r.value, r.parentElement.textContent.trim(), r.checked])")
        idx = None
        if want is not None:
            hit = find_option(src, want, [(v, t) for v, t, _c in opts])
            if hit is None and src.kind == "reference":
                raise DriverError(f"{label}: no option {want!r}")
            idx = next((j for j, (v, _t, _c) in enumerate(opts) if v == hit), None)
        elif src.fallback_first and not any(ch for _v, _t, ch in opts):
            idx = 0
        if idx is not None:
            radios.nth(idx).check()
    elif tag == "tg-typeahead":
        inp = page.locator(f"{host} input")
        if want is None or norm(inp.input_value()) == norm(want):
            return
        inp.click()
        inp.fill(want)
        page.wait_for_selector(f"{host} li[role=option]", timeout=8000)
        opts = page.locator(f"{host} li[role=option]")
        texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
        opts.nth(src.pick(label, texts, want)).click()
        page.wait_for_timeout(80)
    elif c["select"]:
        sel = page.locator(f"{host} select")
        opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
        if want is not None:
            hit = find_option(src, want, opts)
            if hit is None:
                if src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
                return
            sel.select_option(value=hit)
        elif src.fallback_first and sel.input_value() == "":
            sel.select_option(value=next(v for v, _t in opts if v))
    else:
        inp = page.locator(f"{host} input")
        if want is not None and inp.input_value() != want:
            inp.fill(want)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/qq/start")
    stuck = 0
    for _ in range(40):
        page.wait_for_load_state()
        parts = urlsplit(page.url)
        path = parts.path
        if path == "/signin":
            expired = "expired=1" in parts.query
            page.fill("#agent_id", src.creds["username"])
            page.fill("#passphrase", src.creds["password"])
            with page.expect_navigation():
                page.click("button[type=submit]")
            if expired:
                src.events.append("session_expired")
                if src.restart_after_relogin:
                    page.goto(base + "/qq/1")
            continue
        if path.startswith("/qq/done/"):
            return "submitted"
        if path == "/qq/review":
            with page.expect_navigation():
                page.click("button[type=submit].go")
            continue
        if path in ("/qq/1", "/qq/2", "/qq/3"):
            page.wait_for_function("() => customElements.get('tg-field') && document.querySelector('tg-field').shadowRoot")
            if page.locator("p.bad").count():
                stuck += 1
                if stuck >= 3:
                    return "stuck"
            fill_page(page, src)
            with page.expect_navigation():
                page.click("button.go")
            continue
        raise DriverError(f"unexpected page {page.url}")
    return "stuck"
