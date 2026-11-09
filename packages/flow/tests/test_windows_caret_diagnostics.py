"""Pure controls for bounded caret attribution; no native APIs or displays."""

from __future__ import annotations

import copy
import io
import json

import pytest
from PIL import Image

from tests.e2e.windows_caret_diagnostic import close_owned, collect, project


def _samples():
    states = []
    for index in range(8):
        state = {
            "owner": True,
            "focus": True,
            "caret": "owned",
            "visible": bool(index % 2),
            "rect": [26, 10, 27, 25],
            "origin": [0, 0],
            "virtual_size": [64, 48],
            "capture_origin": [0, 0],
            "capture_size": [64, 48],
            "anchor_matches": True,
            "single_monitor": True,
            "dpi": [100, 96],
            "blink_ms": 500,
        }
        states.append(
            {
                "before": state,
                "after": copy.deepcopy(state),
                "started_ns": index * 250_000_000,
                "ended_ns": index * 250_000_000 + 1_000_000,
                "scheduled_ns": index * 250_000_000,
                "sha256": ("a" if index % 2 else "b") * 64,
                "size": [64, 48],
                "difference": None
                if not index
                else {
                    "status": "available",
                    "code": "raster_compared",
                    "expected_dimensions": [64, 48],
                    "current_dimensions": [64, 48],
                    "changed_pixel_count": 15,
                    "changed_bbox": [26, 10, 27, 25],
                },
            }
        )
    return states


def test_only_stable_owned_visibility_and_raster_transition_correlate():
    samples = _samples()
    original = copy.deepcopy(samples)
    result = project(samples)
    assert result["status"] == "CARET_CORRELATED"
    assert result["transition_pairs"] == 7
    assert result["captures"] == 8
    assert samples == original
    assert set(result) == {
        "status",
        "code",
        "captures",
        "dimensions",
        "frame_sha256",
        "capture_duration_ms",
        "lateness_ms",
        "visibility",
        "changed_pixel_count",
        "changed_bbox",
        "transition_pairs",
    }
    assert not {"pid", "hwnd", "title", "text", "image", "rect", "origin"}.intersection(
        result
    )


@pytest.mark.parametrize(
    "change",
    [
        "outside",
        "no_transition",
        "owner",
        "foreign",
        "unknown",
        "focus",
        "dpi",
        "origin",
        "mixed_bounds",
        "anchor",
        "two_inversions",
        "equal_interval",
        "period_changed",
        "inside_bracket",
        "malformed",
        "extra",
        "oversized_time",
    ],
)
def test_false_or_unbound_correlation_remains_unknown(change):
    samples = _samples()
    a, b = samples[3]["before"], samples[3]["after"]
    if change == "outside":
        samples[3]["difference"]["changed_bbox"] = [25, 10, 27, 25]
    elif change == "no_transition":
        for sample in samples:
            sample["before"]["visible"] = sample["after"]["visible"] = False
    elif change in {"owner", "focus", "anchor"}:
        a[{"owner": "owner", "focus": "focus", "anchor": "anchor_matches"}[change]] = (
            False
        )
    elif change in {"foreign", "unknown"}:
        a["caret"] = "foreign" if change == "foreign" else "unavailable"
    elif change == "dpi":
        a["dpi"] = b["dpi"] = [100, 144]
    elif change == "origin":
        a["origin"] = b["origin"] = [20, 0]
    elif change == "mixed_bounds":
        a["rect"] = b["rect"] = [25, 10, 27, 25]
    elif change in {"two_inversions", "equal_interval"}:
        samples[3]["ended_ns"] = samples[3]["started_ns"] + (
            1_000_000_000 if change == "two_inversions" else 500_000_000
        )
    elif change == "period_changed":
        a["blink_ms"] = b["blink_ms"] = 600
    elif change == "inside_bracket":
        b["visible"] = not a["visible"]
    elif change == "malformed":
        a["caret"] = {"sensitive": "value"}
    elif change == "extra":
        a["sensitive"] = "value"
    else:
        samples[3]["ended_ns"] = 10**400
    result = project(samples)
    assert result["status"] == "UNKNOWN"
    assert "sensitive" not in json.dumps(result)


def test_no_observed_motion_is_unknown_not_a_caret_cause():
    samples = _samples()
    for sample in samples:
        sample["before"]["visible"] = sample["after"]["visible"] = False
        if sample["difference"]:
            sample["difference"].update(changed_pixel_count=0, changed_bbox=None)
    assert project(samples)["code"] == "no_visibility_transition"


def test_capture_exception_is_identical_and_is_never_retried():
    error = RuntimeError("synthetic original capture error")
    calls = []

    def capture():
        calls.append(1)
        raise error

    with pytest.raises(RuntimeError) as caught:
        collect(
            capture,
            lambda: _samples()[0]["before"],
            clock=lambda: 0,
            sleep=lambda _: None,
        )
    assert caught.value is error
    assert calls == [1]


def test_fixed_schedule_does_not_resample_late_or_static_frames():
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(stream, format="PNG")
    ticks = iter(range(0, 10_000_000_000, 300_000_000))
    captures = []
    sleeps = []

    def capture():
        captures.append(1)
        return stream.getvalue()

    samples = collect(
        capture,
        lambda: copy.deepcopy(_samples()[0]["before"]),
        clock=lambda: next(ticks),
        sleep=sleeps.append,
    )
    assert len(captures) == len(samples) == 8
    assert sleeps == []
    assert any(s["started_ns"] > s["scheduled_ns"] for s in samples)
    assert project(samples)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "field,value", [("dpi", [125, 120]), ("origin", [20, 0]), ("single_monitor", False)]
)
def test_coherently_wrong_mapping_is_still_unknown(field, value):
    samples = _samples()
    for sample in samples:
        for key in ("before", "after"):
            sample[key][field] = value
            if field == "origin":
                sample[key]["capture_origin"] = value
    assert project(samples)["code"] == "mapping_unavailable"


@pytest.mark.parametrize("prior,failed", [(False, False), (False, True), (True, True)])
def test_cleanup_really_waits_both_exact_children_and_preserves_first_error(
    prior, failed
):
    events = []
    original = RuntimeError("original synthetic error")
    cleanup_error = OSError("synthetic cleanup error")

    class Child:
        def __init__(self, name):
            self.name = name

        def poll(self):
            return None if failed else 0

        def kill(self):
            events.append((self.name, "kill"))
            if self.name == "app":
                raise cleanup_error

        def wait(self, *, timeout):
            assert timeout == 10
            events.append((self.name, "wait"))
            if failed and self.name == "app":
                raise RuntimeError("secondary wait error")
            return 0

    cleanup = {"app_waited": False, "agent_waited": False}
    result = close_owned(
        {"app": Child("app"), "agent": Child("agent")},
        cleanup,
        original if prior else None,
    )
    assert ("app", "wait") in events and ("agent", "wait") in events
    assert cleanup["agent_waited"] is True
    assert cleanup["app_waited"] is (not failed)
    assert result is (original if prior else cleanup_error if failed else None)
    if not failed:
        assert events == [("app", "wait"), ("agent", "wait")]
