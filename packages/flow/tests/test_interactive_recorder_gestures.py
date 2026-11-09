"""Interactive recorder: gesture coverage (double-click, select, checkbox,
contenteditable).

A double-click gesture compiles to ONE ``DOUBLE_CLICK`` step: the in-page
listener marks the second click (``e.detail === 2``) and Python absorbs the
held first click, so replay delivers exactly the two demonstrated clicks. A
native ``<select>`` selection is recorded as the option's label typed into the
select, which replay types and reads back; the compiled steps replay a
different option. No Enter is recorded for it: Enter on a select inside a form
submits the form. A change the page's own script dispatches on a
hidden select (a TomSelect-style widget) is not recorded: the clicks on the
visible widget are. A checkbox records only its click. ``contenteditable``
text entry records the typed text.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import record_interactive

pytestmark = pytest.mark.timeout(600)

PAGE_HTML = """<!doctype html><title>gesture fixture</title>
<body style="margin:0;background:#fff">
<button id="dbl" ondblclick="this.textContent='Opened Row'"
  style="position:absolute;left:20px;top:20px;width:150px;height:30px">
Patient Row</button>
<button id="single" style="position:absolute;left:20px;top:70px;width:150px;
height:30px">Plain Button</button>
<input type="checkbox" id="chk" style="position:absolute;left:20px;
top:120px;width:20px;height:20px">
<div id="ce" contenteditable="true" style="position:absolute;left:20px;
top:160px;width:300px;height:40px;border:1px solid #999"></div>
<label for="sel" style="position:absolute;left:20px;top:220px">Species</label>
<select id="sel" name="sel" style="position:absolute;left:20px;top:240px;
width:200px">
<option value="">Choose</option>
<option value="v-alpha">Label Alpha</option>
<option value="v-beta">Label Beta</option>
</select>
<select id="dup" name="dup" style="position:absolute;left:260px;top:240px;
width:200px">
<option value="">Choose</option>
<option value="p-1">Main clinic</option>
<option value="p-2">main clinic</option>
</select>
<select id="site" name="site" tabindex="-1" aria-hidden="true"
  class="tomselected ts-hidden-accessible"
  style="position:absolute;width:1px;height:1px;overflow:hidden;
  clip:rect(0 0 0 0);left:0;top:0">
<option value="">-</option>
<option value="1">Site One</option>
<option value="2">Site Two</option>
</select>
<div class="ts-wrapper" style="position:absolute;left:20px;top:290px;width:200px">
<div id="site-control" class="ts-control" role="combobox" tabindex="0"
  style="border:1px solid #999;height:24px">Choose a site</div>
<div id="site-dropdown" class="ts-dropdown" role="listbox" style="display:none">
<div class="option" role="option" data-value="1" style="height:24px">Site One</div>
<div class="option" role="option" data-value="2" style="height:24px">Site Two</div>
</div></div>
<script>
// The shape of a TomSelect widget: a hidden native select, a visible control,
// and a script that updates the select and dispatches change on it.
const siteSelect = document.getElementById('site');
const siteControl = document.getElementById('site-control');
const siteDropdown = document.getElementById('site-dropdown');
siteControl.addEventListener('click', () => { siteDropdown.style.display = 'block'; });
for (const opt of siteDropdown.querySelectorAll('.option')) {
  opt.addEventListener('click', () => {
    siteSelect.value = opt.dataset.value;
    siteControl.textContent = opt.textContent;
    siteDropdown.style.display = 'none';
    siteSelect.dispatchEvent(new Event('change', {bubbles: true}));
  });
}
</script>
</body>"""


def _serve() -> tuple[str, Callable[[], None]]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return url, server.shutdown


@pytest.fixture(scope="module")
def fixture_url() -> Iterator[str]:
    url, stop = _serve()
    yield f"{url}/page.html"
    stop()


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _settle(pump, count: int = 12) -> None:
    # 12 pumps x 60 ms poll outlives the 500 ms double-click hold window.
    for _ in range(count):
        pump()


def test_double_click_compiles_to_one_double_click_step(
    fixture_url: str, tmp_path: Path
) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#dbl", state="visible", timeout=20000)
        page.locator("#single").click()
        _settle(pump)
        page.locator("#dbl").dblclick()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    events = _events(rec)
    assert [e["kind"] for e in events] == ["click", "double_click"]
    assert events[1]["structural"]["selector"] == "#dbl"

    workflow = compile_recording(rec, tmp_path / "bundle", name="gestures")
    assert [step.action.value for step in workflow.steps] == [
        "click",
        "double_click",
    ]


def test_single_click_survives_the_double_click_hold(
    fixture_url: str, tmp_path: Path
) -> None:
    """A click followed only by idle pumps flushes as a single click."""

    def drive(page, pump) -> None:
        page.wait_for_selector("#single", state="visible", timeout=20000)
        page.locator("#single").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    assert [e["kind"] for e in _events(rec)] == ["click"]


def test_checkbox_records_click_without_phantom_type(
    fixture_url: str, tmp_path: Path
) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#chk", state="visible", timeout=20000)
        page.locator("#chk").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    assert [e["kind"] for e in _events(rec)] == ["click"]


def test_contenteditable_records_typed_text(fixture_url: str, tmp_path: Path) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#ce", state="visible", timeout=20000)
        page.locator("#ce").click()
        _settle(pump)
        page.keyboard.type("hello notes")
        _settle(pump)
        page.locator("#single").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    kinds = [e["kind"] for e in _events(rec)]
    assert kinds == ["click", "type", "click"]
    type_event = _events(rec)[1]
    assert type_event["text"] == "hello notes"


def _record_native_select(
    fixture_url: str, out: Path, param_fields: tuple[str, ...] = ()
) -> Path:
    def drive(page, pump) -> None:
        page.wait_for_selector("#sel", state="visible", timeout=20000)
        kept = pump.__self__.events_kept
        page.locator("#sel").click()
        _settle(pump)
        # On a slow runner the click may not be kept yet: pump until it is,
        # so the type-ahead changes never share its batch (which the recorder
        # refuses, by design).
        for _ in range(200):
            if pump.__self__.events_kept > kept:
                break
            pump()
        assert pump.__self__.events_kept > kept, "the select click was never kept"
        # Real key events, so the browser's own change event is trusted, as
        # a person's selection is, at a person's pace: a change that arrives
        # before the click's frame is kept is refused, by design.
        page.wait_for_timeout(300)
        kept = pump.__self__.events_kept
        page.keyboard.type("Label Beta", delay=120)
        # Linux Chromium opens a popup on the click; type-ahead only
        # highlights an option there, and the change fires when the popup
        # closes. Closed by the next click, the change and that click share
        # one batch, which the recorder refuses, by design. Enter commits the
        # choice first (and is no step: the change records it). Elsewhere the
        # change has already fired and Enter does nothing (no form here).
        page.keyboard.press("Enter")
        _settle(pump)
        # Pump until a change is kept and none has arrived for a settle
        # window, as a person pauses before the next click.
        quiet = 0
        for _ in range(400):
            before = pump.__self__.events_kept
            pump()
            after = pump.__self__.events_kept
            quiet = quiet + 1 if after == before and after > kept else 0
            if quiet >= 12:
                break
        assert pump.__self__.events_kept > kept, "the select change was never kept"
        page.locator("#single").click()
        _settle(pump)

    return record_interactive(
        fixture_url, out, headless=True, script=drive, param_fields=param_fields
    )


def test_native_select_records_its_label(fixture_url: str, tmp_path: Path) -> None:
    rec = _record_native_select(fixture_url, tmp_path / "rec")
    events = _events(rec)
    assert [e["kind"] for e in events] == ["click", "type", "click"]
    assert events[1]["text"] == "Label Beta"

    workflow = compile_recording(rec, tmp_path / "bundle", name="select")
    assert [step.action.value for step in workflow.steps] == ["click", "type", "click"]


def _replay_native_select(
    fixture_url: str, tmp_path: Path, value: str
) -> tuple[Any, Optional[str], int]:
    """Replay the recorded select with ``value``: the report, the select's
    value once the run ends, and how many key events reached the select."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    sync_api = pytest.importorskip("playwright.sync_api")
    rec = _record_native_select(fixture_url, tmp_path / "rec", ("sel",))
    bundle = tmp_path / "bundle"
    workflow = compile_recording(rec, bundle, name="select")
    typed = next(step for step in workflow.steps if step.action.value == "type")
    assert typed.param == "sel"
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.add_init_script(
                "window.__selKeys = 0; document.addEventListener('keydown',"
                " (e) => { if (e.target.id === 'sel') window.__selKeys += 1; },"
                " true);"
            )
            page.goto(fixture_url)
            page.wait_for_selector("#sel", state="visible")
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle),
                params={"sel": value},
                bundle_dir=bundle,
                run_dir=tmp_path / "run",
            )
            chosen = page.evaluate("document.getElementById('sel').value")
            keys = page.evaluate("window.__selKeys")
        finally:
            browser.close()
    return report, chosen, keys


def test_native_select_replay_chooses_the_labelled_option(
    fixture_url: str, tmp_path: Path
) -> None:
    # Recorded as Label Beta; replayed with another option. The option is
    # chosen by its exact label (select_option), with no keystrokes, so a
    # popup the click opened (Linux Chromium) commits nothing stale when the
    # next click closes it.
    report, chosen, keys = _replay_native_select(fixture_url, tmp_path, "Label Alpha")
    assert report.success, [(r.step_id, r.ok, r.error) for r in report.results]
    assert chosen == "v-alpha"
    assert keys == 0


def test_native_select_replay_refuses_a_missing_label(
    fixture_url: str, tmp_path: Path
) -> None:
    # "Label" names no option exactly (it only prefixes two). Replay refuses
    # the step instead of typing, so type-ahead never lands on a near match,
    # and the run stops before the next click.
    report, chosen, keys = _replay_native_select(fixture_url, tmp_path, "Label")
    assert not report.success
    failed = [r for r in report.results if not r.ok]
    assert len(failed) == 1 and failed[0].safety_halt
    assert "labelled" in (failed[0].error or "")
    assert report.results[-1] is failed[0]
    assert chosen == ""
    assert keys == 0


def _choose_in_select(options_html: str, label: str) -> tuple[Any, str, int, list[str]]:
    """Focus ``#s`` built from ``options_html`` and ask the backend to choose
    ``label``: (the chooser's result or the exception it raised, the select's
    value afterwards, how many change events fired, each option's label as
    the recorder stores it)."""

    from openadapt_flow.backends.playwright_backend import (
        NATIVE_OPTION_LABEL_JS,
        PlaywrightBackend,
    )

    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                '<select id="s"><option value="">Choose</option>'
                + options_html
                + "</select><script>window.__changes = 0;"
                "document.getElementById('s').addEventListener('change',"
                " () => { window.__changes += 1; });</script>"
            )
            stored = page.evaluate(
                "Array.from(document.getElementById('s').options).slice(1)"
                f".map({NATIVE_OPTION_LABEL_JS})"
            )
            page.focus("#s")
            outcome: Any
            try:
                outcome = PlaywrightBackend(page).select_focused_native_option(label)
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                outcome = exc
            value = page.evaluate("document.getElementById('s').value")
            changes = page.evaluate("window.__changes")
        finally:
            browser.close()
    return outcome, value, changes, stored


def _assert_refused(outcome: Any, value: str, changes: int) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    assert isinstance(outcome, StructuralResolutionRefused), outcome
    assert value == ""
    assert changes == 0


_EXACT = '<option value="v-alpha">Label Alpha</option>'


@pytest.mark.parametrize(
    "decoy",
    [
        '<option value="v-shadow">Label  Alpha</option>',
        '<option value="v-shadow" label="Label  Alpha">x</option>',
    ],
    ids=["text", "attr"],
)
def test_native_select_choice_refuses_a_whitespace_decoy(decoy: str) -> None:
    # Recording and replay squash whitespace alike, so "Label  Alpha" and
    # "Label Alpha" are one label: two matches, refused (the recorder refuses
    # such a select too). Never a wrong-option success.
    outcome, value, changes, stored = _choose_in_select(decoy + _EXACT, "Label Alpha")
    assert stored == ["Label Alpha", "Label Alpha"]
    _assert_refused(outcome, value, changes)


def test_native_select_choice_refuses_duplicates() -> None:
    outcome, value, changes, _stored = _choose_in_select(
        '<option value="v-1">Label Alpha</option>'
        '<option value="v-2">Label Alpha</option>',
        "Label Alpha",
    )
    _assert_refused(outcome, value, changes)


def test_native_select_choice_refuses_a_disabled_match() -> None:
    outcome, value, changes, _stored = _choose_in_select(
        '<option value="v-off" disabled>Label Alpha</option>', "Label Alpha"
    )
    _assert_refused(outcome, value, changes)


def test_native_select_choice_selects_a_multiline_option() -> None:
    outcome, value, changes, stored = _choose_in_select(
        '<option value="v-dog">\n    Dog\n  </option>'
        '<option value="v-cat">Cat</option>',
        "Dog",
    )
    assert stored == ["Dog", "Cat"]
    assert outcome is True
    assert value == "v-dog"
    assert changes == 1


# One case per whitespace class, as the recorder stores it (JavaScript \s,
# squashed and trimmed) and replay therefore matches it.
@pytest.mark.parametrize(
    ("option_html", "stored_label"),
    [
        ('<option value="v">Label  Alpha</option>', "Label Alpha"),
        ('<option value="v">Label\tAlpha</option>', "Label Alpha"),
        ('<option value="v">Label\nAlpha</option>', "Label Alpha"),
        ('<option value="v"> Label Alpha </option>', "Label Alpha"),
        ('<option value="v">Label&nbsp;Alpha</option>', "Label Alpha"),
        ('<option value="v">Label&#x2003;Alpha</option>', "Label Alpha"),
        ('<option value="v" label=" Label  Alpha ">x</option>', "Label Alpha"),
        # Zero-width space is not \s: it stays, so "Label Alpha" is no match.
        ('<option value="v">Label&#x200b; Alpha</option>', "Label\u200b Alpha"),
        # Case is kept: the recorder stores it as shown.
        ('<option value="v">label alpha</option>', "label alpha"),
    ],
    ids=[
        "double-space",
        "tab",
        "newline",
        "leading-trailing",
        "nbsp",
        "em-space",
        "label-attr",
        "zero-width-space",
        "case",
    ],
)
def test_native_select_choice_follows_the_recorders_whitespace(
    option_html: str, stored_label: str
) -> None:
    outcome, value, changes, stored = _choose_in_select(option_html, "Label Alpha")
    assert stored == [stored_label]
    if stored_label == "Label Alpha":
        assert outcome is True
        assert value == "v"
        assert changes == 1
    else:
        _assert_refused(outcome, value, changes)


def test_native_select_with_a_shared_label_is_refused(
    fixture_url: str, tmp_path: Path
) -> None:
    # Two options read "Main clinic" (case aside): replay can only choose by
    # label, so it would pick the first. The choice is refused, not recorded.
    def drive(page, pump) -> None:
        page.wait_for_selector("#dup", state="visible", timeout=20000)
        page.locator("#dup").click()
        _settle(pump)
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        _settle(pump)

    try:
        rec = record_interactive(
            fixture_url, tmp_path / "rec", headless=True, script=drive
        )
    except Exception as exc:  # the recorder may stop on the refusal
        assert "select" in str(exc).lower()
        return
    assert not [e for e in _events(rec) if e["kind"] == "type"]


def test_widget_over_a_hidden_select_records_its_clicks(
    fixture_url: str, tmp_path: Path
) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#site-control", state="visible", timeout=20000)
        page.locator("#site-control").click()
        _settle(pump)
        page.get_by_role("option", name="Site Two").click()
        _settle(pump)
        assert page.evaluate("document.getElementById('site').value") == "2"

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    events = _events(rec)
    assert [e["kind"] for e in events] == ["click", "click"]
    assert events[1]["structural"]["name"] == "Site Two"


def test_script_set_select_value_is_not_recorded(
    fixture_url: str, tmp_path: Path
) -> None:
    """Playwright's select_option sets the value from script, as a widget
    does: nothing is recorded for it, and the recording is not refused."""

    def drive(page, pump) -> None:
        page.wait_for_selector("#sel", state="visible", timeout=20000)
        page.locator("#sel").select_option(label="Label Alpha")
        _settle(pump)
        page.locator("#single").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    assert [e["kind"] for e in _events(rec)] == ["click"]
