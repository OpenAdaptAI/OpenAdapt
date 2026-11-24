"""Real Google Chrome on a Desktop-managed profile (Windows and macOS).

Opt-in: runs only with ``OPENADAPT_REAL_CHROME=1``, which the GitHub-hosted
Windows and macOS jobs and the Windows lab set. It opens a visible Chrome
window, so it never runs on a developer Mac by default.

A synthetic sign-in page on loopback sets a one-hour cookie. The test checks,
from the server side only, that Chrome sends it again after Desktop closes
and reopens the profile. Desktop never reads the cookie.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openadapt_desktop.chrome_profile import (
    ChromeProfileError,
    ChromeProfileService,
    find_chrome,
    notice,
    windows_elevated,
)

ELEVATED = sys.platform == "win32" and windows_elevated()

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_REAL_CHROME") != "1",
    reason="opens a real Chrome window; set OPENADAPT_REAL_CHROME=1 (CI and the Windows lab)",
)

TOKEN = "synthetic-session-0001"


class _Portal(BaseHTTPRequestHandler):
    seen: list[tuple[str, bool]] = []

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        signed_in = f"sid={TOKEN}" in (self.headers.get("Cookie") or "")
        _Portal.seen.append((self.path, signed_in))
        if self.path.startswith("/login"):
            self.send_response(302)
            self.send_header("Set-Cookie", f"sid={TOKEN}; Max-Age=3600; Path=/; HttpOnly")
            self.send_header("Location", "/home")
            self.end_headers()
            return
        body = (
            b"<title>Synthetic portal</title><h1>Signed in as tester</h1>"
            if signed_in
            else b"<title>Synthetic portal</title><h1>Sign in</h1>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def portal():
    _Portal.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def _page_text(endpoint: str, origin: str) -> str:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        try:
            pages = [p for c in browser.contexts for p in c.pages if p.url.startswith(origin)]
            assert len(pages) == 1, [p.url for c in browser.contexts for p in c.pages]
            return pages[0].inner_text("h1")
        finally:
            # Leaving only drops the DevTools connection; Chrome stays open.
            browser.close()


def _navigate_open_page(endpoint: str, target_url: str) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        try:
            pages = [p for c in browser.contexts for p in c.pages]
            page = pages[0] if pages else browser.contexts[0].new_page()
            page.goto(target_url, wait_until="domcontentloaded")
        finally:
            browser.close()


def _cookies_db_exists(user_data: Path) -> bool:
    return (user_data / "Default" / "Cookies").exists() or (
        user_data / "Default" / "Network" / "Cookies"
    ).exists()


@pytest.mark.skipif(not ELEVATED, reason="only an elevated Windows process is refused")
def test_an_elevated_windows_desktop_refuses_real_chrome(tmp_path: Path) -> None:
    find_chrome()
    service = ChromeProfileService(tmp_path / "data")
    profile_id = service.create("Synthetic", notice()["sha256"], True)["profile_id"]
    with pytest.raises(ChromeProfileError) as err:
        service.open(profile_id, "https://example.invalid/")
    assert err.value.code == "elevated_refused"
    assert not (service.root / profile_id / "user-data" / "lockfile").exists()


@pytest.mark.skipif(
    ELEVATED,
    reason="an elevated Windows process is refused (GitHub's Windows runner); "
    "the Windows lab runs this without administrator rights",
)
def test_sign_in_persists_and_desktop_closes_only_its_window(tmp_path: Path, portal: str) -> None:
    find_chrome()  # the job installs nothing; Chrome must already be there
    service = ChromeProfileService(tmp_path / "data")
    n = notice()
    profile_id = service.create("Synthetic portal", n["sha256"], True)["profile_id"]

    own: subprocess.Popen[bytes] | None = None
    try:
        opened = service.open(profile_id, f"{portal}/login")
        endpoint = opened["browser_cdp_endpoint"]
        assert endpoint.startswith("http://127.0.0.1:")
        assert _wait_for(lambda: ("/home", True) in _Portal.seen), (
            f"expected ('/home', True) in {_Portal.seen!r}"
        )
        assert _page_text(endpoint, portal) == "Signed in as tester"

        user_data = service.root / profile_id / "user-data"
        # Wait for Chrome's NetworkService to create the on-disk cookie store
        # before starting a second Chrome or closing, so two fresh Chrome
        # processes never race to initialize macOS "Chrome Safe Storage".
        _wait_for(lambda: _cookies_db_exists(user_data), 10.0)

        # The person's own Chrome, which Desktop didn't open and must leave alone.
        own_dir = Path(tempfile.mkdtemp(prefix="own-chrome-"))
        own = subprocess.Popen(
            [
                str(find_chrome()),
                f"--user-data-dir={own_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ]
        )

        # Another Desktop that didn't open this profile refuses it (in use).
        other = ChromeProfileService(tmp_path / "data")
        with pytest.raises(ChromeProfileError) as err:
            other.open(profile_id, f"{portal}/home")
        assert err.value.code == "profile_in_use"

        assert service.close(profile_id)["closed"] is True
        lock = "lockfile" if sys.platform == "win32" else "SingletonLock"
        assert _wait_for(lambda: not os.path.lexists(user_data / lock), 15)

        # Reopen: Chrome sends the cookie it kept; nothing was typed again.
        _Portal.seen.clear()
        reopened = service.open(profile_id, f"{portal}/home")
        if not _wait_for(lambda: ("/home", True) in _Portal.seen, 10.0) and not _Portal.seen:
            # On macOS, launching a second Chrome instance while another Chrome
            # process is active can occasionally open without loading the CLI
            # URL; navigate the already-open tab to /home without re-logging in.
            _navigate_open_page(reopened["browser_cdp_endpoint"], f"{portal}/home")
        assert _wait_for(lambda: ("/home", True) in _Portal.seen, 20.0), (
            f"expected ('/home', True) in {_Portal.seen!r}"
        )
        assert _page_text(reopened["browser_cdp_endpoint"], portal) == "Signed in as tester"
        service.close_all()
        assert all(not p["open"] for p in service.list()["profiles"])

        # The person's own Chrome is still running.
        assert own.poll() is None
    finally:
        service.close_all()
        if own is not None:
            own.terminate()
            try:
                own.wait(10)
            except subprocess.TimeoutExpired:
                own.kill()
