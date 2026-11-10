"""A click that only opens a confirmation dialog is not the save."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.policy import (
    effective_step_risk,
    evaluate_policy,
    load_policy,
    step_tags,
)
from openadapt_flow.risk import (
    classify_step_risk,
    confirmation_opener_step_ids,
    infer_step_risk,
    opener_explanation,
)


def _click(step_id: str, label: str, selector: str = "button") -> Step:
    step = Step(
        id=step_id,
        intent=f"click '{label}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(0, 0, 160, 64),
            click_point=(80, 32),
            ocr_text=label,
            structural=StructuralLocator(selector=selector, role="button", name=label),
        ),
    )
    step.risk = infer_step_risk(step).risk
    return step


def _type(step_id: str) -> Step:
    return Step(
        id=step_id, intent="type <fname>", action=ActionKind.TYPE, param="fname"
    )


def test_openemr_referral_create_then_confirm():
    """The referral recording: "Create New Patient" opens the duplicate check,
    and its "Confirm" button creates the chart."""

    create = _click("step_010", "Create New Patient", "#create")
    confirm = _click("step_011", "Confirm", "#confirmCreate")
    assert create.risk == confirm.risk == "irreversible"
    assert confirmation_opener_step_ids([_type("step_009"), create, confirm]) == (
        ("step_010", "step_011"),
    )


@pytest.mark.parametrize("label", ["Yes", "Confirm Create New Patient", "yes, delete"])
def test_confirm_shaped_labels_match(label):
    pair = [_click("a", "Submit claim"), _click("b", label)]
    if pair[1].risk != "irreversible":
        pytest.skip("label not inferred as a write")
    assert confirmation_opener_step_ids(pair) == (("a", "b"),)


@pytest.mark.parametrize("label", ["OK", "Close", "Done", "Back"])
def test_acknowledgements_never_move_the_save(label):
    assert confirmation_opener_step_ids([_click("a", "Save"), _click("b", label)]) == ()


def test_a_step_between_them_breaks_the_pair():
    steps = [_click("a", "Create New Patient"), _type("t"), _click("b", "Confirm")]
    assert confirmation_opener_step_ids(steps) == ()


def test_an_opener_with_its_own_effect_or_binding_stays_the_save():

    opener = _click("a", "Create New Patient")
    opener.effects = [object()]  # type: ignore[list-item]
    assert confirmation_opener_step_ids([opener, _click("b", "Confirm")]) == ()


def test_two_confirms_in_a_row_keep_the_first():
    steps = [_click("a", "Confirm"), _click("b", "Confirm")]
    assert confirmation_opener_step_ids(steps) == ()


def _annotated_opener(opener: Step, confirm_id: str, dialog_text: str) -> Step:
    opener.risk = "reversible"
    opener.risk_explanation = opener_explanation(confirm_id)
    opener.risk_review_required = False
    opener.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=dialog_text)
    ]
    return opener


def test_screen_check_does_not_prove_a_nonwriting_opener():
    """Even dialog-shaped text cannot prove that an opener did not write."""

    opener = _annotated_opener(
        _click("step_039", "Create New Patient", "#create"),
        "step_040",
        "Confirm Create New Patient",
    )
    assert infer_step_risk(opener).risk == "irreversible"
    assert effective_step_risk(opener) == "irreversible"
    assert "write" in step_tags(opener)


def test_explanation_alone_is_not_a_seal():
    """Without a dialog postcondition, write-shaped labels stay irreversible."""

    opener = _click("step_039", "Create New Patient", "#create")
    opener.risk = "reversible"
    opener.risk_explanation = opener_explanation("step_040")
    assert infer_step_risk(opener).risk == "irreversible"
    assert effective_step_risk(opener) == "irreversible"
    assert "write" in step_tags(opener)


def test_annotated_opener_with_own_effect_stays_a_write():
    opener = _annotated_opener(
        _click("a", "Create New Patient"),
        "b",
        "Confirm",
    )
    opener.effects = [object()]  # type: ignore[list-item]
    assert infer_step_risk(opener).risk == "irreversible"


@pytest.mark.parametrize(
    "action", [ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK]
)
@pytest.mark.parametrize(
    "postcondition",
    [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Ready"),
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Confirm record"),
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="No matches were found.",
            exact=True,
        ),
        Postcondition(kind=PostconditionKind.NO_DUPLICATE_CANDIDATE),
        Postcondition(
            kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
            candidate_rows="[data-candidate]",
            candidate_params=("record_id",),
        ),
    ],
    ids=["status", "dialog-text", "exact-empty-text", "empty-candidates", "candidates"],
)
@pytest.mark.parametrize("relation", ["missing", "adjacent", "separated"])
@pytest.mark.parametrize("require_current", [False, True])
def test_screen_evidence_cannot_waive_write_contracts(
    action: ActionKind,
    postcondition: Postcondition,
    relation: str,
    require_current: bool,
) -> None:
    opener = _annotated_opener(_click("save", "Save"), "confirm", "Ready")
    opener.action = action
    opener.expect = [postcondition]
    assert opener.anchor is not None
    opener.anchor.context_text = "Synthetic record A"
    opener.identity_armed = True
    steps = [opener]
    if relation == "separated":
        steps.append(Step(id="wait", intent="wait", action=ActionKind.WAIT))
    if relation != "missing":
        steps.append(_click("confirm", "Confirm"))
    workflow = Workflow(name="confirmation-write-contract", steps=steps)

    assert infer_step_risk(opener).risk == "irreversible"
    assert classify_step_risk(opener) == "irreversible"
    assert effective_step_risk(opener, workflow) == "irreversible"
    assert "write" in step_tags(opener, workflow)
    report = evaluate_policy(
        workflow,
        load_policy("clinical-write"),
        require_current_risk_certification=require_current,
    )
    assert not report.passed
    assert {"require_system_effects_for", "require_idempotency_key_for"} <= {
        violation.rule for violation in report.violations if violation.step_id == "save"
    }


@pytest.fixture
def writing_dialog() -> Iterator[tuple[str, list[dict[str, int]]]]:
    """A generic click that stores a row and also opens a real dialog."""
    rows: list[dict[str, int]] = []
    html = b"""<!doctype html><title>Synthetic records</title><p>Ready</p>
<button id="save" onclick="fetch('/save',{method:'POST'}).then(r=>r.json()).then(r=>{
 document.querySelector('#count').textContent=r.count;
 document.querySelector('#dialog').hidden=false;
})">Save</button><output id="count">0</output>
<div id="dialog" role="dialog" hidden><p>Confirm record</p>
<button id="confirm">Confirm</button></div>"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self) -> None:
            if self.path != "/save":
                self.send_error(404)
                return
            rows.append({"id": len(rows) + 1})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": len(rows)}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", rows
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("text", ["Ready", "Confirm record"])
def test_actual_write_can_also_satisfy_confirmation_screen_evidence(
    writing_dialog: tuple[str, list[dict[str, int]]], tmp_path: Path, text: str
) -> None:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.replayer import Replayer

    url, rows = writing_dialog
    opener = _annotated_opener(_click("save", "Save", "#save"), "confirm", text)
    assert opener.anchor is not None
    opener.anchor.context_text = "Synthetic record A"
    opener.identity_armed = True
    workflow = Workflow(
        name="writing-confirmation-opener",
        steps=[opener, _click("confirm", "Confirm", "#confirm")],
    )
    report = evaluate_policy(
        workflow,
        load_policy("clinical-write"),
        require_current_risk_certification=True,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url)
            backend = PlaywrightBackend(page)
            replayer = Replayer(backend)
            assert rows == []
            assert not page.get_by_role("dialog").is_visible()
            if text == "Ready":
                assert replayer._postcondition_passes(
                    opener.expect[0], backend.screenshot(), tmp_path
                )
            box = page.locator("#save").bounding_box()
            assert box is not None
            backend.click(
                round(box["x"] + box["width"] / 2),
                round(box["y"] + box["height"] / 2),
            )
            page.wait_for_function(
                "document.querySelector('#count').textContent === '1'"
            )
            assert rows == [{"id": 1}]
            assert page.get_by_role("dialog").is_visible()
            assert page.get_by_role("button", name="Confirm", exact=True).is_visible()
            assert replayer._postcondition_passes(
                opener.expect[0], backend.screenshot(), tmp_path
            )
            assert not report.passed
            assert {"require_system_effects_for", "require_idempotency_key_for"} <= {
                violation.rule
                for violation in report.violations
                if violation.step_id == "save"
            }
        finally:
            browser.close()
