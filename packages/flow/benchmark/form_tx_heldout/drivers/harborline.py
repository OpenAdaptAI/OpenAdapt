"""Driver for the Harborline Mutual legacy table-layout portal."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import DriverError, Source, digits, fmt_date, option_matches

NAIVE_LABELS: dict[str, Any] = {
    "First Name": "applicant.first_name",
    "Last Name": "applicant.last_name",
    "Date of Birth (MM/DD/YYYY)": ("applicant.date_of_birth", fmt_date),
    "Phone": "applicant.phone",
    "E-mail": "applicant.email",
    "Prior Harborline Policy No. (if any)": "history.prior_policy_number",
    "Street Address": "property.address_line1",
    "City": "property.city",
    "State": "property.state",
    "ZIP Code": "property.zip",
    "Year Built": "property.year_built",
    "Total Living Area (sq ft)": "property.square_feet",
    "Living Area (sq ft)": "property.square_feet",
    "Number of Stories": "property.stories",
    "Stories": "property.stories",
    "Roof Covering Last Replaced (Year)": "property.roof_year",
    "Roof Covering Material": "property.roof_material",
    "Number of Losses (Last 5 Yrs)": "history.prior_claims_5yr",
    "Quality Grade": "property.construction_quality",
    "Policy Effective Date (MM/DD/YYYY)": ("coverage.effective_date", fmt_date),
    "All Peril Deductible": "coverage.deductible",
    "Personal Liability (E)": "coverage.liability_limit",
    "Swimming Pool on Premises?": "property.has_pool",
    "Is the pool enclosed by a locking fence?": "property.pool_fenced",
}

REFERENCE_LABELS: dict[str, Any] = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Date of Birth (MM/DD/YYYY)": ("dob", fmt_date),
    "Phone": "phone",
    "E-mail": "email",
    "Prior Harborline Policy No. (if any)": "prior_policy",
    "Street Address": "street",
    "City": "city",
    "State": "state",
    "ZIP Code": "zip",
    "Year Built": "year_built",
    "Total Living Area (sq ft)": "sqft",
    "Living Area (sq ft)": "sqft",
    "Number of Stories": "stories",
    "Stories": "stories",
    "Construction Class": "construction_class",
    "Roof Covering Last Replaced (Year)": "roof_year",
    "Roof Covering Material": "roof_material",
    "Number of Losses (Last 5 Yrs)": "losses_5yr",
    "Quality Grade": "intake:property.construction_quality",
    "Dwelling Coverage (A)": "cov_a",
    "Policy Effective Date (MM/DD/YYYY)": ("eff_date", fmt_date),
    "All Peril Deductible": "deductible",
    "Personal Liability (E)": "liability",
    "Swimming Pool on Premises?": "pool",
    "Is the pool enclosed by a locking fence?": "pool_fenced",
}

ROWS = "table.frm tr:has(td.lbl)"


def fill_rows(page: Any, src: Source) -> None:
    rows = page.locator(ROWS)
    for i in range(rows.count()):
        row = rows.nth(i)
        if not row.is_visible():
            continue
        label = row.locator("td.lbl").inner_text().strip().rstrip(":").strip()
        src.note(label)
        cell = row.locator("td").nth(1)
        if cell.locator("select").count():
            sel = cell.locator("select")
            opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
            want = src.get(label)
            if want is not None:
                hit = next((v for v, t in opts if v and option_matches(want, v, t)), None)
                if hit is None:
                    if src.kind == "reference":
                        raise DriverError(f"{label}: no option for {want!r}")
                    continue
                sel.select_option(value=hit)
            elif src.fallback_first and sel.input_value() == "":
                first = next(v for v, _t in opts if v)
                sel.select_option(value=first)
        elif cell.locator("input[type=radio]").count():
            radios = cell.locator("input[type=radio]")
            opts = radios.evaluate_all(
                "rs => rs.map(r => [r.value, (r.nextSibling && r.nextSibling.textContent || '').trim(), r.checked])"
            )
            want = src.get(label)
            idx = None
            if want is not None:
                idx = next((j for j, (v, t, _c) in enumerate(opts) if option_matches(want, v, t)), None)
                if idx is None and src.kind == "reference":
                    raise DriverError(f"{label}: no radio for {want!r}")
            elif src.fallback_first and not any(c for _v, _t, c in opts):
                idx = 0
            if idx is not None:
                radios.nth(idx).check()
        elif cell.locator("input[value='Estimate RC']").count():
            run_estimator(page, cell, label, src)
        elif cell.locator("input[type=text]").count():
            want = src.get(label)
            if want is not None:
                cell.locator("input[type=text]").fill(want)


# The portal hands figures of $500,000 or more back rounded to the nearest
# $10,000, so a value that really arrived is at most $5,000 off the display.
HANDOFF_TOLERANCE = 5000


def handed_back(displayed: int, current: str) -> bool:
    """Whether the parent box holds the value the estimator handed back."""
    got = digits(current)
    return bool(got) and abs(int(got) - displayed) <= HANDOFF_TOLERANCE


def run_estimator(page: Any, cell: Any, label: str, src: Source) -> None:
    with page.expect_popup() as info:
        cell.locator("input[value='Estimate RC']").click()
    pop = info.value
    pop.wait_for_load_state()
    fill_rows(pop, src)
    pop.locator("input[type=submit][value='Calculate']").click()
    pop.wait_for_load_state()
    shown = pop.locator("#rce_value").inner_text()
    displayed = int(shown.replace("$", "").replace(",", ""))
    with pop.expect_event("close"):
        # The button closes its own window; a click that loses that race
        # raises after the click went through, so a closed popup is not an
        # error by itself. Whether the value arrived is checked below.
        try:
            pop.locator("input[value='Use This Value']").click(no_wait_after=True)
        except Exception:
            if not pop.is_closed():
                raise
    box = cell.locator("input[type=text]")
    current = box.input_value()
    if src.kind == "reference" and not handed_back(displayed, current):
        raise DriverError(
            f"{label}: the estimator closed without handing ${displayed:,} "
            f"to the form (the box holds {current!r})"
        )
    final = src.estimator(label, displayed, current)
    if final is not None:
        box.fill(final)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/quote/new")
    stuck = 0
    for _ in range(60):
        page.wait_for_load_state()
        parts = urlsplit(page.url)
        path = parts.path
        if path == "/agent/login":
            expired = "expired=1" in parts.query
            page.fill("input[name=userid]", src.creds["username"])
            page.fill("input[name=password]", src.creds["password"])
            page.click("input[type=submit][value='Sign In']")
            page.wait_for_load_state()
            if expired:
                src.events.append("session_expired")
                if src.restart_after_relogin:
                    page.goto(base + "/quote/step/1")
            continue
        if path.startswith("/quote/step/"):
            if page.locator("p.err").count():
                stuck += 1
                if stuck >= 3:
                    return "stuck"
            fill_rows(page, src)
            page.click("input[type=submit][name=nav][value='Next >>']")
            continue
        if path == "/quote/review":
            page.click("input[type=submit][value='Submit Application']")
            continue
        if path.startswith("/quote/confirm/"):
            return "submitted"
        raise DriverError(f"unexpected page {page.url}")
    return "stuck"
