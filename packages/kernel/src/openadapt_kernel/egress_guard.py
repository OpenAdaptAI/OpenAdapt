"""Customer-Verifiable Zero-PHI Egress Gatekeeper and SHA-256 Compliance Audit Ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from openadapt_kernel.zero_phi_repair import _FROZEN_UI_VOCAB

_EXTRA_UI_CHROME_VOCAB: FrozenSet[str] = frozenset(
    "accept account action actions active address age agree alert allow annual "
    "application approve approved area arrowdown arrowup attach attachment auto "
    "available back backspace badge billing birth blank block bold bottom box "
    "calendar card center change check checked checkbox choose city claim claims "
    "clear click client close code coinsurance column combobox complete confirm "
    "confirmation contact content continue copay copy country coverage create "
    "created credit currency current custom customer data date day days decline "
    "default delete denied department description detail details diagnosis dialog "
    "disabled display div dob document done download draft edit email empty "
    "enable enabled end enter entry error escape event expand expanded export "
    "facility false family fee female field fields file fill filter filters "
    "finish first fixed flex focus footer form format forward from full gap "
    "gender general go group header heading height help hidden high history "
    "home horizontal hospital hour hours html icon id image in inbox index info "
    "information inline input insert insurance invalid item items label labels "
    "last layout left legend length level line link list listbox load loading "
    "location lock login logout low mail main male manage margin max medium "
    "member memo menu message method middle min modal mode month months more "
    "move mrn name nav new next no none normal note notes notice npi null num "
    "number of ok old on one open option options or order other out outpatient "
    "overflow overview page panel password patient payer payment pending percent "
    "period phone plan pointer policy portal position post prefix previous price "
    "primary print priority privacy procedure profile progress project projects "
    "provider public publish query quick quote radio range rate readonly ready "
    "reason receipt record records ref reference refresh region register reject "
    "relative remove repeat replace reply report request required reset result "
    "results retry return review right role row rows rule rules run safe save "
    "saved schedule school search second section select selected selection send "
    "service services session set setting settings sex share show side sign "
    "signature single site size skip slug solid sort source space span standard "
    "start state status step steps stop street style submit submitted subscriber "
    "success summary support switch sync system tab table tag tags target task "
    "tasks tax team teams template term terms test text textbox textarea theme "
    "tier time title to today toggle token top total track transfer trigger true twin "
    "type unavailable unchecked undo unit unknown unselected up "
    "upcoming update updated upload url user username valid value values verify "
    "version vertical view visible visit wait warning week weekly width window "
    "work workflow write year yearly yes zip".split()
)

_HTML_ARIA_STRUCTURAL_VOCAB: FrozenSet[str] = frozenset(
    "0 a align alt aria article aside auto b background block body border bottom "
    "box br button calc caption cell center charset checked class col colgroup color "
    "column content contents controls cursor data dd default desc details dfn dialog dir "
    "disabled display div dl doctype dt em end fieldset figcaption figure fixed "
    "flex flow font footer form gap grid h1 h2 h3 h4 h5 h6 head header height hidden "
    "hr html i id img inherit initial inline input ins items justify kbd label left legend li "
    "line link list listbox main margin mark max menu menuitem meta min nav "
    "none normal nowrap ol opacity option optgroup output overflow p padding "
    "placeholder pointer position pre px radius readonly relative rem required revert "
    "rgb rgba right role root row s samp section select selected self sizing small "
    "solid span start strong style sub summary sup tab tabindex table tbody td "
    "template textarea tfoot th thead time title top tr transparent u ul unset utf "
    "value var vertical vh visibility vw webkit weight width wrap z".split()
)

_CLINICAL_STRUCTURAL_VOCAB: FrozenSet[str] = frozenset(
    "cdt cpt drg hcpcs icd icd10 icd9 loinc ndc npi rx snomed".split()
)

_PROTOCOL_JSON_KEYS: FrozenSet[str] = frozenset(
    "active_option_count ast_firewall_rejected controls feedback has_value idx "
    "in_modal last_failure_code max_turns missing_expected_token_or_unchanged "
    "newly_mounted_controls readback_refuted readback_status runtime_exception sanitized_html "
    "stripped_item_idx structural_error submit_disabled submit_still_disabled "
    "tag turn twin_html ui_chrome_tokens var_keys var_token_stripped_on_quick_input".split()
)

_BINARY_OR_IMAGE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"data:image/[a-z0-9.+-]+", re.IGNORECASE),
    re.compile(r"data:application/octet-stream", re.IGNORECASE),
    re.compile(r"\bimage/(?:png|jpeg|jpg|gif|webp|bmp|tiff|svg\+xml|x-icon)\b", re.IGNORECASE),
    re.compile(r";base64,", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9+/=])"),
)

_SAFE_STYLE_ATTR_RE = re.compile(r'\bstyle\s*=\s*"([^"]*)"', re.IGNORECASE)
_SAFE_IDX_ATTR_RE = re.compile(r'\bdata-oa-idx\s*=\s*"\d{1,4}"', re.IGNORECASE)
_SAFE_TABINDEX_RE = re.compile(r'\btabindex\s*=\s*"-?\d{1,2}"', re.IGNORECASE)
_SAFE_CHARSET_RE = re.compile(r'\bcharset\s*=\s*"utf-8"', re.IGNORECASE)
_SAFE_CSS_VALUE_RE = re.compile(
    r"(?:rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0?\.\d+|1(?:\.0+)?|0))?\s*\)"
    r"|#[0-9a-fA-F]{3,8}\b"
    r"|\b\d{1,4}(?:\.\d{1,2})?(?:px|rem|em|%|vh|vw)\b"
    r"|\b(?:0?\.\d+|1(?:\.0+)?)\b"
    r"|(?<=z-index:\s)-?\d{1,4}\b"
    r"|(?<=flex:\s)\d{1,2}\b)",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(
    r"SYM_[0-9A-F]{8}|ORD_NUM_\d{4}|CANARY_[A-Z]+_\d+|<OPAQUE(?:_\d+)?>|\$[^\W\d_]\w*|\w+"
)
_CANARY_RE = re.compile(r"^CANARY_[A-Z]+_\d+$")
_OPAQUE_RE = re.compile(r"^<OPAQUE(?:_\d+)?>$")
_BIJECTIVE_SYM_RE = re.compile(r"^SYM_[0-9A-F]{8}$")
_ORD_NUM_RE = re.compile(r"^ORD_NUM_\d{4}$")
_UNICODE_WORD_RE = re.compile(r"[^\W_]+")
_HTML_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")
_HTML_ATTR_VAL_RE = re.compile(
    r'\b(aria-label|placeholder|title|alt|value|label)\s*=\s*"([^"]*)"', re.IGNORECASE
)


class EgressBlockedError(PermissionError):
    """Raised when outbound data fails Zero-PHI egress inspection."""

    def __init__(self, verdict: "EgressVerdict") -> None:
        self.verdict = verdict
        detail = ", ".join(verdict.rejected_tokens[:5])
        msg = f"egress_blocked:{verdict.reason}" + (f" ({detail})" if detail else "")
        super().__init__(msg)


@dataclass(frozen=True)
class EgressPolicy:
    """Frozen allowlist policy governing every byte permitted to leave the local boundary."""

    allowed_tokens: FrozenSet[str] = field(
        default_factory=lambda: (
            _FROZEN_UI_VOCAB
            | _EXTRA_UI_CHROME_VOCAB
            | _HTML_ARIA_STRUCTURAL_VOCAB
            | _CLINICAL_STRUCTURAL_VOCAB
            | _PROTOCOL_JSON_KEYS
        )
    )
    include_html_aria_vocab: bool = True
    include_clinical_vocab: bool = True
    extra_allowed_tokens: FrozenSet[str] = field(default_factory=frozenset)
    allowed_var_keys: FrozenSet[str] = field(default_factory=frozenset)
    allow_bijective_symbols: bool = True

    def effective_tokens(self) -> FrozenSet[str]:
        base = set(self.allowed_tokens)
        if not self.include_html_aria_vocab:
            base -= _HTML_ARIA_STRUCTURAL_VOCAB - _FROZEN_UI_VOCAB - _EXTRA_UI_CHROME_VOCAB
        if not self.include_clinical_vocab:
            base -= _CLINICAL_STRUCTURAL_VOCAB
        if self.extra_allowed_tokens:
            base |= {t.lower() for t in self.extra_allowed_tokens}
        return frozenset(base)

    def is_token_allowed(self, token: str, effective: Optional[FrozenSet[str]] = None) -> bool:
        if _CANARY_RE.match(token) or _OPAQUE_RE.match(token):
            return True
        if self.allow_bijective_symbols and (_BIJECTIVE_SYM_RE.match(token) or _ORD_NUM_RE.match(token)):
            return True
        vocab = effective if effective is not None else self.effective_tokens()
        if token.startswith("$"):
            if self.allow_bijective_symbols and _BIJECTIVE_SYM_RE.match(token[1:]):
                return True
            if token in self.allowed_var_keys:
                return True
            parts = [p.lower() for p in token[1:].split("_") if p]
            return bool(parts) and all(p in vocab for p in parts)
        low = token.lower()
        if low in vocab:
            return True
        if "_" in low:
            parts = [p for p in low.split("_") if p]
            if parts and all(p in vocab for p in parts):
                return True
        return False


@dataclass(frozen=True)
class EgressVerdict:
    """Outcome of inspecting an outbound payload against an EgressPolicy."""

    allowed: bool
    reason: str
    rejected_tokens: Tuple[str, ...] = ()
    payload_sha256: str = ""
    byte_length: int = 0
    entry_sha256: str = ""


def _serialize_payload(payload: Union[Dict[str, Any], List[Any], str, bytes]) -> Tuple[bytes, str]:
    if isinstance(payload, bytes):
        return payload, ""
    if isinstance(payload, str):
        return payload.encode("utf-8"), payload
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8"), text


def _strip_safe_html_numeric_syntax(text: str, include_html: bool) -> str:
    if not include_html:
        return text
    cleaned = _SAFE_IDX_ATTR_RE.sub(" ", text)
    cleaned = _SAFE_TABINDEX_RE.sub(" ", cleaned)
    cleaned = _SAFE_CHARSET_RE.sub(" ", cleaned)

    def _scrub_style(match: re.Match[str]) -> str:
        inner = _SAFE_CSS_VALUE_RE.sub(" ", match.group(1))
        return f'style="{inner}"'

    return _SAFE_STYLE_ATTR_RE.sub(_scrub_style, cleaned)


def _iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def inspect_outbound_payload(
    payload: Union[Dict[str, Any], List[Any], str, bytes],
    policy: Optional[EgressPolicy] = None,
) -> EgressVerdict:
    """Inspect an outbound JSON/HTML payload and block any image, binary, or non-allowlisted token."""
    pol = policy or EgressPolicy()
    raw_bytes, text_repr = _serialize_payload(payload)
    digest = hashlib.sha256(raw_bytes).hexdigest()

    if isinstance(payload, bytes):
        return EgressVerdict(
            allowed=False,
            reason="binary_or_image_blocked",
            rejected_tokens=("<raw_bytes>",),
            payload_sha256=digest,
            byte_length=len(raw_bytes),
        )

    for pattern in _BINARY_OR_IMAGE_PATTERNS:
        m = pattern.search(text_repr)
        if m is not None:
            snippet = m.group(0)[:48]
            return EgressVerdict(
                allowed=False,
                reason="binary_or_image_blocked",
                rejected_tokens=(snippet,),
                payload_sha256=digest,
                byte_length=len(raw_bytes),
            )

    effective = pol.effective_tokens()
    rejected: List[str] = []
    seen_rejected: set[str] = set()
    for segment in _iter_strings(payload):
        scrubbed = _strip_safe_html_numeric_syntax(segment, pol.include_html_aria_vocab)
        for tok in _TOKEN_RE.findall(scrubbed):
            if not pol.is_token_allowed(tok, effective):
                if tok not in seen_rejected:
                    seen_rejected.add(tok)
                    rejected.append(tok)
                    if len(rejected) >= 25:
                        break
        if len(rejected) >= 25:
            break

    if rejected:
        return EgressVerdict(
            allowed=False,
            reason="non_allowlisted_token",
            rejected_tokens=tuple(rejected),
            payload_sha256=digest,
            byte_length=len(raw_bytes),
        )

    return EgressVerdict(
        allowed=True,
        reason="ok",
        rejected_tokens=(),
        payload_sha256=digest,
        byte_length=len(raw_bytes),
    )


class LocalEgressGuard:
    """Local HTTP proxy and callable middleware with an append-only SHA-256 compliance ledger."""

    def __init__(
        self,
        ledger_path: Union[str, Path],
        policy: Optional[EgressPolicy] = None,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.policy = policy or EgressPolicy()
        self._lock = threading.Lock()
        self._last_entry_sha256 = self._load_tail_hash()

    def _load_tail_hash(self) -> str:
        if not self.ledger_path.exists():
            return "0" * 64
        tail = "0" * 64
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and isinstance(rec.get("entry_sha256"), str):
                    tail = rec["entry_sha256"]
            except json.JSONDecodeError:
                continue
        return tail

    def check_and_record(
        self,
        payload: Union[Dict[str, Any], List[Any], str, bytes],
        *,
        destination: str = "unknown",
        content_type: str = "",
    ) -> EgressVerdict:
        """Inspect payload, append verbatim SHA-256 ledger record, and return verdict."""
        raw_bytes, text_repr = _serialize_payload(payload)
        ct_low = content_type.lower()
        if ct_low.startswith("image/") or "application/octet-stream" in ct_low:
            verdict = EgressVerdict(
                allowed=False,
                reason="binary_or_image_blocked",
                rejected_tokens=(content_type,),
                payload_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                byte_length=len(raw_bytes),
            )
        else:
            verdict = inspect_outbound_payload(payload, self.policy)

        with self._lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            entry_body: Dict[str, Any] = {
                "timestamp_utc": round(time.time(), 6),
                "destination": destination,
                "allowed": verdict.allowed,
                "reason": verdict.reason,
                "rejected_tokens": list(verdict.rejected_tokens),
                "byte_length": verdict.byte_length,
                "payload_sha256": verdict.payload_sha256,
                "prev_entry_sha256": self._last_entry_sha256,
                "payload_verbatim": text_repr if not isinstance(payload, bytes) else "<binary_bytes_redacted>",
            }
            canonical = json.dumps(entry_body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            entry_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            entry_body["entry_sha256"] = entry_sha256
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry_body, sort_keys=True, ensure_ascii=False) + "\n")
            self._last_entry_sha256 = entry_sha256

        return EgressVerdict(
            allowed=verdict.allowed,
            reason=verdict.reason,
            rejected_tokens=verdict.rejected_tokens,
            payload_sha256=verdict.payload_sha256,
            byte_length=verdict.byte_length,
            entry_sha256=entry_sha256,
        )

    def call_guarded(
        self,
        fn: Callable[[Any], Any],
        payload: Union[Dict[str, Any], List[Any], str],
        *,
        destination: str = "coding_agent",
    ) -> Tuple[Any, EgressVerdict]:
        """Inspect payload, log to ledger, raise EgressBlockedError if blocked, else invoke fn."""
        verdict = self.check_and_record(payload, destination=destination)
        if not verdict.allowed:
            raise EgressBlockedError(verdict)
        return fn(payload), verdict

    def read_ledger(self) -> List[Dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def verify_ledger_integrity(self) -> bool:
        """Verify every SHA-256 hash-chain link and payload digest in the ledger."""
        prev = "0" * 64
        for rec in self.read_ledger():
            if rec.get("prev_entry_sha256") != prev:
                return False
            recorded_entry_sha = rec.get("entry_sha256")
            check_body = {k: v for k, v in rec.items() if k != "entry_sha256"}
            canonical = json.dumps(check_body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != recorded_entry_sha:
                return False
            verbatim = rec.get("payload_verbatim")
            if isinstance(verbatim, str) and verbatim != "<binary_bytes_redacted>":
                if hashlib.sha256(verbatim.encode("utf-8")).hexdigest() != rec.get("payload_sha256"):
                    return False
            prev = str(recorded_entry_sha)
        return True

    @contextmanager
    def serve_http(
        self,
        upstream_handler: Optional[Callable[[Union[Dict[str, Any], str]], Any]] = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> Iterator[str]:
        """Run a local HTTP egress proxy that returns HTTP 403 on blocked payloads and HTTP 200 on allowed."""
        guard = self

        class _ProxyHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                ctype = self.headers.get("Content-Type", "")
                parsed: Union[Dict[str, Any], List[Any], str, bytes]
                if ctype.lower().startswith("image/") or "octet-stream" in ctype.lower():
                    parsed = raw
                else:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        parsed = raw
                    else:
                        if "application/json" in ctype.lower():
                            try:
                                parsed = json.loads(text)
                            except json.JSONDecodeError:
                                parsed = text
                        else:
                            parsed = text

                verdict = guard.check_and_record(
                    parsed,
                    destination=f"http:{self.path}",
                    content_type=ctype,
                )
                if not verdict.allowed:
                    body = json.dumps(
                        {
                            "allowed": False,
                            "reason": verdict.reason,
                            "rejected_tokens": list(verdict.rejected_tokens),
                            "payload_sha256": verdict.payload_sha256,
                            "entry_sha256": verdict.entry_sha256,
                        }
                    ).encode("utf-8")
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                upstream_res: Any = None
                if upstream_handler is not None and not isinstance(parsed, bytes):
                    upstream_res = upstream_handler(parsed if isinstance(parsed, (dict, str)) else text)
                body = json.dumps(
                    {
                        "allowed": True,
                        "reason": verdict.reason,
                        "payload_sha256": verdict.payload_sha256,
                        "entry_sha256": verdict.entry_sha256,
                        "upstream": upstream_res,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        server = ThreadingHTTPServer((host, port), _ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw_host = server.server_address[0]
            addr_host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else str(raw_host)
            addr_port = int(server.server_address[1])
            yield f"http://{addr_host}:{addr_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


def summarize_egress_ledger(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize allowed vs blocked outbound egress attempts from ledger entries."""
    allowed = sum(1 for e in entries if e.get("allowed") is True)
    blocked = sum(1 for e in entries if e.get("allowed") is False)
    return {
        "total_entries": len(entries),
        "allowed_count": allowed,
        "blocked_count": blocked,
        "total_allowed_bytes": sum(int(e.get("byte_length", 0)) for e in entries if e.get("allowed")),
    }


class BijectiveSymbolCodec:
    """Language-agnostic ephemeral HMAC-SHA256 bijective symbolizer for k=0 zero-PHI egress."""

    def __init__(self, *, preserve_tokens: Optional[FrozenSet[str]] = None) -> None:
        self._secret_key: bytes = secrets.token_bytes(32)
        self._preserve: FrozenSet[str] = (
            preserve_tokens if preserve_tokens is not None else _HTML_ARIA_STRUCTURAL_VOCAB
        )
        self._sym_to_raw: Dict[str, str] = {}
        self._raw_to_sym: Dict[str, str] = {}

    def symbolize_token(self, token: str) -> str:
        if not token:
            return token
        if token.startswith("$"):
            stem = token[1:]
            if not stem or _BIJECTIVE_SYM_RE.match(stem):
                return token
            return f"${self.symbolize_token(stem)}"
        if (
            _BIJECTIVE_SYM_RE.match(token)
            or _ORD_NUM_RE.match(token)
            or _CANARY_RE.match(token)
            or _OPAQUE_RE.match(token)
            or token.casefold() in self._preserve
        ):
            return token
        key = token.casefold()
        existing = self._raw_to_sym.get(key)
        if existing is not None:
            return existing
        digest = hmac.new(self._secret_key, key.encode("utf-8"), hashlib.sha256).hexdigest()[:8].upper()
        sym = f"SYM_{digest}"
        self._raw_to_sym[key] = sym
        self._sym_to_raw[sym] = token
        return sym

    def symbolize_text(self, text: str) -> str:
        return _TOKEN_RE.sub(lambda m: self.symbolize_token(m.group(0)), text or "")

    def symbolize_html(self, html: str) -> str:
        parts = _HTML_TAG_SPLIT_RE.split(html or "")
        out: List[str] = []
        for part in parts:
            if part.startswith("<") and part.endswith(">"):
                out.append(
                    _HTML_ATTR_VAL_RE.sub(
                        lambda m: f'{m.group(1)}="{self.symbolize_text(m.group(2))}"', part
                    )
                )
            else:
                out.append(self.symbolize_text(part))
        return "".join(out)

    def detokenize_text(self, text: str) -> str:
        return re.sub(
            r"SYM_[0-9A-F]{8}",
            lambda m: self._sym_to_raw.get(m.group(0), m.group(0)),
            text or "",
        )


def _format_plain_number(val: float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{val:.6f}".rstrip("0").rstrip(".")


class OrderPreservingRankCodec:
    """Batch-scoped order-preserving rank encoder mapping numeric/currency quantities to ORD_NUM_XXXX."""

    def __init__(self) -> None:
        self._val_to_tok: Dict[float, str] = {}
        self._tok_to_val: Dict[str, float] = {}

    @staticmethod
    def parse_quantity(raw: Any) -> Optional[float]:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        s = str(raw or "").strip()
        if not s:
            return None
        from openadapt_kernel.canonical import parse_canonical_currency_cents

        cents = parse_canonical_currency_cents(s)
        if cents is not None:
            return cents / 100.0
        if s.endswith("%"):
            try:
                return float(s[:-1].strip())
            except ValueError:
                return None
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        return None

    def fit(self, values: Iterable[Any]) -> "OrderPreservingRankCodec":
        parsed = sorted({v for raw in values if (v := self.parse_quantity(raw)) is not None})
        self._val_to_tok = {v: f"ORD_NUM_{i:04d}" for i, v in enumerate(parsed, start=1)}
        self._tok_to_val = {tok: v for v, tok in self._val_to_tok.items()}
        return self

    def encode_value(self, raw: Any) -> Optional[str]:
        v = self.parse_quantity(raw)
        return self._val_to_tok.get(v) if v is not None else None

    def decode_token(self, token: str) -> Optional[float]:
        return self._tok_to_val.get(token)

    def detokenize_text(self, text: str) -> str:
        return re.sub(
            r"ORD_NUM_\d{4}",
            lambda m: _format_plain_number(self._tok_to_val[m.group(0)])
            if m.group(0) in self._tok_to_val
            else m.group(0),
            text or "",
        )


def discover_taint_free_ui_chrome(
    session_tokens_by_subject: Mapping[str, Iterable[str]],
    *,
    tainted_record_values: Iterable[str] = (),
    cohort_by_subject: Optional[Mapping[str, str]] = None,
    min_subjects: int = 3,
    min_cohorts: int = 2,
) -> FrozenSet[str]:
    """Discover cross-subject, cross-cohort UI chrome while permanently disqualifying tainted record tokens."""
    tainted: set[str] = set()
    for val in tainted_record_values:
        s = str(val or "").strip().casefold()
        if s:
            tainted.add(s)
            for tok in _UNICODE_WORD_RE.findall(s):
                tainted.add(tok.casefold())

    token_subjects: Dict[str, set[str]] = {}
    token_cohorts: Dict[str, set[str]] = {}
    for subj, raw_tokens in session_tokens_by_subject.items():
        cohort = cohort_by_subject.get(subj, subj) if cohort_by_subject is not None else subj
        subj_seen: set[str] = set()
        for raw in raw_tokens:
            for tok in _UNICODE_WORD_RE.findall(str(raw or "").casefold()):
                if tok in tainted or tok in subj_seen:
                    continue
                subj_seen.add(tok)
                token_subjects.setdefault(tok, set()).add(subj)
                token_cohorts.setdefault(tok, set()).add(cohort)

    return frozenset(
        tok
        for tok, subjs in token_subjects.items()
        if len(subjs) >= min_subjects
        and (cohort_by_subject is None or len(token_cohorts.get(tok, ())) >= min_cohorts)
    )
