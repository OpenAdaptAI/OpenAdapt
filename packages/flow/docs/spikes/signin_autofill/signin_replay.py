"""Replay a taught sign-in through the person's everyday Chrome, at the OS level.

Spike code, not product code. It reuses openadapt-flow's native macOS backend
(``MacOSBackend``: exact-window binding, foreground proof, CGEvent input) and
adds only what the spike measures:

* an origin gate: the AX web area's URL must match the taught origin before any
  input, or the run refuses;
* waiting for Chrome's autofill list (a separate Chrome-owned window);
* picking the saved login either by keyboard (Down, Return into the bound
  window) or by mouse (a click on the list row);
* noticing a macOS authentication sheet (Touch ID or password) and waiting for
  the person, which counts as their approval;
* submitting and checking the result by page title and server status, never by
  reading the password field.

The script never types a password and never reads a secure field's value. With
``--check-ax-leak`` it compares what AX returns for the password field against
the test password inside this process and prints only a yes/no.

    python signin_replay.py --select keyboard --runs 10
    python signin_replay.py --select mouse --runs 10
    python signin_replay.py --window-title "Spike Login 47812"   # origin test
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)

import openadapt_flow.backends.remote_display as _remote_display
from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError

_exact_title_match = _remote_display._macos_ax_title_matches  # noqa: SLF001


def _title_match_with_profile(window: Any, ax_title: object) -> bool:
    """Spike-only widening: accept Chrome's per-profile title suffix.

    An everyday Chrome that is signed in to a profile titles its windows
    "<page> - Google Chrome - <profile name>". The backend accepts only
    "<page> - Google Chrome", so it cannot bind the person's real window.
    Uniqueness is still enforced by the backend (exactly one AX match).
    """
    candidate = str(ax_title or "")
    prefix = f"{window.title} - Google Chrome - "
    return _exact_title_match(window, ax_title) or (
        window.owner == "Google Chrome"
        and candidate.startswith(prefix)
        and " - " not in candidate[len(prefix) :]
    )


_remote_display._macos_ax_title_matches = _title_match_with_profile  # noqa: SLF001


def _window_id_at_point_skip_cursor(self: Any, x: float, y: float) -> Optional[int]:
    """Spike-only fix: ignore the Window Server's cursor sprite window.

    Over a text field macOS can draw the I-beam as its own top-layer window
    ("Window Server" / "Cursor"). The backend's point check counts it as the
    window under the pointer and refuses every click on a text field.
    """
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    for window in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if (
            window.get("kCGWindowOwnerName") == "Window Server"
            and window.get("kCGWindowName") == "Cursor"
        ):
            continue
        b = window.get("kCGWindowBounds", {}) or {}
        left, top = float(b.get("X", 0.0)), float(b.get("Y", 0.0))
        width, height = float(b.get("Width", 0.0)), float(b.get("Height", 0.0))
        if (
            width > 0
            and height > 0
            and left <= x < left + width
            and top <= y < top + height
        ):
            window_id = int(window.get("kCGWindowNumber", 0) or 0)
            return window_id if window_id > 0 else None
    return None


_remote_display.MacWindowClient.window_id_at_point = _window_id_at_point_skip_cursor


def _frontmost_skip_chrome_bubbles(self: Any) -> Optional[int]:
    """Spike-only fix: skip Chrome's untitled helper bubbles in the top proof.

    Hovering a link makes Chrome show its URL status bubble, hovering the
    window controls shows a tooltip window titled "Window", and update or
    sign-in notices use similar bubbles. Each is a small layer-0
    window owned by Chrome, so the backend sees it as the topmost window and
    refuses all input to the real browser window.
    """
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    for window in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if int(window.get("kCGWindowLayer", 0) or 0) != 0:
            continue
        b = window.get("kCGWindowBounds", {}) or {}
        if (
            window.get("kCGWindowOwnerName") == CHROME
            and float(b.get("Height", 0.0)) < 120
        ):
            continue
        window_id = int(window.get("kCGWindowNumber", 0) or 0)
        if window_id > 0:
            return window_id
    return None


_remote_display.MacWindowClient.frontmost_window_id = _frontmost_skip_chrome_bubbles

HERE = Path(__file__).resolve().parent
RECIPE = HERE / "taught_signin.json"
CHROME = "Google Chrome"
# Owners of the macOS authentication sheet Chrome raises when "Use your screen
# lock when filling passwords" is on. Seen names vary by macOS release.
AUTH_OWNERS = {"coreautha", "SecurityAgent", "LocalAuthenticationRemoteService"}


class Refused(RuntimeError):
    """The replay stopped before input, on purpose."""


@dataclass
class RunResult:
    ok: bool = False
    outcome: str = ""
    select: str = ""
    origin_seen: Optional[str] = None
    popup_seen: bool = False
    popup_layer: Optional[int] = None
    popup_wait_s: Optional[float] = None
    auth_prompt_seen: bool = False
    auth_wait_s: Optional[float] = None
    username_prefilled: bool = False
    backend_refused_popup_click: Optional[str] = None
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


# --- AX helpers (read-only) -------------------------------------------------


def _attr(el: Any, name: str) -> Any:
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _frame(el: Any) -> Optional[tuple[float, float, float, float]]:
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    _, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    _, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    return (p.x, p.y, s.width, s.height)


def _walk(el: Any, depth: int = 0, limit: int = 40):
    yield el
    if depth >= limit:
        return
    for child in _attr(el, "AXChildren") or []:
        yield from _walk(child, depth + 1, limit)


def _chrome_pid() -> int:
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0):
        if w.get("kCGWindowOwnerName") == CHROME:
            return int(w["kCGWindowOwnerPID"])
    raise Refused("Chrome is not running")


def _ax_window(title: str, wait_s: float = 2.0) -> Any:
    deadline = time.monotonic() + wait_s
    while True:
        app = AXUIElementCreateApplication(_chrome_pid())
        windows = _attr(app, "AXWindows") or []
        matches = [
            w
            for w in windows
            if (_attr(w, "AXTitle") or "").startswith(title + " - ")
            or (_attr(w, "AXTitle") or "") == title
        ]
        if len(matches) == 1:
            return matches[0]
        if time.monotonic() > deadline:
            raise Refused(
                f"expected one Chrome window titled {title!r}, saw {len(matches)} "
                f"(AX listed {len(windows)} Chrome windows)"
            )
        time.sleep(0.2)


def _web_area(win: Any) -> Any:
    for el in _walk(win):
        if _attr(el, "AXRole") == "AXWebArea":
            return el
    raise Refused("no web content in the window (AX web area missing)")


def _origin(url: str) -> str:
    parts = urlsplit(str(url))
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{(parts.hostname or '').lower()}{port}"


def _control(web: Any, role: str, title: str, subrole: Optional[str] = None) -> Any:
    found = [
        el
        for el in _walk(web)
        if _attr(el, "AXRole") == role
        and (_attr(el, "AXTitle") or "") == title
        and (subrole is None or _attr(el, "AXSubrole") == subrole)
    ]
    if len(found) != 1:
        raise Refused(f"expected one {role} {title!r}, saw {len(found)}")
    return found[0]


# --- window-server helpers --------------------------------------------------


def _windows() -> list[dict[str, Any]]:
    opts = Quartz.kCGWindowListOptionOnScreenOnly
    return list(Quartz.CGWindowListCopyWindowInfo(opts, 0) or [])


def _popup_near(
    before: set[int], pid: int, field: tuple[float, float, float, float]
) -> Optional[dict[str, Any]]:
    fx, fy, fw, fh = field
    for w in _windows():
        if int(w.get("kCGWindowOwnerPID", 0)) != pid:
            continue
        if int(w["kCGWindowNumber"]) in before:
            continue
        b = w.get("kCGWindowBounds", {})
        # The list opens just under (or over) the field, horizontally overlapping.
        if b["X"] < fx + fw and b["X"] + b["Width"] > fx and b["Height"] < 400:
            return dict(w)
    return None


def _auth_window() -> Optional[dict[str, Any]]:
    for w in _windows():
        if str(w.get("kCGWindowOwnerName", "")) in AUTH_OWNERS:
            return dict(w)
    return None


def _status(origin: str) -> dict[str, int]:
    with urllib.request.urlopen(origin + "/status", timeout=3) as resp:
        return json.loads(resp.read())


# --- the replay -------------------------------------------------------------


class Replay:
    def __init__(self, recipe: dict[str, Any], window_title: str) -> None:
        self.recipe = recipe
        self.window_title = window_title
        self.backend = MacOSBackend(app=CHROME, window_title=window_title)
        self.fill_only = False

    def _to_pixels(self, frame: tuple[float, float, float, float]) -> tuple[int, int]:
        self.backend.screenshot()
        win = self.backend._resolve_window(refresh=True)  # noqa: SLF001
        scale = self.backend._scale or 1.0  # noqa: SLF001
        cx = frame[0] + frame[2] / 2
        cy = frame[1] + frame[3] / 2
        return int((cx - win.bounds[0]) * scale), int((cy - win.bounds[1]) * scale)

    def origin_gate(self, result: RunResult) -> Any:
        web = _web_area(_ax_window(self.window_title))
        seen = _origin(_attr(web, "AXURL") or "")
        result.origin_seen = seen
        if seen != self.recipe["origin"]:
            raise Refused(
                f"page origin {seen} is not the taught origin {self.recipe['origin']}"
            )
        return web

    def run(self, select: str, auth_timeout_s: float = 60.0) -> RunResult:
        result = RunResult(select=select)
        t0 = time.monotonic()
        try:
            self._run(result, select, auth_timeout_s)
        except Refused as exc:
            result.outcome = f"refused: {exc}"
        except MacOSBackendError as exc:
            result.outcome = f"backend refused: {exc}"
        result.elapsed_s = round(time.monotonic() - t0, 2)
        return result

    def _run(self, result: RunResult, select: str, auth_timeout_s: float) -> None:
        self.backend.ensure_foreground()
        web = self.origin_gate(result)
        username = _control(web, "AXTextField", "Username")
        result.username_prefilled = bool(_attr(username, "AXValue"))
        pid = _chrome_pid()
        before = {int(w["kCGWindowNumber"]) for w in _windows()}
        user_frame = _frame(username)
        assert user_frame is not None

        # 1. Focus the username field through the bound, fail-closed click.
        self.backend.click(*self._to_pixels(user_frame))

        # 2. Wait for Chrome's autofill list.
        t_popup = time.monotonic()
        popup = None
        while time.monotonic() - t_popup < 3.0:
            popup = _popup_near(before, pid, user_frame)
            if popup:
                break
            time.sleep(0.05)
        if popup is None:
            raise Refused("Chrome's saved-login list did not appear")
        result.popup_seen = True
        result.popup_layer = int(popup.get("kCGWindowLayer", 0))
        result.popup_wait_s = round(time.monotonic() - t_popup, 2)
        # Chrome ignores clicks on a list that just opened (anti-clickjacking).
        time.sleep(0.8)

        # 3. Re-check the origin right before choosing the login.
        self.origin_gate(result)

        # 4. Choose the saved login.
        if select == "keyboard":
            self.backend.press("down")
            try:
                self.backend.press("return")
            except MacOSBackendError as exc:
                # When Chrome asks for the screen lock, the macOS sheet takes
                # keyboard focus between key-down and the backend's
                # post-release check. Only that case is expected here.
                time.sleep(0.3)
                if not _auth_window():
                    raise
                result.notes.append(f"post-select backend check: {str(exc)[:60]}")
        else:
            pb = popup["kCGWindowBounds"]
            # First row of the list: the saved login for this origin.
            row = (pb["X"] + pb["Width"] / 2, pb["Y"] + min(38.0, pb["Height"] * 0.3))
            try:
                win = self.backend._resolve_window(refresh=True)  # noqa: SLF001
                scale = self.backend._scale or 1.0  # noqa: SLF001
                self.backend.click(
                    int((row[0] - win.bounds[0]) * scale),
                    int((row[1] - win.bounds[1]) * scale),
                )
            except MacOSBackendError as exc:
                # Expected: the list is another window, so the bound-window
                # point check refuses. Use the same client primitive directly
                # to measure whether Chrome accepts an OS-level click there.
                result.backend_refused_popup_click = str(exc)[:160]
                client = self.backend._mac_client  # noqa: SLF001
                client.mouse_move(*row)
                time.sleep(0.05)
                client.mouse(*row, button="left", down=True, click_count=1)
                time.sleep(0.05)
                client.mouse(*row, button="left", down=False, click_count=1)

        # 5. A macOS authentication sheet means Chrome wants the person.
        t_auth = time.monotonic()
        while time.monotonic() - t_auth < 1.5:
            if _auth_window():
                result.auth_prompt_seen = True
                break
            time.sleep(0.05)
        if result.auth_prompt_seen:
            while _auth_window() and time.monotonic() - t_auth < auth_timeout_s:
                time.sleep(0.2)
            result.auth_wait_s = round(time.monotonic() - t_auth, 2)
            if _auth_window():
                raise Refused("the person did not approve the fill in time")
            time.sleep(0.3)

        if self.fill_only:
            result.outcome = "filled (not submitted)"
            result.ok = True
            return

        # 6. Submit through the bound click. Re-read AX: the page may re-render.
        web = self.origin_gate(result)
        button = _control(web, "AXButton", "Sign in")
        before_status = _status(self.recipe["origin"])
        try:
            self.backend.click(*self._to_pixels(_frame(button)))
        except MacOSBackendError as exc:
            # The click navigates and Chrome retitles the window, so the
            # backend's post-release check can no longer find the bound
            # title. The press already happened; the server decides below.
            result.notes.append(f"post-submit backend check: {str(exc)[:90]}")

        # 7. Outcome: the protected page, confirmed by the server.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            after = _status(self.recipe["origin"])
            if after["protected_views"] > before_status["protected_views"]:
                result.ok = True
                result.outcome = "signed in"
                return
            if after["fail"] > before_status["fail"]:
                result.outcome = "server rejected the fill (wrong or empty password)"
                return
            time.sleep(0.1)
        result.outcome = "no sign-in within 5 s (fill or submit did not happen)"

    def sign_out(self) -> None:
        """Reset between runs. Not under test, so it uses AXPress on the link."""
        port = self.recipe["origin"].rsplit(":", 1)[-1]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                web = _web_area(_ax_window("Protected " + port))
                break
            except Refused:
                time.sleep(0.2)
        else:
            raise Refused("protected page never showed")
        links = [el for el in _walk(web) if _attr(el, "AXRole") == "AXLink"]
        if not links:
            raise Refused("no Sign out link")
        # AXPress returns success but Chrome does not follow the link, so
        # click it through the backend. Navigation retitles the window, which
        # trips the backend's post-release check; the click already landed.
        backend = MacOSBackend(app=CHROME, window_title="Protected " + port)
        backend.ensure_foreground()
        backend.screenshot()
        win = backend._resolve_window(refresh=True)  # noqa: SLF001
        scale = backend._scale or 1.0  # noqa: SLF001
        f = _frame(links[0])
        try:
            backend.click(
                int((f[0] + f[2] / 2 - win.bounds[0]) * scale),
                int((f[1] + f[3] / 2 - win.bounds[1]) * scale),
            )
        except MacOSBackendError:
            pass
        while time.monotonic() < deadline + 5.0:
            try:
                _ax_window(self.window_title)
                time.sleep(0.5)
                return
            except Refused:
                time.sleep(0.2)
        raise Refused("sign-out did not return to the login form")


def check_ax_leak(window_title: str) -> None:
    """Does AX hand back the filled password? Prints yes/no only."""
    account = json.loads((HERE / "test_account.local.json").read_text())
    web = _web_area(_ax_window(window_title))
    pw = _control(web, "AXTextField", "Password", "AXSecureTextField")
    value = _attr(pw, "AXValue")
    chars = _attr(pw, "AXNumberOfCharacters")
    leaked = value is not None and account["password"] in str(value)
    print(
        json.dumps(
            {
                "ax_value_present": value is not None,
                "ax_value_is_password": leaked,
                "ax_number_of_characters": chars,
            }
        )
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--select", choices=("keyboard", "mouse"), default="keyboard")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--window-title", default=None)
    p.add_argument("--no-sign-out", action="store_true")
    p.add_argument("--check-ax-leak", action="store_true")
    p.add_argument("--fill-only", action="store_true", help="stop before submit")
    p.add_argument("--log", default=None, help="append JSON lines here")
    p.add_argument(
        "--origin",
        default=None,
        help="test only: override the taught origin (simulates no origin gate)",
    )
    args = p.parse_args()
    recipe = json.loads(RECIPE.read_text())
    if args.origin:
        recipe["origin"] = args.origin
    title = args.window_title or recipe["window_title"]
    if args.check_ax_leak:
        check_ax_leak(title)
        return
    replay = Replay(recipe, title)
    replay.fill_only = args.fill_only
    for i in range(args.runs):
        result = replay.run(args.select)
        line = {"run": i + 1, "window_title": title, **asdict(result)}
        print(json.dumps(line), flush=True)
        if args.log:
            with open(args.log, "a") as fh:
                fh.write(json.dumps(line) + "\n")
        if result.ok and not args.no_sign_out and not args.fill_only:
            replay.sign_out()
        elif not result.ok and args.runs > 1:
            time.sleep(1.0)
    sys.exit(0)


if __name__ == "__main__":
    main()
