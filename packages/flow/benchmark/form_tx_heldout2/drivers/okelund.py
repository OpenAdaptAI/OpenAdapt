"""Driver for the Okelund Casualty AgentNet frameset wizard."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from .base import DriverError, Source, fmt_date, norm, option_matches

NAIVE_LABELS: dict[str, Any] = {
    "First Name": "applicant.first_name",
    "Last Name": "applicant.last_name",
    "Date of Birth (MM/DD/YYYY)": ("applicant.date_of_birth", fmt_date),
    "Marital Status": "applicant.marital_status",
    "E-Mail Address": "applicant.email",
    "Garaging Street": "garaging.address_line1",
    "Garaging City": "garaging.city",
    "Garaging ZIP": "garaging.zip",
    "License State": "driver.license_state",
    "Years Licensed": "driver.years_licensed",
    "At-Fault Accidents (3 Yrs)": "driver.accidents_3yr",
    "Moving Violations (3 Yrs)": "driver.violations_3yr",
    "Completed a state-approved driver training course?": "driver.training_course",
    "Model Year": "vehicle.year",
    "Make": "vehicle.make",
    "Model": "vehicle.model",
    "Primary Use": "vehicle.primary_use",
    "Annual Miles": "vehicle.annual_miles",
    "Ownership": "vehicle.ownership",
    "Any custom or aftermarket equipment (wheels, audio, lift kits)?": "vehicle.aftermarket_equipment",
    "Bodily Injury Limits": "coverage.bi_limits",
    "Collision Deductible": "coverage.collision_deductible",
    "Comprehensive Deductible": "coverage.comprehensive_deductible",
    "Rental Reimbursement": "coverage.rental_reimbursement",
    "Policy Effective Date (MM/DD/YYYY)": ("coverage.effective_date", fmt_date),
}

REFERENCE_LABELS: dict[str, Any] = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Date of Birth (MM/DD/YYYY)": ("dob", fmt_date),
    "Marital Status": "marital_status",
    "E-Mail Address": "email",
    "Garaging Street": "street",
    "Garaging City": "city",
    "Garaging ZIP": "zip",
    "License State": "license_state",
    "Years Licensed": "years_licensed",
    "At-Fault Accidents (3 Yrs)": "accidents",
    "Moving Violations (3 Yrs)": "violations",
    "Completed a state-approved driver training course?": "driver_training",
    "Model Year": "vehicle_year",
    "Make": "make",
    "Model": "model",
    "Primary Use": "use",
    "Annual Miles": "annual_miles",
    "Ownership": "ownership",
    "Any custom or aftermarket equipment (wheels, audio, lift kits)?": "aftermarket",
    "Bodily Injury Limits": "bi_limits",
    "Collision Deductible": "coll_ded",
    "Comprehensive Deductible": "comp_ded",
    "Rental Reimbursement": "rental",
    "Policy Effective Date (MM/DD/YYYY)": ("eff_date", fmt_date),
}


def _main(page: Any) -> Any:
    fr = page.frame(name="main")
    if fr is None:
        raise DriverError("no main frame")
    return fr


def lookup_vehicle(page: Any, fr: Any, src: Source) -> None:
    make, model = src.get("Make"), src.get("Model")
    if model is None:
        return
    cur_make = fr.locator("input[name=make]").input_value()
    cur_model = fr.locator("input[name=model]").input_value()
    if norm(cur_model) == norm(model) and (make is None or norm(cur_make) == norm(make)):
        return
    with page.expect_popup() as info:
        fr.click("input[value='Vehicle Lookup...']")
    pop = info.value
    pop.wait_for_load_state()
    for th in pop.locator("table.frm th").all_inner_texts():
        if th.strip():
            src.note(th.strip().rstrip(":"))
    if make is not None:
        opts = pop.locator("select[name=make]").evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
        hit = next((v for v, t in opts if v and option_matches(make, v, t)), None)
        if hit is None:
            raise DriverError(f"Make: no option {make!r}")
        pop.select_option("select[name=make]", value=hit)
    pop.fill("input[name=model]", model)
    pop.click("input[value=Search]")
    pop.wait_for_load_state()
    rows = pop.locator("#results tr[data-model]")
    if not rows.count():
        pop.close()
        if src.kind == "reference":
            raise DriverError(f"no vehicles for {make} {model}")
        return
    texts = [rows.nth(i).locator("td.m").inner_text().strip() for i in range(rows.count())]
    idx = src.pick("Model", texts, model)
    with pop.expect_event("close"):
        rows.nth(idx).locator("input[value=Select]").click()


def fill_rows(page: Any, fr: Any, src: Source) -> None:
    rows = fr.locator("table.frm tr[id^=r_]")
    for i in range(rows.count()):
        row = rows.nth(i)
        if not row.is_visible():
            continue
        label = row.locator("th").inner_text().strip().rstrip(":").strip()
        src.note(label)
        cell = row.locator("td")
        if cell.locator("select").count():
            sel = cell.locator("select")
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
        elif cell.locator("input[type=radio]").count():
            radios = cell.locator("input[type=radio]")
            opts = radios.evaluate_all("rs => rs.map(r => [r.value, r.parentElement.textContent.trim(), r.checked])")
            want = src.get(label)
            idx = None
            if want is not None:
                idx = next((j for j, (v, t, _c) in enumerate(opts) if option_matches(want, v, t)), None)
                if idx is None and src.kind == "reference":
                    raise DriverError(f"{label}: no radio {want!r}")
            elif src.fallback_first and not any(c for _v, _t, c in opts):
                idx = 0
            if idx is not None:
                radios.nth(idx).check()
        elif cell.locator("input[value='Vehicle Lookup...']").count():
            lookup_vehicle(page, fr, src)
        else:
            inp = cell.locator("input").first
            if inp.get_attribute("readonly") is not None:
                continue
            want = src.get(label)
            if want is not None and inp.input_value() != want:
                inp.fill(want)
                if src.kind == "reference":
                    fr.wait_for_timeout(250)  # let as-you-type lookups land before re-checking


def _click_nav(fr: Any, selector: str) -> None:
    with fr.expect_navigation(timeout=15000):
        fr.click(selector)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/an/desk")
    if "/an/login" in page.url:
        page.fill("input[name=agentcode]", src.creds["username"])
        page.fill("input[name=pwd]", src.creds["password"])
        page.click("input[type=submit][value='Log In']")
        page.wait_for_load_state()
    stuck = 0
    for _ in range(40):
        fr = _main(page)
        fr.wait_for_load_state()
        parts = urlsplit(fr.url)
        q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        if parts.path == "/an/login":
            expired = q.get("expired") == "1"
            fr.fill("input[name=agentcode]", src.creds["username"])
            fr.fill("input[name=pwd]", src.creds["password"])
            _click_nav(fr, "input[type=submit][value='Log In']")
            if expired:
                src.events.append("session_expired")
                if src.restart_after_relogin:
                    fr.goto(base + "/an/app?pg=1")
            continue
        pg = q.get("pg", "")
        if pg == "done":
            return "submitted"
        if pg == "review":
            for th in fr.locator("#review th").all_inner_texts():
                src.note(th.strip().rstrip(":"))
            _click_nav(fr, "input[type=submit][value='Submit for Rating']")
            continue
        if pg in ("1", "2", "3", "4"):
            if fr.locator("span.err").count():
                stuck += 1
                if stuck >= 3:
                    return "stuck"
            fill_rows(page, fr, src)
            if src.kind == "reference":
                fill_rows(page, fr, src)  # a careful human looks the page over once more
            _click_nav(fr, "input[type=submit][value='Save & Next >']")
            continue
        raise DriverError(f"unexpected main frame page {fr.url}")
    return "stuck"
