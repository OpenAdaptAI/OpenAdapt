"""Temporal lease class: bounded zero-input retry, no partial/unknown retry."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    KeyboardFrameChangedBeforeInput,
    StructuralResolutionRefused,
)
from openadapt_flow.ir import FreshActuationEvent
from openadapt_flow.runtime.keyboard_delivery import deliver_with_revalidation


def changed(operation="physical_type_text"):
    return KeyboardFrameChangedBeforeInput(
        operation=operation,
        changed_pixel_count=1,
        changed_bbox=(1, 1, 1, 1),
        frame_size=(20, 10),
        lease_sha256="a" * 64,
        target_fingerprint="b" * 64,
    )


@given(
    st.integers(min_value=0, max_value=8),
    st.sampled_from(["physical_type_text", "physical_press"]),
)
def test_class_retry_bound_and_single_delivery(mismatches, operation):
    attempts, deliveries, checks, evidence = [], [], [], []

    def deliver():
        attempts.append(True)
        if len(attempts) <= mismatches:
            raise changed(operation)
        deliveries.append(True)
        return "actual-ack"

    def revalidate(exc):
        checks.append(exc.target_fingerprint)
        return True

    if mismatches < 3:
        assert (
            deliver_with_revalidation(
                deliver, revalidate, lambda *args: evidence.append(args)
            )
            == "actual-ack"
        )
    else:
        with pytest.raises(StructuralResolutionRefused):
            deliver_with_revalidation(
                deliver, revalidate, lambda *args: evidence.append(args)
            )
    assert len(attempts) == min(mismatches + 1, 3)
    assert len(deliveries) == int(mismatches < 3)
    assert len(checks) == min(mismatches, 2)
    assert [entry[1] for entry in evidence] == (
        [True] * min(mismatches, 2) + ([False] if mismatches >= 3 else [])
    )


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("lost acknowledgement"),
        ActionDeliveryUncertain(
            operation="physical_press", native=False, cause_type="partial_chord"
        ),
        StructuralResolutionRefused("changed focus"),
    ],
)
def test_unknown_partial_or_different_refusal_never_revalidated(failure):
    calls = []

    def deliver():
        calls.append("input-boundary")
        raise failure

    with pytest.raises(type(failure)):
        deliver_with_revalidation(
            deliver,
            lambda _: calls.append("forbidden"),
            lambda *_: calls.append("forbidden"),
        )
    assert calls == ["input-boundary"]


def test_full_revalidation_refusal_stops_without_second_input():
    calls = []

    def deliver():
        calls.append("input-boundary")
        raise changed()

    with pytest.raises(StructuralResolutionRefused):
        deliver_with_revalidation(
            deliver,
            lambda _: False,
            lambda _, retry, attempt: calls.append((retry, attempt)),
        )
    assert calls == ["input-boundary", (False, 1)]


def test_keyboard_phase_evidence_roundtrips_and_legacy_shape_stays_exact():
    fields = dict(
        attempt=1,
        operation="physical_type_text",
        changed_pixel_count=1,
        changed_bbox=(1, 1, 1, 1),
        frame_size=(20, 10),
        retried=True,
    )
    legacy = FreshActuationEvent(**fields)
    assert (
        "phase" not in legacy.model_dump() and "lease_sha256" not in legacy.model_dump()
    )
    keyboard = FreshActuationEvent(**fields, phase="keyboard", lease_sha256="a" * 64)
    assert (
        FreshActuationEvent.model_validate_json(keyboard.model_dump_json()) == keyboard
    )
    with pytest.raises(ValueError):
        FreshActuationEvent(**fields, phase="keyboard")
    with pytest.raises(ValueError):
        FreshActuationEvent(**fields, lease_sha256="a" * 64)


@pytest.mark.parametrize("action", ["type", "key", "hotkey"])
def test_runtime_revalidates_only_keyboard_phase_without_repeating_focus(
    tmp_path, action
):
    from openadapt_flow.ir import ActionKind, Step, Workflow
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeBackend, FakeVision, Match, click_step, make_png

    class NativeLeasedBackend(FakeBackend):
        observations = 0
        attempted = 0

        def native_keyboard_lease_available(self):
            return True

        def guarded_keyboard_target_fingerprint(self):
            return "b" * 64 if self._guarded_keyboard_point is not None else None

        def guarded_keyboard_frame(self):
            self.observations += 1
            return self._frame

        def type_text_guarded(self, text, **kwargs):
            self.attempted += 1
            if self.attempted == 1:
                self._guarded_keyboard_point = None
                raise changed()
            return super().type_text_guarded(text, **kwargs)

        def press_guarded(self, key, **kwargs):
            self.attempted += 1
            if self.attempted == 1:
                self._guarded_keyboard_point = None
                raise changed("physical_press")
            return super().press_guarded(key, **kwargs)

    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates/btn.png").write_bytes(make_png((50, 20)))
    backend = NativeLeasedBackend()
    vision = FakeVision()
    vision.template_results = [Match((110, 105), (100, 100, 50, 20)) for _ in range(8)]
    step = Step(
        id="keyboard",
        intent="edit transient field",
        action=ActionKind(action),
        anchor=click_step(ocr_text="Field").anchor,
        text="example" if action == "type" else None,
        key="a" if action != "type" else None,
        modifiers=["ctrl"] if action == "hotkey" else [],
    )
    replay = Replayer(backend, vision=vision)
    report = replay.run(
        Workflow(name="test", steps=[step]), bundle_dir=bundle, run_dir=tmp_path / "run"
    )
    assert report.success is True, report.results[0].error
    assert backend.attempted == 2
    assert len([edge for edge in backend.actions if edge[0] == "click"]) == int(
        action == "type"
    )
    assert len([edge for edge in backend.actions if edge[0] in {"press", "type"}]) == 1
    assert backend.observations == 2
    event = report.results[0].fresh_actuation_events[0]
    assert event.phase == "keyboard" and event.retried is True
    assert event.lease_sha256 == "a" * 64
