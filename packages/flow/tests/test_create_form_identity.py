"""A create-new form's save is armed by the form the run typed into.

A form that creates a record (Trac's New Ticket, NetBox's Add Device) shows no
existing record next to its save button, so the save compiled with no identity
check and qualification refused it ("save step ... has no armed identity
check"). The record such a save commits is the one the run typed into the form.
So the compiler gives the labelled save a FormIdentity, and before the click
the replayer requires that the button's form holds exactly the fields this run
typed into, with the values it typed.

Real headless Chromium throughout: the interactive recorder on a Trac-shaped
form, the real compiler, and the real Replayer against the same page and
variants that must halt before the click with nothing submitted, including
the ones the #1543 review found: an edit form that looks like a create form,
an address or method changed after typing, a record id added while typing, a
parent left on the wrong default, and a click that submits another form.
"""

from __future__ import annotations

import http.server
import json
import secrets
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


# Variants from the #1543 review: each looks like the same create form, and
# each would submit the wrong thing if only the typed fields were checked.
_SCRIPTS = {
    # Re-review round 2. A click handler copies the page's current parent id
    # into an empty hidden field as the form is sent.
    "copy7": """
window.currentPid = '7';
document.querySelector('[name=submit]').addEventListener('click', () => {
  document.querySelector('[name=pid]').value = window.currentPid;
});""",
    "copy9": """
window.currentPid = '9';
document.querySelector('[name=submit]').addEventListener('click', () => {
  document.querySelector('[name=pid]').value = window.currentPid;
});""",
    # A disabled hidden record id the click handler enables.
    "toggle": """
document.querySelector('[name=submit]').addEventListener('click', () => {
  document.querySelector('[name=id]').disabled = false;
});""",
    # The click handler waits, then submits another form.
    "delayed_other": """
document.querySelector('[name=submit]').addEventListener('click', (e) => {
  e.preventDefault();
  setTimeout(() => document.getElementById('other').requestSubmit(), 300);
});""",
    # The click handler waits, adds a record id, then submits with the button.
    "delayed_mutate": """
document.querySelector('[name=submit]').addEventListener('click', (e) => {
  e.preventDefault();
  setTimeout(() => {
    const f = document.getElementById('ticket');
    const h = document.createElement('input'); h.type = 'hidden'; h.name = 'id';
    h.value = '42'; f.appendChild(h);
    f.requestSubmit(document.querySelector('[name=submit]'));
  }, 300);
});""",
    # A script sets the hidden parent id when typing starts.
    "pid_input7": """
document.querySelector('[name=field_summary]').addEventListener('input', () => {
  document.querySelector('[name=pid]').value = '7';
}, {once: true});""",
    "pid_input9": """
document.querySelector('[name=field_summary]').addEventListener('input', () => {
  document.querySelector('[name=pid]').value = '9';
}, {once: true});""",
    # A script sets a different action and method, and adds a record id,
    # after the run has typed.
    "action_swap": """
document.querySelector('[name=field_description]').addEventListener('focus', () => {
  const f = document.getElementById('ticket'); f.action = '/ticket/42'; f.method = 'post';
  const h = document.createElement('input'); h.type = 'hidden'; h.name = 'id';
  h.value = '42'; f.appendChild(h);
});""",
    # A hidden record id appears while the run types.
    "late_hidden": """
document.querySelector('[name=field_description]').addEventListener('input', () => {
  const f = document.getElementById('ticket');
  if (!f.querySelector('[name=id]')) { const h = document.createElement('input');
    h.type = 'hidden'; h.name = 'id'; h.value = '42'; f.appendChild(h); }
});""",
    # The Create button's click handler submits another form instead.
    "click_other": """
document.querySelector('[name=submit]').addEventListener('click', (e) => {
  e.preventDefault(); document.getElementById('other').requestSubmit();
});""",
    # A hidden revision that changes on every keystroke, as some editors keep.
    "rotating": """
document.querySelector('[name=field_description]').addEventListener('input', () => {
  const h = document.querySelector('[name=draft_rev]');
  h.value = 'r' + Math.random().toString(16).slice(2);
});""",
    # A React-style keyed remount of the summary input on blur, same value.
    "remount": """
document.querySelector('[name=field_summary]').addEventListener('blur', () => {
  const s = document.querySelector('[name=field_summary]');
  const n = s.cloneNode(true); n.value = s.value; s.replaceWith(n);
}, {once: true});""",
}


def _page(variant: str) -> str:
    # "split": the Create button belongs to another form. "formattr": it sits
    # outside the form and names it with form=. "portal": it sits outside the
    # form with no form owner at all.
    owner = {"split": ' form="other"', "formattr": ' form="ticket"'}.get(variant, "")
    inside = (
        _CREATE.format(owner=owner) if variant not in ("formattr", "portal") else ""
    )
    outside = (
        _CREATE.format(owner=owner)
        if variant == "formattr"
        else (_PORTAL if variant == "portal" else "")
    )
    title, action, hidden, extra = "New ticket", "/newticket", "", ""
    if variant in ("query_new", "query_42"):
        action = "/save?mode=new" if variant == "query_new" else "/save?id=42"
    if variant in ("copy7", "copy9", "pid_input7", "pid_input9"):
        hidden = '<input type="hidden" name="pid" value="">'
    if variant == "toggle":
        hidden = '<input type="hidden" name="id" value="42" disabled>'
    if variant in ("uuidA", "uuidB"):
        # Named and shaped like a token, but a record id (hex groups).
        hidden = (
            '<input type="hidden" name="encounter_token" value="enc-3f2a9c1e-000'
            + ("1" if variant == "uuidA" else "2")
            + '">'
        )
    if variant in ("enc7", "enc9"):
        # Named like a token, but a short number: a record id, not a token.
        hidden = (
            '<input type="hidden" name="encounter_token" value="'
            + ("7" if variant == "enc7" else "9")
            + '">'
        )
    if variant == "edit":
        # Ticket 42's edit form: the same blank fields, a record id, another
        # address. Its save updates ticket 42.
        title, action = "Ticket #42 (edit)", "/ticket/42"
        hidden = '<input type="hidden" name="id" value="42">'
    if variant in ("parent_ok", "parent_wrong"):
        hidden = (
            '<input type="hidden" name="pid" value="'
            + ("7" if variant == "parent_ok" else "9")
            + '">'
        )
    if variant in ("select_ok", "select_wrong"):
        chosen = "7" if variant == "select_ok" else "9"
        extra = (
            '<p><label>Patient <select name="pid">'
            + "".join(
                f'<option value="{v}"{" selected" if v == chosen else ""}>{n}</option>'
                for v, n in (("7", "Ann (7)"), ("9", "Bob (9)"))
            )
            + "</select></label></p>"
        )
    if variant == "csrf":
        # A per-session token, different on every page load (Trac's form has
        # one).
        hidden = (
            f'<input type="hidden" name="__FORM_TOKEN" value="{secrets.token_hex(12)}">'
        )
    if variant == "rotating":
        hidden = (
            '<input type="hidden" name="draft_rev" value="r'
            + secrets.token_hex(6)
            + '">'
        )
    script = _SCRIPTS.get(variant, "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title></head><body style="font:16px Arial;margin:24px">
<h1>{title}</h1>
<form id="ticket" action="{action}" onsubmit="event.preventDefault();
  window.created.push(document.querySelector('[name=field_summary]').value)">
  {hidden}
  <p><label>Summary <input name="field_summary" size="40"></label></p>
  <p><label>Description <textarea name="field_description" rows="3"
     cols="40"></textarea></label></p>
  {extra}
  <p>{inside}</p>
</form>
<div id="footer"><p>{outside}</p></div>
<form id="other" onsubmit="event.preventDefault(); window.created.push('other')">
</form>
<script>window.created = [];</script>
{_TAMPER if variant == "tamper" else ""}
{_REFORMAT if variant == "reformat" else ""}
{_BLUR_FORMAT if variant == "blurformat" else ""}
{f"<script>{script}</script>" if script else ""}
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


def _record(url: str, out: Path, *, armed: bool = True) -> Path:
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
        # Let the recorder capture the page after the save before it ends.
        for _ in range(30):
            pump()
        assert page.evaluate("window.created") == ["Printer jams on tray 2"]

    recording = record_interactive(
        url,
        out / "rec",
        headless=True,
        script=drive,
        param_fields=("field_summary", "field_description"),
    )
    compile_recording(recording, out / "bundle", name="newticket")
    # A save recorded unarmed has nothing to check: say so here, so a slow
    # recording can't pass (or fail) a case for the wrong reason.
    save = Workflow.load(out / "bundle").steps[-1]
    assert (save.anchor is not None and save.anchor.form_identity is not None) is armed
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
    portal = _record(f"{url}?v=portal", tmp_path / "portal", armed=False)
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


@pytest.fixture(scope="module")
def parent_bundle(url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(f"{url}?v=parent_ok", tmp_path_factory.mktemp("create-parent"))


@pytest.fixture(scope="module")
def select_bundle(url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(f"{url}?v=select_ok", tmp_path_factory.mktemp("create-select"))


def _halted_by_form(report: Any, created: list, expected: str) -> None:
    assert not report.success
    assert created == []
    checked = [r for r in report.results if r.identity and r.identity.mode == "form"]
    assert checked, [(r.step_id, r.error) for r in report.results]
    assert checked[-1].identity.status == "mismatch"
    assert expected in checked[-1].identity.observed


@pytest.mark.parametrize(
    "variant, expected",
    [
        ("edit", "submits to a different address"),
        ("action_swap", "submits to a different address"),
        ("late_hidden", "different set of fields"),
    ],
)
def test_a_form_that_is_not_the_demonstrated_one_halts(
    url: str, bundle: Path, tmp_path: Path, variant: str, expected: str
) -> None:
    report, created = _replay(f"{url}?v={variant}", bundle, tmp_path / "run")
    _halted_by_form(report, created, expected)


def test_a_hidden_parent_on_another_record_halts(
    url: str, parent_bundle: Path, tmp_path: Path
) -> None:
    save = Workflow.load(parent_bundle).steps[-1]
    assert save.anchor is not None and save.anchor.form_identity is not None
    stored = json.dumps(save.anchor.form_identity.model_dump())
    assert '"7"' not in stored and "pid" in stored  # a digest, not the value

    report, created = _replay(f"{url}?v=parent_ok", parent_bundle, tmp_path / "ok")
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert created == ["Scanner offline on floor 3"]

    report, created = _replay(f"{url}?v=parent_wrong", parent_bundle, tmp_path / "bad")
    _halted_by_form(report, created, "'pid' field, which this run did not type into")


def test_a_select_left_on_another_default_halts(
    url: str, select_bundle: Path, tmp_path: Path
) -> None:
    report, created = _replay(f"{url}?v=select_ok", select_bundle, tmp_path / "ok")
    assert report.success, [(r.step_id, r.error) for r in report.results]

    report, created = _replay(f"{url}?v=select_wrong", select_bundle, tmp_path / "bad")
    _halted_by_form(report, created, "'pid' field, which this run did not type into")


def test_a_click_that_submits_another_form_is_cancelled(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    report, created = _replay(f"{url}?v=click_other", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    save = report.results[-1]
    assert save.identity is not None and save.identity.status == "verified"
    assert "submitted a different form" in (save.error or "")


def test_a_per_session_token_still_verifies(url: str, tmp_path: Path) -> None:
    """A hidden token named like one and shaped like one (Trac's
    __FORM_TOKEN) differs on every page load. It isn't compared, and its
    value is never kept, so a new load with a new token still verifies."""

    bundle = _record(f"{url}?v=csrf", tmp_path / "rec")
    save = Workflow.load(bundle).steps[-1]
    assert save.anchor is not None and save.anchor.form_identity is not None
    assert save.anchor.form_identity.exempt == ("__FORM_TOKEN",)
    report, created = _replay(f"{url}?v=csrf", bundle, tmp_path / "run")
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert created == ["Scanner offline on floor 3"]
    assert report.results[-1].identity.status == "verified"


@pytest.mark.parametrize(
    "recorded, replayed, expected",
    [
        # A hidden value that changed while the demonstration was typed is
        # not exempt for that reason alone.
        ("rotating", "rotating", "'draft_rev' field, which this run did not type"),
        # A parent id a script sets when typing starts is compared.
        ("pid_input7", "pid_input9", "'pid' field, which this run did not type"),
        # A short number under a token-like name is compared.
        ("enc7", "enc9", "'encounter_token' field, which this run did not type"),
        # A record-shaped value under a token-like name is compared too.
        ("uuidA", "uuidB", "'encounter_token' field, which this run did not type"),
        # The address's query parameter names count.
        ("query_new", "query_42", "submits to a different address"),
    ],
)
def test_values_that_only_look_like_tokens_are_compared(
    url: str, tmp_path: Path, recorded: str, replayed: str, expected: str
) -> None:
    bundle = _record(f"{url}?v={recorded}", tmp_path / "rec")
    report, created = _replay(f"{url}?v={replayed}", bundle, tmp_path / "run")
    _halted_by_form(report, created, expected)


@pytest.mark.parametrize(
    "recorded, replayed, expected",
    [
        ("copy7", "copy9", "what the form would send changed after it was checked"),
        ("plain", "toggle", "what the form would send changed after it was checked"),
        ("plain", "delayed_other", "submitted a different form"),
        (
            "plain",
            "delayed_mutate",
            "what the form would send changed after it was checked",
        ),
    ],
)
def test_a_submission_that_differs_from_the_checked_one_is_cancelled(
    url: str, tmp_path: Path, recorded: str, replayed: str, expected: str
) -> None:
    """The form checks out before the click; then a click handler changes
    what is sent, or submits something else, now or 300 ms later. The guard
    cancels it and the run halts."""

    bundle = _record(f"{url}?v={recorded}", tmp_path / "rec")
    report, created = _replay(f"{url}?v={replayed}", bundle, tmp_path / "run")
    assert not report.success
    assert created == []
    save = report.results[-1]
    assert save.identity is not None and save.identity.status == "verified"
    assert expected in (save.error or ""), save.error


def test_a_keyed_remount_halts_a_known_limit(
    url: str, bundle: Path, tmp_path: Path
) -> None:
    """A React-style keyed remount replaces a typed field with a new node, even
    with the same value. The new node isn't one the run typed into, so the run
    halts: a false halt, and the safe direction (LIMITS.md)."""

    report, created = _replay(f"{url}?v=remount", bundle, tmp_path / "run")
    _halted_by_form(report, created, "typed into 1 field(s)")
