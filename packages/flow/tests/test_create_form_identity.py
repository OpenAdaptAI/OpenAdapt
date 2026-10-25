"""A create-new form's save is armed by the form the run typed into.

A form that creates a record (Trac's New Ticket, NetBox's Add Device) shows no
existing record next to its save button, so the save compiled with no identity
check and qualification refused it ("save step ... has no armed identity
check"). The record such a save commits is the one the run typed into the form.
So the compiler gives the labelled save a FormIdentity, and before the click
the replayer requires that the button's form holds exactly the fields this run
typed into, with the values it typed.

Real headless Chromium throughout: the interactive recorder on a Trac-shaped
form, the real compiler, and the real Replayer against the same page and two
variants that must halt before the click.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import record_interactive
from openadapt_flow.ir import Workflow
from openadapt_flow.policy import is_identity_armed

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(900)

_TAMPER = """
<script>
// Something on the page rewrites the summary after the run typed it.
document.querySelector('[name=field_description]').addEventListener(
  'focus', () => { document.querySelector('[name=field_summary]').value = 'Other'; });
</script>"""


_REFORMAT = """
<script>
// A controlled input, as React renders one: every keystroke sets the value
// to a reformatted copy of what was typed.
const summary = document.querySelector('[name=field_summary]');
summary.addEventListener('input', () => { summary.value = summary.value.toUpperCase(); });
</script>"""

_BLUR_FORMAT = """
<script>
// A controlled input that reformats when it loses focus, as many React
// forms do: the read-back right after typing sees the typed text.
const summary = document.querySelector('[name=field_summary]');
summary.addEventListener('blur', () => { summary.value = summary.value.toUpperCase(); });
</script>"""

_CREATE = '<input type="submit" name="submit" value="Create ticket"{owner}>'
_PORTAL = (
    # Rendered outside the form with no form= attribute, as a React portal
    # does; its click handler submits the form from script.
    '<input type="button" name="submit" value="Create ticket" '
    "onclick=\"document.getElementById('ticket').requestSubmit()\">"
)


def _page(variant: str) -> str:
    # "split": the Create button belongs to another form. "formattr": it sits
    # outside the form and names it with form=. "portal": it sits outside the
    # form with no form owner at all.
    owner = {"split": ' form="other"', "formattr": ' form="ticket"'}.get(variant, "")
    inside = (
        _CREATE.format(owner=owner)
        if variant in ("plain", "tamper", "split", "reformat", "blurformat")
        else ""
    )
    outside = (
        _CREATE.format(owner=owner)
        if variant == "formattr"
        else (_PORTAL if variant == "portal" else "")
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>New ticket</title></head><body style="font:16px Arial;margin:24px">
<h1>New ticket</h1>
<form id="ticket" onsubmit="event.preventDefault(); window.created.push(
  document.querySelector('[name=field_summary]').value)">
  <p><label>Summary <input name="field_summary" size="40"></label></p>
  <p><label>Description <textarea name="field_description" rows="3"
     cols="40"></textarea></label></p>
  <p>{inside}</p>
</form>
<div id="footer"><p>{outside}</p></div>
<form id="other" onsubmit="event.preventDefault(); window.created.push('other')">
</form>
<script>window.created = [];</script>
{_TAMPER if variant == "tamper" else ""}
{_REFORMAT if variant == "reformat" else ""}
{_BLUR_FORMAT if variant == "blurformat" else ""}
</body></html>"""


def _serve() -> tuple[str, Callable[[], None]]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            query = parse_qs(urlsplit(self.path).query)
            body = _page(query.get("v", ["plain"])[0]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/newticket", server.shutdown


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    base, stop = _serve()
    yield base
    stop()


def _record(url: str, out: Path) -> Path:
    def drive(page: Any, pump: Any) -> None:
        def settle() -> None:
            for _ in range(12):
                pump()

        page.wait_for_selector("[name=field_summary]", state="visible")
        page.locator("[name=field_summary]").click()
        settle()
        page.keyboard.type("Printer jams on tray 2")
        settle()
        page.locator("[name=field_description]").click()
        settle()
        page.keyboard.type("Seen twice today")
        settle()
        page.locator("[name=submit]").click()
        settle()
        assert page.evaluate("window.created") == ["Printer jams on tray 2"]

    recording = record_interactive(
        url,
        out / "rec",
        headless=True,
        script=drive,
        param_fields=("field_summary", "field_description"),
    )
    compile_recording(recording, out / "bundle", name="newticket")
    return out / "bundle"


@pytest.fixture(scope="module")
def bundle(url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(url, tmp_path_factory.mktemp("create-form"))


@pytest.fixture(scope="module")
def formattr_bundle(url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(f"{url}?v=formattr", tmp_path_factory.mktemp("create-formattr"))


def _replay(url: str, bundle: Path, run_dir: Path) -> tuple[Any, list]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url)
            page.wait_for_selector("[name=field_summary]", state="visible")
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle),
                params={
                    "field_summary": "Scanner offline on floor 3",
                    "field_description": "Since this morning",
                },
                bundle_dir=bundle,
                run_dir=run_dir,
            )
            created = page.evaluate("window.created")
        finally:
            browser.close()
    return report, created


def test_the_create_button_is_armed_by_its_form(bundle: Path) -> None:
    workflow = Workflow.load(bundle)
    save = workflow.steps[-1]
    assert save.risk == "irreversible"
    assert save.anchor is not None and save.anchor.form_identity is not None
    assert save.anchor.form_identity.filled_fields == 2
    assert save.anchor.form_identity.submit_role == "form_submit"
    assert save.identity_armed is True
    assert is_identity_armed(save)
    # Counts and tokens only: nothing typed is stored with it.
    stored = json.dumps(save.anchor.form_identity.model_dump())
    assert "Printer" not in stored and "Seen" not in stored


def test_replay_with_new_values_creates_the_new_ticket(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    report, created = _replay(url, bundle, tmp_path / "run")
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert created == ["Scanner offline on floor 3"]
    save = report.results[-1]
    assert save.identity is not None
    assert (save.identity.mode, save.identity.status) == ("form", "verified")


def test_a_field_changed_after_typing_halts_before_the_click(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    report, created = _replay(f"{url}?v=tamper", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    save = next(r for r in report.results if r.identity and r.identity.mode == "form")
    assert save.identity.status == "mismatch"
    assert "don't hold exactly the value this run typed" in save.identity.observed


def test_a_button_of_another_form_halts_before_the_click(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    report, created = _replay(f"{url}?v=split", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    save = next(r for r in report.results if r.identity and r.identity.mode == "form")
    assert save.identity.status == "mismatch"
    assert "not in the form that owns the filled fields" in save.identity.observed


def test_a_controlled_input_that_reformats_halts_before_the_click(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    """Every keystroke reformats the field. The typed-value read-back on the
    summary step stops the run first; whatever notices first, nothing is
    submitted."""

    report, created = _replay(f"{url}?v=reformat", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    checked = [r for r in report.results if r.identity and r.identity.mode == "form"]
    if checked:
        assert checked[-1].identity.status == "mismatch"
        assert "don't hold exactly the value" in checked[-1].identity.observed


def test_a_value_reformatted_on_blur_halts_at_the_form_check(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    """The read-back after typing passes; the field is reformatted when focus
    moves on. The form check before the save catches it."""

    report, created = _replay(f"{url}?v=blurformat", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    save = next(r for r in report.results if r.identity and r.identity.mode == "form")
    assert save.identity.status == "mismatch"
    assert "don't hold exactly the value this run typed" in save.identity.observed


def test_a_button_outside_the_form_with_form_attribute_is_armed(
    url: str, formattr_bundle: Path, tmp_path: Path
) -> None:
    save = Workflow.load(formattr_bundle).steps[-1]
    assert save.anchor is not None and save.anchor.form_identity is not None
    assert save.anchor.form_identity.submit_role == "form_submit"

    report, created = _replay(f"{url}?v=formattr", formattr_bundle, tmp_path / "run")
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert created == ["Scanner offline on floor 3"]
    assert report.results[-1].identity.status == "verified"


def test_a_portal_button_with_no_form_owner_is_not_armed_and_halts(
    url: str, formattr_bundle: Path, tmp_path: Path
) -> None:
    # Recorded on the portal page: nothing ties the button to the form, so
    # the save gets no form identity and stays unarmed (a policy that
    # refuses unarmed saves refuses it).
    portal = _record(f"{url}?v=portal", tmp_path / "portal")
    save = Workflow.load(portal).steps[-1]
    assert save.anchor is not None and save.anchor.form_identity is None
    assert not is_identity_armed(save)

    # A program recorded with form= meets the portal page: halt before the
    # click.
    report, created = _replay(f"{url}?v=portal", formattr_bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    checked = [r for r in report.results if r.identity and r.identity.mode == "form"]
    assert checked and checked[-1].identity.status == "mismatch"
    assert (
        "not in the form that owns the filled fields" in checked[-1].identity.observed
    )
