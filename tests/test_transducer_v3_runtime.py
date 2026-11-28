from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openadapt.simplex_transducer import (
    ReceiptOutcome,
    SimplexTransducer,
    SupervisorConfig,
)
from openadapt.transducer_obligations import ObligationStatus


def _wrap(*actions, **options):
    agent = Mock()
    agent.act.side_effect = actions
    return SimplexTransducer(agent, **options), agent


def _observation(**raw):
    return {"url": "https://example.test/records", "raw_observation": raw}


def test_intermediate_form_drafts_each_field_and_done_checks_missing_obligations():
    form = {
        "type": "fill_form",
        "raw_action": {
            "form_plan": {
                "fields": [
                    {"selector": "#title", "name": "Title", "value": "Update CLM-17"},
                    {"selector": "#note", "name": "Note", "value": "See REF-20."},
                ]
            }
        },
    }
    repair = {"type": "click", "target_name": "Open record"}
    wrapped, agent = _wrap(
        form,
        {"type": "done"},
        repair,
        config=SupervisorConfig(enable_obligation_compiler=True),
    )
    task = {"instruction": "Record CLM-17, REF-20 and ORD-42."}

    assert wrapped.act(_observation(), task) is form
    state = wrapped.get_state("default-run", "default-task")
    assert {ob.expected_value: ob.status for ob in state.v3_ep.obligations} == {
        "CLM-17": ObligationStatus.DRAFTED,
        "REF-20": ObligationStatus.DRAFTED,
        "ORD-42": ObligationStatus.PENDING,
    }
    agent.on_supervisor_intervention.assert_not_called()

    assert (
        wrapped.act(_observation(readback_text="Saved CLM-17, REF-20."), task) is repair
    )
    hint = agent.on_supervisor_intervention.call_args.kwargs["hint"]
    assert "HOLD_DONE" in hint and "ORD-42" in hint


@pytest.mark.parametrize(
    "field, problem",
    [
        ({"selector": None, "name": "Note", "value": "REF-20"}, "empty selector"),
        ({"selector": "#title", "name": "Title", "value": None}, "blank value"),
        (
            {"selector": "#title", "name": "Title", "value": "sample"},
            "expected exactly",
        ),
        (None, "plan has no fields"),
    ],
)
def test_invalid_form_repairs_are_validated_and_do_not_draft(field, problem):
    fields = (
        []
        if field is None
        else [
            {"selector": "#note", "name": "Details", "value": "REF-20"},
            field,
        ]
    )
    form = {"type": "fill_form", "raw_action": {"fields": fields}}
    wrapped, agent = _wrap(
        form,
        form,
        pinned={"Title": "Sample"},
        config=SupervisorConfig(enable_obligation_compiler=True),
    )

    action = wrapped.act(_observation(), {"instruction": "Record REF-20."})
    assert action.is_abstained and action.raw_action["trap_code"] == "FORM_PLAN_INVALID"
    assert problem in action.raw_action["reason"]
    assert agent.act.call_count == 2
    state = wrapped.get_state("default-run", "default-task")
    assert state.v3_ep.obligations[0].status is ObligationStatus.PENDING


def test_done_audits_entity_documents_and_uses_task_prompt_to_scope_worklists():
    worklist = "CLM-1 | CLM-2 | CLM-3 | CLM-4"
    document = "Record CLM-3 has supporting reference REF-20."
    instruction = "Open CLM-3 and record its supporting reference."
    task = {"instruction": "Open CLM-4", "raw_config": {"task_prompt": instruction}}
    auditor = Mock(return_value={"complete": True, "missing": []})
    wrapped, _ = _wrap(
        {"type": "click"},
        {"type": "click"},
        {"type": "done"},
        completeness_auditor=auditor,
    )
    state = wrapped.get_state("default-run", "default-task")

    wrapped.act(_observation(document_text=worklist), task)
    assert [ob.expected_value for ob in state.v3_ep.obligations] == ["CLM-3"]
    assert state.v3_ep.intake_docs == []
    wrapped.act(_observation(document_text=document), task)
    readback = "Saved CLM-3 and REF-20."
    wrapped.act(_observation(document_text=document, readback_text=readback), task)

    assert state.v3_ep.intake_docs == [document]
    assert [ob.expected_value for ob in state.v3_ep.obligations] == ["CLM-3", "REF-20"]
    auditor.assert_called_once_with(f"{instruction}\n\n{document}", readback)
    assert wrapped.last_receipt.outcome == ReceiptOutcome.VERIFIED


def test_modal_focus_hint_lists_controls_once_per_step():
    wrapped, agent = _wrap({"type": "click"}, {"type": "click"})
    observation = _observation(
        open_modal_with_inputs=True, modal_controls=["#note", "Save note"]
    )
    task = {"instruction": "Write a note."}
    wrapped.act(observation, task)
    state = wrapped.get_state("default-run", "default-task")
    wrapped._v3.on_context(state, observation, task)

    agent.on_supervisor_intervention.assert_called_once()
    intervention = state.intervention_log[0]
    assert intervention.kind == "MODAL_FOCUS_HINT"
    assert "modal dialog is open" in intervention.hint
    assert "background elements are occluded" in intervention.hint
    assert "#note, Save note" in intervention.hint
    assert (
        agent.on_supervisor_intervention.call_args.kwargs["fast_forward_obs"]
        is observation
    )

    wrapped.act(observation, task)
    assert [note.step_index for note in state.intervention_log] == [1, 2]


@pytest.mark.parametrize(
    "observation, expected",
    [
        (_observation(last_error="Modal backdrop intercepts pointer-events"), True),
        (_observation(modal_open=True, last_error="Click timed out"), True),
        (dict(_observation(), last_error="Timeout while a dialog is open"), True),
        (_observation(last_error="Header intercepts pointer events"), False),
        (_observation(last_error="Navigation timeout"), False),
        (_observation(modal_open=True, last_error="Unexpected navigation"), False),
    ],
)
def test_modal_focus_hint_handles_interception_and_timeout(observation, expected):
    wrapped, _ = _wrap({"type": "click"})
    wrapped.act(observation, {"instruction": "Write a note."})
    state = wrapped.get_state("default-run", "default-task")
    assert [note.kind for note in state.intervention_log] == (
        ["MODAL_FOCUS_HINT"] if expected else []
    )


@pytest.mark.parametrize(
    "signature, expected",
    [
        ("legacy", {}),
        ("terminal_only", {"terminal_output": "saved file"}),
        ("extended", {"terminal_output": "saved file", "text_digest": "page-a"}),
        ("kwargs", {"terminal_output": "saved file", "text_digest": "page-a"}),
    ],
)
def test_stall_detector_receives_only_supported_optional_inputs(signature, expected):
    captured = Mock(return_value=None)

    def legacy(url, dom, shot, action):
        return captured(url, dom, shot, action)

    def terminal_only(url, dom, shot, action, *, terminal_output):
        return captured(url, dom, shot, action, terminal_output=terminal_output)

    def extended(url, dom, shot, action, *, terminal_output, text_digest):
        return captured(
            url,
            dom,
            shot,
            action,
            terminal_output=terminal_output,
            text_digest=text_digest,
        )

    def kwargs(url, dom, shot, action, **options):
        return captured(url, dom, shot, action, **options)

    observe_step = {
        "legacy": legacy,
        "terminal_only": terminal_only,
        "extended": extended,
        "kwargs": kwargs,
    }[signature]
    wrapped, _ = _wrap({"type": "click", "target_name": "Open"})
    state = wrapped.get_state("default-run", "default-task")
    wrapped._v3.ep(state).detector = SimpleNamespace(observe_step=observe_step)
    dom = [{"tag": "button", "selector": "#open"}]
    observation = _observation(dom_elements=dom, terminal_output="saved file")
    observation.update(screenshot=b"frame", text_digest="page-a")

    wrapped.act(observation, {"instruction": "Open the record."})

    captured.assert_called_once_with(
        observation["url"],
        dom,
        b"frame",
        {"action": "click", "selector": "Open", "value": ""},
        **expected,
    )
