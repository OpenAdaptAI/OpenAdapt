"""Synthetic PHI never reaches the model payload (the real scrubber, real proxy).

The real FlightProxy runs with the real openadapt-privacy scrubber (Presidio,
spaCy ``en_core_web_sm``, tesseract) in front of a loopback stub provider
that records the exact bytes it received. Anthropic- and OpenAI-shaped
requests carry one synthetic patient's name, date of birth, record number,
phone and address in: a rendered screenshot, message text, a tool result,
tool-call arguments JSON and a ``computer_call_output`` ``current_url``.

None of those values (nor their URL-encoded or digits-only forms) may appear
in the forwarded bytes, and tesseract must not read them in the forwarded
screenshots, though it reads them in the originals.

This checks that every position in the payload is scrubbed. It is not a
recall measurement: one fixed patient is used. Recall on a 50-record corpus
is in ``benchmark/flight/scrub_recall``.

Needs openadapt-privacy with Presidio, ``en_core_web_sm`` and tesseract.
Without them the test fails, except under CI (``CI`` set), where it skips
and says which one is missing.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.flight.proxy import FlightProxy
from openadapt_flow.flight.timeline import Timeline
from tests.fixtures.flight.fake_provider import FakeProvider

NAME = "Maria Gonzalez"
DOB = "03/14/1962"
MRN = "4471923"
PHONE = "(617) 555-0143"
ADDRESS = "42 Elm Street"
PHI = (NAME, DOB, MRN, PHONE, ADDRESS)


def _forms(value: str) -> set[str]:
    """The value and the encodings a payload could carry it in."""
    out = {value, quote(value), quote(value, safe=""), value.replace(" ", "+")}
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 5:
        out.add(digits)
    out |= {part for part in value.split() if len(part) > 3 and part.isalpha()}
    return out


def _scrubber():
    from openadapt_flow.flight.scrub import ScrubberUnavailable, require_scrubber

    try:
        return require_scrubber()
    except ScrubberUnavailable as exc:
        if os.environ.get("CI"):
            pytest.skip(f"CI without the scrubber's dependencies: {exc}")
        raise


def _screenshot() -> bytes:
    image = Image.new("RGB", (960, 380), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    rows = [
        ("Patient name", NAME),
        ("Date of birth", DOB),
        ("MRN", MRN),
        ("Phone", PHONE),
        ("Address", f"{ADDRESS}, Springfield"),
    ]
    for i, (label, value) in enumerate(rows):
        y = 24 + i * 68
        draw.text((24, y), label, fill=(60, 60, 60), font=font)
        draw.rectangle((260, y - 8, 940, y + 38), outline=(150, 150, 150))
        draw.text((272, y), value, fill=(0, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _ocr(png: bytes) -> str:
    import pytesseract

    return pytesseract.image_to_string(Image.open(io.BytesIO(png)))


def _readable(value: str, text: str) -> bool:
    norm = re.sub(r"[^0-9a-z]", "", value.lower())
    hay = re.sub(r"[^0-9a-z]", "", text.lower())
    if norm in hay:
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return value == NAME and any(p.lower() in words for p in NAME.split())


def _anthropic(png_b64: str) -> dict:
    url = f"https://emr.example.test/patients/{MRN}?name={quote(NAME)}"
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "system": f"You are updating the chart of {NAME}.",
        "tools": [{"type": "computer_20250124", "name": "computer"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Patient {NAME}, DOB {DOB}, MRN {MRN}, phone {PHONE}, "
                            f"lives at {ADDRESS}. Chart: {url}"
                        ),
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png_b64,
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "lookup_patient",
                        "input": {
                            "name": NAME,
                            "dob": DOB,
                            "query": f"MRN {MRN}",
                            "mrn_number": int(MRN),
                            "phone_digits": int(re.sub(r"\D", "", PHONE)),
                        },
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "patient": NAME,
                                        "phone": PHONE,
                                        "address": ADDRESS,
                                        "mrn": MRN,
                                    }
                                ),
                            },
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": png_b64,
                                },
                            },
                        ],
                    }
                ],
            },
        ],
    }


def _openai(png_b64: str) -> dict:
    return {
        "model": "computer-use-preview",
        "instructions": f"Update the record of {NAME} ({DOB}).",
        "tools": [{"type": "computer_use_preview", "environment": "browser"}],
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "fill_field",
                "arguments": json.dumps(
                    {"value": NAME, "dob": DOB, "phone": PHONE, "address": ADDRESS}
                ),
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": f"filled {NAME} / {MRN}",
            },
            {
                "type": "computer_call_output",
                "call_id": "call_2",
                "output": {
                    "type": "computer_screenshot",
                    "image_url": "data:image/png;base64," + png_b64,
                },
                "current_url": (
                    f"https://emr.example.test/chart/{MRN}/"
                    f"{quote(NAME)}?dob={quote(DOB, safe='')}"
                ),
            },
        ],
    }


def _post(url: str, body: dict) -> int:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": "sk-test-fake"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _forwarded_images(body: dict) -> list[bytes]:
    out: list[bytes] = []

    def walk(node):
        if isinstance(node, dict):
            source = node.get("source")
            if node.get("type") == "image" and isinstance(source, dict):
                out.append(base64.b64decode(source["data"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.startswith("data:image/"):
            out.append(base64.b64decode(node.split(",", 1)[1]))

    walk(body)
    return out


def test_synthetic_phi_never_reaches_the_model_payload(tmp_path: Path):
    scrubber = _scrubber()
    png = _screenshot()
    before = _ocr(png)
    # The baseline: tesseract reads every value in the original screenshot.
    for value in PHI:
        assert _readable(value, before), f"tesseract can't read {value!r} before"
    png_b64 = base64.b64encode(png).decode()

    provider = FakeProvider().start()
    timeline = Timeline(tmp_path / "run", task_id="phi", executor_label="test")
    proxy = FlightProxy(
        timeline,
        upstreams={"anthropic": provider.url, "openai": provider.url + "/v1"},
        scrubber=scrubber,
    ).start()
    try:
        assert (
            _post(proxy.base_url("anthropic") + "/v1/messages", _anthropic(png_b64))
            == 200
        )
        assert _post(proxy.base_url("openai") + "/responses", _openai(png_b64)) == 200
    finally:
        proxy.stop()
        provider.stop()

    assert len(provider.requests) == 2
    raw = provider.received_bytes()
    for value in PHI:
        for form in _forms(value):
            assert form.encode() not in raw, f"{form!r} reached the provider"

    images = []
    for request in provider.requests:
        images += _forwarded_images(json.loads(request["body"]))
    assert len(images) == 3
    for image in images:
        after = _ocr(image)
        for value in PHI:
            assert not _readable(value, after), f"{value!r} is readable: {after!r}"
        assert Image.open(io.BytesIO(image)).size == Image.open(io.BytesIO(png)).size

    # The protocol fields the providers check came through unchanged.
    sent = json.loads(provider.requests[1]["body"])
    assert sent["input"][0]["name"] == "fill_field"
    assert sent["input"][2]["call_id"] == "call_2"
    assert sent["input"][2]["current_url"] == "https://emr.example.test/<path removed>"
