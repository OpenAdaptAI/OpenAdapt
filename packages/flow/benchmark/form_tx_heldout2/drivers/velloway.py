"""Driver for the Velloway one-page umbrella application."""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlsplit

from .base import DriverError, Source, norm, option_matches

NAIVE_LABELS: dict[str, Any] = {
    "Household account number (optional)": "history.household_account",
    "First name": "applicant.first_name",
    "Last name": "applicant.last_name",
    "Date of birth": "applicant.date_of_birth",
    "Email address": "applicant.email",
    "Phone number": "applicant.phone",
    "Occupation": "applicant.occupation",
    "Address line 1": "home.address_line1",
    "Town or city": "home.city",
    "State": "home.state",
    "ZIP code": "home.zip",
    "Home liability limit": "underlying.home_liability",
    "Rental properties you own": "household.rental_properties",
    "Licensed drivers in your household": "household.drivers",
    "How many of them are under 25?": "household.youthful_drivers",
    "Has any driver under 25 had a moving violation in the last 3 years?": "household.youthful_violations",
    "Vehicles your household owns or leases": "household.vehicles",
    "Auto liability limits": "underlying.auto_liability",
    "Does anyone in your household serve as a director or officer of a nonprofit organization?":
        "household.nonprofit_board",
    "Umbrella limit": "coverage.umbrella_limit",
    "Policy start date": "coverage.start_date",
}

REFERENCE_LABELS: dict[str, Any] = {
    "Household account number (optional)": "intake:history.household_account",
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": "dob",
    "Email address": "email",
    "Phone number": "phone",
    "Occupation": "occupation",
    "Address line 1": "street",
    "Town or city": "city",
    "State": "state",
    "ZIP code": "zip",
    "Home liability limit": "home_liability",
    "Rental properties you own": "rental_properties",
    "Licensed drivers in your household": "drivers",
    "How many of them are under 25?": "youthful_drivers",
    "Has any driver under 25 had a moving violation in the last 3 years?": "youthful_violations",
    "Vehicles your household owns or leases": "vehicles",
    "Auto liability limits": "auto_liability",
    "Does anyone in your household serve as a director or officer of a nonprofit organization?": "nonprofit_board",
    "Umbrella limit": "umbrella_limit",
    "Policy start date": "start_date",
}


def _date(fg: Any, name: str, want: str) -> None:
    d = date.fromisoformat(want)
    for part, val in (("day", d.day), ("month", d.month), ("year", d.year)):
        box = fg.locator(f"#{name}_{part}")
        if box.input_value() != str(val):
            box.fill(str(val))


def _combo(page: Any, fg: Any, label: str, src: Source) -> None:
    want = src.get(label)
    if want is None:
        return
    if norm(fg.locator("#occupation").input_value()) == norm(want):
        return
    combo = fg.locator("#occupation-input")
    combo.fill("")
    combo.fill(want)
    page.locator("#occupation-list li[role=option]").first.wait_for(timeout=8000)
    opts = page.locator("#occupation-list li[role=option]")
    texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
    opts.nth(src.pick(label, texts, want)).click()


def _watercraft(page: Any, src: Source) -> None:
    if src.kind == "reference":
        want = src.expected["watercraft"]
    else:
        want = src.facts.get("household.watercraft")
    rows = page.locator("#wc-rows fieldset.wc")
    for i in range(rows.count()):
        src.note(f"Watercraft {i + 1}")
    if want is None:
        return
    if src.kind == "reference":
        while rows.count() > len(want):
            page.locator(f"#wc-rows button[aria-label='Remove watercraft {rows.count()}']").click()
    while rows.count() < len(want):
        page.click("#wc-add")
    for i, item in enumerate(want):
        row = rows.nth(i)
        row.locator("select[name=wc_type]").select_option(label=item["type"])
        row.locator("input[name=wc_length]").fill(str(item["length_ft"]))


def fill_form(page: Any, src: Source) -> None:
    groups = page.locator("form .fg[data-name]")
    for i in range(groups.count()):
        fg = groups.nth(i)
        if not fg.is_visible():
            continue
        name = fg.get_attribute("data-name")
        if fg.locator(".dates").count():
            label = fg.locator("legend").inner_text().strip()
            src.note(label)
            want = src.get(label)
            if want is not None:
                _date(fg, name, want)
            continue
        if fg.locator("input[type=radio]").count():
            label = fg.locator("legend").inner_text().strip()
            src.note(label)
            radios = fg.locator("input[type=radio]")
            opts = radios.evaluate_all("rs => rs.map(r => [r.value, r.parentElement.textContent.trim(), r.checked])")
            want = src.get(label)
            idx = None
            if want is not None:
                idx = next((j for j, (v, t, _c) in enumerate(opts) if option_matches(want, v, t)), None)
                if idx is None and src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
            elif src.fallback_first and not any(c for _v, _t, c in opts):
                idx = 0
            if idx is not None:
                radios.nth(idx).check()
            continue
        label = fg.locator(":scope > label").inner_text().strip()
        src.note(label)
        if fg.locator("[role=combobox]").count():
            _combo(page, fg, label, src)
            continue
        if fg.locator("select").count():
            sel = fg.locator("select")
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
        inp = fg.locator("input").first
        want = src.get(label)
        if want is not None and inp.input_value() != want:
            inp.fill(want)
    _watercraft(page, src)


def _login(page: Any, src: Source) -> None:
    page.fill("#email", src.creds["username"])
    page.fill("#password", src.creds["password"])
    page.click("button[type=submit]")
    page.wait_for_load_state()


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/umbrella/start")
    stuck = 0
    for _ in range(30):
        page.wait_for_load_state()
        parts = urlsplit(page.url)
        path = parts.path
        if path == "/account/sign-in":
            timed_out = "timeout=1" in parts.query
            _login(page, src)
            if timed_out:
                src.events.append("session_expired")
                if src.restart_after_relogin:
                    page.goto(base + "/umbrella/apply")
            continue
        if path == "/umbrella/start":
            label = page.locator("label[for=account]").inner_text().strip()
            src.note(label)
            want = src.get(label)
            if want:
                page.fill("#account", want)
            page.click("button[type=submit]")
            if page.locator("#account-error").count():
                return "stuck"
            continue
        if path == "/umbrella/apply":
            if page.locator(".esum").count():
                stuck += 1
                if stuck >= 3:
                    return "stuck"
            fill_form(page, src)
            page.click("form button[type=submit]")
            continue
        if path == "/umbrella/summary":
            for dt in page.locator("#summary dt").all_inner_texts():
                src.note(dt)
            page.check("#confirm")
            page.click("form button[type=submit]")
            continue
        if path.startswith("/umbrella/quote/"):
            return "submitted"
        raise DriverError(f"unexpected page {page.url}")
    return "stuck"
