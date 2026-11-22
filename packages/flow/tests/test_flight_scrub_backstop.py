"""Base64 images in any encoding shape are scrubbed or the request refused.

Two layers: JSON escapes are decoded (a few levels) before base64 runs are
looked for, and a density backstop refuses any string that still holds a
512-character window of base64-like text. The cases are the reviewer's
probes on #2493, plus a fuzz test that encodes random images and transforms
them at random: each must be scrubbed or refused, never forwarded.
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import struct
import urllib.error
import urllib.request
from typing import Any

import pytest
from PIL import Image

from openadapt_flow.flight.scrub import ScrubRefused, dense_payload, scrub_request

_ALPHA_ONLY = re.compile(r"[^A-Za-z0-9+/_-]")


class Stub:
    """Text unchanged; images counted and replaced by a marker PNG."""

    def __init__(self) -> None:
        self.images = 0
        self.texts: list[str] = []

    def text(self, value: str) -> str:
        self.texts.append(value)
        return value

    def image(self, raw: bytes) -> tuple[bytes, bool]:
        # As the real scrubber does: an image that can't be decoded whole
        # (cut short, split across strings) refuses.
        try:
            with Image.open(io.BytesIO(raw)) as decoded:
                decoded.load()
        except Exception as exc:  # noqa: BLE001
            raise ScrubRefused(f"undecodable image ({type(exc).__name__})") from None
        self.images += 1
        return b"\x89PNG\r\n\x1a\nSCRUBBED", True


def _bmp(w: int = 40, h: int = 40) -> bytes:
    row = (w * 3 + 3) // 4 * 4
    px = bytes((i * 7) % 256 for i in range(row * h))
    header = b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(px), 0, 0, 0, 0)
    return header + info + px


B64 = base64.b64encode(_bmp()).decode()


def _fold(text: str, width: int = 76, sep: str = "\n") -> str:
    return sep.join(text[i : i + width] for i in range(0, len(text), width))


def _outcome(text: str, *, block: bool = False) -> tuple[str, Stub, str]:
    stub = Stub()
    content: Any = [{"type": "text", "text": text}] if block else text
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": content}
                ],
            }
        ]
    }
    try:
        out = scrub_request(body, stub)
    except ScrubRefused:
        return "refused", stub, ""
    return "sent", stub, json.dumps(out)


def _leaked(payload_b64: str, forwarded: str) -> bool:
    """Whether a middle stretch of the payload survives in what is sent,
    with every separator and escape taken out."""
    middle = _ALPHA_ONLY.sub("", payload_b64)[300:364]
    return middle in _ALPHA_ONLY.sub("", forwarded)


PHP = json.dumps({"id": 7, "mimetype": "image/bmp", "data": B64}).replace("/", "\\/")
CASES = {
    "plain": B64,
    "img-src-folded": _fold('<img alt="scan" src="data:image/bmp;base64,' + B64 + '">'),
    "prefix-folded": _fold("Attachment from the referral letter: " + B64),
    "json-escaped-lf": json.dumps({"file": _fold(B64)}),
    "json-escaped-crlf": json.dumps({"file": _fold(B64, sep="\r\n")}),
    "php-slash-single": json.dumps({"file": B64}).replace("/", "\\/"),
    "php-slash-wrapped": json.dumps({"file": _fold(B64)}).replace("/", "\\/"),
    "json-string-of-json": json.dumps(json.dumps({"file": _fold(B64)})),
    "groups-of-4": " ".join(B64[i : i + 4] for i in range(0, len(B64), 4)),
    "report-done": "Report: " + B64 + " done",
    "tab-separated": json.dumps({"file": "\t".join(_fold(B64).split("\n"))}),
    "html-entity": "&#10;".join(_fold(B64).split("\n")),
    "br-joined": "<br>".join(_fold(B64).split("\n")),
    "quoted-concat": '"+\n"'.join(_fold(B64).split("\n")),
    "php-json-in-response": "HTTP/1.1 200 OK\n" + PHP,
    "curl-prefixed": "$ curl ...\n" + PHP,
}


@pytest.mark.parametrize("block", [False, True], ids=["tool-result", "text-block"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_every_probe_shape_is_scrubbed_or_refused(name, block):
    result, stub, forwarded = _outcome(CASES[name], block=block)
    if result == "sent":
        assert stub.images >= 1, name
        assert not _leaked(B64, forwarded), name


def test_escaped_and_nested_json_is_scrubbed_not_just_refused():
    for name in ("php-slash-single", "php-slash-wrapped", "json-string-of-json"):
        result, stub, _ = _outcome(CASES[name])
        assert result == "sent" and stub.images == 1, name


@pytest.mark.parametrize(
    "text",
    [
        "The patient was seen today and the following notes apply to the "
        "encounter in full detail. " * 6,
        "Internationalization considerations antidisestablishmentarianism "
        "notwithstanding characteristically " * 5,
        "see https://emr.example/" + "patients/abc-def/" * 40,
        "see emr.example/" + "Patients/ABC-def/0123/" * 40,
        "\n".join(["see the patient chart and add a note"] * 40),
        "Order ID 4471923, ward B, bed 12. " * 30,
    ],
)
def test_prose_and_urls_are_not_refused(text):
    assert not dense_payload(text)
    result, stub, _ = _outcome(text)
    assert result == "sent" and stub.images == 0


def test_a_jwt_or_opaque_blob_refuses():
    token = base64.urlsafe_b64encode(
        b'{"sub":"x","name":"Zed Quux","pad":"' + b"y" * 600 + b'"}'
    ).decode()
    for text in ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9." + token + ".sig",):
        assert _outcome(text)[0] == "refused"


# -- fuzz ------------------------------------------------------------------


def _random_image(rng: random.Random) -> bytes:
    from PIL import ImageDraw

    w, h = rng.randint(8, 160), rng.randint(8, 64)
    kind = rng.randrange(3)
    if kind == 0:  # noise
        image = Image.new("RGB", (w, h))
        image.putdata(
            [
                (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(w * h)
            ]
        )
    else:  # a white page with a line of text: mostly one byte value
        image = Image.new("RGB", (w, h), "white")
        ImageDraw.Draw(image).text((2, 2), "John Smith 4471923", fill="black")
        if kind == 2:
            image = image.convert("1")  # 1-bit
    fmt = rng.choice(["PNG", "BMP", "JPEG", "GIF", "TIFF"])
    if fmt == "JPEG" and image.mode == "1":
        image = image.convert("L")
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()


def _transform(rng: random.Random, b64: str) -> str:
    text = b64
    shape = rng.randrange(11)
    if shape == 8:  # separators the code does not know, widths under 64
        width = rng.randint(8, 63)
        sep = rng.choice(["|", ";", ",", " | ", "#", "~", "::"])
        return _fold(text, width, sep)
    if shape == 9:  # a JSON array of chunks
        width = rng.randint(8, 63)
        chunks = [text[i : i + width] for i in range(0, len(text), width)]
        return json.dumps({"chunks": chunks})
    if shape == 10:  # Python quoted lines
        width = rng.randint(8, 63)
        lines = [
            "    '" + text[i : i + width] + "'" for i in range(0, len(text), width)
        ]
        return "ICON = (\n" + "\n".join(lines) + "\n)"
    if shape == 1:
        width = rng.choice([16, 32, 40, 64, 76, 100])
        sep = rng.choice(["\n", "\r\n", "\n  ", "\t", " ", "<br>", "&#10;", '"+\n"'])
        text = _fold(text, width, sep)
    elif shape == 2:
        size = rng.randint(1, 20)
        text = " ".join(text[i : i + size] for i in range(0, len(text), size))
    elif shape == 3:
        text = _fold(text, rng.choice([64, 76]))
        text = json.dumps({"file": text})
    elif shape == 4:
        text = json.dumps({"data": text}).replace("/", "\\/")
    elif shape == 5:
        text = json.dumps(json.dumps({"file": _fold(text)}))
    elif shape == 6:
        text = "data:image/png;base64," + _fold(text, 76)
    elif shape == 7:
        text = "Content-Transfer-Encoding: base64\r\n\r\n" + _fold(text, 76, "\r\n")
    prefix = rng.choice(["", "screenshot: ", '<img src="', "Report:\n", "x"])
    suffix = rng.choice(["", " done", '">', "\r\n--boundary--", "\n"])
    return prefix + text + suffix


def test_fuzz_random_images_in_random_shapes_never_leave_unscrubbed():
    rng = random.Random(2493)
    for _ in range(800):
        raw = _random_image(rng)
        encode = rng.choice([base64.b64encode, base64.urlsafe_b64encode])
        b64 = encode(raw).decode()
        text = _transform(rng, b64)
        result, stub, forwarded = _outcome(text, block=rng.random() < 0.5)
        if result == "refused":
            continue
        assert stub.images >= 1, text[:80]
        if len(_ALPHA_ONLY.sub("", b64)) >= 364:
            assert not _leaked(b64, forwarded), text[:80]


# -- the backstop sends nothing --------------------------------------------

OPAQUE_GROUPS = " ".join(
    _b[i : i + 4]
    for _b in [base64.b64encode(random.Random(7).randbytes(1200)).decode()]
    for i in range(0, len(_b), 4)
)


def test_opaque_encoded_data_split_into_groups_refuses():
    assert dense_payload(OPAQUE_GROUPS)
    assert _outcome(OPAQUE_GROUPS)[0] == "refused"


def test_the_backstop_refusal_sends_zero_bytes_upstream(tmp_path):
    from openadapt_flow.flight.proxy import FlightProxy
    from openadapt_flow.flight.timeline import Timeline
    from tests.fixtures.flight.fake_provider import FakeProvider

    class FakeScrubber:
        def scrub_text(self, text, is_separated=False):
            return text

        def scrub_image(self, image, fill_color=None):
            return image

    provider = FakeProvider().start()
    proxy = FlightProxy(
        Timeline(tmp_path / "run", task_id="t", executor_label="x"),
        upstreams={"anthropic": provider.url},
        scrubber=FakeScrubber(),
        ocr_words=lambda image: [],
    ).start()
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 8,
        # Not an image: random bytes, base64 in groups of four. Only the
        # density backstop can tell this is encoded data.
        "messages": [{"role": "user", "content": OPAQUE_GROUPS}],
    }
    request = urllib.request.Request(
        proxy.base_url("anthropic") + "/v1/messages",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(request, timeout=60)
    finally:
        proxy.stop()
        provider.stop()
    assert err.value.code == 502
    assert b"encoded data" in err.value.read()
    assert provider.requests == [] and provider.received_bytes() == b""


# -- the reviewer's last-round probes ----------------------------------------


def _page(fmt: str, mode: str = "RGB", size: tuple[int, int] = (120, 40)) -> bytes:
    from PIL import ImageDraw

    image = Image.new(mode, size, "white")
    ImageDraw.Draw(image).text((2, 10), "John Smith", fill="black")
    buf = io.BytesIO()
    image.save(buf, fmt)
    return buf.getvalue()


@pytest.mark.parametrize(
    "raw",
    [
        _page("PNG"),
        _page("BMP"),
        _page("BMP", "1"),
        _page("GIF"),
        _page("TIFF"),
        _page("BMP", size=(400, 60)),
    ],
    ids=["png", "bmp", "bmp-1bit", "gif", "tiff", "bmp-wide"],
)
@pytest.mark.parametrize(
    "shape",
    [
        "pyconcat",
        "single-plus",
        "pipe",
        "hex-entity",
        "comma-quoted",
        "json-array",
        "pipe48",
        "u-escape",
        "double-escaped",
    ],
)
def test_white_pages_in_unknown_separators_are_scrubbed_or_refused(raw, shape):
    b64 = base64.b64encode(raw).decode()
    chunks = [b64[i : i + 60] for i in range(0, len(b64), 60)]
    text = {
        "pyconcat": "data = (\n" + "\n".join("    '" + c + "'" for c in chunks) + "\n)",
        "single-plus": "x = '" + "' + '".join(chunks) + "'",
        "pipe": "|".join(chunks),
        "hex-entity": "&#xA;".join(b64[i : i + 76] for i in range(0, len(b64), 76)),
        "comma-quoted": '"' + '","'.join(chunks) + '"',
        "json-array": json.dumps({"chunks": chunks}),
        "pipe48": "|".join(b64[i : i + 48] for i in range(0, len(b64), 48)),
        "u-escape": "".join("\\u%04x" % ord(c) for c in b64),
        "double-escaped": json.dumps(json.dumps({"d": b64.replace("/", "\\/")})),
    }[shape]
    result, stub, forwarded = _outcome(text, block=True)
    if result == "sent":
        assert stub.images >= 1
        assert not _leaked(b64, forwarded)


def test_an_image_split_across_text_blocks_refuses():
    b64 = base64.b64encode(_page("PNG")).decode()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "content": [
                            {"type": "text", "text": b64[i : i + 200]}
                            for i in range(0, len(b64), 200)
                        ],
                    }
                ],
            }
        ]
    }
    with pytest.raises(ScrubRefused):
        scrub_request(body, Stub())


@pytest.mark.parametrize(
    "text",
    [
        "+----------+----------+\n| col      | value    |\n" * 40,
        "=" * 600,
        "Results\n" + "-" * 700 + "\nDone",
        "AAPL MSFT GOOG " * 60,
    ],
)
def test_tables_and_rules_are_not_refused_by_the_diversity_check(text):
    assert _outcome(text)[0] == "sent"


def _other_format(kind: str) -> bytes:
    """A small image in a format with no signature check here (random
    pixels, so it does not compress to one letter)."""
    rng = random.Random(kind)
    pixels = rng.randbytes(1800)
    if kind == "ppm":
        return b"P6\n30 20\n255\n" + pixels
    if kind == "qoi":
        return (
            b"qoif"
            + (30).to_bytes(4, "big")
            + (20).to_bytes(4, "big")
            + b"\x03\x00"
            + pixels
        )
    return b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + pixels  # avif


@pytest.mark.parametrize("kind", ["avif", "qoi", "ppm"])
def test_an_unrecognised_format_split_across_short_blocks_refuses(kind):
    b64 = base64.b64encode(_other_format(kind)).decode()
    blocks = [
        {"type": "text", "text": b64[i : i + 200]} for i in range(0, len(b64), 200)
    ]
    assert all(len(block["text"]) < 512 for block in blocks)
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": blocks}
                ],
            }
        ]
    }
    with pytest.raises(ScrubRefused, match="split across strings"):
        scrub_request(body, Stub())


def test_ordinary_text_across_many_blocks_is_not_refused():
    blocks = [
        {"type": "text", "text": f"Step {i}: opened the referral and added a note."}
        for i in range(60)
    ]
    body = {"messages": [{"role": "user", "content": blocks}]}
    scrub_request(body, Stub())
