"""Checkbox attestations on four kinds of app.

- a web form (membership application) with a declaration and a marketing box;
- an EMR-like chart where a medication review must be attested in a dialog;
- a SaaS danger zone where the attestation is a switch;
- a spreadsheet-like grid whose "final totals" box sits in a dialog.

An attestation is ticked only on a person's grant for the pinned statement;
an undeclared box, a changed statement, a box bound to a fact or a missing
grant stops the run before anything is ticked.
"""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.gui.attestations import (
    AttestationGrant,
    AttestationSpec,
    BoxDecision,
    IgnoredBox,
    PackError,
    apply_attestations,
    check_pack,
    judge_attestations,
    judge_screen,
    learn_attestations,
    plan_checkboxes,
    statement_digest,
)
from openadapt_flow.gui.dom import PageActuator
from openadapt_flow.gui.model import ControlSpec, Recorder, match_one
from openadapt_flow.verdict import Status
from tests.gui import _apps
from tests.gui._grid import GridApp, Sheet

TRUE = "I confirm the information I have given is true and complete"


def _membership(
    statement: str = TRUE, extra: str = "", agree: bool = False
) -> _apps.App:
    box = (
        f'<p>{statement}</p><label><input type="checkbox" id="dec" name="dec">I agree</label>'
        if agree
        else f'<label><input type="checkbox" id="dec" name="dec">{statement}</label>'
    )

    def form(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Apply",
            f"""<main><h1>Membership application</h1><form method="post" action="/apply">
<label for="nm">Full name</label><input id="nm" name="name">
<fieldset><legend>Declaration</legend>{box}</fieldset>
<label><input type="checkbox" id="mk" name="mk">Send me news and offers</label>{extra}
<button>Submit application</button></form></main>""",
        )

    def apply(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        app.state["applied"] = {k: v[0] for k, v in f.items()}
        return 200, _apps.page("Thanks", "<main><h1>Application received</h1></main>")

    return _apps.App("join.test", {("GET", "/apply"): form, ("POST", "/apply"): apply})


DECL = AttestationSpec(
    "declaration",
    ControlSpec("checkbox", TRUE, ("Declaration",)),
    statement_digest(TRUE),
)
MARKETING = IgnoredBox(
    ControlSpec("checkbox", "Send me news and offers"), checked=False
)
GRANT = AttestationGrant("declaration", statement_digest(TRUE), granted_by="applicant")


def _open(page: Any, app: _apps.App) -> PageActuator:
    act = PageActuator(page)
    page.goto(f"{app.origin}/apply")
    return act


# -- web form --------------------------------------------------------------------


def test_web_form_declaration_is_ticked_on_a_grant_and_read_back(page) -> None:
    app = _membership()
    with _apps.serve(page, app):
        act = _open(page, app)
        screen = act.observe()
        name = match_one(ControlSpec("textbox", "Full name"), screen).handle
        act.set_text(name, "Cy Sample")
        plan = plan_checkboxes(
            act.observe(),
            attestations=[DECL],
            grants=[GRANT],
            ignored=[MARKETING],
            run_id="r1",
        )
        assert [b.decision for b in plan.boxes] == [BoxDecision.TICK, BoxDecision.LEAVE]
        after = apply_attestations(act, plan)
        verdict = judge_screen(plan, after)
        assert verdict.status is Status.HELD, verdict.settlement
        act.click(match_one(ControlSpec("button", "Submit application"), after).handle)
    assert app.state["applied"] == {"name": "Cy Sample", "dec": "on"}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"grants": []}, "no one attested"),
        (
            {"grants": [AttestationGrant("declaration", "0" * 64, "applicant")]},
            "different statement",
        ),
        ({"ignored": []}, "does not declare"),
    ],
)
def test_web_form_without_a_matching_grant_or_declaration_stops(
    page, kwargs, reason
) -> None:
    app = _membership()
    with _apps.serve(page, app):
        act = _open(page, app)
        args = {
            "attestations": [DECL],
            "grants": [GRANT],
            "ignored": [MARKETING],
            **kwargs,
        }
        plan = plan_checkboxes(act.observe(), **args)
        assert plan.stops and reason in plan.stops[0].reason
        with pytest.raises(ValueError):
            apply_attestations(act, plan)
        assert all(
            c.checked is False for c in act.observe().controls if c.role == "checkbox"
        )


def test_web_form_changed_wording_or_a_new_box_stops(page) -> None:
    reworded = _membership(TRUE + ", and I accept the new terms")
    with _apps.serve(page, reworded):
        act = _open(page, reworded)
        # The spec names the box by its old words, so a reworded box is a
        # box the pack does not declare.
        plan = plan_checkboxes(
            act.observe(), attestations=[DECL], grants=[GRANT], ignored=[MARKETING]
        )
        assert [b.reason for b in plan.stops] == [
            "a checkbox the pack does not declare"
        ]
    # A box labelled "I agree" pins the paragraph around it: new words there
    # are a different statement.
    agree_text = f"Declaration {TRUE} I agree"
    agree = AttestationSpec(
        "declaration",
        ControlSpec("checkbox", "I agree", ("Declaration",)),
        statement_digest(agree_text),
        statement_from="container",
    )
    grant = AttestationGrant("declaration", agree.statement_sha256, "applicant")
    same = _membership(agree=True)
    with _apps.serve(page, same):
        act = _open(page, same)
        plan = plan_checkboxes(
            act.observe(), attestations=[agree], grants=[grant], ignored=[MARKETING]
        )
        assert not plan.stops, plan.stops
        assert judge_screen(plan, apply_attestations(act, plan)).status is Status.HELD
    changed = _membership(TRUE + " and I accept the new fees", agree=True)
    with _apps.serve(page, changed):
        act = _open(page, changed)
        plan = plan_checkboxes(
            act.observe(), attestations=[agree], grants=[grant], ignored=[MARKETING]
        )
        assert [b.reason for b in plan.stops] == [
            "the statement differs from the declared one"
        ]
    added = _membership(
        extra='<label><input type="checkbox" id="arb">I agree to binding arbitration</label>'
    )
    with _apps.serve(page, added):
        act = _open(page, added)
        plan = plan_checkboxes(
            act.observe(), attestations=[DECL], grants=[GRANT], ignored=[MARKETING]
        )
        assert [b.name for b in plan.stops] == ["I agree to binding arbitration"]


def test_a_grant_must_be_a_grant_not_a_fact() -> None:
    with pytest.raises(TypeError):
        plan_checkboxes(_membership_screen(), attestations=[DECL], grants=[True])
    with pytest.raises(TypeError):
        plan_checkboxes(
            _membership_screen(), attestations=[DECL], grants=[{"declaration": True}]
        )


def _membership_screen():
    from openadapt_flow.gui.model import Container, Control, Screen

    return Screen(
        controls=(
            Control(
                "#dec",
                "checkbox",
                TRUE,
                checked=False,
                containers=(Container("fs", "group", "Declaration"),),
            ),
        )
    )


def test_web_form_demo_suggests_the_declaration_and_the_box_left_alone(page) -> None:
    app = _membership()
    with _apps.serve(page, app):
        act = _open(page, app)
        rec = Recorder(act)
        s = act.observe()
        rec.set_text(
            match_one(ControlSpec("textbox", "Full name"), s).handle, "Dee Sample"
        )
        rec.set_checked("#dec", True)
        rec.click(
            match_one(ControlSpec("button", "Submit application"), act.observe()).handle
        )
    candidates, leave = learn_attestations(rec.steps)
    assert [(c.name, c.control.containers) for c in candidates] == [
        (TRUE, ("Declaration",))
    ]
    assert candidates[0].statement_sha256 == DECL.statement_sha256
    assert [(i.control.name, i.checked) for i in leave] == [
        ("Send me news and offers", False)
    ]


# -- EMR-like chart: an attestation bound to a fact is refused --------------------


def _chart() -> _apps.App:
    def chart(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Chart",
            """<main><h1>Chart: Test Patient B</h1>
<button onclick="d.hidden=false">Review medications</button>
<div role="dialog" aria-modal="true" aria-label="Medication review" id="d" hidden>
<ul><li>Drug A 10 mg daily</li></ul>
<label><input type="checkbox" id="att">I attest that I reviewed this medication list with the patient</label>
<button onclick="fetch('/review',{method:'POST',body:'{}'});d.hidden=true">Done</button></div></main>""",
        )

    return _apps.App(
        "chart.test",
        {("GET", "/chart"): chart, ("POST", "/review"): lambda a, p, m, f: (200, "{}")},
    )


REVIEW = "I attest that I reviewed this medication list with the patient"
REVIEW_SPEC = AttestationSpec(
    "med_review", ControlSpec("checkbox", REVIEW), statement_digest(REVIEW)
)


def test_emr_attestation_bound_to_a_fact_is_refused_by_pack_check_and_plan(
    page,
) -> None:
    with pytest.raises(PackError):
        check_pack(
            [REVIEW_SPEC],
            [ControlSpec("textbox", "Note"), ControlSpec("checkbox", REVIEW)],
        )
    check_pack([REVIEW_SPEC], [ControlSpec("textbox", "Note")])
    app = _chart()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/chart")
        act.click(
            match_one(ControlSpec("button", "Review medications"), act.observe()).handle
        )
        screen = act.observe()
        box = match_one(ControlSpec("checkbox", REVIEW, dialog=True), screen)
        grant = AttestationGrant(
            "med_review", REVIEW_SPEC.statement_sha256, "clinician 4"
        )
        bound = plan_checkboxes(
            screen, attestations=[REVIEW_SPEC], grants=[grant], bound=[box.handle]
        )
        assert [b.reason for b in bound.stops] == [
            "a declared attestation is bound to a field"
        ]
        plan = plan_checkboxes(
            screen, attestations=[REVIEW_SPEC], grants=[grant], run_id="r2"
        )
        after = apply_attestations(act, plan)
        assert judge_screen(plan, after).status is Status.HELD
        assert plan.ticks[0].granted_by == "clinician 4"


# -- SaaS: a switch ---------------------------------------------------------------


DANGER = "I understand this permanently deletes every project in the workspace"


def _danger_zone(prechecked: bool = False) -> _apps.App:
    state = "true" if prechecked else "false"

    def page_(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Danger zone",
            f"""<main><h1>Danger zone</h1><section aria-label="Delete workspace">
<span role="switch" tabindex="0" id="sw" aria-checked="{state}"
 onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked')==='true'?'false':'true')">{DANGER}</span>
</section></main>""",
        )

    return _apps.App("app.test", {("GET", "/danger"): page_})


DANGER_SPEC = AttestationSpec(
    "delete_ack", ControlSpec("switch", DANGER), statement_digest(DANGER)
)


def test_saas_switch_attestation_ticks_on_grant_and_refuses_a_prechecked_one(
    page,
) -> None:
    grant = AttestationGrant(
        "delete_ack", DANGER_SPEC.statement_sha256, "workspace owner"
    )
    app = _danger_zone()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/danger")
        plan = plan_checkboxes(
            act.observe(), attestations=[DANGER_SPEC], grants=[grant]
        )
        assert [b.decision for b in plan.boxes] == [BoxDecision.TICK]
        act.click(plan.ticks[0].handle)  # a switch is toggled by a click
        assert judge_screen(plan, act.observe()).status is Status.HELD
    pre = _danger_zone(prechecked=True)
    with _apps.serve(page, pre):
        act = PageActuator(page)
        page.goto(f"{pre.origin}/danger")
        plan = plan_checkboxes(
            act.observe(), attestations=[DANGER_SPEC], grants=[grant]
        )
        assert [b.reason for b in plan.stops] == [
            "the box was ticked before the run touched it"
        ]


# -- grid: a box in a dialog, no browser --------------------------------------------


FINAL = "These totals are final for the quarter"


class _ClosingGrid(GridApp):
    def tree(self) -> dict[str, Any]:
        self.dialog = {
            "role": "dialog",
            "name": "Close quarter",
            "handle": "dlg",
            "children": [
                {
                    "role": "check box",
                    "name": FINAL,
                    "handle": "chk:final",
                    "checked": self.checks.get("chk:final", False),
                },
                {
                    "role": "check box",
                    "name": "Email the summary to me",
                    "handle": "chk:email",
                    "checked": self.checks.get("chk:email", False),
                },
            ],
        }
        return super().tree()


def test_grid_dialog_attestation_settles_against_a_fresh_read() -> None:
    grid = _ClosingGrid([Sheet("Q3", ("Item", "Amount"), [["Rent", "1300"]])])
    spec = AttestationSpec(
        "final",
        ControlSpec("checkbox", FINAL, ("Close quarter",)),
        statement_digest(FINAL),
    )
    grant = AttestationGrant("final", spec.statement_sha256, "controller")
    leave = IgnoredBox(ControlSpec("checkbox", "Email the summary to me"))
    plan = plan_checkboxes(
        grid.observe(),
        attestations=[spec],
        grants=[grant],
        ignored=[leave],
        run_id="g1",
    )
    after = apply_attestations(grid, plan)
    assert judge_screen(plan, after).status is Status.HELD
    # Someone ticks the box the pack leaves alone after the plan: refuted.
    grid.checks["chk:email"] = True
    assert judge_screen(plan, grid.observe()).status is Status.REFUTED


def test_judge_refuses_empty_unexpected_and_unread_boxes() -> None:
    row = {
        "handle": "a",
        "decision": "tick",
        "digest": statement_digest("x"),
        "want_checked": True,
    }
    good = [{"handle": "a", "statement": "x", "checked": True, "run_id": "r"}]
    assert judge_attestations([row], good, run_id="r").status is Status.HELD
    assert judge_attestations([], [], run_id="r").status is not Status.HELD
    extra = good + [{"handle": "b", "statement": "y", "checked": False, "run_id": "r"}]
    assert judge_attestations([row], extra, run_id="r").status is not Status.HELD
    unread = [{**good[0], "checked": None}]
    assert judge_attestations([row], unread, run_id="r").status is Status.NOT_CHECKED
    other_run = [{**good[0], "run_id": "q"}]
    assert judge_attestations([row], other_run, run_id="r").status is not Status.HELD
    assert (
        judge_attestations([row], good, run_id="r", complete=False).status
        is not Status.HELD
    )
