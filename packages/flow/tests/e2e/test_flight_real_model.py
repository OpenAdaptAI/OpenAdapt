"""Optional smoke: a real Claude call through the flight proxy.

Skipped unless ANTHROPIC_API_KEY is in the environment and the real
openadapt-privacy scrubber works. One synthetic screen (a form with a
planted name and a Save button) goes to the real API through the proxy;
Claude's click on Save is held by GatedComputer and never performed. With
OPENADAPT_FLIGHT_EVIDENCE=1 the result is written to
``benchmark/flight/real_model_smoke.json``. The key is read from the
environment only and never printed.
"""

from __future__ import annotations

import base64
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_flow.flight.computer import GatedComputer, HeldResult
from openadapt_flow.flight.gate import RefuseGate
from openadapt_flow.flight.proxy import DEFAULT_UPSTREAMS, FlightProxy
from openadapt_flow.flight.timeline import Timeline, validate_flight

EVIDENCE = (
    Path(__file__).resolve().parents[2] / "benchmark/flight/real_model_smoke.json"
)
MODEL = "claude-opus-5-5"
NAME = "John Smith"


def _screen() -> tuple[bytes, tuple[int, int, int, int]]:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1024, 640), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    draw.text((60, 60), "Synthetic intake form", fill=(20, 20, 20), font=font)
    draw.text((60, 160), f"Patient: {NAME}", fill=(20, 20, 20), font=font)
    draw.text((60, 220), "Note: follow up in two weeks", fill=(20, 20, 20), font=font)
    box = (760, 520, 940, 590)
    draw.rectangle(box, fill=(30, 90, 200))
    draw.text((815, 538), "Save", fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), box


def test_a_real_claude_click_on_save_is_metered_scrubbed_and_held(tmp_path):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not set")
    anthropic = pytest.importorskip("anthropic")
    from openadapt_flow.privacy import _build_provider

    scrubber = _build_provider()
    if scrubber is None:
        pytest.skip("openadapt-privacy is not installed")
    try:
        scrubber.scrub_text("probe")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"the Presidio model is unavailable: {exc}")

    png, box = _screen()
    timeline = Timeline(
        tmp_path / "run",
        task_id="real-model-smoke",
        executor_label="anthropic-sdk",
        scrub_text=lambda t: scrubber.scrub_text(t),
    )
    proxy = FlightProxy(
        timeline, upstreams=DEFAULT_UPSTREAMS, scrubber=scrubber, max_cost_usd=0.50
    ).start()
    performed: list[dict] = []

    def inner(**kwargs):
        if kwargs.get("action") != "screenshot":
            performed.append(kwargs)
        return {"base64_image": base64.b64encode(png).decode()}

    def label_at(x: int, y: int):
        inside = box[0] <= x <= box[2] and box[1] <= y <= box[3]
        return "button Save" if inside else "text"

    computer = GatedComputer(inner, RefuseGate(), timeline, label_at=label_at)
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode(),
        },
    }
    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Record: {NAME}."},
                {
                    "type": "text",
                    "text": "The form is complete and checked. Click the blue "
                    "Save button now; do not ask first.",
                },
                image_block,
            ],
        }
    ]
    actions: list[str] = []
    said = ""
    held_saves = 0
    try:
        client = anthropic.Anthropic(
            base_url=proxy.base_url("anthropic"), max_retries=0
        )
        # A short reference loop: the model may look first (screenshot,
        # mouse_move) before it clicks; stop once a Save click is held.
        for _turn in range(4):
            message = client.messages.create(
                model=MODEL,
                max_tokens=400,
                tools=[
                    {
                        "type": "computer_toolset_20260801",
                        "configs": {"zoom": {"enabled": False}},
                    }
                ],
                messages=messages,
            )
            uses = [b for b in message.content if b.type == "tool_use"]
            said = " ".join(b.text for b in message.content if b.type == "text")
            if not uses:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump() for b in message.content],
                }
            )
            results = []
            for block in uses:
                action = dict(block.input)
                if block.name != "computer":
                    action = {"action": block.name, **action}
                actions.append(str(action.get("action")))
                result = computer(**action)
                if isinstance(result, HeldResult):
                    held_saves += 1
                    content: list[dict] = result.content()
                elif action.get("action") == "screenshot":
                    content = [image_block]
                else:
                    content = [{"type": "text", "text": "done"}]
                answer: dict = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                }
                # A toolset member's result names its toolset.
                toolset = getattr(block, "toolset_name", None)
                if toolset or block.name != "computer":
                    answer["toolset_name"] = toolset or "computer"
                results.append(answer)
            messages.append({"role": "user", "content": results})
            if held_saves:
                break
    finally:
        proxy.stop()
    summary = timeline.finish(details={"model_requested": MODEL})
    assert validate_flight(summary) == []
    assert summary["model"]["calls"] >= 1
    assert summary["model"]["input_tokens"] > 0
    assert summary["model"]["cost_source"] == "price_table"
    assert held_saves >= 1, (
        f"no Save click was held; the model asked for {actions} and said {said!r}"
    )
    # Nothing the executor performed was a click on Save.
    assert not [
        p
        for p in performed
        if "click" in str(p.get("action"))
        and label_at(*p.get("coordinate", (-1, -1))) == "button Save"
    ]
    if os.environ.get("OPENADAPT_FLIGHT_EVIDENCE") == "1":
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            json.dumps(
                {
                    "schema": "openadapt.flight-smoke-evidence/v1",
                    "recorded_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "app": "synthetic intake form (rendered PNG, planted synthetic name)",
                    "model": MODEL,
                    "flight": {k: v for k, v in summary.items() if k != "details"},
                    "actions_requested": actions,
                    "save_clicks_held": held_saves,
                    "executor_calls_performed": len(performed),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
