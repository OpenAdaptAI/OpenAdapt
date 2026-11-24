"""Driver for the Cobalt Ridge single-page auto quote."""

from __future__ import annotations

from typing import Any

from ..cobalt import STATE_NAME
from .base import DriverError, Source, norm, option_matches


def _state_display(code: Any) -> str:
    return f"{STATE_NAME[str(code)]} ({code})"


def _driver2(field: str):
    def get(src: Any) -> str | None:
        drivers = src.expected["additional_drivers"]
        return str(drivers[0][field]) if drivers else None
    return get


NAIVE_LABELS: dict[str, Any] = {
    "First name": "applicant.first_name",
    "Last name": "applicant.last_name",
    "Date of birth": "applicant.date_of_birth",
    "Email address": "applicant.email",
    "State where licensed": "applicant.license_state",
    "Marital status": "applicant.marital_status",
    "Driver 2 First name": "driver2.first_name",
    "Driver 2 Last name": "driver2.last_name",
    "Driver 2 Date of birth": "driver2.date_of_birth",
    "Driver 2 Relationship to you": "driver2.relationship",
    "Model year": "vehicle.year",
    "Make": "vehicle.make",
    "Model": "vehicle.model",
    "Primary use": "vehicle.primary_use",
    "One-way commute (miles)": "vehicle.commute_miles_one_way",
    "Estimated annual mileage": "vehicle.annual_miles",
    "Garaging ZIP code": "garaging.zip",
    "Garaging city": "garaging.city",
    "Ownership": "vehicle.ownership",
    "Lienholder or lessor name": "vehicle.lienholder",
    "Bodily injury liability": "coverage.bi_limits",
    "Collision deductible": "coverage.collision_deductible",
    "Comprehensive deductible": "coverage.comprehensive_deductible",
}

REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": "dob",
    "Email address": "email",
    "State where licensed": ("license_state", _state_display),
    "Marital status": "marital_status",
    "Driver 2 First name": _driver2("first_name"),
    "Driver 2 Last name": _driver2("last_name"),
    "Driver 2 Date of birth": _driver2("dob"),
    "Driver 2 Relationship to you": _driver2("relationship"),
    "Model year": "vehicle_year",
    "Make": "make",
    "Model": "model",
    "Primary use": "use",
    "One-way commute (miles)": "commute_miles",
    "Used for rideshare or delivery?": "rideshare",
    "Estimated annual mileage": "annual_miles",
    "Garaging ZIP code": "garaging_zip",
    "Garaging city": lambda s: f"{s.expected['garaging_city']}, {s.expected['garaging_state']}",
    "Ownership": "ownership",
    "Lienholder or lessor name": "lienholder",
    "Bodily injury liability": "bi_limits",
    "Collision deductible": "collision_ded",
    "Comprehensive deductible": "comp_ded",
}

TITLES = {"About you": 1, "Other drivers": 2, "Your vehicle": 3, "Coverage": 4,
          "Review your details": 5, "Your quote is ready": 6}


def _title(page: Any) -> str:
    return page.locator("#app h1").first.inner_text().strip()


def _click_and_wait(page: Any, button: Any) -> None:
    """Click a button that talks to the API, then wait for the re-render."""
    before = _title(page)
    with page.expect_response(lambda r: "/api/step/" in r.url or "/api/submit" in r.url, timeout=15000) as resp:
        button.click()
    status = resp.value.status
    page.wait_for_timeout(200)
    if status == 200:
        page.wait_for_function(
            """b => { const h = document.querySelector('#app h1');
                     return document.getElementById('login-modal') || (h && h.textContent.trim() !== b); }""",
            arg=before, timeout=15000,
        )
        page.wait_for_timeout(150)


def _label_of(field: Any) -> str:
    return field.evaluate(
        """f => { const fs = f.closest('fieldset'); const l = f.querySelector('.cr-label').textContent.trim();
                 return fs ? fs.querySelector('legend').textContent.trim() + ' ' + l : l; }"""
    )


def _combo(page: Any, inp: Any, label: str, src: Source) -> None:
    want = src.get(label)
    if want is None:
        return
    if norm(inp.input_value()) == norm(want):
        return
    query = want.split(",")[0].split(" (")[0].strip()
    inp.click()
    inp.fill("")
    inp.fill(query)
    lb = inp.get_attribute("aria-controls")
    page.wait_for_selector(f"#{lb} li", timeout=8000)
    opts = page.locator(f"#{lb} li[role=option]")
    texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
    if not texts:
        if src.kind == "reference":
            raise DriverError(f"{label}: no suggestions for {query!r}")
        return
    idx = src.pick(label, texts, want)
    opts.nth(idx).click()
    page.wait_for_timeout(100)


def fill_step(page: Any, src: Source) -> None:
    i = 0
    while True:
        fields = page.locator("#app .card .cr-field")
        if i >= fields.count():
            break
        field = fields.nth(i)
        label = _label_of(field)
        src.note(label)
        if field.locator("[role=radiogroup]").count():
            radios = field.locator("[role=radio]")
            texts = [radios.nth(j).inner_text().strip() for j in range(radios.count())]
            checked = [radios.nth(j).get_attribute("aria-checked") == "true" for j in range(radios.count())]
            want = src.get(label)
            idx = None
            if want is not None:
                idx = next((j for j, t in enumerate(texts) if option_matches(want, t, t)), None)
                if idx is None and src.kind == "reference":
                    raise DriverError(f"{label}: no option {want!r}")
            elif src.fallback_first and not any(checked):
                idx = 0
            if idx is not None and not checked[idx]:
                radios.nth(idx).click()
                page.wait_for_timeout(50)
        elif field.locator("select").count():
            sel = field.locator("select")
            opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
            want = src.get(label)
            if want is not None:
                hit = next((v for v, t in opts if v and option_matches(want, v, t)), None)
                if hit is None:
                    if src.kind == "reference":
                        raise DriverError(f"{label}: no option {want!r}")
                else:
                    sel.select_option(value=hit)
            elif src.fallback_first and sel.input_value() == "":
                sel.select_option(value=next(v for v, _t in opts if v))
        elif field.locator("input[role=combobox]").count():
            _combo(page, field.locator("input[role=combobox]"), label, src)
        else:
            inp = field.locator("input")
            want = src.get(label)
            if want is not None and inp.input_value() != want:
                inp.fill(want)
        i += 1


def fix_drivers(page: Any, src: Source) -> None:
    sets = page.locator("#app fieldset")
    for k in range(sets.count()):
        src.note(f"Driver {k + 2}")
    if src.kind == "reference":
        want = len(src.expected["additional_drivers"])
        while sets.count() > want:
            page.locator(f"#app fieldset button[aria-label='Remove driver {sets.count() + 1}']").click()
            page.wait_for_timeout(50)
        while sets.count() < want:
            page.get_by_role("button", name="+ Add another driver").click()
            page.wait_for_timeout(50)
    else:
        if sets.count() == 0 and src.get("Driver 2 First name") is not None:
            page.get_by_role("button", name="+ Add another driver").click()
            page.wait_for_timeout(50)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/quote")
    page.wait_for_selector("#app h1", timeout=15000)
    stuck = 0
    for _ in range(60):
        if page.locator("#login-modal").count():
            page.fill("#login-user", src.creds["username"])
            page.fill("#login-pass", src.creds["password"])
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_selector("#login-modal", state="detached", timeout=10000)
            page.wait_for_timeout(300)
            src.events.append("session_expired")
            if src.restart_after_relogin:
                for _b in range(5):
                    if TITLES.get(_title(page)) == 1:
                        break
                    _click_and_wait(page, page.get_by_role("button", name="Back"))
            continue
        title = _title(page)
        step = TITLES.get(title)
        if step is None:
            page.wait_for_timeout(300)
            continue
        if step == 6:
            return "submitted"
        if page.locator(".cr-error").count():
            stuck += 1
            if stuck >= 3:
                return "stuck"
        if step == 5:
            _click_and_wait(page, page.get_by_role("button", name="Get my quote"))
            continue
        if step == 2:
            fix_drivers(page, src)
        fill_step(page, src)
        _click_and_wait(page, page.locator("#app .actions button.primary"))
    return "stuck"
