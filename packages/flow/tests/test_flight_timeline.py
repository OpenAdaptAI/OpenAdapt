"""The flight run directory: flight.json shape, scrubbed timeline, viewer."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from openadapt_flow.flight.timeline import (
    FLIGHT_SCHEMA,
    WITHHELD,
    Timeline,
    render_html,
    validate_flight,
)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1600, 900), (200, 10, 10)).save(out, format="PNG")
    return out.getvalue()


def _timeline(tmp_path, **kw):
    return Timeline(tmp_path / "run", task_id="task-1", executor_label="worker", **kw)


def test_flight_json_has_exactly_the_ledger_keys(tmp_path):
    timeline = _timeline(tmp_path)
    timeline.model_call(
        provider="anthropic",
        model="claude-opus-5-5",
        usage={"input_tokens": 10, "output_tokens": 2},
        cost_usd=0.00008,
        streamed=False,
        status=200,
        seconds=0.1,
    )
    timeline.gate_event("held", "the target reads 'Save' (save)")
    summary = timeline.finish(details={"exit_code": 0})
    on_disk = json.loads((tmp_path / "run/flight.json").read_text())
    assert on_disk == summary
    assert set(summary) == {
        "schema",
        "task_id",
        "executor_label",
        "started_at",
        "ended_at",
        "model",
        "actions_count",
        "gate_events",
        "timeline_path",
        "details",
    }
    assert summary["schema"] == FLIGHT_SCHEMA
    assert set(summary["model"]) == {
        "calls",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "cost_source",
    }
    assert summary["model"]["cost_source"] == "price_table"
    assert set(summary["gate_events"][0]) == {"at", "kind", "reason"}
    assert validate_flight(summary) == []
    assert (tmp_path / "run/timeline.html").exists()


def test_an_unpriced_call_makes_the_whole_cost_unknown(tmp_path):
    timeline = _timeline(tmp_path)
    for model, cost in (("claude-opus-5-5", 0.01), ("other-model", None)):
        timeline.model_call(
            provider="x",
            model=model,
            usage={"input_tokens": 1},
            cost_usd=cost,
            streamed=False,
            status=200,
            seconds=0.0,
        )
    summary = timeline.finish()
    assert summary["model"]["cost_usd"] is None
    assert summary["model"]["cost_source"] == "unknown"
    assert summary["details"]["priced_cost_usd"] == pytest.approx(0.01)
    assert summary["details"]["unpriced_models"] == ["other-model"]


def test_no_calls_is_zero_cost_not_unknown(tmp_path):
    summary = _timeline(tmp_path).finish()
    assert summary["model"] == {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "no_calls",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("task_id"),
        lambda d: d.update(extra=1),
        lambda d: d.update(schema="other/v1"),
        lambda d: d["model"].update(cost_source="guess"),
        lambda d: d["model"].update(cost_usd=1.0, cost_source="unknown"),
        lambda d: d["model"].update(calls=-1),
        lambda d: d.update(gate_events=[{"at": "x", "kind": "maybe", "reason": ""}]),
    ],
)
def test_validation_rejects_a_wrong_shape(tmp_path, mutate):
    summary = _timeline(tmp_path).finish()
    mutate(summary)
    assert validate_flight(summary)


def test_without_a_scrubber_values_and_frames_are_withheld(tmp_path):
    timeline = _timeline(tmp_path)
    assert timeline.local_text("Zyxwv Qtest") == WITHHELD
    assert timeline.frame(_png()) is None
    timeline.executor_action(
        dialect="anthropic",
        action={"action": "type", "text": "Zyxwv Qtest"},
        commit_class=False,
        label="textbox Zyxwv",
        label_source="accessibility",
        decision="performed",
        frame_png=_png(),
    )
    raw = (tmp_path / "run/timeline.jsonl").read_text()
    assert "Zyxwv" not in raw
    assert list((tmp_path / "run/frames").iterdir()) == []


def test_a_failing_scrubber_withholds_too(tmp_path):
    def broken(_: object) -> object:
        raise RuntimeError("no")

    timeline = _timeline(tmp_path, scrub_image=broken, scrub_text=broken)
    assert timeline.local_text("Zyxwv") == WITHHELD
    assert timeline.frame(_png()) is None


def test_frames_are_scrubbed_thumbnails_written_once(tmp_path):
    black = io.BytesIO()
    Image.new("RGB", (1600, 900), (0, 0, 0)).save(black, format="PNG")
    timeline = _timeline(tmp_path, scrub_image=lambda png: black.getvalue())
    first = timeline.frame(_png())
    again = timeline.frame(_png())
    assert first == again and first is not None
    thumb = Image.open(tmp_path / "run" / first)
    assert thumb.width == 640
    assert thumb.convert("RGB").getpixel((3, 3)) == (0, 0, 0)


def test_the_viewer_escapes_what_it_shows(tmp_path):
    timeline = _timeline(tmp_path, scrub_text=lambda text: text)
    timeline.gate_event("refused", "<script>alert(1)</script>", label="<b>Save</b>")
    timeline.finish()
    page = (tmp_path / "run/timeline.html").read_text()
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert render_html(tmp_path / "run").name == "timeline.html"
