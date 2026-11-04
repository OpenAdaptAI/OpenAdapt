"""Dolibarr 21: create a third party through the UI, read it back in the list.

The acting session signs in as ``DOLI_ACT_USER`` and creates a customer on
``societe/card.php``; the save leaves the card page. The read-back opens the
third-party list filtered by the name (``societe/list.php?search_nom=``): a
search view, so a second copy of the record is counted. Name and alias are
compared; the list reformats phone numbers, so they aren't read.
"""

from __future__ import annotations

import time
from typing import Any

from benchmark.ui_second_read.common import (
    base,
    check,
    env,
    final_section,
    ledger_state,
    settle,
    token,
    written,
)
from openadapt_flow.runtime.effects.ui_readback import UiReadbackSpec

APP = "http://100.111.126.101:9635"


def spec(**extra: Any) -> UiReadbackSpec:
    b = base(APP)
    data = {
        "record_url": b + "/societe/list.php?search_nom={name}",
        "url_params": {"name": {"param": "name"}},
        "sign_in": {
            "url": b + "/index.php",
            "username_selector": "#username",
            "password_selector": "#password",
            "submit_selector": "input[type=submit]",
            "signed_in_selector": "#topmenu-login-dropdown",
            "username_env": "DOLI_READ_USER",
            "password_env": "DOLI_READ_PASS",
        },
        "ready_selector": "table.liste",
        "row_selector": "tr.oddeven:has(td[data-key=ref])",
        "fields": {
            "name": {"selector": "td[data-key=ref] a"},
            "alias": {"selector": "td:nth-child(3)"},
        },
        "identity": {
            "reader": {"selector": "input[name=search_nom]", "attribute": "value"},
            "value": {"param": "name"},
        },
        "navigation_timeout_s": 30.0,
        **extra,
    }
    return UiReadbackSpec.model_validate(data)


class App:
    def __init__(self, out: Any) -> None:
        self.out = out

    def __enter__(self) -> "App":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)
        self.page = self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.browser.close()
        self._pw.stop()

    def login(self) -> Any:
        page = self.browser.new_context().new_page()
        page.set_default_timeout(60_000)
        for _ in range(3):  # a chaos 502 on sign-in is a harness event
            try:
                page.goto(base(APP) + "/index.php")
                page.fill("#username", env("DOLI_ACT_USER"))
                page.fill("#password", env("DOLI_ACT_PASS"))
                page.click("input[type=submit]")
                page.wait_for_selector("#topmenu-login-dropdown")
                return page
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise RuntimeError("acting sign-in failed")

    def create(self, name: str, alias: str, *, save: bool = True, page: Any = None) -> None:
        page = page or self.page
        page.goto(base(APP) + "/societe/card.php?action=create&type=c")
        page.fill("#name", name)
        page.fill("#name_alias_input", alias)
        if save:
            page.click("input[name=save]")
            page.wait_for_load_state()
            if "socid=" not in page.url:
                raise RuntimeError("the acting save did not reach the card page")

    def set_alias(self, socid_page_url: str, alias: str, page: Any) -> None:
        page.goto(socid_page_url.replace("card.php?", "card.php?action=edit&"))
        page.fill("#name_alias_input", alias)
        page.click("input[name=save]")
        page.wait_for_load_state()


def _row(read: Any, **extra: Any) -> dict[str, Any]:
    return {
        "verdict": read.verdict,
        "reason": read.reason,
        "read_seconds": read.seconds,
        "independence": (read.evidence.get("independence") or {}),
        **extra,
    }


def correct(app: App) -> dict[str, Any]:
    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"
    s = spec()
    r = check(s, {"name": name}, written({"name": name, "alias": alias}),
              lambda: app.create(name, alias), app.page)
    return _row(r)


def wrong_value(app: App) -> dict[str, Any]:
    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"
    r = check(spec(), {"name": name}, written({"name": name, "alias": alias}),
              lambda: app.create(name, alias + " X"), app.page)
    return _row(r)


def duplicate(app: App) -> dict[str, Any]:
    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"

    def twice() -> None:
        app.create(name, alias)
        app.create(name, alias)

    r = check(spec(), {"name": name}, written({"name": name, "alias": alias}),
              twice, app.page)
    return _row(r)


def missing_save(app: App) -> dict[str, Any]:
    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"
    r = check(spec(), {"name": name}, written({"name": name, "alias": alias}),
              lambda: app.create(name, alias, save=False), app.page)
    return _row(r)


def same_view(app: App) -> dict[str, Any]:
    """The save is followed by the list view on the acting screen; the read
    opens that same page."""

    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"

    def act() -> None:
        app.create(name, alias)
        app.page.goto(base(APP) + f"/societe/list.php?search_nom={name}")

    r = check(spec(), {"name": name}, written({"name": name, "alias": alias}),
              act, app.page)
    return _row(r)


def _provisional(app: App, late_change: bool) -> dict[str, Any]:
    name, alias = f"UI2R Corp {token()}", f"Alias {token()}"
    s = spec(final=final_section())
    r = check(s, {"name": name}, written({"name": name, "alias": alias}),
              lambda: app.create(name, alias), app.page)
    card = app.page.url
    if late_change:
        other = app.login()  # a second session changes the record later
        app.set_alias(card, alias + " LATE", other)
        other.context.close()
    settled, secs = settle(s)
    final = ledger_state(s, r.deferred) if r.deferred else None
    return _row(r, final=final, settle_seconds=secs,
                settled=[(x.state, x.code) for x in settled])


def provisional_then_final(app: App) -> dict[str, Any]:
    return _provisional(app, late_change=False)


def late_change(app: App) -> dict[str, Any]:
    return _provisional(app, late_change=True)


def batch_settle(app: App) -> dict[str, Any]:
    """Five provisional saves, then one settle session for all five."""

    s = spec(final=final_section())
    reads = []
    for _ in range(5):
        name, alias = f"UI2R Corp {token()}", f"Alias {token()}"
        reads.append(check(s, {"name": name}, written({"name": name, "alias": alias}),
                           lambda: app.create(name, alias), app.page))
    settled, secs = settle(s)
    return {
        "verdict": ",".join(r.verdict for r in reads),
        "per_item_read_seconds": [r.seconds for r in reads],
        "settle_seconds": secs,
        "settled": [(x.state, x.code) for x in settled],
    }


CASES = [
    ("C1", "correct", "confirmed", correct),
    ("C2", "correct", "confirmed", correct),
    ("C3", "correct", "confirmed", correct),
    ("C4", "correct", "confirmed", correct),
    ("C5", "correct", "confirmed", correct),
    ("F1", "wrong value", "refuted", wrong_value),
    ("F2", "duplicate", "refuted", duplicate),
    ("F4", "missing save", "not confirmed", missing_save),
    ("F5c", "provisional, unchanged", "provisional then verified", provisional_then_final),
    ("F5", "late change", "provisional then reconciliation_required", late_change),
    ("F7", "same view", "not confirmed", same_view),
    ("B5", "batch of 5 provisional", "5 provisional, 5 verified in one settle", batch_settle),
]
