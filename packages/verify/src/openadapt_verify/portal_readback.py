"""Third-party portal independent read-back oracles (no DB/API access required).

Provides two fail-closed verification oracles for third-party web portals (such as
insurance carrier quoting portals or payer portals) where direct SQL or backend
API read-back is unavailable:

1. ``FreshReloadSummaryOracle``:
   Requires an independent producer (``separate_session`` or ``system_of_record``),
   valid same-origin ``submit_url`` / ``summary_url``, ISO-8601 ``reloaded_at_utc``,
   a 64-hex ``summary_dom_sha256`` digest, and distinct non-empty DOM tokens
   proving a fresh navigation/reload distinct from the submit DOM. It then
   reads each field from the reloaded page itself: the visible value bound to
   the field's label (or locator), compared as a whole normalized value. A
   repeated label with different values is UNCERTAIN. Inline DOM bytes with a
   declared producer cannot reach VERIFIED; a DOM file in a graded store can.

2. ``ConfirmationPdfOracle``:
   Enforces artifact provenance via
   :mod:`openadapt_flow.runtime.effects.artifact_provenance` (refusing ``unknown``,
   ``acting_session``, ``Downloads``, ``playwright-artifacts-*``, and symlinks
   escaping the store), checks PDF integrity (``%%EOF``, no incremental-update
   shadowing, no silent ``FlateDecode`` decompression errors, optional
   ``expected_fetch_sha256``), ignores invisible text (``3 Tr``), detects
   ambiguous duplicate labels with conflicting values, preserves currency codes,
   and diffs every field with per-field provenance and SHA-256 artifact digest.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from openadapt_flow.runtime.effects.artifact_provenance import (
    ArtifactProducer,
    grade_artifact,
    parse_producer,
)

FieldStatus = Literal["CONFIRMED", "REFUTED", "ABSTAIN"]
Verdict = Literal["VERIFIED", "HALT"]
ReceiptVerdict = Literal["CONFIRMED", "REFUTED", "UNCERTAIN"]
NavigationType = Literal["reload", "fresh_navigation", "new_session", "same_dom"]

_VALID_RELOAD_NAV_TYPES = frozenset({"reload", "fresh_navigation", "new_session"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_CURRENCY_SYMBOL_TO_CODE = {
    "$": "$",
    "€": "€",
    "£": "£",
}
# Groups: a sign before the symbol, the symbol, then the amount (which may carry
# its own sign). ``-$500`` and ``$-500`` both normalize to ``$:-500.00``.
_CURRENCY_RE = re.compile(
    r"^\s*([+-])?\s*([$€£])?\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*$"
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


def _valid_http_origin(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _valid_iso_utc(ts: str) -> bool:
    if not isinstance(ts, str) or not ts.strip():
        return False
    norm = ts.strip()
    if norm.endswith("Z"):
        norm = norm[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(norm)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _format_decimal_plain(dec: Decimal) -> str:
    """Format a Decimal without scientific notation (e.g. 2000 -> '2000', not '2E+3')."""
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


@dataclass(frozen=True)
class ReloadProof:
    """Evidence proving the summary page was freshly loaded in an independent session."""

    navigation_type: str
    submit_url: str
    summary_url: str
    submit_dom_token: str
    summary_dom_token: str
    reloaded_at_utc: str = ""
    summary_dom_sha256: str = ""
    producer: str | ArtifactProducer = ArtifactProducer.UNKNOWN

    def validate(self) -> tuple[bool, str]:
        try:
            parsed_producer = parse_producer(self.producer)
        except ValueError as exc:
            return False, f"unverified_producer_provenance: {exc}"
        if parsed_producer not in (
            ArtifactProducer.SEPARATE_SESSION,
            ArtifactProducer.SYSTEM_OF_RECORD,
        ):
            return (
                False,
                f"unverified_producer_provenance: producer={parsed_producer.value!r} cannot issue VERIFIED",
            )
        if self.navigation_type not in _VALID_RELOAD_NAV_TYPES:
            return (
                False,
                f"stale_in_memory_dom_without_reload_proof: navigation_type={self.navigation_type!r}",
            )
        submit_origin = _valid_http_origin(self.submit_url)
        summary_origin = _valid_http_origin(self.summary_url)
        if not submit_origin or not summary_origin:
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: submit_url and summary_url must be valid http(s) URLs",
            )
        if submit_origin != summary_origin:
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: summary_url origin does not match submit_url origin",
            )
        if not _valid_iso_utc(self.reloaded_at_utc):
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: reloaded_at_utc must be a timezone-aware ISO-8601 timestamp",
            )
        if not _SHA256_RE.match(self.summary_dom_sha256.strip()):
            return (
                False,
                "stale_in_memory_dom_without_reload_proof: summary_dom_sha256 must be a 64-hex SHA-256 digest",
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
    receipt_verdict: ReceiptVerdict = "UNCERTAIN"
    provenance_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "checks": [asdict(c) for c in self.checks],
        }


def normalize_field_value(value: Any, *, field_type: str | None = None) -> str:
    """Normalize currency, dates, booleans, and numbers without blurring look-alikes or currencies."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        dec = Decimal(str(value))
        return (
            f"{dec.quantize(Decimal('0.01'))}"
            if field_type == "currency"
            else _format_decimal_plain(dec)
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
    lead_sign = m_cur.group(1) if m_cur else None
    if (
        m_cur
        and not (lead_sign and m_cur.group(3)[0] in "+-")
        and (
            field_type == "currency"
            or m_cur.group(2) is not None
            or "," in m_cur.group(3)
        )
    ):
        raw_num = (lead_sign or "") + m_cur.group(3).replace(",", "")
        try:
            dec = Decimal(raw_num).quantize(Decimal("0.01"))
            sym = m_cur.group(2)
            if sym is not None:
                code = _CURRENCY_SYMBOL_TO_CODE.get(sym, sym)
                return f"{code}:{dec}"
            return f"{dec}"
        except InvalidOperation:
            pass

    if field_type == "number":
        try:
            return _format_decimal_plain(Decimal(text.replace(",", "")))
        except InvalidOperation:
            pass

    return text


def _strip_currency_code(norm: str) -> tuple[str | None, str]:
    m = re.match(r"^([$€£]|[A-Z]{3}):([+-]?\d+\.\d{2})$", norm)
    if m:
        return m.group(1), m.group(2)
    return None, norm


def _compare_normalized_values(norm_exp: str, norm_obs: str) -> FieldStatus:
    """Compare normalized values, returning CONFIRMED, REFUTED, or ABSTAIN.

    - Two explicit currencies MUST match currency symbol/code (``$:500.00`` != ``€:500.00``).
    - When the expected value explicitly names a currency (e.g. ``€:500.00``) and the
      observed value is bare (``500.00``), return ``ABSTAIN`` rather than blurring currency.
    - When only the observed value carries a currency symbol (e.g. expected bare ``1000``
      vs observed ``$:1000.00``), compare quantized numeric amounts.
    """
    if norm_exp == norm_obs:
        return "CONFIRMED"
    exp_code, exp_num = _strip_currency_code(norm_exp)
    obs_code, obs_num = _strip_currency_code(norm_obs)
    if exp_code is not None and obs_code is not None:
        return (
            "CONFIRMED" if (exp_code == obs_code and exp_num == obs_num) else "REFUTED"
        )
    if exp_code is not None and obs_code is None:
        try:
            if Decimal(exp_num).quantize(Decimal("0.01")) == Decimal(obs_num).quantize(
                Decimal("0.01")
            ):
                return "ABSTAIN"
        except InvalidOperation:
            pass
        return "REFUTED"
    if exp_code is None and obs_code is not None:
        try:
            if Decimal(exp_num).quantize(Decimal("0.01")) == Decimal(obs_num).quantize(
                Decimal("0.01")
            ):
                return "CONFIRMED"
        except InvalidOperation:
            pass
        return "REFUTED"
    return "REFUTED"


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

        effective_type = ftype
        if effective_type is None and (
            (isinstance(exp_val, str) and any(c in exp_val for c in "$€£"))
            or (isinstance(obs_val, str) and any(c in obs_val for c in "$€£"))
        ):
            effective_type = "currency"
            norm_exp = normalize_field_value(exp_val, field_type="currency")

        norm_obs = normalize_field_value(obs_val, field_type=effective_type)
        status = _compare_normalized_values(norm_exp, norm_obs)
        if status == "CONFIRMED":
            confirmed += 1
        elif status == "REFUTED":
            refuted += 1
        else:
            abstained += 1
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


_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Elements whose text is never rendered page text.
_NEVER_RENDERED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "head", "title"}
)
# Form controls: their text is editable state, not a persisted summary. It counts
# only when the control is the field being checked (see ``_bound_values``).
_FORM_CONTROL_TEXT_TAGS = frozenset({"textarea", "select", "option", "datalist"})
_HIDDEN_STYLE_DECLS = frozenset(
    {"display:none", "visibility:hidden", "visibility:collapse"}
)
# Input types whose ``value`` is a field value a person reads on the page.
_VALUE_INPUT_TYPES = frozenset(
    {"", "text", "number", "email", "tel", "url", "search", "date", "month", "week"}
)


# Inline elements: text inside them joins the surrounding text into one node, so
# ``$500<span>,000</span>`` reads as ``$500,000``, never as ``$500``.
_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "font",
        "i",
        "ins",
        "kbd",
        "mark",
        "nobr",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        "wbr",
    }
)


def _attrs_hide_element(tag: str, attrs: Mapping[str, str | None]) -> bool:
    """Whether inline markup alone hides this element and its subtree."""
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if tag == "input" and (attrs.get("type") or "").strip().lower() == "hidden":
        return True
    style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
    for decl in style.split(";"):
        if decl.replace("!important", "") in _HIDDEN_STYLE_DECLS:
            return True
    return False


@dataclass(frozen=True)
class _TextNode:
    """One run of visible text, and the element its block sits in.

    ``scope`` is the parent of the nearest non-inline element around the text.
    A label in one node binds the next node only when both share a scope: two
    cells of one table row, a ``dt``/``dd`` pair in one ``dl``, or two blocks in
    one container. A header row never binds the next row's cells.
    """

    text: str
    scope: int


class _VisibleDomExtractor(HTMLParser):
    """Collect what a reloaded page shows, using inline markup only.

    - ``nodes``: visible text runs in document order. Text split only by inline
      elements (``span``, ``b``, ``sup``, ``del`` ...) is one run. Comments,
      attributes, ``script``/``style``/``template``/``head``, form-control text,
      and subtrees hidden by ``hidden``, ``aria-hidden="true"`` or an inline
      ``display:none`` / ``visibility:hidden`` style are left out.
    - ``controls_by_id`` / ``controls_by_name``: values of visible, text-like
      ``input`` and ``textarea`` controls. They are read only when a check binds
      a field to that control by ``<label for>`` or by an explicit locator.
    - ``label_for``: ``(label text, control id)`` for each ``<label for=...>``.

    Limits: hiding by stylesheet rules, classes or script cannot be evaluated
    by a standard-library parser, so the bytes are expected to be the rendered
    page; a value hidden only by a stylesheet still reads as shown. Elements
    are closed only by an explicit end tag, as written; mis-nested markup can
    be read differently from a browser.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool, int]] = []
        self._next_eid = 0
        self.nodes: list[_TextNode] = []
        self.controls_by_id: dict[str, list[str]] = {}
        self.controls_by_name: dict[str, list[str]] = {}
        self.label_for: list[tuple[str, str]] = []
        self._label: tuple[str, list[str]] | None = None
        self._textarea: tuple[str, str, list[str]] | None = None
        self._buf: list[str] = []
        self._buf_scope = -1

    def _hidden(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    def _scope(self) -> int:
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx][0] not in _INLINE_TAGS:
                return self._stack[idx - 1][2] if idx > 0 else -1
        return -1

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if not text:
            return
        if self._label is not None:
            self._label[1].append(text)
        else:
            self.nodes.append(_TextNode(text, self._buf_scope))

    def _add_control(self, ctrl_id: str, name: str, value: str) -> None:
        compact = " ".join(value.split())
        if ctrl_id:
            self.controls_by_id.setdefault(ctrl_id, []).append(compact)
        if name:
            self.controls_by_name.setdefault(name, []).append(compact)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _INLINE_TAGS:
            self._flush()
        amap = {k.lower(): v for k, v in attrs}
        hidden = (
            self._hidden()
            or _attrs_hide_element(tag, amap)
            or tag in _NEVER_RENDERED_TAGS
        )
        ctrl_id = (amap.get("id") or "").strip()
        name = (amap.get("name") or "").strip()
        if tag == "input":
            itype = (amap.get("type") or "").strip().lower()
            if not hidden and itype in _VALUE_INPUT_TYPES:
                self._add_control(ctrl_id, name, amap.get("value") or "")
        if tag in _VOID_TAGS:
            return
        if tag == "label" and not hidden and (amap.get("for") or "").strip():
            self._label = ((amap.get("for") or "").strip(), [])
        if tag == "textarea" and not hidden:
            self._textarea = (ctrl_id, name, [])
        self._next_eid += 1
        self._stack.append(
            (tag, hidden or tag in _FORM_CONTROL_TEXT_TAGS, self._next_eid)
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _INLINE_TAGS:
            self._flush()
        if tag == "label" and self._label is not None:
            for_id, parts = self._label
            self.label_for.append((" ".join(" ".join(parts).split()), for_id))
            self._label = None
        if tag == "textarea" and self._textarea is not None:
            ctrl_id, name, parts = self._textarea
            self._add_control(ctrl_id, name, "".join(parts))
            self._textarea = None
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx][0] == tag:
                del self._stack[idx:]
                return
        # An end tag with no open element is ignored.

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea[2].append(data)
            return
        if self._hidden():
            return
        if not self._buf:
            self._buf_scope = self._scope()
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _normalize_dom_label(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.strip().lower()).strip()
    return cleaned


def _node_values_for_label(nodes: Sequence[_TextNode], target_label: str) -> list[str]:
    """Visible text values bound to ``target_label`` (already normalized).

    A value is bound when the label is its own text run and the value is the
    next run in the same scope (``<dt>Label</dt><dd>value</dd>``, two cells of
    one row), or when one run reads ``Label: value`` or ``Label value``. The
    whole rest of the run is the value.
    """
    label_words = target_label.split()
    prefix_re = re.compile(
        r"^\s*" + r"[\s_-]+".join(re.escape(w) for w in label_words) + r"\s+(.+)$",
        re.IGNORECASE,
    )
    bound_values: list[str] = []
    for idx, node in enumerate(nodes):
        text = node.text
        if _normalize_dom_label(text.rstrip(":")) == target_label:
            if idx + 1 < len(nodes) and nodes[idx + 1].scope == node.scope:
                bound_values.append(nodes[idx + 1].text.strip())
            continue
        if ":" in text:
            left, right = text.split(":", 1)
            if _normalize_dom_label(left) == target_label:
                bound_values.append(right.strip())
        else:
            m = prefix_re.match(text)
            if m:
                bound_values.append(m.group(1).strip())
    return bound_values


def _locator_values(dom: _VisibleDomExtractor, locator: str) -> list[str] | None:
    """Values of the visible control a locator names, or ``None`` if unsupported.

    Supported locators: ``#id``, ``id=x``, ``name=x`` and ``[name="x"]``.
    """
    loc = locator.strip()
    m = re.fullmatch(r"\[\s*(id|name)\s*=\s*['\"]?([^'\"\]]+)['\"]?\s*\]", loc)
    if m:
        kind, key = m.group(1), m.group(2).strip()
    elif loc.startswith("#") and len(loc) > 1:
        kind, key = "id", loc[1:]
    elif re.fullmatch(r"(id|name)=\S+", loc):
        kind, key = loc.split("=", 1)
    else:
        return None
    table = dom.controls_by_id if kind == "id" else dom.controls_by_name
    return list(table.get(key, []))


def _bound_values(
    dom: _VisibleDomExtractor,
    field_name: str,
    labels: Sequence[str],
    locator: str | None,
) -> tuple[list[str], str]:
    """Every visible value bound to ``field_name``, and how it was bound."""
    if locator is not None:
        located = _locator_values(dom, locator)
        if located is None:
            return [], "unsupported_locator"
        return located, f"locator={locator!r}"
    targets: list[str] = []
    for lbl in labels:
        norm = _normalize_dom_label(lbl)
        if norm and norm not in targets:
            targets.append(norm)
    values: list[str] = []
    for target in targets:
        values.extend(_node_values_for_label(dom.nodes, target))
        for label_text, for_id in dom.label_for:
            if _normalize_dom_label(label_text.rstrip(":")) == target:
                values.extend(dom.controls_by_id.get(for_id, []))
    return values, f"label={labels[0]!r}" if labels else "label=None"


class FreshReloadSummaryOracle:
    """Verify persisted portal summary fields against freshly reloaded DOM bytes.

    Each expected field is located in the reloaded page by its label (or an
    explicit locator), and only the visible value bound to it is compared, as
    a whole value after normalization. Caller-supplied observed values are not
    trusted: they are cross-checked against the page and never stand in for it.

    The producer: inline ``summary_dom_bytes`` carry nothing the oracle can
    observe about who fetched them, so a declared ``separate_session`` producer
    alone is capped at HALT/UNCERTAIN (``producer_source: declared``,
    ``check_level: same_screen``). Pass ``summary_dom_path`` (a file in the
    store a separate read-only session writes to) to have the store graded the
    same way the confirmation PDF channel grades its store; only that route can
    return VERIFIED.
    """

    def verify(
        self,
        expected_fields: Mapping[str, Any],
        observed_summary_fields: Mapping[str, Any] | None,
        reload_proof: ReloadProof,
        *,
        summary_dom_bytes: bytes | None = None,
        summary_dom_path: str | Path | None = None,
        acting_roots: Sequence[str | Path] | None = None,
        field_types: Mapping[str, str] | None = None,
        field_labels: Mapping[str, Sequence[str]] | None = None,
        field_locators: Mapping[str, str] | None = None,
    ) -> PortalReadbackResult:
        channel = "fresh_reload_summary"

        def halt(
            reason: str,
            receipt: ReceiptVerdict = "UNCERTAIN",
            **extra: Any,
        ) -> PortalReadbackResult:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict=receipt,
                channel=channel,
                reason=reason,
                checks=extra.pop("checks", ()),
                confirmed_count=extra.pop("confirmed_count", 0),
                refuted_count=extra.pop("refuted_count", 0),
                abstained_count=extra.pop("abstained_count", len(expected_fields)),
                **extra,
            )

        if not expected_fields:
            return halt("empty_expected_fields", abstained_count=0)

        ok_reload, reload_reason = reload_proof.validate()
        if not ok_reload:
            return halt(reload_reason)

        declared = parse_producer(reload_proof.producer)
        prov_evidence: dict[str, Any]
        producer_proven = False
        if summary_dom_path is not None:
            if summary_dom_bytes is not None:
                return halt(
                    "ambiguous_dom_source: pass summary_dom_path or "
                    "summary_dom_bytes, not both"
                )
            p = Path(summary_dom_path)
            if not p.is_file():
                return halt("missing_summary_dom_bytes: summary_dom_path is not a file")
            try:
                grade = grade_artifact(
                    declared, root=p.parent, acting_roots=acting_roots
                )
            except ValueError as exc:
                return halt(f"unverified_producer_provenance: {exc}")
            prov_evidence = grade.evidence()
            if not grade.independent:
                return halt(
                    f"unverified_producer_provenance: producer={grade.producer.value!r} "
                    f"(check_level={grade.check_level.label}) cannot issue VERIFIED",
                    provenance_evidence=prov_evidence,
                )
            acting_reason = grade.acting_artifact([p])
            if acting_reason:
                return halt(
                    f"unverified_producer_provenance: {acting_reason}",
                    provenance_evidence=prov_evidence,
                )
            try:
                dom_bytes = p.read_bytes()
            except OSError as exc:
                return halt(f"missing_summary_dom_bytes: {exc}")
            producer_proven = True
        else:
            if not isinstance(summary_dom_bytes, (bytes, bytearray)):
                return halt(
                    "missing_summary_dom_bytes: oracle must compute digest over "
                    "reloaded DOM bytes"
                )
            dom_bytes = bytes(summary_dom_bytes)
            prov_evidence = {
                "producer": ArtifactProducer.UNKNOWN.value,
                "declared_producer": declared.value,
                "check_level": "same_screen",
                "producer_source": "declared",
                "not_proven_because": (
                    "inline DOM bytes: nothing observed shows who fetched them"
                ),
            }
        prov_evidence["navigation_type"] = reload_proof.navigation_type
        prov_evidence["reloaded_at_utc"] = reload_proof.reloaded_at_utc

        if not dom_bytes:
            return halt("missing_summary_dom_bytes: empty DOM bytes")

        computed_dom_sha = hashlib.sha256(dom_bytes).hexdigest()
        prov_evidence["summary_dom_sha256"] = computed_dom_sha
        claimed_dom_sha = reload_proof.summary_dom_sha256.strip().lower()
        if not hmac.compare_digest(computed_dom_sha, claimed_dom_sha):
            return halt(
                "summary_dom_digest_mismatch",
                "REFUTED",
                artifact_sha256=computed_dom_sha,
            )

        try:
            dom_text = dom_bytes.decode("utf-8")
            dom = _VisibleDomExtractor()
            dom.feed(dom_text)
            dom.close()
        except (UnicodeDecodeError, ValueError, AssertionError) as exc:
            return halt(
                f"unparseable_summary_dom: {type(exc).__name__}",
                artifact_sha256=computed_dom_sha,
                provenance_evidence=prov_evidence,
            )

        ftypes = field_types or {}
        labels_cfg = field_labels or {}
        locators = field_locators or {}
        caller_observed = observed_summary_fields or {}
        observed: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        ambiguous_fields: list[str] = []
        disagreeing_fields: list[str] = []
        base = f"fresh_reload:{computed_dom_sha[:12]}"
        for fname in expected_fields:
            labels = tuple(labels_cfg.get(fname) or _default_labels_for_field(fname))
            values, how = _bound_values(dom, fname, labels, locators.get(fname))
            values = [v for v in values if v.strip()]
            if not values:
                provenance[fname] = f"{base}:not_bound:{how}"
                continue
            norms = {
                normalize_field_value(v, field_type=ftypes.get(fname)) for v in values
            }
            if len(norms) > 1:
                ambiguous_fields.append(fname)
                provenance[fname] = f"{base}:ambiguous:{how}"
                continue
            observed[fname] = values[0]
            provenance[fname] = f"{base}:{how}"
            claimed = caller_observed.get(fname)
            if claimed is not None and not (
                isinstance(claimed, str) and not claimed.strip()
            ):
                _, agree, _, _ = _compare_fields(
                    {fname: claimed}, {fname: values[0]}, field_types=ftypes
                )
                if agree != 1:
                    disagreeing_fields.append(fname)

        checks, confirmed, refuted, abstained = _compare_fields(
            expected_fields,
            observed,
            field_types=field_types,
            provenance_map=provenance,
        )
        counts: dict[str, Any] = {
            "checks": checks,
            "confirmed_count": confirmed,
            "refuted_count": refuted,
            "abstained_count": abstained,
            "artifact_sha256": computed_dom_sha,
            "provenance_evidence": prov_evidence,
        }

        if refuted > 0:
            return halt("field_mismatch", "REFUTED", **counts)
        if ambiguous_fields:
            return halt(
                "conflicting_duplicate_label_values_in_dom_bytes: "
                + ", ".join(ambiguous_fields),
                **counts,
            )
        if abstained > 0:
            return halt("missing_required_fields", **counts)
        if disagreeing_fields:
            return halt(
                "caller_observed_value_disagrees_with_dom: "
                + ", ".join(disagreeing_fields),
                **counts,
            )
        if not producer_proven:
            return halt(
                f"unverified_producer_provenance: producer={declared.value!r} was "
                "declared, not proven (inline DOM bytes); cannot issue VERIFIED",
                **counts,
            )
        return PortalReadbackResult(
            verdict="VERIFIED",
            receipt_verdict="CONFIRMED",
            channel=channel,
            reason="ok",
            **counts,
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
    """Extract visible text runs from PDF Tj / TJ operators.

    Tracks ``q``/``Q`` graphics-state save/restore for text rendering mode
    (``Tr``) and skips invisible text modes ``3 Tr`` (neither fill nor stroke)
    and ``7 Tr`` (clip-only).

    Known limitation: the stdlib parser does not evaluate fill colour (e.g.
    white-on-white), size-0 fonts, or off-page coordinates; ``ConfirmationPdfOracle``
    relies on the independent producer provenance gate to prevent untrusted
    caller-authored PDFs from reaching ``VERIFIED``.
    """
    text = stream_bytes.decode("latin-1", errors="ignore")
    lines: list[str] = []
    render_mode = 0
    state_stack: list[int] = []

    # Match `q`/`Q`, `<d> Tr`, `(text) Tj`, and `[(a) -20 (b)] TJ`.
    # Alternatives inside the TJ array are strictly disjoint (no backtracking).
    token_re = re.compile(
        r"(?<![A-Za-z0-9_/])([qQ])(?![A-Za-z0-9_])"
        r"|(?<![.\d])\b([0-7](?:\.0+)?)\s+Tr\b"
        r"|\(((?:\\.|[^\\()])*)\)\s*Tj"
        r"|\[((?:[^\[\]()]|\((?:\\.|[^\\()])*\))*)\]\s*TJ",
        re.DOTALL,
    )
    literal_re = re.compile(r"\(((?:\\.|[^\\()])*)\)")

    for m in token_re.finditer(text):
        if m.group(1) is not None:
            if m.group(1) == "q":
                state_stack.append(render_mode)
            else:
                render_mode = state_stack.pop() if state_stack else 0
            continue
        if m.group(2) is not None:
            render_mode = int(float(m.group(2)))
            continue
        if render_mode in (3, 7):
            # PDF render mode 3 (invisible) and 7 (clip-only) draw no visible glyphs.
            continue
        if m.group(3) is not None:
            val = _unescape_pdf_literal(m.group(3)).strip()
            if val:
                lines.append(val)
        elif m.group(4) is not None:
            parts = [
                _unescape_pdf_literal(lm.group(1))
                for lm in literal_re.finditer(m.group(4))
            ]
            joined = "".join(parts).strip()
            if joined:
                lines.append(joined)
    return lines


def extract_pdf_text_stdlib(pdf_bytes: bytes) -> str:
    """Extract readable text from uncompressed or FlateDecode PDF streams using stdlib only.

    Fails closed on missing ``%PDF-`` header, missing or multiple ``%%EOF``
    markers (incremental-update shadowing), or corrupt ``/FlateDecode`` streams.
    """
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("missing %PDF- header")
    eof_count = pdf_bytes.count(b"%%EOF")
    if eof_count == 0:
        raise ValueError("missing %%EOF trailer")
    if eof_count > 1:
        raise ValueError("incremental PDF updates (multiple %%EOF) are not allowed")

    extracted_lines: list[str] = []
    stream_re = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
    for m in stream_re.finditer(pdf_bytes):
        raw_stream = m.group(1)
        prefix = pdf_bytes[max(0, m.start() - 256) : m.start()]
        if b"/FlateDecode" in prefix:
            try:
                decoded = zlib.decompress(raw_stream)
            except zlib.error as exc:
                raise ValueError(f"corrupt FlateDecode stream: {exc}") from exc
            extracted_lines.extend(_extract_text_from_content_stream(decoded))
        else:
            extracted_lines.extend(_extract_text_from_content_stream(raw_stream))

    return "\n".join(extracted_lines)


def _default_labels_for_field(field_name: str) -> tuple[str, ...]:
    human = field_name.replace("_", " ").strip()
    return (human, field_name)


class ConfirmationPdfOracle:
    """Extract and verify persisted fields from an independently obtained confirmation PDF."""

    def verify(
        self,
        pdf_path: str | Path,
        expected_fields: Mapping[str, Any],
        *,
        producer: str | ArtifactProducer = ArtifactProducer.UNKNOWN,
        acting_roots: Sequence[str | Path] | None = None,
        expected_fetch_sha256: str | None = None,
        field_labels: Mapping[str, Sequence[str]] | None = None,
        field_patterns: Mapping[str, str] | None = None,
        field_types: Mapping[str, str] | None = None,
    ) -> PortalReadbackResult:
        if not expected_fields:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
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
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=f"missing_pdf: {p}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
            )

        try:
            grade = grade_artifact(
                producer,
                root=p.parent,
                acting_roots=acting_roots,
                observe_local=True,
            )
        except ValueError as exc:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=f"unverified_producer_provenance: {exc}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
            )

        prov_evidence = grade.evidence()
        if not grade.independent:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=(
                    f"unverified_producer_provenance: producer={grade.producer.value!r} "
                    f"(check_level={grade.check_level.label}) cannot issue VERIFIED"
                ),
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
                provenance_evidence=prov_evidence,
            )

        acting_reason = grade.acting_artifact([p])
        if acting_reason:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=f"unverified_producer_provenance: {acting_reason}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
                provenance_evidence=prov_evidence,
            )

        try:
            raw_bytes = p.read_bytes()
            pdf_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if expected_fetch_sha256 is not None:
                exp_hex = expected_fetch_sha256.strip().lower()
                if not _SHA256_RE.match(exp_hex) or not hmac.compare_digest(
                    pdf_sha256, exp_hex
                ):
                    return PortalReadbackResult(
                        verdict="HALT",
                        receipt_verdict="REFUTED",
                        channel="confirmation_pdf",
                        reason="fetch_digest_mismatch",
                        checks=(),
                        confirmed_count=0,
                        refuted_count=0,
                        abstained_count=len(expected_fields),
                        artifact_sha256=pdf_sha256,
                        provenance_evidence=prov_evidence,
                    )
            pdf_text = extract_pdf_text_stdlib(raw_bytes)
        except (OSError, ValueError) as exc:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=f"corrupt_or_unreadable_pdf: {exc}",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
                provenance_evidence=prov_evidence,
            )

        if not pdf_text.strip():
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason="corrupt_or_unreadable_pdf: no extractable text streams",
                checks=(),
                confirmed_count=0,
                refuted_count=0,
                abstained_count=len(expected_fields),
                artifact_sha256=pdf_sha256,
                provenance_evidence=prov_evidence,
            )

        labels_cfg = field_labels or {}
        patterns_cfg = field_patterns or {}
        ftypes = field_types or {}
        observed: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        ambiguous_fields: list[str] = []

        for fname in expected_fields:
            if fname in patterns_cfg:
                matches = list(
                    re.finditer(
                        patterns_cfg[fname], pdf_text, re.IGNORECASE | re.MULTILINE
                    )
                )
                if matches:
                    vals = [
                        (m.group(1).strip() if m.lastindex else m.group(0).strip())
                        for m in matches
                    ]
                    norms = {
                        normalize_field_value(v, field_type=ftypes.get(fname))
                        for v in vals
                    }
                    if len(norms) > 1:
                        ambiguous_fields.append(fname)
                        provenance[fname] = (
                            f"pdf:{pdf_sha256[:12]}:ambiguous_regex:{fname}"
                        )
                    else:
                        observed[fname] = vals[0]
                        provenance[fname] = f"pdf:{pdf_sha256[:12]}:regex:{fname}"
                continue

            candidate_labels = labels_cfg.get(fname) or _default_labels_for_field(fname)
            found_vals: list[str] = []
            matched_label: str | None = None
            for lbl in candidate_labels:
                pat = re.compile(
                    rf"(?:^|\n)\s*{re.escape(lbl)}\s*[:\-]\s*([^\n]+)",
                    re.IGNORECASE,
                )
                hits = [m.group(1).strip() for m in pat.finditer(pdf_text)]
                if hits:
                    found_vals.extend(hits)
                    if matched_label is None:
                        matched_label = lbl
            if found_vals:
                norms = {
                    normalize_field_value(v, field_type=ftypes.get(fname))
                    for v in found_vals
                }
                if len(norms) > 1:
                    ambiguous_fields.append(fname)
                    provenance[fname] = (
                        f"pdf:{pdf_sha256[:12]}:ambiguous_label={matched_label!r}"
                    )
                else:
                    observed[fname] = found_vals[0]
                    provenance[fname] = f"pdf:{pdf_sha256[:12]}:label={matched_label!r}"

        checks, confirmed, refuted, abstained = _compare_fields(
            expected_fields,
            observed,
            field_types=field_types,
            provenance_map=provenance,
            default_provenance=f"pdf:{pdf_sha256[:12]}:missing",
        )

        if refuted > 0:
            reason = (
                f"field_mismatch; ambiguous_duplicate_label: {', '.join(ambiguous_fields)}"
                if ambiguous_fields
                else "field_mismatch"
            )
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="REFUTED",
                channel="confirmation_pdf",
                reason=reason,
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
                artifact_sha256=pdf_sha256,
                provenance_evidence=prov_evidence,
            )
        if ambiguous_fields:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason=f"ambiguous_duplicate_label: {', '.join(ambiguous_fields)}",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
                artifact_sha256=pdf_sha256,
                provenance_evidence=prov_evidence,
            )
        if abstained > 0:
            return PortalReadbackResult(
                verdict="HALT",
                receipt_verdict="UNCERTAIN",
                channel="confirmation_pdf",
                reason="missing_required_fields",
                checks=checks,
                confirmed_count=confirmed,
                refuted_count=refuted,
                abstained_count=abstained,
                artifact_sha256=pdf_sha256,
                provenance_evidence=prov_evidence,
            )
        return PortalReadbackResult(
            verdict="VERIFIED",
            receipt_verdict="CONFIRMED",
            channel="confirmation_pdf",
            reason="ok",
            checks=checks,
            confirmed_count=confirmed,
            refuted_count=0,
            abstained_count=0,
            artifact_sha256=pdf_sha256,
            provenance_evidence=prov_evidence,
        )
