"""Semantic and adversarial tests for exact overlay target tracking."""

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    CONTROL_OVERLAY_FRAME_V2_SCHEMA,
    CONTROL_OVERLAY_TIMELINE_V2_SCHEMA,
    ControlOverlayDataClassification,
    ControlOverlayFrameV2,
    ControlOverlayMediaFrameBindingV2,
    ControlOverlayMode,
    ControlOverlayNormalizedRectV2,
    ControlOverlayObservationBindingV2,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlaySourceViewportV2,
    ControlOverlayTargetActionKind,
    ControlOverlayTargetTrackingV2,
    ControlOverlayTimelineEventV2,
    ControlOverlayTimelineV2,
)

DIGEST = "a" * 64
OBSERVATION_HMAC = "b" * 64


def _target(*, frame_index: int | None = None) -> ControlOverlayTargetTrackingV2:
    binding = (
        ControlOverlayObservationBindingV2(observation_hmac_sha256=OBSERVATION_HMAC)
        if frame_index is None
        else ControlOverlayMediaFrameBindingV2(
            media_sha256=DIGEST,
            frame_index=frame_index,
        )
    )
    return ControlOverlayTargetTrackingV2(
        rect=ControlOverlayNormalizedRectV2(
            x=0.1,
            y=0.2,
            width=0.3,
            height=0.1,
        ),
        source_viewport=ControlOverlaySourceViewportV2(
            width_css_px=1280,
            height_css_px=720,
            device_pixel_ratio=2.0,
        ),
        binding=binding,
        action_kind=ControlOverlayTargetActionKind.CLICK,
    )


def _frame(
    sequence: int, target: ControlOverlayTargetTrackingV2 | None
) -> ControlOverlayFrameV2:
    return ControlOverlayFrameV2.build(
        event_sequence=sequence,
        observed_at_unix_ms=1_785_000_000_000 + sequence,
        observed_at_monotonic_ms=1000.0 + sequence,
        visible=True,
        phase=ControlOverlayPhase.EXECUTING,
        mode=ControlOverlayMode.GOVERNED,
        profile=ControlOverlayProfile.STANDARD,
        current_step=sequence,
        total_steps=3,
        target_tracking=target,
    )


def test_v2_is_additive_and_exact_observation_matching_is_fail_closed() -> None:
    frame = _frame(1, _target())

    assert frame.schema_version == CONTROL_OVERLAY_FRAME_V2_SCHEMA
    assert frame.tracking_for_observation(OBSERVATION_HMAC) == frame.target_tracking
    assert frame.tracking_for_observation("c" * 64) is None
    assert ":target-" in frame.state_id
    assert frame.state_id != _frame(1, None).state_id


def test_geometry_and_viewport_refuse_non_finite_or_out_of_bounds_values() -> None:
    with pytest.raises(ValidationError, match="exceeds viewport width"):
        ControlOverlayNormalizedRectV2(x=0.8, y=0.0, width=0.3, height=0.1)
    with pytest.raises(ValidationError):
        ControlOverlayNormalizedRectV2(x=float("nan"), y=0.0, width=0.3, height=0.1)
    with pytest.raises(ValidationError, match="less than or equal to 32768"):
        ControlOverlaySourceViewportV2(
            width_css_px=32769,
            height_css_px=720,
            device_pixel_ratio=2.0,
        )


def test_contract_rejects_selectors_content_and_raw_observation_hash_fields() -> None:
    payload = _target().model_dump(mode="json")
    for forbidden in (
        "selector",
        "accessible_name",
        "typed_value",
        "url",
        "screenshot",
        "viewport_to_presentation",
    ):
        candidate = dict(payload)
        candidate[forbidden] = "private"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ControlOverlayTargetTrackingV2.model_validate(candidate)

    binding = payload["binding"]
    binding["observation_sha256"] = binding.pop("observation_hmac_sha256")
    with pytest.raises(ValidationError):
        ControlOverlayTargetTrackingV2.model_validate(payload)


def test_timeline_exposes_tracking_only_on_the_exact_decoded_frame() -> None:
    timeline = ControlOverlayTimelineV2(
        data_classification=ControlOverlayDataClassification.SYNTHETIC,
        evidence_pack_id="reference-v2",
        media_sha256=DIGEST,
        media_frame_count=120,
        duration_ms=4000,
        events=(
            ControlOverlayTimelineEventV2(
                at_ms=0,
                media_frame_index=0,
                frame=_frame(1, _target(frame_index=0)),
            ),
            ControlOverlayTimelineEventV2(
                at_ms=1000,
                media_frame_index=30,
                frame=_frame(2, _target(frame_index=30)),
            ),
        ),
    )

    assert timeline.schema_version == CONTROL_OVERLAY_TIMELINE_V2_SCHEMA
    assert timeline.tracking_for_media_frame(30) is not None
    assert timeline.tracking_for_media_frame(29) is None
    assert timeline.event_for_media_frame(30) == timeline.events[1]

    payload = timeline.model_dump(mode="json")
    payload["events"][1]["frame"] = _frame(2, _target(frame_index=29)).model_dump(
        mode="json"
    )
    with pytest.raises(ValidationError, match="does not match timeline frame"):
        ControlOverlayTimelineV2.model_validate(payload)


def test_timeline_refuses_live_observation_hmac_target_tracking() -> None:
    with pytest.raises(ValidationError, match="requires a media-frame binding"):
        ControlOverlayTimelineV2(
            data_classification=ControlOverlayDataClassification.SYNTHETIC,
            evidence_pack_id="reference-v2",
            media_sha256=DIGEST,
            media_frame_count=120,
            duration_ms=4000,
            events=(
                ControlOverlayTimelineEventV2(
                    at_ms=0,
                    media_frame_index=0,
                    frame=_frame(1, None),
                ),
                ControlOverlayTimelineEventV2(
                    at_ms=1000,
                    media_frame_index=30,
                    frame=_frame(2, _target()),
                ),
            ),
        )


def test_v2_json_schemas_are_deterministic_packaged_contracts() -> None:
    packaged = files("openadapt_types.schemas")
    for filename, model in (
        ("control-overlay-frame-v2.json", ControlOverlayFrameV2),
        ("control-overlay-timeline-v2.json", ControlOverlayTimelineV2),
    ):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert "selector" not in json.dumps(schema).lower()
        assert json.loads(packaged.joinpath(filename).read_text()) == schema
    assert (
        ControlOverlayTimelineV2.model_json_schema()["x-openadapt-target-validity"]
        == "exact_decoded_media_frame_only"
    )
