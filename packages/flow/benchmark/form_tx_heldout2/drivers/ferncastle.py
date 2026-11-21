"""Driver for the Ferncastle Indemnity tabbed producer workstation."""

from __future__ import annotations

from typing import Any

from .base import DriverError, Source, norm, option_matches

NAIVE_LABELS: dict[str, Any] = {
    "Client number": "history.client_number",
    "First name": "applicant.first_name",
    "Last name": "applicant.last_name",
    "Email": "applicant.email",
    "Phone": "applicant.phone",
    "Street address": "property.address_line1",
    "City": "property.city",
    "State": "property.state",
    "ZIP code": "property.zip",
    "Year built": "property.year_built",
    "Number of units": "property.units",
    "Construction": "property.construction",
    "Total square feet": "property.square_feet",
    "Roof replaced (year)": "property.roof_year",
    "Mortgagee name (blank if none)": "property.mortgagee_name",
    "Monthly rent per unit ($)": "rental.monthly_rent",
    "Policy effective date": "coverage.effective_date",
    "Dwelling limit ($)": "coverage.dwelling_limit",
    "Loss of rents": "coverage.loss_of_rents",
    "Premises liability": "coverage.liability_limit",
    "Deductible": "coverage.deductible",
}

REFERENCE_LABELS: dict[str, Any] = {
    "Client number": "client_no",
    "First name": "first_name",
    "Last name": "last_name",
    "Email": "email",
    "Phone": "phone",
    "Street address": "street",
    "City": "city",
    "State": "state",
    "ZIP code": "zip",
    "Year built": "year_built",
    "Number of units": "units",
    "Construction": "construction",
    "Total square feet": "sqft",
    "Roof replaced (year)": "roof_year",
    "Mortgagee name (blank if none)": "mortgagee",
    "Monthly rent per unit ($)": "monthly_rent",
    "Unit occupancy": "occupancy",
    "Policy effective date": "eff_date",
    "Dwelling limit ($)": "dwelling_limit",
    "Loss of rents": "loss_of_rents",
    "Premises liability": "liability",
    "Deductible": "deductible",
}

ORDER = ["insured", "location", "building", "rental", "coverage", "rate"]


def _relogin_open(page: Any) -> bool:
    return bool(page.evaluate("() => document.getElementById('relogin').open"))


def _relogin(page: Any, src: Source) -> None:
    src.note(page.locator("#rl-title").inner_text())
    page.fill("#rl-user", src.creds["username"])
    page.fill("#rl-pass", src.creds["password"])
    page.click("#rl-go")
    page.wait_for_function("() => !document.getElementById('relogin').open", timeout=10000)
    page.wait_for_timeout(300)
    src.events.append("session_expired")


def _wait_settled(page: Any, tab: str | None = None) -> None:
    page.wait_for_function(
        """t => { const p = document.getElementById('panel');
                  if (document.getElementById('relogin').open) return true;
                  if (p.dataset.busy) return false;
                  const s = p.dataset.tab || '';
                  return t ? (s === t || s.startsWith('rate') || s === 'done') : (s && s !== 'rate-loading'); }""",
        arg=tab, timeout=15000,
    )
    page.wait_for_timeout(120)


def lookup_client(page: Any, src: Source) -> None:
    want = src.get("Client number")
    current = page.locator("#f-client_no").input_value()
    if not want or norm(current) == norm(want):
        return
    page.click("#find-client")
    page.wait_for_selector("#lookup[open]")
    src.note(page.locator("label[for=lk-number]").inner_text())
    page.fill("#lk-number", want)
    page.click("#lk-search")
    page.wait_for_selector("#lk-match, #lk-none", timeout=10000)
    if page.locator("#lk-none").count():
        page.click("#lk-cancel")
        if src.kind == "reference":
            raise DriverError(f"client {want!r} not found")
        return
    page.click("#lk-apply")
    page.wait_for_function("() => !document.getElementById('lookup').open", timeout=10000)
    _wait_settled(page, "insured")


def fill_panel(page: Any, src: Source) -> None:
    rows = page.locator("#panel .fc-row")
    for i in range(rows.count()):
        row = rows.nth(i)
        label = row.locator("label").inner_text().strip()
        src.note(label)
        if row.locator("select").count():
            sel = row.locator("select")
            opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
            want = src.get(label)
            if want is not None:
                hit = next((v for v, t in opts if v and option_matches(want, v, t)), None)
                if hit is None:
                    if src.kind == "reference":
                        raise DriverError(f"{label}: no option {want!r}")
                    continue
                sel.select_option(value=hit)
            elif src.fallback_first and sel.input_value() == "":
                sel.select_option(value=next(v for v, _t in opts if v))
            continue
        inp = row.locator("input")
        if inp.get_attribute("readonly") is not None:
            continue
        want = src.get(label)
        if want is not None and inp.input_value() != want:
            inp.fill(want)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/desk/quote")
    if "/desk/login" in page.url:
        page.fill("#producer", src.creds["username"])
        page.fill("#password", src.creds["password"])
        page.click("button[type=submit]")
        page.wait_for_load_state()
    page.wait_for_selector("#panel[data-tab=insured]", timeout=15000)
    idx = 0
    stuck = 0
    for _ in range(60):
        if _relogin_open(page):
            _relogin(page, src)
            _wait_settled(page)
            if src.restart_after_relogin:
                idx = 0
            else:  # carry on from wherever the app put us
                st = page.locator("#panel").get_attribute("data-tab") or ""
                if st.startswith("rate") or st == "done":
                    idx = ORDER.index("rate")
                elif st in ORDER:
                    idx = ORDER.index(st)
            continue
        tab = ORDER[idx]
        state = page.locator("#panel").get_attribute("data-tab") or ""
        if tab == "rate":
            if not state.startswith("rate") and state != "done":
                page.click("#tab-rate")
            _wait_settled(page, "rate")
            if _relogin_open(page):
                continue
            state = page.locator("#panel").get_attribute("data-tab")
            if state == "rate-incomplete":
                return "stuck"
            if state == "done":
                return "submitted"
            for row in page.locator("#rate-summary tr td:first-child").all_inner_texts():
                src.note(row)
            page.click("#request")
            page.wait_for_function(
                "() => document.getElementById('panel').dataset.tab === 'done' || document.getElementById('relogin').open",
                timeout=15000,
            )
            continue
        if state != tab:
            page.click(f"#tab-{tab}")
            _wait_settled(page, tab)
        if tab == "insured":
            lookup_client(page, src)
        fill_panel(page, src)
        page.click("#save")
        _wait_settled(page)
        if _relogin_open(page):
            continue
        if page.locator("#panel .fc-err").count():
            stuck += 1
            if stuck >= 3:
                return "stuck"
            continue
        idx += 1
    return "stuck"
