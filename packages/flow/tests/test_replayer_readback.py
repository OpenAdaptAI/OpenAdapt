"""Replayer wiring for the auto-derived on-screen read-back oracle.

These pin the no-connector default path: a GUI-only bundle whose consequential
step declares a valid bounded on-screen read-back effect (either reset-backed
different-path reacquisition at tier 3, or bounded same-screen read-back at
tier 4) is verified out of the box with no external ``effect_verifier``
configured. Unbounded, unstructured, or unadmitted demonstrated effects still
refuse to auto-default and stop with ``no EffectVerifier``.

No model calls, no network: the record truth is modelled by the backend.
"""

from __future__ import annotations

from types import SimpleNamespace

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ReadbackNav,
    ReadbackReset,
    ReadbackSpec,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match

NOTE = "chest pain follow up in two weeks"
REGION = (100, 200, 300, 40)
RENAV = [
    ReadbackNav(action="click", point=(10, 10)),  # close the form
    ReadbackNav(action="click", point=(50, 60)),  # re-open the record
]


class ReadbackBackend(FakeBackend):
    """Models the app's persistence + a different-path re-open.

    ``press('Enter')`` is the SAVE: it persists the note unless ``phantom`` (the
    optimistic-UI success the record never received). The read-back's
    re-navigation (clicks) re-opens the record, so ``view`` becomes what the
    record actually holds — the note for a real save, nothing for a phantom.
    """

    def __init__(self, *, phantom: bool = False, viewport=(400, 300)):
        super().__init__(viewport=viewport)
        self.phantom = phantom
        self._persisted = ""
        self.view = "editing note"  # the write's own form (same-surface)

    def press(self, key):
        super().press(key)
        if key == "Enter":
            self._persisted = "" if self.phantom else NOTE

    def click(self, x, y, *, double=False):
        # Re-opening the record re-reads what the system actually persisted.
        self.view = self._persisted


def _ocr_from(backend: ReadbackBackend):
    def ocr(png: bytes, *, region=None):
        if not backend.view:
            return []
        return [SimpleNamespace(text=backend.view, confidence=0.95, region=region)]

    return ocr


def _readback_workflow() -> Workflow:
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=NOTE,
        risk="irreversible",
        readback=ReadbackSpec(
            region=REGION,
            different_path=True,
            renavigation=RENAV,
            reset=ReadbackReset(kind="reopen_window", at=0),
        ),
    )
    return Workflow(
        name="save",
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
                risk="irreversible",
                effects=[effect],
            )
        ],
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


def test_auto_default_readback_confirms_real_save(tmp_path, monkeypatch):
    import openadapt_flow.vision as vision_mod

    backend = ReadbackBackend(phantom=False)
    monkeypatch.setattr(vision_mod, "ocr", _ocr_from(backend))
    bundle, run_dir = _dirs(tmp_path)

    # NO effect_verifier configured — the different-path read-back is the
    # out-of-the-box default oracle.
    replayer = Replayer(backend, vision=_vision_confirms_saved(), poll_interval_s=0.01)
    report = replayer.run(_readback_workflow(), bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    r = report.results[0]
    assert r.effect_verified is True
    assert any("CONFIRMED" in line for line in r.effect_results)
    assert any("DIFFERENT-PATH" in line for line in r.effect_results)
    assert report.model_calls == 0


def test_auto_default_readback_halts_phantom_save(tmp_path, monkeypatch):
    import openadapt_flow.vision as vision_mod

    backend = ReadbackBackend(phantom=True)
    monkeypatch.setattr(vision_mod, "ocr", _ocr_from(backend))
    bundle, run_dir = _dirs(tmp_path)

    replayer = Replayer(backend, vision=_vision_confirms_saved(), poll_interval_s=0.01)
    report = replayer.run(_readback_workflow(), bundle_dir=bundle, run_dir=run_dir)

    # THE SAFETY PROPERTY: the screen said "Saved" but the record has nothing;
    # the different-path re-open finds no note -> HALT, never a false CONFIRM.
    assert report.success is False
    r = report.results[0]
    assert r.effect_verified is False
    assert report.model_calls == 0


def test_same_surface_bounded_effect_auto_verifies_at_tier_four(tmp_path, monkeypatch):
    import openadapt_flow.vision as vision_mod

    backend = ReadbackBackend(phantom=False)
    backend.view = NOTE
    monkeypatch.setattr(vision_mod, "ocr", _ocr_from(backend))
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=NOTE,
        risk="irreversible",
        readback=ReadbackSpec(region=REGION, different_path=False),
    )
    wf = Workflow(
        name="save-in-place",
        steps=[
            Step(
                id="save",
                intent="save in place",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                effects=[effect],
            )
        ],
    )
    bundle, run_dir = _dirs(tmp_path / "confirm")
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    r = report.results[0]
    assert r.effect_verified is True
    assert r.effect_evidence[0].verification_tier == 4
    assert any("SAME-SURFACE" in line for line in r.effect_results)

    # Wrong on-screen text halts (REFUTED), never a silent pass.
    backend.view = "stale previous value"
    bad_bundle, bad_run = _dirs(tmp_path / "refute")
    bad_report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bad_bundle, run_dir=bad_run
    )
    assert bad_report.success is False
    assert bad_report.results[0].effect_verified is False


def test_unbounded_or_unadmitted_effect_is_not_auto_default(tmp_path):
    # Structured effects without readback, unbounded readbacks (region=None),
    # and unadmitted demonstrated checks must NOT auto-default; they stop with
    # no EffectVerifier configured.
    cases = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"id": "1"},
            risk="irreversible",
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            value=NOTE,
            risk="irreversible",
            readback=ReadbackSpec(region=None, different_path=False),
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            value=NOTE,
            risk="irreversible",
            readback=ReadbackSpec(
                region=REGION,
                different_path=True,
                renavigation=RENAV,
                reset=ReadbackReset(kind="reopen_window", at=0),
                demonstrated=True,
            ),
        ),
    ]
    for index, effect in enumerate(cases):
        backend = ReadbackBackend(phantom=True)
        wf = Workflow(
            name="save",
            steps=[
                Step(
                    id="save",
                    intent="save",
                    action=ActionKind.KEY,
                    key="Enter",
                    risk="irreversible",
                    effects=[effect],
                )
            ],
        )
        bundle, run_dir = _dirs(tmp_path / f"case-{index}")
        replayer = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01)
        report = replayer.run(wf, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.effect_verified is False
        assert "no EffectVerifier" in " ".join(
            r.effect_results
        ) or "no EffectVerifier" in (r.error or "")
