"""Replayer wiring for the auto-derived on-screen read-back oracle.

These pin the no-connector DEFAULT path: a GUI-only bundle whose consequential
step declares an auto-derived DIFFERENT-PATH on-screen read-back effect is
verified out of the box with NO ``effect_verifier`` configured — the replayer
wires a backend-bound read-back verifier, re-navigates to re-open the record,
and rules on the re-read value. The safety property under test is asymmetric: a
genuine save CONFIRMS and proceeds; a PHANTOM save (the record never persisted)
is HALTED (never a false CONFIRM), even though the write's own screen said
"Saved".

No model calls, no network — the record truth is modelled by the backend.
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
        readback=ReadbackSpec(region=REGION, different_path=True, renavigation=RENAV),
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


def test_same_surface_only_effect_is_not_auto_default(tmp_path):
    # A SAME-SURFACE (weak) read-back must NOT be auto-verified with no verifier
    # configured — it falls through to the fail-safe HALT (non-default).
    backend = ReadbackBackend(phantom=True)
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=NOTE,
        risk="irreversible",
        readback=ReadbackSpec(region=REGION, different_path=False),
    )
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
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01)
    report = replayer.run(wf, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    r = report.results[0]
    assert r.effect_verified is False
    assert "no EffectVerifier" in " ".join(r.effect_results) or "no EffectVerifier" in (
        r.error or ""
    )
