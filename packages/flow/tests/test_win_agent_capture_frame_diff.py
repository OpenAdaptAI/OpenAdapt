"""Software controls for post-decision frame differences, never native capture."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

from tests.e2e import win_agent_capture_telemetry as telemetry_module
from tests.test_win_agent_capture_telemetry import _guard, _RunningServer

FIELDS = {
    "status",
    "code",
    "expected_dimensions",
    "current_dimensions",
    "changed_pixel_count",
    "changed_bbox",
}


def png(rgb: bytes, width: int = 4, height: int = 3) -> bytes:
    with Image.frombytes("RGB", (width, height), rgb) as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def pair():
    before = bytes([10, 20, 30]) * 12
    after = bytearray(before)
    for x, y, channel in ((1, 0, 0), (2, 1, 1), (3, 2, 2)):
        after[(y * 4 + x) * 3 + channel] += 1
    return before, bytes(after)


def event(owner, expected_png, current_sequence):
    owner.refused_guard(hashlib.sha256(expected_png).hexdigest(), current_sequence)
    payload = json.loads(owner.sidecar.read_text())
    return payload["guarded_refusals"][-1]


def assert_unavailable(value):
    assert set(value) == FIELDS
    assert value["status"] == "unavailable"
    assert value["code"] in {
        "raster_missing", "raster_invalid", "dimension_mismatch", "raster_size_limit"
    }
    assert value["changed_pixel_count"] is None
    assert value["changed_bbox"] is None


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_one_unit_in_each_channel_counts_one_pixel_without_grayscale_loss(channel):
    before = bytes([100, 100, 100]) * 12
    after = bytearray(before)
    after[(1 * 4 + 2) * 3 + channel] += 1
    assert telemetry_module.compare_captured_rasters(
        (before, 4, 3), (bytes(after), 4, 3)
    ) == {
        "status": "available", "code": "raster_compared",
        "expected_dimensions": [4, 3], "current_dimensions": [4, 3],
        "changed_pixel_count": 1, "changed_bbox": [2, 1, 3, 2],
    }


def test_changed_pixel_count_is_channel_union_and_bbox_is_half_open():
    before, after = pair()
    result = telemetry_module.compare_captured_rasters((before, 4, 3), (after, 4, 3))
    assert set(result) == FIELDS
    assert result["changed_pixel_count"] == 3
    assert result["changed_bbox"] == [1, 0, 4, 3]
    all_channels = bytearray(before)
    all_channels[0:3] = b"\xff\xff\xff"
    assert telemetry_module.compare_captured_rasters(
        (before, 4, 3), (bytes(all_channels), 4, 3)
    )["changed_pixel_count"] == 1


def test_equal_rasters_have_zero_changed_pixels_and_no_bbox():
    before, _ = pair()
    result = telemetry_module.compare_captured_rasters((before, 4, 3), (before, 4, 3))
    assert result["status"] == "available"
    assert result["changed_pixel_count"] == 0
    assert result["changed_bbox"] is None


@pytest.mark.parametrize(
    "invalid",
    [None, (), (b"", 4), (b"", 4, 3), (b"123", True, 1),
     (b"123", 1, False), (b"123", 1.0, 1), ("123", 1, 1),
     (b"123", -1, 1), (b"123", 1, 0), (b"123", 4096, 2160)],
)
def test_missing_malformed_or_oversized_never_decodes_or_returns_measurement(
    monkeypatch, invalid
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid raster reached decoder")
    monkeypatch.setattr(Image, "frombytes", forbidden)
    result = telemetry_module.compare_captured_rasters(invalid, (b"123", 1, 1))
    assert_unavailable(result)


def test_dimension_mismatch_never_compares_even_with_equal_byte_length():
    result = telemetry_module.compare_captured_rasters((b"123456", 2, 1), (b"123456", 1, 2))
    assert_unavailable(result)
    assert result["code"] == "dimension_mismatch"


def test_real_cache_deduplicates_and_evicts_without_unbounded_storage(tmp_path):
    assert telemetry_module._RASTER_CACHE_IMAGE_LIMIT == 2
    assert telemetry_module._MAX_RASTER_PIXELS == 8_294_400
    assert telemetry_module._MAX_RASTER_IMAGE_BYTES == 24_883_200
    assert telemetry_module._MAX_RASTER_CACHE_BYTES == 49_766_400
    owner = telemetry_module.CaptureTelemetry(tmp_path / "cache.json")
    first = png(bytes([10, 20, 30]) * 12)
    owner.capture(first)
    owner.capture(first)
    assert len(owner._raster_cache) == 1
    assert owner._raster_cache.byte_count == 36
    for value in range(4):
        owner.capture(png(bytes([value, 20, 30]) * 12))
        assert len(owner._raster_cache) <= 2
        assert owner._raster_cache.byte_count <= 72
    result = event(owner, first, 6)["pixel_diff"]
    assert_unavailable(result)


def test_duplicate_expected_provenance_stays_unknown_with_bounded_spatial_fact(tmp_path):
    owner = telemetry_module.CaptureTelemetry(tmp_path / "duplicates.json")
    before, after = pair()
    first, changed = png(before), png(after)
    assert owner.capture(first) == 1
    assert owner.capture(first) == 2
    assert owner.capture(changed) == 3
    measured = event(owner, first, 3)["pixel_diff"]
    assert measured["changed_pixel_count"] == 3
    assert measured["changed_bbox"] == [1, 0, 4, 3]
    projected = telemetry_module.safe_projection(owner.sidecar)["diagnoses"][0]
    assert projected["status"] == "unknown"
    assert projected["code"] == "ambiguous_or_missing_provenance"
    assert projected["pixel_diff"] == measured
    # Later captures cannot recompute the refusal against a newer frame.
    owner.capture(png(bytes([50, 60, 70]) * 12))
    later = telemetry_module.safe_projection(owner.sidecar)["diagnoses"][0]
    assert later["pixel_diff"] == measured
    assert later["status"] == "unknown"


def test_future_expected_capture_cannot_authorize_spatial_comparison(tmp_path):
    owner = telemetry_module.CaptureTelemetry(tmp_path / "future.json")
    before, after = pair()
    first = png(before)
    owner.capture(png(after))
    owner.capture(first)
    assert_unavailable(event(owner, first, 1)["pixel_diff"])


def test_inconsistent_prior_expected_rasters_are_unavailable(tmp_path):
    owner = telemetry_module.CaptureTelemetry(tmp_path / "inconsistent.json")
    before, after = pair()
    first = png(before)
    owner.capture(first)
    owner.capture(first)
    owner._captures[0]["rgb_sha256"] = "0" * 64
    owner.capture(png(after))
    assert_unavailable(event(owner, first, 3)["pixel_diff"])


def test_exact_guard_sequence_must_be_unique(tmp_path):
    owner = telemetry_module.CaptureTelemetry(tmp_path / "ambiguous.json")
    before, after = pair()
    first = png(before)
    owner.capture(first)
    owner.capture(png(after))
    owner._captures.append(dict(owner._captures[-1]))
    owner.refused_guard(hashlib.sha256(first).hexdigest(), 2)
    payload = json.loads(owner.sidecar.read_text())
    assert not payload["guarded_refusals"]


@pytest.mark.parametrize("failure", [False, True])
def test_actual_http_refusal_compares_only_after_decision_and_never_changes_it(
    tmp_path, monkeypatch, failure
):
    before, after = pair()
    first, changed = png(before), png(after)
    running = _RunningServer(monkeypatch, tmp_path, [first, changed])
    original = telemetry_module.compare_captured_rasters
    calls = []
    def compare(*args, **kwargs):
        running.processing_events.append("pixel_diff")
        calls.append(True)
        if failure:
            raise RuntimeError("PRIVATE_DIAGNOSTIC_FAILURE")
        return original(*args, **kwargs)
    monkeypatch.setattr(telemetry_module, "compare_captured_rasters", compare)
    try:
        response = running.session.get(f"{running.url}/screenshot", timeout=5)
        assert response.status_code == 200 and response.content == first
        guarded = _guard(running.session, running.url, first)
        assert guarded.status_code == 409
        assert guarded.json()["code"] == "stale_frame"
        assert running.cursor == 2 and running.context_calls == 0
        assert running.input_calls == [] and calls == [True]
        events = running.processing_events
        assert events.index("stale_decision") < events.index("pixel_diff")
        assert events.index("pixel_diff") < events.index("guard_response_started")
        assert "PRIVATE_DIAGNOSTIC_FAILURE" not in running.sidecar.read_text()
        if not failure:
            diagnosis = telemetry_module.safe_projection(running.sidecar)["diagnoses"][0]
            assert diagnosis["pixel_diff"]["changed_pixel_count"] == 3
    finally:
        running.close()


def test_successful_guard_never_runs_comparison_or_changes_original_outcome(tmp_path, monkeypatch):
    before, _ = pair()
    frame = png(before)
    running = _RunningServer(monkeypatch, tmp_path, [frame, frame])
    monkeypatch.setattr(telemetry_module, "compare_captured_rasters", lambda *a, **k: pytest.fail("success must not compare"))
    try:
        assert running.session.get(f"{running.url}/screenshot", timeout=5).content == frame
        response = _guard(running.session, running.url, frame)
        assert response.status_code == 200
        assert running.cursor == 2 and running.context_calls == 1
        assert len(running.input_calls) == 1
        assert telemetry_module.safe_projection(running.sidecar) == {"status": "available", "diagnoses": []}
    finally:
        running.close()


@pytest.mark.parametrize("limit,value", [
    ("_MAX_RASTER_PIXELS", 11),
    ("_MAX_RASTER_IMAGE_BYTES", 35),
    ("_MAX_RASTER_CACHE_BYTES", 35),
])
def test_cache_refuses_image_exceeding_each_byte_or_pixel_bound(tmp_path, monkeypatch, limit, value):
    monkeypatch.setattr(telemetry_module, limit, value)
    owner = telemetry_module.CaptureTelemetry(tmp_path / "bounded.json")
    before, _ = pair()
    owner.capture(png(before))
    assert len(owner._raster_cache) == 0
    assert owner._raster_cache.byte_count == 0


@pytest.mark.parametrize("field,value", [
    ("changed_pixel_count", -1),
    ("changed_pixel_count", True),
    ("changed_pixel_count", 13),
    ("changed_bbox", [-1, 0, 4, 3]),
    ("changed_bbox", [1, 0, 5, 3]),
    ("changed_bbox", [2, 0, 1, 3]),
    ("changed_bbox", [1, 0, 1, 3]),
    ("changed_bbox", [True, 0, 4, 3]),
    ("expected_dimensions", [4, 4]),
    ("code", "PRIVATE_ARBITRARY_REASON"),
    ("unknown", "PRIVATE_ARBITRARY_PAYLOAD"),
])
def test_projection_rejects_malformed_spatial_values_without_export(tmp_path, field, value):
    owner = telemetry_module.CaptureTelemetry(tmp_path / "malformed.json")
    before, after = pair()
    first = png(before)
    owner.capture(first)
    owner.capture(png(after))
    event(owner, first, 2)
    payload = json.loads(owner.sidecar.read_text())
    payload["guarded_refusals"][0]["pixel_diff"][field] = value
    owner.sidecar.write_text(json.dumps(payload))
    assert telemetry_module.safe_projection(owner.sidecar) == {
        "status": "unavailable", "code": "invalid_capture_telemetry"
    }
