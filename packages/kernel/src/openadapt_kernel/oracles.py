"""Independent cross-rail read-back oracles (SQLite RO, PDF content stream, cross-tab DOM).

Provides verifiable post-commit read-back channels without relying on same-screen
success banners:
  - read_sqlite_ro: opens a SQLite database strictly read-only (URI mode=ro) and
    verifies expected field values against queried rows.
  - extract_pdf_visible_text / verify_pdf_readback: pure-stdlib PDF content-stream
    extractor that filters invisible text render modes (3 Tr, 7 Tr) and checks
    expected fields via token-boundary matching.
  - verify_cross_tab_dom: inspects all open pages in a Playwright browser context
    to find persisted confirmation evidence across multi-tab workflows.
"""

from __future__ import annotations

import re
import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openadapt_kernel.ledger import ReadBackProof

_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_TOKEN_RE = re.compile(r"\((?:\\.|[^\\()])*\)|\[(?:\\.|[^\[\]])*\]\s*TJ|\b\d+\s+Tr\b")


@dataclass
class OracleVerdict:
    verified: bool
    channel: str
    locator: str
    cited_substrings: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    external_ref: Optional[str] = None

    def to_proof(self) -> ReadBackProof:
        if not self.verified:
            raise ValueError("Cannot convert unverified OracleVerdict to ReadBackProof")
        return ReadBackProof(
            channel=self.channel,  # type: ignore[arg-type]
            url_or_locator=self.locator,
            cited_substrings=self.cited_substrings,
            external_ref=self.external_ref,
        )


def token_boundary_check(expected_fields: Dict[str, str], text: str) -> tuple[List[str], List[str]]:
    """Return (cited_values, missing_keys) using strict word/token boundaries."""
    compact = " ".join((text or "").split())
    cited: List[str] = []
    missing: List[str] = []
    for key, val in expected_fields.items():
        v = str(val).strip()
        if not v:
            continue
        if re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact):
            cited.append(v)
        else:
            missing.append(key)
    return cited, missing


def verify_sqlite_ro(
    db_path: Path,
    query: str,
    params: Sequence[Any],
    expected_fields: Dict[str, str],
    *,
    ref_column: Optional[str] = None,
) -> OracleVerdict:
    """Query a local SQLite database strictly in read-only mode (URI mode=ro)."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        return OracleVerdict(
            verified=False,
            channel="sql_read",
            locator=f"sqlite://{db_path.name}",
            missing_fields=list(expected_fields.keys()),
        )

    latest = rows[-1]
    row_text = " ".join(f"{k}={v}" for k, v in latest.items() if v is not None)
    cited, missing = token_boundary_check(expected_fields, row_text)
    ext_ref = str(latest[ref_column]) if ref_column and ref_column in latest and latest[ref_column] is not None else None
    return OracleVerdict(
        verified=(len(missing) == 0 and len(cited) > 0),
        channel="sql_read",
        locator=f"sqlite://{db_path.name}",
        cited_substrings=cited,
        missing_fields=missing,
        external_ref=ext_ref,
    )


def _unescape_pdf_string(raw: str) -> str:
    return (
        raw.replace(r"\(", "(")
        .replace(r"\)", ")")
        .replace(r"\\", "\\")
        .replace(r"\n", " ")
        .replace(r"\r", " ")
    )


def extract_pdf_visible_text(pdf_bytes: bytes) -> str:
    """Extract visible text from raw or FlateDecode PDF streams, filtering 3 Tr / 7 Tr invisible text."""
    chunks: List[str] = []
    streams = _STREAM_RE.findall(pdf_bytes)
    payloads: List[bytes] = list(streams) if streams else [pdf_bytes]

    for raw_stream in payloads:
        decoded_bytes = raw_stream
        try:
            decoded_bytes = zlib.decompress(raw_stream)
        except zlib.error:
            pass
        text = decoded_bytes.decode("latin-1", errors="ignore")
        render_mode = 0
        for match in _TOKEN_RE.finditer(text):
            tok = match.group(0)
            if tok.endswith("Tr"):
                try:
                    render_mode = int(tok.split()[0])
                except ValueError:
                    render_mode = 0
                continue
            # Render mode 3 (invisible) and 7 (clip-only) are excluded to block hidden-text spoofing
            if render_mode in (3, 7):
                continue
            if tok.startswith("(") and tok.endswith(")"):
                chunks.append(_unescape_pdf_string(tok[1:-1]))
            elif tok.startswith("["):
                inner = re.findall(r"\((?:\\.|[^\\()])*\)", tok)
                chunks.append("".join(_unescape_pdf_string(s[1:-1]) for s in inner))
    return " ".join(c for c in chunks if c.strip())


def verify_pdf_readback(
    pdf_path: Path,
    expected_fields: Dict[str, str],
    *,
    ref_pattern: Optional[str] = None,
) -> OracleVerdict:
    """Verify expected fields against visible text extracted from a confirmation PDF."""
    visible = extract_pdf_visible_text(pdf_path.read_bytes())
    cited, missing = token_boundary_check(expected_fields, visible)
    ext_ref: Optional[str] = None
    if ref_pattern:
        m = re.search(ref_pattern, visible)
        if m:
            ext_ref = m.group(1) if m.groups() else m.group(0)
    return OracleVerdict(
        verified=(len(missing) == 0 and len(cited) > 0),
        channel="pdf_stream",
        locator=f"pdf://{pdf_path.name}",
        cited_substrings=cited,
        missing_fields=missing,
        external_ref=ext_ref,
    )


def verify_cross_tab_dom(
    page: Any,
    expected_fields: Dict[str, str],
    *,
    reload_active: bool = True,
) -> OracleVerdict:
    """Verify expected fields across the active tab (after fresh reload) and all other open tabs."""
    if reload_active:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(200)

    texts: List[str] = []
    urls: List[str] = []
    ctx = getattr(page, "context", None)
    pages = list(ctx.pages) if ctx is not None else [page]
    for p in pages:
        try:
            if p.is_closed():
                continue
            txt = str(p.evaluate("() => (document.body && document.body.innerText) || ''") or "")
            if txt.strip():
                texts.append(txt)
                urls.append(str(p.url))
        except Exception:
            continue

    combined = "\n".join(texts)
    cited, missing = token_boundary_check(expected_fields, combined)
    channel = "cross_tab" if len(urls) > 1 else "fresh_reload"
    return OracleVerdict(
        verified=(len(missing) == 0 and len(cited) > 0),
        channel=channel,
        locator=urls[0] if urls else str(getattr(page, "url", "")),
        cited_substrings=cited,
        missing_fields=missing,
    )
