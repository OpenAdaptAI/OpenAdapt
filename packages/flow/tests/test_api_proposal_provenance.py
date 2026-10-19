"""API step fields are bound by which step typed them, not by equal values.

The reviewer's cases for #1269: a blank demonstration value bound as the
form's default, so a later run's value was silently dropped; and equal
demonstration values made the binding ambiguous. Synthetic values only.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.compiler.api_proposal import propose_api_step
from openadapt_flow.runtime.actuators.web_form import WebFormActuator, form_controls
from tests.test_api_tier_web_form import FORM_HTML, _recording, _workflow


def _save(**fields):
    form = {
        "csrf_token_form": "<session:csrf_token_form>",
        "form_lname": "Marsh",
        "form_state": "MA",
        "form_note": "Referred",
        "form_extra": "",
        "form_status": "active",
    }
    form.update(fields)
    return [[k, v] for k, v in form.items()]


def _with_params(rec, **params):
    meta = json.loads((rec / "meta.json").read_text())
    meta["params"].update(params)
    (rec / "meta.json").write_text(json.dumps(meta))
    return rec


def _fields(step):
    return {f.name: f for f in step.api_binding.web_form.fields}


def test_a_blank_demonstration_value_is_still_the_parameter(tmp_path):
    rec = _with_params(_recording(tmp_path, save_form=_save(form_note="")), note="")
    wf = _workflow()
    wf.params["note"] = ""
    _wf, step, reasons = propose_api_step(wf, rec)
    assert step is not None, reasons
    note = _fields(step)["form_note"]
    assert (note.source, note.param) == ("param", "note")
    # A later run's note is sent, not dropped for the form's empty default.
    controls = form_controls(
        FORM_HTML.replace("<session:csrf_token_form>", "live"), "/app/save.php"
    )
    body, problem = WebFormActuator.build_body(
        step.api_binding.web_form,
        {"lname": "Marsh", "state_label": "Massachusetts", "note": "New note"},
        controls,
    )
    assert problem is None and ("form_note", "New note") in body


def test_equal_demonstration_values_bind_each_field_to_its_own_step(tmp_path):
    rec = _with_params(
        _recording(tmp_path, save_form=_save(form_note="Marsh")), note="Marsh"
    )
    wf = _workflow()
    wf.params["note"] = "Marsh"
    _wf, step, reasons = propose_api_step(wf, rec)
    assert step is not None, reasons
    fields = _fields(step)
    assert fields["form_lname"].param == "lname"
    assert fields["form_note"].param == "note"


def test_a_field_carrying_a_value_no_step_typed_there_is_refused(tmp_path):
    rec = _recording(tmp_path, save_form=_save() + [["form_copy", "Marsh"]])
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is None
    assert "no recorded step typed it there" in " ".join(reasons)


def test_a_typed_field_sent_with_another_value_is_refused(tmp_path):
    rec = _recording(tmp_path, save_form=_save(form_note="REFERRED"))
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is None
    assert "sent another value" in " ".join(reasons)


def test_a_typed_field_the_save_does_not_send_is_refused(tmp_path):
    form = [row for row in _save() if row[0] != "form_note"]
    rec = _recording(tmp_path, save_form=form)
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is None
    assert "doesn't send it" in " ".join(reasons)


@pytest.mark.parametrize("selector", ["#nowhere", None])
def test_a_typed_step_whose_field_is_unknown_is_refused(tmp_path, selector):
    rec = _recording(tmp_path)
    events = [json.loads(x) for x in (rec / "events.jsonl").read_text().splitlines()]
    events[4]["structural"] = {"selector": selector, "role": "textbox", "name": "?"}
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is None
    assert "isn't a control of the form" in " ".join(reasons)
