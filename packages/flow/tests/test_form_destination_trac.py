"""Opt-in real Trac form submission with an isolated synthetic environment.

Install Trac 1.6, then set OPENADAPT_TRAC_FORM_TEST=1. The test creates its
own environment and retains its database and observations under tmp_path.
It uses anonymous ticket permissions; no sign-in credentials are needed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest

from openadapt_flow.backends.playwright_backend import (
    PlaywrightBackend,
    _FORM_EXPECTED_JS,
    prepare_ws_guard,
)
from openadapt_flow.wire_check import body_fields, expected_submission

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OPENADAPT_TRAC_FORM_TEST") != "1",
        reason="requires an isolated Trac installation and fixture lease",
    ),
    pytest.mark.timeout(90),
]


def _tickets(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id, summary, description, status FROM ticket ORDER BY id"
            )
        ]


@pytest.mark.parametrize("drift", [False, True], ids=["native", "changed-address"])
def test_trac_action_radio_does_not_replace_the_checked_destination(
    tmp_path: Path, drift: bool
) -> None:
    from playwright.sync_api import Error, sync_playwright
    from trac.env import Environment
    from trac.perm import PermissionSystem

    environment = tmp_path / "trac"
    env = Environment(
        str(environment), create=True, options=[("project", "name", "Synthetic Desk")]
    )
    permissions = PermissionSystem(env)
    for permission in ("TICKET_CREATE", "TICKET_VIEW"):
        if permission not in permissions.get_user_permissions("anonymous"):
            permissions.grant_permission("anonymous", permission)
    env.shutdown()
    database = environment / "db" / "trac.db"
    before = _tickets(database)
    port = int(os.environ.get("OPENADAPT_TRAC_FORM_PORT", "9686"))
    base = f"http://127.0.0.1:{port}"
    observations: dict[str, Any] = {"before": before, "requests": [], "responses": []}
    with (tmp_path / "tracd.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from trac.web.standalone import main; main()",
                "--single-env",
                "--hostname=127.0.0.1",
                f"--port={port}",
                str(environment),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                assert process.poll() is None, "isolated tracd exited"
                try:
                    with urlopen(base + "/newticket", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("isolated tracd did not start")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(service_workers="block")
                    prepare_ws_guard(page.context)

                    def request_seen(request: Any) -> None:
                        fields = body_fields(request)
                        observations["requests"].append(
                            {
                                "url": request.url,
                                "method": request.method,
                                "redirected_from": (
                                    request.redirected_from.url
                                    if request.redirected_from is not None
                                    else None
                                ),
                                "field_names": sorted(k for k, _ in fields or []),
                            }
                        )

                    page.on("request", request_seen)
                    page.on(
                        "response",
                        lambda response: observations["responses"].append(
                            {
                                "url": response.url,
                                "status": response.status,
                                "location": response.header_value("location"),
                            }
                        ),
                    )
                    page.goto(base + "/newticket")
                    summary = "Synthetic scanner form destination check"
                    description = "Created only in this isolated test environment."
                    page.locator("#field-summary").fill(summary)
                    page.locator("#field-description").fill(description)
                    # Let Trac's live preview finish before holding the save.
                    page.wait_for_timeout(3000)
                    control = page.locator('input[name="submit"]')
                    control.scroll_into_view_if_needed()
                    box = control.bounding_box()
                    assert box is not None
                    args = {
                        "x": box["x"] + box["width"] / 2,
                        "y": box["y"] + box["height"] / 2,
                        "exempt": [],
                    }
                    native = control.evaluate(
                        "el => Object.getOwnPropertyDescriptor("
                        "HTMLFormElement.prototype, 'action').get.call(el.form)"
                    )
                    raw = page.evaluate(_FORM_EXPECTED_JS, args)
                    assert raw is not None
                    observations["checked"] = {
                        "address": raw["address"],
                        "method": raw["method"],
                        "field_names": sorted(k for k, _ in raw["pairs"]),
                        "native_action": native,
                    }
                    held_start = len(observations["requests"])
                    backend = PlaywrightBackend(page)
                    assert backend._arm_wire_check(expected_submission(raw, ())) is None
                    if drift:
                        page.evaluate(
                            "document.getElementById('propertyform').addEventListener("
                            "'submit', e => e.target.setAttribute('action', '/another-ticket'),"
                            " true)"
                        )
                    try:
                        control.click(timeout=5000)
                    except Error as error:
                        observations["click_error"] = str(error)
                    page.wait_for_timeout(500)
                    refusal = backend.form_submit_guard_refusal(disarm=True)
                    observations["refusal"] = refusal
                    held_requests = observations["requests"][held_start:]
                    observations["held_requests"] = held_requests
                    after = _tickets(database)
                    observations["after"] = after
                    if drift:
                        assert refusal is not None and "different address" in refusal
                        assert after == before
                    else:
                        assert refusal is None, refusal
                        assert raw["address"] == native.split("#", 1)[0]
                        assert [
                            (request["method"], request["url"])
                            for request in held_requests
                            if request["method"] == "POST"
                        ] == [("POST", base + "/newticket")]
                        assert after == [
                            {
                                "id": 1,
                                "summary": summary,
                                "description": description,
                                "status": "new",
                            }
                        ]
                finally:
                    browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
            (tmp_path / "observations.json").write_text(
                json.dumps(observations, indent=2) + "\n"
            )
