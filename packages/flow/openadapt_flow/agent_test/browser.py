"""The screen every agent gets: the app's own pages in a 1280x800 headless browser.

The browser talks only to the app's front door, so a save made by clicking
meets the scenario's fault. The harness signs in with the app pack's steps
in a fresh browser context for every scenario, and the run starts on the
app's start page. No agent sees or types the sign-in values. Navigation to
any other origin is aborted in the browser and logged.

Only ``chrome-headless-shell`` (Playwright's headless build, or the path in
``OPENADAPT_TEST_CHROMIUM_EXECUTABLE``) is launched; nothing opens a window.
The browser also listens for the Chrome DevTools Protocol on a local port,
so an agent in another process (a Browser-Use script, an MCP browser
server) can drive the same signed-in page.
"""

from __future__ import annotations

import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from openadapt_flow.agent_test.packs import AppPack, fill

VIEWPORT = {"width": 1280, "height": 800}

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
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return ""
    if not u.scheme or not u.netloc:
        return ""
    return f"{u.scheme.lower()}://{u.netloc.lower()}"


def off_app(allowed: Iterable[str]) -> Callable[[str], bool]:
    keep = frozenset(origin(a) for a in allowed) - {""}

    def match(url: str) -> bool:
        if not url.lower().startswith(("http://", "https://", "ws://", "wss://")):
            return False
        o = origin(url)
        return o == "" or o not in keep

    return match


@dataclass
class Screen:
    """One scenario's page, with the actions computer-use adapters map onto."""

    page: Any
    actions: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)
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
        self._do("type", chars=len(text))
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_steps(
    page: Any, base: str, steps: list[dict[str, Any]], values: dict[str, Any]
) -> None:
    """Run app-pack steps (sign-in, the reference agents) on ``page``."""

    for raw in steps:
        st = fill(raw, values)
        timeout = float(st.get("timeout_s", 30)) * 1000
        if "goto" in st:
            target = st["goto"]
            page.goto(
                target if target.startswith("http") else base + target,
                timeout=timeout * 2,
            )
        elif "fill" in st:
            page.fill(st["fill"], str(st.get("value", "")), timeout=timeout)
        elif "click" in st:
            page.click(st["click"], timeout=timeout)
        elif "click_role" in st:
            page.get_by_role(st["click_role"], name=st.get("name"), exact=True).click(
                timeout=timeout
            )
        elif "select" in st:
            page.select_option(st["select"], str(st.get("value", "")), timeout=timeout)
        elif "key" in st:
            page.keyboard.press(chord(str(st["key"]).split("+")))
        elif "submit" in st:
            # Submit a form directly; deferred so the navigation does not
            # destroy the evaluating context.
            page.eval_on_selector(st["submit"], "f => setTimeout(() => f.submit(), 0)")
            try:
                page.wait_for_load_state("load", timeout=max(timeout, 60000))
            except Exception:  # noqa: BLE001
                pass
        elif "eval" in st:
            page.evaluate(st["eval"])
        elif "sleep" in st:
            time.sleep(float(st["sleep"]))
        elif "wait" in st:
            try:
                page.wait_for_load_state(st["wait"], timeout=max(timeout, 30000))
            except Exception:  # noqa: BLE001 - a page that keeps polling
                pass
        elif "wait_selector" in st:
            page.wait_for_selector(
                st["wait_selector"], state=st.get("state", "visible"), timeout=timeout
            )
        elif "wait_url_not_contains" in st:
            frag = st["wait_url_not_contains"]
            page.wait_for_url(
                lambda u, frag=frag: frag not in u, timeout=max(timeout, 60000)
            )
        elif "wait_url_contains" in st:
            frag = st["wait_url_contains"]
            page.wait_for_url(
                lambda u, frag=frag: frag in u, timeout=max(timeout, 60000)
            )
        elif "absent" in st:
            if page.locator(st["absent"]).count():
                raise RuntimeError(
                    st.get("error") or f"{st['absent']} is still on the page"
                )
        else:
            raise ValueError(f"unknown step {sorted(st)}")


class AppBrowser:
    """A headless Chromium the harness signs in to the app, once per scenario."""

    def __init__(self, app: AppPack, base: str) -> None:
        self.app = app
        self.base = base.rstrip("/")
        self.cdp_port = free_port()
        self._pw: Any = None
        self._browser: Any = None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"

    def start(self) -> "AppBrowser":
        from playwright.sync_api import sync_playwright

        exe = os.environ.get("OPENADAPT_TEST_CHROMIUM_EXECUTABLE") or None
        if exe and "headless" not in os.path.basename(exe).lower():
            raise SystemExit(
                "OPENADAPT_TEST_CHROMIUM_EXECUTABLE must be chrome-headless-shell"
            )
        from openadapt_flow._browser_readiness import launch_rendering_ready

        self._pw = sync_playwright().start()
        try:  # a browser that does not render is closed and refused
            self._browser = launch_rendering_ready(
                self._pw.chromium,
                headless=True,
                executable_path=exe,
                args=[
                    f"--remote-debugging-port={self.cdp_port}",
                    "--remote-debugging-address=127.0.0.1",
                ],
            )
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        return self

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def is_signed_out(self, screen: Screen) -> bool:
        if (
            self.app.signed_out_url_contains
            and self.app.signed_out_url_contains in screen.url()
        ):
            return True
        sel = self.app.signed_out_selector
        try:
            return bool(sel and screen.page.locator(sel).count())
        except Exception:  # noqa: BLE001
            return False

    def open(self) -> tuple[Any, Screen]:
        ctx = self._browser.new_context(viewport=VIEWPORT)
        allowed = (self.base,)
        blocked: list[dict[str, Any]] = []

        def stay(route: Any, request: Any) -> None:
            if request.is_navigation_request():
                blocked.append({"a": "blocked_navigation", "url": request.url[:200]})
                route.abort("blockedbyclient")
                return
            route.fallback()

        ctx.route(off_app(allowed), stay)
        page = ctx.new_page()
        run_steps(page, self.base, self.app.login, {})
        if self.app.start_path is not None:
            home = self.base + self.app.start_path
            if page.url != home:
                page.goto(home)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        return ctx, Screen(page, log=blocked, allowed=allowed)
