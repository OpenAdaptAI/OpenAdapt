"""Corvane wizard driver: sign in, walk steps 1-4, check the review, request the quote."""

from __future__ import annotations

from typing import Any

from ..scoring import load_answer_key, normalize
from .base import DriverError, Source, digits, find_option, fmt_date, norm, us_date

REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("date_of_birth", fmt_date),
    "Email": "email",
    "Phone": "phone",
    "Co-applicant full name": "co_applicant_name",
    "Property street address": "property_street",
    "City": "property_city",
    "State": "property_state",
    "ZIP code": "property_zip",
    "Year built": "year_built",
    "Finished living area (sq ft)": "square_feet",
    "Construction type": "construction",
    "Roof material": "roof_material",
    "Year roof was installed or last replaced": "roof_year",
    "Occupancy": "occupancy",
    "Are any dogs kept at this address?": "dogs",
    "Breed of each dog": "dog_breeds",
    "Has any dog at this address ever bitten a person or another animal?": "dog_bite_history",
    "Mortgagee (lender)": "mortgagee",
    "Loan number": "loan_number",
    # Typed directly unless the intake asks for the estimate.
    "Dwelling (Coverage A) limit": lambda s: None if "dwelling_limit" not in s.facts else str(s.expected["dwelling_limit"]),
    "Estimated replacement cost": "dwelling_limit",
    "Personal liability": "personal_liability",
    "All-perils deductible": "deductible",
    "Policy effective date": ("effective_date", fmt_date),
}

NAIVE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("date_of_birth", fmt_date),
    "Email": "email",
    "Phone": "phone",
    "Co-applicant full name": "spouse_name",
    "Property street address": "property_street",
    "City": "property_city",
    "State": "property_state",
    "ZIP code": "property_zip",
    "Year built": "year_built",
    "Finished living area (sq ft)": "square_feet",
    "Construction type": "construction",
    "Roof material": "roof_material",
    "Year roof was installed or last replaced": "roof_year",
    "Occupancy": "occupancy",
    "Are any dogs kept at this address?": "dogs_on_premises",
    "Breed of each dog": "dog_breeds",
    "Has any dog at this address ever bitten a person or another animal?": "dog_bite_history",
    "Mortgagee (lender)": "lender_name",
    "Loan number": "loan_number",
    "Dwelling (Coverage A) limit": "dwelling_limit",
    "Personal liability": "personal_liability",
    "All-perils deductible": "deductible",
    "Policy effective date": ("effective_date", fmt_date),
}

PORTAL = "corvane"


class Expired(Exception):
    pass


def _login(page: Any, src: Source) -> None:
    page.wait_for_selector("#u")
    src.note("Agency code")
    page.get_by_label("Agency code").fill(src.creds["username"])
    page.get_by_label("Password").fill(src.creds["password"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/quote**")
    page.wait_for_selector("#app[data-ready]")


def _wait_render(page: Any, before: str | None) -> None:
    page.wait_for_function(
        "(b) => location.pathname.startsWith('/login') || location.pathname.startsWith('/quote/result') || "
        "(document.querySelector('#app') && document.querySelector('#app').dataset.ready && "
        "(document.querySelector('#app').dataset.ready !== b || document.querySelector('#app').dataset.errors))",
        arg=before or "",
    )


def _goto_step(page: Any, base: str, n: int) -> None:
    page.goto(f"{base}/quote?step={n}")
    page.wait_for_selector(f"#app[data-step='{n}'][data-ready]")


def _fill_field(page: Any, box: Any, src: Source) -> None:
    name = box.get_attribute("data-name")
    kind = box.get_attribute("data-kind")
    label = (box.locator("label, .lbl").first.inner_text()).strip()
    src.note(label)
    want = src.get(label)
    if kind in ("text", "int", "date", "money"):
        inp = box.locator(f"#f-{name}")
        if want is None and box.locator("#est-open").count():
            _use_estimator(page, inp, src)
            return
        if want is not None:
            inp.fill(want)
        return
    if kind == "select":
        sel = box.locator("select")
        opts = [(o.get_attribute("value") or "", o.inner_text().strip()) for o in sel.locator("option").all()]
        if want is not None:
            val = find_option(src, want, opts)
            if val is None:
                raise DriverError(f"{label}: no option for {want!r}")
            sel.select_option(val)
        elif not sel.input_value() and src.fallback_first:
            sel.select_option(next(v for v, _ in opts if v))
        return
    if kind == "radio":
        radios = box.locator("input[type=radio]").all()
        opts = [(r.get_attribute("value") or "", r.get_attribute("value") or "") for r in radios]
        if want is not None:
            val = find_option(src, want, opts)
            if val is None:
                raise DriverError(f"{label}: no option for {want!r}")
        elif not any(r.is_checked() for r in radios) and src.fallback_first:
            val = opts[0][0]
        else:
            return
        box.locator(f"input[type=radio][value='{val}']").check()
        return
    if kind == "typeahead":
        inp = box.locator(f"#f-{name}")
        if want is None:
            return
        query = want.split(" ISAOA")[0]
        inp.fill("")
        inp.type(query, delay=10)
        box.locator("ul[role=listbox]:not([hidden])").wait_for()
        opts = box.locator("[role=option]")
        try:
            opts.first.wait_for(timeout=3000)
        except Exception as exc:
            raise DriverError(f"{label}: no suggestions for {query!r}") from exc
        texts = [t.strip() for t in opts.all_inner_texts()]
        idx = src.pick(label, texts, want)
        opts.nth(idx).click()
        return
    raise DriverError(f"unknown field kind {kind!r}")


def _use_estimator(page: Any, inp: Any, src: Source) -> None:
    page.click("#est-open")
    page.wait_for_selector("#estdlg[open] #est-total")
    label = "Estimated replacement cost"
    src.note(label)
    displayed = int(digits(page.inner_text("#est-total")))
    page.click("#est-use")
    page.wait_for_selector("#estdlg:not([open])", state="attached")
    current = inp.input_value()
    src.events.append(f"estimator shown {displayed}, field {current}")
    fix = src.estimator(label, displayed, current)
    if fix is not None:
        inp.fill(fix)


def _fill_step(page: Any, src: Source) -> None:
    names = page.locator("#stepform .fld").evaluate_all("els => els.map(e => e.dataset.name)")
    for name in names:
        box = page.locator(f"#stepform .fld[data-name='{name}']")
        if not box.is_visible():
            continue
        _fill_field(page, box, src)


def _save(page: Any) -> None:
    before = page.locator("#app").get_attribute("data-ready")
    page.evaluate("document.querySelector('#app').dataset.errors = ''")
    page.click("#save")
    try:
        _wait_render(page, before)
    except Exception:  # navigation to the sign-in page destroys the context
        page.wait_for_load_state()
    if page.url.split("?")[0].endswith("/login"):
        raise Expired()
    errs = [e for e in page.locator("[data-err]").all_inner_texts() if e.strip()] if page.locator("#stepform").count() else []
    if errs and page.locator("#app").get_attribute("data-ready") == before:
        raise DriverError(f"validation: {errs}")


def _walk(page: Any, base: str, src: Source, start: int) -> None:
    n = start
    while n <= 4:
        _goto_step(page, base, n)
        _fill_step(page, src)
        try:
            _save(page)
        except Expired:
            src.events.append(f"session expired at step {n}")
            _login(page, src)
            if src.restart_after_relogin:
                src.events.append("restart from step 1")
                n = 1
            continue
        n += 1


def _review_mismatches(page: Any, src: Source) -> list[str]:
    exp = getattr(src, "expected", None)
    if exp is None:
        return []
    types = load_answer_key()["portals"][PORTAL]["field_types"]
    rows = page.locator("table.sum tr[data-name]").evaluate_all(
        "rs => rs.map(r => [r.dataset.name, r.querySelector('td').textContent])")
    shown = {k: v for k, v in rows}
    bad = []
    for field, want in exp.items():
        got = shown.get(field)
        if got is None:
            if want not in ("", None):
                bad.append(field)
            continue
        got = "" if got == "—" else got
        if types[field] == "date":
            if got != (us_date(want) if want else ""):
                bad.append(field)
        elif types[field] == "bool":
            if norm(got) != ("yes" if want else "no"):
                bad.append(field)
        elif normalize(got, types[field]) != normalize(want, types[field]):
            bad.append(field)
    return bad


def run(page: Any, base: str, src: Source) -> str:
    from ..corvane import FIELD_STEP

    page.goto(base + "/quote")
    _login(page, src)
    _walk(page, base, src, 1)
    for _ in range(3):
        _goto_step(page, base, 5)
        bad = _review_mismatches(page, src)
        if not bad:
            break
        src.events.append(f"review differs: {bad}")
        for step in sorted({FIELD_STEP[f] for f in bad}):
            _goto_step(page, base, step)
            _fill_step(page, src)
            _save(page)
    else:
        raise DriverError("review still differs after corrections")
    page.click("#submit")
    page.wait_for_url("**/quote/result/**")
    ref = page.inner_text("#quote-ref").strip()
    src.events.append(f"quote {ref}")
    return "submitted"
