"""Pellbrook conversation driver: answer each card, check the summary, get the quote."""

from __future__ import annotations

from typing import Any

from ..case_defs import PB_LAYUP_LABEL
from ..scoring import load_answer_key, normalize
from .base import DriverError, Source, digits, find_option, fmt_date, norm, us_date

REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("date_of_birth", fmt_date),
    "Email address": "email",
    "ZIP code where the boat is kept": "garaging_zip",
    "Model year": "boat_year",
    "Manufacturer": "boat_make",
    "Model": "boat_model",
    "Length overall (feet)": "length_ft",
    "Engine type": "engine_type",
    "Total horsepower (all engines combined)": "total_horsepower",
    "Number of engines": "engine_count",
    # Typed directly unless the intake asks for book value.
    "Agreed hull value": lambda s: None if "agreed_hull_value" not in s.facts else str(s.expected["agreed_hull_value"]),
    "Book value (average retail)": "agreed_hull_value",
    "Where is the boat kept during the boating season?": "storage",
    PB_LAYUP_LABEL: "layup_period",
    "Where will you use the boat?": "navigation_area",
    "Has the primary operator completed a boating safety course?": "safety_course",
    "Boat insurance claims in the last 3 years": "losses_3yr",
    "Lienholder (lender with a loan on this boat)": "lienholder",
    "Liability limit": "liability_limit",
    "Hull deductible": "deductible",
    "Coverage start date": ("effective_date", fmt_date),
}

NAIVE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Date of birth": ("date_of_birth", fmt_date),
    "Email address": "email",
    "ZIP code where the boat is kept": "garaging_zip",
    "Model year": "boat_year",
    "Manufacturer": "boat_make",
    "Model": "boat_model",
    "Length overall (feet)": "length_ft",
    "Engine type": "engine_type",
    "Total horsepower (all engines combined)": "total_horsepower",
    "Number of engines": "engine_count",
    "Agreed hull value": "agreed_hull_value",
    "Where is the boat kept during the boating season?": "storage",
    PB_LAYUP_LABEL: "layup_period",
    "Where will you use the boat?": "navigation_area",
    "Has the primary operator completed a boating safety course?": "safety_course",
    "Boat insurance claims in the last 3 years": "losses_3yr",
    "Lienholder (lender with a loan on this boat)": "lienholder",
    "Liability limit": "liability_limit",
    "Hull deductible": "deductible",
    "Coverage start date": ("effective_date", fmt_date),
}

PORTAL = "pellbrook"
MAX_CARDS = 60


def _login(page: Any, src: Source) -> None:
    page.wait_for_selector("#em")
    page.get_by_label("Email").fill(src.creds["username"])
    page.get_by_label("Password").fill(src.creds["password"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/boat/quote")


def _seq(page: Any) -> str:
    return page.locator("#convo").get_attribute("data-seq") or "0"


def _wait_new(page: Any, before: str) -> None:
    page.wait_for_function(
        "(b) => { const c = document.querySelector('#convo'); return c && (c.dataset.seq !== b || c.dataset.errors); }",
        arg=before,
    )


def _use_book_value(page: Any, src: Source) -> None:
    page.click("#bv-open")
    page.wait_for_selector("#bv-average")
    label = "Book value (average retail)"
    src.note("Average retail")
    displayed = int(digits(page.inner_text("#bv-average")))
    page.click("#bv-use")
    current = page.input_value("#answer")
    src.events.append(f"book value shown {displayed}, field {current}")
    fix = src.estimator(label, displayed, current)
    if fix is not None:
        page.fill("#answer", fix)


def _answer_card(page: Any, src: Source) -> None:
    card = page.locator("#card")
    kind = card.get_attribute("data-kind")
    label = page.locator("#card .q").inner_text().strip()
    src.note(label)
    want = src.get(label)
    if kind == "choice":
        radios = card.locator("input[type=radio]").all()
        opts = [(r.get_attribute("value") or "", (r.locator("xpath=following-sibling::span").inner_text()).strip())
                for r in radios]
        if want is not None:
            val = find_option(src, want, opts)
            if val is None:
                raise DriverError(f"{label}: no option for {want!r}")
        elif any(r.is_checked() for r in radios):
            val = None
        elif src.fallback_first:
            val = opts[0][0]
        else:
            raise DriverError(f"{label}: no answer")
        if val is not None:
            card.locator(f"input[type=radio][value='{val}']").check(force=True)
    else:
        if want is None and card.locator("#bv-open").count():
            _use_book_value(page, src)
        elif want is not None:
            page.fill("#answer", want)
    before = _seq(page)
    page.evaluate("document.querySelector('#convo').dataset.errors = ''")
    page.click("#next")
    _wait_new(page, before)
    if _seq(page) == before:
        raise DriverError(f"{label}: {page.inner_text('#err')}")


def _summary_mismatches(page: Any, src: Source) -> list[str]:
    exp = getattr(src, "expected", None)
    if exp is None:
        return []
    types = load_answer_key()["portals"][PORTAL]["field_types"]
    rows = page.locator("#summary tr[data-key]").evaluate_all(
        "rs => rs.map(r => [r.dataset.key, r.querySelector('td').textContent])")
    shown = {k: v for k, v in rows}
    bad = []
    for field, want in exp.items():
        got = shown.get(field)
        if got is None:
            if want not in ("", None):
                bad.append(field)
            continue
        got = "" if got == "None" else got
        t = types[field]
        if t == "date":
            ok = got == (us_date(want) if want else "")
        elif t == "bool":
            ok = norm(got) == ("yes" if want else "no")
        else:
            ok = normalize(got, t) == normalize(want, t)
        if not ok:
            bad.append(field)
    return bad


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/boat/quote")
    _login(page, src)
    page.wait_for_selector("#convo[data-view]")
    for _ in range(MAX_CARDS):
        if page.locator("#convo").get_attribute("data-view") == "summary":
            bad = _summary_mismatches(page, src)
            if not bad:
                break
            src.events.append(f"summary differs: {bad}")
            before = _seq(page)
            page.click(f"[data-change='{bad[0]}']")
            _wait_new(page, before)
            continue
        _answer_card(page, src)
    else:
        raise DriverError("conversation did not reach the summary")
    page.click("#get-quote")
    page.wait_for_url("**/boat/quote/**")
    ref = page.inner_text("#quote-ref").strip()
    src.events.append(f"quote {ref}")
    return "submitted"
