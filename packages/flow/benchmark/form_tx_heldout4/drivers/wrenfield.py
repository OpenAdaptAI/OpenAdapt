"""Driver for the Wrenfield Mutual condo wizard."""

from __future__ import annotations

import re
from typing import Any

from .base import DriverError, Source, digits, fmt_date
from .kit import choose, fill_select, fill_text, summary_mismatches, wait_until

REFERENCE_LABELS: dict[str, Any] = {
    "Condominium association": "association",
    "Unit street address": "street",
    "Unit number": "unit_number",
    "City": "city",
    "State": "state",
    "ZIP code": "zip",
    "Year the building was built": "year_built",
    "Floor the unit is on": "floor",
    "How is the unit occupied?": "occupancy",
    "Seasonal occupancy credit": "seasonal_credit",
    "Wood-burning stove or fireplace insert": "wood_stove",
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("dob", fmt_date),
    "Email": "email",
    "Mobile phone": "phone",
    "Unit improvements and betterments (Coverage A)": "improvements",
    "Personal property (Coverage C)": "personal_property",
    "Loss assessment": "loss_assessment",
    "Water and sewer backup": "water_backup",
    "Deductible": "deductible",
    "Policy start date": ("start_date", fmt_date),
}

NAIVE_LABELS: dict[str, Any] = {
    "Condominium association": "association.name",
    "Unit street address": "unit.street",
    "Unit number": "unit.unit_number",
    "City": "unit.city",
    "State": "unit.state",
    "ZIP code": "unit.zip",
    "Year the building was built": "building.year_built",
    "Floor the unit is on": "unit.floor",
    "How is the unit occupied?": "unit.occupancy",
    "Wood-burning stove or fireplace insert": "unit.wood_stove",
    "First name": "owner.first_name",
    "Last name": "owner.last_name",
    "Date of birth": ("owner.date_of_birth", fmt_date),
    "Email": "owner.email",
    "Mobile phone": "owner.phone",
    "Unit improvements and betterments (Coverage A)": "coverage.improvements",
    "Personal property (Coverage C)": "coverage.personal_property",
    "Loss assessment": "coverage.loss_assessment",
    "Water and sewer backup": "coverage.water_backup",
    "Deductible": "coverage.deductible",
    "Policy start date": ("policy.start_date", fmt_date),
}

SUMMARY_FIELDS = {k: (v if isinstance(v, str) else v[0]) for k, v in REFERENCE_LABELS.items()}
STEP_OF = {
    **{f: 1 for f in ("association", "street", "unit_number", "city", "state", "zip", "year_built", "floor",
                      "occupancy", "seasonal_credit", "wood_stove")},
    **{f: 2 for f in ("first_name", "last_name", "dob", "email", "phone")},
    **{f: 3 for f in ("improvements", "personal_property", "loss_assessment", "water_backup", "deductible",
                      "start_date")},
}
IMPR = "Unit improvements and betterments (Coverage A)"


def _section(page: Any, n: int) -> Any:
    return page.locator(f'section[data-step="{n}"]')


def _sign_in(page: Any, src: Source) -> None:
    page.get_by_label("Email", exact=True).fill(src.creds["username"])
    page.get_by_label("Password", exact=True).fill(src.creds["password"])
    page.get_by_role("button", name="Sign in").click()


def _association(page: Any, src: Source) -> None:
    label = "Condominium association"
    src.note(label)
    want = src.get(label)
    if want is None:
        return
    box = page.locator("#association")
    box.fill("")
    box.fill(want)
    opts = page.locator("#assoc-list li[role=option]")
    try:
        opts.first.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise DriverError(f"{label}: no suggestions for {want!r}") from exc
    page.wait_for_timeout(150)
    texts = [t.strip() for t in opts.all_inner_texts()]
    idx = src.pick(label, texts, want)
    opts.nth(idx).click()


def _step1(page: Any, src: Source) -> None:
    s = _section(page, 1)
    _association(page, src)
    for label in ("Unit street address", "Unit number", "City"):
        fill_text(src, s.get_by_label(label, exact=True), label)
    fill_select(src, s.get_by_label("State", exact=True), "State")
    fill_text(src, s.get_by_label("ZIP code", exact=True), "ZIP code")
    for label in ("Year the building was built", "Floor the unit is on"):
        fill_text(src, s.get_by_label(label, exact=True), label)
    fill_select(src, s.get_by_label("How is the unit occupied?", exact=True), "How is the unit occupied?")
    if page.locator("#seasonal-wrap").is_visible():
        fill_select(src, s.get_by_label("Seasonal occupancy credit", exact=True), "Seasonal occupancy credit")
    fill_select(src, s.get_by_label("Wood-burning stove or fireplace insert", exact=True),
                "Wood-burning stove or fireplace insert")


def _step2(page: Any, src: Source) -> None:
    s = _section(page, 2)
    for label in ("First name", "Last name", "Date of birth", "Email", "Mobile phone"):
        fill_text(src, s.get_by_label(label, exact=True), label)


def _estimator(page: Any, src: Source) -> None:
    page.get_by_role("button", name="Estimate it").click()
    dlg = page.locator("#est")
    dlg.wait_for(state="visible")
    src.note("Square footage")
    dlg.get_by_label("Square footage", exact=True).fill(str(src.facts["unit.square_feet"]))
    src.note("Finish level")
    dlg.get_by_label("Finish level", exact=True).select_option(str(src.facts["unit.finish_level"]))
    shown = dlg.locator("#est-out").inner_text()
    m = re.search(r"\$([\d,]+)", shown)
    if not m:
        raise DriverError(f"estimator shows no figure: {shown!r}")
    displayed = int(m.group(1).replace(",", ""))
    dlg.get_by_role("button", name="Use this amount").click()
    dlg.wait_for(state="hidden")
    field = page.get_by_label(IMPR, exact=True)
    src.note(IMPR)
    fix = src.estimator(IMPR, displayed, field.input_value())
    if fix is not None:
        field.fill(fix)
        src.events.append("estimator_corrected")


def _step3(page: Any, src: Source) -> None:
    s = _section(page, 3)
    if "unit.square_feet" in src.facts:
        _estimator(page, src)
    else:
        fill_text(src, s.get_by_label(IMPR, exact=True), IMPR)
    fill_text(src, s.get_by_label("Personal property (Coverage C)", exact=True), "Personal property (Coverage C)")
    fill_select(src, s.get_by_label("Loss assessment", exact=True), "Loss assessment")
    fill_select(src, s.get_by_label("Water and sewer backup", exact=True), "Water and sewer backup")
    radios = s.locator("input[name=deductible]")
    opts = []
    current = ""
    for i in range(radios.count()):
        r = radios.nth(i)
        v = r.get_attribute("value") or ""
        text = r.evaluate("e => e.parentElement.textContent").strip()
        opts.append((v, text))
        if r.is_checked():
            current = v
    pick = choose(src, "Deductible", opts, current)
    if pick is not None:
        s.locator(f'input[name=deductible][value="{pick}"]').check()
    fill_text(src, s.get_by_label("Policy start date", exact=True), "Policy start date")


FILL = {1: _step1, 2: _step2, 3: _step3}


def _save(page: Any, n: int) -> str:
    _section(page, n).locator(f'[data-save="{n}"]').click()

    def state() -> str:
        if page.locator("#relogin").evaluate("d => d.open"):
            return "relogin"
        if _section(page, n + 1).is_visible():
            return "ok"
        errs = [t for t in _section(page, n).locator("[data-err]").all_inner_texts() if t.strip()]
        return "errors:" + "; ".join(errs) if errs else ""

    got = wait_until(state, 10)
    if not got:
        raise DriverError(f"step {n}: no response to Continue")
    return got


def _relogin(page: Any, src: Source) -> None:
    dlg = page.locator("#relogin")
    dlg.get_by_label("Email", exact=True).fill(src.creds["username"])
    dlg.get_by_label("Password", exact=True).fill(src.creds["password"])
    dlg.get_by_role("button", name="Sign in").click()
    dlg.wait_for(state="hidden")
    page.wait_for_timeout(400)  # draft reload
    src.events.append("relogin")


def _do_steps(page: Any, src: Source, first: int) -> None:
    n = first
    restarts = 0
    while n <= 3:
        FILL[n](page, src)
        got = _save(page, n)
        if got == "relogin":
            _relogin(page, src)
            restarts += 1
            if restarts > 2:
                raise DriverError("session keeps ending")
            if src.restart_after_relogin:
                page.locator('[data-go="1"]').click()
                n = 1
            continue
        if got.startswith("errors:"):
            raise DriverError(f"step {n} blocked: {got[7:]}")
        n += 1


def _review_rows(page: Any) -> dict[str, str]:
    page.locator("#review table.summary tr").first.wait_for(state="visible")
    rows = {}
    for tr in page.locator("#review table.summary tr").all():
        rows[tr.locator("th").inner_text().strip()] = tr.locator("td").inner_text().strip()
    return rows


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/condo/quote")
    if "/account/login" in page.url:
        _sign_in(page, src)
    _section(page, 1).wait_for(state="visible")
    page.wait_for_timeout(300)  # draft load
    _do_steps(page, src, 1)
    for _ in range(3):
        rows = _review_rows(page)
        bad = summary_mismatches(src, rows, SUMMARY_FIELDS)
        if not bad:
            break
        src.events.append("summary_fix:" + ",".join(bad))
        first = min(STEP_OF[f] for f in bad)
        page.locator(f'[data-edit="{first}"]').click()
        _do_steps(page, src, first)
    else:
        raise DriverError(f"summary still differs: {bad}")
    page.locator("#confirm").check()
    page.get_by_role("button", name="Get my quote").click()
    try:
        page.wait_for_url(re.compile(r".*/condo/quote/WF-\d+$"), timeout=10000)
    except Exception as exc:
        raise DriverError(f"submit failed: {page.locator('#submit-err').inner_text()!r}") from exc
    page.locator("#quote-ref").wait_for()
    ref = page.locator("#quote-ref").inner_text()
    if not digits(ref):
        raise DriverError("no quote reference shown")
    return "submitted"
