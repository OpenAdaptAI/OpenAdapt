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
from urllib.parse import urlparse

from openadapt_kernel.ledger import ReadBackProof

_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_TOKEN_RE = re.compile(
    r"\((?:\\.|[^\\()])*\)|\[(?:\\.|[^\\\[\]])*\]\s*TJ|\b\d+\s+Tr\b"
)
_DOM_EXTRACT_JS = """() => {
  const bindings = [];
  const labelFor = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return (l.innerText || l.textContent || '').trim();
    }
    const p = el.closest && el.closest('label');
    return p ? (p.innerText || p.textContent || '').trim() : '';
  };
  for (const el of document.querySelectorAll('input,textarea,select')) {
    const val = el.tagName === 'SELECT' && el.selectedOptions && el.selectedOptions[0]
      ? `${el.value} ${el.selectedOptions[0].text || ''}`.trim() : String(el.value || '');
    bindings.push({key: [el.name, el.id, el.getAttribute('data-testid'), labelFor(el)].filter(Boolean).join(' '), val});
  }
  for (const dt of document.querySelectorAll('dt')) {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') bindings.push({key: (dt.innerText || '').trim(), val: (dd.innerText || '').trim()});
  }
  for (const tr of document.querySelectorAll('tr')) {
    const cells = tr.querySelectorAll('th,td');
    if (cells.length >= 2) bindings.push({key: (cells[0].innerText || '').trim(), val: (cells[1].innerText || '').trim()});
  }
  for (const el of document.querySelectorAll('[data-field],[data-testid]')) {
    const k = el.getAttribute('data-field') || el.getAttribute('data-testid') || '';
    const v = (el.innerText || el.textContent || '').trim();
    if (k && v) bindings.push({key: k, val: v});
  }
  return { text: (document.body && document.body.innerText) || '', bindings };
}"""


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


def _key_aliases(key: str) -> List[str]:
    base = key.strip()
    parts = [p for p in re.split(r"[\s_-]+", base) if len(p) >= 3]
    out = [base, base.replace("_", " "), base.replace("-", " ")]
    for p in parts:
        if p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return [a for a in out if a]


def _field_bound_match(key: str, val: str, compact: str, field_bindings: Sequence[Dict[str, str]]) -> bool:
    v_re = rf"(?<!\w){re.escape(val)}(?!\w)"
    aliases = _key_aliases(key)
    key_toks = {t.lower() for t in re.findall(r"[a-z0-9]+", key) if len(t) >= 2}
    for b in field_bindings:
        bk = str(b.get("key") or "")
        bv = " ".join(str(b.get("val") or "").split())
        if not bk or not bv:
            continue
        btoks = {t.lower() for t in re.findall(r"[a-z0-9]+", bk)}
        if (key.lower() in bk.lower() or (key_toks and len(key_toks & btoks) / len(key_toks) >= 0.5)) and re.search(v_re, bv):
            return True
    for alias in aliases:
        if re.search(rf"(?<!\w){re.escape(alias)}[\s\"']{{0,3}}[:=][\s\"']{{0,3}}{re.escape(val)}(?!\w)", compact, re.IGNORECASE):
            return True
    return False


def token_boundary_check(
    expected_fields: Dict[str, str],
    text: str,
    *,
    field_bindings: Sequence[Dict[str, str]] = (),
) -> tuple[List[str], List[str]]:
    """Return (cited_values, missing_keys) with field-bound matching; bare 1-2 char values require binding."""
    compact = " ".join((text or "").split())
    cited: List[str] = []
    missing: List[str] = []
    for key, val in expected_fields.items():
        v = str(val).strip()
        if not v:
            continue
        if _field_bound_match(key, v, compact, field_bindings):
            cited.append(v)
        elif len(v) >= 3 and re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact):
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


def _origin_of(url: str) -> str:
    p = urlparse(url or "")
    return f"{p.scheme.lower()}://{p.netloc.lower()}" if p.scheme and p.netloc else ""


def _origin_matches(tab_url: str, target_origin: str) -> bool:
    tp, op = urlparse(tab_url or ""), urlparse(target_origin or "")
    if not tp.scheme or not tp.netloc or not op.scheme or not op.netloc:
        return False
    if op.port is not None:
        return tp.scheme.lower() == op.scheme.lower() and tp.netloc.lower() == op.netloc.lower()
    return tp.scheme.lower() == op.scheme.lower() and (tp.hostname or "").lower() == (op.hostname or "").lower()


def verify_cross_tab_dom(
    page: Any,
    expected_fields: Dict[str, str],
    *,
    reload_active: bool = True,
    target_origin: Optional[str] = None,
    structured_bindings: Optional[Dict[str, str]] = None,
) -> OracleVerdict:
    """Verify expected fields strictly on the mutated target tab/origin using field-bound matching."""
    if reload_active:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(120)

    active_url = str(getattr(page, "url", "") or "")
    active_origin = _origin_of(active_url)
    if target_origin and active_origin and not _origin_matches(active_url, target_origin):
        return OracleVerdict(
            verified=False,
            channel="fresh_reload",
            locator=active_url,
            missing_fields=list(expected_fields.keys()),
        )

    ctx = getattr(page, "context", None)
    if ctx is not None and active_origin:
        pages = [
            p for p in ctx.pages
            if not p.is_closed()
            and _origin_of(str(getattr(p, "url", "") or "")) == active_origin
            and (not target_origin or _origin_matches(str(getattr(p, "url", "") or ""), target_origin))
        ]
    else:
        pages = [page]

    texts: List[str] = []
    urls: List[str] = []
    bindings: List[Dict[str, str]] = [
        {"key": k, "val": str(v)} for k, v in (structured_bindings or {}).items()
    ]
    for p in pages:
        try:
            if p.is_closed():
                continue
            snap = p.evaluate(_DOM_EXTRACT_JS) or {}
            txt = str(snap.get("text") or "")
            if txt.strip():
                texts.append(txt)
                urls.append(str(p.url))
            bindings.extend(snap.get("bindings") or [])
        except Exception:
            continue

    combined = "\n".join(texts)
    cited, missing = token_boundary_check(expected_fields, combined, field_bindings=bindings)
    channel = "cross_tab" if len(urls) > 1 else "fresh_reload"
    return OracleVerdict(
        verified=(len(missing) == 0 and len(cited) > 0),
        channel=channel,
        locator=urls[0] if urls else active_url,
        cited_substrings=cited,
        missing_fields=missing,
    )
