"""Turn a referral PDF, image (fax/scan), or email into plain text lines."""

from __future__ import annotations

import email
import email.policy
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

#: Upper bound on input size. Intake documents are a few pages; refuse
#: anything larger instead of parsing it.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

OcrFn = Callable[[bytes], list[str]]


@dataclass
class IntakeDocument:
    """Text extracted from one intake document, one entry per line."""

    source: str
    kind: str  # "pdf" | "image" | "email" | "text"
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# -- PDF -------------------------------------------------------------------

_STREAM_RE = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)
_TEXT_OP_RE = re.compile(
    rb"\((?P<s>(?:\\.|[^\\)])*)\)\s*(?P<op>Tj|'|\")"
    rb"|\[(?P<arr>(?:\\.|[^\]])*)\]\s*TJ"
    rb"|(?P<nl>T\*|Td|TD|Tm|ET)(?![A-Za-z])"
)
_ARR_STR_RE = re.compile(rb"\(((?:\\.|[^\\)])*)\)")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _unescape(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i : i + 1]
        if c == b"\\" and i + 1 < len(raw):
            nxt = raw[i + 1 : i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", raw[i + 1 : i + 4])
            if octal:
                out.append(int(octal.group(), 8) & 0xFF)
                i += 1 + len(octal.group())
                continue
            out += nxt
            i += 2
            continue
        out += c
        i += 1
    return out.decode("latin-1")


def _pdf_text_builtin(data: bytes) -> str:
    """Minimal text extraction for simple PDFs (literal strings, Flate streams).

    Covers PDFs whose text uses standard fonts and literal strings, which is
    what most generated referral forms produce. Scanned PDFs carry no text
    layer; route those through OCR instead. Install ``pypdf`` for broader
    coverage; :func:`read_document` uses it when present.
    """
    lines: list[str] = []
    for dict_bytes, body in _STREAM_RE.findall(data):
        if b"/FlateDecode" in dict_bytes:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        elif b"/Filter" in dict_bytes:
            continue
        current = ""
        for m in _TEXT_OP_RE.finditer(body):
            if m.group("nl"):
                if current.strip():
                    lines.append(current)
                current = ""
            elif m.group("s") is not None:
                if m.group("op") in (b"'", b'"') and current.strip():
                    lines.append(current)
                    current = ""
                current += _unescape(m.group("s"))
            else:
                current += "".join(
                    _unescape(s) for s in _ARR_STR_RE.findall(m.group("arr"))
                )
        if current.strip():
            lines.append(current)
    return "\n".join(lines)


def _pdf_text(data: bytes) -> str:
    try:
        import pypdf  # type: ignore[import-not-found]
    except ImportError:
        return _pdf_text_builtin(data)
    import io

    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# -- images (fax / scan) ------------------------------------------------------


def _default_ocr(data: bytes) -> list[str]:
    """OCR with RapidOCR (a core dependency), grouped into reading-order lines."""
    import io

    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]

    image = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
    result, _ = RapidOCR()(image)
    boxes = []
    for box, text, _score in result or []:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((sum(ys) / len(ys), min(xs), max(ys) - min(ys), text))
    boxes.sort()
    lines: list[list[tuple[float, str]]] = []
    last_y: Optional[float] = None
    for y, x, h, text in boxes:
        if last_y is None or abs(y - last_y) > max(h, 1) * 0.6:
            lines.append([])
            last_y = y
        lines[-1].append((x, text))
    return [" ".join(t for _, t in sorted(ln)) for ln in lines]


# -- email ----------------------------------------------------------------------


def _email_lines(data: bytes, ocr: OcrFn) -> list[str]:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    lines: list[str] = []
    subject = msg.get("Subject")
    if subject:
        lines.append(f"Subject: {subject}")
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        raw_payload = part.get_payload(decode=True)
        payload = raw_payload if isinstance(raw_payload, bytes) else b""
        payload = raw_payload if isinstance(raw_payload, bytes) else b""
        if ctype == "text/plain":
            charset = part.get_content_charset() or "utf-8"
            lines += _clean_lines(payload.decode(charset, errors="replace"))
        elif ctype == "application/pdf":
            lines += _clean_lines(_pdf_text(payload))
        elif ctype.startswith("image/"):
            lines += [ln.strip() for ln in ocr(payload) if ln.strip()]
    return lines


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}


def read_document(path: Path | str, *, ocr: Optional[OcrFn] = None) -> IntakeDocument:
    """Read a PDF, image, ``.eml`` email, or text file into lines.

    ``ocr`` overrides the image OCR function (tests inject a fake one).
    """
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"{p.name} is {size} bytes; intake accepts at most {MAX_DOCUMENT_BYTES}"
        )
    data = p.read_bytes()
    ocr_fn = ocr or _default_ocr
    suffix = p.suffix.lower()
    if data.startswith(b"%PDF") or suffix == ".pdf":
        return IntakeDocument(str(p), "pdf", _clean_lines(_pdf_text(data)))
    if suffix in _IMAGE_SUFFIXES:
        return IntakeDocument(
            str(p), "image", [ln.strip() for ln in ocr_fn(data) if ln.strip()]
        )
    if suffix == ".eml":
        return IntakeDocument(str(p), "email", _email_lines(data, ocr_fn))
    return IntakeDocument(
        str(p), "text", _clean_lines(data.decode("utf-8", errors="replace"))
    )
