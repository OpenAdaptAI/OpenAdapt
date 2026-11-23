"""Links, tabs and menu items as steps, and the commit point, on four kinds of app.

- a web form whose next control is a link (registration wizard);
- an EMR-like chart whose commit is a plain link (a GET that signs the note);
- a SaaS settings page with tabs and a link that publishes by POST;
- a spreadsheet-like grid with no browser (sheet tabs, a Save menu item).

Each app is recorded once through the same backend a run uses, the steps are
learned from that demonstration, and a second run with other values replays
them. The negative cases change the app after the demonstration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import openadapt_flow
from openadapt_flow.gui.adapters import screen_from_form_snapshot
from openadapt_flow.gui.dom import PageActuator
from openadapt_flow.gui.model import ControlSpec, Recorder, match_one
from openadapt_flow.gui.steps import (
    StepEvidence,
    StepRole,
    StepSpec,
    commit_points,
    judge_step,
    learn_steps,
    take_step,
)
from openadapt_flow.verdict import Status
from tests.gui import _apps
from tests.gui._grid import GridApp, Sheet


def test_code_under_test_is_this_checkout() -> None:
    flow = Path(__file__).resolve().parents[2]
    assert Path(openadapt_flow.__file__).resolve().is_relative_to(flow)


def _handle(screen, role: str, name: str) -> str:
    return match_one(ControlSpec(role, name), screen).handle


def _record_registration(page, app) -> list:
    act = PageActuator(page)
    page.goto(f"{app.origin}/contact")
    rec = Recorder(act)
    s = act.observe()
    rec.set_text(_handle(s, "textbox", "Full name"), "Ada Example")
    rec.set_text(_handle(s, "textbox", "Email"), "ada@example.test")
    rec.click(_handle(s, "link", "Continue"))
    s = act.observe()
    page.select_option(_handle(s, "combobox", "Ticket type"), "std")
    rec.click(_handle(act.observe(), "button", "Register"))
    return rec.steps


# -- web form: the next control is a link ------------------------------------------


def test_web_form_link_is_a_navigation_step_and_the_button_commits(page) -> None:
    with _apps.serve(page, _apps.registration_app()) as app:
        learned = learn_steps(_record_registration(page, app))
    roles = [(s.control.role, s.control.name, s.role) for s in learned]
    assert roles == [
        ("link", "Continue", StepRole.NAVIGATE),
        ("button", "Register", StepRole.COMMIT),
    ]
    nxt = learned[0].spec
    assert nxt is not None and nxt.control.role == "link"
    assert nxt.control.target == "/ticket" or nxt.control.target is None
    (commit,) = commit_points(learned)
    assert commit.spec is not None and commit.spec.writes == 1


def test_web_form_replays_learned_link_and_commit_with_new_values(page) -> None:
    with _apps.serve(page, _apps.registration_app()) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]
    app2 = _apps.registration_app()
    with _apps.serve(page, app2):
        act = PageActuator(page)
        page.goto(f"{app2.origin}/contact")
        s = act.observe()
        act.set_text(_handle(s, "textbox", "Full name"), "Bo Sample")
        act.set_text(_handle(s, "textbox", "Email"), "bo@example.test")
        out = take_step(act, steps[0], run_id="r1")
        assert out.held, out.result.reason
        page.select_option(_handle(act.observe(), "combobox", "Ticket type"), "vip")
        out = take_step(act, steps[1], run_id="r1")
        assert out.held, out.result.reason
    assert app2.state["records"] == [
        {"name": "Bo Sample", "email": "bo@example.test", "ticket": "vip"}
    ]


def test_a_link_that_starts_writing_is_refuted(page) -> None:
    with _apps.serve(page, _apps.registration_app()) as app:
        nxt = learn_steps(_record_registration(page, app))[0].spec
    changed = _apps.registration_app(next_writes=True)
    with _apps.serve(page, changed):
        act = PageActuator(page)
        page.goto(f"{changed.origin}/contact")
        out = take_step(act, nxt)
    assert out.clicked and out.result.status is Status.REFUTED
    assert "navigate step wrote" in out.result.reason


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ({"commit_posts": 2}, "wrote 2 times; the demo wrote 1"),
        ({"error_after": True}, "shows an error"),
    ],
)
def test_a_commit_that_differs_from_the_demo_is_refuted(page, variant, reason) -> None:
    with _apps.serve(page, _apps.registration_app()) as app:
        commit = learn_steps(_record_registration(page, app))[1].spec
    changed = _apps.registration_app(**variant)
    with _apps.serve(page, changed):
        act = PageActuator(page)
        page.goto(f"{changed.origin}/ticket")
        out = take_step(act, commit)
    assert out.result.status is Status.REFUTED
    assert reason in out.result.reason


def test_fetch_then_navigate_is_refuted_as_aborted(page) -> None:
    """Pins a known limit: a link that fires ``fetch()`` and then navigates
    aborts its own request, so the step is refuted for ERR_ABORTED (failing
    closed) rather than for the write it started."""
    with _apps.serve(page, _apps.registration_app()) as app:
        nxt = learn_steps(_record_registration(page, app))[0].spec
    changed = _apps.registration_app(next_writes=True, fetch_writes=True)
    with _apps.serve(page, changed):
        act = PageActuator(page)
        page.goto(f"{changed.origin}/contact")
        out = take_step(act, nxt)
    assert out.clicked and out.result.status is Status.REFUTED
    assert "ERR_ABORTED" in out.result.reason


def _replay_commit(page, steps, app, run_id: str):
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/contact")
        s = act.observe()
        act.set_text(_handle(s, "textbox", "Full name"), "Bo Sample")
        act.set_text(_handle(s, "textbox", "Email"), "bo@example.test")
        assert take_step(act, steps[0], run_id=run_id).held
        page.select_option(_handle(act.observe(), "combobox", "Ticket type"), "vip")
        return take_step(act, steps[1], run_id=run_id)


def test_a_commit_bounced_to_sign_in_is_not_held(page) -> None:
    """Demo POST answers 200; replay answers 302 to a plain sign-in page with
    no alert and saves nothing. The redirect is not the demo's outcome."""
    with _apps.serve(page, _apps.registration_app()) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]
    assert steps[1].outcomes == ("2xx",)
    app2 = _apps.registration_app(redirect="/login")
    out = _replay_commit(page, steps, app2, "r302")
    assert out.clicked and out.result.status is not Status.HELD
    assert out.result.status is Status.REFUTED, out.result.reason
    assert "3xx /login" in out.result.reason
    assert not app2.state.get("records")


def test_a_redirect_to_another_path_than_the_demo_is_not_held(page) -> None:
    """Demo POST answers 302 to /done; replay answers 302 to /login."""
    with _apps.serve(page, _apps.registration_app(redirect="/done")) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]
    assert steps[1].outcomes == ("3xx /done",)
    app2 = _apps.registration_app(redirect="/login")
    out = _replay_commit(page, steps, app2, "r302b")
    assert out.clicked and out.result.status is Status.REFUTED, out.result.reason
    assert not app2.state.get("records")
    app3 = _apps.registration_app(redirect="/done")
    out = _replay_commit(page, steps, app3, "r302c")
    assert out.held, out.result.reason
    assert app3.state["records"] == [
        {"name": "Bo Sample", "email": "bo@example.test", "ticket": "vip"}
    ]


def test_a_commit_spec_without_recorded_outcomes_is_never_clicked(page) -> None:
    with _apps.serve(page, _apps.registration_app()) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]
    old = StepSpec(steps[1].step_id, steps[1].control, StepRole.COMMIT, 1)
    app2 = _apps.registration_app()
    out = _replay_commit(page, [steps[0], old], app2, "rold")
    assert not out.clicked and out.result.status is Status.NOT_CHECKED
    assert not app2.state.get("records")


def test_a_missing_renamed_or_doubled_link_is_never_clicked(page) -> None:
    spec = StepSpec("next", ControlSpec("link", "Continue"), StepRole.NAVIGATE)
    app = _apps.registration_app()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/contact")
        page.evaluate("document.getElementById('next').textContent = 'Next'")
        out = take_step(act, spec)
        assert not out.clicked and out.result.status is Status.NOT_CHECKED
        page.evaluate(
            "document.getElementById('next').textContent = 'Continue';"
            "document.querySelector('nav').insertAdjacentHTML('beforeend',"
            "'<a href=\"/elsewhere\">Continue</a>')"
        )
        out = take_step(act, spec)
        assert not out.clicked and "2 controls match" in out.result.reason
        pinned = StepSpec(
            "next", ControlSpec("link", "Continue", target="/ticket"), StepRole.NAVIGATE
        )
        page.evaluate("document.getElementById('next').setAttribute('href','/other')")
        out = take_step(act, pinned)
        assert not out.clicked
    assert ("GET", "/elsewhere") not in app.log and ("GET", "/other") not in app.log


# -- EMR-like chart: the commit is a GET link ----------------------------------------


def _record_chart(page, app, *, write_get_paths=()) -> list:
    act = PageActuator(page, write_get_paths=write_get_paths)
    page.goto(f"{app.origin}/patient/7")
    rec = Recorder(act)
    s = act.observe()
    rec.set_text(_handle(s, "textbox", "Note"), "Seen today; stable.")
    rec.click(_handle(s, "button", "Save draft"))
    rec.click(_handle(act.observe(), "link", "Sign note"))
    return rec.steps


def test_emr_sign_link_is_a_commit_only_when_its_request_is_declared(page) -> None:
    with _apps.serve(page, _apps.chart_app()) as app:
        declared = learn_steps(
            _record_chart(page, app, write_get_paths=[r"^/notes/\d+/sign$"])
        )
    assert [(s.control.name, s.role) for s in declared] == [
        ("Save draft", StepRole.COMMIT),
        ("Sign note", StepRole.COMMIT),
    ]
    assert [s.control.name for s in commit_points(declared)] == [
        "Save draft",
        "Sign note",
    ]
    with _apps.serve(page, _apps.chart_app()) as app:
        undeclared = learn_steps(_record_chart(page, app))
    # A GET the backend does not count is not evidence of a commit: the link
    # is learned as a navigation, and the program has one commit point fewer.
    assert [(s.control.name, s.role) for s in undeclared][1] == (
        "Sign note",
        StepRole.NAVIGATE,
    )


def test_emr_learned_sign_link_replays_and_signs(page) -> None:
    paths = [r"^/notes/\d+/sign$"]
    with _apps.serve(page, _apps.chart_app()) as app:
        steps = [
            s.spec for s in learn_steps(_record_chart(page, app, write_get_paths=paths))
        ]
    app2 = _apps.chart_app()
    with _apps.serve(page, app2):
        act = PageActuator(page, write_get_paths=paths)
        page.goto(f"{app2.origin}/patient/7")
        act.set_text(_handle(act.observe(), "textbox", "Note"), "Follow up in 2 weeks.")
        assert take_step(act, steps[0]).held
        out = take_step(act, steps[1])
        assert out.held, out.result.reason
    assert app2.state["signed"] == "Follow up in 2 weeks."


# -- SaaS settings: tabs move between panels, a link publishes ------------------------


def test_saas_tabs_navigate_and_a_publish_link_commits(page) -> None:
    app = _apps.settings_app()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/settings")
        rec = Recorder(act)
        s = act.observe()
        rec.set_text(_handle(s, "textbox", "Workspace name"), "North team")
        rec.click(_handle(s, "tab", "Billing"))
        s = act.observe()
        rec.set_text(_handle(s, "textbox", "Billing email"), "ap@example.test")
        rec.click(_handle(s, "link", "Publish changes"))
        learned = learn_steps(rec.steps)
    assert [(s.control.role, s.role) for s in learned] == [
        ("tab", StepRole.NAVIGATE),
        ("link", StepRole.COMMIT),
    ]
    assert app.state["settings"] == {"ws": "North team", "be": "ap@example.test"}


# -- spreadsheet-like grid: no browser -------------------------------------------------


def _grid() -> GridApp:
    return GridApp(
        [
            Sheet("Q1", ("Item", "Amount"), [["Rent", "1200"]]),
            Sheet("Q2", ("Item", "Amount"), [["Rent", ""]]),
        ]
    )


def test_grid_sheet_tab_navigates_and_save_menu_item_commits() -> None:
    grid = _grid()
    rec = Recorder(grid)
    rec.click("tab:1")
    rec.set_text("cell:Q2:0:1", "1250")
    rec.click("tool:save")
    learned = learn_steps(rec.steps)
    assert [(s.control.role, s.control.name, s.role) for s in learned] == [
        ("tab", "Q2", StepRole.NAVIGATE),
        ("menuitem", "Save", StepRole.COMMIT),
    ]
    replay = _grid()
    for step in learned:
        assert take_step(replay, step.spec).held
    assert replay.saved[-1]["Q2"] == [["Rent", ""]]


def test_a_backend_that_cannot_count_writes_never_takes_a_step() -> None:
    grid = _grid()
    rec = Recorder(grid)
    rec.click("tab:1")
    rec.click("tool:save")
    tab, save = learn_steps(rec.steps)
    blind = _grid()
    blind.count_writes = False
    for step in (save, tab):
        out = take_step(blind, step.spec)
        assert not out.clicked and out.result.status is Status.NOT_CHECKED
    assert blind.saved == [] and blind.active == 0
    rec2 = Recorder(blind)
    rec2.click("tool:save")
    (unknown,) = learn_steps(rec2.steps)
    assert unknown.role is StepRole.UNKNOWN and unknown.spec is None


# -- the verdict and the form_tx adapter ------------------------------------------------


def test_an_in_place_step_that_moves_the_screen_is_refuted() -> None:
    ev = StepEvidence("s", StepRole.IN_PLACE, 0, 2, 2, True, True)
    assert judge_step(ev).status is Status.REFUTED
    held = StepEvidence("s", StepRole.IN_PLACE, 0, 2, 2, False, True)
    assert judge_step(held).status is Status.HELD


def test_judge_step_needs_exact_whole_counts() -> None:
    base = dict(
        step_id="s",
        role=StepRole.COMMIT,
        expected_writes=1,
        writes_before=3,
        writes_after=4,
        moved=True,
        after_complete=True,
        expected_outcomes=("2xx",),
        outcomes=("2xx",),
    )
    assert judge_step(StepEvidence(**base)).status is Status.HELD
    for change in (
        {"outcomes": ("3xx /login",)},
        {"outcomes": None},
        {"outcomes": "2xx"},
        {"outcomes": ("2xx", "2xx")},
        {"expected_outcomes": None},
        {"expected_outcomes": ()},
        {"writes_after": "4"},
        {"writes_after": True},
        {"writes_before": None},
        {"writes_after": 2},
        {"role": "submit"},
        {"expected_writes": 0},
        {"after_complete": False},
    ):
        got = judge_step(StepEvidence(**{**base, **change}))
        assert got.status is not Status.HELD, change


def test_form_snapshot_links_become_link_controls() -> None:
    from openadapt_flow.form_tx.types import FormSnapshot, ObservedControl

    snap = FormSnapshot(
        url_origin_and_path="/hub",
        controls=(
            ObservedControl(
                control_id="a1", dom_selector="#a1", tag="a", label_text="Continue"
            ),
            ObservedControl(
                control_id="b1", dom_selector="#b1", tag="button", label_text="Back"
            ),
        ),
    )
    screen = screen_from_form_snapshot(snap)
    assert [(c.role, c.name) for c in screen.controls] == [
        ("link", "Continue"),
        ("button", "Back"),
    ]
    spec = ControlSpec("activatable", "Continue")
    assert match_one(spec, screen).handle == "#a1"


def test_commit_server_500_without_alert_is_refuted(page) -> None:
    """A 500 response on a commit POST must refute even with no role=alert."""
    with _apps.serve(page, _apps.registration_app()) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]
    app2 = _apps.registration_app()
    with _apps.serve(page, app2):
        act = PageActuator(page)
        page.goto(f"{app2.origin}/contact")
        s = act.observe()
        act.set_text(_handle(s, "textbox", "Full name"), "Bo Sample")
        act.set_text(_handle(s, "textbox", "Email"), "bo@example.test")
        assert take_step(act, steps[0], run_id="r500").held
        page.select_option(_handle(act.observe(), "combobox", "Ticket type"), "vip")
        page.route(
            f"{app2.origin}/register",
            lambda route: route.fulfill(
                status=500,
                content_type="text/html",
                body="<html><body><h1>Internal Server Error</h1></body></html>",
            ),
        )
        try:
            out = take_step(act, steps[1], run_id="r500")
        finally:
            page.unroute(f"{app2.origin}/register")
    assert out.clicked and out.result.status is Status.REFUTED
    assert "HTTP 500" in out.result.reason
    assert act.write_count() == 0
    assert not app2.state.get("records")


def test_commit_third_party_beacon_does_not_count_as_write_or_commit(page) -> None:
    """A third-party beacon POST must neither satisfy a commit nor teach one."""
    with _apps.serve(page, _apps.registration_app()) as app:
        steps = [s.spec for s in learn_steps(_record_registration(page, app))]

    beacon_hits: list[str] = []
    page.route(
        "http://analytics.test/collect",
        lambda route: (
            beacon_hits.append(route.request.method),
            route.fulfill(
                status=200,
                headers={"Access-Control-Allow-Origin": "*"},
                body="ok",
            ),
        ),
    )
    try:
        app2 = _apps.registration_app()
        with _apps.serve(page, app2):
            act = PageActuator(page)
            page.goto(f"{app2.origin}/contact")
            s = act.observe()
            act.set_text(_handle(s, "textbox", "Full name"), "Bo Sample")
            act.set_text(_handle(s, "textbox", "Email"), "bo@example.test")
            assert take_step(act, steps[0], run_id="rbeacon").held
            page.select_option(_handle(act.observe(), "combobox", "Ticket type"), "vip")
            # Replace submit with a third-party beacon that prevents the real POST.
            page.evaluate(
                """() => {
                    const form = document.querySelector('form');
                    form.addEventListener('submit', (ev) => {
                        ev.preventDefault();
                        fetch('http://analytics.test/collect', {method: 'POST', body: 'ping'});
                    });
                }"""
            )
            out = take_step(act, steps[1], run_id="rbeacon")
        assert beacon_hits == ["POST"]
        assert out.clicked and out.result.status is Status.REFUTED
        assert act.write_count() == 0
        assert not app2.state.get("records")

        # Also verify learn_steps does not learn a third-party-only beacon as COMMIT.
        app3 = _apps.registration_app()
        with _apps.serve(page, app3):
            act3 = PageActuator(page)
            page.goto(f"{app3.origin}/ticket")
            page.evaluate(
                """() => {
                    const form = document.querySelector('form');
                    form.addEventListener('submit', (ev) => {
                        ev.preventDefault();
                        fetch('http://analytics.test/collect', {method: 'POST', body: 'demo'});
                    });
                }"""
            )
            rec = Recorder(act3)
            rec.click(_handle(act3.observe(), "button", "Register"))
            learned = learn_steps(rec.steps)
        assert len(learned) == 1
        assert learned[0].role is not StepRole.COMMIT
        assert act3.write_count() == 0
    finally:
        page.unroute("http://analytics.test/collect")
