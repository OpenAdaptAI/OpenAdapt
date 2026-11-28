from __future__ import annotations

import json

import pytest

from openadapt.transducer_form_batch import (
    DeclarativeSopContract,
    FormFieldWrite,
    FormSubmitPlan,
    MicroHandoffCompoundingController,
    SopRule,
    evaluate_readback_against_sop,
    expand_form_plan_to_steps,
    validate_form_plan,
)
from openadapt.transducer_macros import ISOLATION_PERSISTENT, MacroStore
from openadapt.transducer_obligations import (
    CompiledFieldObligation,
    MicroHandoffResolution,
)


def _plan(mrn: str = "MRN-123456", cpt: str = "99213") -> FormSubmitPlan:
    return FormSubmitPlan(
        fields=[
            FormFieldWrite("#mrn", "MRN", mrn),
            FormFieldWrite("#cpt", "CPT", cpt),
            FormFieldWrite("#note", "Note", "Follow-up visit", exact_code=False),
        ],
        submit_selector="#save",
        target_url="/orders/new",
    )


PINNED = {"MRN": "MRN-123456", "CPT": "99213"}


def test_valid_plan_passes():
    r = validate_form_plan(_plan(), pinned_entities=PINNED)
    assert r.ok and not r.problems


@pytest.mark.parametrize(
    "mrn,cpt,bad",
    [("MRN-123457", "99213", "MRN"), ("MRN-123456", "99231", "CPT")],
)
def test_one_char_code_typo_rejected(mrn, cpt, bad):
    r = validate_form_plan(_plan(mrn, cpt), pinned_entities=PINNED)
    assert not r.ok
    assert r.mismatched_fields == [bad]


def test_exact_code_flag_on_unknown_key():
    plan = FormSubmitPlan(
        [FormFieldWrite("#x", "Widget", "AB-1", exact_code=True)], "#go"
    )
    assert not validate_form_plan(plan, pinned_entities={"Widget": "AB-2"}).ok
    assert validate_form_plan(plan, pinned_entities={"Widget": "AB-1"}).ok


def test_blank_value_rejected():
    plan = _plan()
    plan.fields[2].value = "  "
    r = validate_form_plan(plan, pinned_entities=PINNED)
    assert not r.ok and r.blank_fields == ["Note"]


def test_missing_required_obligation():
    obs = [
        CompiledFieldObligation("npi", "id", "NPI", "1234567890"),
        CompiledFieldObligation("mrn", "id", "MRN", "MRN-123456"),
        CompiledFieldObligation("opt", "id", "Opt", "x", required=False),
    ]
    r = validate_form_plan(_plan(), pinned_entities=PINNED, required_obligations=obs)
    assert not r.ok and r.missing_fields == ["NPI"]


def test_empty_plan_rejected():
    assert not validate_form_plan(FormSubmitPlan([], "#s")).ok


def test_expansion_is_fills_then_single_commit():
    steps = expand_form_plan_to_steps(_plan())
    assert [s["action"] for s in steps] == ["fill", "fill", "fill", "click"]
    assert [s["is_commit"] for s in steps] == [False, False, False, True]
    assert steps[-1]["selector"] == "#save"
    assert steps[0]["value"] == "MRN-123456"


def _contract() -> DeclarativeSopContract:
    return DeclarativeSopContract(
        sop_id="prior-auth",
        domain="payer",
        version="2",
        rules=[
            SopRule("R1", "Policy cited", "token", required_tokens=("policy",)),
            SopRule(
                "R2", "Dx code", "pattern", required_patterns=(r"\b[A-Z]\d{2}\.\d\b",)
            ),
            SopRule("R3", "Justification", "length", min_length=40),
        ],
    )


def test_sop_json_round_trip(tmp_path):
    c = _contract()
    path = tmp_path / "sop.json"
    path.write_text(json.dumps(c.to_dict()))
    assert DeclarativeSopContract.from_json_file(path) == c
    assert DeclarativeSopContract.from_dict(c.to_dict()).to_dict() == c.to_dict()


def test_sop_partial_hold_done_names_only_unmet():
    r = evaluate_readback_against_sop(_contract(), "Policy approved")
    assert not r.passed
    assert r.missing_rules == ["R2", "R3"]
    assert r.satisfied_rules == ["R1"]
    assert r.repair_hint.startswith("HOLD_DONE")
    assert "R2" in r.repair_hint and "R3" in r.repair_hint and "R1" not in r.repair_hint


def test_sop_full_pass():
    text = "Policy applies. Diagnosis E11.9 documented with supporting labs and notes."
    r = evaluate_readback_against_sop(_contract(), text)
    assert r.passed and r.missing_rules == [] and r.repair_hint == ""


@pytest.mark.parametrize("flag", ["is_toast", "is_editable"])
def test_sop_rejects_toast_and_editable(flag):
    text = "Policy E11.9 " + "x" * 50
    r = evaluate_readback_against_sop(_contract(), text, **{flag: True})
    assert not r.passed and len(r.missing_rules) == 3


def test_sop_bad_regex_does_not_pass():
    c = DeclarativeSopContract(
        "s", "d", "1", [SopRule("R", "l", "p", required_patterns=("(",))]
    )
    assert not evaluate_readback_against_sop(c, "anything").passed


def test_compounding_run1_human_run2_zero(tmp_path):
    store = MacroStore(tmp_path / "m.json", isolation_mode=ISOLATION_PERSISTENT)
    ctl = MicroHandoffCompoundingController(store)
    calls = []

    def human(ticket):
        calls.append(ticket)
        return MicroHandoffResolution(
            ticket_id=ticket.ticket_id,
            resolved=True,
            learned_selector="#real-submit",
        )

    kw = dict(
        task_id="t1",
        task_kind="submit_form",
        origin_path_pattern="/orders/*",
        reason="stalled",
        blocker_kind="no_progress",
        stalled_selectors=["#old"],
        human_resolver=human,
    )
    o1 = ctl.handle_stall(run_id="r1", **kw)
    assert (o1.autonomy, o1.human_touches, o1.completed) == ("AVC_H", 1, True)
    o2 = ctl.handle_stall(run_id="r2", **kw)
    assert (o2.autonomy, o2.human_touches, o2.used_macro) == ("AVC_0", 0, True)
    assert len(calls) == 1
    assert ctl.human_touches_by_task == {"t1": 1}
    macro = store.get(
        task_id="t1", origin_path_pattern="/orders/*", task_kind="submit_form"
    )
    assert macro.steps[0].selector == "#real-submit" and macro.steps[0].is_commit


def test_unresolved_human_is_blocked_and_not_stored(tmp_path):
    store = MacroStore(tmp_path / "m.json", isolation_mode=ISOLATION_PERSISTENT)
    ctl = MicroHandoffCompoundingController(store)
    o = ctl.handle_stall(
        run_id="r",
        task_id="t",
        task_kind="k",
        origin_path_pattern="/p",
        reason="x",
        blocker_kind="b",
        human_resolver=lambda t: MicroHandoffResolution(t.ticket_id, resolved=False),
    )
    assert o.autonomy == "BLOCKED" and not o.completed
    assert store.get(task_id="t", origin_path_pattern="/p", task_kind="k") is None


def test_controller_requires_persistent_store(tmp_path):
    with pytest.raises(ValueError):
        MicroHandoffCompoundingController(MacroStore(tmp_path / "m.json"))
