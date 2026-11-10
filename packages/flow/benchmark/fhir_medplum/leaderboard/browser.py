"""The screen every agent gets: Medplum's own web app in a 1280x800 browser.

The app talks to the Medplum server. Its FHIR requests (``/fhir/R4/...``)
are routed, inside the browser, through the case's fault proxy, so a write
made by clicking Save meets the same faults as a write made over the API.
Everything else (sign-in, ``/auth/me``, static files) goes straight to the
server.

The harness signs the agent's user in once, by the app's own sign-in page,
and starts every run from a fresh browser context holding that session, on
the app's home page. No agent ever sees or types a password.

The browser stays inside the app. Any navigation to another origin, from a
click, a key, a script, a pop-up or a direct ``goto``, is aborted in the
browser before a request leaves it, and logged in the run's actions as
``blocked_navigation``. Only the app's own origin and the Medplum server's
origin can be navigated to.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

VIEWPORT = {"width": 1280, "height": 800}

#: Key names agents use, mapped to Playwright's.
_KEYS = {
    "return": "Enter",
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "space": " ",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "page_up": "PageUp",
    "pageup": "PageUp",
    "page_down": "PageDown",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
    "ctrl": "Control",
    "control": "Control",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
}


def key_name(name: str) -> str:
    n = name.strip()
    if len(n) == 1:
        return n
    low = n.lower()
    if low in _KEYS:
        return _KEYS[low]
    if re.fullmatch(r"f\d{1,2}", low):
        return low.upper()
    return n[:1].upper() + n[1:]


def chord(keys: list[str]) -> str:
    return "+".join(key_name(k) for k in keys if k)


def origin(url: str) -> str:
    """``scheme://host[:port]``, lower-cased; ``""`` for a URL with no host."""

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return ""
    if not u.scheme or not u.netloc:
        return ""
    return f"{u.scheme.lower()}://{u.netloc.lower()}"


def off_app(allowed: Iterable[str]) -> Callable[[str], bool]:
    """A URL matcher for every http(s) URL outside the ``allowed`` origins."""

    keep = frozenset(origin(a) for a in allowed) - {""}

    def match(url: str) -> bool:
        if not url.lower().startswith(("http://", "https://", "ws://", "wss://")):
            return False
        o = origin(url)
        return o == "" or o not in keep

    return match


@dataclass
class Screen:
    """One run's browser page, with the actions every adapter maps onto."""

    page: Any
    actions: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)
    #: Origins the browser may navigate to; empty means no check here (the
    #: browser context's route still applies).
    allowed: tuple[str, ...] = ()

    def screenshot(self) -> bytes:
        return self.page.screenshot(type="png")

    def url(self) -> str:
        return str(self.page.url)

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001 - a busy page is still a page
            pass

    def _do(self, what: str, **kw: Any) -> None:
        self.actions += 1
        self.log.append({"a": what, **kw})

    def click(
        self,
        x: float,
        y: float,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Optional[list[str]] = None,
    ) -> None:
        self._do("click", x=x, y=y, button=button, count=count, mods=modifiers or [])
        mods = [key_name(m) for m in modifiers or []]
        for m in mods:
            self.page.keyboard.down(m)
        try:
            self.page.mouse.click(x, y, button=button, click_count=count)
        finally:
            for m in reversed(mods):
                self.page.keyboard.up(m)
        self._settle()

    def move(self, x: float, y: float) -> None:
        self._do("move", x=x, y=y)
        self.page.mouse.move(x, y)

    def drag(self, path: list[tuple[float, float]]) -> None:
        self._do("drag", path=path)
        (x0, y0), rest = path[0], path[1:]
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for x, y in rest:
            self.page.mouse.move(x, y, steps=8)
        self.page.mouse.up()
        self._settle()

    def type(self, text: str) -> None:
        self._do("type", text=text)
        self.page.keyboard.type(text, delay=5)
        self._settle()

    def key(self, keys: list[str]) -> None:
        self._do("key", keys=keys)
        self.page.keyboard.press(chord(keys))
        self._settle()

    def scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        self._do("scroll", x=x, y=y, dx=dx, dy=dy)
        self.page.mouse.move(x, y)
        self.page.mouse.wheel(dx, dy)
        time.sleep(0.3)

    def wait(self, seconds: float = 1.0) -> None:
        self._do("wait", s=seconds)
        time.sleep(min(max(seconds, 0.0), 5.0))

    def back(self) -> None:
        self._do("back")
        self.page.go_back()
        self._settle()

    def goto(self, url: str) -> None:
        """Open a page of the app; other origins are refused."""

        if self.allowed and off_app(self.allowed)(url):
            self._do("goto_refused", url=url)
            raise ValueError("only the clinic's app can be opened in this browser")
        self._do("goto", url=url)
        self.page.goto(url)
        self._settle()


class MedplumBrowser:
    """A browser signed in to the Medplum app as the agent's user."""

    def __init__(self, ui_url: str, server_url: str, *, headless: bool = True) -> None:
        self.ui_url = ui_url.rstrip("/") + "/"
        self.server_url = server_url.rstrip("/")
        self.headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self.state: Optional[dict[str, Any]] = None
        self._creds: Optional[tuple[str, str]] = None
        self.signed_in_at = 0.0

    def start(self) -> "MedplumBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        return self

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def sign_in(self, email: str, password: str) -> None:
        """The app's own sign-in, done by the harness; the session is kept."""

        ctx = self._browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        page.goto(self.ui_url + "signin")
        page.get_by_label(re.compile("email", re.I)).fill(email)
        nxt = page.get_by_role("button", name=re.compile("^(next|continue)$", re.I))
        if nxt.count():
            nxt.first.click()
        page.get_by_test_id("auth.password").fill(password)
        page.get_by_role("button", name=re.compile("sign in", re.I)).click()
        for _ in range(3):
            try:
                page.wait_for_url(lambda u: "signin" not in u, timeout=30000)
                break
            except Exception:  # noqa: BLE001 - a project chooser, or a slow first load
                choice = page.get_by_text(re.compile("synthetic clinic", re.I))
                if choice.count():
                    choice.first.click()
        else:
            page.screenshot(path=str(Path.home() / ".openadapt" / "leaderboard" / "signin-failed.png"))
            raise RuntimeError("the harness couldn't sign in to the Medplum app")
        page.wait_for_load_state("networkidle")
        self.state = ctx.storage_state()
        ctx.close()
        self._creds = (email, password)
        self.signed_in_at = time.monotonic()

    def fresh(self, max_age_s: float = 900) -> None:
        """Sign in again if the session is older than ``max_age_s``.

        Medplum's session lasts an hour; a run may take 40 minutes, so every
        run starts on a session at most 15 minutes old.
        """

        if self._creds and time.monotonic() - self.signed_in_at > max_age_s:
            self.sign_in(*self._creds)

    def open(self, proxy_fhir_url: str, evidence: Optional[Path] = None) -> tuple[Any, Screen]:
        """A fresh context on the home page, its FHIR traffic sent through the proxy."""

        assert self.state is not None, "sign_in first"
        ctx = self._browser.new_context(viewport=VIEWPORT, storage_state=self.state)
        server_fhir = f"{self.server_url}/fhir/R4"
        proxy = proxy_fhir_url.rstrip("/")

        def via_proxy(route: Any, request: Any) -> None:
            url = proxy + request.url[len(server_fhir) :]
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "origin", "referer", "content-length")
            }
            try:
                resp = route.fetch(url=url, headers=headers, timeout=60000)
            except Exception:  # noqa: BLE001 - the proxy hung up: a network error
                route.abort("failed")
                return
            body = resp.body().replace(proxy.encode(), server_fhir.encode())
            out = {
                k: v.replace(proxy, server_fhir) if k.lower() == "location" else v
                for k, v in resp.headers.items()
                if k.lower() in ("content-type", "location", "etag", "last-modified")
            }
            out["access-control-allow-origin"] = self.ui_url.rstrip("/")
            out["access-control-allow-credentials"] = "true"
            out["access-control-expose-headers"] = "Location, ETag, Last-Modified"
            route.fulfill(status=resp.status, headers=out, body=body)

        allowed = (self.ui_url, self.server_url)
        blocked: list[dict[str, Any]] = []

        def stay_in_app(route: Any, request: Any) -> None:
            # Off-app subresources the app itself loads pass as before; a
            # navigation (any frame, any pop-up) to another origin never
            # leaves the browser.
            if request.is_navigation_request():
                blocked.append({"a": "blocked_navigation", "url": request.url})
                route.abort("blockedbyclient")
                return
            route.fallback()

        # Registered first, so the FHIR route below is tried before it.
        ctx.route(off_app(allowed), stay_in_app)
        ctx.route(re.compile("^" + re.escape(server_fhir) + r"(/|\?|$)"), via_proxy)
        page = ctx.new_page()
        page.goto(self.ui_url)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        screen = Screen(page, log=blocked, allowed=allowed)
        return ctx, screen
