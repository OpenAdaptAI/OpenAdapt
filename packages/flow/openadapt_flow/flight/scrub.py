"""Scrub a model API request body before it leaves the machine.

Every string in the body is scrubbed, wherever it sits, with three kinds of
exception that must reach the provider unchanged:

* opaque signed blocks (``thinking``, ``redacted_thinking``, ``reasoning``)
  pass byte-identical, because the provider checks their signatures. They
  came from the provider, so resending them sends nothing new;
* tool definitions, ``tool_choice``, ``model``, ``metadata`` and the other
  request settings listed in :data:`_TOP_SETTINGS`;
* protocol fields: block and item ``type``, ``role``, ids
  (``id``, ``tool_use_id``, ``call_id``, ``previous_response_id``),
  ``signature``, ``encrypted_content``, ``media_type``, ``cache_control``,
  and the tool ``name`` of a tool call.

Inside a tool call's arguments (Anthropic ``tool_use.input``, OpenAI
``function_call.arguments`` and Chat Completions ``function.arguments``) and
inside a tool result, no key is exempt: every string value is scrubbed.
Arguments sent as a JSON string are parsed, scrubbed leaf by leaf and
serialised again, so they stay valid JSON.

Each string goes through two passes:

1. URLs are cut to their origin (``https://emr.example/<path removed>``):
   record ids and names in paths and query strings are not left to NER.
2. The deterministic patterns in :data:`PATTERNS` (dates, phone numbers,
   e-mail, street addresses, numbers of five or more digits, and values
   after a ``Name:``/``MRN:``/``DOB:`` style label), then the
   openadapt-privacy text scrubber (Presidio), then the patterns again.

Images (Anthropic base64 ``image`` blocks, ``data:image/...`` URLs) are
always redacted with openadapt-privacy image redaction, then OCR'd with
tesseract: each OCR line goes through the patterns and the text scrubber, and
every word either of them removes is painted over too. An image by
remote URL, a file input, a document that is not plain text, a string the
scrubber returns no text for, or any scrubber error raises
:class:`ScrubRefused`, and the caller must not send the request.

:func:`require_scrubber` probes all of this once (the provider, its spaCy
model, tesseract, one text scrub and one image scrub) and raises
:class:`ScrubberUnavailable` when any part can't run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import shutil
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "PATTERNS",
    "Scrub",
    "ScrubRefused",
    "ScrubberUnavailable",
    "redact_image_patterns",
    "require_scrubber",
    "scrub_pattern_text",
    "scrub_request",
]


class ScrubRefused(Exception):
    """The request can't be scrubbed whole, so it is not forwarded."""


class ScrubberUnavailable(Exception):
    """The text or image scrubber can't run on this machine."""


# -- deterministic patterns --------------------------------------------------

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)
_STREET = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|"
    r"Ct|Way|Place|Pl|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|Hwy|Square|"
    r"Sq|Trail|Trl|Loop|Row|Alley|Pike)"
)
_LABEL = (
    r"(?:patient(?:\s+name)?|name|pt|dob|date\s+of\s+birth|birth\s*date|mrn|"
    r"medical\s+record(?:\s+(?:no|number|#))?|chart(?:\s+(?:no|number|#))?|"
    r"account(?:\s+(?:no|number|#))?|acct|member\s+id|patient\s+id|ssn|"
    r"phone|tel|mobile|address|addr|e-?mail)"
)
#: (placeholder, compiled pattern) pairs applied after the NER scrubber.
#: General shapes only: no application, vendor or corpus value is named.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "<LABELLED>",
        re.compile(
            r"(?i)(?<![A-Za-z])" + _LABEL + r"(?![A-Za-z])\s*[:#=]\s*"
            r"(?P<value>(?![<\s])[^\n,;|]{1,60}?)(?=\s*(?:[\n,;|]|$|\s{2,}|"
            + r"(?<![A-Za-z])"
            + _LABEL
            + r"\s*[:#=]))"
        ),
    ),
    ("<EMAIL>", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    (
        "<ADDRESS>",
        re.compile(
            r"(?i)\b\d{1,6}\s+(?:[A-Za-z][A-Za-z'.-]*\s+){1,4}"
            + _STREET
            + r"\b\.?(?:\s*,?\s*(?:Apt|Apartment|Unit|Suite|Ste|#)\.?\s*[\w-]+)?"
        ),
    ),
    ("<DATE>", re.compile(r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b")),
    (
        "<DATE>",
        re.compile(
            r"(?i)\b(?:" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|"
            r"\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+\d{2,4})\b"
        ),
    ),
    (
        "<PHONE>",
        re.compile(
            r"(?<![\w])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])"
        ),
    ),
    ("<ID>", re.compile(r"(?<![\w])[\d-]*\d{3}-\d{2}-\d{4}(?![\w])")),
    # Any token carrying five or more digits: record numbers, ZIP codes,
    # account numbers, whatever the label.
    (
        "<ID>",
        re.compile(r"(?<![\w-])(?=(?:[A-Za-z-]*\d){5})[A-Za-z0-9][A-Za-z0-9-]*\d\w*"),
    ),
)

_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp|file)://[^\s\"'<>()\[\]{}]+")


def _cut_url(match: re.Match[str]) -> str:
    from urllib.parse import urlsplit

    url = match.group(0)
    try:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return "<URL>"
    if "@" in parts.netloc:  # credentials in the authority
        return f"{parts.scheme}://<URL>"
    rest = url[len(origin) :]
    if rest in ("", "/"):
        return url
    return origin + "/<path removed>"


def scrub_urls(text: str) -> str:
    """``text`` with every URL cut to its origin."""
    return _URL.sub(_cut_url, text)


def scrub_pattern_text(text: str) -> str:
    """``text`` with every :data:`PATTERNS` match replaced (URLs kept to
    their origin, untouched by the other patterns)."""
    pieces: list[str] = []
    last = 0
    for match in _URL.finditer(text):
        pieces.append(_apply_patterns(text[last : match.start()]))
        pieces.append(_cut_url(match))
        last = match.end()
    pieces.append(_apply_patterns(text[last:]))
    return "".join(pieces)


def _apply_patterns(text: str) -> str:
    if not text:
        return text
    for placeholder, pattern in PATTERNS:
        if "value" in pattern.groupindex:

            def labelled(m: re.Match[str]) -> str:
                start = m.start("value") - m.start()
                return m.group(0)[:start] + "<REDACTED>"

            text = pattern.sub(labelled, text)
        else:
            text = pattern.sub(placeholder, text)
    return text


def _pattern_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for _placeholder, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if "value" in pattern.groupindex:
                spans.append((m.start("value"), m.end("value")))
            else:
                spans.append((m.start(), m.end()))
    for m in _URL.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


OcrWords = Callable[[Any], Sequence[tuple[str, tuple[int, int, int, int], int]]]


def _tesseract_words(image: Any) -> list[tuple[str, tuple[int, int, int, int], int]]:
    """(word, (x, y, w, h), line key) for each word tesseract reads."""
    import pytesseract

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    out = []
    for i, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        key = hash((data["block_num"][i], data["par_num"][i], data["line_num"][i]))
        box = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        out.append((str(word), box, key))
    return out


def redact_image_patterns(
    png: bytes,
    ocr_words: Optional[OcrWords] = None,
    text_scrub: Optional[Callable[[str], str]] = None,
) -> bytes:
    """``png`` with each OCR'd word inside a :data:`PATTERNS` match painted
    over, and, given ``text_scrub``, each word the text scrubber removes from
    its OCR line (a name the image redactor's own pass missed). OCR and
    scrubber errors propagate (the caller refuses the send)."""
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(png)).convert("RGB")
    words = list((ocr_words or _tesseract_words)(image))
    lines: dict[int, list[tuple[str, tuple[int, int, int, int]]]] = {}
    for word, box, key in words:
        lines.setdefault(key, []).append((word, box))
    boxes: list[tuple[int, int, int, int]] = []
    for line in lines.values():
        text = ""
        offsets = []
        for word, box in line:
            if text:
                text += " "
            offsets.append((len(text), len(text) + len(word), box))
            text += word
        for start, end in _pattern_spans(text):
            for w_start, w_end, box in offsets:
                if w_start < end and start < w_end:
                    boxes.append(box)
        if text_scrub is not None:
            kept = text_scrub(text)
            if not isinstance(kept, str):
                raise ScrubRefused("the text scrubber returned no text")
            survivors = set(re.findall(r"\w+", kept))
            for w_start, w_end, box in offsets:
                if any(
                    t not in survivors for t in re.findall(r"\w+", text[w_start:w_end])
                ):
                    boxes.append(box)
    if not boxes:
        return png
    draw = ImageDraw.Draw(image)
    for x, y, w, h in boxes:
        draw.rectangle((x - 2, y - 2, x + w + 2, y + h + 2), fill=(0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# -- the scrubber ------------------------------------------------------------


class Scrub:
    """Memoised scrubbing of one run's strings and images (deterministic, so
    the provider's prompt cache still matches across turns)."""

    def __init__(self, scrubber: Any, *, ocr_words: Optional[OcrWords] = None) -> None:
        self.scrubber = scrubber
        self.ocr_words = ocr_words
        self._images: OrderedDict[str, bytes] = OrderedDict()
        self._texts: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def text(self, value: str) -> str:
        from openadapt_flow.privacy import scrub_outbound_text

        if not value or not value.strip():
            return value
        key = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._texts.get(key)
        if hit is not None:
            return hit
        # Patterns first (NER can split a street number or a record
        # number into a date and leave the rest), then NER, then patterns
        # again on what NER left.
        out = scrub_outbound_text(scrub_pattern_text(value), self.scrubber)
        if not isinstance(out, str):
            raise ScrubRefused("the text scrubber returned no text")
        out = scrub_pattern_text(out)
        with self._lock:
            self._texts[key] = out
            while len(self._texts) > 8192:
                self._texts.popitem(last=False)
        return out

    def image(self, raw: bytes) -> tuple[bytes, bool]:
        """(scrubbed PNG, whether it is new this run)."""
        from PIL import Image

        from openadapt_flow.privacy import scrub_outbound_image, scrub_outbound_text

        key = hashlib.sha256(raw).hexdigest()
        with self._lock:
            hit = self._images.get(key)
        if hit is not None:
            return hit, False
        try:
            before = Image.open(io.BytesIO(raw))
            before.load()
        except Exception as exc:  # noqa: BLE001 - undecodable: refuse
            raise ScrubRefused(f"an image could not be decoded ({type(exc).__name__})")
        out = scrub_outbound_image(raw, self.scrubber)
        out = redact_image_patterns(
            out,
            self.ocr_words,
            lambda line: scrub_outbound_text(line, self.scrubber) or "",
        )
        after = Image.open(io.BytesIO(out))
        if after.size != before.size:
            raise ScrubRefused("the image scrubber changed the image size")
        with self._lock:
            self._images[key] = out
            while len(self._images) > 512:
                self._images.popitem(last=False)
        return out, True


def require_scrubber(*, probe_image: bool = True) -> Any:
    """The openadapt-privacy provider, checked to work end to end, or raise
    :class:`ScrubberUnavailable` naming what is missing."""
    from openadapt_flow.privacy import get_scrubber

    try:
        import pytesseract  # noqa: F401
    except ImportError:
        raise ScrubberUnavailable(
            "pytesseract is not installed, so screenshots can't be scrubbed"
        ) from None
    if shutil.which("tesseract") is None:
        raise ScrubberUnavailable(
            "the tesseract binary is not on PATH, so screenshots can't be scrubbed"
        )
    scrubber = get_scrubber()
    if scrubber is None:
        raise ScrubberUnavailable(
            "openadapt-privacy is not installed (pip install 'openadapt-flow[privacy]'"
            " and python -m spacy download en_core_web_sm)"
        )
    validate = getattr(scrubber, "validate_ready", None)
    try:
        if callable(validate):
            # openadapt-privacy's Modality values (TEXT, PIL_IMAGE).
            validate(["TEXT", "PIL_IMAGE"] if probe_image else ["TEXT"])
        from openadapt_flow.privacy import scrub_outbound_text

        probe = scrub_outbound_text("Patient John Smith called today.", scrubber)
        if not isinstance(probe, str) or "Smith" in probe:
            raise ScrubberUnavailable("the text scrubber left a name in a probe")
        scrub = Scrub(scrubber)
        if probe_image:
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (64, 32), (255, 255, 255)).save(buf, format="PNG")
            scrub.image(buf.getvalue())
    except ScrubberUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. the spaCy model is missing
        raise ScrubberUnavailable(
            f"the scrubber can't run ({type(exc).__name__}: {exc})"
        ) from None
    return scrubber


# -- the request walk --------------------------------------------------------

#: Blocks passed through byte-identical: their signatures must verify.
_OPAQUE_TYPES = {"thinking", "redacted_thinking", "reasoning"}
#: Top-level request settings, never scrubbed (definitions and options).
_TOP_SETTINGS = {
    "model",
    "tools",
    "tool_choice",
    "metadata",
    "response_format",
    "text",
    "reasoning",
    "thinking",
    "stream",
    "stream_options",
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "stop",
    "truncation",
    "service_tier",
    "parallel_tool_calls",
    "include",
    "store",
    "previous_response_id",
    "prompt_cache_key",
    "modalities",
    "functions",
    "function_call",
    "n",
    "seed",
    "logprobs",
    "top_logprobs",
    "presence_penalty",
    "frequency_penalty",
    "betas",
    "container",
    "mcp_servers",
}
#: Protocol fields kept unchanged at any structural position.
_PROTOCOL_KEYS = {
    "type",
    "role",
    "id",
    "tool_use_id",
    "tool_call_id",
    "call_id",
    "signature",
    "encrypted_content",
    "media_type",
    "cache_control",
    "status",
    "detail",
}
#: Blocks whose ``name`` is a tool name (protocol, not data).
_TOOL_CALL_TYPES = {
    "tool_use",
    "server_tool_use",
    "mcp_tool_use",
    "function_call",
    "custom_tool_call",
    "mcp_call",
    "function",
}
#: Where a tool call carries its arguments: every string under it is data.
_ARGUMENT_KEYS = {"input", "arguments"}
#: OpenAI safety-check lists: their ids must come back unchanged.
_SAFETY_KEYS = {"pending_safety_checks", "acknowledged_safety_checks"}
#: Tool results: every string under them is data.
_RESULT_TYPES = {
    "tool_result",
    "function_call_output",
    "computer_call_output",
    "custom_tool_call_output",
    "mcp_tool_result",
    "web_search_tool_result",
}


def _b64(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ScrubRefused("an image's base64 data could not be decoded") from None


def scrub_request(
    body: Any, scrub: Any, on_frame: Optional[Callable[[bytes], None]] = None
) -> Any:
    """Return a scrubbed copy of a JSON request body, or raise ScrubRefused.

    ``scrub`` has ``text(str) -> str`` and ``image(bytes) -> (png, new)``.
    ``on_frame`` gets each scrubbed image seen for the first time.
    """

    def image(raw: bytes) -> bytes:
        png, new = scrub.image(raw)
        if new and on_frame is not None:
            on_frame(png)
        return png

    def data_url(value: str) -> str:
        head, sep, data = value.partition(",")
        if not sep or ";base64" not in head:
            raise ScrubRefused("an image data URL is not base64")
        png = image(_b64(data))
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    def string(value: str, *, image_slot: bool) -> str:
        if value.startswith("data:"):
            if value[5:].lower().startswith("image/"):
                return data_url(value)
            raise ScrubRefused("a non-image data URL can't be scrubbed")
        if image_slot and value.lower().startswith(("http://", "https://")):
            raise ScrubRefused("an image by URL can't be scrubbed before it is sent")
        out = scrub.text(value)
        if not isinstance(out, str):
            raise ScrubRefused("the text scrubber returned no text")
        return out

    def arguments(value: str) -> str:
        """A JSON-string argument list: scrub its leaves, keep it JSON."""
        try:
            parsed = json.loads(value)
        except ValueError:
            return string(value, image_slot=False)
        if isinstance(parsed, str):
            return json.dumps(payload(parsed, None))
        return json.dumps(payload(parsed, None), ensure_ascii=False)

    def payload(node: Any, key: Optional[str]) -> Any:
        """Tool arguments and results: every string is data."""
        if isinstance(node, str):
            return string(node, image_slot=False)
        if isinstance(node, list):
            return [payload(item, key) for item in node]
        if isinstance(node, dict):
            kind = node.get("type")
            if kind in _OPAQUE_TYPES:
                return node
            if kind in ("image", "document", "input_image", "image_url", "input_file"):
                return structural(node, key)
            return {k: payload(v, k) for k, v in node.items()}
        return node

    def structural(node: Any, key: Optional[str], parent_type: Any = None) -> Any:
        if isinstance(node, str):
            image_slot = key == "image_url" or (
                key == "url" and parent_type in ("image_url", "input_image", "image")
            )
            return string(node, image_slot=image_slot)
        if isinstance(node, list):
            return [structural(item, key, parent_type) for item in node]
        if not isinstance(node, dict):
            return node
        kind = node.get("type")
        if kind in _OPAQUE_TYPES:
            return node
        source = node.get("source")
        if kind in ("image", "document") and isinstance(source, dict):
            if kind == "image" and source.get("type") == "base64":
                png = image(_b64(str(source.get("data", ""))))
                return {
                    **{
                        k: structural(v, k, kind)
                        for k, v in node.items()
                        if k != "source"
                    },
                    "source": {
                        **source,
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                }
            if kind == "document" and source.get("type") == "text":
                return {
                    **{
                        k: structural(v, k, kind)
                        for k, v in node.items()
                        if k != "source"
                    },
                    "source": {
                        **source,
                        "data": string(str(source.get("data", "")), image_slot=False),
                    },
                }
            raise ScrubRefused(
                f"a {kind} sent as {source.get('type')!r} can't be scrubbed"
            )
        if kind in ("input_file", "file"):
            raise ScrubRefused("a file input can't be scrubbed")
        tool_call = kind in _TOOL_CALL_TYPES or key == "function"
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _PROTOCOL_KEYS or (k == "name" and tool_call):
                out[k] = v
            elif k in _ARGUMENT_KEYS and tool_call:
                out[k] = arguments(v) if isinstance(v, str) else payload(v, k)
            elif k in _SAFETY_KEYS:
                out[k] = structural(v, k, kind)
            elif kind in _RESULT_TYPES and k not in ("output", "content"):
                out[k] = payload(v, k)
            elif kind in _RESULT_TYPES and isinstance(v, str):
                out[k] = arguments(v)
            else:
                out[k] = structural(v, k, kind)
        return out

    if not isinstance(body, dict):
        return structural(body, None)
    clean: dict[str, Any] = {}
    for k, v in body.items():
        clean[k] = v if k in _TOP_SETTINGS else structural(v, k)
    return clean
