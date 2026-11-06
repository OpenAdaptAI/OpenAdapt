"""Fresh browser/capture/actuator controls for effective form submission."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

from openadapt_flow.compiler.api_proposal import admit_api_step, propose_api_step
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.network_capture import NETWORK_FILE, SECRET, NetworkCapture, load
from openadapt_flow.runtime.actuators import ActuationStatus
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

FORM = """<!doctype html><html><head></head><body>
<form id="entry" action="/publish" method="post">
<input name="title" id="title"><button id="send" name="decision" value="Send">Send</button>
</form></body></html>"""
CAPTURE_COMPLETION_TIMEOUT_S = 3.0


class Site:
    def __init__(self) -> None:
        self.html = FORM
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def handle_request(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type", ""),
                        "body": raw.decode("utf-8"),
                        "received_monotonic": time.monotonic(),
                    }
                )
                body = (
                    owner.html if up.urlsplit(self.path).path == "/compose" else "ok"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = handle_request
            do_POST = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.last_capture_diagnostics: dict[str, Any] | None = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


def _safe_capture_diagnostics(
    capture: NetworkCapture,
    site: Site,
    page: Any,
    t0: float,
    click_started: float,
    stopped: float,
) -> dict[str, Any]:
    """Project capture timing and shape without exposing request contents."""
    try:
        posts = [line for line in capture.lines(t0) if line.get("method") == "POST"]
        server_posts = site.writes()
        return {
            "schema": "openadapt.test-capture-diagnostics/1",
            "available": True,
            "captured_post_count": len(posts),
            "captured_post_with_form_count": sum(
                isinstance(line.get("form"), list) for line in posts
            ),
            "captured_post_failed_count": sum(
                line.get("failed") is True for line in posts
            ),
            "captured_post_body_omitted_count": sum(
                "body_omitted" in line for line in posts
            ),
            "captured_post_multipart_count": sum(
                line.get("request_type") == "multipart/form-data" for line in posts
            ),
            "captured_post_urlencoded_count": sum(
                line.get("request_type") == "application/x-www-form-urlencoded"
                for line in posts
            ),
            "captured_post_other_content_type_count": sum(
                line.get("request_type")
                not in (
                    None,
                    "multipart/form-data",
                    "application/x-www-form-urlencoded",
                )
                for line in posts
            ),
            "captured_post_elapsed_ms": [
                round((float(line["t"]) - (click_started - t0)) * 1000)
                for line in posts
                if isinstance(line.get("t"), (int, float))
            ],
            "page_pending_write_count": len(capture.pending_writes([page])),
            "server_post_count": len(server_posts),
            "server_post_elapsed_ms": [
                round((float(item["received_monotonic"]) - click_started) * 1000)
                for item in server_posts
                if isinstance(item.get("received_monotonic"), (int, float))
            ],
            "capture_stop_after_click_ms": round((stopped - click_started) * 1000),
        }
    except Exception:  # noqa: BLE001 - diagnostics must not replace the test result
        return {
            "schema": "openadapt.test-capture-diagnostics/1",
            "available": False,
        }


def _proposal_reason_codes(reasons: list[str]) -> list[str]:
    """Keep assertion diagnostics to fixed labels, never request-derived text."""
    return [
        "no_form_request"
        if reason == "no form request was sent by the save step"
        else "other"
        for reason in reasons
    ]


def _wait_for_captured_post(
    capture: NetworkCapture,
    page: Any,
    post_url: str,
    *,
    t0: float,
    click_started: float,
    timeout_s: float = CAPTURE_COMPLETION_TIMEOUT_S,
) -> bool:
    """Wait for this submit's terminal capture event, without parsing its body."""
    click_t = click_started - t0
    deadline = click_started + timeout_s
    while True:
        if any(
            line.get("method") == "POST"
            and line.get("url") == post_url
            and isinstance(line.get("t"), (int, float))
            # Capture event times are rounded to milliseconds in ``lines``.
            and float(line["t"]) >= click_t - 0.001
            for line in capture.lines(t0)
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def site_page(browser):
    site = Site()
    context = browser.new_context()
    page = context.new_page()
    page.goto(site.base + "/")
    yield site, page
    context.close()
    site.close()


def binding_from_browser(tmp_path: Path, site: Site, page: Any):
    """Capture the real normal submit, then propose and test-admit its binding."""
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "meta.json").write_text(
        json.dumps({"params": {"title": "Note Alpha"}})
    )
    clock = time.monotonic()
    capture = NetworkCapture(page.context).start()
    try:
        page.goto(site.base + "/compose")
        # Finish capturing the form before recording its first input event.
        # Playwright may return from goto before requestfinished is delivered.
        deadline = time.monotonic() + 3
        while not any(
            r.get("method") == "GET"
            and r.get("url") == site.base + "/compose"
            and r.get("status") == 200
            for r in capture.lines(clock)
        ):
            assert time.monotonic() < deadline, "the form capture did not finish"
            page.wait_for_timeout(10)
        page.wait_for_timeout(10)  # separate events from millisecond capture stamps
        events = [
            {
                "i": 0,
                "kind": "click",
                "t": time.monotonic() - clock,
                "structural": {
                    "selector": "#title",
                    "role": "textbox",
                    "name": "Title",
                },
            }
        ]
        page.click("#title")
        events.append(
            {
                "i": 1,
                "kind": "type",
                "text": "Note Alpha",
                "param": "title",
                "t": time.monotonic() - clock,
            }
        )
        page.fill("#title", "Note Alpha")
        events.append(
            {
                "i": 2,
                "kind": "click",
                "t": time.monotonic() - clock,
                "structural": {"selector": "#send", "role": "button", "name": "Send"},
            }
        )
        click_started = time.monotonic()
        page.click("#send")
        _wait_for_captured_post(
            capture,
            page,
            site.base + "/publish",
            t0=clock,
            click_started=click_started,
        )
    finally:
        stopped = time.monotonic()
        capture.stop()
    site.last_capture_diagnostics = _safe_capture_diagnostics(
        capture, site, page, clock, click_started, stopped
    )
    capture.write(recording, clock)
    (recording / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )
    workflow = Workflow(
        name="form-boundaries",
        params={"title": "Note Alpha"},
        steps=[
            Step(id="step_000", intent="Focus title", action=ActionKind.CLICK),
            Step(
                id="step_001",
                intent="Type title",
                action=ActionKind.TYPE,
                param="title",
            ),
            Step(
                id="step_002",
                intent="Send",
                action=ActionKind.CLICK,
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"title": "Note Alpha"},
                        expected_count=1,
                    )
                ],
            ),
        ],
    )
    proposed, step, reasons = propose_api_step(workflow, recording)
    assert step is not None, json.dumps(
        {
            "proposal_reason_codes": _proposal_reason_codes(reasons),
            "capture": site.last_capture_diagnostics,
        },
        sort_keys=True,
    )
    admitted, identifiers = admit_api_step(
        proposed, admitted_by="synthetic-shape-check"
    )
    assert identifiers == ["step_002"]
    result = admitted.steps[-1].api_binding
    assert result is not None and result.admitted()
    site.requests.clear()
    return result


def native_submit(site: Site, page: Any, title: str = "Note Beta") -> None:
    page.goto(site.base + "/compose")
    page.fill("#title", title)
    button = page.locator("#send")
    if button.is_enabled():
        button.click(timeout=3000)
    else:
        with pytest.raises(BrowserTimeout):
            button.click(timeout=300)
    page.wait_for_timeout(180)


@pytest.mark.parametrize("target", ["relative", "absolute", "protocol_relative"])
def test_an_unbound_transport_destination_is_not_resolved(target, site_page):
    site, page = site_page
    endpoint = {
        "relative": "publish",
        "absolute": site.base + "/publish",
        "protocol_relative": "//publish",
    }[target]
    result = PlaywrightSessionTransport(page).evaluate_form(
        "/compose", endpoint, [("title", "Note Beta")], submitter="#send"
    )
    assert result.status == "unevaluable", result.reason
    assert not site.writes()


@pytest.mark.parametrize("value", ["7", "68", "907", "", "long-synthetic-value"])
def test_declared_multipart_secret_is_scrubbed_before_persistence(
    value, tmp_path, site_page
):
    site, page = site_page
    site.html = FORM.replace(
        'method="post"', 'method="post" enctype="multipart/form-data"'
    ).replace("<button", '<input name="access_code" id="code" type="password"><button')
    capture = NetworkCapture(page.context, secret_names=["access_code"]).start()
    try:
        page.goto(site.base + "/compose")
        page.fill("#title", "Note Alpha")
        page.fill("#code", value)
        page.click("#send")
        page.wait_for_timeout(180)
    finally:
        capture.stop()
    capture.write(tmp_path, 0)
    posts = [r for r in load(tmp_path) if r["method"] == "POST"]
    assert len(posts) == 1 and len(site.writes()) == 1
    assert ["access_code", SECRET if value else ""] in posts[0]["form"]
    if value:
        assert ["access_code", value] not in posts[0]["form"]
        assert (
            json.dumps(["access_code", value])
            not in (tmp_path / NETWORK_FILE).read_text()
        )


@pytest.mark.parametrize(
    "mode,allowed",
    [
        ("normal", True),
        ("nested_disabled", False),
        ("first_legend", True),
        ("required_file", False),
        ("optional_file", True),
        ("action", False),
        ("query", False),
        ("method", False),
        ("encoding", False),
        ("same_action", True),
    ],
)
def test_live_native_submit_and_api_agree(mode, allowed, tmp_path, site_page):
    site, page = site_page
    if mode in ("required_file", "optional_file"):
        site.html = FORM.replace(
            'method="post"', 'method="post" enctype="multipart/form-data"'
        ).replace("<button", '<input type="file" name="bundle" id="bundle"><button')
    binding = binding_from_browser(tmp_path, site, page)
    original = site.html
    if mode == "nested_disabled":
        site.html = original.replace(
            "<button", "<fieldset disabled><div><fieldset><button"
        ).replace("</button>", "</button></fieldset></div></fieldset>")
    elif mode == "first_legend":
        site.html = original.replace(
            "<button", "<fieldset disabled><legend><button"
        ).replace("</button>", "</button></legend></fieldset>")
    elif mode == "required_file":
        site.html = original.replace('id="bundle"', 'id="bundle" required')
    else:
        override = {
            "action": 'formaction="/alternate"',
            "query": 'formaction="/publish?route=other"',
            "method": 'formmethod="get"',
            "encoding": 'formenctype="text/plain"',
            "same_action": 'formaction="/publish"',
        }.get(mode, "")
        site.html = original.replace('id="send"', 'id="send" ' + override)
    native_submit(site, page)
    reference = site.writes()
    if mode in ("nested_disabled", "required_file", "method"):
        assert not reference
    else:
        assert len(reference) == 1
    if mode == "action":
        assert reference[0]["path"] == "/alternate"
    if mode == "query":
        assert reference[0]["path"] == "/publish?route=other"
    if mode == "encoding":
        assert reference[0]["content_type"].startswith("text/plain")
    site.requests.clear()
    page.goto(site.base + "/")
    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        binding, {"title": "Note Beta"}
    )
    print(
        json.dumps(
            {
                "case": mode,
                "browser": reference,
                "api": site.writes(),
                "status": result.status.value,
            }
        )
    )
    if allowed:
        assert result.status is ActuationStatus.ACTUATED, result.reason
        assert (
            len(site.writes()) == 1 and site.writes()[0]["path"] == reference[0]["path"]
        )
        actual = site.writes()[0]
        if reference[0]["content_type"].startswith("multipart/form-data"):
            old = re.search(r"boundary=([^; ]+)", reference[0]["content_type"])
            new = re.search(r"boundary=([^; ]+)", actual["content_type"])
            assert old and new
            assert reference[0]["body"].replace(old.group(1), "BOUNDARY") == actual[
                "body"
            ].replace(new.group(1), "BOUNDARY")
        else:
            assert up.parse_qsl(actual["body"], keep_blank_values=True) == up.parse_qsl(
                reference[0]["body"], keep_blank_values=True
            )
    else:
        assert result.status is ActuationStatus.UNAVAILABLE, result.reason
        assert not site.writes()


def test_capture_waits_for_a_delayed_native_submit(tmp_path, site_page):
    site, page = site_page
    site.html = FORM.replace(
        "</form>",
        """</form><script>
          (() => {
            let deferred = false;
            const form = document.getElementById('entry');
            form.addEventListener('submit', event => {
              if (deferred) return;
              event.preventDefault();
              deferred = true;
              const submitter = event.submitter;
              setTimeout(() => form.requestSubmit(submitter), 350);
            });
          })();
        </script>""",
    )

    binding = binding_from_browser(tmp_path, site, page)

    diagnostics = site.last_capture_diagnostics
    assert binding.admitted()
    assert diagnostics is not None and diagnostics["available"] is True
    assert diagnostics["captured_post_count"] == 1
    assert diagnostics["captured_post_with_form_count"] == 1
    assert diagnostics["captured_post_failed_count"] == 0
    assert diagnostics["server_post_count"] == 1
    assert len(diagnostics["captured_post_elapsed_ms"]) == 1
    assert diagnostics["captured_post_elapsed_ms"][0] >= 250
    assert diagnostics["page_pending_write_count"] == 0


@pytest.mark.parametrize(
    "channel,remote", [("fetch", False), ("fetch", True), ("submit", True)]
)
def test_actual_script_destination_is_bound(channel, remote, tmp_path, site_page):
    site, page = site_page
    other = Site()
    try:
        destination = json.dumps(other.base + "/publish")
        send = (
            "fetch(target, {method: 'POST', body: new URLSearchParams(new FormData(f))});"
            if channel == "fetch"
            else "f.action = target; f.submit();"
        )
        site.html = FORM.replace(
            "</body>",
            """<script>
          document.getElementById('send').addEventListener('click', e => {
            e.preventDefault(); const f = document.getElementById('entry');
            const target = f.elements.title.value === 'Across' ? DESTINATION : '/publish';
            SEND
          });
        </script></body>""".replace("DESTINATION", destination).replace("SEND", send),
        )
        binding = binding_from_browser(tmp_path, site, page)
        title = "Across" if remote else "Note Beta"
        native_submit(site, page, title)
        if remote:
            assert len(other.writes()) == 1 and not site.writes()
        else:
            assert len(site.writes()) == 1 and not other.writes()
        site.requests.clear()
        other.requests.clear()
        page.goto(site.base + "/")
        result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
            binding, {"title": title}
        )
        if remote:
            assert result.status is ActuationStatus.UNAVAILABLE, result.reason
            assert not site.writes() and not other.writes()
        else:
            assert result.status is ActuationStatus.ACTUATED, result.reason
            assert len(site.writes()) == 1 and not other.writes()
    finally:
        other.close()
