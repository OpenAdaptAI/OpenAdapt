"""The OpenEMR plumbing every non-OpenAdapt arm shares.

All arms get the same start for every item: a browser signed in as the clerk,
looking at a blank New Patient form opened through OpenEMR's own tab shell
(the form needs ``top.restoreSession`` from that shell to save). When the
session has ended (the soak's ``session_expiry`` event), the same runner code
signs in again before handing the page over. This is the runner's job in a
deployment of any of these arms, so it is the same for all of them and is not
charged to the arm's model budget. It is recorded per item (``signed_in_again``).

The REST read is the clerk's own API access (the fixture's ``actor`` OAuth
client), the same access OpenAdapt's record check uses. No arm ever sees the
harness's oracle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

FORM_PATH = "/interface/new/new_comprehensive.php"
FORM_FRAME_NAME = "openadapt-new-patient"
VIEWPORT = {"width": 1280, "height": 800}

#: The fields a referral sets, with the New Patient form's control ids.
FORM_IDS = {
    "title": "form_title",
    "fname": "form_fname",
    "lname": "form_lname",
    "DOB": "form_DOB",
    "sex": "form_sex",
    "street": "form_street",
    "city": "form_city",
    "state_label": "form_state",
    "postal_code": "form_postal_code",
    "phone_home": "form_phone_home",
    "email": "form_email",
    "country_code": "form_country_code",
}

SOP = """\
Register one referred patient in OpenEMR from the referral below.
Rules (the clinic's standard procedure):
- Create exactly one new patient chart with the referral's values. Dates of \
birth go in as YYYY-MM-DD; names as the person's name (trim stray spaces, \
normal capitalisation).
- If OpenEMR shows that a matching patient may already exist, or you otherwise \
find this person already has a chart (same name and date of birth, or the same \
name and a date of birth that differs only by an obvious typo with the same \
address), do not create another chart: stop and say so.
- If the referral has no date of birth, do not create a chart: stop and say \
the referral goes back to the sender. A missing home phone is fine.
- If the save fails, you may try once more; never leave two charts.
- Work only in the OpenEMR page. All data is synthetic test data.
End your final reply with exactly one line, either
RESULT: SAVED
or
RESULT: NOT SAVED: <one-sentence reason>"""


def claimed_from(text: str) -> tuple[str, str]:
    """(``done`` | ``stopped``, last line) from an agent's final reply."""

    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    for ln in reversed(lines):
        if ln.upper().startswith("RESULT:"):
            last = ln
            break
    up = last.upper()
    if up.startswith("RESULT: SAVED"):
        return "done", last
    return "stopped", last or "(no final reply)"


@dataclass
class OpenEMRSession:
    """One Playwright browser, signed in, reused across items."""

    env: Any
    headless: bool = True
    _pw: Any = None
    _browser: Any = None
    context: Any = None
    page: Any = None
    signins: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ui_url(self) -> str:
        return self.env.ui_url.rstrip("/")

    def start(self) -> None:
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        hook = getattr(self.env, "extra", {}).get("context_hook")
        if hook is not None:  # the offline mock serves the app through routes
            hook(self.context)
        self.page = self.context.new_page()

    def close(self) -> None:
        for obj, how in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, how)()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self._pw = self._browser = self.context = self.page = None

    def _account(self) -> tuple[str, str]:
        if self.env.accounts:
            return self.env.accounts[0]
        fx = self.env.extra["fixture"]
        v = fx._runtime_values()
        return v["OPENEMR_ACTOR_USER"], v["OPENEMR_ACTOR_PASSWORD"]

    def sign_in(self) -> None:
        page = self.page
        user, password = self._account()
        page.goto(self.ui_url + "/interface/login/login.php?site=default")
        page.locator("#authUser").fill(user)
        page.locator("#clearPass").fill(password)
        page.locator("#login-button").click()
        page.wait_for_url("**/interface/main/tabs/main.php**", timeout=90_000)
        self.signins += 1
        self._dismiss_registration()

    def _shell_ready(self) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    "() => typeof window.navigateTab === 'function' "
                    "&& typeof window.activateTabByName === 'function'"
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _dismiss_registration(self) -> None:
        reg = self.page.locator(".product-registration-modal")
        try:
            if reg.count() and reg.is_visible():
                tel = reg.locator("#allowTelemetry")
                if tel.count() and tel.is_checked():
                    tel.uncheck()
                reg.get_by_role("button", name="Ask again later", exact=True).click()
                reg.wait_for(state="hidden", timeout=30_000)
        except Exception:  # noqa: BLE001 - the dialog only shows on a fresh profile
            pass

    def form_frame(self, timeout_s: float = 60.0) -> Any:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self.page.frame(name=FORM_FRAME_NAME)
            if frame is None:
                try:
                    h = self.page.locator(f'iframe[name="{FORM_FRAME_NAME}"]').element_handle()
                    frame = None if h is None else h.content_frame()
                except Exception:  # noqa: BLE001
                    frame = None
            if frame is not None and FORM_PATH in (frame.url or ""):
                return frame
            self.page.wait_for_timeout(150)
        raise RuntimeError("the New Patient form frame did not open")

    def blank_form(self) -> dict[str, Any]:
        """Leave the page on a blank New Patient form; sign in again if needed."""

        self.start()
        t0 = time.time()
        again = False
        if not self._shell_ready() or "login" in (self.page.url or ""):
            self.sign_in()
            again = self.signins > 1
        # Close any earlier form tab, then open a fresh one.
        try:
            self.page.evaluate(
                "({path, name}) => { window.navigateTab(path, name); "
                "window.activateTabByName(name, true); }",
                {"path": FORM_PATH + "?_t=" + str(int(time.time() * 1000)), "name": FORM_FRAME_NAME},
            )
            frame = self.form_frame()
            frame.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        except Exception:
            # The shell may have been logged out under us (expired session).
            self.sign_in()
            again = True
            self.page.evaluate(
                "({path, name}) => { window.navigateTab(path, name); "
                "window.activateTabByName(name, true); }",
                {"path": FORM_PATH, "name": FORM_FRAME_NAME},
            )
            frame = self.form_frame()
            frame.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        self._dismiss_registration()
        return {"setup_s": round(time.time() - t0, 2), "signed_in_again": again}


class RestReader:
    """Read-only use of OpenEMR's patient API, as the clerk's integration."""

    def __init__(self, env: Any) -> None:
        self.env = env
        self._session: Any = None
        self._issued = 0.0

    def _s(self) -> Any:
        # Tokens are short-lived; take a fresh one every 5 minutes.
        if self._session is None or time.time() - self._issued > 300:
            getter = self.env.extra.get("rest_session")
            if getter is not None:
                self._session = getter()
            else:
                self._session = self.env.extra["fixture"].token_session("actor")
            self._issued = time.time()
        return self._session

    @property
    def base(self) -> str:
        api = self.env.api_url or self.env.extra["fixture"].api_base_url
        return api.rstrip("/") + "/apis/default/api"

    def find(self, **query: str) -> list[dict[str, Any]]:
        q = {k: v for k, v in query.items() if v}
        r = self._s().get(self.base + "/patient", params=q)
        if r.status_code != 200:
            return [{"error": f"HTTP {r.status_code}"}]
        data = r.json()
        rows = data.get("data") if isinstance(data, dict) else data
        out = []
        for p in rows or []:
            out.append(
                {
                    k: p.get(k)
                    for k in (
                        "uuid", "pid", "title", "fname", "lname", "DOB", "sex",
                        "street", "city", "state", "postal_code", "phone_home", "email",
                    )
                }
            )
        return out


def fill_value(frame: Any, field_id: str, value: str) -> None:
    """Set one New Patient control the way a person would."""

    loc = frame.locator(f"#{field_id}")
    tag = loc.evaluate("e => e.tagName.toLowerCase()")
    if tag == "select":
        try:
            loc.select_option(label=value)
        except Exception:  # noqa: BLE001 - some lists key by value
            loc.select_option(value=value)
    else:
        loc.fill(value)
        loc.dispatch_event("change")


def deadline_ok(started: float, limit_s: Optional[float]) -> bool:
    return limit_s is None or time.time() - started < limit_s
