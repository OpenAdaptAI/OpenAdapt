"""Run pseudonyms: one token per value in text and images, reversed only
inside tool-call inputs on the way back to the agent.

The real proxy runs in front of a loopback stub provider that records the
bytes it gets. The response-path tests use a stand-in scrubber that finds
one planted name; the image test uses the real scrubber and tesseract (it
fails without them, except under CI, where it skips).
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.flight.proxy import FlightProxy
from openadapt_flow.flight.pseudonym import (
    Pseudonyms,
    StreamDetokenizer,
    detokenize_response,
)
from openadapt_flow.flight.timeline import Timeline
from tests.fixtures.flight.fake_provider import FakeProvider

NAME = "Jane Sample"


class FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        out = re.sub(r"(?i)jane\s+sample", "<PERSON>", text)
        return out.replace("Referral Tasks", "<ORGANIZATION>")

    def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
        return image


@pytest.fixture
def provider():
    fake = FakeProvider(model="claude-opus-5-5").start()
    yield fake
    fake.stop()


def _proxy(tmp_path: Path, provider: FakeProvider, scrubber: Any = None, **kw):
    timeline = Timeline(tmp_path / "run", task_id="t", executor_label="test")
    return FlightProxy(
        timeline,
        upstreams={"anthropic": provider.url, "openai": provider.url + "/v1"},
        scrubber=scrubber or FakeScrubber(),
        **kw,
    ).start()


def _post(url: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _message(text: str, **extra: Any) -> dict:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        **extra,
    }


def _type_turn(text: str) -> list[dict]:
    return [
        {"type": "text", "text": f"I will type {text}."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "computer",
            "input": {"action": "type", "text": text},
        },
    ]


def test_a_token_in_a_tool_call_reaches_the_agent_as_the_real_value(tmp_path, provider):
    provider.anthropic_turns.append(_type_turn("[PERSON_1]"))
    proxy = _proxy(tmp_path, provider)
    try:
        status, raw = _post(
            proxy.base_url("anthropic") + "/v1/messages",
            _message(f"Open the referral for {NAME} and type her name."),
        )
    finally:
        proxy.stop()
    assert status == 200
    forwarded = json.loads(provider.requests[0]["body"])
    assert forwarded["messages"][0]["content"][0]["text"] == (
        "Open the referral for [PERSON_1] and type her name."
    )
    assert NAME.encode() not in provider.received_bytes()
    reply = json.loads(raw)
    assert reply["content"][1]["input"] == {"action": "type", "text": NAME}
    # Text the model writes for itself keeps the token.
    assert reply["content"][0]["text"] == "I will type [PERSON_1]."
    # The map is local, owner-only, and holds the real value.
    path = tmp_path / "run" / "pseudonyms.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["tokens"] == {"[PERSON_1]": NAME}


def test_a_streamed_tool_call_is_held_and_sent_de_tokenized(tmp_path, provider):
    anthropic = pytest.importorskip("anthropic")
    provider.anthropic_turns.append(_type_turn("[PERSON_1]"))
    proxy = _proxy(tmp_path, provider)
    try:
        # The first call puts the name in the map; the second streams.
        _post(proxy.base_url("anthropic") + "/v1/messages", _message(f"For {NAME}."))
        provider.anthropic_turns.append(_type_turn("[PERSON_1]"))
        client = anthropic.Anthropic(
            api_key="sk-test", base_url=proxy.base_url("anthropic"), max_retries=0
        )
        with client.messages.stream(**_message(f"Again for {NAME}.")) as stream:
            final = stream.get_final_message()
    finally:
        proxy.stop()
    tool = next(b for b in final.content if b.type == "tool_use")
    assert tool.input == {"action": "type", "text": NAME}
    assert NAME.encode() not in provider.received_bytes()


def test_the_agents_history_with_the_real_value_is_tokenized_again(tmp_path, provider):
    proxy = _proxy(tmp_path, provider)
    body = _message(f"For {NAME}.")
    body["messages"] += [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "computer",
                    # The agent typed the real value; a lower-case copy is not
                    # something the stand-in scrubber would find by itself.
                    "input": {"action": "type", "text": "jane   SAMPLE"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "typed"}
            ],
        },
    ]
    try:
        assert _post(proxy.base_url("anthropic") + "/v1/messages", body)[0] == 200
    finally:
        proxy.stop()
    forwarded = json.loads(provider.requests[0]["body"])
    assert forwarded["messages"][1]["content"][0]["input"]["text"] == "[PERSON_1]"
    assert b"SAMPLE" not in provider.received_bytes()


def test_a_header_word_survives_and_an_organisation_hit_is_not_redacted(
    tmp_path, provider
):
    proxy = _proxy(tmp_path, provider)
    try:
        _post(
            proxy.base_url("anthropic") + "/v1/messages",
            _message(f"Referral Tasks: open {NAME}, MRN 12345."),
        )
    finally:
        proxy.stop()
    text = json.loads(provider.requests[0]["body"])["messages"][0]["content"][0]["text"]
    assert text == "Referral Tasks: open [PERSON_1], MRN [ID_1]."


def test_openai_stream_argument_deltas_are_held_until_done():
    p = Pseudonyms()
    p.token("PERSON", NAME)
    rewriter = StreamDetokenizer(p)
    events = [
        {"type": "response.output_text.delta", "item_id": "m", "delta": "[PERSON_1]"},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc",
            "delta": '{"value": "[PERS',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc",
            "delta": 'ON_1]"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc",
            "arguments": '{"value": "[PERSON_1]"}',
        },
        {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "computer_call",
                        "call_id": "c",
                        "action": {"type": "type", "text": "[PERSON_1]"},
                    }
                ]
            },
        },
    ]
    raw = b"".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )
    out = b""
    for i in range(0, len(raw), 7):  # split anywhere
        out += rewriter.feed(raw[i : i + 7])
    out += rewriter.close()
    parsed = [
        json.loads(line[6:])
        for line in out.decode().splitlines()
        if line.startswith("data: ")
    ]
    assert parsed[0]["delta"] == "[PERSON_1]"  # the model's own text
    deltas = [e for e in parsed if e["type"].endswith("arguments.delta")]
    assert len(deltas) == 1 and json.loads(deltas[0]["delta"]) == {"value": NAME}
    done = next(e for e in parsed if e["type"].endswith("arguments.done"))
    assert json.loads(done["arguments"]) == {"value": NAME}
    assert parsed[-1]["response"]["output"][0]["action"]["text"] == NAME


def test_a_stream_that_cant_be_parsed_ends_with_an_error():
    p = Pseudonyms()
    p.token("PERSON", NAME)
    rewriter = StreamDetokenizer(p)
    out = rewriter.feed(b"event: x\ndata: {not json\n\n")
    assert b'"type": "error"' in out and rewriter.failed
    assert rewriter.feed(b'data: {"type": "ping"}\n\n') == b""


def test_a_streamed_chat_completions_request_is_refused_once_tokens_exist(
    tmp_path, provider
):
    proxy = _proxy(tmp_path, provider)
    try:
        _post(proxy.base_url("anthropic") + "/v1/messages", _message(f"For {NAME}."))
        status, raw = _post(
            proxy.base_url("openai") + "/chat/completions",
            {
                "model": "gpt-x",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    finally:
        proxy.stop()
    assert status == 502 and b"stream=false" in raw
    assert len(provider.requests) == 1


def test_detokenize_response_leaves_other_text_alone():
    p = Pseudonyms()
    p.token("ID", "12345")
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "see [ID_1]",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "f",
                                    "arguments": '{"mrn": "[ID_1]"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()
    out = json.loads(detokenize_response(body, p))
    message = out["choices"][0]["message"]
    assert message["content"] == "see [ID_1]"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "mrn": "12345"
    }


# -- with the real scrubber ---------------------------------------------------


def _real_scrubber():
    from openadapt_flow.flight.scrub import ScrubberUnavailable, require_scrubber

    try:
        return require_scrubber()
    except ScrubberUnavailable as exc:
        if os.environ.get("CI"):
            pytest.skip(f"CI without the scrubber's dependencies: {exc}")
        raise


def _screen() -> bytes:
    image = Image.new("RGB", (900, 260), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    draw.text((24, 20), "Referral Tasks", fill=(0, 0, 0), font=font)
    draw.text((24, 90), "Patient", fill=(0, 0, 0), font=font)
    draw.text((300, 90), "Reason", fill=(0, 0, 0), font=font)
    draw.text((24, 160), NAME, fill=(0, 0, 0), font=font)
    draw.text((300, 160), "Chest pain follow-up", fill=(0, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_the_same_name_gets_the_same_token_in_text_and_on_screen(tmp_path, provider):
    # The scrubber check first: under CI without Presidio or tesseract it
    # skips with the reason; anywhere else their absence fails.
    scrubber = _real_scrubber()
    import pytesseract

    png = _screen()
    assert NAME in pytesseract.image_to_string(Image.open(io.BytesIO(png)))
    proxy = _proxy(tmp_path, provider, scrubber=scrubber)
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Open the referral for {NAME}."},
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
        assert _post(proxy.base_url("anthropic") + "/v1/messages", body)[0] == 200
    finally:
        proxy.stop()
    assert NAME.encode() not in provider.received_bytes()
    content = json.loads(provider.requests[0]["body"])["messages"][0]["content"]
    assert content[0]["text"] == "Open the referral for [PERSON_1]."
    seen = pytesseract.image_to_string(
        Image.open(io.BytesIO(base64.b64decode(content[1]["source"]["data"])))
    )
    assert "Sample" not in seen and "Jane" not in seen
    assert re.search(r"PERSON[_ ]?1", seen), seen
    # Headers and the referral reason are not identifiers and stay readable.
    for word in ("Referral", "Tasks", "Patient", "Reason", "Chest"):
        assert word in seen, (word, seen)
