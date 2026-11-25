"""Hub pages of section dialogs on three kinds of app.

- an EMR-like chart hub (Allergies, Medications) whose cards open dialogs;
- a SaaS onboarding checklist, where a dialog that says "Confirm" but holds a
  field is a form, not a confirmation;
- a desktop preferences window read as an accessibility tree, no browser.

Sections are learned from one demonstration and replayed with other values.
Completion is read from the hub after each save, never assumed.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.gui.adapters import screen_from_tree
from openadapt_flow.gui.dom import PageActuator
from openadapt_flow.gui.hub import (
    DialogKind,
    SectionSpec,
    classify_dialog,
    expected_writes,
    judge_hub,
    judge_hub_screen,
    learn_hub,
    run_section,
)
from openadapt_flow.gui.model import ControlSpec, Recorder, Screen, match_one
from openadapt_flow.verdict import Status
from tests.gui import _apps

# -- an EMR-like chart hub ---------------------------------------------------------

CHART_JS = """
function openS(id){document.getElementById('dlg-'+id).hidden=false;}
function saveS(id, field){
  const v=document.getElementById(field).value;
  if(!v){document.getElementById('err-'+id).textContent='Enter a value';
         document.getElementById(field).setAttribute('aria-invalid','true');return;}
  fetch('/save/'+id,{method:'POST',body:JSON.stringify({v:v})}).then(r=>r.json()).then(j=>{
    if(j.ok){document.getElementById('st-'+id).textContent=j.status;
             document.getElementById('dlg-'+id).hidden=true;}});
}"""


def _card(
    sid: str, title: str, field: str, status: str, dialog_name: Optional[str] = None
) -> str:
    return f"""<article aria-labelledby="h-{sid}"><h2 id="h-{sid}">{title}</h2>
<p id="st-{sid}">{status}</p><button onclick="openS('{sid}')">Edit {title.lower()}</button></article>
<div role="dialog" aria-modal="true" aria-label="{dialog_name or title}" id="dlg-{sid}" hidden>
<label for="f-{sid}">{field}</label><input id="f-{sid}" aria-errormessage="err-{sid}">
<p id="err-{sid}" role="alert"></p>
<button onclick="saveS('{sid}','f-{sid}')">Save</button></div>"""


def chart_hub(
    *,
    ignore_saves: bool = False,
    allergies_status: str = "Not reviewed",
    allergy_dialog: Optional[str] = None,
) -> _apps.App:
    def hub(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Chart",
            "<main><h1>Chart: Test Patient C</h1>"
            + _card("al", "Allergies", "Allergy", allergies_status, allergy_dialog)
            + _card("md", "Medications", "Medication", "Not reviewed")
            + "<button onclick=\"fetch('/sign',{method:'POST',body:'{}'})\">Sign chart</button>"
            + f"<script>{CHART_JS}</script></main>",
        )

    def save(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        sid = path.rsplit("/", 1)[1]
        if ignore_saves:
            return 200, '{"ok": true, "status": "Not reviewed"}'
        app.state[sid] = f["v"][0]
        return 200, '{"ok": true, "status": "Reviewed \\u2713 ' + f["v"][0] + '"}'

    return _apps.App(
        "chart.test",
        {
            ("GET", "/chart"): hub,
            ("POST", "/save/al"): save,
            ("POST", "/save/md"): save,
            ("POST", "/sign"): lambda a, p, m, f: (200, "{}"),
        },
    )


def _fill_with(value: str):
    def fill(screen: Screen, dialog: Any) -> None:
        (field,) = [c for c in screen.in_dialog(dialog.handle) if c.editable]
        ACT[0].set_text(field.handle, value)

    return fill


ACT: list[Any] = []


def _record_chart(page: Any, app: _apps.App) -> list:
    act = PageActuator(page)
    page.goto(f"{app.origin}/chart")
    rec = Recorder(act)
    for title, field, value in (
        ("Allergies", "Allergy", "Penicillin"),
        ("Medications", "Medication", "Drug D 5 mg"),
    ):
        rec.click(
            match_one(
                ControlSpec("button", f"Edit {title.lower()}"), act.observe()
            ).handle
        )
        s = act.observe()
        rec.set_text(
            match_one(ControlSpec("textbox", field, dialog=True), s).handle, value
        )
        rec.click(
            match_one(ControlSpec("button", "Save", dialog=True), act.observe()).handle
        )
    return rec.steps


def _learned_chart(page: Any) -> list[SectionSpec]:
    with _apps.serve(page, chart_hub()) as app:
        learned = learn_hub(_record_chart(page, app))
    assert all(s.spec is not None for s in learned), [s.reason for s in learned]
    return [s.spec for s in learned]  # type: ignore[misc]


def test_emr_hub_sections_are_learned_from_the_card_tokens(page) -> None:
    specs = _learned_chart(page)
    assert [(s.section_id, s.card, s.dialog_name) for s in specs] == [
        ("allergies", "Allergies", "Allergies"),
        ("medications", "Medications", "Medications"),
    ]
    # "Reviewed" was on the card before and after; the typed value is not a
    # done token. What the card gained is the check mark; it lost "not".
    assert specs[0].done_tokens == ("✓",)
    assert specs[0].pending_tokens == ("not",)
    assert specs[0].save_writes == 1
    assert expected_writes(specs, ["allergies", "medications"]) == 2


def test_emr_hub_replays_each_section_and_reads_completion_from_the_hub(page) -> None:
    specs = _learned_chart(page)
    app = chart_hub()
    with _apps.serve(page, app):
        act = PageActuator(page)
        ACT[:] = [act]
        page.goto(f"{app.origin}/chart")
        before = judge_hub_screen(
            act.observe(), specs, ["allergies", "medications"], run_id="r1"
        )
        assert before.status is Status.REFUTED
        for spec, value in zip(specs, ("Latex", "Drug E 2 mg")):
            out = run_section(act, spec, _fill_with(value), sections=specs, run_id="r1")
            assert out.held, out.reason
        verdict = judge_hub_screen(
            act.observe(), specs, ["allergies", "medications"], run_id="r1"
        )
        assert verdict.status is Status.HELD
    assert app.state == {"al": "Latex", "md": "Drug E 2 mg"}


@pytest.mark.parametrize(
    ("variant", "value", "status", "reason"),
    [
        ({"ignore_saves": True}, "Latex", Status.REFUTED, "still shows it pending"),
        ({}, "", Status.REFUTED, "did not close: Enter a value"),
        (
            {"allergies_status": "Reviewed ✓ Sulfa"},
            "Latex",
            Status.NOT_CHECKED,
            "before the run opened",
        ),
        (
            {"allergy_dialog": "Allergy history import"},
            "Latex",
            Status.NOT_CHECKED,
            "undeclared_form",
        ),
    ],
)
def test_emr_hub_section_that_does_not_complete_stops(
    page, variant, value, status, reason
) -> None:
    specs = _learned_chart(page)
    app = chart_hub(**variant)
    with _apps.serve(page, app):
        act = PageActuator(page)
        ACT[:] = [act]
        page.goto(f"{app.origin}/chart")
        out = run_section(act, specs[0], _fill_with(value), sections=specs, run_id="r2")
    assert out.status is status and reason in out.reason


# -- SaaS onboarding checklist: form dialogs are never confirmations -----------------


def onboarding() -> _apps.App:
    def hub(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Get started",
            """<main><h1>Get started</h1><ul>
<li><h3>Company profile</h3><span id="s1">To do</span>
<a href="#" role="button" onclick="d1.hidden=false;return false;">Set up</a></li></ul>
<div role="dialog" aria-modal="true" aria-labelledby="t1" id="d1" hidden>
<h2 id="t1">Company profile</h2><label for="cn">Company name</label><input id="cn">
<button onclick="fetch('/co',{method:'POST',body:JSON.stringify({cn:cn.value})}).then(()=>{
s1.textContent='Done';d1.hidden=true;})">Confirm</button></div>
<div role="dialog" aria-modal="true" aria-label="Confirm your email" id="d2" hidden>
<label for="code">Code</label><input id="code"><button>Confirm</button></div>
<div role="alertdialog" aria-label="Leave setup?" id="d3" hidden>
<p>Your progress is kept.</p><button>Leave</button><button>Stay</button></div>
</main>""",
        )

    def co(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        app.state["company"] = f["cn"][0]
        return 200, "{}"

    return _apps.App("app.test", {("GET", "/start"): hub, ("POST", "/co"): co})


def test_saas_dialog_with_a_field_is_a_form_even_when_it_says_confirm(page) -> None:
    section = SectionSpec(
        "company",
        ControlSpec("button", "Set up", ("Company profile",)),
        "Company profile",
        ControlSpec("button", "Confirm", ("Company profile",)),
        card="Company profile",
        done_tokens=("done",),
        pending_tokens=("to",),
    )
    app = onboarding()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/start")
        for dlg, kind in (
            ("d2", DialogKind.UNDECLARED_FORM),
            ("d3", DialogKind.CONFIRMATION),
        ):
            page.evaluate(f"{dlg}.hidden=false")
            screen = act.observe()
            (dialog,) = screen.dialogs
            assert classify_dialog(screen, dialog, [section])[0] is kind
            page.evaluate(f"{dlg}.hidden=true")
        ACT[:] = [act]
        out = run_section(act, section, _fill_with("Acme Test Co"), run_id="r3")
        assert out.held, out.reason
    assert app.state["company"] == "Acme Test Co"


# -- desktop preferences window, read as an accessibility tree -----------------------


class Prefs:
    """A native preferences window: each group shows a status and opens a
    dialog whose OK button saves."""

    def __init__(self, *, ok_saves: bool = True) -> None:
        self.status = {"Account": "Not set", "Proxy": "Not set"}
        self.values: dict[str, str] = {}
        self.open: Optional[str] = None
        self.draft = ""
        self.writes = 0
        self.ok_saves = ok_saves

    def tree(self) -> dict[str, Any]:
        groups = [
            {
                "role": "group",
                "name": g,
                "handle": f"grp:{g}",
                "text": f"{g} {self.status[g]}",
                "children": [
                    {"role": "push button", "name": "Change", "handle": f"chg:{g}"}
                ],
            }
            for g in self.status
        ]
        if self.open:
            groups.append(
                {
                    "role": "dialog",
                    "name": f"{self.open} settings",
                    "handle": "dlg",
                    "children": [
                        {
                            "role": "edit",
                            "name": "Value",
                            "value": self.draft,
                            "handle": "dlg:val",
                        },
                        {"role": "push button", "name": "OK", "handle": "dlg:ok"},
                    ],
                }
            )
        return {"role": "window", "name": "Preferences", "children": groups}

    def observe(self) -> Screen:
        return screen_from_tree(self.tree(), location="prefs")

    def click(self, handle: str) -> None:
        if handle.startswith("chg:"):
            self.open, self.draft = handle[4:], ""
        elif handle == "dlg:ok" and self.open:
            if self.ok_saves:
                self.values[self.open] = self.draft
                self.status[self.open] = "Configured"
                self.writes += 1
            self.open = None

    def set_text(self, handle: str, value: str) -> None:
        self.draft = value

    def set_checked(self, handle: str, checked: bool) -> None:
        raise KeyError(handle)

    def write_count(self) -> Optional[int]:
        return self.writes


def _record_prefs() -> list[SectionSpec]:
    app = Prefs()
    rec = Recorder(app)
    for group, value in (("Account", "tester"), ("Proxy", "proxy.example.test:3128")):
        rec.click(f"chg:{group}")
        rec.set_text("dlg:val", value)
        rec.click("dlg:ok")
    learned = learn_hub(rec.steps)
    assert all(s.spec for s in learned), [s.reason for s in learned]
    return [s.spec for s in learned]  # type: ignore[misc]


def test_desktop_prefs_hub_learns_and_replays_without_a_browser() -> None:
    specs = _record_prefs()
    assert [(s.card, s.done_tokens, s.pending_tokens) for s in specs] == [
        ("Account", ("configured",), ("not", "set")),
        ("Proxy", ("configured",), ("not", "set")),
    ]
    app = Prefs()

    def fill(value: str):
        return lambda screen, dialog: app.set_text("dlg:val", value)

    for spec, value in zip(specs, ("other", "proxy2.example.test:8080")):
        assert run_section(app, spec, fill(value), sections=specs, run_id="d1").held
    assert (
        judge_hub_screen(app.observe(), specs, ["account", "proxy"], run_id="d1").status
        is Status.HELD
    )


def test_desktop_prefs_ok_that_saves_nothing_is_refuted() -> None:
    specs = _record_prefs()
    app = Prefs(ok_saves=False)
    out = run_section(
        app, specs[0], lambda s, d: app.set_text("dlg:val", "x"), sections=specs
    )
    assert out.status is Status.REFUTED and "saving wrote 0" in out.reason


def test_judge_hub_never_passes_empty_unread_or_unplanned() -> None:
    row = {"section_id": "a", "found": True, "done": True, "run_id": "r"}
    assert judge_hub(["a"], [row], run_id="r").status is Status.HELD
    assert judge_hub([], [], run_id="r").status is not Status.HELD
    assert (
        judge_hub(["a"], [row, {**row, "section_id": "b"}], run_id="r").status
        is not Status.HELD
    )
    assert (
        judge_hub(["a"], [{**row, "found": False}], run_id="r").status
        is Status.NOT_CHECKED
    )
    assert judge_hub(["a"], [row], run_id="r", open_dialogs=1).status is not Status.HELD
    assert judge_hub(["a", "b"], [row], run_id="r").status is not Status.HELD
