"""The read-only browser teach lane's guards, in a real headless Chromium.

The full teach -> tool -> file loop is tests/e2e/test_browser_teach_e2e.py.
These check the pieces that keep a compiled run from doing the wrong thing:
it stops on look-alike or relabelled controls, the browser sends no write
request and stays on one site, and teach_finish refuses what it can't
compile honestly.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("playwright")

from openadapt_flow.agent import browser_teach as bt  # noqa: E402
from openadapt_flow.agent.bridge import BridgeError  # noqa: E402
from openadapt_flow.agent.cli import main as agent_main  # noqa: E402
from openadapt_flow.agent.example_app import serve  # noqa: E402


@pytest.fixture(scope="module")
def portal() -> Iterator[str]:
    base, stop = serve(0)
    yield base
    stop()


@pytest.fixture
def browser(portal: str, tmp_path: Path) -> Iterator[bt._Browser]:
    thread = bt._BrowserThread()
    box: dict = {}
    thread.call(lambda: box.update(b=bt._Browser(bt._origin(portal), tmp_path, True)))
    browser = box["b"]
    browser.thread = thread  # type: ignore[attr-defined]
    yield browser
    thread.call(browser.close)
    thread.stop()


def _on(browser: bt._Browser, fn):
    return browser.thread.call(fn)  # type: ignore[attr-defined]


def _writes(base: str) -> int:
    with urllib.request.urlopen(f"{base}/_stats", timeout=5) as response:
        return json.loads(response.read())["writes"]


def test_two_lookalike_buttons_stop_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content("<button>Export</button><button>Export</button>")
        target = {"role": "button", "name": "Export", "label": "Export"}
        with pytest.raises(bt.StepStopped, match="2 controls match 'Export'"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_relabelled_control_stops_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content('<button id="go">Export all</button>')
        target = {"css": 'button[id="go"]', "label": "Export"}
        with pytest.raises(bt.StepStopped, match="now labelled 'Export all'"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_missing_control_stops_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        target = {"role": "link", "name": "Export CSV", "label": "Export CSV"}
        with pytest.raises(bt.StepStopped, match="not on the page"):
            bt._resolve(page, target, 300)

    _on(browser, check)


def test_links_to_one_place_are_one_target(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        raw = page.evaluate(bt._OBSERVE_JS)
        _, controls = bt._render(raw)
        link = next(c for c in controls if c["label"] == "Sales report")
        target = bt._unique_target(page, link)
        assert target is not None and target["same_href"] == "/reports"
        assert bt._resolve(page, target, 500) is not None
        page.set_content('<a href="/a">Sales report</a><a href="/b">Sales report</a>')
        with pytest.raises(bt.StepStopped, match="go to different places"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_browser_sends_no_write_request(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/reports")
        sent = page.evaluate(
            "() => fetch('/reports/email', {method: 'POST', body: 'x'})"
            ".then(() => 'sent', () => 'failed')"
        )
        assert sent == "failed"
        assert browser.blocked == ["POST /reports/email, a write this run doesn't send"]

    _on(browser, check)
    assert _writes(portal) == 0


def test_browser_stays_on_the_taught_site(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content('<a href="https://example.com/">Elsewhere</a>')
        page.get_by_role("link", name="Elsewhere").click()
        browser.settle()
        assert browser.blocked == [
            "leaving " + bt._origin(portal) + " for https://example.com"
        ]

    _on(browser, check)


def _ref(page: str, label: str) -> str:
    found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(label) + '"', page)
    assert found, page
    return found.group(1)


def test_finish_refuses_without_a_download_or_with_an_unused_input(
    portal: str, tmp_path: Path
) -> None:
    bridge = bt.BrowserTeachBridge(tmp_path / "tools", tmp_path / "files")
    started = bridge.dispatch(
        "teach_start",
        {
            "name": "report",
            "url": f"{portal}/reports",
            "task": "Export the sales report",
            "inputs": {"start_date": "2026-02-01", "end_date": "2026-02-10"},
        },
    )
    page = started["page"]
    with pytest.raises(BridgeError, match="no file has downloaded"):
        bridge.dispatch("teach_finish", {})
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "From date"), "input": "start_date"},
    )["page"]
    # Typed as text, but it is the input's value: it becomes the input.
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "To date"), "text": "2026-02-10"},
    )["page"]
    page = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Show report")}
    )["page"]
    done = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Export CSV")}
    )
    assert done["rows"] > 0
    with pytest.raises(BridgeError, match="no column 'when'"):
        bridge.dispatch(
            "teach_finish",
            {
                "checks": {
                    "between": [
                        {"column": "when", "from": "start_date", "to": "end_date"}
                    ]
                }
            },
        )
    finished = bridge.dispatch("teach_finish", {})
    assert finished["status"] == "ready"
    steps = json.loads(Path(finished["program"]).read_text())["steps"]
    assert [s["value"] for s in steps if s["op"] == "fill"] == [
        {"input": "start_date"},
        {"input": "end_date"},
    ]

    bridge2 = bt.BrowserTeachBridge(tmp_path / "tools2", tmp_path / "files")
    bridge2.dispatch(
        "teach_start",
        {
            "name": "report",
            "url": f"{portal}/reports",
            "task": "Export the sales report",
            "inputs": {"start_date": "2026-02-01", "region": "West"},
        },
    )
    page = bridge2.dispatch("teach_observe", {})["page"]
    for args in (
        {"action": "type", "ref": _ref(page, "From date"), "input": "start_date"},
        {"action": "type", "ref": _ref(page, "To date"), "text": "2026-02-03"},
        {"action": "click", "ref": _ref(page, "Show report")},
    ):
        page = bridge2.dispatch("teach_act", args)["page"]
    bridge2.dispatch("teach_act", {"action": "click", "ref": _ref(page, "Export CSV")})
    with pytest.raises(BridgeError, match="input 'region' was never typed"):
        bridge2.dispatch("teach_finish", {})
    bridge2.dispatch("teach_abort", {})


def test_check_file_reports_rows_with_another_value(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("id,region\n1,West\n2,East\n")
    program = {
        "result": {"suffix": ".csv", "columns": ["id", "region"]},
        "checks": {"equals": [{"column": "region", "input": "r"}]},
    }
    _, failed, _ = bt._check_file(path, program, {"r": "West"})
    assert failed == ["1 rows have region other than 'West' (first: 'East')"]


def test_check_file_reports_rows_outside_the_range(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("id,date\n1,2026-01-05\n2,2026-02-01\n")
    program = {
        "result": {"suffix": ".csv", "columns": ["id", "date"]},
        "checks": {"between": [{"column": "date", "from": "a", "to": "b"}]},
    }
    passed, failed, facts = bt._check_file(
        path, program, {"a": "2026-01-01", "b": "2026-01-31"}
    )
    assert passed == ["its columns match the taught file"]
    assert failed == [
        "1 rows have date outside 2026-01-01 to 2026-01-31 (first: '2026-02-01')"
    ]
    assert facts["rows"] == 2


def test_write_labels_and_post_forms_are_refused() -> None:
    bridge = bt.BrowserTeachBridge.__new__(bt.BrowserTeachBridge)
    for label in ("Save", "Delete report", "Send email", "Pay now"):
        with pytest.raises(BridgeError, match="changes data"):
            bridge._refuse_write({"label": label, "role": "button"}, "clicking")
    with pytest.raises(BridgeError, match="POST"):
        bridge._refuse_write(
            {"label": "Go", "role": "button", "method": "POST"}, "clicking"
        )
    bridge._refuse_write({"label": "Export CSV", "role": "link"}, "clicking")
    bridge._refuse_write(
        {"label": "Show report", "role": "button", "method": "GET"}, "clicking"
    )


def test_bad_programs_on_disk_are_skipped(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / bt.PROGRAM_FILE).write_text("{not json")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / bt.PROGRAM_FILE).write_text(json.dumps({"schema": "x"}))
    bridge = bt.BrowserTeachBridge(tmp_path, tmp_path / "files")
    assert bridge.programs == {}
    assert capsys.readouterr().err.count("skipped") == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--teach-browser", "--url", "http://127.0.0.1:1"],
        ["serve", "--teach-browser", "--bundles", "x"],
        ["serve", "--tools-dir", "x"],
    ],
)
def test_serve_teach_browser_refuses_other_modes(argv: list[str], capsys) -> None:
    assert agent_main(argv) == 2
    assert "--teach-browser" in capsys.readouterr().err


def test_controls_in_a_named_frame_are_listed_and_found(
    browser: bt._Browser, portal: str
) -> None:
    """OpenEMR shows its forms in named iframes. A control there is listed
    with its frame, found again through it, and clicked; a frame with no name
    or id can't be found again, so its controls aren't offered."""

    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content(
            '<iframe name="pat" srcdoc="<button onclick=\'window.parent.hit=1\'>'
            'Inner Save</button>"></iframe>'
            '<iframe srcdoc="<button>Hidden twin</button>"></iframe>'
        )
        page.wait_for_function("window.frames.length === 2")
        page.frames[1].wait_for_selector("button")
        raw = bt._observe_page(page, bt._origin(portal))
        text, controls = bt._render(raw)
        assert "(a frame with no name is not listed)" in text
        inner = next(c for c in controls if c["label"] == "Inner Save")
        assert inner["frames"] == ['iframe[name="pat"]']
        assert not any(c["label"] == "Hidden twin" for c in controls)
        target = bt._unique_target(page, inner)
        assert target is not None and target["frames"] == ['iframe[name="pat"]']
        bt._resolve(page, target, 1000).click()
        assert page.evaluate("window.hit") == 1

    _on(browser, check)
