"""Tests for compiler effect-mining (``compiler.effect_mining``).

The miner auto-derives typed system-of-record ``Effect``s from what a
demonstration actually observed, honestly:

- an OBSERVED ``/api/db`` delta -> a real ``record_written`` / ``field_equals``
  the ``EffectVerifier`` then CONFIRMS against the live system of record;
- a consequential step with NO captured delta -> a flagged PLACEHOLDER the run
  refuses to silently trust (never a fabricated endpoint);
- an ordinary step with nothing observed -> NO effect + an honest gap.

No model calls; the live-local tests use the in-process MockMed fault server.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from openadapt_flow.compiler import compile_recording, mine_step_effects
from openadapt_flow.compiler.effect_mining import (
    PLACEHOLDER_MATCH,
    SOR_AFTER_KEY,
    SOR_BEFORE_KEY,
    derive_different_path_readback,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
)
from openadapt_flow.runtime.effects import (
    EffectKind,
    RestRecordVerifier,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer

# Reuse the scripted fakes + fault-server helpers from the replayer-effects
# tests (pytest prepend import mode puts tests/ on sys.path).
from tests.test_replayer_effects import (
    WritingBackend,
    _dirs,
    _fault_server,
    _save_workflow,
    _vision_that_confirms_saved,
)

NOTE = "confidential follow up note"


def _key_step(risk: str = "irreversible") -> Step:
    return Step(
        id="step_000",
        intent="save encounter",
        action=ActionKind.KEY,
        key="Enter",
        risk=risk,
    )


def _record(**kw) -> dict:
    base = {
        "id": 1,
        "patient_id": "p1",
        "type": "Triage",
        "note": NOTE,
        "source": "replay",
        "key": None,
    }
    base.update(kw)
    return base


# -- unit: derive from an observed SoR delta --------------------------------


def test_mines_record_written_and_field_equals_from_delta():
    event = {
        "i": 0,
        "kind": "key",
        "key": "Enter",
        SOR_BEFORE_KEY: [],
        SOR_AFTER_KEY: [_record()],
    }
    mined = mine_step_effects(event, _key_step(), exclude_texts=(NOTE,))

    assert mined.derived is True
    kinds = [e.kind for e in mined.effects]
    assert EffectKind.RECORD_WRITTEN in kinds
    assert EffectKind.FIELD_EQUALS in kinds

    written = next(e for e in mined.effects if e.kind is EffectKind.RECORD_WRITTEN)
    # The surrogate id is NOT a selector; the typed note is payload, not
    # identity; null fields are dropped. Identity fields survive.
    assert written.match == {"patient_id": "p1", "type": "Triage", "source": "replay"}
    assert "id" not in written.match
    assert "note" not in written.match
    assert written.expected_count == 1
    assert written.risk == "irreversible"
    assert written.needs_operator_confirmation is False

    field = next(e for e in mined.effects if e.kind is EffectKind.FIELD_EQUALS)
    assert field.field == "note"
    assert field.value == NOTE  # the demonstrated typed value, read back
    assert "note" not in field.match


def test_mines_idempotency_key_only_when_observed():
    with_key = mine_step_effects(
        {SOR_BEFORE_KEY: [], SOR_AFTER_KEY: [_record(key="idem-abc")]},
        _key_step(),
        exclude_texts=(NOTE,),
    )
    written = next(e for e in with_key.effects if e.kind is EffectKind.RECORD_WRITTEN)
    assert written.idempotency_key == "idem-abc"
    assert written.key_field == "key"

    without = mine_step_effects(
        {SOR_BEFORE_KEY: [], SOR_AFTER_KEY: [_record()]},
        _key_step(),
        exclude_texts=(NOTE,),
    )
    written2 = next(e for e in without.effects if e.kind is EffectKind.RECORD_WRITTEN)
    # Never invented: §7 names the idempotency key as app-specific.
    assert written2.idempotency_key is None


# -- unit: honest gaps (no fabricated bindings) -----------------------------


def test_no_delta_non_consequential_yields_no_effect(caplog):
    event = {SOR_BEFORE_KEY: [], SOR_AFTER_KEY: []}  # observed, nothing wrote
    step = _key_step(risk="reversible")
    with caplog.at_level(logging.DEBUG):
        mined = mine_step_effects(event, step, exclude_texts=(NOTE,))
    assert mined.disposition == "none"
    assert mined.effects == []
    assert "no verifiable effect derivable" in mined.reason


def test_no_observation_non_consequential_yields_no_effect():
    # No SoR keys at all, reversible step: honest gap, not a placeholder.
    mined = mine_step_effects({"kind": "key"}, _key_step(risk="reversible"))
    assert mined.disposition == "none"
    assert mined.effects == []


def test_consequential_without_observation_emits_flagged_placeholder():
    mined = mine_step_effects({"kind": "key"}, _key_step(risk="irreversible"))
    assert mined.placeholder is True
    assert len(mined.effects) == 1
    eff = mined.effects[0]
    assert eff.kind is EffectKind.RECORD_WRITTEN
    assert eff.needs_operator_confirmation is True
    # No invented endpoint: the selector is a sentinel, not a real record.
    assert eff.match == PLACEHOLDER_MATCH
    assert "PLACEHOLDER" in (eff.probe or "")


def test_consequential_with_snapshot_but_no_new_record_is_placeholder():
    # The SoR was observed but no single new record appeared for a step marked
    # consequential -> flagged placeholder (cannot bind an at-most-once
    # contract honestly), never a false green.
    event = {SOR_BEFORE_KEY: [_record()], SOR_AFTER_KEY: [_record()]}
    mined = mine_step_effects(event, _key_step(), exclude_texts=(NOTE,))
    assert mined.placeholder is True
    assert mined.effects[0].needs_operator_confirmation is True


# -- unit: structured DOM field map (form-level, flagged) -------------------


def test_dom_field_delta_mines_flagged_field_equals():
    event = {
        "dom_fields_before": {"note": ""},
        "dom_fields_after": {"note": NOTE},
    }
    mined = mine_step_effects(event, _key_step(), exclude_texts=(NOTE,))
    assert mined.placeholder is True  # form-level, not a record write
    eff = mined.effects[0]
    assert eff.kind is EffectKind.FIELD_EQUALS
    assert eff.value == NOTE
    assert eff.needs_operator_confirmation is True


# -- live-local: mined effect CONFIRMS against the real system of record ----


def test_mined_effects_confirmed_by_verifier_on_real_sor():
    base, db, stop = _fault_server()
    try:
        verifier = RestRecordVerifier(base)
        before = db.snapshot()["records"]
        requests.post(
            f"{base}/api/encounter",
            json={"patient_id": "p1", "type": "Triage", "note": NOTE},
            timeout=5,
        )
        after = db.snapshot()["records"]
        event = {SOR_BEFORE_KEY: before, SOR_AFTER_KEY: after}
        mined = mine_step_effects(event, _key_step(), exclude_texts=(NOTE,))
        assert mined.derived is True

        # The mined contracts, checked against the LIVE store, all CONFIRM.
        pre = verifier.capture_pre_state()
        for eff in mined.effects:
            v = verifier.verify(eff, pre)
            assert v.confirmed, f"{eff.kind}: {v.reason}"
    finally:
        stop()


def test_mined_record_written_refutes_a_duplicate_on_the_real_sor():
    base, db, stop = _fault_server()
    try:
        verifier = RestRecordVerifier(base)
        before = db.snapshot()["records"]
        requests.post(
            f"{base}/api/encounter",
            json={"patient_id": "p1", "type": "Triage", "note": NOTE},
            timeout=5,
        )
        after = db.snapshot()["records"]
        mined = mine_step_effects(
            {SOR_BEFORE_KEY: before, SOR_AFTER_KEY: after},
            _key_step(),
            exclude_texts=(NOTE,),
        )
        written = next(e for e in mined.effects if e.kind is EffectKind.RECORD_WRITTEN)
        # A duplicate submission lands a SECOND matching row.
        requests.post(
            f"{base}/api/encounter",
            json={"patient_id": "p1", "type": "Triage", "note": NOTE},
            timeout=5,
        )
        v = verifier.verify(written, verifier.capture_pre_state())
        assert v.verdict is Verdict.REFUTED
        assert v.observed_count == 2
    finally:
        stop()


# -- live-local: a placeholder effect is NOT silently trusted at replay -----


def test_placeholder_effect_halts_the_run(tmp_path):
    base, db, stop = _fault_server()
    try:
        # A consequential step whose binding the compiler could NOT derive: the
        # mined placeholder must HALT the run rather than verify a fabricated
        # binding (fail-safe, like the identity gate).
        placeholder = mine_step_effects({"kind": "key"}, _key_step(risk="irreversible"))
        assert placeholder.placeholder

        backend = WritingBackend(base)
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(effects=placeholder.effects, risk="irreversible")
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(base),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.effect_verified is False
        assert "PLACEHOLDER" in (r.error or "")
        assert any("NEEDS OPERATOR CONFIRMATION" in line for line in r.effect_results)
        # $0 guarantee holds even on the fail-safe path.
        assert report.model_calls == 0
    finally:
        stop()


# -- integration: compile_recording wiring + back-compat --------------------


def _write_recording(tmp_path: Path, *, sor_after: list[dict]) -> Path:
    """A minimal 2-event recording (type note, then a consequential save that
    observed a system-of-record delta). No frames needed for type/key steps."""
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    events = [
        {"i": 0, "kind": "type", "text": NOTE, "param": "note", "t": 1.0},
        {
            "i": 1,
            "kind": "key",
            "key": "Enter",
            "t": 2.0,
            SOR_BEFORE_KEY: [],
            SOR_AFTER_KEY: sor_after,
        },
    ]
    (rec / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (rec / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-mine-001",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": [1280, 800],
                "params": {"note": NOTE},
            }
        )
    )
    return rec


def test_compile_attaches_mined_effects_when_opted_in(tmp_path):
    rec = _write_recording(tmp_path, sor_after=[_record()])
    wf = compile_recording(
        rec,
        tmp_path / "bundle",
        name="mine-demo",
        risk_overrides={"step_001": "irreversible"},
        mine_effects=True,
    )
    save = wf.steps[1]
    assert save.action is ActionKind.KEY
    assert [e.kind for e in save.effects] == [
        EffectKind.RECORD_WRITTEN,
        EffectKind.FIELD_EQUALS,
    ]
    assert save.effects[0].risk == "irreversible"
    # The mined effects are surfaced in the reviewable workflow.py rendering.
    rendered = (tmp_path / "bundle" / "workflow.py").read_text()
    assert "effect record_written" in rendered


def test_compile_default_off_is_back_compat(tmp_path):
    rec = _write_recording(tmp_path, sor_after=[_record()])
    wf = compile_recording(
        rec,
        tmp_path / "bundle",
        name="mine-demo",
        risk_overrides={"step_001": "irreversible"},
    )
    # Default: no mining -> no effects on any step (byte-identical to before).
    assert all(step.effects == [] for step in wf.steps)


def test_compile_mining_on_but_nothing_derivable_leaves_effects_empty(tmp_path):
    # Observed SoR but no new record + reversible step -> honest no-effect;
    # bundle carries no effects even with mining ON.
    rec = _write_recording(tmp_path, sor_after=[])
    wf = compile_recording(
        rec, tmp_path / "bundle", name="mine-demo", mine_effects=True
    )
    assert all(step.effects == [] for step in wf.steps)


# -- unit: on-screen read-back auto-derivation (the no-API default oracle) ---


def _view_step(
    step_id: str,
    *,
    region=(100, 200, 300, 40),
    show_value: str = NOTE,
    action=ActionKind.KEY,
    click_point=None,
    risk: str = "irreversible",
) -> Step:
    """A step whose postconditions show ``show_value`` rendered in a stable
    region (the demonstrator viewing the saved value)."""
    anchor = None
    if click_point is not None:
        anchor = Anchor(
            template="t.png", region=(0, 0, 10, 10), click_point=click_point
        )
    return Step(
        id=step_id,
        intent="view saved encounter",
        action=action,
        key="Enter" if action is ActionKind.KEY else None,
        anchor=anchor,
        risk=risk,
        expect=[
            Postcondition(kind=PostconditionKind.REGION_STABLE, region=region),
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text=f"Saved: {show_value}"
            ),
        ],
    )


def test_onscreen_readback_derived_when_demo_views_saved_value():
    step = _view_step("step_000")
    mined = mine_step_effects({"kind": "key"}, step, exclude_texts=(NOTE,))
    assert mined.disposition == "readback"
    assert len(mined.effects) == 1
    eff = mined.effects[0]
    assert eff.kind is EffectKind.FIELD_EQUALS
    assert eff.value == NOTE
    assert eff.readback is not None
    assert eff.readback.region == (100, 200, 300, 40)
    # Same-surface (weak) until a different-path re-open is derived.
    assert eff.readback.different_path is False
    assert eff.needs_operator_confirmation is False


def test_onscreen_readback_requires_a_bounded_region_else_placeholder():
    # Payload viewed (text_present) but NO stable region to bound the re-read:
    # we do NOT emit an unbounded whole-frame read-back -> honest placeholder.
    step = Step(
        id="step_000",
        intent="save",
        action=ActionKind.KEY,
        key="Enter",
        risk="irreversible",
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=f"Saved {NOTE}")
        ],
    )
    mined = mine_step_effects({"kind": "key"}, step, exclude_texts=(NOTE,))
    assert mined.placeholder is True
    assert mined.effects[0].needs_operator_confirmation is True


def test_different_path_upgrade_from_reopen_sequence():
    # A consequential write (same-surface read-back), then the demonstrator
    # navigates AWAY and re-opens the record (>= 2 nav actions) ending in a
    # re-view of the saved value -> upgrade to different-path.
    write = _view_step("step_000")  # gets a same-surface read-back effect
    write_mined = mine_step_effects({"kind": "key"}, write, exclude_texts=(NOTE,))
    write.effects = write_mined.effects

    close = _plain_click("step_001", (10, 10))  # close the form
    search = Step(
        id="step_002",
        intent="type patient id",
        action=ActionKind.TYPE,
        text="p1",  # a LITERAL (non-parameterized) search term
    )
    reopen = _view_step(
        "step_003",
        region=(50, 60, 200, 30),
        action=ActionKind.CLICK,
        click_point=(70, 65),
    )
    steps = [write, close, search, reopen]

    upgraded = derive_different_path_readback(steps)
    assert upgraded == 1
    rb = write.effects[0].readback
    assert rb is not None
    assert rb.different_path is True
    assert rb.region == (50, 60, 200, 30)
    # The re-navigation replays the recorded away-and-back actions.
    assert [n.action for n in rb.renavigation] == ["click", "type", "click"]


def test_different_path_not_upgraded_when_renav_is_parameterized():
    # A parameterized search step cannot be replayed deterministically as a
    # re-navigation -> stays SAME-SURFACE (safe: an over-eager upgrade could
    # only false-HALT, but we decline it).
    write = _view_step("step_000")
    write.effects = mine_step_effects(
        {"kind": "key"}, write, exclude_texts=(NOTE,)
    ).effects
    close = _plain_click("step_001", (10, 10))
    param_search = Step(
        id="step_002",
        intent="type patient id",
        action=ActionKind.TYPE,
        param="patient_id",  # per-run value, not a literal -> not replayable
    )
    reopen = _view_step("step_003", action=ActionKind.CLICK, click_point=(70, 65))
    upgraded = derive_different_path_readback([write, close, param_search, reopen])
    assert upgraded == 0
    assert write.effects[0].readback.different_path is False


def _plain_click(step_id: str, point) -> Step:
    return Step(
        id=step_id,
        intent="navigate",
        action=ActionKind.CLICK,
        anchor=Anchor(template="t.png", region=(0, 0, 10, 10), click_point=point),
        risk="reversible",
    )


def test_readback_effect_rendered_in_workflow_py_for_review():
    from openadapt_flow.compiler.codegen import _effect_comment

    step = _view_step("step_000")
    step.effects = mine_step_effects(
        {"kind": "key"}, step, exclude_texts=(NOTE,)
    ).effects
    rendered = "\n".join(_effect_comment(step.effects))
    assert "onscreen-readback=same-surface" in rendered
    assert "SAME-SURFACE read-back (weak; non-default)" in rendered

    derive_different_path_readback(
        [
            step,
            _plain_click("step_001", (10, 10)),
            _plain_click("step_002", (20, 20)),
            _view_step("step_003", action=ActionKind.CLICK, click_point=(70, 65)),
        ]
    )
    rendered2 = "\n".join(_effect_comment(step.effects))
    assert "onscreen-readback=different-path" in rendered2
    assert "renav=" in rendered2
