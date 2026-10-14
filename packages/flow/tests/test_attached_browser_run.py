"""Runs start from the signed-in session the task uses (B274).

A program compiled from a demonstration that began on a signed-in screen
cannot start in a browser Flow launches: that browser opens on the sign-in
page and step 1 never matches. The practice run, the drills, and production
runs therefore attach to the operator's running, signed-in Chromium through
the same loopback DevTools contract the recorder uses. No credential enters
the program, the recording, or the deployment config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.__main__ import (
    _attached_web_page,
    _resolve_backend_config,
    build_parser,
    main,
)
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.deployment import BackendConfig, DeploymentConfig

_SESSION_COOKIE = "oa_test_session=signed-in-clerk"

_SIGN_IN_HTML = b"""<!doctype html>
<html><head><title>Sign in</title></head>
<body style="font-family: sans-serif; margin: 40px">
  <h1>Sign in to continue</h1>
  <label>User <input id="user" type="text"></label>
  <label>Password <input id="password" type="password"></label>
  <button id="sign-in" style="font-size: 22px; padding: 12px 24px">Sign in</button>
</body></html>"""

_WORK_HTML = b"""<!doctype html>
<html><head><title>Referral queue</title></head>
<body style="font-family: sans-serif; margin: 40px">
  <h1>Referral queue</h1>
  <p>Signed in as the test clerk</p>
  <button id="open-next" style="font-size: 22px; padding: 12px 24px"
    onclick="document.getElementById('status').textContent = 'Referral opened for review'">
    Open next referral</button>
  <p id="status" style="font-size: 22px"></p>
</body></html>"""


@pytest.fixture(scope="module")
def signed_in_app() -> str:
    """A local app whose work screen exists only inside a signed-in session."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            if self.path.startswith("/login"):
                # The test's stand-in for a person signing in: it sets a
                # session cookie in the browser. No credential is involved.
                self.send_response(302)
                self.send_header("Set-Cookie", f"{_SESSION_COOKIE}; Path=/")
                self.send_header("Location", "/work")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            signed_in = _SESSION_COOKIE in (self.headers.get("Cookie") or "")
            payload = _WORK_HTML if signed_in else _SIGN_IN_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chromium_executable() -> Path | None:
    configured = os.environ.get("OPENADAPT_TEST_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates.append(Path(playwright.chromium.executable_path))
    except Exception:
        pass
    for command in ("google-chrome", "google-chrome-stable", "chromium"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _signed_in_bundle(app: str, tmp_path: Path) -> Path:
    """Record and compile a task whose demonstration began signed in."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder

    recording = tmp_path / "recording"
    bundle = tmp_path / "bundle"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800}, device_scale_factor=1
            )
            page.goto(f"{app}/login")
            page.get_by_text("Signed in as the test clerk").wait_for()
            recorder = Recorder(PlaywrightBackend(page), recording, app_url=page.url)
            box = page.locator("#open-next").bounding_box()
            assert box is not None
            recorder.click(
                int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
            )
            page.get_by_text("Referral opened for review").wait_for()
            recorder.finish()
        finally:
            browser.close()
    compile_recording(recording, bundle, name="open-next-referral")
    return bundle


def _start_signed_in_chromium(executable: Path, app: str, profile: Path):
    """Start a person's signed-in browser with a loopback DevTools endpoint."""

    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            f"{app}/login",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 20
    port_line = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("Chromium exited before its DevTools endpoint was ready")
        try:
            port_line = port_file.read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            port_line = ""
        if port_line:
            break
        time.sleep(0.05)
    assert port_line, "Chromium DevTools endpoint did not become ready"
    return process, f"http://127.0.0.1:{int(port_line)}"


def _match_recorded_viewport(endpoint: str) -> None:
    """Size the signed-in tab like the demonstration (a person resizing it).

    This resizes the browser window itself, as a person would. An emulation
    override belongs to the DevTools session that set it, and current Chromium
    drops it when that session disconnects, before the run attaches.
    """

    from playwright.sync_api import sync_playwright

    recorded = [1280, 800]
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        page = next(
            page
            for context in browser.contexts
            for page in context.pages
            if page.url.endswith("/work")
        )
        page.get_by_text("Signed in as the test clerk").wait_for(timeout=10_000)
        session = page.context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        live = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {
                    "width": window["bounds"]["width"] + recorded[0] - live[0],
                    "height": window["bounds"]["height"] + recorded[1] - live[1],
                },
            },
        )
        page.wait_for_function(
            "([w, h]) => window.innerWidth === w && window.innerHeight === h",
            arg=recorded,
            timeout=10_000,
        )
        session.detach()


def _page_text(endpoint: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        return [
            page.inner_text("body")
            for context in browser.contexts
            for page in context.pages
            if page.url.startswith("http://127.0.0.1")
        ]


@pytest.mark.timeout(300)
def test_practice_run_starts_from_the_signed_in_session_it_was_recorded_in(
    signed_in_app: str, tmp_path: Path
) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    bundle = _signed_in_bundle(signed_in_app, tmp_path)
    workflow = json.loads((bundle / "workflow.json").read_text())
    assert workflow["steps"][0]["action"] == "click"

    # The old path: a browser Flow launches opens on the sign-in page, so
    # step 1 never matches and the run stops before doing anything.
    fresh_run = tmp_path / "fresh-run"
    assert (
        main(
            [
                "replay",
                str(bundle),
                "--url",
                f"{signed_in_app}/work",
                "--run-dir",
                str(fresh_run),
            ]
        )
        == 1
    )
    fresh = json.loads((fresh_run / "report.json").read_text())
    assert fresh["success"] is False
    assert fresh["results"][0]["step_id"] == "step_000"
    assert fresh["results"][0]["ok"] is False

    # The fix: the same program attached to the signed-in browser passes
    # step 1 and finishes, and the person's browser stays open and signed in.
    process, endpoint = _start_signed_in_chromium(
        executable, signed_in_app, tmp_path / "profile"
    )
    try:
        _match_recorded_viewport(endpoint)
        attached_run = tmp_path / "attached-run"
        assert (
            main(
                [
                    "replay",
                    str(bundle),
                    "--url",
                    f"{signed_in_app}/work",
                    "--browser-cdp-endpoint",
                    endpoint,
                    "--run-dir",
                    str(attached_run),
                ]
            )
            == 0
        )
        attached = json.loads((attached_run / "report.json").read_text())
        assert attached["success"] is True
        assert attached["results"][0]["step_id"] == "step_000"
        assert attached["results"][0]["ok"] is True
        assert process.poll() is None, "the run must not close the person's browser"
        texts = _page_text(endpoint)
        assert len(texts) == 1
        assert "Signed in as the test clerk" in texts[0]
        assert "Referral opened for review" in texts[0]
        # Nothing in the program or the run's config carries the session.
        assert "signed-in-clerk" not in (bundle / "workflow.json").read_text()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_cli_flags_and_config_select_the_attached_session() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "bundle",
            "--browser-cdp-endpoint",
            "http://127.0.0.1:9222",
            "--browser-page-url",
            "http://127.0.0.1:9301/work",
        ]
    )
    backend = _resolve_backend_config(args, DeploymentConfig())
    assert backend.browser_cdp_endpoint == "http://127.0.0.1:9222"
    assert backend.browser_page_url == "http://127.0.0.1:9301/work"

    configured = DeploymentConfig.model_validate(
        {"backend": {"kind": "web", "browser_cdp_endpoint": "http://localhost:9333"}}
    )
    plain = build_parser().parse_args(["run", "bundle"])
    assert (
        _resolve_backend_config(plain, configured).browser_cdp_endpoint
        == "http://localhost:9333"
    )


@pytest.mark.parametrize(
    ("backend", "url", "message"),
    [
        (
            BackendConfig(browser_cdp_endpoint="http://10.0.0.5:9222"),
            "http://127.0.0.1:9301",
            "loopback",
        ),
        (
            BackendConfig(browser_cdp_endpoint="http://127.0.0.1"),
            "http://127.0.0.1:9301",
            "port",
        ),
        (
            BackendConfig(browser_cdp_endpoint="http://user:pw@127.0.0.1:9222"),
            "http://127.0.0.1:9301",
            "credentials",
        ),
        (
            BackendConfig(browser_cdp_endpoint="http://127.0.0.1:9222"),
            None,
            "application URL",
        ),
        (
            BackendConfig(browser_page_url="http://127.0.0.1:9301/work"),
            "http://127.0.0.1:9301",
            "browser_cdp_endpoint",
        ),
    ],
)
def test_attached_session_refuses_before_connecting(backend, url, message) -> None:
    with pytest.raises(SystemExit, match=message) as refused:
        with _attached_web_page(backend, url=url, workflow=None):
            pytest.fail("a refused attach must not yield a page")
    assert "Nothing was executed" in str(refused.value)


def test_attached_browser_keeps_css_pixel_frames() -> None:
    page = SimpleNamespace(on=lambda *_args: None)
    attached = build_backend(
        BackendConfig(browser_cdp_endpoint="http://127.0.0.1:9222"), page=page
    )
    launched = build_backend(BackendConfig(), page=page)
    assert attached._screenshot_scale == "css"
    assert launched._screenshot_scale == "device"


def test_attended_actions_refuse_a_signed_in_browser_they_would_not_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attended service launches its own visible browser. Given a config
    that names the person's signed-in browser, it must refuse rather than act
    in a new, signed-out session while the operator believes it is attached."""

    from contextlib import nullcontext

    from openadapt_flow.__main__ import _attended_service_from_args
    from openadapt_flow.runtime.durable import attended_service

    launched: list[str] = []

    def executor(*_args, **_kwargs):
        launched.append("browser")
        return nullcontext(None)

    monkeypatch.setattr(attended_service, "_deployment_executor", executor)
    backend = {
        "kind": "web",
        "url": "http://127.0.0.1:9301/work",
        "headed": True,
        "browser_cdp_endpoint": "http://127.0.0.1:9222",
    }
    with pytest.raises(ValueError, match="signed-in browser"):
        attended_service.AttendedActionService(
            DeploymentConfig.model_validate({"backend": backend})
        )
    page_only = {
        key: value for key, value in backend.items() if key != "browser_cdp_endpoint"
    }
    page_only["browser_page_url"] = "http://127.0.0.1:9301/work"
    with pytest.raises(ValueError, match="signed-in browser"):
        attended_service.AttendedActionService(
            DeploymentConfig.model_validate({"backend": page_only})
        )

    config = tmp_path / "deployment.yaml"
    config.write_text(
        "backend:\n"
        "  kind: web\n"
        "  url: http://127.0.0.1:9301/work\n"
        "  headed: true\n"
        "  browser_cdp_endpoint: http://127.0.0.1:9222\n"
    )
    args = build_parser().parse_args(
        ["console", "--attend", "--allow-actions", "--config", str(config)]
    )
    with pytest.raises(SystemExit, match="signed-in browser"):
        with _attended_service_from_args(args):
            pytest.fail("a refused attended service must not open")
    assert launched == []


@pytest.mark.parametrize(
    ("tab_url", "claimed"),
    [
        ("http://127.0.0.1:9301/work", "http://127.0.0.1:9301/work"),
        ("http://127.0.0.1:9301/work#top", "http://127.0.0.1:9301/work"),
        ("http://127.0.0.1:9301/patients/42", None),
        ("http://127.0.0.1:9301/work?patient=42", None),
    ],
)
def test_attached_run_claims_only_an_entry_url_its_tab_shows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tab_url: str,
    claimed: str | None,
) -> None:
    """An attached run requests no URL: it starts from whatever the signed-in
    tab shows. Its report may name the configured entry URL only when the tab
    is on it, so hosted validation never accepts a run as having started at
    the attested URL when it started on another page."""

    from contextlib import contextmanager

    import openadapt_flow.__main__ as cli
    from openadapt_flow.ir import ActionKind, Step, Workflow

    bundle = tmp_path / "bundle"
    Workflow(
        name="attached-entry",
        steps=[Step(id="settle", intent="Wait", action=ActionKind.WAIT)],
    ).save(bundle)
    page = SimpleNamespace(url=tab_url, on=lambda *_args: None)

    @contextmanager
    def attached(_backend_cfg, *, url, workflow=None):
        yield page

    class Captured(Exception):
        pass

    seen: dict[str, object] = {}

    def run(_backend, **kwargs):
        seen.update(kwargs)
        raise Captured

    monkeypatch.setattr(cli, "_attached_web_page", attached)
    monkeypatch.setattr(cli, "_build_and_run_replayer", run)
    args = build_parser().parse_args(
        [
            "replay",
            str(bundle),
            "--url",
            "http://127.0.0.1:9301/work",
            "--browser-cdp-endpoint",
            "http://127.0.0.1:9222",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    with pytest.raises(Captured):
        args.func(args)

    assert seen["execution_origin"] == "http://127.0.0.1:9301"
    assert seen["execution_entry_url"] == claimed
