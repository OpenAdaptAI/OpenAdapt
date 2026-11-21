"""Driver for the Glasswing renters widget embedded in the resident site."""

from __future__ import annotations

import time
from typing import Any

from .base import DriverError, Source, digits, norm, option_matches


def _suggestion(src: Any) -> str:
    e = src.expected
    unit = f" {e['unit']}" if e["unit"] else ""
    return f"{e['street']}{unit}, {e['city']}, {e['state']} {e['zip']}"


NAIVE_LABELS: dict[str, Any] = {
    "First name": "applicant.first_name",
    "Last name": "applicant.last_name",
    "Date of birth": "applicant.date_of_birth",
    "Email": "applicant.email",
    "Street address": "residence.address_line1",
    "Policy start date": "coverage.start_date",
    "Does your building have a fire sprinkler system?": "building.sprinklered",
    "Roommate to add to the policy (full name)": "household.roommate_name",
    "Number of rooms": "contents.rooms",
    "How furnished are they?": "contents.furnished",
    "Electronics, jewelry and instruments ($)": "contents.valuables",
    "Deductible": "coverage.deductible",
    "Liability limit": "coverage.liability_limit",
}

REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": "dob",
    "Email": "email",
    "Street address": "street",
    "Address suggestion": _suggestion,
    "Policy start date": "start_date",
    "Does your building have a fire sprinkler system?": "sprinklered",
    "Roommate to add to the policy (full name)": "roommate",
    "Number of rooms": "intake:contents.rooms",
    "How furnished are they?": "intake:contents.furnished",
    "Electronics, jewelry and instruments ($)": "intake:contents.valuables",
    "Personal property coverage ($)": "personal_property",
    "Deductible": "deductible",
    "Liability limit": "liability",
}


def _frame(page: Any) -> Any:
    deadline = time.time() + 15
    while time.time() < deadline:
        for fr in page.frames:
            if fr.url.endswith("/gw/embed"):
                return fr
        page.wait_for_timeout(100)
    raise DriverError("widget frame not found")


def _step(fr: Any) -> str:
    return fr.evaluate("() => document.querySelector('gw-quote').dataset.step || ''")


def _label(field: Any) -> str:
    return field.evaluate("f => (f.querySelector(':scope > label, :scope > .q') || {}).textContent || ''").strip()


def _combo(fr: Any, field: Any, label: str, src: Source) -> None:
    want = src.get(label)
    if want is None:
        return
    combo = field.locator("input[role=combobox]")
    exact = src.get("Address suggestion")
    ro = {k: fr.locator(f"#ro-{k}").input_value() for k in ("unit", "city", "state", "zip")}
    unit = f" {ro['unit']}" if ro["unit"] else ""
    current = f"{combo.input_value()}{unit}, {ro['city']}, {ro['state']} {ro['zip']}"
    if norm(combo.input_value()) == norm(want) and ro["zip"] and (exact is None or norm(current) == norm(exact)):
        return
    combo.fill("")
    combo.fill(want)
    fr.locator("#addr-list li[role=option]").first.wait_for(timeout=8000)
    opts = fr.locator("#addr-list li[role=option]")
    texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
    idx = src.pick(label, texts, exact)
    opts.nth(idx).click()
    fr.wait_for_function("() => document.querySelector('gw-quote').shadowRoot.getElementById('ro-zip').value !== ''")


def _radios(field: Any, label: str, src: Source) -> None:
    radios = field.locator("input[type=radio]")
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


def _select(sel: Any, label: str, src: Source) -> None:
    opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text])")
    want = src.get(label)
    if want is not None:
        hit = next((v for v, t in opts if v and option_matches(want, v, t)), None)
        if hit is None:
            if src.kind == "reference":
                raise DriverError(f"{label}: no option {want!r}")
            return
        sel.select_option(value=hit)
    elif src.fallback_first and sel.input_value() == "":
        sel.select_option(value=next(v for v, _t in opts if v))


def _estimator(fr: Any, field: Any, label: str, src: Source) -> None:
    fr.locator("#open-calc").click()
    dlg = fr.locator("#calc")
    dlg.wait_for(state="visible")
    src.note(fr.locator("#calc-title").inner_text())
    for f in dlg.locator(".field").all():
        lab = f.locator("label").inner_text().strip()
        src.note(lab)
        if f.locator("select").count():
            _select(f.locator("select"), lab, src)
        else:
            want = src.get(lab)
            if want is not None:
                f.locator("input").fill(want)
    fr.locator("#c-calc").click()
    out = fr.locator("#calc-total").inner_text()
    if "$" not in out:
        raise DriverError(f"calculator did not produce a figure: {out!r}")
    displayed = int(digits(out))
    fr.locator("#c-use").click()
    dlg.wait_for(state="hidden")
    box = field.locator("input[type=number]")
    final = src.estimator(label, displayed, box.input_value())
    if final is not None:
        box.fill(final)


def fill_step(fr: Any, src: Source) -> None:
    fields = fr.locator("gw-quote form > .field, gw-quote form > .row2 > .field")
    for i in range(fields.count()):
        field = fields.nth(i)
        label = _label(field)
        src.note(label)
        if field.locator("input[role=combobox]").count():
            _combo(fr, field, label, src)
        elif field.locator("[role=radiogroup]").count():
            _radios(field, label, src)
        elif field.locator("select").count():
            _select(field.locator("select"), label, src)
        elif field.locator("#open-calc").count():
            _estimator(fr, field, label, src)
        else:
            inp = field.locator("input")
            if inp.get_attribute("readonly") is not None:
                continue
            want = src.get(label)
            if want is not None and inp.input_value() != want:
                inp.fill(want)


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/resident")
    if "/signin" in page.url:
        page.fill("#email", src.creds["username"])
        page.fill("#password", src.creds["password"])
        page.click("button[type=submit]")
        page.wait_for_load_state()
    fr = _frame(page)
    fr.wait_for_function("() => document.documentElement.dataset.ready === '1'", timeout=15000)
    stuck = 0
    for _ in range(30):
        step = _step(fr)
        if step == "done":
            return "submitted"
        if step == "3":
            for dt in fr.locator("#review dt").all_inner_texts():
                src.note(dt)
            fr.locator("#next").click()
            fr.wait_for_function("() => document.querySelector('gw-quote').dataset.step !== '3'", timeout=15000)
            continue
        fill_step(fr, src)
        fr.locator("#next").click()
        fr.wait_for_timeout(150)
        if _step(fr) == step:
            stuck += 1
            if stuck >= 3:
                return "stuck"
    return "stuck"
