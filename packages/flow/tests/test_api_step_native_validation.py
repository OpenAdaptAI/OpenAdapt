"""The API step enforces what the browser would check before it submits.

The reviewer's case for #1269: only validate.js ``presence`` rules were
enforced. A browser also checks native HTML constraints (required, pattern,
min, max, maxlength, type), and a form's own script can validate or rewrite
values; the API step either evaluates them or leaves the save to the GUI.
Synthetic values only.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import propose_api_step
from openadapt_flow.ir import WebFormField, WebFormSpec
from openadapt_flow.runtime.actuators import ActuationStatus
from openadapt_flow.runtime.actuators.web_form import (
    WebFormActuator,
    client_rule_problem,
    form_controls,
    native_problem,
)
from tests.test_api_tier_web_form import (
    FORM_HTML,
    FakeTransport,
    _admitted_binding,
    _recording,
    _workflow,
)

NATIVE = """<form action='save.php'>
  <input name="code" required pattern="[A-Z]{3}-\\d{2}" maxlength="6">
  <input name="email" type="email">
  <input name="dose" type="number" min="1" max="10">
  <input name="seen" type="date" max="2026-10-19">
  <input type="hidden" name="h" value="">
</form>"""


def _controls():
    return form_controls(NATIVE, "/save.php")


@pytest.mark.parametrize(
    "name, value, problem",
    [
        ("code", "", "required"),
        ("code", "abc-12", "pattern"),
        ("code", "ABC-123", "maxlength"),
        ("email", "not-an-address", "email"),
        ("dose", "11", "max"),
        ("dose", "0", "min"),
        ("dose", "ten", "number"),
        ("seen", "2026-10-20", "max"),
        ("seen", "10/19/2026", "date"),
    ],
)
def test_native_constraints_refuse_what_the_browser_would(name, value, problem):
    found = native_problem(name, _controls()[name], value)
    assert found is not None and problem in found


@pytest.mark.parametrize(
    "name, value",
    [
        ("code", "ABC-12"),
        ("email", "a@b.example"),
        ("dose", "5"),
        ("seen", "2026-10-01"),
        ("h", ""),
    ],
)
def test_native_constraints_accept_valid_values(name, value):
    assert native_problem(name, _controls()[name], value) is None


def test_validate_js_length_format_and_numericality_are_enforced():
    rule = {"length": {"maximum": 3}, "format": {"pattern": "[a-z]+"}}
    assert "length" in client_rule_problem("f", rule, "abcd")
    assert "format" in client_rule_problem("f", rule, "AB")
    assert client_rule_problem("f", {"numericality": True}, "x") is not None
    assert client_rule_problem("f", rule, "ab") is None


def test_the_actuator_refuses_a_value_the_browser_would_refuse(tmp_path):
    spec = WebFormSpec(
        form_path="/new.php",
        fields=[WebFormField(name="code", source="param", param="code")],
        controls=["code", "dose", "email", "h", "seen"],
        replaces_steps=["s0"],
        folded_checks_digest="sha256:" + "0" * 64,
    )
    body, problem = WebFormActuator.build_body(spec, {"code": "abc"}, _controls())
    assert body == [] and "pattern" in problem


def _rec_with_form(tmp_path, html):
    rec = _recording(tmp_path)
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    for row in rows[1:]:
        if "<form" in (row.get("response_text") or ""):
            row["response_text"] = html
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rec


@pytest.mark.parametrize(
    "edit, named",
    [
        (
            lambda h: h.replace(
                "<form action='save.php'",
                "<form action='save.php' onsubmit='return check()'",
            ),
            "form onsubmit",
        ),
        (
            lambda h: h.replace(
                '<input name="form_lname"',
                '<input onchange="capitalizeMe(this)" name="form_lname"',
            ),
            "form_lname onchange",
        ),
        (
            lambda h: (
                h + "<script>document.forms[0].addEventListener('submit', v)</script>"
            ),
            "listening for submit",
        ),
        (
            lambda h: h.replace(
                '{"form_lname":{"presence":true}}',
                '{"form_lname":{"presence":true,"pastDate":{}}}',
            ),
            "form_lname pastDate",
        ),
    ],
)
def test_script_the_api_step_cant_run_keeps_the_save_on_the_gui(tmp_path, edit, named):
    html = edit(FORM_HTML)
    _wf, step, reasons = propose_api_step(_workflow(), _rec_with_form(tmp_path, html))
    assert step is None and named in " ".join(reasons)
    # The same script on the live form makes an admitted API step unavailable.
    live = edit(FORM_HTML.replace("<session:csrf_token_form>", "live-session-value"))
    (tmp_path / "admitted").mkdir()
    out = WebFormActuator(FakeTransport(form=live)).actuate(
        _admitted_binding(tmp_path / "admitted"),
        {"lname": "Marsh", "state_label": "California", "note": "Referred"},
    )
    assert out.status is ActuationStatus.UNAVAILABLE
    # A rule kind the recording didn't have is first a changed rule set.
    assert named in out.reason or "rules changed" in out.reason
