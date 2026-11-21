"""The flight recorder's model-API proxy: scrub, meter, price, forward.

Every test runs a loopback fake provider; the assertions scan the exact
bytes the provider received.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.compile_agent.agents import PRICES_PER_MTOK
from openadapt_flow.flight.proxy import FlightProxy, check_upstream, price_usd
from openadapt_flow.flight.timeline import Timeline, validate_flight
from tests.fixtures.flight.fake_provider import (
    ANTHROPIC_USAGE,
    OPENAI_USAGE,
    FakeProvider,
    anthropic_click,
    openai_click,
)

PHI = "Zyxwv Qtest"
MRN = "MRN 99-4417"
KEY = "sk-ant-test-not-a-real-key-123"


class FakeScrubber:
    """Replaces the planted values; paints images black (same size)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.images = 0

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        if self.fail:
            raise RuntimeError("scrubber broke")
        return text.replace(PHI, "<PERSON>").replace(MRN, "<ID>")

    def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
        if self.fail:
            raise RuntimeError("scrubber broke")
        self.images += 1
        return Image.new("RGB", image.size, (0, 0, 0))


def _png(
    text: str = PHI, size: tuple[int, int] = (320, 120), fmt: str = "PNG"
) -> bytes:
    image = Image.new("RGB", size, (255, 255, 255))
    ImageDraw.Draw(image).text((10, 40), text, fill=(0, 0, 0))
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


@pytest.fixture
def provider():
    fake = FakeProvider().start()
    yield fake
    fake.stop()


def _proxy(tmp_path: Path, provider: FakeProvider, **kw: Any) -> FlightProxy:
    scrubber = kw.pop("scrubber", FakeScrubber())
    timeline = Timeline(
        tmp_path / "run",
        task_id="t-1",
        executor_label="test-agent",
        scrub_image=lambda png: png,
        scrub_text=lambda text: scrubber.scrub_text(text),
    )
    return FlightProxy(
        timeline,
        upstreams={"anthropic": provider.url, "openai": provider.url + "/v1"},
        scrubber=scrubber if kw.get("scrub", True) else None,
        **kw,
    ).start()


def _post(
    url: str, body: Any, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _anthropic_body(
    image: bytes, *, stream: bool = False, media: str = "image/png"
) -> dict:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 256,
        "stream": stream,
        "system": f"You help with the chart of {PHI}.",
        "tools": [
            {
                "type": "computer_20250124",
                "name": "computer",
                "display_width_px": 320,
                "display_height_px": 120,
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Open the chart for {PHI}, {MRN}."},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media,
                            "data": base64.b64encode(image).decode(),
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "plan the clicks",
                        "signature": "sig-abc==",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_prev",
                        "name": "computer",
                        "input": {"action": "type", "text": f"{PHI}"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media,
                                    "data": base64.b64encode(image).decode(),
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    }


def test_anthropic_sdk_call_is_scrubbed_metered_and_priced(tmp_path, provider):
    anthropic = pytest.importorskip("anthropic")
    provider.anthropic_turns.append([anthropic_click(40, 60)])
    proxy = _proxy(tmp_path, provider)
    try:
        client = anthropic.Anthropic(
            api_key=KEY, base_url=proxy.base_url("anthropic"), max_retries=0
        )
        body = _anthropic_body(_png())
        message = client.messages.create(
            **{k: v for k, v in body.items() if k != "stream"}
        )
    finally:
        proxy.stop()
    assert message.content[0].type == "tool_use"
    assert len(provider.requests) == 1
    received = provider.received_bytes()
    assert PHI.encode() not in received and MRN.encode() not in received
    assert provider.requests[0]["headers"]["x-api-key"] == KEY
    forwarded = json.loads(provider.requests[0]["body"])
    # Tool definitions, ids and the thinking block's signature are untouched.
    assert forwarded["tools"] == body["tools"]
    assert forwarded["messages"][1]["content"][0] == body["messages"][1]["content"][0]
    assert forwarded["messages"][2]["content"][0]["tool_use_id"] == "toolu_prev"
    source = forwarded["messages"][0]["content"][1]["source"]
    image = Image.open(io.BytesIO(base64.b64decode(source["data"])))
    assert source["media_type"] == "image/png"
    assert image.size == (320, 120)
    assert image.getextrema() == ((0, 0), (0, 0), (0, 0))  # the scrubbed frame

    summary = proxy.timeline.finish()
    assert validate_flight(summary) == []
    model = summary["model"]
    assert model["calls"] == 1
    assert model["input_tokens"] == ANTHROPIC_USAGE["input_tokens"]
    assert model["output_tokens"] == ANTHROPIC_USAGE["output_tokens"]
    row = PRICES_PER_MTOK["claude-opus-5-5"]
    expected = (
        1200 * row["input"]
        + 80 * row["output"]
        + 300 * row["cache_write"]
        + 500 * row["cache_read"]
    ) / 1_000_000
    assert model["cost_usd"] == pytest.approx(expected)
    assert model["cost_source"] == "price_table"
    # The key is never written anywhere in the run directory.
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()
    events = [
        json.loads(line)
        for line in (tmp_path / "run/timeline.jsonl").read_text().splitlines()
    ]
    actions = [e for e in events if e["kind"] == "model_action"]
    assert actions and actions[0]["action"]["coordinate"] == [40, 60]
    assert actions[0]["ops"][0]["kind"] == "click"
    # The same screenshot sent twice is scrubbed once and written once.
    frames = [e for e in events if e["kind"] == "frame"]
    assert len(frames) == 1


def test_anthropic_stream_passes_through_and_usage_comes_from_the_final_events(
    tmp_path, provider
):
    anthropic = pytest.importorskip("anthropic")
    provider.anthropic_turns.append(
        [{"type": "text", "text": "ok"}, anthropic_click(7, 9, "toolu_s")]
    )
    proxy = _proxy(tmp_path, provider)
    try:
        client = anthropic.Anthropic(
            api_key=KEY, base_url=proxy.base_url("anthropic"), max_retries=0
        )
        body = _anthropic_body(_png())
        body.pop("stream")
        with client.messages.stream(**body) as stream:
            final = stream.get_final_message()
    finally:
        proxy.stop()
    assert final.content[1].input == {"action": "left_click", "coordinate": [7, 9]}
    summary = proxy.timeline.finish()
    assert summary["model"]["input_tokens"] == ANTHROPIC_USAGE["input_tokens"]
    assert summary["model"]["output_tokens"] == ANTHROPIC_USAGE["output_tokens"]
    assert summary["details"]["cache_read_input_tokens"] == 500
    assert summary["actions_count"] == 1
    assert PHI.encode() not in provider.received_bytes()


def test_a_jpeg_screenshot_is_sent_as_a_scrubbed_png_of_the_same_size(
    tmp_path, provider
):
    proxy = _proxy(tmp_path, provider)
    try:
        status, _ = _post(
            proxy.base_url("anthropic") + "/v1/messages",
            _anthropic_body(_png(size=(200, 90), fmt="JPEG"), media="image/jpeg"),
            {"x-api-key": KEY},
        )
    finally:
        proxy.stop()
    assert status == 200
    source = json.loads(provider.requests[0]["body"])["messages"][0]["content"][1][
        "source"
    ]
    assert source["media_type"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(source["data"]))).size == (200, 90)


@pytest.mark.parametrize(
    "case", ["scrubber_raises", "bad_image", "image_by_url", "not_json"]
)
def test_a_request_that_cant_be_scrubbed_gets_502_and_nothing_is_sent(
    tmp_path, provider, case
):
    scrubber = FakeScrubber(fail=case == "scrubber_raises")
    proxy = _proxy(tmp_path, provider, scrubber=scrubber)
    body: Any = _anthropic_body(_png())
    if case == "bad_image":
        body["messages"][0]["content"][1]["source"]["data"] = base64.b64encode(
            b"not an image"
        ).decode()
    elif case == "image_by_url":
        body["messages"][0]["content"][1]["source"] = {
            "type": "url",
            "url": "https://example.test/chart.png",
        }
    elif case == "not_json":
        body = b"\x00\x01 not json"
    try:
        status, raw = _post(
            proxy.base_url("anthropic") + "/v1/messages", body, {"x-api-key": KEY}
        )
    finally:
        proxy.stop()
    assert status == 502
    assert b"not sent" in raw
    assert provider.requests == []
    summary = proxy.timeline.finish()
    assert summary["details"]["refused_requests"][0]["status"] == 502


def test_openai_responses_computer_call_is_logged_and_unpriced_cost_is_unknown(
    tmp_path, provider
):
    provider.openai_turns.append([openai_click(11, 22)])
    provider.openai_turns.append([openai_click(33, 44, "call_2")])
    proxy = _proxy(tmp_path, provider)
    image = "data:image/png;base64," + base64.b64encode(_png()).decode()
    body = {
        "model": "computer-use-preview",
        "instructions": f"Chart of {PHI}",
        "tools": [
            {
                "type": "computer_use_preview",
                "display_width": 320,
                "display_height": 120,
            }
        ],
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": f"Find {MRN}"}],
            },
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "opaque-blob",
                "summary": [],
            },
            {
                "type": "computer_call_output",
                "call_id": "call_0",
                "output": {"type": "input_image", "image_url": image},
            },
        ],
    }
    try:
        status, raw = _post(
            proxy.base_url("openai") + "/responses",
            body,
            {"Authorization": "Bearer " + KEY},
        )
        assert status == 200
        streamed = dict(body, stream=True)
        status, raw = _post(
            proxy.base_url("openai") + "/responses",
            streamed,
            {"Authorization": "Bearer " + KEY},
        )
        assert status == 200 and b"response.completed" in raw
    finally:
        proxy.stop()
    assert all(r["path"] == "/v1/responses" for r in provider.requests)
    received = provider.received_bytes()
    assert PHI.encode() not in received and MRN.encode() not in received
    forwarded = json.loads(provider.requests[0]["body"])
    assert forwarded["input"][1] == body["input"][1]  # reasoning item untouched
    url = forwarded["input"][2]["output"]["image_url"]
    assert url.startswith("data:image/png;base64,")
    summary = proxy.timeline.finish()
    assert summary["model"]["calls"] == 2
    # OpenAI's input_tokens include the cached ones; they are counted once.
    cached = OPENAI_USAGE["input_tokens_details"]["cached_tokens"]
    assert summary["model"]["input_tokens"] == 2 * (
        OPENAI_USAGE["input_tokens"] - cached
    )
    assert summary["model"]["cost_usd"] is None
    assert summary["model"]["cost_source"] == "unknown"
    assert summary["actions_count"] == 2
    assert validate_flight(summary) == []


def test_the_spend_cap_refuses_with_429_once_reached(tmp_path, provider):
    proxy = _proxy(tmp_path, provider, max_cost_usd=0.000001)
    body = _anthropic_body(_png())
    try:
        first, _ = _post(
            proxy.base_url("anthropic") + "/v1/messages", body, {"x-api-key": KEY}
        )
        second, raw = _post(
            proxy.base_url("anthropic") + "/v1/messages", body, {"x-api-key": KEY}
        )
    finally:
        proxy.stop()
    assert first == 200 and second == 429
    assert b"spend cap" in raw
    assert len(provider.requests) == 1


def test_a_spend_cap_on_an_unpriced_model_refuses_before_sending(tmp_path, provider):
    proxy = _proxy(tmp_path, provider, max_cost_usd=5.0)
    try:
        status, raw = _post(
            proxy.base_url("openai") + "/responses",
            {"model": "computer-use-preview", "input": "hi"},
        )
    finally:
        proxy.stop()
    assert status == 429 and b"can't be enforced" in raw
    assert provider.requests == []


def test_a_wrong_token_is_404_and_nothing_is_sent(tmp_path, provider):
    proxy = _proxy(tmp_path, provider)
    try:
        url = proxy.base_url("anthropic").replace(proxy.token, "wrong-token")
        status, _ = _post(url + "/v1/messages", _anthropic_body(_png()))
    finally:
        proxy.stop()
    assert status == 404 and provider.requests == []


def test_no_scrub_forwards_the_body_unchanged(tmp_path, provider):
    proxy = _proxy(tmp_path, provider, scrub=False)
    body = _anthropic_body(_png())
    try:
        status, _ = _post(proxy.base_url("anthropic") + "/v1/messages", body)
    finally:
        proxy.stop()
    assert status == 200
    assert PHI.encode() in provider.received_bytes()


def test_upstreams_must_be_https_unless_loopback():
    assert check_upstream("https://api.anthropic.com/") == "https://api.anthropic.com"
    assert check_upstream("http://127.0.0.1:9/v1") == "http://127.0.0.1:9/v1"
    with pytest.raises(ValueError):
        check_upstream("http://api.example.test")


def test_prices_come_from_the_table_and_dated_ids_resolve():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert (
        price_usd("claude-opus-5-5", usage)
        == PRICES_PER_MTOK["claude-opus-5-5"]["input"]
    )
    assert price_usd("claude-opus-5-5-20261101", usage) == price_usd(
        "claude-opus-5-5", usage
    )
    assert price_usd("gpt-unknown", usage) is None


def test_openai_cached_tokens_are_priced_once():
    from openadapt_flow.flight.proxy import _usage

    usage = _usage(
        {
            "input_tokens": 900,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 100},
        }
    )
    assert usage["input_tokens"] == 800
    assert usage["cache_read_input_tokens"] == 100
    row = PRICES_PER_MTOK["claude-opus-5-5"]
    expected = (800 * row["input"] + 40 * row["output"] + 100 * row["cache_read"]) / 1e6
    assert price_usd("claude-opus-5-5", usage) == pytest.approx(expected)
    # Anthropic's input_tokens already exclude the cache: left as they are.
    anthropic = _usage({"input_tokens": 900, "cache_read_input_tokens": 100})
    assert anthropic["input_tokens"] == 900


def test_no_scrub_is_refused_without_the_acceptance_flag(tmp_path, provider, capsys):
    import argparse

    from openadapt_flow.flight.cli import add_parser

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    args = parser.parse_args(
        ["flight", "--out", str(tmp_path / "run"), "--no-scrub", "--", "true"]
    )
    assert args.func(args) == 2
    assert "--i-accept-unscrubbed" in capsys.readouterr().err
    assert provider.requests == []
    assert not (tmp_path / "run" / "flight.json").exists()


def _real_scrubber():
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("presidio_image_redactor")
    pytesseract = pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    from openadapt_flow.privacy import _build_provider

    scrubber = _build_provider()
    if scrubber is None:
        pytest.skip("openadapt-privacy is not installed")
    try:
        scrubber.scrub_text("probe John Smith")
    except Exception as exc:  # pragma: no cover - missing spaCy model
        pytest.skip(f"the Presidio model is unavailable: {exc}")
    return scrubber, pytesseract


def test_the_real_presidio_scrubber_removes_a_name_from_text_and_from_the_screenshot(
    tmp_path, provider
):
    scrubber, pytesseract = _real_scrubber()
    from PIL import ImageFont

    name = "John Smith"
    image = Image.new("RGB", (900, 200), (255, 255, 255))
    try:
        font = ImageFont.truetype("Arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    ImageDraw.Draw(image).text((20, 60), f"Patient: {name}", fill=(0, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    png = out.getvalue()
    before = pytesseract.image_to_string(image)
    if "Smith" not in before:
        pytest.skip("tesseract can't read the planted name on this machine")
    timeline = Timeline(tmp_path / "run", task_id="t", executor_label="x")
    proxy = FlightProxy(
        timeline,
        upstreams={"anthropic": provider.url},
        scrubber=scrubber,
    ).start()
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Open the chart for {name}."},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(png).decode(),
                        },
                    },
                ],
            }
        ],
    }
    try:
        status, _ = _post(proxy.base_url("anthropic") + "/v1/messages", body)
    finally:
        proxy.stop()
    assert status == 200
    forwarded = json.loads(provider.requests[0]["body"])
    assert name not in forwarded["messages"][0]["content"][0]["text"]
    data = forwarded["messages"][0]["content"][1]["source"]["data"]
    after = Image.open(io.BytesIO(base64.b64decode(data)))
    assert after.size == image.size
    assert "Smith" not in pytesseract.image_to_string(after)
