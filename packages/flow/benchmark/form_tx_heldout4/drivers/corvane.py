"""Driver for the Corvane Casualty motorcycle rating worksheet."""

from __future__ import annotations

import re
from typing import Any

from .base import DriverError, Source, find_option, fmt_date, norm
from .kit import fill_select, fill_text, summary_mismatches, wait_until

REFERENCE_LABELS: dict[str, Any] = {
    "Effective date": ("effective_date", fmt_date),
    "Prior motorcycle insurance": "prior_insurance",
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("dob", fmt_date),
    "Phone": "phone",
    "Years with motorcycle endorsement": "years_endorsed",
    "Completed a rider safety course in the last 3 years": "safety_course",
    "Street address": "street",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "Annual mileage": "annual_miles",
    "Liability limits (BI/PD)": "bi_pd_limits",
    "Collision deductible": "collision_deductible",
    "Comprehensive deductible": "comprehensive_deductible",
    "Custom parts and equipment": "cpe_coverage",
    "Roadside coverage": "roadside",
}

NAIVE_LABELS: dict[str, Any] = {
    "Effective date": ("policy.effective_date", fmt_date),
    "Prior motorcycle insurance": "rider.prior_insurance",
    "First name": "rider.first_name",
    "Last name": "rider.last_name",
    "Date of birth": ("rider.date_of_birth", fmt_date),
    "Phone": "rider.phone",
    "Years with motorcycle endorsement": "rider.years_endorsed",
    "Completed a rider safety course in the last 3 years": "rider.safety_course",
    "Street address": "garaging.street",
    "City": "garaging.city",
    "State": "garaging.state",
    "ZIP": "garaging.zip",
    "Annual mileage": "usage.mileage",
    "Liability limits (BI/PD)": "coverage.liability_limits",
    "Collision deductible": "coverage.collision_deductible",
    "Comprehensive deductible": "coverage.comprehensive_deductible",
    "Custom parts and equipment": "coverage.cpe_amount",
    "Roadside coverage": "coverage.roadside",
}

SUMMARY_FIELDS = {k: (v if isinstance(v, str) else v[0]) for k, v in REFERENCE_LABELS.items()}
ORDER = ["basics", "rider", "garaging", "motorcycles", "coverage"]
CPE = "Custom parts and equipment"


def _sign_in(page: Any, src: Source) -> None:
    page.get_by_label("Agent code", exact=True).fill(src.creds["username"])
    page.get_by_label("Password", exact=True).fill(src.creds["password"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(r".*/rater/"))


def _panel(page: Any, key: str) -> Any:
    p = page.locator(f'section.panel[data-panel="{key}"]')
    hdr = p.locator("button.acc")
    if hdr.get_attribute("aria-expanded") != "true":
        hdr.click()
    p.locator(".body").wait_for(state="visible")
    return p


def _texts(src: Source, scope: Any, labels: list[str]) -> None:
    for label in labels:
        fill_text(src, scope.get_by_label(label, exact=True), label)


def _bikes(src: Source) -> list[dict[str, Any]]:
    if src.kind == "reference":
        return list(getattr(src, "expected")["motorcycles"])
    return list(src.facts.get("motorcycles", []))


def _add_bike(page: Any, src: Source, bike: dict[str, Any]) -> None:
    page.get_by_role("button", name="Add motorcycle").click()
    dlg = page.locator("#bike")
    dlg.wait_for(state="visible")
    src.note("VIN")
    dlg.get_by_label("VIN", exact=True).fill(str(bike["vin"]))
    with page.expect_popup() as info:
        dlg.get_by_role("button", name="Decode VIN").click()
    pop = info.value
    pop.wait_for_load_state()
    models = [t.strip() for t in pop.locator("td.model").all_inner_texts()]
    if not models:
        raise DriverError(f"VIN {bike['vin']} did not decode")
    src.note("Model")
    idx = src.pick("Model", models, str(bike.get("model", "")))
    try:
        pop.locator("[data-pick]").nth(idx).click(timeout=5000)
    except Exception:
        if not pop.is_closed():  # the popup closes itself on click; that is fine
            raise
    if not wait_until(lambda: dlg.get_by_label("Model", exact=True).input_value().strip(), 5):
        raise DriverError("decoded vehicle did not reach the dialog")
    src.note("Ownership")
    own = dlg.get_by_label("Ownership", exact=True)
    opts = [(v, t) for v, t in own.evaluate("s => [...s.options].map(o => [o.value || o.text, o.text])") if v != "-- Select --"]
    want = bike.get("ownership")
    value = find_option(src, str(want), opts) if want else (opts[0][0] if src.fallback_first else None)
    if value is None:
        raise DriverError(f"Ownership: no option for {want!r}")
    own.select_option(value)
    lien = dlg.get_by_label("Lienholder name", exact=True)
    if lien.is_visible():
        src.note("Lienholder name")
        if bike.get("lienholder"):
            lien.fill(str(bike["lienholder"]))
    dlg.get_by_role("button", name="Add to quote").click()
    page.wait_for_timeout(150)
    if dlg.evaluate("d => d.open"):
        raise DriverError(f"motorcycle not added: {page.locator('#bike-err').inner_text()}")


def _cpe_worksheet(page: Any, src: Source) -> None:
    page.get_by_role("button", name="CPE worksheet").click()
    dlg = page.locator("#cpe")
    dlg.wait_for(state="visible")
    items = src.facts["coverage.cpe_items"]
    for n, item in enumerate(items, 1):
        dlg.get_by_role("button", name="Add item").click()
        dlg.get_by_label(f"Item {n}", exact=True).fill(str(item["item"]))
        dlg.get_by_label(f"Value {n}", exact=True).fill(str(item["value"]))
    shown = dlg.locator("#cpe-total").inner_text()
    m = re.search(r"\$([\d,]+)", shown)
    if not m:
        raise DriverError(f"CPE worksheet shows no total: {shown!r}")
    displayed = int(m.group(1).replace(",", ""))
    dlg.get_by_role("button", name="Apply total").click()
    dlg.wait_for(state="hidden")
    field = page.get_by_label(CPE, exact=True)
    src.note(CPE)
    fix = src.estimator(CPE, displayed, field.input_value())
    if fix is not None:
        field.fill(fix)
        src.events.append("estimator_corrected")


def _fill(page: Any, src: Source, key: str) -> None:
    p = _panel(page, key)
    if key == "basics":
        fill_text(src, p.get_by_label("Effective date", exact=True), "Effective date")
        fill_select(src, p.get_by_label("Prior motorcycle insurance", exact=True), "Prior motorcycle insurance")
    elif key == "rider":
        _texts(src, p, ["First name", "Last name", "Date of birth", "Phone", "Years with motorcycle endorsement"])
        label = "Completed a rider safety course in the last 3 years"
        fill_select(src, p.get_by_label(label, exact=True), label)
    elif key == "garaging":
        _texts(src, p, ["Street address", "City"])
        fill_select(src, p.get_by_label("State", exact=True), "State")
        fill_text(src, p.get_by_label("ZIP", exact=True), "ZIP")
    elif key == "motorcycles":
        grid = page.locator("#bike-grid tbody tr")
        while grid.count():
            grid.first.get_by_role("button", name="Remove").click()
        for bike in _bikes(src):
            _add_bike(page, src, bike)
        fill_text(src, p.get_by_label("Annual mileage", exact=True), "Annual mileage")
    else:
        for label in ("Liability limits (BI/PD)", "Collision deductible", "Comprehensive deductible"):
            fill_select(src, p.get_by_label(label, exact=True), label)
        if "coverage.cpe_items" in src.facts:
            _cpe_worksheet(page, src)
        else:
            fill_text(src, p.get_by_label(CPE, exact=True), CPE)
        fill_select(src, p.get_by_label("Roadside coverage", exact=True), "Roadside coverage")


def _save(page: Any, key: str) -> str:
    p = page.locator(f'section.panel[data-panel="{key}"]')
    p.locator(f'[data-save="{key}"]').click()

    def state() -> str:
        if "/agent/login" in page.url:
            return "relogin"
        try:
            if not p.count():
                return ""
            if page.locator("#banner .warn").count() and "timed out" in page.locator("#banner").inner_text(timeout=1000):
                return ""
            if p.locator("[data-saved]").inner_text(timeout=1000).strip():
                return "ok"
            errs = [t for t in p.locator("[data-err], [data-panel-err]").all_inner_texts() if t.strip()]
        except Exception:
            return ""
        return "errors:" + "; ".join(errs) if errs else ""

    got = wait_until(state, 10)
    if not got:
        raise DriverError(f"panel {key}: no response to Save panel")
    return got


def _panels(page: Any, src: Source) -> None:
    i = 0
    relogins = 0
    while i < len(ORDER):
        key = ORDER[i]
        _fill(page, src, key)
        got = _save(page, key)
        if got == "relogin":
            relogins += 1
            if relogins > 2:
                raise DriverError("session keeps ending")
            page.wait_for_load_state()
            _sign_in(page, src)
            page.locator("section.panel").first.wait_for()
            src.events.append("relogin")
            i = 0 if src.restart_after_relogin else i + 1
            continue
        if got.startswith("errors:"):
            raise DriverError(f"panel {key} blocked: {got[7:]}")
        i += 1


def _rate(page: Any) -> None:
    page.get_by_role("button", name="Rate this quote").click()
    page.wait_for_url(re.compile(r".*/rater/(rated|worksheet\?msg=.*)$"))
    if "/rater/rated" not in page.url:
        raise DriverError(f"rating blocked: {page.locator('#banner').inner_text()}")


def _rated_rows(page: Any) -> dict[str, str]:
    rows = {}
    for tr in page.locator("#rated tr").all():
        tds = tr.locator("td")
        if tds.count() == 2:
            rows[tds.nth(0).inner_text().strip()] = tds.nth(1).inner_text().strip()
    return rows


def _bikes_differ(page: Any, src: Source) -> bool:
    if src.kind != "reference":
        return False
    shown = sorted(norm(t) for t in page.locator("#rated-bikes tbody tr td:nth-child(4)").all_inner_texts())
    want = sorted(norm(b["model"]) for b in getattr(src, "expected")["motorcycles"])
    return shown != want


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/rater/worksheet")
    if "/agent/login" in page.url:
        _sign_in(page, src)
    page.locator("section.panel").first.wait_for()
    _panels(page, src)
    _rate(page)
    for _ in range(3):
        bad = summary_mismatches(src, _rated_rows(page), SUMMARY_FIELDS)
        if not bad and not _bikes_differ(page, src):
            break
        src.events.append("summary_fix:" + ",".join(bad or ["motorcycles"]))
        if bad == ["city"] and page.locator("#std-notice").count():
            page.get_by_role("button", name="Use address as entered").click()
            page.wait_for_url(re.compile(r".*/rater/rated$"))
            continue
        page.get_by_role("link", name="Back to worksheet").click()
        page.locator("section.panel").first.wait_for()
        _panels(page, src)
        _rate(page)
    else:
        raise DriverError("rated quote still differs from the answer key")
    page.get_by_role("button", name="Submit application").click()
    page.wait_for_url(re.compile(r".*/rater/quote/CVQ-\d+$"))
    if not page.locator("#quote-ref").inner_text().strip():
        raise DriverError("no quote reference shown")
    return "submitted"
