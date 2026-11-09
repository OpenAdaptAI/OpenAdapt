"""Durable contract tests for the cross-surface control overlay."""

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    CONTROL_OVERLAY_FRAME_SCHEMA,
    CONTROL_OVERLAY_TIMELINE_SCHEMA,
    ControlOverlayDataClassification,
    ControlOverlayFrameV1,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayTimelineBindingV1,
    ControlOverlayTimelineEventV1,
    ControlOverlayTimelineV1,
)


def _frame(sequence: int, phase: ControlOverlayPhase) -> ControlOverlayFrameV1:
    return ControlOverlayFrameV1.build(
        event_sequence=sequence,
        observed_at_unix_ms=1_785_000_000_000 + sequence,
        observed_at_monotonic_ms=1000.0 + sequence,
        visible=True,
        phase=phase,
        mode=ControlOverlayMode.GOVERNED,
        profile=ControlOverlayProfile.REGULATED,
        current_step=2,
        total_steps=5,
    )


def test_frame_builder_derives_canonical_public_state() -> None:
    frame = _frame(4, ControlOverlayPhase.EXECUTING)

    assert frame.schema_version == CONTROL_OVERLAY_FRAME_SCHEMA
    assert frame.state_id == (
        "visible:executing:governed:regulated:2:5:" "no-pause:no-resume:no-stop"
    )
    assert frame.workflow_label == "Governed workflow"
    assert frame.status == "Executing with verification gates"
    assert frame.presentation is True


def test_frame_refuses_free_form_or_inconsistent_presentation_data() -> None:
    payload = _frame(1, ControlOverlayPhase.EXECUTING).model_dump(mode="json")
    payload["workflow_label"] = "Customer workflow"
    with pytest.raises(ValidationError, match="workflow_label"):
        ControlOverlayFrameV1.model_validate(payload)

    payload = _frame(1, ControlOverlayPhase.EXECUTING).model_dump(mode="json")
    payload["screenshot"] = "secret.png"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ControlOverlayFrameV1.model_validate(payload)

    payload = _frame(1, ControlOverlayPhase.EXECUTING).model_dump(mode="json")
    payload["state_id"] = "visible:verified"
    with pytest.raises(ValidationError, match="state_id"):
        ControlOverlayFrameV1.model_validate(payload)


def test_timeline_is_ordered_and_bound_to_exact_pack_and_media() -> None:
    digest = "a" * 64
    timeline = ControlOverlayTimelineV1(
        data_classification=ControlOverlayDataClassification.SYNTHETIC,
        evidence_pack_id="mockmed-triage-v2",
        media_sha256=digest,
        duration_ms=4000,
        events=(
            ControlOverlayTimelineEventV1(
                at_ms=0,
                frame=_frame(1, ControlOverlayPhase.EXECUTING),
            ),
            ControlOverlayTimelineEventV1(
                at_ms=3000,
                frame=_frame(2, ControlOverlayPhase.VERIFIED),
            ),
        ),
    )

    assert timeline.schema_version == CONTROL_OVERLAY_TIMELINE_SCHEMA
    assert timeline.frame_at(2999).phase == ControlOverlayPhase.EXECUTING
    assert timeline.frame_at(3000).phase == ControlOverlayPhase.VERIFIED
    timeline.assert_binding(
        ControlOverlayTimelineBindingV1(
            evidence_pack_id="mockmed-triage-v2",
            media_sha256=digest,
        )
    )
    with pytest.raises(ValueError, match="exact media digest"):
        timeline.assert_binding(
            ControlOverlayTimelineBindingV1(
                evidence_pack_id="mockmed-triage-v2",
                media_sha256="b" * 64,
            )
        )

    payload = timeline.model_dump(mode="json")
    payload["events"][1]["at_ms"] = 0
    with pytest.raises(ValidationError, match="offsets must increase"):
        ControlOverlayTimelineV1.model_validate(payload)


def test_contracts_export_strict_language_agnostic_json_schema() -> None:
    frame_schema = ControlOverlayFrameV1.model_json_schema()
    timeline_schema = ControlOverlayTimelineV1.model_json_schema()

    assert frame_schema["additionalProperties"] is False
    assert timeline_schema["additionalProperties"] is False
    assert frame_schema["x-openadapt-status-by-phase"]["verified"] == (
        "Outcome verified"
    )
    assert json.dumps(frame_schema).count("openadapt.control-overlay-frame/v1") >= 1
    assert (
        json.dumps(timeline_schema).count("openadapt.control-overlay-timeline/v1") >= 1
    )

    packaged_schemas = files("openadapt_types.schemas")
    assert (
        json.loads(
            packaged_schemas.joinpath("control-overlay-frame-v1.json").read_text()
        )
        == frame_schema
    )
    assert (
        json.loads(
            packaged_schemas.joinpath("control-overlay-timeline-v1.json").read_text()
        )
        == timeline_schema
    )
