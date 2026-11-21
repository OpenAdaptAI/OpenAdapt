"""Driver for the Thistlebrook Pet Health quote flow."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .base import DriverError, Source, fmt_date, norm
from .kit import choose, fill_select, fill_text, summary_mismatches, wait_until

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def _month(v: Any) -> str:
    return MONTHS[date.fromisoformat(str(v)).month - 1]


def _year(v: Any) -> str:
    return str(date.fromisoformat(str(v)).year)


REFERENCE_LABELS: dict[str, Any] = {
    "First name": "first_name",
    "Last name": "last_name",
    "Email": "email",
    "Mobile number": "phone",
    "ZIP code": "zip",
    "Pet's name": "pet_name",
    "Species": "species",
    "Breed": "breed",
    "Weight (lb)": "weight_lb",
    "Birth month": "birth_month",
    "Birth year": "birth_year",
    "Spayed or neutered": "spayed_neutered",
    "Indoor-only cat": "indoor_only",
    "Illness or injury in the last 12 months": "recent_illness",
    "Describe the condition": "condition_notes",
    "Annual limit": "annual_limit",
    "Reimbursement": "reimbursement",
    "Annual deductible": "deductible",
    "Add routine care (wellness)": "wellness",
    "Policy start date": ("start_date", fmt_date),
}

NAIVE_LABELS: dict[str, Any] = {
    "First name": "owner.first_name",
    "Last name": "owner.last_name",
    "Email": "owner.email",
    "Mobile number": "owner.phone",
    "ZIP code": "owner.zip",
    "Pet's name": "pet.name",
    "Species": "pet.species",
    "Breed": "pet.breed",
    "Weight (lb)": "pet.weight",
    "Birth month": ("pet.birth_date", _month),
    "Birth year": ("pet.birth_date", _year),
    "Spayed or neutered": "pet.spayed_neutered",
    "Illness or injury in the last 12 months": "pet.recent_illness",
    "Describe the condition": "pet.condition_notes",
    "Annual limit": "plan.annual_limit",
    "Reimbursement": "plan.reimbursement",
    "Annual deductible": "plan.deductible",
    "Add routine care (wellness)": "plan.wellness",
    "Policy start date": ("policy.start_date", fmt_date),
}

SUMMARY_FIELDS = {k: (v if isinstance(v, str) else v[0]) for k, v in REFERENCE_LABELS.items()}
SCREEN_OF = {
    "first_name": "owner", "last_name": "owner", "email": "contact", "phone": "contact", "zip": "contact",
    "pet_name": "pet", "species": "pet", "breed": "breed", "weight_lb": "breed", "birth_month": "breed",
    "birth_year": "breed", "spayed_neutered": "health", "indoor_only": "health", "recent_illness": "health",
    "condition_notes": "health", "annual_limit": "plan", "reimbursement": "plan", "deductible": "plan",
    "wellness": "plan", "start_date": "start",
}


def _card(page: Any) -> Any:
    return page.locator("#card")


def _screen(page: Any) -> str:
    try:
        return _card(page).get_attribute("data-screen", timeout=2000) or ""
    except Exception:
        return ""


def _chips(page: Any, src: Source, label: str) -> None:
    group = _card(page).get_by_role("radiogroup", name=label, exact=True)
    buttons = group.get_by_role("radio")
    opts, current = [], ""
    for i in range(buttons.count()):
        b = buttons.nth(i)
        v = b.get_attribute("data-value") or ""
        opts.append((v, b.inner_text().strip()))
        if b.get_attribute("aria-checked") == "true":
            current = v
    pick = choose(src, label, opts, current)
    if pick is not None:
        group.locator(f'[role=radio][data-value="{pick}"]').click()


def _breed(page: Any, src: Source) -> None:
    label = "Breed"
    src.note(label)
    want = src.get(label)
    if want is None:
        return
    box = _card(page).get_by_label(label, exact=True)
    box.fill("")
    box.fill(want)
    opts = page.locator("#breed-list [role=option]")
    try:
        opts.first.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise DriverError(f"Breed: no suggestions for {want!r}") from exc
    page.wait_for_timeout(150)
    texts = [t.strip() for t in opts.all_inner_texts()]
    opts.nth(src.pick(label, texts, want)).click()


def _fill(page: Any, src: Source, screen: str, fix: bool = False) -> None:
    c = _card(page)
    if screen == "owner":
        for label in ("First name", "Last name"):
            fill_text(src, c.get_by_label(label, exact=True), label)
    elif screen == "contact":
        for label in ("Email", "Mobile number", "ZIP code"):
            fill_text(src, c.get_by_label(label, exact=True), label)
    elif screen == "pet":
        fill_text(src, c.get_by_label("Pet's name", exact=True), "Pet's name")
        _chips(page, src, "Species")
    elif screen == "breed":
        _breed(page, src)
        fill_text(src, c.get_by_label("Weight (lb)", exact=True), "Weight (lb)")
        fill_select(src, c.get_by_label("Birth month", exact=True), "Birth month")
        fill_select(src, c.get_by_label("Birth year", exact=True), "Birth year")
    elif screen == "health":
        _chips(page, src, "Spayed or neutered")
        if c.get_by_role("radiogroup", name="Indoor-only cat", exact=True).count():
            _chips(page, src, "Indoor-only cat")
        _chips(page, src, "Illness or injury in the last 12 months")
        notes = c.get_by_label("Describe the condition", exact=True)
        if notes.is_visible():
            fill_text(src, notes, "Describe the condition")
    elif screen == "plan":
        order = ["Annual limit", "Annual deductible", "Reimbursement"] if fix else \
            ["Annual limit", "Reimbursement", "Annual deductible"]
        for label in order:
            _chips(page, src, label)
        label = "Add routine care (wellness)"
        src.note(label)
        want = src.get(label)
        if want is not None:
            c.get_by_label(label, exact=True).set_checked(norm(want) in ("yes", "true", "1"))
    elif screen == "start":
        fill_text(src, c.get_by_label("Policy start date", exact=True), "Policy start date")


def _next(page: Any, screen: str) -> str:
    _card(page).locator("#next").click()

    def state() -> str:
        now = _screen(page)
        if now and now != screen:
            return now
        try:
            errs = [t for t in _card(page).locator("[data-err]").all_inner_texts() if t.strip()]
        except Exception:
            errs = []
        return "errors:" + "; ".join(errs) if errs else ""

    got = wait_until(state, 10)
    if not got:
        raise DriverError(f"screen {screen}: no response to Next")
    if got.startswith("errors:"):
        raise DriverError(f"screen {screen} blocked: {got[7:]}")
    return got


def _summary_rows(page: Any) -> dict[str, str]:
    rows_loc = page.locator("#sum .row")
    rows_loc.first.wait_for(state="visible")
    rows = {}
    for r in rows_loc.all():
        rows[r.locator("dt").inner_text().strip()] = r.locator("dd").inner_text().strip()
    return rows


def _sign_in_again(page: Any, src: Source) -> None:
    c = _card(page)
    c.get_by_label("Email", exact=True).fill(src.creds["username"])
    c.get_by_label("Password", exact=True).fill(src.creds["password"])
    c.get_by_role("button", name="Sign in").click()
    if wait_until(lambda: _screen(page) == "summary", 10) is not True:
        raise DriverError("sign-in screen did not return to the summary")
    src.events.append("relogin")


def _after_quote(page: Any) -> str:
    if "/quote/done/" in page.url:
        return "done"
    try:
        if _screen(page) == "signin":
            return "signin"
        e = page.locator("#quote-err")
        if e.count() and e.inner_text(timeout=500).strip():
            return "error"
    except Exception:
        return ""
    return ""


def run(page: Any, base: str, src: Source) -> str:
    page.goto(base + "/quote")
    if "/signin" in page.url:
        page.get_by_label("Email", exact=True).fill(src.creds["username"])
        page.get_by_label("Password", exact=True).fill(src.creds["password"])
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(re.compile(r".*/quote$"))
    if not wait_until(lambda: _screen(page) == "owner", 10):
        raise DriverError("quote flow did not start")
    screen = "owner"
    while screen != "summary":
        _fill(page, src, screen)
        screen = _next(page, screen)
        if screen == "signin":
            raise DriverError("signed out before the summary")
    relogins = 0
    for _ in range(6):
        bad = summary_mismatches(src, _summary_rows(page), SUMMARY_FIELDS)
        if bad:
            src.events.append("summary_fix:" + ",".join(bad))
            target = SCREEN_OF[bad[0]]
            _card(page).locator(f'[data-change="{target}"]').click()
            if not wait_until(lambda: _screen(page) == target, 5):
                raise DriverError(f"Change did not open {target}")
            _fill(page, src, target, fix=True)
            _next(page, target)
            continue
        _card(page).get_by_role("button", name="Get my quote").click()
        got = wait_until(lambda: _after_quote(page), 10)
        if got == "done":
            break
        if got == "signin":
            relogins += 1
            if relogins > 2:
                raise DriverError("session keeps ending")
            _sign_in_again(page, src)
            continue
        raise DriverError(f"quote failed: {page.locator('#quote-err').inner_text(timeout=1000) if got else 'no response'}")
    else:
        raise DriverError("summary still differs from the answer key")
    page.locator("#quote-ref").wait_for()
    if not page.locator("#quote-ref").inner_text().strip():
        raise DriverError("no quote reference shown")
    return "submitted"
