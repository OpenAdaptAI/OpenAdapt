"""P0-3 regression: runtime params must bind into effect contracts.

Before this fix an ``Effect`` carried plain static strings, so a PARAMETERIZED
workflow verified its system-of-record effects against the values baked in at
DEMONSTRATION time: it could write patient "Susan" via the GUI yet verify the
recorded demo patient "Phil", check the demonstrated note instead of the run's,
or reuse ONE frozen idempotency key across unrelated runs. These tests pin that
the runtime now resolves each :class:`ValueExpr` against THIS run's params
before snapshotting the pre-state and verifying, and that back-compat with the
old plain-string form is exact.

The backend and vision are faked (as in ``test_replayer``); most tests use a
capturing verifier so the assertion is directly on the RESOLVED contract the
runtime handed the verifier. One test drives the REAL ``RestRecordVerifier``
against the in-process MockMed fault server end-to-end. Zero model calls.
"""

from __future__ import annotations

import requests

# Reuse the scripted fakes from the main replayer unit tests (pytest's prepend
# import mode puts tests/ on sys.path).
from tests.test_replayer import FakeBackend, FakeVision, Match

from openadapt_flow.ir import (
    ActionKind,
    ParamSpec,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    RestRecordVerifier,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer

# -- helpers -----------------------------------------------------------------


class CapturingVerifier:
    """An EffectVerifier that CONFIRMS everything and records the exact
    (already-resolved) Effect it was handed, so a test can assert on the
    contract the runtime bound to this run's params."""

    substrate = "fake"

    def __init__(self) -> None:
        self.verified: list[Effect] = []

    def capture_pre_state(self, context=None) -> EffectState:
        return EffectState(substrate=self.substrate, reachable=True, records=[])

    def verify(self, expected: Effect, before: EffectState, context=None):
        self.verified.append(expected)
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=expected.kind,
            substrate=self.substrate,
            reason="confirmed (test)",
        )


def _vision_confirms_saved() -> FakeVision:
    vision = FakeVision()
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    return vision


def _dirs(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    return bundle, tmp_path / "run"


def _param_workflow(effects, *, param_specs=None):
    """A one-step parameterized 'save encounter' workflow."""
    return Workflow(
        name="save-param",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
                effects=effects,
            )
        ],
        param_specs=param_specs or {},
    )


# -- 1. the resolved effect targets the RUN's values, not the demo's ---------


def test_parameterized_effect_verifies_the_runs_patient_and_note(tmp_path):
    # The demo recorded patient "phil"/note "phil-note" (the param examples).
    # A parameterized effect references those params; a run supplying "susan"
    # must have its effect resolved to "susan", NOT the frozen demo "phil".
    verifier = CapturingVerifier()
    effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient_id": ValueExpr(param="patient_id"), "type": "Triage"},
            expected_count=1,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"patient_id": ValueExpr(param="patient_id")},
            field="note",
            value=ValueExpr(param="note"),
        ),
    ]
    specs = {
        "patient_id": ParamSpec(name="patient_id", example="phil"),
        "note": ParamSpec(name="note", example="phil-note"),
    }
    workflow = _param_workflow(effects, param_specs=specs)
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(
        FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=verifier
    )

    report = replayer.run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        params={"patient_id": "susan", "note": "susan-note"},
    )

    assert report.success is True
    written, field_eq = verifier.verified
    # The record_written selector was bound to the RUN's patient, not the demo.
    assert str(written.match["patient_id"]) == "susan"
    assert str(written.match["patient_id"]) != "phil"
    # The field_equals value was bound to the RUN's note, not the demo.
    assert str(field_eq.value) == "susan-note"
    assert str(field_eq.value) != "phil-note"


def test_unsupplied_param_falls_back_to_demo_example(tmp_path):
    # Control: with NO caller override the same param-based effect resolves to
    # the recorded demo example -- proving the value tracks the run's params
    # (of which the demo example is the default), not a frozen literal.
    verifier = CapturingVerifier()
    effects = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"patient_id": ValueExpr(param="patient_id")},
            field="note",
            value=ValueExpr(param="note"),
        )
    ]
    specs = {
        "patient_id": ParamSpec(name="patient_id", example="phil"),
        "note": ParamSpec(name="note", example="phil-note"),
    }
    workflow = _param_workflow(effects, param_specs=specs)
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(
        FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=verifier
    )

    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    (field_eq,) = verifier.verified
    assert str(field_eq.value) == "phil-note"
    assert str(field_eq.match["patient_id"]) == "phil"


# -- 2. back-compat: an old plain-string Effect loads + verifies identically -


def test_v1_plain_string_effect_loads_and_verifies_identically(tmp_path):
    # A hand-authored v1 bundle serializes effect values as bare strings. It
    # must load into the ValueExpr fields and verify EXACTLY as before against a
    # matching record -- the parameterization is additive.
    verifier = CapturingVerifier()
    v1_effect_json = {
        "kind": "field_equals",
        "match": {"patient_id": "p1", "type": "Triage"},
        "field": "note",
        "value": "Phil",
        "idempotency_key": "static-key",
    }
    effect = Effect.model_validate(v1_effect_json)
    # Loads as literals that compare and stringify exactly like the old strings.
    assert effect.match == {"patient_id": "p1", "type": "Triage"}
    assert effect.value == "Phil"
    assert effect.idempotency_key == "static-key"

    workflow = _param_workflow([effect])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    # Reload the bundle from disk (full serialize -> validate round-trip).
    reloaded = Workflow.model_validate_json((bundle / "workflow.json").read_text())
    assert reloaded.steps[0].effects[0].value == "Phil"

    replayer = Replayer(
        FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=verifier
    )
    report = replayer.run(reloaded, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    (seen,) = verifier.verified
    # A pure-literal (v1) effect resolves to itself -- byte-for-byte unchanged.
    assert seen.match == {"patient_id": "p1", "type": "Triage"}
    assert seen.value == "Phil"
    assert seen.idempotency_key == "static-key"


# -- 3. the idempotency key is PER-RUN, not the frozen demo literal ----------


def test_idempotency_key_differs_across_runs_with_different_params(tmp_path):
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": ValueExpr(param="patient_id")},
        idempotency_key=ValueExpr(param="visit_id"),
        expected_count=1,
    )
    specs = {
        "patient_id": ParamSpec(name="patient_id", example="phil"),
        "visit_id": ParamSpec(name="visit_id", example="v-demo"),
    }
    workflow = _param_workflow([effect], param_specs=specs)

    v1 = CapturingVerifier()
    r1 = Replayer(FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=v1)
    b1, d1 = _dirs(tmp_path / "run1")
    r1.run(
        workflow,
        bundle_dir=b1,
        run_dir=d1,
        params={"patient_id": "a", "visit_id": "v-001"},
    )

    v2 = CapturingVerifier()
    r2 = Replayer(FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=v2)
    b2, d2 = _dirs(tmp_path / "run2")
    r2.run(
        workflow,
        bundle_dir=b2,
        run_dir=d2,
        params={"patient_id": "b", "visit_id": "v-002"},
    )

    key1 = str(v1.verified[0].idempotency_key)
    key2 = str(v2.verified[0].idempotency_key)
    assert key1 == "v-001"
    assert key2 == "v-002"
    assert key1 != key2  # per-run, not the frozen demo "v-demo"


def test_idempotency_key_can_bind_to_stable_run_identity(tmp_path):
    # Even with no business param for it, an idempotency key bound to the
    # reserved __run_id__ resolves to a distinct value per run.
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1"},
        idempotency_key=ValueExpr(param="__run_id__"),
        expected_count=1,
    )
    workflow = _param_workflow([effect])

    seen = []
    for i in range(2):
        v = CapturingVerifier()
        r = Replayer(FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=v)
        b, d = _dirs(tmp_path / f"run{i}")
        r.run(workflow, bundle_dir=b, run_dir=d)
        seen.append(str(v.verified[0].idempotency_key))

    assert seen[0] and seen[1]
    assert seen[0] != seen[1]  # distinct per run


# -- 4. the resolved contract hash is recorded in the RunReport --------------


def test_resolved_contract_hash_is_recorded(tmp_path):
    verifier = CapturingVerifier()
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": ValueExpr(param="patient_id")},
        field="note",
        value=ValueExpr(param="note"),
    )
    specs = {
        "patient_id": ParamSpec(name="patient_id", example="phil"),
        "note": ParamSpec(name="note", example="phil-note"),
    }
    workflow = _param_workflow([effect], param_specs=specs)
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(
        FakeBackend(), vision=_vision_confirms_saved(), effect_verifier=verifier
    )

    report = replayer.run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        params={"patient_id": "susan", "note": "susan-note"},
    )

    result = report.results[0]
    assert len(result.effect_contract_hashes) == 1
    recorded = result.effect_contract_hashes[0]
    assert recorded.startswith("sha256:")
    # The recorded hash is the digest of the RESOLVED (run) contract, and it
    # differs from the digest of the demo-valued contract.
    resolved = effect.resolve({"patient_id": "susan", "note": "susan-note"})
    demo = effect.resolve({"patient_id": "phil", "note": "phil-note"})
    assert recorded == resolved.contract_hash()
    assert recorded != demo.contract_hash()


# -- 5. end-to-end against the REAL RestRecordVerifier + MockMed -------------


class _ParamWritingBackend(FakeBackend):
    """A fake backend whose consequential keypress POSTs the RUN's record to
    the MockMed system of record (patient/note supplied at construction, as the
    GUI actuation would type them from the run's params)."""

    def __init__(self, sor_url, *, patient_id, note, viewport=(300, 200)):
        super().__init__(viewport=viewport)
        self.sor_url = sor_url.rstrip("/")
        self._patient_id = patient_id
        self._note = note

    def press(self, key):
        super().press(key)
        requests.post(
            f"{self.sor_url}/api/encounter",
            json={"patient_id": self._patient_id, "type": "Triage", "note": self._note},
            timeout=5,
        )


def test_parameterized_effect_confirmed_against_real_sor(tmp_path):
    url, _db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        # The GUI writes the RUN's patient ("susan"); a param-bound effect must
        # resolve to "susan" and CONFIRM against the real record. A frozen demo
        # literal ("phil") would have refuted -- see the control below.
        effects = [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": ValueExpr(param="patient_id"), "type": "Triage"},
                expected_count=1,
                timeout_s=2.0,
            ),
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"patient_id": ValueExpr(param="patient_id")},
                field="note",
                value=ValueExpr(param="note"),
                timeout_s=2.0,
            ),
        ]
        specs = {
            "patient_id": ParamSpec(name="patient_id", example="phil"),
            "note": ParamSpec(name="note", example="phil-note"),
        }
        workflow = _param_workflow(effects, param_specs=specs)
        bundle, run_dir = _dirs(tmp_path)
        backend = _ParamWritingBackend(url, patient_id="susan", note="susan-note")
        replayer = Replayer(
            backend,
            vision=_vision_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(
            workflow,
            bundle_dir=bundle,
            run_dir=run_dir,
            params={"patient_id": "susan", "note": "susan-note"},
        )

        assert report.success is True
        r = report.results[0]
        assert r.effect_verified is True
        assert all("CONFIRMED" in line for line in r.effect_results)
    finally:
        stop()


def test_demo_literal_effect_refutes_when_run_writes_different_patient(tmp_path):
    # The pre-fix hazard, demonstrated: a FROZEN demo-literal effect (patient
    # "phil") verified against a run that wrote patient "susan" refutes/halts --
    # the effect was checking the demonstration's record, not the run's. With
    # param binding (test above) the same run CONFIRMS.
    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        effects = [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": "phil", "type": "Triage"},  # frozen demo value
                expected_count=1,
                timeout_s=1.0,
            )
        ]
        workflow = _param_workflow(effects)
        bundle, run_dir = _dirs(tmp_path)
        backend = _ParamWritingBackend(url, patient_id="susan", note="susan-note")
        replayer = Replayer(
            backend,
            vision=_vision_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.effect_verified is False
        # The run's record DID land -- the demo-literal effect simply looked at
        # the wrong patient.
        assert any(rec["patient_id"] == "susan" for rec in db.snapshot()["records"])
    finally:
        stop()
