"""Reusable semantic subroutines (OpenAdapt#1892, first slice).

A subroutine is a typed, content-addressed unit that workflows embed by
digest. These tests pin its identity, the narrow body it may hold, the
library's version and re-qualification bookkeeping, the per-use admission
refusals, the live boundary checks the runtime enforces through the existing
interpreter, and application-free detection of recurring sequences.

Backend and vision are faked (reused from test_replayer). No model calls.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from openadapt_flow.bundle_validation import validate_workflow
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    StructuralLocator,
    SurfaceSpec,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.subroutines import (
    SUBFLOW_PREFIX,
    Subroutine,
    SubroutineError,
    SubroutineInput,
    SubroutineLibrary,
    SubroutineSignature,
    admit_workflow,
    body_problems,
    embed,
    mine_recurring_sequences,
    parse_subflow_key,
    step_shape,
    subflow_key,
    subroutine_from_steps,
    wrapped_body,
)
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

EVIDENCE = "e" * 64

# -- builders -----------------------------------------------------------------


def text(value: str) -> Predicate:
    return Predicate(kind=PredicateKind.TEXT_PRESENT, text=value)


def key_step(step_id: str, key: str) -> Step:
    return Step(id=step_id, intent=f"press {key}", action=ActionKind.KEY, key=key)


def type_step(step_id: str, param: str) -> Step:
    return Step(id=step_id, intent=f"type {param}", action=ActionKind.TYPE, param=param)


def anchored_click(step_id: str, label: str, role: str = "button") -> Step:
    return Step(
        id=step_id,
        intent=f"click {label}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text=label,
            structural=StructuralLocator(role=role, name=label),
        ),
    )


def signature(**overrides) -> SubroutineSignature:
    fields = dict(
        name="find_record",
        intent="search for a record and open it",
        inputs=(SubroutineInput(name="query"),),
        surface_kind="web",
        entry_checks=(text("Find record"),),
    )
    fields.update(overrides)
    return SubroutineSignature(**fields)


def find_record(**overrides) -> Subroutine:
    steps = overrides.pop(
        "steps",
        [key_step("focus", "F"), type_step("enter", "query"), key_step("go", "Enter")],
    )
    return subroutine_from_steps(signature(**overrides), steps)


def caller(sub: Subroutine, *, name: str = "intake", **fields) -> Workflow:
    base = Workflow(
        name=name,
        surface=fields.pop("surface", "web"),
        param_specs=fields.pop(
            "param_specs", {"query": ParamSpec(name="query", example="x")}
        ),
        **fields,
    )
    embedded, key = embed(base, sub)
    embedded.program = ProgramGraph(
        entry="call",
        states={
            "call": State(
                id="call",
                kind=StateKind.SUBFLOW_CALL,
                subflow=key,
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    return embedded


def ready_library(sub: Subroutine, *workflows: Workflow) -> SubroutineLibrary:
    library = SubroutineLibrary()
    library.publish(sub)
    library.qualify(sub.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    for workflow in workflows:
        library.register(workflow.name, workflow)
    return library


def codes(admission) -> set[str]:
    return {refusal.code for refusal in admission.refusals}


# -- the unit -------------------------------------------------------------------


def test_entry_check_must_read_the_live_screen():
    for weak in (
        Predicate(kind=PredicateKind.PARAM_EQUALS, param="query", value="x"),
        Predicate(kind=PredicateKind.TEXT_ABSENT, text="Error"),
        Predicate(kind=PredicateKind.NOT, operands=[text("Error")]),
        Predicate(kind=PredicateKind.OR, operands=[text("A"), text("B")]),
    ):
        with pytest.raises(ValidationError, match="affirmative entry check"):
            signature(entry_checks=(weak,))
    with pytest.raises(ValidationError):
        signature(entry_checks=())
    # An AND that includes one live check is accepted.
    signature(
        entry_checks=(
            Predicate(
                kind=PredicateKind.AND,
                operands=[
                    text("Find record"),
                    Predicate(kind=PredicateKind.TEXT_ABSENT, text="Error"),
                ],
            ),
        )
    )


def test_digest_is_content_identity():
    a, b = find_record(), find_record()
    assert a.sha256 == b.sha256 and len(a.sha256) == 64
    changed = [
        find_record(entry_checks=(text("Search"),)),
        find_record(surface_kind="windows"),
        find_record(inputs=(SubroutineInput(name="query", type=ParamKind.DATE),)),
        find_record(
            steps=[
                key_step("focus", "G"),
                type_step("enter", "query"),
                key_step("go", "Enter"),
            ]
        ),
        find_record(exit_checks=(text("Record"),)),
    ]
    digests = {a.sha256, *(sub.sha256 for sub in changed)}
    assert len(digests) == 1 + len(changed)


@pytest.mark.parametrize(
    ("step", "fragment"),
    [
        (
            Step(
                id="w",
                intent="save",
                action=ActionKind.KEY,
                key="s",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN, entity="row", match={"id": "1"}
                    )
                ],
            ),
            "system-of-record effect",
        ),
        (
            Step(
                id="w",
                intent="save",
                action=ActionKind.KEY,
                key="s",
                risk="irreversible",
            ),
            "reviewed reversible",
        ),
        (
            Step(
                id="w",
                intent="save",
                action=ActionKind.KEY,
                key="s",
                risk_review_required=True,
            ),
            "reviewed reversible",
        ),
        (
            Step(
                id="w", intent="pw", action=ActionKind.TYPE, param="query", secret=True
            ),
            "secret",
        ),
        (
            Step(id="w", intent="go", action=ActionKind.KEY, key="x", surface="main"),
            "surface",
        ),
    ],
)
def test_body_refuses_writes_reads_secrets_and_surface_changes(step, fragment):
    steps = [type_step("enter", "query"), step]
    with pytest.raises(ValidationError, match=fragment):
        find_record(steps=steps)


def test_body_refuses_a_plaintext_identity_band():
    click = anchored_click("pick", "Open")
    click.anchor.context_text = "row text"
    with pytest.raises(ValidationError, match="plaintext identity band"):
        find_record(steps=[type_step("enter", "query"), click])


def test_body_reads_exactly_its_declared_inputs():
    with pytest.raises(ValidationError, match="undeclared inputs \\['other'\\]"):
        find_record(steps=[type_step("enter", "query"), type_step("more", "other")])
    with pytest.raises(ValidationError, match="never read \\['query'\\]"):
        find_record(steps=[key_step("focus", "F")])
    # A parameter read only by a boundary check counts as read too.
    with pytest.raises(ValidationError, match="undeclared inputs \\['mode'\\]"):
        find_record(
            steps=[type_step("enter", "query"), key_step("go", "Enter")],
            entry_checks=(
                Predicate(
                    kind=PredicateKind.AND,
                    operands=[
                        text("Find record"),
                        Predicate(
                            kind=PredicateKind.PARAM_EQUALS, param="mode", value="a"
                        ),
                    ],
                ),
            ),
        )


def test_body_refuses_nesting_reserved_ids_and_dangling_edges():
    sig = signature()
    nested = ProgramGraph(
        entry="t",
        states={
            "t": State(
                id="t",
                kind=StateKind.ACTION,
                step=type_step("t", "query"),
                transitions=[Transition(target="c")],
            ),
            "c": State(
                id="c",
                kind=StateKind.SUBFLOW_CALL,
                subflow="other",
                transitions=[Transition(target="end")],
            ),
            "end": State(id="end", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    with pytest.raises(ValidationError, match="'subflow_call' is not allowed"):
        Subroutine(signature=sig, body=nested)
    reserved = ProgramGraph(
        entry="__sr_x",
        states={
            "__sr_x": State(
                id="__sr_x",
                kind=StateKind.ACTION,
                step=type_step("t", "query"),
                transitions=[Transition(target="missing")],
            ),
        },
    )
    fall_off = ProgramGraph(
        entry="t",
        states={
            "t": State(id="t", kind=StateKind.ACTION, step=type_step("t", "query")),
            "end": State(id="end", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    with pytest.raises(ValidationError, match="no outgoing transition"):
        Subroutine(signature=sig, body=fall_off)
    with pytest.raises(ValidationError) as exc:
        Subroutine(signature=sig, body=reserved)
    message = str(exc.value)
    assert "reserved prefix" in message
    assert "undefined state" in message
    assert "no success terminal" in message


# -- embedding and the live boundary checks ---------------------------------------


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


def test_embedded_subroutine_is_a_valid_program():
    sub = find_record(exit_checks=(text("Record"),))
    workflow = caller(sub)
    key = subflow_key(sub)
    assert key == f"{SUBFLOW_PREFIX}find_record/{sub.sha256}"
    assert parse_subflow_key(key) == ("find_record", sub.sha256)
    report = validate_workflow(workflow)
    structure = [i for i in report.issues if i.category == "structure"]
    assert structure == []
    # Re-embedding the same content is idempotent; different content refuses.
    again, _ = embed(workflow, sub)
    assert again.subflows[key] == workflow.subflows[key]
    tampered = workflow.model_copy(deep=True)
    tampered.subflows[key].states["focus"].step.key = "Z"
    with pytest.raises(SubroutineError, match="different body"):
        embed(tampered, sub)


def test_malformed_subroutine_key_is_refused():
    with pytest.raises(SubroutineError):
        parse_subflow_key(f"{SUBFLOW_PREFIX}find_record/short")
    assert parse_subflow_key("plain") is None


def _run(workflow, bundle, run_dir, screen_text):
    vision = FakeVision()
    vision.text_results = {t: Match((10, 10), (0, 0, 5, 5)) for t in screen_text}
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, params={"query": "abc"}, bundle_dir=bundle, run_dir=run_dir
    )
    return report, backend


def test_runtime_runs_body_only_when_entry_check_holds(bundle, tmp_path):
    workflow = caller(find_record())
    report, backend = _run(workflow, bundle, tmp_path / "ok", {"Find record"})
    assert report.success is True
    assert [a[0] for a in backend.actions] == ["press", "type", "press"]

    report, backend = _run(workflow, bundle, tmp_path / "wrong", set())
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []


def test_runtime_halts_when_exit_check_fails(bundle, tmp_path):
    workflow = caller(find_record(exit_checks=(text("Record"),)))
    report, backend = _run(workflow, bundle, tmp_path / "ok", {"Find record", "Record"})
    assert report.success is True
    report, backend = _run(workflow, bundle, tmp_path / "bad", {"Find record"})
    assert report.success is False
    assert report.terminal_outcome == "halt"
    # The body ran; returning without the exit check is what halted.
    assert len(backend.actions) == 3


# -- library --------------------------------------------------------------------


def test_library_versions_requalification_and_no_going_back():
    v1 = find_record()
    a, b = caller(v1, name="intake"), caller(v1, name="referral")
    other = caller(find_record(name="pick_date"), name="schedule")
    library = ready_library(v1, a, b)
    library.publish(find_record(name="pick_date"))
    library.register("schedule", other)
    assert library.dependents("find_record") == ("intake", "referral")

    again = library.publish(v1)
    assert again.version == 1 and again.stale_uses == ()

    v2 = find_record(entry_checks=(text("Search"),))
    plan = library.publish(v2)
    assert plan.version == 2
    assert plan.workflows == ("intake", "referral")
    assert {u.sha256 for u in plan.stale_uses} == {v1.sha256}
    assert library.current("find_record").sha256 == v2.sha256
    assert library.versions("find_record") == (v1.sha256, v2.sha256)

    with pytest.raises(SubroutineError, match="older version"):
        library.publish(v1)
    with pytest.raises(SubroutineError, match="lacks"):
        library.qualify("0" * 64, policy_name="standard", evidence_sha256=EVIDENCE)


def test_library_round_trips_and_refuses_edits():
    sub = find_record()
    workflow = caller(sub)
    library = ready_library(sub, workflow)
    text_doc = library.to_json()
    loaded = SubroutineLibrary.from_json(text_doc)
    assert loaded.to_json() == text_doc
    assert admit_workflow(workflow, loaded).admitted

    edited = json.loads(text_doc)
    body = edited["subroutines"][sub.sha256]["body"]
    body["states"]["focus"]["step"]["key"] = "Z"
    with pytest.raises(SubroutineError, match="does not match its digest"):
        SubroutineLibrary.from_json(json.dumps(edited))

    orphan = json.loads(text_doc)
    orphan["versions"] = {}
    with pytest.raises(SubroutineError, match="not in any version list"):
        SubroutineLibrary.from_json(json.dumps(orphan))

    stray = json.loads(text_doc)
    stray["qualifications"][0]["sha256"] = "1" * 64
    with pytest.raises(SubroutineError, match="unknown digest"):
        SubroutineLibrary.from_json(json.dumps(stray))

    with pytest.raises(SubroutineError, match="not a subroutine library"):
        SubroutineLibrary.from_json(json.dumps({"schema": "other"}))


# -- admission ----------------------------------------------------------------------


def test_admits_a_registered_qualified_current_use():
    sub = find_record()
    workflow = caller(sub)
    admission = admit_workflow(workflow, ready_library(sub, workflow))
    assert admission.admitted, admission.refusals
    assert [u.sha256 for u in admission.uses] == [sub.sha256]


def test_workflow_without_subroutines_is_unaffected():
    plain = Workflow(name="plain", steps=[key_step("a", "A")])
    admission = admit_workflow(plain, SubroutineLibrary())
    assert admission.admitted and admission.uses == ()


def test_refuses_unknown_unqualified_and_unregistered():
    sub = find_record()
    workflow = caller(sub)
    assert codes(admit_workflow(workflow, SubroutineLibrary())) == {
        "unknown_subroutine"
    }

    library = SubroutineLibrary()
    library.publish(sub)
    assert codes(admit_workflow(workflow, library)) == {
        "not_qualified",
        "unregistered_use",
    }
    library.qualify(sub.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    assert codes(admit_workflow(workflow, library)) == {"unregistered_use"}
    library.register(workflow.name, workflow)
    assert admit_workflow(workflow, library).admitted


def test_changing_a_subroutine_requalifies_every_user():
    v1 = find_record()
    a, b = caller(v1, name="intake"), caller(v1, name="referral")
    library = ready_library(v1, a, b)
    assert admit_workflow(a, library).admitted and admit_workflow(b, library).admitted

    v2 = find_record(exit_checks=(text("Record"),))
    plan = library.publish(v2)
    library.qualify(v2.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    assert plan.workflows == ("intake", "referral")
    for workflow in (a, b):
        assert codes(admit_workflow(workflow, library)) == {"superseded"}

    # Re-embedding v2 alone is not enough: the workflow must be registered
    # again (re-qualified) for the new version.
    a2 = caller(v2, name="intake")
    assert codes(admit_workflow(a2, library)) == {"unregistered_use"}
    library.register("intake", a2)
    assert admit_workflow(a2, library).admitted
    assert codes(admit_workflow(b, library)) == {"superseded"}


def test_refuses_an_edited_embedded_body():
    sub = find_record()
    workflow = caller(sub)
    library = ready_library(sub, workflow)
    key = subflow_key(sub)
    edited = workflow.model_copy(deep=True)
    edited.subflows[key].states["go"].step.key = "Tab"
    assert codes(admit_workflow(edited, library)) == {"embedded_drift"}
    skipped = workflow.model_copy(deep=True)
    skipped.subflows[key].entry = "focus"  # bypass the entry check
    assert codes(admit_workflow(skipped, library)) == {"embedded_drift"}


def test_refuses_surface_mismatches():
    sub = find_record()
    for surface, expected in (
        ("windows", "surface_kind_mismatch"),
        (None, "surface_undeclared"),
    ):
        workflow = caller(sub, surface=surface)
        library = ready_library(sub, workflow)
        assert codes(admit_workflow(workflow, library)) == {expected}
    multi = caller(sub, surfaces=[SurfaceSpec(id="main", kind="web")])
    assert codes(admit_workflow(multi, ready_library(sub, multi))) == {"multi_surface"}


def test_refuses_inputs_the_caller_cannot_supply():
    dated = find_record(inputs=(SubroutineInput(name="query", type=ParamKind.DATE),))

    def verdict(sub, **fields):
        workflow = caller(sub, **fields)
        return codes(admit_workflow(workflow, ready_library(sub, workflow)))

    assert verdict(find_record(), param_specs={}) == {"missing_input"}
    assert verdict(dated) == {"input_type_mismatch"}
    assert verdict(dated, param_specs={}, params={"query": "2026-01-01"}) == {
        "input_type_mismatch"
    }
    assert (
        verdict(
            dated,
            param_specs={
                "query": ParamSpec(name="query", type=ParamKind.DATE, example="d")
            },
        )
        == set()
    )
    assert verdict(
        find_record(),
        param_specs={"query": ParamSpec(name="query", required=False)},
    ) == {"missing_input"}
    assert verdict(find_record(), secret_params=["query"]) == {"secret_input"}

    enum_sub = find_record(
        inputs=(SubroutineInput(name="query", type=ParamKind.ENUM, choices=("a", "b")),)
    )
    wide = {
        "query": ParamSpec(
            name="query", type=ParamKind.ENUM, choices=["a", "c"], example="a"
        )
    }
    narrow = {
        "query": ParamSpec(
            name="query", type=ParamKind.ENUM, choices=["a"], example="a"
        )
    }
    assert verdict(enum_sub, param_specs=wide) == {"input_choices_mismatch"}
    assert verdict(enum_sub, param_specs=narrow) == set()


def test_refusal_messages_do_not_echo_screen_text():
    sub = find_record(
        steps=[type_step("enter", "query"), anchored_click("pick", "Row Label Marker")],
        entry_checks=(text("Sample Clinic"),),
    )
    workflow = caller(sub, surface="windows")
    edited = workflow.model_copy(deep=True)
    edited.subflows[subflow_key(sub)].states["pick"].step.key = "x"
    admission = admit_workflow(edited, SubroutineLibrary())
    library = ready_library(sub)
    admission2 = admit_workflow(edited, library)
    dumped = admission.model_dump_json() + admission2.model_dump_json()
    assert "Marker" not in dumped and "Sample" not in dumped


# -- detection ------------------------------------------------------------------------


def _linear(name, *steps):
    return Workflow(name=name, surface="web", steps=list(steps))


def test_mines_a_sequence_shared_across_workflows_by_shape_only():
    # Same shape, different labels: detection never compares labels.
    a = _linear(
        "a",
        key_step("a0", "Escape"),
        anchored_click("a1", "Search patients", role="searchbox"),
        type_step("a2", "name"),
        key_step("a3", "Enter"),
        anchored_click("a4", "Done", role="button"),
    )
    b = _linear(
        "b",
        anchored_click("b1", "Find a person", role="searchbox"),
        type_step("b2", "who"),
        key_step("b3", "Enter"),
        key_step("b4", "Tab"),
    )
    candidates = mine_recurring_sequences({"a": a, "b": b})
    assert len(candidates) == 1
    (candidate,) = candidates
    assert candidate.admitted is False and candidate.runnable is False
    assert candidate.workflows == ("a", "b")
    assert len(candidate.shape) == 3
    assert {o.step_ids for o in candidate.occurrences} == {
        ("a1", "a2", "a3"),
        ("b1", "b2", "b3"),
    }
    assert {o.params for o in candidate.occurrences} == {("name",), ("who",)}
    blob = json.dumps(candidate.model_dump(mode="json"))
    assert "Search" not in blob and "Find" not in blob


def test_repeats_inside_one_workflow_are_not_enough():
    steps = [
        type_step(f"s{i}", "q") if i % 2 else key_step(f"s{i}", "Enter")
        for i in range(8)
    ]
    assert mine_recurring_sequences({"only": _linear("only", *steps)}) == []
    with pytest.raises(SubroutineError):
        mine_recurring_sequences({}, min_workflows=1)


def test_mines_straight_runs_inside_program_graphs():
    sub = find_record()
    a, b = caller(sub, name="a"), caller(sub, name="b")
    candidates = mine_recurring_sequences({"a": a, "b": b})
    assert candidates and candidates[0].workflows == ("a", "b")
    assert [step_shape(s.step) for s in sub.body.states.values() if s.step] == list(
        candidates[0].shape
    )


def test_wrapped_body_and_problems_are_deterministic():
    sub = find_record(exit_checks=(text("Record"),))
    assert wrapped_body(sub) == wrapped_body(sub)
    assert body_problems(sub) == []
