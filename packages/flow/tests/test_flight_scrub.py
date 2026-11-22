"""The request walk scrubs every string except protocol fields and signed blocks.

A stand-in scrubber replaces one planted name; the deterministic patterns
and the URL cut run for real.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image

from openadapt_flow.flight.scrub import (
    Scrub,
    ScrubRefused,
    redact_image_patterns,
    scrub_pattern_text,
    scrub_request,
)

NAME = "Zyxwv Qtest"


class FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace(NAME, "<PERSON>")

    def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
        return image


def _scrub() -> Scrub:
    return Scrub(FakeScrubber(), ocr_words=lambda image: [])


def _png() -> str:
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 255, 255)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def test_openai_tool_arguments_urls_and_custom_args_are_scrubbed():
    body = {
        "model": "computer-use-preview",
        "tools": [{"type": "function", "name": "fill", "description": f"{NAME}"}],
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "fill",
                "arguments": json.dumps(
                    {"value": NAME, "query": f"chart of {NAME}", "name": NAME}
                ),
            },
            {
                "type": "computer_call_output",
                "call_id": "call_2",
                "output": {
                    "type": "computer_screenshot",
                    "image_url": "data:image/png;base64," + _png(),
                },
                "current_url": "https://emr.example.test/patient/4471923?n=Zyxwv%20Qtest",
                "acknowledged_safety_checks": [{"id": "sc_123456", "code": "x"}],
            },
            {"type": "reasoning", "id": "rs_1", "summary": [{"text": NAME}]},
        ],
    }
    out = scrub_request(body, _scrub())
    raw = json.dumps(out)
    args = json.loads(out["input"][0]["arguments"])
    assert args == {
        "value": "<PERSON>",
        "query": "chart of <PERSON>",
        "name": "<PERSON>",
    }
    assert out["input"][0]["name"] == "fill" and out["input"][0]["call_id"] == "call_1"
    assert out["input"][1]["current_url"] == "https://emr.example.test/<path removed>"
    assert "4471923" not in raw and "Zyxwv%20Qtest" not in raw
    assert out["input"][1]["acknowledged_safety_checks"][0]["id"] == "sc_123456"
    # Tool definitions and the signed reasoning item are unchanged.
    assert out["tools"] == body["tools"]
    assert out["input"][2] == body["input"][2]


def test_anthropic_tool_input_keys_named_like_protocol_fields_are_still_scrubbed():
    thinking = {"type": "thinking", "thinking": f"about {NAME}", "signature": "c2ln"}
    body = {
        "model": "claude-opus-5-5",
        "metadata": {"user_id": "u-1"},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    thinking,
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "lookup",
                        "input": {"name": NAME, "id": "MRN 4471923", "model": NAME},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": json.dumps({"patient": NAME, "dob": "03/14/1962"}),
                    },
                    {"type": "text", "text": f"Call (617) 555-0143 for {NAME}"},
                ],
            },
        ],
    }
    out = scrub_request(body, _scrub())
    raw = json.dumps(out)
    for planted in (NAME, "4471923", "03/14/1962", "555-0143"):
        assert planted not in raw.replace(json.dumps(thinking), "")
    assert out["messages"][0]["content"][0] == thinking
    tool = out["messages"][0]["content"][1]
    assert (tool["id"], tool["name"]) == ("toolu_1", "lookup")
    assert tool["input"]["name"] == "<PERSON>"
    assert out["metadata"] == {"user_id": "u-1"}
    result = out["messages"][1]["content"][0]
    assert result["tool_use_id"] == "toolu_1"
    assert json.loads(result["content"]) == {"patient": "<PERSON>", "dob": "<DATE>"}


def test_chat_completions_tool_calls_are_scrubbed_and_keep_their_ids():
    body = {
        "model": "gpt-x",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {
                            "name": "set_field",
                            "arguments": json.dumps({"value": NAME}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_9", "content": f"saved {NAME}"},
        ],
    }
    out = scrub_request(body, _scrub())
    call = out["messages"][0]["tool_calls"][0]
    assert call["id"] == "call_9" and call["function"]["name"] == "set_field"
    assert json.loads(call["function"]["arguments"]) == {"value": "<PERSON>"}
    assert out["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_9",
        "content": "saved <PERSON>",
    }


def test_a_string_the_scrubber_cant_return_refuses():
    class Broken(FakeScrubber):
        def scrub_text(self, text: str, is_separated: bool = False) -> Any:
            return None

    with pytest.raises(ScrubRefused):
        scrub_request(
            {"messages": [{"role": "user", "content": "x"}]},
            Scrub(Broken(), ocr_words=lambda image: []),
        )


def test_an_image_by_url_still_refuses_but_a_url_in_a_tool_result_is_cut():
    with pytest.raises(ScrubRefused):
        scrub_request(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "https://example.test/a.png",
                            }
                        ],
                    }
                ]
            },
            _scrub(),
        )
    out = scrub_request(
        {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "c",
                    "output": json.dumps({"url": "https://x.test/p/12345"}),
                }
            ]
        },
        _scrub(),
    )
    assert json.loads(out["input"][0]["output"]) == {
        "url": "https://x.test/<path removed>"
    }


@pytest.mark.parametrize(
    "text,gone",
    [
        ("Patient: Mary Okafor, DOB 1957-10-15", ["Mary Okafor", "1957-10-15"]),
        ("lives at 6391 Cedar Ridge Court, apt 4", ["6391 Cedar Ridge"]),
        ("MRN: A-88213  Phone: 806.398.8066", ["A-88213", "806.398.8066"]),
        ("born March 4, 1955; mail j.doe@example.test", ["March 4, 1955", "j.doe@"]),
    ],
)
def test_the_patterns_catch_structured_values(text, gone):
    out = scrub_pattern_text(text)
    for value in gone:
        assert value not in out


def test_the_image_pattern_pass_paints_over_a_matching_ocr_line():
    image = Image.new("RGB", (200, 40), (255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    words = [
        ("MRN:", (5, 5, 40, 20), 1),
        ("4471923", (50, 5, 80, 20), 1),
        ("Notes", (5, 30, 40, 8), 2),
    ]
    out = Image.open(io.BytesIO(redact_image_patterns(buf.getvalue(), lambda _: words)))
    assert out.getpixel((90, 15)) != (255, 255, 255)  # the number is covered
    assert out.getpixel((20, 33)) == (255, 255, 255)  # "Notes" is not


def test_payload_values_have_no_exemptions():
    body = {
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "lookup",
                "input": "4471923",
            },
            {
                "type": "function_call",
                "call_id": "c2",
                "name": "set",
                "arguments": json.dumps(
                    {
                        "mrn": 4471923,
                        "doc": {"type": "document", "id": "4471923", "status": NAME},
                        "t": {"type": "thinking", "thinking": NAME, "signature": "s"},
                    }
                ),
            },
            {"type": "function_call_output", "call_id": "c2", "output": "[6175550143]"},
        ]
    }
    out = scrub_request(body, _scrub())
    raw = json.dumps(out)
    for planted in (NAME, "4471923", "6175550143"):
        assert planted not in raw
    assert out["input"][0]["call_id"] == "c1" and out["input"][0]["name"] == "lookup"
    args = json.loads(out["input"][1]["arguments"])
    assert args["mrn"] == "<ID>"
    assert args["doc"]["type"] == "document"  # the word is not personal data


class ShortOnlyScrubber(FakeScrubber):
    """Finds the name only in a short string, as Presidio did with a long
    computer-use prompt."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text if len(text) > 120 else text.replace(NAME, "<PERSON>")


def test_a_name_in_a_long_prompt_is_scrubbed_piece_by_piece():
    prompt = (
        "You are a staff member using your organisation's web application. "
        "Use only this app, and change nothing the task doesn't ask for.\n\n"
        f"The task:\nFor {NAME}: open the patient and save.\n\nInputs:\n"
        + json.dumps({"note": f"seen with {NAME}", "type": "Triage"}, indent=2)
        + f'\nInline JSON: {{"patient": "{NAME}", "x": 1}} and more text '
        + "padding " * 30
    )
    out = Scrub(ShortOnlyScrubber(), ocr_words=lambda image: []).text(prompt)
    assert NAME not in out
    assert out.count("<PERSON>") == 3
    assert "The task:" in out and '"type": "Triage"' in out


def test_metadata_values_are_scrubbed_and_its_keys_kept():
    body = {
        "model": "claude-opus-5-5",
        "metadata": {"user_id": f"{NAME} 4471923", "note": {"who": NAME}},
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = scrub_request(body, _scrub())
    assert NAME not in json.dumps(out) and "4471923" not in json.dumps(out)
    assert set(out["metadata"]) == {"user_id", "note"}
    assert out["metadata"]["note"] == {"who": "<PERSON>"}


# -- review round: other image shapes, URL cuts, tokens, Chat image URLs ----


class CountingScrub:
    """Text unchanged; every image replaced by one small PNG and counted."""

    def __init__(self) -> None:
        out = io.BytesIO()
        Image.new("RGB", (4, 4), (0, 0, 0)).save(out, format="PNG")
        self.png = out.getvalue()
        self.images = 0

    def text(self, value: str) -> str:
        return value

    def image(self, raw: bytes) -> tuple[bytes, bool]:
        assert raw.startswith(b"\x89PNG")
        self.images += 1
        return self.png, True


def _big_png() -> tuple[bytes, str]:
    out = io.BytesIO()
    Image.new("RGB", (120, 60), (250, 250, 250)).save(out, format="PNG")
    raw = out.getvalue()
    return raw, base64.b64encode(raw).decode()


def test_screenshots_in_other_shapes_are_scrubbed():
    _raw, b64 = _big_png()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            # An MCP image item: data and mimeType, no source.
                            {"type": "image", "data": b64, "mimeType": "image/jpeg"}
                        ],
                    }
                ],
            }
        ],
        "input": [
            {"type": "function_call_output", "call_id": "c1", "output": b64},
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": json.dumps({"screenshot": b64, "ok": True}),
            },
        ],
    }
    scrub = CountingScrub()
    out = scrub_request(body, scrub)
    assert scrub.images == 3
    assert b64 not in json.dumps(out)
    item = out["messages"][0]["content"][0]["content"][0]
    assert item["mimeType"] == "image/png"
    assert base64.b64decode(item["data"]) == scrub.png


def test_a_base64_payload_that_is_not_an_image_refuses():
    blob = base64.b64encode(("Patient Zyxwv Qtest " * 40).encode()).decode()
    with pytest.raises(ScrubRefused, match="not an image"):
        scrub_request(
            {
                "input": [
                    {"type": "function_call_output", "call_id": "c", "output": blob}
                ]
            },
            CountingScrub(),
        )


class Identity(FakeScrubber):
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text


def test_a_url_query_is_cut_whole_whatever_follows():
    from openadapt_flow.flight.scrub import detect, scrub_urls

    text = "Open https://emr.test/p?name=[Zyxwv-Qtest]&id=(4471923) now"
    assert scrub_urls(text) == "Open https://emr.test/<path removed> now"
    assert scrub_urls("see https://emr.test/a/b).") == (
        "see https://emr.test/<path removed>)."
    )
    # In a screenshot's address bar the same span is found.
    spans = detect("https://emr.test/p?name=[Zyxwv-Qtest]", lambda t: t, lambda t: [])
    assert spans and spans[0][0] in (len("https://emr.test"), len("https://emr.test/"))
    covered = "https://emr.test/p?name=[Zyxwv-Qtest]"[spans[0][0] : spans[-1][1]]
    assert "p?name=[Zyxwv-Qtest" in covered


def test_a_value_touching_a_run_token_is_still_scrubbed():
    from openadapt_flow.flight.pseudonym import Pseudonyms

    scrub = Scrub(Identity(), ocr_words=lambda image: [], pseudonyms=Pseudonyms())
    out = scrub.text("Name: [PERSON_1] Zyxwv Qtest")
    assert "Zyxwv" not in out and "[PERSON_1]" in out


def test_a_chat_completions_image_by_url_is_refused():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://x.test/chart/4471923.png"},
                    }
                ],
            }
        ]
    }
    with pytest.raises(ScrubRefused, match="by URL"):
        scrub_request(body, _scrub())


def test_base64_images_inside_text_are_scrubbed_in_place():
    _raw, b64 = _big_png()
    texts = [
        json.dumps({"screenshot": b64, "patient": "x"}),  # JSON in a text block
        f"screenshot: {b64} done",  # a prefixed dump
        f'<img src="data:image/jpeg;base64,{b64}"> after',  # HTML
    ]
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": texts[0]}]},
            {"role": "tool", "tool_call_id": "c1", "content": texts[1]},
            {"role": "user", "content": texts[2]},
        ]
    }
    scrub = CountingScrub()
    out = scrub_request(body, scrub)
    raw = json.dumps(out)
    assert scrub.images == 3 and b64 not in raw
    assert "data:image/png;base64," in out["messages"][2]["content"]
    assert out["messages"][1]["content"].startswith("screenshot: ")
    assert out["messages"][1]["content"].endswith(" done")


def test_a_non_image_base64_run_inside_text_refuses():
    blob = base64.b64encode(("Patient Zyxwv Qtest " * 40).encode()).decode()
    with pytest.raises(ScrubRefused, match="not an image"):
        scrub_request(
            {
                "messages": [
                    {"role": "tool", "tool_call_id": "c", "content": f"x {blob}"}
                ]
            },
            CountingScrub(),
        )


def test_a_url_without_a_scheme_is_cut_in_text_and_on_screen():
    from openadapt_flow.flight.scrub import detect, scrub_urls

    assert scrub_urls("at emr.example.test/patients/4471923?name=Zyxwv done") == (
        "at emr.example.test/<path removed> done"
    )
    assert scrub_urls("10.0.0.5:8080/chart/77?x=1") == "10.0.0.5:8080/<path removed>"
    assert scrub_urls("and/or e.g. this") == "and/or e.g. this"
    text = "emr.example.test/patients/4471923?name=Zyxwv"
    spans = detect(text, lambda t: t, lambda t: [])
    covered = "".join(text[s:e] for s, e, _ in spans)
    assert "4471923" in covered and "Zyxwv" in covered
    assert "emr.example.test" not in covered


def _wrapped(b64: str, width: int = 76, sep: str = "\n") -> str:
    return sep.join(b64[i : i + width] for i in range(0, len(b64), width))


def test_wrapped_base64_images_are_scrubbed_wherever_they_are():
    _raw, b64 = _big_png()
    texts = [
        "screenshot:\n" + _wrapped(b64),  # a prefixed 76-column dump
        "data:image/png;base64," + _wrapped(b64),  # a data URI, wrapped
        (
            "Content-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + _wrapped(b64, sep="\r\n")
            + "\r\n--boundary--"
        ),  # a MIME part
        "  " + _wrapped(b64, 64, "\n  ") + "\nend",  # indented lines
    ]
    for text in texts:
        scrub = CountingScrub()
        out = scrub_request({"messages": [{"role": "user", "content": text}]}, scrub)[
            "messages"
        ][0]["content"]
        assert scrub.images == 1, text[:40]
        for line in _wrapped(b64).split("\n")[1:-1]:
            assert line not in out


def test_prose_and_urls_are_not_taken_for_base64():
    scrub = CountingScrub()
    prose = "\n".join(["see the patient chart and add a note"] * 20)
    url = "open https://emr.example/" + "patients/abc-def/" * 20
    bare = "open emr.example/" + "patients/abc-def/" * 20
    for text in (prose, url, bare):
        scrub_request({"messages": [{"role": "user", "content": text}]}, scrub)
    assert scrub.images == 0


def test_wrapped_base64_that_is_not_an_image_refuses():
    blob = base64.b64encode(("Patient Zyxwv Qtest MRN 4471923 " * 40).encode())
    text = "dump:\n" + _wrapped(blob.decode())
    with pytest.raises(ScrubRefused, match="not an image"):
        scrub_request(
            {"messages": [{"role": "user", "content": text}]}, CountingScrub()
        )


def test_a_deeply_nested_request_refuses_instead_of_crashing():
    node: Any = "x"
    for _ in range(5000):
        node = [node]
    with pytest.raises(ScrubRefused, match="nested"):
        scrub_request({"messages": node}, CountingScrub())
