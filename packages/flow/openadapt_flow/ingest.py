"""Multimodal document ingestion for PDF, CSV, XLSX, and OCR inputs.

Provides:
- Digital PDF extraction including CIDFont / ToUnicode streams, using ``pypdf``
  when available and a linear-time, ReDoS-free stdlib fallback parser.
- Tabular ``.csv`` and ``.xlsx`` ingestion using stdlib ``csv``, ``zipfile``,
  and ``xml.etree.ElementTree`` (with optional ``openpyxl`` support).
- OCR hook integration emitting :class:`SpanAnchor` records with confidence
  gating on critical identifiers and clinical codes.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

__all__ = [
    "CRITICAL_FIELD_KINDS",
    "DEFAULT_CRITICAL_OCR_THRESHOLD",
    "DEFAULT_GENERAL_OCR_THRESHOLD",
    "BBox",
    "IngestResult",
    "LowConfidenceError",
    "OcrHook",
    "SpanAnchor",
    "TableData",
    "extract_pdf_pages_stdlib",
    "gate_ocr_spans",
    "ingest_csv",
    "ingest_file",
    "ingest_ocr",
    "ingest_pdf",
    "ingest_xlsx",
    "is_critical_span",
]

BBox = tuple[float, float, float, float]
SourceKind = Literal["pdf", "csv", "xlsx", "ocr"]
GateMode = Literal["refuse", "flag"]

CRITICAL_FIELD_KINDS: frozenset[str] = frozenset(
    {
        "claim_id",
        "code",
        "cpt",
        "dob",
        "hcpcs",
        "icd10",
        "identifier",
        "member_id",
        "mrn",
        "npi",
        "patient_id",
        "ssn",
    }
)
DEFAULT_CRITICAL_OCR_THRESHOLD = 0.85
DEFAULT_GENERAL_OCR_THRESHOLD = 0.60

# Simple bounded linear patterns for detecting critical codes/identifiers in
# untagged OCR spans (no nested quantifiers).
_ICD10_PATTERN = re.compile(r"\b[A-TV-Z]\d{2}(?:\.[A-Z0-9]{1,4})?\b")
_CPT_OR_NPI_PATTERN = re.compile(r"\b\d{5}\b|\b\d{10}\b")
_DATE_PATTERN = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_MRN_LABEL_PATTERN = re.compile(
    r"\b(?:MRN|NPI|DOB|SSN|ICD|CPT|HCPCS|MEMBER\s*ID)\b", re.IGNORECASE
)

_MAX_PDF_DECOMPRESSED_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SpanAnchor:
    """One extracted text span with optional page, bounding box, and OCR score."""

    page: int
    bbox: Optional[BBox]
    text: str
    ocr_conf: Optional[float] = None
    source: SourceKind = "pdf"
    field_kind: Optional[str] = None
    sheet: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    low_confidence: bool = False
    gate_reason: Optional[str] = None


@dataclass(frozen=True)
class TableData:
    """Structured table extracted from CSV or XLSX."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class IngestResult:
    """Result of ingesting a document or OCR image."""

    source_kind: SourceKind
    text: str
    spans: tuple[SpanAnchor, ...]
    tables: tuple[TableData, ...] = ()
    flagged_spans: tuple[SpanAnchor, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class LowConfidenceError(ValueError):
    """Raised when OCR confidence on a critical identifier/code is below threshold."""

    def __init__(self, message: str, flagged_spans: Sequence[SpanAnchor]) -> None:
        super().__init__(message)
        self.flagged_spans = tuple(flagged_spans)


OcrHook = Callable[[bytes], Sequence[SpanAnchor | Mapping[str, Any]]]


def is_critical_span(
    span: SpanAnchor,
    *,
    critical_kinds: frozenset[str] = CRITICAL_FIELD_KINDS,
) -> bool:
    """Return True if ``span`` carries a critical identifier or clinical code."""
    if span.field_kind and span.field_kind.strip().lower() in critical_kinds:
        return True
    text = span.text.strip()
    if not text:
        return False
    return bool(
        _MRN_LABEL_PATTERN.search(text)
        or _ICD10_PATTERN.search(text)
        or _CPT_OR_NPI_PATTERN.search(text)
        or _DATE_PATTERN.search(text)
    )


def gate_ocr_spans(
    spans: Sequence[SpanAnchor],
    *,
    critical_threshold: float = DEFAULT_CRITICAL_OCR_THRESHOLD,
    general_threshold: float = DEFAULT_GENERAL_OCR_THRESHOLD,
    critical_kinds: frozenset[str] = CRITICAL_FIELD_KINDS,
    mode: GateMode = "refuse",
) -> tuple[tuple[SpanAnchor, ...], tuple[SpanAnchor, ...]]:
    """Evaluate OCR confidence on spans and refuse or flag low-confidence items.

    Critical identifiers and codes require ``ocr_conf >= critical_threshold``.
    Other OCR spans below ``general_threshold`` are flagged. In ``"refuse"``
    mode, any failing critical span raises :class:`LowConfidenceError` instead
    of passing silently.
    """
    if mode not in ("refuse", "flag"):
        raise ValueError(f"unsupported gate mode: {mode!r}")

    checked: list[SpanAnchor] = []
    flagged: list[SpanAnchor] = []
    critical_failures: list[SpanAnchor] = []

    for span in spans:
        if span.source != "ocr" and span.ocr_conf is None:
            checked.append(span)
            continue

        critical = is_critical_span(span, critical_kinds=critical_kinds)
        conf = span.ocr_conf

        if critical:
            if conf is None or conf < critical_threshold:
                reason = (
                    "missing_ocr_confidence_on_critical_span"
                    if conf is None
                    else (
                        f"critical_ocr_confidence_below_threshold "
                        f"({conf:.3f} < {critical_threshold:.3f})"
                    )
                )
                updated = replace(span, low_confidence=True, gate_reason=reason)
                checked.append(updated)
                flagged.append(updated)
                critical_failures.append(updated)
                continue
        elif conf is not None and conf < general_threshold:
            reason = (
                f"ocr_confidence_below_threshold ({conf:.3f} < {general_threshold:.3f})"
            )
            updated = replace(span, low_confidence=True, gate_reason=reason)
            checked.append(updated)
            flagged.append(updated)
            continue

        checked.append(span)

    if mode == "refuse" and critical_failures:
        sample = ", ".join(
            f"page {s.page}: {s.text!r} (conf={s.ocr_conf})"
            for s in critical_failures[:3]
        )
        raise LowConfidenceError(
            f"refused {len(critical_failures)} low-confidence critical OCR span(s): {sample}",
            critical_failures,
        )

    return tuple(checked), tuple(flagged)


def _normalize_ocr_item(item: SpanAnchor | Mapping[str, Any]) -> SpanAnchor:
    if isinstance(item, SpanAnchor):
        return item if item.source == "ocr" else replace(item, source="ocr")
    raw_bbox = item.get("bbox")
    bbox: Optional[BBox] = None
    if raw_bbox is not None and len(raw_bbox) == 4:
        bbox = (
            float(raw_bbox[0]),
            float(raw_bbox[1]),
            float(raw_bbox[2]),
            float(raw_bbox[3]),
        )
    conf_raw = item.get("ocr_conf")
    ocr_conf = float(conf_raw) if conf_raw is not None else None
    return SpanAnchor(
        page=int(item.get("page", 1)),
        bbox=bbox,
        text=str(item.get("text", "")),
        ocr_conf=ocr_conf,
        source="ocr",
        field_kind=item.get("field_kind"),
    )


def ingest_ocr(
    image_bytes: bytes,
    ocr_hook: OcrHook,
    *,
    critical_threshold: float = DEFAULT_CRITICAL_OCR_THRESHOLD,
    general_threshold: float = DEFAULT_GENERAL_OCR_THRESHOLD,
    critical_kinds: frozenset[str] = CRITICAL_FIELD_KINDS,
    gate_mode: GateMode = "refuse",
) -> IngestResult:
    """Run ``ocr_hook`` on image bytes and apply confidence gating."""
    raw_spans = ocr_hook(image_bytes)
    normalized = [_normalize_ocr_item(item) for item in raw_spans]
    checked, flagged = gate_ocr_spans(
        normalized,
        critical_threshold=critical_threshold,
        general_threshold=general_threshold,
        critical_kinds=critical_kinds,
        mode=gate_mode,
    )
    text = "\n".join(s.text for s in checked if s.text)
    return IngestResult(
        source_kind="ocr",
        text=text,
        spans=checked,
        flagged_spans=flagged,
        metadata={"span_count": len(checked), "flagged_count": len(flagged)},
    )


# ---------------------------------------------------------------------------
# Linear-time, ReDoS-free stdlib PDF extraction (including CIDFont / ToUnicode)
# ---------------------------------------------------------------------------


def _safe_flate_decompress(raw: bytes) -> Optional[bytes]:
    """Decompress a FlateDecode stream with a strict output size cap."""
    try:
        dec = zlib.decompressobj()
        out = dec.decompress(raw, _MAX_PDF_DECOMPRESSED_BYTES + 1)
        if len(out) > _MAX_PDF_DECOMPRESSED_BYTES:
            return None
        return out
    except zlib.error:
        return None


def _parse_hex_token(token: bytes) -> bytes:
    """Decode ASCII hex bytes inside ``<...>``, ignoring whitespace."""
    cleaned = bytearray()
    for b in token:
        if (48 <= b <= 57) or (65 <= b <= 70) or (97 <= b <= 102):
            cleaned.append(b)
    if len(cleaned) % 2 == 1:
        cleaned.append(ord("0"))
    try:
        return bytes.fromhex(cleaned.decode("ascii"))
    except ValueError:
        return b""


def _decode_utf16_hex(hex_bytes: bytes) -> str:
    raw = _parse_hex_token(hex_bytes)
    if not raw:
        return ""
    if len(raw) % 2 == 0:
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


def _extract_hex_groups(line: bytes) -> list[bytes]:
    """Linearly extract all ``<...>`` tokens from one CMap line."""
    groups: list[bytes] = []
    idx = 0
    n = len(line)
    while idx < n:
        start = line.find(b"<", idx)
        if start == -1:
            break
        end = line.find(b">", start + 1)
        if end == -1:
            break
        groups.append(line[start + 1 : end])
        idx = end + 1
    return groups


def _parse_tounicode_cmap(stream: bytes) -> dict[bytes, str]:
    """Parse PDF ``ToUnicode`` CMap ``bfchar`` and ``bfrange`` sections linearly."""
    cmap: dict[bytes, str] = {}
    mode: Optional[str] = None
    for raw_line in stream.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if b"beginbfchar" in line:
            mode = "bfchar"
            continue
        if b"endbfchar" in line:
            mode = None
            continue
        if b"beginbfrange" in line:
            mode = "bfrange"
            continue
        if b"endbfrange" in line:
            mode = None
            continue

        if mode == "bfchar":
            groups = _extract_hex_groups(line)
            if len(groups) >= 2:
                src = _parse_hex_token(groups[0])
                dst = _decode_utf16_hex(groups[1])
                if src and dst:
                    cmap[src] = dst
        elif mode == "bfrange":
            groups = _extract_hex_groups(line)
            if len(groups) >= 3:
                start_bytes = _parse_hex_token(groups[0])
                end_bytes = _parse_hex_token(groups[1])
                if not start_bytes or len(start_bytes) != len(end_bytes):
                    continue
                start_int = int.from_bytes(start_bytes, "big")
                end_int = int.from_bytes(end_bytes, "big")
                width = len(start_bytes)
                span = min(end_int - start_int + 1, 65536)
                if b"[" in line and len(groups) >= 2 + span:
                    for offset in range(span):
                        cid = (start_int + offset).to_bytes(width, "big")
                        cmap[cid] = _decode_utf16_hex(groups[2 + offset])
                else:
                    dst_base = _parse_hex_token(groups[2])
                    if not dst_base:
                        continue
                    dst_int = int.from_bytes(dst_base, "big")
                    dst_width = len(dst_base)
                    for offset in range(max(0, span)):
                        cid = (start_int + offset).to_bytes(width, "big")
                        val_bytes = (dst_int + offset).to_bytes(dst_width, "big")
                        try:
                            cmap[cid] = val_bytes.decode("utf-16-be")
                        except UnicodeDecodeError:
                            cmap[cid] = val_bytes.decode("latin-1", errors="replace")
    return cmap


def _decode_pdf_hex_string(
    hex_body: bytes, cmap: Mapping[bytes, str], cid_widths: tuple[int, ...]
) -> str:
    raw = _parse_hex_token(hex_body)
    if not raw:
        return ""
    if cmap:
        out: list[str] = []
        idx = 0
        n = len(raw)
        while idx < n:
            matched = False
            for w in cid_widths:
                if idx + w <= n:
                    chunk = raw[idx : idx + w]
                    mapped = cmap.get(chunk)
                    if mapped is not None:
                        out.append(mapped)
                        idx += w
                        matched = True
                        break
            if not matched:
                out.append(raw[idx : idx + 1].decode("latin-1", errors="replace"))
                idx += 1
        return "".join(out)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def _read_literal_string(buf: bytes, start: int) -> tuple[str, int]:
    """Read a PDF literal string starting at ``(`` in linear time."""
    n = len(buf)
    idx = start + 1
    depth = 1
    out = bytearray()
    while idx < n and depth > 0:
        b = buf[idx]
        if b == 92:  # backslash
            idx += 1
            if idx >= n:
                break
            esc = buf[idx]
            if esc == ord("n"):
                out.append(10)
            elif esc == ord("r"):
                out.append(13)
            elif esc == ord("t"):
                out.append(9)
            elif esc == ord("b"):
                out.append(8)
            elif esc == ord("f"):
                out.append(12)
            elif esc in (ord("("), ord(")"), 92):
                out.append(esc)
            elif 48 <= esc <= 55:
                oct_digits = bytes([esc])
                for _ in range(2):
                    if idx + 1 < n and 48 <= buf[idx + 1] <= 55:
                        idx += 1
                        oct_digits += bytes([buf[idx]])
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
            else:
                out.append(esc)
            idx += 1
            continue
        if b == ord("("):
            depth += 1
            out.append(b)
        elif b == ord(")"):
            depth -= 1
            if depth > 0:
                out.append(b)
        else:
            out.append(b)
        idx += 1
    return out.decode("latin-1", errors="replace"), idx


def _extract_text_from_content_stream(content: bytes, cmap: Mapping[bytes, str]) -> str:
    """Linear-time parser for PDF text operators (``Tj``, ``TJ``, ``'``, ``"``)."""
    cid_widths = tuple(sorted({len(k) for k in cmap}, reverse=True)) if cmap else ()
    n = len(content)
    idx = 0
    in_text = False
    operands: list[Any] = []
    chunks: list[str] = []

    while idx < n:
        b = content[idx]
        # Skip whitespace
        if b in (9, 10, 12, 13, 32):
            idx += 1
            continue
        # Skip comments
        if b == ord("%"):
            nl = content.find(b"\n", idx)
            idx = n if nl == -1 else nl + 1
            continue
        # Literal string
        if b == ord("("):
            s, idx = _read_literal_string(content, idx)
            operands.append(("str", s))
            continue
        # Hex string (not dict <<...>>)
        if b == ord("<"):
            if idx + 1 < n and content[idx + 1] == ord("<"):
                idx += 2
                continue
            end = content.find(b">", idx + 1)
            if end == -1:
                break
            s = _decode_pdf_hex_string(content[idx + 1 : end], cmap, cid_widths)
            operands.append(("str", s))
            idx = end + 1
            continue
        if b == ord(">") and idx + 1 < n and content[idx + 1] == ord(">"):
            idx += 2
            continue
        # Array [...] for TJ
        if b == ord("["):
            idx += 1
            arr: list[tuple[str, Any]] = []
            while idx < n and content[idx] != ord("]"):
                cb = content[idx]
                if cb in (9, 10, 12, 13, 32):
                    idx += 1
                elif cb == ord("("):
                    s, idx = _read_literal_string(content, idx)
                    arr.append(("str", s))
                elif cb == ord("<"):
                    end = content.find(b">", idx + 1)
                    if end == -1:
                        idx = n
                        break
                    s = _decode_pdf_hex_string(content[idx + 1 : end], cmap, cid_widths)
                    arr.append(("str", s))
                    idx = end + 1
                else:
                    start = idx
                    while idx < n and content[idx] not in (
                        9,
                        10,
                        12,
                        13,
                        32,
                        ord("]"),
                        ord("("),
                        ord("<"),
                    ):
                        idx += 1
                    tok = content[start:idx]
                    try:
                        arr.append(("num", float(tok)))
                    except ValueError:
                        pass
            if idx < n and content[idx] == ord("]"):
                idx += 1
            operands.append(("arr", arr))
            continue
        # Name /...
        if b == ord("/"):
            idx += 1
            while idx < n and content[idx] not in (
                9,
                10,
                12,
                13,
                32,
                ord("/"),
                ord("["),
                ord("]"),
                ord("("),
                ord("<"),
                ord(">"),
            ):
                idx += 1
            continue

        # Token: number or operator
        start = idx
        while idx < n and content[idx] not in (
            9,
            10,
            12,
            13,
            32,
            ord("/"),
            ord("["),
            ord("]"),
            ord("("),
            ord("<"),
            ord(">"),
            ord("%"),
        ):
            idx += 1
        tok = content[start:idx]
        if tok == b"BT":
            in_text = True
            operands.clear()
        elif tok == b"ET":
            in_text = False
            if chunks and not chunks[-1].endswith("\n"):
                chunks.append("\n")
            operands.clear()
        elif in_text and tok in (b"Tj", b"'", b'"'):
            if tok in (b"'", b'"') and chunks and not chunks[-1].endswith("\n"):
                chunks.append("\n")
            if operands and operands[-1][0] == "str":
                chunks.append(operands[-1][1])
            operands.clear()
        elif in_text and tok == b"TJ":
            if operands and operands[-1][0] == "arr":
                for kind, val in operands[-1][1]:
                    if kind == "str":
                        chunks.append(val)
                    elif kind == "num" and val <= -120.0:
                        chunks.append(" ")
            operands.clear()
        elif in_text and tok in (b"T*", b"Td", b"TD", b"Tm"):
            if chunks and not chunks[-1].endswith("\n"):
                chunks.append("\n")
            operands.clear()
        elif tok and ((48 <= tok[0] <= 57) or tok[0] in (ord("+"), ord("-"), ord("."))):
            operands.append(("num", tok))
        else:
            operands.clear()

    return "".join(chunks).strip()


def _iter_pdf_streams(data: bytes) -> list[tuple[bytes, bytes]]:
    """Linearly scan PDF ``stream`` ... ``endstream`` blocks with preceding headers."""
    streams: list[tuple[bytes, bytes]] = []
    pos = 0
    n = len(data)
    while pos < n:
        s_idx = data.find(b"stream", pos)
        if s_idx == -1:
            break
        # Verify "stream" is followed by \r\n or \n (per PDF spec).
        after = s_idx + 6
        if after < n and data[after : after + 2] == b"\r\n":
            body_start = after + 2
        elif after < n and data[after : after + 1] == b"\n":
            body_start = after + 1
        else:
            pos = after
            continue
        end_idx = data.find(b"endstream", body_start)
        if end_idx == -1:
            break
        header_start = max(0, data.rfind(b"<<", pos, s_idx))
        header = data[header_start:s_idx]
        raw_body = data[body_start:end_idx]
        if raw_body.endswith(b"\r\n"):
            raw_body = raw_body[:-2]
        elif raw_body.endswith(b"\n") or raw_body.endswith(b"\r"):
            raw_body = raw_body[:-1]
        streams.append((header, raw_body))
        pos = end_idx + 9
    return streams


def extract_pdf_pages_stdlib(data: bytes) -> list[str]:
    """Extract text pages from a PDF using a linear-time stdlib parser.

    Supports uncompressed and ``/FlateDecode`` streams, as well as CIDFont
    ``ToUnicode`` CMaps (``bfchar`` / ``bfrange``). Guaranteed linear scan with
    no backtracking regular expressions.
    """
    decoded_streams: list[bytes] = []
    combined_cmap: dict[bytes, str] = {}

    for header, raw_body in _iter_pdf_streams(data):
        if b"/FlateDecode" in header:
            body = _safe_flate_decompress(raw_body)
            if body is None:
                continue
        else:
            body = raw_body
        if b"beginbfchar" in body or b"beginbfrange" in body:
            combined_cmap.update(_parse_tounicode_cmap(body))
        else:
            decoded_streams.append(body)

    pages: list[str] = []
    for stream in decoded_streams:
        if b"BT" not in stream:
            continue
        text = _extract_text_from_content_stream(stream, combined_cmap)
        if text:
            pages.append(text)
    return pages


def ingest_pdf(
    source: str | Path | bytes,
    *,
    use_pypdf: bool = True,
    ocr_hook: Optional[OcrHook] = None,
    critical_threshold: float = DEFAULT_CRITICAL_OCR_THRESHOLD,
    general_threshold: float = DEFAULT_GENERAL_OCR_THRESHOLD,
    gate_mode: GateMode = "refuse",
) -> IngestResult:
    """Ingest a digital or scanned PDF into :class:`IngestResult`."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    pages: list[str] = []
    backend = "stdlib"

    if use_pypdf:
        try:
            import pypdf  # type: ignore[import-not-found]

            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
            if any(pages):
                backend = "pypdf"
            else:
                pages = []
        except ImportError:
            pages = []

    if not pages:
        pages = extract_pdf_pages_stdlib(data)
        backend = "stdlib"

    if not any(pages) and ocr_hook is not None:
        return ingest_ocr(
            data,
            ocr_hook,
            critical_threshold=critical_threshold,
            general_threshold=general_threshold,
            gate_mode=gate_mode,
        )

    spans = tuple(
        SpanAnchor(page=idx, bbox=None, text=page_text, ocr_conf=None, source="pdf")
        for idx, page_text in enumerate(pages, start=1)
        if page_text
    )
    return IngestResult(
        source_kind="pdf",
        text="\n\n".join(s.text for s in spans),
        spans=spans,
        metadata={"backend": backend, "page_count": len(pages)},
    )


# ---------------------------------------------------------------------------
# CSV and XLSX ingestion
# ---------------------------------------------------------------------------


def ingest_csv(
    source: str | Path | bytes,
    *,
    encoding: str = "utf-8-sig",
    delimiter: str = ",",
    table_name: str = "sheet1",
) -> IngestResult:
    """Ingest a CSV file or bytes into structured table data and spans."""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    text_io = io.StringIO(raw.decode(encoding))
    reader = csv.reader(text_io, delimiter=delimiter)
    all_rows = [tuple(cell for cell in row) for row in reader]

    headers: tuple[str, ...] = all_rows[0] if all_rows else ()
    body_rows: tuple[tuple[str, ...], ...] = (
        tuple(all_rows[1:]) if len(all_rows) > 1 else ()
    )
    table = TableData(name=table_name, headers=headers, rows=body_rows)

    spans: list[SpanAnchor] = []
    lines: list[str] = []
    for r_idx, row in enumerate(all_rows, start=1):
        lines.append("\t".join(row))
        for c_idx, cell in enumerate(row, start=1):
            if cell:
                spans.append(
                    SpanAnchor(
                        page=1,
                        bbox=None,
                        text=cell,
                        ocr_conf=None,
                        source="csv",
                        sheet=table_name,
                        row=r_idx,
                        col=c_idx,
                    )
                )

    return IngestResult(
        source_kind="csv",
        text="\n".join(lines),
        spans=tuple(spans),
        tables=(table,),
        metadata={"row_count": len(body_rows), "column_count": len(headers)},
    )


def _col_ref_to_index(cell_ref: str) -> int:
    """Convert an Excel cell reference like ``C12`` into a 0-based column index."""
    col = 0
    for ch in cell_ref:
        if "A" <= ch <= "Z":
            col = col * 26 + (ord(ch) - ord("A") + 1)
        elif "a" <= ch <= "z":
            col = col * 26 + (ord(ch) - ord("a") + 1)
        else:
            break
    return max(0, col - 1)


def _read_xlsx_stdlib(data: bytes) -> list[TableData]:
    """Read worksheets from an ``.xlsx`` archive using stdlib zipfile + ElementTree."""
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

    tables: list[TableData] = []
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        names = set(zf.namelist())
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{ns_main}si"):
                parts = [elem.text or "" for elem in si.iter(f"{ns_main}t")]
                shared_strings.append("".join(parts))

        # Resolve sheet names and target paths from workbook.xml + rels.
        sheet_targets: list[tuple[str, str]] = []
        rel_map: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels_root:
                rid = rel.attrib.get("Id")
                target = rel.attrib.get("Target")
                if rid and target:
                    normalized = target.lstrip("/")
                    if not normalized.startswith("xl/"):
                        normalized = f"xl/{normalized}"
                    rel_map[rid] = normalized

        if "xl/workbook.xml" in names:
            wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
            for sheet_el in wb_root.iter(f"{ns_main}sheet"):
                s_name = sheet_el.attrib.get("name", "Sheet1")
                rid = sheet_el.attrib.get(f"{ns_rel}id", "")
                target = rel_map.get(rid, "")
                if target in names:
                    sheet_targets.append((s_name, target))

        if not sheet_targets:
            for member in sorted(names):
                if member.startswith("xl/worksheets/sheet") and member.endswith(".xml"):
                    sheet_targets.append((Path(member).stem, member))

        for sheet_name, target in sheet_targets:
            root = ET.fromstring(zf.read(target))
            rows: list[tuple[str, ...]] = []
            for row_el in root.iter(f"{ns_main}row"):
                row_cells: list[str] = []
                for c_el in row_el.findall(f"{ns_main}c"):
                    ref = c_el.attrib.get("r", "")
                    col_idx = _col_ref_to_index(ref) if ref else len(row_cells)
                    while len(row_cells) < col_idx:
                        row_cells.append("")

                    c_type = c_el.attrib.get("t", "")
                    val = ""
                    if c_type == "inlineStr":
                        val = "".join(t.text or "" for t in c_el.iter(f"{ns_main}t"))
                    else:
                        v_el = c_el.find(f"{ns_main}v")
                        raw_v = v_el.text if v_el is not None and v_el.text else ""
                        if c_type == "s" and raw_v.isdigit():
                            idx = int(raw_v)
                            val = (
                                shared_strings[idx]
                                if 0 <= idx < len(shared_strings)
                                else ""
                            )
                        elif c_type == "b":
                            val = "TRUE" if raw_v == "1" else "FALSE"
                        else:
                            val = raw_v
                    row_cells.append(val)
                rows.append(tuple(row_cells))

            headers = rows[0] if rows else ()
            body = tuple(rows[1:]) if len(rows) > 1 else ()
            tables.append(TableData(name=sheet_name, headers=headers, rows=body))

    return tables


def ingest_xlsx(
    source: str | Path | bytes,
    *,
    use_openpyxl: bool = True,
) -> IngestResult:
    """Ingest an ``.xlsx`` workbook into structured table data and spans."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    tables: list[TableData] = []
    backend = "stdlib"

    if use_openpyxl:
        try:
            import openpyxl  # type: ignore[import-not-found, import-untyped]

            wb = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
            try:
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    rows = [
                        tuple("" if cell is None else str(cell) for cell in row)
                        for row in ws.iter_rows(values_only=True)
                    ]
                    headers = rows[0] if rows else ()
                    body = tuple(rows[1:]) if len(rows) > 1 else ()
                    tables.append(
                        TableData(name=sheet_name, headers=headers, rows=body)
                    )
                backend = "openpyxl"
            finally:
                wb.close()
        except ImportError:
            tables = []

    if not tables:
        tables = _read_xlsx_stdlib(data)
        backend = "stdlib"

    spans: list[SpanAnchor] = []
    lines: list[str] = []
    for page_idx, table in enumerate(tables, start=1):
        all_rows = ((table.headers,) if table.headers else ()) + table.rows
        for r_idx, row in enumerate(all_rows, start=1):
            lines.append("\t".join(row))
            for c_idx, cell in enumerate(row, start=1):
                if cell:
                    spans.append(
                        SpanAnchor(
                            page=page_idx,
                            bbox=None,
                            text=cell,
                            ocr_conf=None,
                            source="xlsx",
                            sheet=table.name,
                            row=r_idx,
                            col=c_idx,
                        )
                    )

    return IngestResult(
        source_kind="xlsx",
        text="\n".join(lines),
        spans=tuple(spans),
        tables=tuple(tables),
        metadata={"backend": backend, "sheet_count": len(tables)},
    )


def ingest_file(
    path: str | Path,
    *,
    ocr_hook: Optional[OcrHook] = None,
    critical_threshold: float = DEFAULT_CRITICAL_OCR_THRESHOLD,
    general_threshold: float = DEFAULT_GENERAL_OCR_THRESHOLD,
    gate_mode: GateMode = "refuse",
) -> IngestResult:
    """Dispatch ingestion by file extension (``.pdf``, ``.csv``, ``.xlsx``, images)."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return ingest_pdf(
            file_path,
            ocr_hook=ocr_hook,
            critical_threshold=critical_threshold,
            general_threshold=general_threshold,
            gate_mode=gate_mode,
        )
    if suffix == ".csv":
        return ingest_csv(file_path)
    if suffix == ".xlsx":
        return ingest_xlsx(file_path)
    if suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
        if ocr_hook is None:
            raise ValueError(
                f"ocr_hook is required to ingest image file {file_path.name}"
            )
        return ingest_ocr(
            file_path.read_bytes(),
            ocr_hook,
            critical_threshold=critical_threshold,
            general_threshold=general_threshold,
            gate_mode=gate_mode,
        )
    raise ValueError(f"unsupported file extension for ingestion: {suffix!r}")
