"""Tests for bundle schema v2: manifest / digest / provenance, v1->v2
migration, load-time structural validation, and integrity verification.

Import-light: builds ``Workflow`` / ``ProgramGraph`` objects directly (no
Playwright, OCR, or model deps) and exercises save/load on ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow import bundle_validation as bv
from openadapt_flow.ir import (
    SCHEMA_VERSION,
    ActionKind,
    Anchor,
    LoopSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

# --------------------------------------------------------------------------
# tiny builders
# --------------------------------------------------------------------------


def key_step(step_id="s1", **kw) -> Step:
    return Step(id=step_id, intent="press", action=ActionKind.KEY, key="Enter", **kw)


def click_step(step_id="s1", *, risk="reversible", effects=(), **kw) -> Step:
    return Step(
        id=step_id,
        intent="click",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save",
        ),
        risk=risk,
        effects=list(effects),
        **kw,
    )


def action(step: Step, *, to: str, on_exception: str | None = None) -> State:
    return State(
        id=step.id,
        kind=StateKind.ACTION,
        step=step,
        transitions=[Transition(target=to)],
        on_exception=on_exception,
    )


def terminal(state_id="done", outcome="success") -> State:
    return State(id=state_id, kind=StateKind.TERMINAL, outcome=outcome)


def graph(entry: str, *states: State) -> ProgramGraph:
    return ProgramGraph(entry=entry, states={s.id: s for s in states})


def _write_bundle_dir(tmp_path, *, template=True):
    b = tmp_path / "bundle"
    (b / "templates").mkdir(parents=True)
    if template:
        (b / "templates" / "btn.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-crop-bytes")
    return b


def _good_program_workflow() -> Workflow:
    program = graph(
        "s1",
        action(click_step("s1"), to="s2"),
        action(key_step("s2"), to="done"),
        terminal("done"),
    )
    return Workflow(name="good-prog", program=program)


# --------------------------------------------------------------------------
# 1. schema version + migration
# --------------------------------------------------------------------------


def test_fresh_workflow_defaults_to_v2():
    assert SCHEMA_VERSION == 2
    assert Workflow(name="x").schema_version == 2


def test_v1_bundle_loads_and_migrates(tmp_path):
    """A raw v1 workflow.json (schema_version 1, no manifest) loads, migrates to
    v2 in memory, and gets a manifest computed on read -- without breaking."""
    b = _write_bundle_dir(tmp_path)
    (b / "workflow.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "legacy",
                "steps": [
                    {
                        "id": "s1",
                        "intent": "click",
                        "action": "click",
                        "anchor": {
                            "template": "templates/btn.png",
                            "region": [100, 100, 50, 20],
                            "click_point": [110, 105],
                        },
                    }
                ],
            }
        )
    )
    wf = Workflow.load(b)
    assert wf.schema_version == 2  # migrated on read
    assert wf.name == "legacy"
    assert wf.manifest is not None  # computed because absent
    assert wf.manifest.content_digest  # non-empty
    assert "templates/btn.png" in wf.manifest.file_hashes


def test_migrate_bundle_dict_is_additive_noop_on_fields():
    raw = {"name": "x", "steps": []}  # no schema_version at all -> treated as v1
    out = bv.migrate_bundle_dict(raw)
    assert out["schema_version"] == 2
    # already-current bundle left untouched
    cur = {"schema_version": 2, "name": "y"}
    assert bv.migrate_bundle_dict(cur)["schema_version"] == 2


# --------------------------------------------------------------------------
# 2. v2 round-trip + stable digest + manifest/provenance
# --------------------------------------------------------------------------


def test_v2_round_trips_with_stable_digest(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    # sidecar manifest written
    assert (b / "manifest.json").is_file()

    loaded = Workflow.load(b)
    dig = loaded.manifest.content_digest
    assert dig
    assert loaded.manifest.provenance.compiler_version  # stamped
    assert "templates/btn.png" in loaded.manifest.file_hashes

    # re-saving unchanged content reproduces the SAME digest (stable)
    loaded.save(b)
    reloaded = Workflow.load(b)
    assert reloaded.manifest.content_digest == dig


def test_digest_changes_when_content_changes(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    d1 = Workflow.load(b).manifest.content_digest

    wf2 = _good_program_workflow()
    wf2.name = "renamed"
    b2 = tmp_path / "bundle2"
    (b2 / "templates").mkdir(parents=True)
    (b2 / "templates" / "btn.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-crop-bytes")
    wf2.save(b2)
    d2 = Workflow.load(b2).manifest.content_digest
    assert d1 != d2


def test_manifest_encrypted_flag_present_and_false(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    loaded = Workflow.load(b)
    assert loaded.manifest.encrypted is False
    assert loaded.encrypted is False


def test_certification_provenance_persists(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.stamp_certification(
        "clinical-write", passed=True, expires_at="2027-01-01T00:00:00Z"
    )
    wf.save(b)
    loaded = Workflow.load(b)
    prov = loaded.manifest.provenance
    assert prov.policy_name == "clinical-write"
    assert prov.certified is True
    assert prov.certification_status == "certified"
    assert prov.expires_at == "2027-01-01T00:00:00Z"
    assert prov.certified_at  # timestamped


# --------------------------------------------------------------------------
# 3. integrity verification
# --------------------------------------------------------------------------


def test_tampered_workflow_json_fails_integrity(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    # tamper: rename the workflow AFTER the digest was sealed
    raw = json.loads((b / "workflow.json").read_text())
    raw["name"] = "tampered"
    (b / "workflow.json").write_text(json.dumps(raw))
    with pytest.raises(bv.BundleIntegrityError):
        Workflow.load(b)


def test_tampered_template_fails_integrity(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    (b / "templates" / "btn.png").write_bytes(b"different-bytes-entirely")
    with pytest.raises(bv.BundleIntegrityError):
        Workflow.load(b)


def test_integrity_can_be_skipped(tmp_path):
    b = _write_bundle_dir(tmp_path)
    wf = _good_program_workflow()
    wf.save(b)
    raw = json.loads((b / "workflow.json").read_text())
    raw["name"] = "tampered"
    (b / "workflow.json").write_text(json.dumps(raw))
    # explicit opt-out still loads
    wf2 = Workflow.load(b, verify_integrity=False)
    assert wf2.name == "tampered"


# --------------------------------------------------------------------------
# 4. structural validation: a good program passes; each rule rejects a bad one
# --------------------------------------------------------------------------


def test_good_program_and_linear_validate():
    assert bv.validate_workflow(_good_program_workflow()).ok
    # a linear bundle validates trivially
    lin = Workflow(name="lin", steps=[click_step("s1"), key_step("s2")])
    assert bv.validate_workflow(lin).ok


def _codes(wf: Workflow) -> set[str]:
    return {i.code for i in bv.validate_workflow(wf).issues}


def test_missing_entry_rejected():
    program = graph("nope", action(click_step("s1"), to="done"), terminal("done"))
    # entry "nope" is not a defined state
    program.entry = "nope"
    wf = Workflow(name="bad", program=program)
    assert "missing_entry" in _codes(wf)


def test_dangling_transition_target_rejected():
    program = graph(
        "s1",
        action(click_step("s1"), to="ghost"),  # ghost is undefined
        terminal("done"),
    )
    wf = Workflow(name="bad", program=program)
    assert "dangling_transition" in _codes(wf)


def test_kind_payload_mismatch_action_without_step_rejected():
    bad = State(
        id="s1",
        kind=StateKind.ACTION,
        step=None,
        transitions=[Transition(target="done")],
    )
    program = graph("s1", bad, terminal("done"))
    wf = Workflow(name="bad", program=program)
    assert "action_without_step" in _codes(wf)


def test_kind_payload_mismatch_terminal_without_outcome_rejected():
    bad_term = State(id="done", kind=StateKind.TERMINAL, outcome=None)
    program = graph("s1", action(click_step("s1"), to="done"), bad_term)
    wf = Workflow(name="bad", program=program)
    assert "terminal_without_outcome" in _codes(wf)


def test_branch_without_predicate_rejected():
    br = State(
        id="b",
        kind=StateKind.BRANCH,
        transitions=[Transition(target="done")],  # unconditional only, no predicate
    )
    program = graph("b", br, terminal("done"))
    wf = Workflow(name="bad", program=program)
    assert "branch_without_predicate" in _codes(wf)


def test_missing_subflow_call_rejected():
    call = State(
        id="c",
        kind=StateKind.SUBFLOW_CALL,
        subflow="absent",
        transitions=[Transition(target="done")],
    )
    program = graph("c", call, terminal("done"))
    wf = Workflow(name="bad", program=program)  # no subflows defined
    assert "missing_subflow" in _codes(wf)


def test_missing_loop_body_rejected():
    lp = State(
        id="loop",
        kind=StateKind.LOOP,
        loop=LoopSpec(relation="queue", body="absent_body"),
        transitions=[Transition(target="done")],
    )
    program = graph("loop", lp, terminal("done"))
    wf = Workflow(name="bad", program=program)
    assert "missing_loop_body" in _codes(wf)


def test_duplicate_step_id_rejected():
    # two distinct ACTION states whose steps share the id "dup"
    a = State(
        id="sa",
        kind=StateKind.ACTION,
        step=click_step("dup"),
        transitions=[Transition(target="sb")],
    )
    b = State(
        id="sb",
        kind=StateKind.ACTION,
        step=key_step("dup"),
        transitions=[Transition(target="done")],
    )
    program = graph("sa", a, b, terminal("done"))
    wf = Workflow(name="bad", program=program)
    assert "duplicate_step_id" in _codes(wf)


def test_duplicate_state_id_across_subflows_rejected():
    program = graph("s1", action(click_step("s1"), to="done"), terminal("done"))
    # subflow reuses state id "s1" (via a step also named "s1")
    sub = graph("s1", action(click_step("s1"), to="s_end"), terminal("s_end"))
    wf = Workflow(name="bad", program=program, subflows={"sub": sub})
    assert "duplicate_state_id" in _codes(wf)


def test_unreachable_terminal_rejected():
    program = ProgramGraph(
        entry="s1",
        states={
            "s1": action(click_step("s1"), to="done"),
            "done": terminal("done"),
            "orphan": terminal("orphan"),  # never targeted
        },
    )
    wf = Workflow(name="bad", program=program)
    assert "unreachable_terminal" in _codes(wf)


def test_unsafe_unconditional_cycle_rejected():
    program = ProgramGraph(
        entry="a",
        states={
            "a": State(
                id="a",
                kind=StateKind.ACTION,
                step=click_step("a"),
                transitions=[Transition(target="b")],  # unconditional
            ),
            "b": State(
                id="b",
                kind=StateKind.ACTION,
                step=key_step("b"),
                transitions=[Transition(target="a")],  # unconditional -> cycle
            ),
        },
    )
    wf = Workflow(name="bad", program=program)
    assert "unsafe_cycle" in _codes(wf)


def test_guarded_cycle_is_not_flagged_unsafe():
    """A cycle whose back-edge is GUARDED (a predicate can break it) is not an
    unsafe unconditional cycle."""
    program = ProgramGraph(
        entry="a",
        states={
            "a": State(
                id="a",
                kind=StateKind.ACTION,
                step=click_step("a"),
                transitions=[Transition(target="b")],
            ),
            "b": State(
                id="b",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=Predicate(kind=PredicateKind.TEXT_PRESENT, text="More"),
                        target="a",
                    ),
                    Transition(target="done"),
                ],
            ),
            "done": terminal("done"),
        },
    )
    wf = Workflow(name="ok", program=program)
    assert "unsafe_cycle" not in _codes(wf)


# --------------------------------------------------------------------------
# 5. safety rule: consequential write with no effect verification
# --------------------------------------------------------------------------


def _record_effect(risk="irreversible") -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1"},
        risk=risk,
    )


def test_irreversible_write_without_effect_is_flagged():
    wf = Workflow(name="risky", steps=[click_step("s1", risk="irreversible")])
    report = bv.validate_workflow(wf)
    codes = {i.code for i in report.issues}
    assert "unverified_consequential_write" in codes
    # it is a SAFETY issue, not a structural one
    assert report.structural_ok is True
    assert not report.ok


def test_irreversible_write_with_effect_passes():
    wf = Workflow(
        name="safe",
        steps=[click_step("s1", risk="irreversible", effects=[_record_effect()])],
    )
    assert bv.validate_workflow(wf).ok


def test_reversible_write_without_effect_is_fine():
    wf = Workflow(name="benign", steps=[click_step("s1", risk="reversible")])
    assert bv.validate_workflow(wf).ok


# --------------------------------------------------------------------------
# 6. load-time behavior: structural raises, safety does not
# --------------------------------------------------------------------------


def test_load_raises_on_structurally_malformed_bundle(tmp_path):
    b = _write_bundle_dir(tmp_path)
    program = graph(
        "s1",
        action(click_step("s1"), to="ghost"),  # dangling target
        terminal("done"),
    )
    wf = Workflow(name="broken", program=program)
    wf.save(b)  # save does not validate; write the bad bundle
    with pytest.raises(bv.BundleValidationError):
        Workflow.load(b)


def test_load_does_not_raise_on_safety_only_issue(tmp_path):
    """A well-formed but uncertifiable (irreversible write, no effect) bundle
    still LOADS -- the safety finding is surfaced by lint/certify, not load, so
    existing uncertified bundles keep working."""
    b = _write_bundle_dir(tmp_path)
    wf = Workflow(name="risky", steps=[click_step("s1", risk="irreversible")])
    wf.save(b)
    loaded = Workflow.load(b)  # must not raise
    assert loaded.name == "risky"
    # but validation still reports the safety issue
    report = bv.validate_workflow(loaded)
    assert any(i.code == "unverified_consequential_write" for i in report.issues)


def test_load_validation_can_be_disabled(tmp_path):
    b = _write_bundle_dir(tmp_path)
    program = graph("s1", action(click_step("s1"), to="ghost"), terminal("done"))
    wf = Workflow(name="broken", program=program)
    wf.save(b)
    # opt out of validation -> loads despite the dangling target
    loaded = Workflow.load(b, validate=False)
    assert loaded.name == "broken"
