"""Scrub a model API request body before it leaves the machine.

Every string in the body is scrubbed, wherever it sits, with three kinds of
exception that must reach the provider unchanged:

* opaque signed blocks (``thinking``, ``redacted_thinking``, ``reasoning``)
  pass byte-identical, because the provider checks their signatures. They
  came from the provider, so resending them sends nothing new;
* tool definitions, ``tool_choice``, ``model`` and the other request
  settings listed in :data:`_TOP_SETTINGS` (``metadata`` is not one: its
  values are caller data and are scrubbed, its keys kept);
* protocol fields: block and item ``type``, ``role``, ids
  (``id``, ``tool_use_id``, ``call_id``, ``previous_response_id``),
  ``signature``, ``encrypted_content``, ``media_type``, ``cache_control``,
  and the tool ``name`` of a tool call.

Inside a tool call's arguments (Anthropic ``tool_use.input``, OpenAI
``function_call.arguments`` and Chat Completions ``function.arguments``) and
in a tool result's fields, no key and no block type is exempt: every string
is scrubbed, and a base64 image is redacted. Arguments sent as a JSON object
or array in a string are parsed, scrubbed leaf by leaf and serialised again,
so they stay valid JSON. The content list of a tool result is a list of
protocol blocks (text, image), walked like message content. Numbers of five
or more digits anywhere outside the request settings become ``"<ID>"``.

Each string is handled in three steps:

1. URLs are cut to their origin (``https://emr.example/<path removed>``):
   record ids and names in paths and query strings are not left to NER.
2. Personal values are found (:func:`detect`): values already in the run's
   token map, the deterministic patterns in :data:`PATTERNS` (dates, phone
   numbers, e-mail, street addresses, numbers of five or more digits, and
   values after a ``Name:``/``MRN:``/``DOB:`` style label), and the
   openadapt-privacy text scrubber (Presidio) on the whole string and again
   on each line, sentence and quoted string (it misses names in long text
   that it finds in the same sentence alone). Presidio's ORGANIZATION and
   NRP hits are not identifiers and are left alone, unless Presidio asked
   about the same words alone names an identifier; a DATE_TIME with no
   digit ("today") too.
3. Each value is replaced with its run token (``[PERSON_1]``, see
   :mod:`openadapt_flow.flight.pseudonym`), or with a bare ``<PERSON>``
   placeholder when there is no token map.

Images (Anthropic base64 ``image`` blocks, ``data:image/...`` URLs) are
OCR'd with tesseract. Each OCR line, split into table cells at wide gaps,
goes through the same detection (without the whole-screen pass: NER over a
screen's lines joined together tags menu and header words as a person), and
each value is painted over with its token written in the box, so a name has
the same token in the instructions and on screen. An image by remote URL, a
file input, a document that is not plain text, a string the scrubber
returns no text for, or any scrubber or OCR error raises
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

#: A URL runs to the next space, quote or angle bracket. Brackets and
#: parentheses don't end it: a query such as ``?name=[Jane`` is cut whole.
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp|file)://[^\s\"'<>]+")
#: Trailing punctuation given back to the text when a URL has no query.
_URL_TAIL = ".,;:!)]}"


def _split_url(url: str) -> tuple[str, int, int]:
    """(kept prefix, end of the removed part, end of the URL) for ``url``.

    The kept prefix is the origin (``scheme://host[:port]``), or
    ``scheme://`` when the authority holds credentials. Everything from the
    path on is removed; with no ``?`` or ``#``, trailing punctuation that
    closes the sentence is kept as text."""
    from urllib.parse import urlsplit

    end = len(url)
    if "?" not in url and "#" not in url:
        while end > 0 and url[end - 1] in _URL_TAIL:
            end -= 1
    try:
        parts = urlsplit(url[:end])
        origin = f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return "", end, len(url)
    if "@" in parts.netloc:  # credentials in the authority
        return f"{parts.scheme}://", end, len(url)
    return origin, end, len(url)


def _cut_url(match: re.Match[str]) -> str:
    url = match.group(0)
    origin, end, total = _split_url(url)
    tail = url[end:total]
    if not origin:
        return "<URL>" + tail
    rest = url[len(origin) : end]
    if origin.endswith("://"):
        return origin + "<URL>" + tail
    if rest in ("", "/"):
        return url
    return origin + "/<path removed>" + tail


#: A URL without a scheme, as an address bar shows it: a host name (or
#: IP) with a dot, then a path. Not inside a URL that has a scheme.
_BARE_URL = re.compile(
    r"(?i)(?<![\w@/.:-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}|"
    r"\d{1,3}(?:\.\d{1,3}){3}|localhost)(:\d+)?(/[^\s\"'<>]*)"
)


def _bare_path(m: re.Match[str]) -> tuple[int, int]:
    """(start, end) of a scheme-less URL's path and query in its text."""
    start, end = m.start(3), m.end(3)
    path = m.group(3)
    if "?" not in path and "#" not in path:
        while end > start and m.string[end - 1] in _URL_TAIL:
            end -= 1
    return start, end


def scrub_urls(text: str) -> str:
    """``text`` with every URL cut to its origin, with or without a scheme."""
    text = _URL.sub(_cut_url, text)
    out = []
    last = 0
    for m in _BARE_URL.finditer(text):
        start, end = _bare_path(m)
        if text[start:end] in ("", "/") or text[start:end] == "/<path removed>":
            continue
        out.append(text[last:start])
        out.append("/<path removed>")
        last = end
    out.append(text[last:])
    return "".join(out)


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


#: Token kind for each pattern placeholder.
_PATTERN_KINDS = {
    "<EMAIL>": "EMAIL",
    "<ADDRESS>": "ADDRESS",
    "<DATE>": "DATE",
    "<PHONE>": "PHONE",
    "<ID>": "ID",
}
#: Token kind for the value after a label, by the label's first word.
_LABEL_KINDS = (
    (re.compile(r"(?i)^(patient(\s+name)?|name|pt)\b"), "PERSON"),
    (re.compile(r"(?i)^(dob|date\s+of\s+birth|birth\s*date)\b"), "DATE"),
    (re.compile(r"(?i)^(phone|tel|mobile)\b"), "PHONE"),
    (re.compile(r"(?i)^(address|addr)\b"), "ADDRESS"),
    (re.compile(r"(?i)^e-?mail\b"), "EMAIL"),
)
#: Presidio entity types and the token kind each becomes. A type not listed
#: becomes ID (fail closed); the types in _NER_DROP are not identifiers.
_NER_KINDS = {
    "PERSON": "PERSON",
    "DATE_TIME": "DATE",
    "PHONE_NUMBER": "PHONE",
    "EMAIL_ADDRESS": "EMAIL",
    "LOCATION": "LOCATION",
    "URL": "URL",
    "AGE": "AGE",
}
#: Presidio types that are not one of HIPAA's 18 identifiers and that hit
#: app names, headers and ordinary words ("DONE", "Triage"): not redacted,
#: unless a deterministic pattern matches the same text, or NER asked about
#: the same words alone names an identifier (a name it read in a sentence
#: as an organisation).
_NER_DROP = {"ORGANIZATION", "NRP"}
Span = tuple[int, int, str]


def _pattern_spans(text: str) -> list[Span]:
    """(start, end, kind) for every pattern match outside URLs."""
    spans: list[Span] = []
    for placeholder, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if "value" in pattern.groupindex:
                label = m.group(0)[: m.start("value") - m.start()]
                kind = next(
                    (k for rx, k in _LABEL_KINDS if rx.match(label.strip())), "ID"
                )
                spans.append((m.start("value"), m.end("value"), kind))
            else:
                spans.append((m.start(), m.end(), _PATTERN_KINDS[placeholder]))
    return spans


_WORDISH = re.compile(r"\w+|\s+|[^\w\s]")


def ner_spans(
    text: str, scrubbed: str, dropped: Optional[list[tuple[int, int]]] = None
) -> list[Span]:
    """Where the NER scrubber replaced text, and the kind it named.

    ``scrubbed`` is the scrubber's output for ``text``, with ``<TYPE>``
    placeholders. The two are aligned word by word; every original run the
    scrubber replaced or deleted is a span. A run it replaced only with a
    type in _NER_DROP is left out (and added to ``dropped``), and a
    DATE_TIME with no digit ("today") too.
    """
    import difflib

    a = [(m.start(), m.end(), m.group(0)) for m in _WORDISH.finditer(text)]
    b = [m.group(0) for m in _WORDISH.finditer(scrubbed)]
    matcher = difflib.SequenceMatcher(None, [t for _, _, t in a], b, autojunk=False)
    spans: list[Span] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "delete") or i1 == i2:
            continue
        start, end = a[i1][0], a[i2 - 1][1]
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        types = _PLACEHOLDER.findall("".join(b[j1:j2]))
        types = [t[1:-1] for t in types]
        kept = [t for t in types if t not in _NER_DROP]
        if types and not kept:
            if dropped is not None:
                dropped.append((start, end))
            continue
        kind = _NER_KINDS.get(kept[0], "ID") if kept else "ID"
        value = text[start:end]
        if kind == "DATE" and not any(ch.isdigit() for ch in value):
            continue
        spans.append((start, end, kind))
    return spans


#: Longest piece of text sent to the NER scrubber in one call.
_CHUNK_MAX = 200
#: A JSON-style quoted string inside text: its value is scrubbed alone.
_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_PLACEHOLDER = re.compile(r"<[A-Z_]+>")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
#: Text no detector may touch: tokens, placeholders, cut URL paths.
_PROTECTED = re.compile(r"\[[A-Z]+_\d+\]|<[A-Z_]+>|<path removed>")


def piece_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each line, sentence and quoted string of ``text``,
    each at most _CHUNK_MAX characters (cut at a space)."""
    out: list[tuple[int, int]] = []

    def add(start: int, end: int) -> None:
        while end - start > _CHUNK_MAX:
            cut = text.rfind(" ", start, start + _CHUNK_MAX)
            cut = cut if cut > start else start + _CHUNK_MAX
            out.append((start, cut))
            start = cut
        if end > start:
            out.append((start, end))

    for line in re.finditer(r"[^\n]+", text):
        ls, le = line.span()
        add(ls, le)
        last = ls
        for m in _SENTENCE_END.finditer(text, ls, le):
            add(last, m.start())
            last = m.end()
        if last != ls:
            add(last, le)
        for m in _QUOTED.finditer(text, ls, le):
            add(m.start(1), m.end(1))
    return out


def detect(
    text: str,
    ner: Callable[[str], Any],
    known: Callable[[str], list[Span]],
    *,
    whole: bool = True,
) -> list[Span]:
    """Every personal value in ``text`` as merged (start, end, kind) spans.

    Values already in the run's map (``known``), the deterministic
    patterns, NER on the whole string and NER on each line, sentence and
    quoted string (Presidio can miss a name in a long prompt that it finds
    in the same sentence alone). ``whole=False`` skips the whole-string
    pass: for a screenshot's OCR text, lines joined by newlines are no
    sentence, and NER over all of them tags runs of menu and header words as
    one person. Raises ScrubRefused when ``ner`` returns no text.
    """
    found: list[tuple[int, int, str, int]] = []
    for s, e, k in known(text):
        found.append((s, e, k, 0))
    for s, e, k in _pattern_spans(text):
        found.append((s, e, k, 1))
    for m in _URL.finditer(text):
        # A URL in a screenshot (an address bar): its path and query can
        # carry record ids and names. Request text has its URLs cut first.
        if "<path removed>" in m.group(0) or m.group(0).endswith("<URL>"):
            continue
        origin, end, _total = _split_url(m.group(0))
        if m.start() + len(origin) < m.start() + end and m.group(0)[
            len(origin) : end
        ] not in ("", "/"):
            found.append((m.start() + len(origin), m.start() + end, "URL", 1))
    pieces = [p for p in piece_spans(text) if p != (0, len(text))]
    if whole:
        pieces.insert(0, (0, len(text)))
    elif "\n" not in text:
        pieces.insert(0, (0, len(text)))
    for ps, pe in pieces:
        piece = text[ps:pe]
        if not any(ch.isalpha() for ch in piece):
            continue
        out = ner(piece)
        if not isinstance(out, str):
            raise ScrubRefused("the text scrubber returned no text")
        dropped: list[tuple[int, int]] = []
        spans = ner_spans(piece, out, dropped)
        for ds, de in dropped:
            # NER reads a name in a sentence as an organisation as often as
            # it reads a header as one. Asked again about the run alone, it
            # names the person; a header stays an organisation or nothing.
            alone = piece[ds:de]
            again = ner(alone)
            if not isinstance(again, str):
                raise ScrubRefused("the text scrubber returned no text")
            spans += [(ds + s, ds + e, k) for s, e, k in ner_spans(alone, again)]
        for s, e, k in spans:
            # An entity never runs across a line break.
            for m in re.finditer(r"[^\n]+", piece[s:e]):
                found.append((ps + s + m.start(), ps + s + m.end(), k, 2))
    # Tokens, placeholders and cut URL paths are cut out of a span that
    # touches them; the rest of the span is still scrubbed.
    protected: list[Span] = [(*m.span(), "") for m in _PROTECTED.finditer(text)]
    for m in _BARE_URL.finditer(text):
        start, end = _bare_path(m)
        if text[start:end] not in ("", "/") and not text[start:end].startswith(
            "/<path removed>"
        ):
            found.append((start, end, "URL", 1))
        protected.append((m.start(), m.start(3), ""))
    # A URL's origin is the app's address, kept on purpose (see _cut_url).
    for m in _URL.finditer(text):
        origin = _split_url(m.group(0))[0]
        if origin and not origin.endswith("://"):
            protected.append((m.start(), m.start() + len(origin), ""))
    # Higher-ranked spans (values already in the map, then patterns, then
    # NER) are taken whole; a lower-ranked span keeps only the parts no
    # taken span covers. Every detected character stays covered, and a
    # known value keeps its own token even when NER reads it with a
    # neighbouring word.
    found.sort(key=lambda f: (f[3], -(f[1] - f[0]), f[0]))
    taken: list[Span] = []
    for s, e, k, _rank in found:
        parts = [(s, e)]
        for ts, te, _ in protected + taken:
            parts = [
                piece
                for ps, pe in parts
                for piece in ((ps, min(pe, ts)), (max(ps, te), pe))
                if piece[0] < piece[1]
            ]
        for ps, pe in parts:
            while ps < pe and not text[ps].isalnum():
                ps += 1
            while pe > ps and not text[pe - 1].isalnum():
                pe -= 1
            if ps < pe:
                taken.append((ps, pe, k))
    return sorted(taken)


def apply_spans(text: str, spans: list[Span], token: Callable[[str, str], str]) -> str:
    out = []
    last = 0
    for s, e, k in spans:
        out.append(text[last:s])
        out.append(token(k, text[s:e]))
        last = e
    out.append(text[last:])
    return "".join(out)


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


def _font(size: int) -> Any:
    from PIL import ImageFont

    for name in ("Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def redact_image(
    png: bytes,
    detect_text: Callable[[str], list[Span]],
    token: Callable[[str, str], str],
    ocr_words: Optional[OcrWords] = None,
) -> bytes:
    """``png`` with each detected value painted over and its token written
    in the box. The image's OCR text (lines joined by newlines) goes through
    the same detection as request text, so a value gets the same token in
    both. OCR and detection errors propagate (the caller refuses the send).
    """
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(png)).convert("RGB")
    words = list((ocr_words or _tesseract_words)(image))
    lines: dict[Any, list[tuple[str, tuple[int, int, int, int]]]] = {}
    gaps: dict[Any, int] = {}
    for word, box, key in words:
        # A wide gap on one OCR line is a new table cell: NER reads each
        # cell on its own, as a person reads the column.
        line = lines.get((key, gaps.get(key, 0)))
        if line:
            px, _, pw, ph = line[-1][1]
            if box[0] - (px + pw) > 2 * max(ph, box[3], 8):
                gaps[key] = gaps.get(key, 0) + 1
        lines.setdefault((key, gaps.get(key, 0)), []).append((word, box))
    text = ""
    offsets: list[tuple[int, int, tuple[int, int, int, int], int]] = []
    for n, line in enumerate(lines.values()):
        if text:
            # Each table cell is a line of its own (see the gap split).
            text += "\n"
        for i, (word, box) in enumerate(line):
            if i:
                text += " "
            offsets.append((len(text), len(text) + len(word), box, n))
            text += word
    if not text.strip():
        return png
    spans = detect_text(text)
    if not spans:
        return png
    draw = ImageDraw.Draw(image)
    for s, e, kind in spans:
        label = token(kind, text[s:e])
        per_line: dict[int, list[int]] = {}
        for w_start, w_end, (x, y, w, h), n in offsets:
            if w_start < e and s < w_end:
                cover = per_line.setdefault(n, [x, y, x + w, y + h])
                cover[0], cover[1] = min(cover[0], x), min(cover[1], y)
                cover[2], cover[3] = max(cover[2], x + w), max(cover[3], y + h)
        for x0, y0, x1, y1 in per_line.values():
            height = max(y1 - y0, 8)
            font = _font(max(8, int(height * 0.9)))
            tw = draw.textlength(label, font=font)
            while tw > (x1 - x0) and font.size > 8:
                font = _font(font.size - 1)
                tw = draw.textlength(label, font=font)
            # Cover the value whole; widen the box if the token is longer.
            right = max(x1 + 3, int(x0 + tw + 6))
            draw.rectangle((x0 - 3, y0 - 3, right, y1 + 3), fill=(235, 235, 235))
            draw.text((x0, y0 - 1), label, fill=(0, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def redact_image_patterns(
    png: bytes,
    ocr_words: Optional[OcrWords] = None,
    text_scrub: Optional[Callable[[str], str]] = None,
) -> bytes:
    """:func:`redact_image` with the patterns and, given ``text_scrub``, NER;
    values become bare ``<KIND>`` placeholders."""

    def found(text: str) -> list[Span]:
        if text_scrub is None:
            return detect(text, lambda t: t, lambda t: [])
        return detect(text, text_scrub, lambda t: [])

    return redact_image(png, found, lambda k, v: f"<{k}>", ocr_words)


# -- the scrubber ------------------------------------------------------------


class Scrub:
    """Scrubbing of one run's strings and images, memoised (deterministic,
    so the provider's prompt cache still matches across turns).

    With ``pseudonyms`` (:class:`openadapt_flow.flight.pseudonym.Pseudonyms`)
    each value becomes its run token (``[PERSON_1]``); without, a bare
    ``<PERSON>`` placeholder.
    """

    def __init__(
        self,
        scrubber: Any,
        *,
        ocr_words: Optional[OcrWords] = None,
        pseudonyms: Any = None,
    ) -> None:
        from openadapt_flow.flight.pseudonym import Placeholders

        self.scrubber = scrubber
        self.ocr_words = ocr_words
        self.pseudonyms = pseudonyms if pseudonyms is not None else Placeholders()
        self._images: OrderedDict[str, bytes] = OrderedDict()
        self._texts: OrderedDict[str, str] = OrderedDict()
        self._ner: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def ner(self, piece: str) -> Any:
        from openadapt_flow.privacy import scrub_outbound_text

        with self._lock:
            hit = self._ner.get(piece)
        if hit is not None:
            return hit
        out = scrub_outbound_text(piece, self.scrubber)
        if isinstance(out, str):
            with self._lock:
                self._ner[piece] = out
                while len(self._ner) > 16384:
                    self._ner.popitem(last=False)
        return out

    def detect(self, text: str) -> list[Span]:
        return detect(text, self.ner, self.pseudonyms.known_spans)

    def detect_screen(self, text: str) -> list[Span]:
        return detect(text, self.ner, self.pseudonyms.known_spans, whole=False)

    def token(self, kind: str, value: str) -> str:
        return self.pseudonyms.token(kind, value)

    def text(self, value: str) -> str:
        if not value or not value.strip():
            return value
        key = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._texts.get(key)
        if hit is not None and not self.pseudonyms.active:
            return hit
        cut = scrub_urls(value)
        out = apply_spans(cut, self.detect(cut), self.token)
        with self._lock:
            self._texts[key] = out
            while len(self._texts) > 8192:
                self._texts.popitem(last=False)
        return out

    def image(self, raw: bytes) -> tuple[bytes, bool]:
        """(scrubbed PNG, whether it is new this run)."""
        from PIL import Image

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
        buf = io.BytesIO()
        before.convert("RGB").save(buf, format="PNG")
        out = redact_image(
            buf.getvalue(), self.detect_screen, self.token, self.ocr_words
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


#: A string at least this long, all base64, is an encoded payload.
_BLOB_MIN = 256
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")


#: A run of base64 characters long enough to be an encoded payload.
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{%d,}={0,2}" % 256)


def _base64_blob(value: str) -> Optional[bytes]:
    """The decoded bytes when ``value`` is a long base64 string, else None."""
    if len(value) < _BLOB_MIN:
        return None
    compact = "".join(value.split())
    if not _BASE64.fullmatch(compact):
        return None
    try:
        if "-" in compact or "_" in compact:
            return base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
        return base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def _is_image(data: bytes) -> bool:
    if data.startswith(_IMAGE_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _b64(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ScrubRefused("an image's base64 data could not be decoded") from None


def _mime_png(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Name PNG as the type of a ``data`` field that became a scrubbed PNG."""
    data = after.get("data")
    if (
        isinstance(data, str)
        and data != before.get("data")
        and data.startswith("iVBORw0KGgo")
    ):
        for key in ("mimeType", "mime_type", "media_type"):
            if key in after:
                after[key] = "image/png"
    return after


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
        blob = _base64_blob(value)
        if blob is None and len(value) >= _BLOB_MIN and _B64_RUN.search(value):
            return embedded(value, image_slot)
        if blob is not None:
            # A screenshot in another shape (an MCP image item's data, a raw
            # base64 PNG in a tool result or in JSON text): scrubbed as an
            # image. Any other encoded payload can't be read, so it refuses.
            if not _is_image(blob):
                raise ScrubRefused(
                    "a base64 payload that is not an image can't be scrubbed"
                )
            return base64.b64encode(image(blob)).decode("ascii")
        if image_slot and value.lower().startswith(("http://", "https://")):
            raise ScrubRefused("an image by URL can't be scrubbed before it is sent")
        out = scrub.text(value)
        if not isinstance(out, str):
            raise ScrubRefused("the text scrubber returned no text")
        return out

    def embedded(value: str, image_slot: bool) -> str:
        """``value`` with every base64 run of _BLOB_MIN or more characters
        inside it (JSON in a text block, ``<img src="data:...">``, a prefixed
        dump) scrubbed as an image in place, and the text around the runs
        scrubbed as text; a run that is not an image refuses."""
        out = []
        last = 0
        for m in _B64_RUN.finditer(value):
            blob = _base64_blob(m.group(0))
            if blob is None:
                continue  # not valid base64: plain text, scrubbed as such
            if not _is_image(blob):
                raise ScrubRefused(
                    "a base64 payload that is not an image can't be scrubbed"
                )
            head = value[last : m.start()]
            head = re.sub(
                r"data:image/[\w.+-]+;base64,$", "data:image/png;base64,", head
            )
            out.append(string(head, image_slot=False) if head else head)
            out.append(base64.b64encode(image(blob)).decode("ascii"))
            last = m.end()
        tail = value[last:]
        out.append(string(tail, image_slot=False) if tail else tail)
        return "".join(out)

    def number(value: Any) -> Any:
        """A number of five or more digits is an identifier until shown
        otherwise (a record number, a phone, a ZIP code)."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if sum(ch.isdigit() for ch in repr(value)) >= 5:
            return getattr(scrub, "token", lambda k, v: "<ID>")("ID", repr(value))
        return value

    def arguments(value: str) -> str:
        """A JSON-string argument list: scrub its leaves, keep it JSON.
        Only an object or array is parsed; anything else is text."""
        try:
            parsed = json.loads(value)
        except ValueError:
            return string(value, image_slot=False)
        if not isinstance(parsed, (dict, list)):
            return string(value, image_slot=False)
        return json.dumps(payload(parsed, None), ensure_ascii=False)

    def payload(node: Any, key: Optional[str]) -> Any:
        """Tool arguments and results: every value is data. No key and no
        block type is exempt; only a base64 image is handled as an image."""
        if isinstance(node, str):
            return string(node, image_slot=False)
        if isinstance(node, list):
            return [payload(item, key) for item in node]
        if isinstance(node, dict):
            kind = node.get("type")
            source = node.get("source")
            if kind in ("image", "document") and isinstance(source, dict):
                if kind == "image" and source.get("type") == "base64":
                    png = image(_b64(str(source.get("data", ""))))
                    return {
                        **{k: payload(v, k) for k, v in node.items() if k != "source"},
                        "source": {
                            **{k: payload(v, k) for k, v in source.items()},
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(png).decode("ascii"),
                        },
                    }
                raise ScrubRefused(f"a {kind} in a tool payload can't be scrubbed")
            if kind in ("input_image", "image_url", "input_file", "file"):
                url = node.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if not (isinstance(url, str) and url.startswith("data:image/")):
                    raise ScrubRefused(f"a {kind} by reference can't be scrubbed")
            return _mime_png(node, {k: payload(v, k) for k, v in node.items()})
        return number(node)

    def structural(node: Any, key: Optional[str], parent_type: Any = None) -> Any:
        if isinstance(node, str):
            image_slot = key == "image_url" or (
                key == "url" and parent_type in ("image_url", "input_image", "image")
            )
            return string(node, image_slot=image_slot)
        if isinstance(node, list):
            return [structural(item, key, parent_type) for item in node]
        if not isinstance(node, dict):
            return number(node)
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
        # A Chat Completions image_url object has no type of its own.
        kind = kind or ("image_url" if key == "image_url" else None)
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
        return _mime_png(node, out)

    if not isinstance(body, dict):
        return structural(body, None)
    clean: dict[str, Any] = {}
    for k, v in body.items():
        if k == "metadata":
            # Caller-defined values (a user id, a session label): data.
            clean[k] = payload(v, k)
        else:
            clean[k] = v if k in _TOP_SETTINGS else structural(v, k)
    return clean
