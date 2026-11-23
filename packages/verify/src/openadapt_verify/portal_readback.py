"""Third-party portal independent read-back oracles (no DB/API access required).

Provides two fail-closed verification oracles for third-party web portals (such as
insurance carrier quoting portals or payer portals) where direct SQL or backend
API read-back is unavailable:

1. ``FreshReloadSummaryOracle``:
   Requires cryptographic/session proof of a fresh navigation or full page reload
   distinct from the submit DOM, then diffs extracted summary fields against the
   expected intake record. Stale in-memory DOM without reload proof -> ``HALT``.
   Missing required field -> ``ABSTAIN`` (overall verdict ``HALT``).
   Mismatched field -> ``REFUTED`` (overall verdict ``HALT``).

2. ``ConfirmationPdfOracle``:
   Extracts persisted field values from a downloaded quote/submission confirmation
   PDF using Python stdlib only (supports uncompressed and ``FlateDecode`` PDF
   text streams), normalizes currency/dates/booleans, and diffs every agreed field
   against the expected record with per-field provenance and SHA-256 digest.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import zlib
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

FieldStatus = Literal["CONFIRMED", "REFUTED", "ABSTAIN"]
Verdict = Literal["VERIFIED", "HALT"]
NavigationType = Literal["reload", "fresh_navigation", "new_session", "same_dom"]

_VALID_RELOAD_NAV_TYPES = frozenset({"reload", "fresh_navigation", "new_session"})

_CURRENCY_RE = re.compile(
    r"^\s*([$€£])?\s*([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)\s*$"
)
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
)
_BOOL_TRUE = frozenset({"true", "yes", "y", "checked", "on"})
_BOOL_FALSE = frozenset({"false", "no", "n", "unchecked", "off"})


@dataclass(frozen=True)
class ReloadProof:
    """Evidence proving the summary page was freshly loaded after commit."""

    navigation_type: str
    submit_url: str
    summary_url: str
    submit_dom_token: str
    summary_dom_token: str
    reloaded_at_utc: str = ""

    def validate(self) -> tuple[bool, str]:
        if self.navigation_type not in _VALID_RELOAD_NAV_TYPES:
            return (
                False,
                f"stale_in_memory_dom_without_reload_proof: navigation_type={self.navigation_type!r}",
            )
        if not self.submit_dom_token.strip() or not self.summary_dom_token.strip():
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: missing submit/summary DOM tokens",
            )
        if self.submit_dom_token.strip() == self.summary_dom_token.strip():
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: summary_dom_token matches submit_dom_token",
            )
        return True, "fresh_reload_confirmed"


@dataclass(frozen=True)
class FieldCheck:
    field: str
    expected: Any
    observed: Any
    normalized_expected: str
    normalized_observed: str | None
    status: FieldStatus
    provenance: str


@dataclass(frozen=True)
class PortalReadbackResult:
    verdict: Verdict
    channel: str
    reason: str
    checks: tuple[FieldCheck, ...]
    confirmed_count: int
    refuted_count: int
    abstained_count: int
    artifact_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "checks": [asdict(c) for c in self.checks],
        }


def normalize_field_value(value: Any, *, field_type: str | None = None) -> str:
    """Normalize currency, dates, booleans, and text without blurring identifier look-alikes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        dec = Decimal(str(value))
        return (
            f"{dec.quantize(Decimal('0.01'))}"
            if field_type == "currency"
            else str(dec.normalize())
        )

    text = " ".join(str(value).strip().split())
    low = text.lower()

    if field_type == "boolean" or low in _BOOL_TRUE or low in _BOOL_FALSE:
        if low in _BOOL_TRUE:
            return "true"
        if low in _BOOL_FALSE:
            return "false"

    if field_type == "date" or re.search(
        r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/-]\d{1,2}[/-]\d{4}$|^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}$",
        text,
    ):
        for fmt in _DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(text, fmt).date()
                return parsed.isoformat()
            except ValueError:
                continue

    m_cur = _CURRENCY_RE.match(text)
    if m_cur and (
        field_type == "currency" or m_cur.group(1) is not None or "," in m_cur.group(2)
    ):
        raw_num = m_cur.group(2).replace(",", "")
        try:
            dec = Decimal(raw_num)
            return f"{dec.quantize(Decimal('0.01'))}"
        except InvalidOperation:
            pass

    if field_type == "number":
        try:
            return str(Decimal(text.replace(",", "")).normalize())
        except InvalidOperation:
            pass

    return text


def _compare_fields(
    expected_fields: Mapping[str, Any],
    observed_fields: Mapping[str, Any],
    *,
    field_types: Mapping[str, str] | None = None,
    provenance_map: Mapping[str, str] | None = None,
    default_provenance: str = "",
) -> tuple[tuple[FieldCheck, ...], int, int, int]:
    ftypes = field_types or {}
    pmap = provenance_map or {}
    checks: list[FieldCheck] = []
    confirmed = 0
    refuted = 0
    abstained = 0

    for fname, exp_val in expected_fields.items():
        ftype = ftypes.get(fname)
        norm_exp = normalize_field_value(exp_val, field_type=ftype)
        obs_val = observed_fields.get(fname)
        prov = pmap.get(fname, default_provenance)

        if obs_val is None or (isinstance(obs_val, str) and not obs_val.strip()):
            abstained += 1
            checks.append(
                FieldCheck(
                    field=fname,
                    expected=exp_val,
                    observed=obs_val,
                    normalized_expected=norm_exp,
                    normalized_observed=None,
                    status="ABSTAIN",
                    provenance=prov or "missing_in_readback",
                )
            )
            continue

        # Infer currency comparison when either side carries a currency symbol
        effective_type = ftype
        if effective_type is None and (
            (isinstance(exp_val, str) and any(c in exp_val for c in "$€£"))
            or (isinstance(obs_val, str) and any(c in obs_val for c in "$€£"))
        ):
            effective_type = "currency"
            norm_exp = normalize_field_value(exp_val, field_type="currency")

        norm_obs = normalize_field_value(obs_val, field_type=effective_type)
        if norm_obs == norm_exp:
            confirmed += 1
            status: FieldStatus = "CONFIRMED"
        else:
            refuted += 1
            status = "REFUTED"
        checks.append(
            FieldCheck(
                field=fname,
                expected=exp_val,
                observed=obs_val,
                normalized_expected=norm_exp,
                normalized_observed=norm_obs,
                status=status,
                provenance=prov,
            )
        )

    return tuple(checks), confirmed, refuted, abstained


class FreshReloadSummaryOracle:
    """Verify persisted portal summary fields after an explicit fresh reload/navigation."""

    def verify(
        self,
        expected_fields: Mapping[str, Any],
        observed_summary_fields: Mapping[str, Any],
        reload_proof: ReloadProof,
        *,
        field_types: Mapping[str, str] | None = None,
    ) -> PortalReadbackResult:
        if not expected_fields:
            return PortalReadbackResult(
                verdict="HALT",
                channel="fresh_reload_summary",
                reason="empty_expected_fields",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=0,
            )

        ok_reload, reload_reason = reload_proof.validate()
        if not ok_reload:
            return PortalReadbackResult(
                verdict="HALT",
                channel="fresh_reload_summary",
                reason=reload_reason,
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
            )

        prov = f"fresh_reload:{reload_proof.navigation_type}:{reload_proof.summary_url}"
        checks, confirmed, refuted, abstained = _compare_fields(
            expected_fields,
            observed_summary_fields,
            field_types=field_types,
            default_provenance=prov,
        )

        if refuted > 0:
            return PortalReadbackResult(
                verdict="HALT",
                channel="fresh_reload_summary",
                reason="field_mismatch",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
            )
        if abstained > 0:
            return PortalReadbackResult(
                verdict="HALT",
                channel="fresh_reload_summary",
                reason="missing_required_fields",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
            )
        return PortalReadbackResult(
            verdict="VERIFIED",
            channel="fresh_reload_summary",
            reason="ok",
            checks=checks,
            confirmed_count=confirmed,
            refuted_count=0,
            abstained_count=0,
        )


def _unescape_pdf_literal(raw: str) -> str:
    """Decode PDF literal string escapes (\\(, \\), \\\\, \\n, \\r, \\t, octal)."""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in ("(", ")", "\\"):
                out.append(nxt)
                i += 2
            elif nxt == "n":
                out.append("\n")
                i += 2
            elif nxt == "r":
                out.append("\r")
                i += 2
            elif nxt == "t":
                out.append("\t")
                i += 2
            elif nxt in "01234567":
                oct_digits = raw[i + 1 : i + 4]
                m = re.match(r"^[0-7]{1,3}", oct_digits)
                if m:
                    out.append(chr(int(m.group(0), 8)))
                    i += 1 + len(m.group(0))
                else:
                    out.append(nxt)
                    i += 2
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _extract_text_from_content_stream(stream_bytes: bytes) -> list[str]:
    """Extract text runs from PDF Tj / TJ operators in a decoded content stream."""
    text = stream_bytes.decode("latin-1", errors="ignore")
    lines: list[str] = []

    # Match both single literal `(text) Tj` and array `[(a) -20 (b)] TJ`
    token_re = re.compile(
        r"\(((?:\\.|[^\\()])*)\)\s*Tj|\[((?:[^\[\]]|\((?:\\.|[^\\()])*\))*)\]\s*TJ",
        re.DOTALL,
    )
    literal_re = re.compile(r"\(((?:\\.|[^\\()])*)\)")

    for m in token_re.finditer(text):
        if m.group(1) is not None:
            val = _unescape_pdf_literal(m.group(1)).strip()
            if val:
                lines.append(val)
        elif m.group(2) is not None:
            parts = [
                _unescape_pdf_literal(lm.group(1))
                for lm in literal_re.finditer(m.group(2))
            ]
            joined = "".join(parts).strip()
            if joined:
                lines.append(joined)
    return lines


def extract_pdf_text_stdlib(pdf_bytes: bytes) -> str:
    """Extract readable text from uncompressed or FlateDecode PDF streams using stdlib only."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("missing %PDF- header")

    extracted_lines: list[str] = []
    stream_re = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
    for m in stream_re.finditer(pdf_bytes):
        raw_stream = m.group(1)
        # Check whether preceding object dictionary indicates FlateDecode
        prefix = pdf_bytes[max(0, m.start() - 256) : m.start()]
        if b"/FlateDecode" in prefix:
            try:
                decoded = zlib.decompress(raw_stream)
            except zlib.error:
                continue
            extracted_lines.extend(_extract_text_from_content_stream(decoded))
        else:
            extracted_lines.extend(_extract_text_from_content_stream(raw_stream))

    return "\n".join(extracted_lines)


def _default_labels_for_field(field_name: str) -> tuple[str, ...]:
    human = field_name.replace("_", " ").strip()
    return (human, field_name)


class ConfirmationPdfOracle:
    """Extract and verify persisted fields from a downloaded quote confirmation PDF."""

    def verify(
        self,
        pdf_path: str | Path,
        expected_fields: Mapping[str, Any],
        *,
        field_labels: Mapping[str, Sequence[str]] | None = None,
        field_patterns: Mapping[str, str] | None = None,
        field_types: Mapping[str, str] | None = None,
    ) -> PortalReadbackResult:
        if not expected_fields:
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason="empty_expected_fields",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=0,
            )

        p = Path(pdf_path)
        if not p.is_file():
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason=f"missing_pdf: {p}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
            )

        try:
            raw_bytes = p.read_bytes()
            pdf_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            pdf_text = extract_pdf_text_stdlib(raw_bytes)
        except (OSError, ValueError) as exc:
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason=f"corrupt_or_unreadable_pdf: {exc}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
            )

        if not pdf_text.strip():
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason="corrupt_or_unreadable_pdf: no extractable text streams",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
                artifact_sha256=pdf_sha256,
            )

        labels_cfg = field_labels or {}
        patterns_cfg = field_patterns or {}
        observed: dict[str, Any] = {}
        provenance: dict[str, str] = {}

        for fname in expected_fields:
            if fname in patterns_cfg:
                m = re.search(
                    patterns_cfg[fname], pdf_text, re.IGNORECASE | re.MULTILINE
                )
                if m:
                    val = m.group(1).strip() if m.lastindex else m.group(0).strip()
                    observed[fname] = val
                    provenance[fname] = f"pdf:{pdf_sha256[:12]}:regex:{fname}"
                continue

            candidate_labels = labels_cfg.get(fname) or _default_labels_for_field(fname)
            for lbl in candidate_labels:
                pat = re.compile(
                    rf"(?:^|\n)\s*{re.escape(lbl)}\s*[:\-]\s*([^\n]+)",
                    re.IGNORECASE,
                )
                m = pat.search(pdf_text)
                if m:
                    observed[fname] = m.group(1).strip()
                    provenance[fname] = f"pdf:{pdf_sha256[:12]}:label={lbl!r}"
                    break

        checks, confirmed, refuted, abstained = _compare_fields(
            expected_fields,
            observed,
            field_types=field_types,
            provenance_map=provenance,
            default_provenance=f"pdf:{pdf_sha256[:12]}:missing",
        )

        if refuted > 0:
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason="field_mismatch",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
                artifact_sha256=pdf_sha256,
            )
        if abstained > 0:
            return PortalReadbackResult(
                verdict="HALT",
                channel="confirmation_pdf",
                reason="missing_required_fields",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
                artifact_sha256=pdf_sha256,
            )
        return PortalReadbackResult(
            verdict="VERIFIED",
            channel="confirmation_pdf",
            reason="ok",
            checks=checks,
            confirmed_count=confirmed,
            refuted_count=0,
            abstained_count=0,
            artifact_sha256=pdf_sha256,
        )
