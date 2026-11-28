"""Unified SimplexTransducer runtime behind ``openadapt.wrap(agent)``.

Provides thread-safe per-episode supervision for any ``BenchmarkAgent``
exposing ``.act(observation, task, history)`` using stdlib-only top-level
imports and lazy optional bridges to ``openadapt_types``,
``openadapt_kernel``, and ``openadapt_flow``.
"""

from __future__ import annotations

import copy
import enum
import hashlib
import inspect
import json
import os
import re
import struct
import sys
import tempfile
import threading
import uuid
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - Win32 fallback
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "AppCartridge",
    "AtomicCartridgeStore",
    "DecisionRequest",
    "DecisionResponse",
    "EpisodeState",
    "InterventionNote",
    "Obligation",
    "ObligationKind",
    "OpaqueCredentialVault",
    "ReceiptOutcome",
    "SagaCommitRecord",
    "SagaState",
    "SimplexTransducer",
    "SourceItem",
    "SpanAnchor",
    "SupervisorConfig",
    "TransducerAction",
    "TransducerReceipt",
    "TrustLevel",
    "WriteAheadSagaLog",
]

T = TypeVar("T")

_TERMINAL_ACTION_TYPES: frozenset[str] = frozenset(
    {"done", "answer", "infeasible", "finish", "terminate"}
)
_MUTATING_WIRE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXACT_ID_SUFFIXES: tuple[str, ...] = (
    "_id",
    "_code",
    "_key",
    "_no",
    "_num",
    "_number",
)
_EXACT_ID_NAMES: frozenset[str] = frozenset(
    {"id", "mrn", "ssn", "account", "order", "code", "token", "laterality"}
)
_DEFAULT_NON_TERMINAL_STATUSES: tuple[str, ...] = (
    "draft",
    "error",
    "failed",
    "unsaved",
    "pending_edit",
)
_VOLATILE_FINGERPRINT_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "created_at",
        "updated_at",
        "step_index",
        "step",
        "receipt_id",
        "commit_index",
        "latency_ms",
        "nonce",
    }
)
_DISCONNECT_TITLE_MARKERS: tuple[str, ...] = (
    "disconnected",
    "disconnect",
    "reconnecting",
    "connection lost",
    "session lost",
    "no signal",
)
_MODAL_ROLES: frozenset[str] = frozenset(
    {"dialog", "alertdialog", "modal", "sheet", "axdialog", "axsheet"}
)
_GRAMMATICAL_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "before",
        "best",
        "between",
        "but",
        "by",
        "can",
        "could",
        "date",
        "dear",
        "do",
        "does",
        "don",
        "draft",
        "each",
        "every",
        "for",
        "forward",
        "from",
        "further",
        "get",
        "has",
        "have",
        "hello",
        "her",
        "here",
        "hi",
        "him",
        "his",
        "how",
        "if",
        "in",
        "include",
        "into",
        "is",
        "it",
        "its",
        "item",
        "just",
        "keep",
        "let",
        "make",
        "may",
        "me",
        "mention",
        "more",
        "most",
        "must",
        "my",
        "no",
        "none",
        "not",
        "note",
        "notes",
        "of",
        "on",
        "once",
        "only",
        "or",
        "our",
        "out",
        "over",
        "please",
        "regards",
        "remember",
        "reply",
        "send",
        "share",
        "she",
        "should",
        "so",
        "some",
        "specify",
        "state",
        "status",
        "subject",
        "summary",
        "sure",
        "task",
        "thanks",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "time",
        "to",
        "today",
        "tomorrow",
        "under",
        "up",
        "update",
        "us",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "yesterday",
        "you",
        "your",
    }
)
_WEEKDAYS_AND_MONTHS: frozenset[str] = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)
_PROPER_NOUN_PHRASE_RE = re.compile(
    r"\b(?:(?:Dr|Mr|Mrs|Ms|Prof)\.\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b"
)
_SINGLE_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_TIME_RE = re.compile(
    r"\b(?:\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|\d{1,2}\s*(?:AM|PM|am|pm))\b"
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?)\b"
)
_REQUIRED_ANCHOR_CLAUSE_RE = re.compile(
    r"(?:make\s+sure\s+to|be\s+sure\s+to|remember\s+to|don'?t\s+forget\s+to|"
    r"must|explicitly)\s+(?:mention|include|note|state|specify|add)\s+"
    r"([^.;!\n]+)",
    re.IGNORECASE,
)
_QUOTED_SPAN_RE = re.compile(r"""["']([^"']{2,80})["']""")
_SECTION_HEADER_RE = re.compile(
    r"^(?:#{1,4}\s+(.+)|\[([^\]]{2,60})\]|---\s*([^-]{2,60}?)\s*---|"
    r"Container:\s*(.+))$",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _sha256_hex(payload: Any) -> str:
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest()
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _durable_fsync(fd: int) -> None:
    """Flush file descriptor to stable storage using F_FULLFSYNC on macOS."""
    if sys.platform == "darwin" and fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _extract_attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _action_type_str(action: Any) -> str:
    raw = _extract_attr_or_key(action, "type", "")
    if isinstance(raw, enum.Enum):
        raw = raw.value
    return str(raw or "").strip().lower()


def _action_raw_dict(action: Any) -> dict[str, Any]:
    raw = _extract_attr_or_key(action, "raw_action", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(action, Mapping):
        return dict(action)
    return {}


def _commit_fingerprint(action: Any) -> str:
    """Compute a deterministic content fingerprint for a commit action."""
    raw = _action_raw_dict(action)
    explicit = _extract_attr_or_key(action, "idempotency_key", None) or raw.get(
        "idempotency_key"
    )
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    filtered_raw = {k: v for k, v in raw.items() if k not in _VOLATILE_FINGERPRINT_KEYS}
    target_node_id = _extract_attr_or_key(action, "target_node_id", None)
    target_name = _extract_attr_or_key(action, "target_name", None)
    target_role = _extract_attr_or_key(action, "target_role", None)
    text = _extract_attr_or_key(action, "text", None)
    answer = _extract_attr_or_key(action, "answer", None)
    entities = _extract_attr_or_key(action, "entities", None)

    canonical: dict[str, Any] = {"type": _action_type_str(action)}
    if target_node_id is not None:
        canonical["target_node_id"] = target_node_id
    if target_name is not None:
        canonical["target_name"] = target_name
    if target_role is not None:
        canonical["target_role"] = target_role
    if text is not None:
        canonical["text"] = text
    if answer is not None:
        canonical["answer"] = answer
    if entities is not None:
        canonical["entities"] = entities
    if filtered_raw:
        canonical["raw"] = filtered_raw

    has_semantic_payload = any(
        k in canonical
        for k in ("target_node_id", "target_name", "text", "answer", "entities")
    ) or bool({k for k in filtered_raw if k != "is_commit"})
    if not has_semantic_payload:
        x = _extract_attr_or_key(action, "x", None)
        y = _extract_attr_or_key(action, "y", None)
        if x is not None:
            canonical["x"] = x
        if y is not None:
            canonical["y"] = y
    return _sha256_hex(canonical)


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _is_blank_png(data: bytes) -> bool:
    """Return True if *data* is a valid non-interlaced PNG with zero pixel variance."""
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    idat_parts: list[bytes] = []
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + length
        if chunk_end + 4 > len(data):
            break
        chunk = data[chunk_start:chunk_end]
        if ctype == b"IHDR" and length >= 13:
            width, height, bit_depth, color_type, _comp, _filt, interlace = (
                struct.unpack(">IIBBBBB", chunk[:13])
            )
        elif ctype == b"IDAT":
            idat_parts.append(chunk)
        elif ctype == b"IEND":
            break
        pos = chunk_end + 4

    if width <= 0 or height <= 0 or interlace != 0 or bit_depth not in (8, 16):
        return False
    channels_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_map.get(color_type)
    if channels is None or not idat_parts:
        return False
    bpp = channels * (bit_depth // 8)
    row_bytes = width * bpp
    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error:
        return False
    stride = 1 + row_bytes
    if len(raw) < height * stride:
        return False

    zero_row = b"\x00" * row_bytes
    prev_row = bytearray(row_bytes)
    first_pixel: bytes | None = None
    expected_row: bytes | None = None

    for r in range(height):
        row_offset = r * stride
        ftype = raw[row_offset]
        row_in = raw[row_offset + 1 : row_offset + stride]
        if expected_row is not None:
            if ftype == 2 and row_in == zero_row:
                continue
            if ftype == 0 and row_in == expected_row:
                continue
            if (
                ftype == 1
                and first_pixel is not None
                and row_in[:bpp] == first_pixel
                and row_in[bpp:] == zero_row[bpp:]
            ):
                continue

        if ftype == 0:
            recon = bytearray(row_in)
        elif ftype == 1:
            recon = bytearray(row_bytes)
            for i in range(row_bytes):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (row_in[i] + left) & 0xFF
        elif ftype == 2:
            recon = bytearray(
                (row_in[i] + prev_row[i]) & 0xFF for i in range(row_bytes)
            )
        elif ftype == 3:
            recon = bytearray(row_bytes)
            for i in range(row_bytes):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                recon[i] = (row_in[i] + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:
            recon = bytearray(row_bytes)
            for i in range(row_bytes):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                up_left = prev_row[i - bpp] if i >= bpp else 0
                recon[i] = (row_in[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            return False

        if first_pixel is None:
            first_pixel = bytes(recon[:bpp])
            expected_row = first_pixel * width
        if bytes(recon) != expected_row:
            return False
        prev_row = recon

    return first_pixel is not None


def _is_blank_frame(screenshot: Any) -> bool:
    """Return True if *screenshot* is a zero-variance uniform blank frame."""
    if screenshot is None:
        return False
    if isinstance(screenshot, (bytes, bytearray, memoryview)):
        data = bytes(screenshot)
        if not data:
            return False
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return _is_blank_png(data)
        return len(data) >= 16 and len(set(data)) <= 1
    getextrema = getattr(screenshot, "getextrema", None)
    if callable(getextrema):
        try:
            extrema = getextrema()
        except Exception:
            return False
        if isinstance(extrema, tuple) and extrema:
            if isinstance(extrema[0], tuple):
                return all(lo == hi for lo, hi in extrema)
            if len(extrema) == 2:
                return extrema[0] == extrema[1]
    return False


def _node_label(node: Mapping[str, Any]) -> str:
    for key in ("name", "title", "label", "aria_label", "aria-label", "text"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _collect_subtree_ids_and_names(
    node: Mapping[str, Any], *, max_nodes: int = 256
) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    stack: list[Mapping[str, Any]] = [node]
    visited = 0
    while stack and visited < max_nodes:
        cur = stack.pop()
        visited += 1
        for id_key in ("node_id", "id", "element_id"):
            val = cur.get(id_key)
            if val is not None and str(val).strip():
                ids.add(str(val).strip())
        lbl = _node_label(cur)
        if lbl:
            names.add(lbl.lower())
        children = cur.get("children")
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            for child in children:
                if isinstance(child, Mapping):
                    stack.append(child)
    return ids, names


def _detect_blocking_modals(observation: Any) -> list[dict[str, Any]]:
    """Inspect ``focused_element`` and ``accessibility_tree`` for blocking modals."""
    modals: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _is_modal_node(node: Mapping[str, Any]) -> bool:
        if node.get("visible") is False or node.get("hidden") is True:
            return False
        role = (
            str(
                node.get("role")
                or node.get("control_type")
                or node.get("aria_role")
                or node.get("type")
                or ""
            )
            .strip()
            .lower()
        )
        if role in _MODAL_ROLES:
            return True
        if node.get("aria_modal") is True or node.get("modal") is True:
            return True
        return False

    def _record_modal(node: Mapping[str, Any]) -> None:
        title = _node_label(node)
        if not title:
            children = node.get("children")
            if isinstance(children, Sequence) and not isinstance(
                children, (str, bytes)
            ):
                for child in children:
                    if isinstance(child, Mapping):
                        child_lbl = _node_label(child)
                        if child_lbl:
                            title = child_lbl
                            break
        node_id = str(node.get("node_id") or node.get("id") or "")
        dedup_key = (node_id, title.lower())
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
        subtree_ids, subtree_names = _collect_subtree_ids_and_names(node)
        modals.append(
            {
                "node_id": node_id,
                "title": title or "dialog",
                "subtree_ids": subtree_ids,
                "subtree_names": subtree_names,
            }
        )

    focused = _extract_attr_or_key(observation, "focused_element", None)
    if isinstance(focused, Mapping) and _is_modal_node(focused):
        _record_modal(focused)

    tree = _extract_attr_or_key(observation, "accessibility_tree", None)
    if isinstance(tree, Mapping):
        stack: list[Mapping[str, Any]] = [tree]
        visited = 0
        while stack and visited < 512:
            cur = stack.pop()
            visited += 1
            if _is_modal_node(cur):
                _record_modal(cur)
                continue
            children = cur.get("children")
            if isinstance(children, Sequence) and not isinstance(
                children, (str, bytes)
            ):
                for child in children:
                    if isinstance(child, Mapping):
                        stack.append(child)
    return modals


def _modal_referenced_in_instruction(modal_title: str, instruction: str) -> bool:
    if not modal_title or not instruction:
        return False
    title_low = modal_title.strip().lower()
    instr_low = instruction.lower()
    if title_low in ("dialog", "alertdialog", "modal"):
        return False
    if title_low in instr_low:
        return True
    tokens = [
        tok
        for tok in re.findall(r"[a-z0-9]{3,}", title_low)
        if tok not in _GRAMMATICAL_STOPWORDS
    ]
    if not tokens:
        return False
    matched = sum(1 for tok in tokens if tok in instr_low)
    return matched >= max(1, (len(tokens) + 1) // 2)


def _extract_proper_nouns(text: str) -> list[str]:
    """Extract proper-noun phrases, emails, and mid-sentence capitalized names."""
    if not text:
        return []
    found: list[str] = []
    seen_low: set[str] = set()

    def _add(candidate: str) -> None:
        cleaned = candidate.strip(" ,.;:!?()[]{}\"'")
        low = cleaned.lower()
        if not cleaned or low in seen_low:
            return
        words = [w.strip(".") for w in cleaned.split()]
        if all(
            w.lower() in _GRAMMATICAL_STOPWORDS or w.lower() in _WEEKDAYS_AND_MONTHS
            for w in words
        ):
            return
        seen_low.add(low)
        found.append(cleaned)

    for email in _EMAIL_RE.findall(text):
        _add(email)

    phrase_spans: list[tuple[int, int]] = []
    for match in _PROPER_NOUN_PHRASE_RE.finditer(text):
        phrase_spans.append((match.start(), match.end()))
        _add(match.group(0))

    for match in _SINGLE_PROPER_NOUN_RE.finditer(text):
        start, end = match.start(), match.end()
        if any(ps <= start and end <= pe for ps, pe in phrase_spans):
            continue
        token = match.group(0)
        low = token.lower()
        if low in _GRAMMATICAL_STOPWORDS or low in _WEEKDAYS_AND_MONTHS:
            continue
        # Only treat single-word capitalized tokens as proper nouns when they are
        # mid-sentence (not right after sentence boundary) or followed by ':'.
        prefix = text[:start].rstrip()
        is_sentence_start = not prefix or prefix[-1] in ".!?\n-*"
        followed_by_colon = end < len(text) and text[end] == ":"
        if not is_sentence_start or followed_by_colon:
            _add(token)

    return found


def _extract_required_anchors(text: str) -> list[str]:
    """Extract explicitly requested named/time/quoted anchors from task text."""
    if not text:
        return []
    anchors: list[str] = []
    seen_low: set[str] = set()

    def _add(val: str) -> None:
        cleaned = val.strip(" ,.;:!?()[]{}\"'")
        low = re.sub(r"\s+", " ", cleaned.lower())
        if not cleaned or len(cleaned) < 2 or low in seen_low:
            return
        if low in _GRAMMATICAL_STOPWORDS:
            return
        seen_low.add(low)
        anchors.append(cleaned)

    for match in _REQUIRED_ANCHOR_CLAUSE_RE.finditer(text):
        clause = match.group(1).strip()
        for q in _QUOTED_SPAN_RE.findall(clause):
            _add(q)
        for t in _TIME_RE.findall(clause):
            _add(t)
        for d in _DATE_RE.findall(clause):
            _add(d)
        for pn in _extract_proper_nouns(clause):
            _add(pn)
        # Also catch standalone capitalized tokens at the start of the clause
        for tok in _SINGLE_PROPER_NOUN_RE.findall(clause):
            if tok.lower() not in _GRAMMATICAL_STOPWORDS:
                _add(tok)
    return anchors


def _extract_content_tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]{3,}", text.lower())
        if tok not in _GRAMMATICAL_STOPWORDS
    }


def _normalize_anchor_for_match(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _split_outbound_segments(payload: Any) -> list[str]:
    if isinstance(payload, str):
        raw_parts = re.split(r"(?:\n+|(?<=[.!?])\s+)", payload.strip())
        return [p.strip(" -*\t") for p in raw_parts if p.strip(" -*\t")]
    if isinstance(payload, Sequence) and not isinstance(payload, (bytes, bytearray)):
        out: list[str] = []
        for item in payload:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, Mapping):
                out.append(json.dumps(item, sort_keys=True, ensure_ascii=True))
            elif item is not None:
                out.append(str(item).strip())
        return [s for s in out if s]
    if isinstance(payload, Mapping):
        return [
            f"{k}: {v}" for k, v in payload.items() if v is not None and str(v).strip()
        ]
    return []


class TrustLevel(str, enum.Enum):
    """Authority level of an instruction or observation span."""

    TRUSTED_INSTRUCTION = "TRUSTED_INSTRUCTION"
    UNTRUSTED_DATA = "UNTRUSTED_DATA"


class ObligationKind(str, enum.Enum):
    """Event-driven obligation categories accrued during an episode."""

    RECORD = "Record"
    FILE = "File"
    TOGGLE = "Toggle"
    ANSWER = "Answer"


class SagaState(str, enum.Enum):
    """Write-ahead saga commit lifecycle states."""

    @staticmethod
    def _generate_next_value_(
        name: str, start: int, count: int, last_values: list[Any]
    ) -> str:
        return name

    INTENT = enum.auto()
    PENDING_EFFECT = enum.auto()
    VERIFIED = enum.auto()
    COMPENSATED_UNDO = enum.auto()
    RECONCILE = enum.auto()


class ReceiptOutcome(str, enum.Enum):
    """Outcome recorded on a ``TransducerReceipt``."""

    @staticmethod
    def _generate_next_value_(
        name: str, start: int, count: int, last_values: list[Any]
    ) -> str:
        return name

    VERIFIED = enum.auto()
    COMPLETED_UNVERIFIED = enum.auto()
    HALTED = enum.auto()


_VALID_SAGA_TRANSITIONS: dict[SagaState | None, frozenset[SagaState]] = {
    None: frozenset({SagaState.INTENT}),
    SagaState.INTENT: frozenset({SagaState.PENDING_EFFECT, SagaState.COMPENSATED_UNDO}),
    SagaState.PENDING_EFFECT: frozenset(
        {SagaState.VERIFIED, SagaState.COMPENSATED_UNDO, SagaState.RECONCILE}
    ),
    SagaState.RECONCILE: frozenset(
        {SagaState.VERIFIED, SagaState.COMPENSATED_UNDO, SagaState.RECONCILE}
    ),
    SagaState.VERIFIED: frozenset({SagaState.VERIFIED}),
    SagaState.COMPENSATED_UNDO: frozenset({SagaState.COMPENSATED_UNDO}),
}


@dataclass(frozen=True)
class SupervisorConfig:
    """Configuration for ``SimplexTransducer``."""

    isolation_mode: Literal["iid_ephemeral", "continual_persistent"] = "iid_ephemeral"
    max_repairs_per_commit: int = 2
    min_budget_for_ui_reopen: int = 4
    max_semantic_fpr: float = 0.02
    enable_repair_hints: bool = True
    enable_verify_before_done: bool = True
    enable_intra_episode_skills: bool = True
    enable_conformal_veto: bool = True
    wal_dir: str | Path | None = None
    cartridge_path: str | Path | None = None
    environment_id: str = "local-env-0001"
    runner_id: str = "simplex-runner-01"
    workflow_version: str = "v1.0.0-wrap"
    qualification_id: str = "qual-default-01"
    minimum_effect_strength: str = "independent_session"

    def __post_init__(self) -> None:
        if self.isolation_mode not in ("iid_ephemeral", "continual_persistent"):
            raise ValueError(
                "isolation_mode must be 'iid_ephemeral' or 'continual_persistent'"
            )
        if self.max_repairs_per_commit < 0:
            raise ValueError("max_repairs_per_commit must be >= 0")
        if self.min_budget_for_ui_reopen < 1:
            raise ValueError("min_budget_for_ui_reopen must be >= 1")
        if not (0.0 <= self.max_semantic_fpr <= 1.0):
            raise ValueError("max_semantic_fpr must be between 0.0 and 1.0")

    @property
    def config_sha256(self) -> str:
        """Deterministic SHA-256 hex digest of this configuration."""
        payload = {
            "isolation_mode": self.isolation_mode,
            "max_repairs_per_commit": self.max_repairs_per_commit,
            "min_budget_for_ui_reopen": self.min_budget_for_ui_reopen,
            "max_semantic_fpr": self.max_semantic_fpr,
            "enable_repair_hints": self.enable_repair_hints,
            "enable_verify_before_done": self.enable_verify_before_done,
            "enable_intra_episode_skills": self.enable_intra_episode_skills,
            "enable_conformal_veto": self.enable_conformal_veto,
            "environment_id": self.environment_id,
            "runner_id": self.runner_id,
            "workflow_version": self.workflow_version,
            "qualification_id": self.qualification_id,
            "minimum_effect_strength": self.minimum_effect_strength,
        }
        return _sha256_hex(payload)


@dataclass
class SpanAnchor:
    """Observed entity anchor captured across surfaces for exact join checks."""

    key: str
    value: str
    entity_type: str = "entity"
    source_surface: str = ""
    source_origin: str = ""
    trust_level: TrustLevel = TrustLevel.TRUSTED_INSTRUCTION
    step_index: int = 0
    exact_required: bool = False


@dataclass
class SourceItem:
    """Discrete source record indexed from a multi-container observation or prompt."""

    item_id: str
    container_id: str
    text: str
    entities: set[str] = field(default_factory=set)
    raw_entities: list[str] = field(default_factory=list)
    is_task_anchored: bool = False


@dataclass
class Obligation:
    """Event-driven obligation accrued during an episode."""

    obligation_id: str
    kind: ObligationKind
    target: str
    expected_state: dict[str, Any] = field(default_factory=dict)
    fulfilled: bool = False
    verified: bool = False
    evidence_tier: int = 0
    commit_index: int | None = None


@dataclass
class InterventionNote:
    """Append-only intervention delivered to the wrapped agent."""

    kind: str
    hint: str
    delta_note: str
    step_index: int
    timestamp: str = field(default_factory=_utc_now_iso)


@dataclass
class SagaCommitRecord:
    """Per-commit state recorded in ``WriteAheadSagaLog`` and ``EpisodeState``."""

    run_id: str
    task_id: str
    commit_index: int
    state: SagaState
    action_summary: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_utc_now_iso)
    idempotency_key: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "commit_index": self.commit_index,
            "state": self.state.value,
            "action_summary": self.action_summary,
            "evidence": self.evidence,
            "updated_at": self.updated_at,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SagaCommitRecord:
        return cls(
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            commit_index=int(data["commit_index"]),
            state=SagaState(str(data["state"])),
            action_summary=dict(data.get("action_summary") or {}),
            evidence=dict(data.get("evidence") or {}),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )


@dataclass
class TransducerAction:
    """Normalized action object returned on abstention or repair when needed."""

    type: str
    raw_action: dict[str, Any] = field(default_factory=dict)
    reported_success: bool = False
    is_commit: bool = False

    @property
    def is_abstained(self) -> bool:
        return self.raw_action.get("outcome") == "abstained"


@dataclass(frozen=True)
class DecisionRequest:
    """Payload passed to an optional in-loop ``decision_resolver``."""

    run_id: str
    task_id: str
    commit_index: int
    trap_code: str
    reason: str
    proposed_action: Any
    observation_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionResponse:
    """Resolution returned by an in-loop ``decision_resolver`` (AVC_H)."""

    approved: bool
    replacement_action: Any = None
    reason: str = ""


@dataclass(frozen=True)
class TransducerReceipt:
    """Episode or commit receipt compatible with ``ExecuteEvidenceReceiptV1``."""

    outcome: Literal["VERIFIED", "COMPLETED_UNVERIFIED", "HALTED"]
    receipt_id: str
    execution_id: str
    run_id: str
    task_id: str
    config_sha256: str
    oracle_tier: Literal[0, 1, 2, 3]
    observed_effect_strength: str | None
    minimum_effect_strength: str
    authorization_passed: bool
    identity_passed: bool
    postcondition_passed: bool
    effect_passed: bool
    delivery_uncertain: bool
    evidence_digest: str
    issued_at: str
    human_touches: int = 0
    commit_index: int = 0
    reason: str = ""
    environment_id: str = "local-env-0001"
    runner_id: str = "simplex-runner-01"
    workflow_version: str = "v1.0.0-wrap"
    qualification_id: str = "qual-default-01"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_execute_evidence_receipt_v1(self) -> Any:
        """Lazily build an ``openadapt_types.ExecuteEvidenceReceiptV1``."""
        from openadapt_types.execute import (
            EffectStrengthV1,
            ExecuteEvidenceContractV1,
            ExecuteEvidenceReceiptV1,
            ExecuteTerminalOutcomeV1,
        )

        min_strength = EffectStrengthV1(self.minimum_effect_strength)
        obs_strength = (
            EffectStrengthV1(self.observed_effect_strength)
            if self.observed_effect_strength is not None
            else None
        )

        # Map transducer outcome to ExecuteTerminalOutcomeV1 while respecting
        # the ExecuteEvidenceReceiptV1 proof ladder (production seals require
        # oracle tier 2/3 system-of-record evidence).
        if self.outcome == ReceiptOutcome.HALTED:
            terminal = ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
            delivery_uncertain = self.delivery_uncertain
        elif (
            self.outcome == ReceiptOutcome.VERIFIED
            and obs_strength is EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD
        ):
            terminal = ExecuteTerminalOutcomeV1.VERIFIED
            delivery_uncertain = False
        else:
            terminal = ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
            delivery_uncertain = True

        # Ensure opaque IDs satisfy pattern ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$
        def _opaque(val: str, prefix: str) -> str:
            cleaned = "".join(
                ch if (ch.isalnum() or ch in "._:-") else "-" for ch in val
            ).lstrip("._:-")
            if len(cleaned) < 8:
                cleaned = f"{prefix}-{cleaned or '00000000'}"
            return cleaned[:128]

        contracts = ExecuteEvidenceContractV1(
            authorization_passed=self.authorization_passed,
            identity_passed=self.identity_passed,
            postcondition_passed=self.postcondition_passed,
            effect_passed=self.effect_passed,
            minimum_effect_strength=min_strength,
            observed_effect_strength=obs_strength,
            model_used=True,
            external_network_used=False,
        )
        return ExecuteEvidenceReceiptV1(
            receipt_id=_opaque(self.receipt_id, "rcpt"),
            execution_id=_opaque(self.execution_id, "exec"),
            workflow_digest=f"sha256:{self.config_sha256}",
            workflow_version=_opaque(self.workflow_version, "ver"),
            qualification_id=_opaque(self.qualification_id, "qual"),
            environment_id=_opaque(self.environment_id, "env"),
            runner_id=_opaque(self.runner_id, "run"),
            nonce=_opaque(f"nonce-{uuid.uuid4().hex[:16]}", "nonce"),
            oracle_tier=self.oracle_tier,
            outcome=terminal,
            contracts=contracts,
            delivery_uncertain=delivery_uncertain,
            compensation_effect_verified=False,
            evidence_digest=f"sha256:{self.evidence_digest}",
            issued_at=self.issued_at,
        )


class OpaqueCredentialVault:
    """Holds secrets behind opaque ``vault://<key_name>`` handles.

    Secrets are resolved strictly inside low-level login callbacks and never
    enter ``EpisodeState``, WAL logs, interventions, or receipts.
    """

    HANDLE_PREFIX = "vault://"

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._lock = threading.RLock()
        self._secrets: dict[str, str] = {}
        if initial:
            for key, value in initial.items():
                self.put(key, value)

    def put(self, key_name: str, secret_value: str) -> str:
        clean_key = str(key_name).strip()
        if not clean_key or "/" in clean_key or " " in clean_key:
            raise ValueError("vault key_name must be a non-empty token without slashes")
        if not isinstance(secret_value, str) or not secret_value:
            raise ValueError("secret_value must be a non-empty string")
        with self._lock:
            self._secrets[clean_key] = secret_value
        return f"{self.HANDLE_PREFIX}{clean_key}"

    @classmethod
    def is_handle(cls, value: Any) -> bool:
        return isinstance(value, str) and value.startswith(cls.HANDLE_PREFIX)

    def resolve_for_callback(self, handle: str, callback: Callable[[str], T]) -> T:
        """Resolve ``vault://<key_name>`` strictly inside *callback*."""
        if not self.is_handle(handle):
            raise ValueError("handle must start with 'vault://'")
        key_name = handle[len(self.HANDLE_PREFIX) :]
        with self._lock:
            if key_name not in self._secrets:
                raise KeyError(f"unknown vault handle: {handle}")
            secret = self._secrets[key_name]
        return callback(secret)

    def redact(self, value: Any) -> Any:
        """Scrub any raw vault secret values from strings or nested containers."""
        with self._lock:
            items = list(self._secrets.items())
        if not items:
            return value
        return self._redact_inner(value, items)

    def _redact_inner(self, value: Any, items: list[tuple[str, str]]) -> Any:
        if isinstance(value, str):
            out = value
            for key_name, secret in items:
                if secret and secret in out:
                    out = out.replace(secret, f"{self.HANDLE_PREFIX}{key_name}")
            return out
        if isinstance(value, dict):
            return {
                self._redact_inner(k, items): self._redact_inner(v, items)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._redact_inner(v, items) for v in value]
        if isinstance(value, tuple):
            return tuple(self._redact_inner(v, items) for v in value)
        return value

    def __repr__(self) -> str:
        with self._lock:
            keys = sorted(self._secrets.keys())
        return f"OpaqueCredentialVault(keys={keys!r})"


class WriteAheadSagaLog:
    """Durable write-ahead saga log with ``F_FULLFSYNC`` (macOS) / ``fsync``.

    Records commit transitions::

        INTENT -> PENDING_EFFECT -> VERIFIED | COMPENSATED_UNDO | RECONCILE

    Indexed per ``(run_id, task_id, commit_index)`` so resuming after a crash
    or halt skips already-verified commits ``1..k-1``.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str, int], SagaCommitRecord] = {}
        if self.path is not None and self.path.exists():
            self._recover_from_disk()

    def _recover_from_disk(self) -> None:
        assert self.path is not None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    # Ignore truncated trailing line from a crash mid-write.
                    continue
                if not isinstance(data, Mapping):
                    continue
                rec = SagaCommitRecord.from_json(data)
                self._records[(rec.run_id, rec.task_id, rec.commit_index)] = rec

    def _append_durable(self, record: SagaCommitRecord) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(record.to_json(), sort_keys=True, ensure_ascii=True) + "\n"
        ).encode("utf-8")
        fd = os.open(
            str(self.path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, line)
            _durable_fsync(fd)
        finally:
            os.close(fd)

    def _transition(
        self,
        *,
        run_id: str,
        task_id: str,
        commit_index: int,
        new_state: SagaState,
        action_summary: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> SagaCommitRecord:
        if commit_index < 1:
            raise ValueError("commit_index must be >= 1")
        key = (run_id, task_id, commit_index)
        with self._lock:
            existing = self._records.get(key)
            current_state = existing.state if existing is not None else None
            allowed = _VALID_SAGA_TRANSITIONS.get(current_state, frozenset())
            if new_state not in allowed:
                raise ValueError(
                    f"invalid saga transition {current_state} -> {new_state} "
                    f"for commit {commit_index}"
                )
            merged_summary = dict(existing.action_summary if existing else {})
            if action_summary:
                merged_summary.update(action_summary)
            merged_evidence = dict(existing.evidence if existing else {})
            if evidence:
                merged_evidence.update(evidence)
            resolved_idem_key = (
                str(idempotency_key)
                if idempotency_key
                else (existing.idempotency_key if existing is not None else "")
            )
            rec = SagaCommitRecord(
                run_id=run_id,
                task_id=task_id,
                commit_index=commit_index,
                state=new_state,
                action_summary=merged_summary,
                evidence=merged_evidence,
                updated_at=_utc_now_iso(),
                idempotency_key=resolved_idem_key,
            )
            self._append_durable(rec)
            self._records[key] = rec
            return rec

    def record_intent(
        self,
        run_id: str,
        task_id: str,
        commit_index: int,
        action_summary: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str = "",
    ) -> SagaCommitRecord:
        return self._transition(
            run_id=run_id,
            task_id=task_id,
            commit_index=commit_index,
            new_state=SagaState.INTENT,
            action_summary=action_summary,
            idempotency_key=idempotency_key,
        )

    def record_pending_effect(
        self,
        run_id: str,
        task_id: str,
        commit_index: int,
        action_summary: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str = "",
    ) -> SagaCommitRecord:
        return self._transition(
            run_id=run_id,
            task_id=task_id,
            commit_index=commit_index,
            new_state=SagaState.PENDING_EFFECT,
            action_summary=action_summary,
            idempotency_key=idempotency_key,
        )

    def record_outcome(
        self,
        run_id: str,
        task_id: str,
        commit_index: int,
        state: SagaState,
        evidence: Mapping[str, Any] | None = None,
        *,
        action_summary: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> SagaCommitRecord:
        if state not in (
            SagaState.VERIFIED,
            SagaState.COMPENSATED_UNDO,
            SagaState.RECONCILE,
        ):
            raise ValueError(
                "outcome state must be VERIFIED, COMPENSATED_UNDO, or RECONCILE"
            )
        return self._transition(
            run_id=run_id,
            task_id=task_id,
            commit_index=commit_index,
            new_state=state,
            action_summary=action_summary,
            evidence=evidence,
            idempotency_key=idempotency_key,
        )

    def get(
        self, run_id: str, task_id: str, commit_index: int
    ) -> SagaCommitRecord | None:
        with self._lock:
            return self._records.get((run_id, task_id, commit_index))

    def is_commit_verified(self, run_id: str, task_id: str, commit_index: int) -> bool:
        rec = self.get(run_id, task_id, commit_index)
        return rec is not None and rec.state is SagaState.VERIFIED

    def find_by_idempotency_key(
        self, run_id: str, task_id: str, key: str
    ) -> SagaCommitRecord | None:
        """Return the matching WAL record for *key*, preferring ``VERIFIED``."""
        if not key:
            return None
        matches = [
            rec
            for rec in self.commits_for(run_id, task_id)
            if rec.idempotency_key and rec.idempotency_key == key
        ]
        if not matches:
            return None
        for rec in matches:
            if rec.state is SagaState.VERIFIED:
                return rec
        return matches[-1]

    def commits_for(self, run_id: str, task_id: str) -> list[SagaCommitRecord]:
        with self._lock:
            matched = [
                rec
                for (r, t, _), rec in self._records.items()
                if r == run_id and t == task_id
            ]
        matched.sort(key=lambda item: item.commit_index)
        return matched

    def resume_point(self, run_id: str, task_id: str) -> int:
        """Return the first commit index ``k >= 1`` not yet ``VERIFIED``."""
        commits = self.commits_for(run_id, task_id)
        by_idx = {rec.commit_index: rec for rec in commits}
        k = 1
        while k in by_idx and by_idx[k].state is SagaState.VERIFIED:
            k += 1
        return k

    def pending_effects_for(self, run_id: str, task_id: str) -> list[SagaCommitRecord]:
        """Return commits currently in ``PENDING_EFFECT`` for ``(run_id, task_id)``."""
        return [
            rec
            for rec in self.commits_for(run_id, task_id)
            if rec.state is SagaState.PENDING_EFFECT
        ]

    def pending_reconcile(self, run_id: str, task_id: str) -> list[SagaCommitRecord]:
        """Return commits left in ``PENDING_EFFECT`` or ``RECONCILE`` after a halt."""
        return [
            rec
            for rec in self.commits_for(run_id, task_id)
            if rec.state in (SagaState.PENDING_EFFECT, SagaState.RECONCILE)
        ]


@dataclass
class AppCartridge:
    """Learned language-agnostic commit signatures, reopen paths, and micro-skills."""

    cartridge_id: str
    commit_signatures: list[dict[str, Any]] = field(default_factory=list)
    reopen_paths: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    micro_skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    non_terminal_statuses: list[str] = field(
        default_factory=lambda: list(_DEFAULT_NON_TERMINAL_STATUSES)
    )
    version: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "cartridge_id": self.cartridge_id,
            "commit_signatures": list(self.commit_signatures),
            "reopen_paths": {k: list(v) for k, v in sorted(self.reopen_paths.items())},
            "micro_skills": {k: dict(v) for k, v in sorted(self.micro_skills.items())},
            "non_terminal_statuses": list(self.non_terminal_statuses),
            "version": self.version,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> AppCartridge:
        return cls(
            cartridge_id=str(data["cartridge_id"]),
            commit_signatures=[
                dict(item) for item in (data.get("commit_signatures") or [])
            ],
            reopen_paths={
                str(k): [dict(step) for step in v]
                for k, v in (data.get("reopen_paths") or {}).items()
            },
            micro_skills={
                str(k): dict(v) for k, v in (data.get("micro_skills") or {}).items()
            },
            non_terminal_statuses=list(
                data.get("non_terminal_statuses") or _DEFAULT_NON_TERMINAL_STATUSES
            ),
            version=int(data.get("version", 1)),
        )


def _merge_cartridge_pair(base: AppCartridge, incoming: AppCartridge) -> AppCartridge:
    seen_sigs = {_sha256_hex(sig): sig for sig in base.commit_signatures}
    for sig in incoming.commit_signatures:
        seen_sigs[_sha256_hex(sig)] = dict(sig)
    merged_reopen = {k: list(v) for k, v in base.reopen_paths.items()}
    for k, steps in incoming.reopen_paths.items():
        merged_reopen[k] = [dict(step) for step in steps]
    merged_skills = {k: dict(v) for k, v in base.micro_skills.items()}
    for k, skill in incoming.micro_skills.items():
        merged_skills[k] = dict(skill)
    merged_statuses = list(
        dict.fromkeys(
            [s.lower() for s in base.non_terminal_statuses]
            + [s.lower() for s in incoming.non_terminal_statuses]
        )
    )
    return AppCartridge(
        cartridge_id=base.cartridge_id,
        commit_signatures=list(seen_sigs.values()),
        reopen_paths=merged_reopen,
        micro_skills=merged_skills,
        non_terminal_statuses=merged_statuses,
        version=max(base.version, incoming.version) + 1,
    )


class AtomicCartridgeStore:
    """Concurrency-safe cartridge store using ``fcntl.flock`` + ``os.replace``."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._lock = threading.RLock()
        self._memory: dict[str, AppCartridge] = {}
        if self.path is not None and self.path.exists():
            self._memory = self._read_disk_unlocked()

    def _lock_path(self) -> Path:
        assert self.path is not None
        return self.path.parent / f".{self.path.name}.lock"

    def _read_disk_unlocked(self) -> dict[str, AppCartridge]:
        assert self.path is not None
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        entries = raw.get("cartridges", {}) if isinstance(raw, Mapping) else {}
        out: dict[str, AppCartridge] = {}
        for cid, body in entries.items():
            if isinstance(body, Mapping):
                out[str(cid)] = AppCartridge.from_json(body)
        return out

    def _write_disk_unlocked(self, data: Mapping[str, AppCartridge]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema": "openadapt.cartridge_store/v1",
                "cartridges": {k: v.to_json() for k, v in sorted(data.items())},
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                _durable_fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _with_file_lock(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self.path is None or fcntl is None:
                return fn()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(
                str(self._lock_path()),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    return fn()
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def load(self) -> dict[str, AppCartridge]:
        def _op() -> dict[str, AppCartridge]:
            if self.path is not None:
                self._memory = self._read_disk_unlocked()
            return {k: copy.deepcopy(v) for k, v in self._memory.items()}

        return self._with_file_lock(_op)

    def get(self, cartridge_id: str) -> AppCartridge | None:
        return self.load().get(cartridge_id)

    def merge_cartridge(self, cartridge: AppCartridge) -> AppCartridge:
        def _op() -> AppCartridge:
            current = (
                self._read_disk_unlocked()
                if self.path is not None
                else dict(self._memory)
            )
            existing = current.get(cartridge.cartridge_id)
            merged = (
                _merge_cartridge_pair(existing, cartridge)
                if existing is not None
                else copy.deepcopy(cartridge)
            )
            current[cartridge.cartridge_id] = merged
            self._memory = current
            if self.path is not None:
                self._write_disk_unlocked(current)
            return copy.deepcopy(merged)

        return self._with_file_lock(_op)

    def reset(self, isolation_mode: str = "iid_ephemeral") -> None:
        """Wipe store on reset in ``iid_ephemeral`` mode; keep in ``continual_persistent``."""
        if isolation_mode == "continual_persistent":
            return

        def _op() -> None:
            self._memory.clear()
            if self.path is not None and self.path.exists():
                self._write_disk_unlocked({})

        self._with_file_lock(_op)


@dataclass
class EpisodeState:
    """Thread-safe per-``(run_id, task_id)`` supervision state."""

    run_id: str
    task_id: str
    trusted_origins: set[str] = field(default_factory=set)
    observed_entities: dict[str, SpanAnchor] = field(default_factory=dict)
    dirty_fields: dict[str, Any] = field(default_factory=dict)
    accrued_obligations: list[Obligation] = field(default_factory=list)
    pagination_state: dict[str, Any] = field(default_factory=dict)
    saga_commits: list[SagaCommitRecord] = field(default_factory=list)
    human_touches: int = 0
    repairs_by_commit: dict[int, int] = field(default_factory=dict)
    intervention_log: list[InterventionNote] = field(default_factory=list)
    receipts: list[TransducerReceipt] = field(default_factory=list)
    step_count: int = 0
    next_commit_index: int = 1
    pending_verification: bool = False
    verification_subgoal_issued: bool = False
    last_reopen_steps: list[dict[str, Any]] = field(default_factory=list)
    crash_resume_delivered: bool = False
    loaded_from_cold_wal: bool = False
    has_seen_frame: bool = False
    action_cls: type[Any] | None = None
    task_instruction_text: str = ""
    task_instruction_indexed: str = ""
    source_items: list[SourceItem] = field(default_factory=list)
    source_item_ids: set[str] = field(default_factory=set)
    task_scope_entities: set[str] = field(default_factory=set)
    required_task_anchors: list[str] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def record_entity(self, anchor: SpanAnchor) -> None:
        with self._lock:
            self.observed_entities[anchor.key] = anchor

    def accrue_obligation(self, obligation: Obligation) -> None:
        with self._lock:
            for existing in self.accrued_obligations:
                if existing.obligation_id == obligation.obligation_id:
                    return
            self.accrued_obligations.append(obligation)

    def unverified_obligations(self) -> list[Obligation]:
        with self._lock:
            return [ob for ob in self.accrued_obligations if not ob.verified]


def _is_exact_key(key: str, value: str, anchor: SpanAnchor | None) -> bool:
    if anchor is not None and anchor.exact_required:
        return True
    lower = key.strip().lower()
    if lower in _EXACT_ID_NAMES or any(
        lower.endswith(sfx) for sfx in _EXACT_ID_SUFFIXES
    ):
        return True
    if len(value.strip()) < 4:
        return True
    return False


class SimplexTransducer:
    """Unified supervision wrapper for any ``BenchmarkAgent`` exposing ``.act``.

    Enforces per-``(run_id, task_id)`` state isolation, origin/authority
    confinement, exact cross-surface identity joins, dirty-buffer checks,
    write-ahead saga logging, bounded self-repair hints, optional human
    decision resolution, and post-commit verification before releasing ``DONE``.
    """

    def __init__(
        self,
        agent: Any,
        *,
        config: SupervisorConfig | None = None,
        decision_resolver: Callable[
            [DecisionRequest], DecisionResponse | Mapping[str, Any] | bool
        ]
        | None = None,
        vault: OpaqueCredentialVault | None = None,
        wal: WriteAheadSagaLog | None = None,
        cartridge_store: AtomicCartridgeStore | None = None,
        cartridge: AppCartridge | None = None,
        rung3_verifier: Callable[[Any, Any, EpisodeState], dict[str, Any] | bool]
        | None = None,
        rung2_verifier: Callable[[Any, Any, EpisodeState], dict[str, Any] | bool]
        | None = None,
        reopen_executor: Callable[
            [list[dict[str, Any]], Any, EpisodeState], dict[str, Any] | None
        ]
        | None = None,
        pinned: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(getattr(agent, "act", None)):
            raise TypeError(
                "SimplexTransducer requires an agent exposing a callable .act method"
            )
        self.__wrapped__ = agent
        self.config = config or SupervisorConfig()
        self.decision_resolver = decision_resolver
        self.vault = vault or OpaqueCredentialVault()
        self.wal = wal or WriteAheadSagaLog(
            Path(self.config.wal_dir) / "saga_wal.jsonl"
            if self.config.wal_dir is not None
            else None
        )
        self.cartridge_store = cartridge_store or AtomicCartridgeStore(
            self.config.cartridge_path
        )
        if cartridge is not None:
            self.cartridge_store.merge_cartridge(cartridge)
        self.rung3_verifier = rung3_verifier
        self.rung2_verifier = rung2_verifier
        self.reopen_executor = reopen_executor
        self.pinned: dict[str, Any] = dict(pinned or {})
        self._states_lock = threading.RLock()
        self._states: dict[tuple[str, str], EpisodeState] = {}
        self._last_receipt_lock = threading.RLock()
        self._last_receipt: TransducerReceipt | None = None

    @property
    def last_receipt(self) -> TransducerReceipt | None:
        with self._last_receipt_lock:
            return self._last_receipt

    def _set_last_receipt(self, receipt: TransducerReceipt) -> None:
        with self._last_receipt_lock:
            self._last_receipt = receipt

    def get_state(self, run_id: str, task_id: str) -> EpisodeState:
        key = (run_id, task_id)
        with self._states_lock:
            state = self._states.get(key)
            if state is None:
                state = EpisodeState(run_id=run_id, task_id=task_id)
                # Recover resume point from WAL if prior commits exist.
                state.saga_commits = self.wal.commits_for(run_id, task_id)
                state.loaded_from_cold_wal = bool(state.saga_commits)
                state.next_commit_index = self.wal.resume_point(run_id, task_id)
                for k, v in self.pinned.items():
                    redacted = str(self.vault.redact(str(v)))
                    state.record_entity(
                        SpanAnchor(
                            key=str(k),
                            value=redacted,
                            entity_type="pinned",
                            source_surface="pinned_config",
                            trust_level=TrustLevel.TRUSTED_INSTRUCTION,
                            exact_required=True,
                        )
                    )
                self._states[key] = state
            return state

    def reset(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Reset episode state and respect ``isolation_mode`` on cartridge store."""
        with self._states_lock:
            if run_id is not None and task_id is not None:
                self._states.pop((run_id, task_id), None)
            elif run_id is not None:
                for key in list(self._states.keys()):
                    if key[0] == run_id:
                        self._states.pop(key, None)
            else:
                self._states.clear()
        self.cartridge_store.reset(self.config.isolation_mode)
        inner_reset = getattr(self.__wrapped__, "reset", None)
        if callable(inner_reset):
            sig = inspect.signature(inner_reset)
            if kwargs and any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                inner_reset(**kwargs)
            else:
                inner_reset()

    def _resolve_ids(self, observation: Any, task: Any) -> tuple[str, str]:
        run_id = str(
            _extract_attr_or_key(task, "run_id", None)
            or _extract_attr_or_key(observation, "run_id", None)
            or "default-run"
        )
        task_id = str(
            _extract_attr_or_key(task, "task_id", None)
            or _extract_attr_or_key(task, "id", None)
            or _extract_attr_or_key(observation, "task_id", None)
            or "default-task"
        )
        return run_id, task_id

    def _ingest_context(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
    ) -> None:
        with state._lock:
            # Trusted origins from task
            origins = _extract_attr_or_key(task, "trusted_origins", None)
            if isinstance(origins, (list, tuple, set, frozenset)):
                for origin in origins:
                    if origin:
                        state.trusted_origins.add(str(origin).strip().lower())
            single_origin = _extract_attr_or_key(task, "origin", None)
            if single_origin:
                state.trusted_origins.add(str(single_origin).strip().lower())

            # Trusted entities from task
            task_entities = _extract_attr_or_key(task, "entities", None)
            if isinstance(task_entities, Mapping):
                for k, v in task_entities.items():
                    clean_val = str(self.vault.redact(str(v)))
                    state.observed_entities[str(k)] = SpanAnchor(
                        key=str(k),
                        value=clean_val,
                        source_surface="task",
                        trust_level=TrustLevel.TRUSTED_INSTRUCTION,
                        step_index=state.step_count,
                        exact_required=_is_exact_key(str(k), clean_val, None),
                    )

            # Event-driven obligations from task
            raw_obs = _extract_attr_or_key(task, "obligations", None)
            if isinstance(raw_obs, Sequence) and not isinstance(raw_obs, (str, bytes)):
                for idx, item in enumerate(raw_obs):
                    if isinstance(item, Obligation):
                        state.accrue_obligation(item)
                    elif isinstance(item, Mapping):
                        kind_raw = str(item.get("kind", "Record"))
                        try:
                            kind = ObligationKind(kind_raw)
                        except ValueError:
                            kind = ObligationKind.RECORD
                        state.accrue_obligation(
                            Obligation(
                                obligation_id=str(
                                    item.get("obligation_id", f"ob-{idx + 1}")
                                ),
                                kind=kind,
                                target=str(item.get("target", "")),
                                expected_state=dict(item.get("expected_state") or {}),
                            )
                        )

            # Observation entities, dirty fields, pagination
            obs_entities = _extract_attr_or_key(observation, "entities", None)
            obs_origin = str(_extract_attr_or_key(observation, "origin", "") or "")
            obs_surface = str(_extract_attr_or_key(observation, "surface", "") or "")
            obs_trust_raw = _extract_attr_or_key(
                observation, "trust_level", TrustLevel.TRUSTED_INSTRUCTION.value
            )
            obs_trust = (
                TrustLevel.UNTRUSTED_DATA
                if str(obs_trust_raw).upper() == TrustLevel.UNTRUSTED_DATA.value
                else TrustLevel.TRUSTED_INSTRUCTION
            )
            if isinstance(obs_entities, Mapping):
                for k, v in obs_entities.items():
                    # Never let UNTRUSTED_DATA overwrite a TRUSTED_INSTRUCTION anchor
                    existing = state.observed_entities.get(str(k))
                    if (
                        existing is not None
                        and existing.trust_level is TrustLevel.TRUSTED_INSTRUCTION
                        and obs_trust is TrustLevel.UNTRUSTED_DATA
                    ):
                        continue
                    clean_val = str(self.vault.redact(str(v)))
                    state.observed_entities[str(k)] = SpanAnchor(
                        key=str(k),
                        value=clean_val,
                        source_surface=obs_surface,
                        source_origin=obs_origin,
                        trust_level=obs_trust,
                        step_index=state.step_count,
                        exact_required=_is_exact_key(str(k), clean_val, existing),
                    )

            dirty = _extract_attr_or_key(observation, "dirty_fields", None)
            if isinstance(dirty, Mapping):
                state.dirty_fields = {
                    str(k): self.vault.redact(v) for k, v in dirty.items()
                }
            pagination = _extract_attr_or_key(observation, "pagination", None)
            if isinstance(pagination, Mapping):
                state.pagination_state.update(
                    {str(k): self.vault.redact(v) for k, v in pagination.items()}
                )

            # Index task instruction, explicit required anchors, and structured
            # multi-container source provenance for contextual scope checks.
            self._ingest_task_and_containers_unlocked(state, observation, task)

    def _index_source_item_unlocked(
        self,
        state: EpisodeState,
        *,
        container_id: str,
        raw_item: Any,
    ) -> None:
        explicit_entities: list[str] = []
        if isinstance(raw_item, Mapping):
            text_parts: list[str] = []
            for key in (
                "subject",
                "title",
                "name",
                "sender",
                "author",
                "person",
                "recipient",
                "owner",
            ):
                val = raw_item.get(key)
                if isinstance(val, str) and val.strip():
                    clean_v = str(self.vault.redact(val.strip()))
                    explicit_entities.append(clean_v)
                    text_parts.append(clean_v)
            for list_key in ("participants", "entities", "people", "recipients"):
                vals = raw_item.get(list_key)
                if isinstance(vals, Sequence) and not isinstance(vals, (str, bytes)):
                    for item_v in vals:
                        if isinstance(item_v, str) and item_v.strip():
                            clean_v = str(self.vault.redact(item_v.strip()))
                            explicit_entities.append(clean_v)
            for body_key in (
                "text",
                "content",
                "body",
                "summary",
                "description",
                "value",
            ):
                val = raw_item.get(body_key)
                if isinstance(val, str) and val.strip():
                    text_parts.append(str(self.vault.redact(val.strip())))
            text = (
                " | ".join(dict.fromkeys(text_parts))
                if text_parts
                else str(
                    self.vault.redact(json.dumps(raw_item, sort_keys=True, default=str))
                )
            )
        else:
            text = str(self.vault.redact(str(raw_item))).strip()

        if not text:
            return
        item_id = _sha256_hex({"container": container_id, "text": text})[:20]
        if item_id in state.source_item_ids:
            return
        state.source_item_ids.add(item_id)

        raw_entities: list[str] = []
        seen_low: set[str] = set()
        for ent in explicit_entities + _extract_proper_nouns(text):
            low = ent.strip().lower()
            if low and low not in seen_low and low not in _GRAMMATICAL_STOPWORDS:
                seen_low.add(low)
                raw_entities.append(ent.strip())
        state.source_items.append(
            SourceItem(
                item_id=item_id,
                container_id=container_id,
                text=text,
                entities=seen_low,
                raw_entities=raw_entities,
                is_task_anchored=False,
            )
        )

    def _ingest_containers_unlocked(self, state: EpisodeState, containers: Any) -> None:
        if isinstance(containers, Mapping):
            for cid, body in containers.items():
                cid_str = str(cid).strip() or "container"
                if isinstance(body, Mapping):
                    nested = None
                    for list_key in (
                        "items",
                        "records",
                        "messages",
                        "notes",
                        "events",
                        "entries",
                        "emails",
                    ):
                        cand = body.get(list_key)
                        if isinstance(cand, Sequence) and not isinstance(
                            cand, (str, bytes)
                        ):
                            nested = cand
                            break
                    if nested is not None:
                        for item in nested:
                            self._index_source_item_unlocked(
                                state, container_id=cid_str, raw_item=item
                            )
                    else:
                        self._index_source_item_unlocked(
                            state, container_id=cid_str, raw_item=body
                        )
                elif isinstance(body, Sequence) and not isinstance(body, (str, bytes)):
                    for item in body:
                        self._index_source_item_unlocked(
                            state, container_id=cid_str, raw_item=item
                        )
                elif isinstance(body, str) and body.strip():
                    chunks = [
                        p.strip(" -*\t")
                        for p in re.split(r"\n\s*\n|\n(?=\s*[-*•]\s+)", body.strip())
                        if p.strip(" -*\t")
                    ]
                    for chunk in chunks:
                        self._index_source_item_unlocked(
                            state, container_id=cid_str, raw_item=chunk
                        )
        elif isinstance(containers, Sequence) and not isinstance(
            containers, (str, bytes)
        ):
            for idx, entry in enumerate(containers):
                if isinstance(entry, Mapping):
                    cid_str = str(
                        entry.get("container_id")
                        or entry.get("id")
                        or entry.get("name")
                        or entry.get("source")
                        or entry.get("app")
                        or f"container-{idx + 1}"
                    ).strip()
                    nested = None
                    for list_key in (
                        "items",
                        "records",
                        "messages",
                        "notes",
                        "events",
                        "entries",
                        "emails",
                    ):
                        cand = entry.get(list_key)
                        if isinstance(cand, Sequence) and not isinstance(
                            cand, (str, bytes)
                        ):
                            nested = cand
                            break
                    if nested is not None:
                        for item in nested:
                            self._index_source_item_unlocked(
                                state, container_id=cid_str, raw_item=item
                            )
                    else:
                        self._index_source_item_unlocked(
                            state, container_id=cid_str, raw_item=entry
                        )

    def _ingest_task_and_containers_unlocked(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
    ) -> None:
        raw_cfg = _extract_attr_or_key(task, "raw_config", None)
        instr = str(_extract_attr_or_key(task, "instruction", "") or "").strip()
        task_prompt = ""
        if isinstance(raw_cfg, Mapping):
            task_prompt = str(raw_cfg.get("task_prompt") or "").strip()
        full_instr = instr
        if task_prompt and task_prompt not in full_instr:
            full_instr = (
                f"{full_instr}\n{task_prompt}".strip() if full_instr else task_prompt
            )

        if full_instr and full_instr != state.task_instruction_indexed:
            state.task_instruction_indexed = full_instr
            # Parse structured app sections if embedded in instruction (2+ headers).
            lines = full_instr.splitlines()
            headers: list[tuple[int, str]] = []
            for idx, line in enumerate(lines):
                m = _SECTION_HEADER_RE.match(line.strip())
                if m:
                    hdr = next(g for g in m.groups() if g is not None).strip()
                    headers.append((idx, hdr))
            if len(headers) >= 2:
                directive_lines = lines[: headers[0][0]]
                for h_idx, (line_no, hdr_name) in enumerate(headers):
                    end_no = (
                        headers[h_idx + 1][0]
                        if h_idx + 1 < len(headers)
                        else len(lines)
                    )
                    sec_lines = [
                        ln.strip(" -*\t")
                        for ln in lines[line_no + 1 : end_no]
                        if ln.strip(" -*\t")
                    ]
                    for sec_item in sec_lines:
                        self._index_source_item_unlocked(
                            state, container_id=hdr_name, raw_item=sec_item
                        )
                directive_text = "\n".join(directive_lines).strip() or full_instr
            else:
                directive_text = full_instr

            state.task_instruction_text = directive_text
            for pn in _extract_proper_nouns(directive_text):
                state.task_scope_entities.add(pn.lower())
            for anchor in _extract_required_anchors(directive_text):
                if anchor.lower() not in {
                    a.lower() for a in state.required_task_anchors
                }:
                    state.required_task_anchors.append(anchor)
                state.task_scope_entities.add(anchor.lower())

        # Keep trusted observed_entities in task_scope_entities
        for anchor_obj in state.observed_entities.values():
            if anchor_obj.trust_level is TrustLevel.TRUSTED_INSTRUCTION:
                val_low = str(anchor_obj.value).strip().lower()
                if val_low:
                    state.task_scope_entities.add(val_low)

        # Index multi-container observations from obs.raw_observation or obs dict
        raw_obs = _extract_attr_or_key(observation, "raw_observation", None)
        if isinstance(raw_obs, Mapping) and "containers" in raw_obs:
            self._ingest_containers_unlocked(state, raw_obs.get("containers"))
        elif isinstance(observation, Mapping) and "containers" in observation:
            self._ingest_containers_unlocked(state, observation.get("containers"))

        # Classify source items: anchored if co-located with any task scope entity
        # or required task anchor.
        if state.source_items:
            instr_low = state.task_instruction_text.lower()
            scope_entities = set(state.task_scope_entities)
            req_norm = [
                _normalize_anchor_for_match(a) for a in state.required_task_anchors
            ]
            for item in state.source_items:
                text_low = item.text.lower()
                norm_text = _normalize_anchor_for_match(item.text)
                anchored = False
                for se in scope_entities:
                    if not se:
                        continue
                    if se in item.entities:
                        anchored = True
                        break
                    if len(se) >= 3 and re.search(rf"\b{re.escape(se)}\b", text_low):
                        anchored = True
                        break
                if not anchored and req_norm:
                    for rn in req_norm:
                        if rn and rn in norm_text:
                            anchored = True
                            break
                if not anchored and instr_low:
                    for ent in item.entities:
                        if len(ent) >= 3 and re.search(
                            rf"\b{re.escape(ent)}\b", instr_low
                        ):
                            anchored = True
                            break
                item.is_task_anchored = anchored

    def _call_inner_act(
        self,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        inner_act = self.__wrapped__.act
        sig = inspect.signature(inner_act)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        # Pass a shallow copy of history so inner agent cannot mutate caller's prefix.
        safe_history = list(history) if history is not None else None
        if len(params) >= 3 or any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
        ):
            return inner_act(observation, task, safe_history)
        return inner_act(observation, task)

    def _deliver_intervention(
        self,
        state: EpisodeState,
        *,
        kind: str,
        hint: str,
        delta_note: str,
        fast_forward_obs: Any = None,
    ) -> None:
        safe_hint = str(self.vault.redact(hint))
        safe_delta = str(self.vault.redact(delta_note))
        with state._lock:
            state.intervention_log.append(
                InterventionNote(
                    kind=kind,
                    hint=safe_hint,
                    delta_note=safe_delta,
                    step_index=state.step_count,
                )
            )
        hook = getattr(self.__wrapped__, "on_supervisor_intervention", None)
        if callable(hook):
            hook(
                hint=safe_hint,
                delta_note=safe_delta,
                fast_forward_obs=fast_forward_obs,
            )

    def _active_cartridges(self) -> list[AppCartridge]:
        return list(self.cartridge_store.load().values())

    def _is_commit_boundary(self, action: Any) -> bool:
        """Language-agnostic commit boundary detection."""
        raw = _action_raw_dict(action)
        # 1. Explicit action metadata
        if _extract_attr_or_key(action, "is_commit", False) is True:
            return True
        if raw.get("is_commit") is True:
            return True
        # 2. Attached wire / OS mutation signals
        if _extract_attr_or_key(action, "wire_mutation", False) is True:
            return True
        if _extract_attr_or_key(action, "os_mutation", False) is True:
            return True
        if raw.get("wire_mutation") is True or raw.get("os_mutation") is True:
            return True
        http_method = str(raw.get("http_method") or "").strip().upper()
        if http_method in _MUTATING_WIRE_METHODS:
            return True
        if raw.get("fs_write") is True:
            return True
        parsed_json = raw.get("parsed_json")
        if (
            isinstance(parsed_json, Mapping)
            and parsed_json.get("shared_content") is not None
        ):
            return True
        if raw.get("shared_content") is not None:
            return True
        # 3. Structural AppCartridge signature match (zero English button regexes)
        for cart in self._active_cartridges():
            for sig in cart.commit_signatures:
                if self._matches_signature(action, raw, sig):
                    return True
        return False

    @staticmethod
    def _matches_signature(
        action: Any, raw: Mapping[str, Any], signature: Mapping[str, Any]
    ) -> bool:
        if not signature:
            return False
        for key, expected in signature.items():
            actual = raw.get(key, _extract_attr_or_key(action, key, None))
            if actual != expected:
                return False
        return True

    def _is_terminal_action(self, action: Any) -> bool:
        return _action_type_str(action) in _TERMINAL_ACTION_TYPES

    def _check_pre_commit(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
    ) -> tuple[str | None, str]:
        """Run before-click / before-commit checks.

        Returns ``(trap_code, reason)`` or ``(None, "")`` when clear.
        """
        raw = _action_raw_dict(action)

        # 1. Origin / authority confinement (indirect prompt injection defense)
        source_trust = str(
            _extract_attr_or_key(action, "source_trust", None)
            or raw.get("source_trust")
            or raw.get("instruction_source")
            or TrustLevel.TRUSTED_INSTRUCTION.value
        ).upper()
        if source_trust == TrustLevel.UNTRUSTED_DATA.value:
            return (
                "UNTRUSTED_INSTRUCTION_ORIGIN",
                "Action originated from UNTRUSTED_DATA; blocked to prevent indirect prompt injection.",
            )

        target_origin = (
            str(
                _extract_attr_or_key(action, "target_origin", None)
                or raw.get("target_origin")
                or raw.get("origin")
                or ""
            )
            .strip()
            .lower()
        )
        with state._lock:
            trusted_origins = set(state.trusted_origins)
            anchors = dict(state.observed_entities)
            dirty_fields = dict(state.dirty_fields)

        if target_origin and trusted_origins and target_origin not in trusted_origins:
            return (
                "UNTRUSTED_TARGET_ORIGIN",
                f"Target origin {target_origin!r} is outside trusted origins {sorted(trusted_origins)!r}.",
            )

        # 2. Cross-surface identity join (100% exact on IDs and <4-char codes)
        action_entities = (
            _extract_attr_or_key(action, "entities", None)
            or raw.get("entities")
            or raw.get("target_entities")
        )
        if isinstance(action_entities, Mapping):
            for key, actual_val in action_entities.items():
                key_str = str(key)
                anchor = anchors.get(key_str)
                if anchor is None:
                    continue
                expected_str = str(anchor.value)
                actual_str = str(actual_val)
                if _is_exact_key(key_str, expected_str, anchor):
                    if actual_str != expected_str:
                        return (
                            "IDENTITY_MISMATCH",
                            f"Exact match failed for {key_str!r}: expected {expected_str!r}, got {actual_str!r}.",
                        )
                else:
                    if actual_str.strip().lower() != expected_str.strip().lower():
                        return (
                            "IDENTITY_MISMATCH",
                            f"Entity mismatch for {key_str!r}: expected {expected_str!r}, got {actual_str!r}.",
                        )

        # 3. Dirty-buffer check
        if dirty_fields and (
            raw.get("discards_dirty_buffer") is True
            or raw.get("navigate_away") is True
            or self._is_terminal_action(action)
        ):
            return (
                "DIRTY_BUFFER",
                f"Unsaved dirty fields present: {sorted(dirty_fields.keys())}.",
            )

        # 4. Conformal uncertainty veto
        if self.config.enable_conformal_veto:
            err_prob = raw.get(
                "semantic_error_prob",
                _extract_attr_or_key(action, "semantic_error_prob", None),
            )
            if err_prob is not None and float(err_prob) > self.config.max_semantic_fpr:
                return (
                    "CONFORMAL_VETO",
                    f"Estimated error probability {float(err_prob):.4f} exceeds max_semantic_fpr {self.config.max_semantic_fpr:.4f}.",
                )

        # 5. Unexpected focus-steal modal guard (native BenchmarkObservation signals)
        if not self._is_terminal_action(action):
            modal_trap = self._check_focus_steal_modal(state, action, observation)
            if modal_trap is not None:
                return modal_trap

        # 6. General contextual integrity & scope provenance guard
        scope_trap = self._check_contextual_integrity(state, action)
        if scope_trap is not None:
            return scope_trap

        return None, ""

    def _check_focus_steal_modal(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
    ) -> tuple[str, str] | None:
        modals = _detect_blocking_modals(observation)
        if not modals:
            return None
        with state._lock:
            instr = state.task_instruction_text
        unexpected = [
            m for m in modals if not _modal_referenced_in_instruction(m["title"], instr)
        ]
        if not unexpected:
            return None

        raw = _action_raw_dict(action)
        if (
            _extract_attr_or_key(action, "dismiss_modal", False) is True
            or raw.get("dismiss_modal") is True
        ):
            return None
        key = (
            str(_extract_attr_or_key(action, "key", None) or raw.get("key") or "")
            .strip()
            .lower()
        )
        if key == "escape":
            return None

        target_node_id = str(
            _extract_attr_or_key(action, "target_node_id", None)
            or raw.get("target_node_id")
            or ""
        ).strip()
        target_name = (
            str(
                _extract_attr_or_key(action, "target_name", None)
                or raw.get("target_name")
                or ""
            )
            .strip()
            .lower()
        )

        for modal in unexpected:
            if target_node_id and target_node_id in modal["subtree_ids"]:
                return None
            if target_name and target_name in modal["subtree_names"]:
                return None

        modal_titles = [m["title"] for m in unexpected]
        return (
            "FOCUS_STEAL_MODAL",
            f"Unexpected blocking modal dialog {modal_titles!r} has stolen focus over "
            "the target application. Dismiss the modal dialog first before committing.",
        )

    def _extract_outbound_payload(self, action: Any) -> Any:
        raw = _action_raw_dict(action)
        parsed = raw.get("parsed_json")
        if isinstance(parsed, Mapping) and parsed.get("shared_content") is not None:
            return parsed["shared_content"]
        if raw.get("shared_content") is not None:
            return raw["shared_content"]
        answer = _extract_attr_or_key(action, "answer", None)
        if isinstance(answer, str) and answer.strip():
            return answer
        text = _extract_attr_or_key(action, "text", None)
        if isinstance(text, str) and text.strip() and self._is_commit_boundary(action):
            return text
        return None

    def _check_contextual_integrity(
        self,
        state: EpisodeState,
        action: Any,
    ) -> tuple[str, str] | None:
        payload = self._extract_outbound_payload(action)
        if payload is None:
            return None
        segments = _split_outbound_segments(payload)
        if not segments:
            return None
        full_text = " ".join(segments)
        norm_full = _normalize_anchor_for_match(full_text)

        with state._lock:
            required_anchors = list(state.required_task_anchors)
            source_items = list(state.source_items)
            task_scope = set(state.task_scope_entities)
            instr_text = state.task_instruction_text
            trusted_obs_vals = {
                str(a.value).strip().lower()
                for a in state.observed_entities.values()
                if a.trust_level is TrustLevel.TRUSTED_INSTRUCTION
                and str(a.value).strip()
            }

        # 1. MISSING_TASK_ANCHOR: explicit requested anchors omitted from outbound payload
        if required_anchors:
            missing = [
                anchor
                for anchor in required_anchors
                if _normalize_anchor_for_match(anchor) not in norm_full
            ]
            if missing:
                return (
                    "MISSING_TASK_ANCHOR",
                    f"Outbound payload omitted required task anchor(s): {missing!r}. "
                    "Include all requested task anchors while keeping out-of-scope records excluded.",
                )

        # 2. CONTEXTUAL_SCOPE_BLEED: out-of-scope provenance + unreferenced entity
        if not source_items:
            return None
        out_of_scope_sources = [it for it in source_items if not it.is_task_anchored]
        if not out_of_scope_sources:
            return None

        in_scope_entities: set[str] = set(task_scope) | trusted_obs_vals
        in_scope_texts: list[str] = [instr_text.lower()]
        for it in source_items:
            if it.is_task_anchored:
                in_scope_entities.update(it.entities)
                in_scope_texts.append(it.text.lower())
        in_scope_corpus = " ".join(in_scope_texts)

        def _is_in_scope_entity(ent_low: str) -> bool:
            if ent_low in in_scope_entities:
                return True
            if any(
                ent_low in existing or existing in ent_low
                for existing in in_scope_entities
                if len(existing) >= 3
            ):
                return True
            if len(ent_low) >= 3 and re.search(
                rf"\b{re.escape(ent_low)}\b", in_scope_corpus
            ):
                return True
            return False

        bleeding_snippets: list[str] = []
        offending_entities: list[str] = []
        offending_containers: list[str] = []

        for seg in segments:
            seg_low = seg.lower()
            seg_pns = _extract_proper_nouns(seg)
            matched_ents: list[str] = []
            matched_cids: list[str] = []
            for src in out_of_scope_sources:
                src_low = src.text.lower()
                # Check proper nouns introduced by the outbound segment
                for pn in seg_pns:
                    pn_low = pn.lower()
                    if _is_in_scope_entity(pn_low):
                        continue
                    if pn_low in src.entities or (
                        len(pn_low) >= 3
                        and re.search(rf"\b{re.escape(pn_low)}\b", src_low)
                    ):
                        if pn not in matched_ents:
                            matched_ents.append(pn)
                        if src.container_id not in matched_cids:
                            matched_cids.append(src.container_id)
                # Check entities indexed on the out-of-scope source item
                for src_ent in src.raw_entities:
                    ent_low = src_ent.lower()
                    if _is_in_scope_entity(ent_low):
                        continue
                    if len(ent_low) >= 3 and re.search(
                        rf"\b{re.escape(ent_low)}\b", seg_low
                    ):
                        if src_ent not in matched_ents:
                            matched_ents.append(src_ent)
                        if src.container_id not in matched_cids:
                            matched_cids.append(src.container_id)
            if matched_ents:
                snippet = seg if len(seg) <= 96 else f"{seg[:93]}..."
                bleeding_snippets.append(snippet)
                for e in matched_ents:
                    if e not in offending_entities:
                        offending_entities.append(e)
                for c in matched_cids:
                    if c not in offending_containers:
                        offending_containers.append(c)

        if bleeding_snippets:
            return (
                "CONTEXTUAL_SCOPE_BLEED",
                f"Outbound payload includes out-of-scope item(s) from unrelated "
                f"container(s) {offending_containers!r} introducing unreferenced "
                f"entity/entities {offending_entities!r}: {bleeding_snippets!r}. "
                "Remove ONLY these out-of-scope item(s) while keeping all required task items.",
            )
        return None

    def _abstain_action(
        self,
        state: EpisodeState,
        *,
        trap_code: str,
        reason: str,
        commit_index: int,
    ) -> TransducerAction:
        safe_reason = str(self.vault.redact(reason))
        receipt = self._emit_receipt(
            state=state,
            outcome="HALTED",
            oracle_tier=0,
            observed_effect_strength=None,
            authorization_passed=(
                trap_code
                not in ("UNTRUSTED_INSTRUCTION_ORIGIN", "UNTRUSTED_TARGET_ORIGIN")
            ),
            identity_passed=(trap_code != "IDENTITY_MISMATCH"),
            postcondition_passed=False,
            effect_passed=False,
            delivery_uncertain=False,
            commit_index=commit_index,
            reason=safe_reason,
        )
        return TransducerAction(
            type="error",
            raw_action={
                "outcome": "abstained",
                "reported_success": False,
                "trap_code": trap_code,
                "reason": safe_reason,
                "receipt_id": receipt.receipt_id,
            },
            reported_success=False,
            is_commit=False,
        )

    def _emit_receipt(
        self,
        *,
        state: EpisodeState,
        outcome: Literal["VERIFIED", "COMPLETED_UNVERIFIED", "HALTED"],
        oracle_tier: Literal[0, 1, 2, 3],
        observed_effect_strength: str | None,
        authorization_passed: bool,
        identity_passed: bool,
        postcondition_passed: bool,
        effect_passed: bool,
        delivery_uncertain: bool,
        commit_index: int,
        reason: str,
    ) -> TransducerReceipt:
        safe_reason = str(self.vault.redact(reason))
        evidence_payload = {
            "run_id": state.run_id,
            "task_id": state.task_id,
            "commit_index": commit_index,
            "outcome": outcome,
            "oracle_tier": oracle_tier,
            "reason": safe_reason,
            "step_count": state.step_count,
        }
        receipt = TransducerReceipt(
            outcome=outcome,
            receipt_id=f"rcpt-{uuid.uuid4().hex[:16]}",
            execution_id=f"exec-{_sha256_hex([state.run_id, state.task_id])[:16]}",
            run_id=state.run_id,
            task_id=state.task_id,
            config_sha256=self.config.config_sha256,
            oracle_tier=oracle_tier,
            observed_effect_strength=observed_effect_strength,
            minimum_effect_strength=self.config.minimum_effect_strength,
            authorization_passed=authorization_passed,
            identity_passed=identity_passed,
            postcondition_passed=postcondition_passed,
            effect_passed=effect_passed,
            delivery_uncertain=delivery_uncertain,
            evidence_digest=_sha256_hex(evidence_payload),
            issued_at=_utc_now_iso(),
            human_touches=state.human_touches,
            commit_index=commit_index,
            reason=safe_reason,
            environment_id=self.config.environment_id,
            runner_id=self.config.runner_id,
            workflow_version=self.config.workflow_version,
            qualification_id=self.config.qualification_id,
        )
        with state._lock:
            state.receipts.append(receipt)
        self._set_last_receipt(receipt)
        return receipt

    def _resolve_decision(
        self,
        state: EpisodeState,
        *,
        commit_index: int,
        trap_code: str,
        reason: str,
        proposed_action: Any,
        observation: Any,
    ) -> DecisionResponse | None:
        if self.decision_resolver is None:
            return None
        with state._lock:
            state.human_touches += 1
        req = DecisionRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            commit_index=commit_index,
            trap_code=trap_code,
            reason=str(self.vault.redact(reason)),
            proposed_action=proposed_action,
            observation_summary={
                "step_count": state.step_count,
                "dirty_fields": sorted(state.dirty_fields.keys()),
            },
        )
        raw_resp = self.decision_resolver(req)
        if isinstance(raw_resp, DecisionResponse):
            return raw_resp
        if isinstance(raw_resp, bool):
            return DecisionResponse(approved=raw_resp)
        if isinstance(raw_resp, Mapping):
            return DecisionResponse(
                approved=bool(raw_resp.get("approved", False)),
                replacement_action=raw_resp.get("replacement_action"),
                reason=str(raw_resp.get("reason") or ""),
            )
        return DecisionResponse(approved=False, reason="unrecognized resolver response")

    def _non_terminal_statuses(self) -> set[str]:
        statuses = set(_DEFAULT_NON_TERMINAL_STATUSES)
        for cart in self._active_cartridges():
            for item in cart.non_terminal_statuses:
                statuses.add(str(item).strip().lower())
        return statuses

    def _evaluate_verification_frame(self, frame: Any) -> tuple[bool, str, str | None]:
        """Verify non-editable, non-toast persisted delta + frame status.

        Returns ``(verified, reason, observed_effect_strength)``.
        """
        if not isinstance(frame, Mapping):
            # Support object attributes too
            frame = {
                "is_editable": _extract_attr_or_key(frame, "is_editable", None),
                "editable": _extract_attr_or_key(frame, "editable", None),
                "is_toast": _extract_attr_or_key(frame, "is_toast", False),
                "has_delta": _extract_attr_or_key(frame, "has_delta", None),
                "delta": _extract_attr_or_key(frame, "delta", None),
                "record_status": _extract_attr_or_key(frame, "record_status", None),
                "status": _extract_attr_or_key(frame, "status", None),
                "separate_session": _extract_attr_or_key(
                    frame, "separate_session", False
                ),
                "reopened": _extract_attr_or_key(frame, "reopened", False),
            }
        is_toast = bool(frame.get("is_toast", False))
        if is_toast:
            return (
                False,
                "Toast notification alone is not persisted verification.",
                None,
            )
        editable = frame.get("is_editable")
        if editable is None:
            editable = frame.get("editable")
        if editable is True:
            return False, "Editable form view does not prove persisted state.", None

        status_raw = frame.get("record_status")
        if status_raw is None:
            status_raw = frame.get("status")
        status_str = str(status_raw or "").strip().lower()
        if status_str in self._non_terminal_statuses():
            return (
                False,
                f"Persisted record is still in non-terminal state {status_str!r}.",
                None,
            )

        has_delta = frame.get("has_delta")
        if has_delta is None:
            has_delta = bool(frame.get("delta")) or bool(status_str)
        if not has_delta:
            return (
                False,
                "No persisted state delta observed on verification frame.",
                None,
            )

        strength = (
            "independent_session"
            if frame.get("separate_session") is True
            else "persisted_state_reacquisition"
        )
        return (
            True,
            f"Verified persisted frame status={status_str or 'persisted'}.",
            strength,
        )

    def _compile_reopen_path(
        self,
        state: EpisodeState,
        observation: Any,
        reopen_steps: list[dict[str, Any]],
    ) -> None:
        if not self.config.enable_intra_episode_skills or not reopen_steps:
            return
        cartridge_id = str(
            _extract_attr_or_key(observation, "cartridge_id", None)
            or _extract_attr_or_key(observation, "surface", None)
            or "default"
        )
        path_key = str(
            _extract_attr_or_key(observation, "obligation_key", None) or state.task_id
        )
        cart = AppCartridge(
            cartridge_id=cartridge_id,
            reopen_paths={path_key: reopen_steps},
            micro_skills={
                f"reopen:{path_key}": {
                    "steps": reopen_steps,
                    "compiled_at": _utc_now_iso(),
                }
            },
        )
        self.cartridge_store.merge_cartridge(cart)

    def _verify_before_done(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        """Active ``VERIFYING_EFFECT`` gate before releasing ``DONE``."""
        remaining_steps = _extract_attr_or_key(observation, "remaining_steps", None)
        if remaining_steps is None:
            max_steps = _extract_attr_or_key(task, "max_steps", None)
            if max_steps is None:
                max_steps = _extract_attr_or_key(task, "time_limit_steps", None)
            if max_steps is not None:
                remaining_steps = max(0, int(max_steps) - state.step_count)
            else:
                remaining_steps = self.config.min_budget_for_ui_reopen

        commit_idx = max(1, state.next_commit_index - 1)
        has_unverified_work = bool(state.unverified_obligations()) or (
            state.pending_verification
        )

        # 1. Check Rung 3 / Rung 2 attached verifiers first
        if self.rung3_verifier is not None:
            res = self.rung3_verifier(observation, task, state)
            ok = (
                bool(res.get("verified", False))
                if isinstance(res, Mapping)
                else bool(res)
            )
            if ok:
                self._mark_verified(state, commit_idx, tier=2)
                self._emit_receipt(
                    state=state,
                    outcome=ReceiptOutcome.VERIFIED.value,
                    oracle_tier=2,
                    observed_effect_strength="independent_system_of_record",
                    authorization_passed=True,
                    identity_passed=True,
                    postcondition_passed=True,
                    effect_passed=True,
                    delivery_uncertain=False,
                    commit_index=commit_idx,
                    reason="Verified by attached Rung 3 system-of-record check.",
                )
                return action

        if self.rung2_verifier is not None:
            res = self.rung2_verifier(observation, task, state)
            ok = (
                bool(res.get("verified", False))
                if isinstance(res, Mapping)
                else bool(res)
            )
            if ok:
                self._mark_verified(state, commit_idx, tier=1)
                self._emit_receipt(
                    state=state,
                    outcome=ReceiptOutcome.VERIFIED.value,
                    oracle_tier=1,
                    observed_effect_strength="independent_session",
                    authorization_passed=True,
                    identity_passed=True,
                    postcondition_passed=True,
                    effect_passed=True,
                    delivery_uncertain=False,
                    commit_index=commit_idx,
                    reason="Verified by attached Rung 2 independent-session check.",
                )
                return action

        # 2. Check compiled different_path reopen skill in AppCartridge
        if self.reopen_executor is not None:
            for cart in self._active_cartridges():
                for path_key, steps in cart.reopen_paths.items():
                    frame = self.reopen_executor(steps, observation, state)
                    if frame is not None:
                        ok, reason, strength = self._evaluate_verification_frame(frame)
                        if ok and strength is not None:
                            tier: Literal[0, 1, 2, 3] = (
                                1 if strength == "independent_session" else 0
                            )
                            self._mark_verified(state, commit_idx, tier=tier)
                            self._emit_receipt(
                                state=state,
                                outcome=ReceiptOutcome.VERIFIED.value,
                                oracle_tier=tier,
                                observed_effect_strength=strength,
                                authorization_passed=True,
                                identity_passed=True,
                                postcondition_passed=True,
                                effect_passed=True,
                                delivery_uncertain=False,
                                commit_index=commit_idx,
                                reason=f"Verified via compiled reopen path {path_key!r}: {reason}",
                            )
                            return action

        # 3. Check current observation if it already carries a reopened verification frame
        verif_frame = _extract_attr_or_key(observation, "verification_frame", None)
        if verif_frame is not None:
            ok, reason, strength = self._evaluate_verification_frame(verif_frame)
            if ok and strength is not None:
                tier = 1 if strength == "independent_session" else 0
                self._mark_verified(state, commit_idx, tier=tier)
                reopen_steps = _extract_attr_or_key(observation, "reopen_steps", None)
                if isinstance(reopen_steps, list):
                    self._compile_reopen_path(state, observation, reopen_steps)
                elif state.last_reopen_steps:
                    self._compile_reopen_path(
                        state, observation, state.last_reopen_steps
                    )
                self._emit_receipt(
                    state=state,
                    outcome=ReceiptOutcome.VERIFIED.value,
                    oracle_tier=tier,
                    observed_effect_strength=strength,
                    authorization_passed=True,
                    identity_passed=True,
                    postcondition_passed=True,
                    effect_passed=True,
                    delivery_uncertain=False,
                    commit_index=commit_idx,
                    reason=reason,
                )
                return action
            # Frame showed Draft/Error/editable/toast -> HOLD_DONE and repair!
            if (
                self.config.enable_repair_hints
                and int(remaining_steps) >= self.config.min_budget_for_ui_reopen
            ):
                repairs_used = state.repairs_by_commit.get(commit_idx, 0)
                if repairs_used < self.config.max_repairs_per_commit:
                    state.repairs_by_commit[commit_idx] = repairs_used + 1
                    self._deliver_intervention(
                        state,
                        kind="HOLD_DONE_REPAIR",
                        hint=f"HOLD_DONE: {reason} Complete the commit and verify persisted non-editable state before finishing.",
                        delta_note=reason,
                        fast_forward_obs=observation,
                    )
                    repaired = self._call_inner_act(observation, task, history)
                    return repaired

        # 4. If unverified work remains and budget >= min_budget_for_ui_reopen, issue 1-turn VERIFICATION_SUBGOAL
        if (
            has_unverified_work
            and not state.verification_subgoal_issued
            and int(remaining_steps) >= self.config.min_budget_for_ui_reopen
        ):
            state.verification_subgoal_issued = True
            self._deliver_intervention(
                state,
                kind="VERIFICATION_SUBGOAL",
                hint=(
                    "HOLD_DONE: Before finishing, reopen the affected record "
                    "in a non-editable view and confirm the persisted state "
                    "without relying on toast messages."
                ),
                delta_note="verification_subgoal_requested",
                fast_forward_obs=observation,
            )
            subgoal_action = self._call_inner_act(observation, task, history)
            if not self._is_terminal_action(subgoal_action):
                state.last_reopen_steps.append(
                    {
                        "type": _action_type_str(subgoal_action),
                        "raw_action": self.vault.redact(
                            _action_raw_dict(subgoal_action)
                        ),
                    }
                )
                return subgoal_action

        # 5. Budget exhausted or no oracle confirmed -> release DONE honestly as COMPLETED_UNVERIFIED
        self._emit_receipt(
            state=state,
            outcome="COMPLETED_UNVERIFIED",
            oracle_tier=0,
            observed_effect_strength=None,
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=not has_unverified_work,
            effect_passed=False,
            delivery_uncertain=True,
            commit_index=commit_idx,
            reason=(
                "Released DONE without independent verification "
                f"(remaining_steps={remaining_steps})."
            ),
        )
        return action

    def _mark_verified(
        self, state: EpisodeState, commit_index: int, *, tier: int
    ) -> None:
        with state._lock:
            state.pending_verification = False
            for ob in state.accrued_obligations:
                ob.fulfilled = True
                ob.verified = True
                ob.evidence_tier = max(ob.evidence_tier, tier)
            existing = self.wal.get(state.run_id, state.task_id, commit_index)
            if existing is not None and existing.state in (
                SagaState.PENDING_EFFECT,
                SagaState.RECONCILE,
            ):
                self.wal.record_outcome(
                    state.run_id,
                    state.task_id,
                    commit_index,
                    SagaState.VERIFIED,
                    evidence={"tier": tier},
                )
                state.saga_commits = self.wal.commits_for(state.run_id, state.task_id)
                state.next_commit_index = self.wal.resume_point(
                    state.run_id, state.task_id
                )

    def _check_stream_disconnect(
        self, state: EpisodeState, observation: Any
    ) -> str | None:
        """Detect VDI/stream disconnect from native ``BenchmarkObservation`` signals."""
        window_title = str(
            _extract_attr_or_key(observation, "window_title", "") or ""
        ).strip()
        title_low = window_title.lower()
        if title_low and any(
            marker in title_low for marker in _DISCONNECT_TITLE_MARKERS
        ):
            return f"Window title indicates stream disconnect: {window_title!r}"

        screenshot = _extract_attr_or_key(observation, "screenshot", None)
        screenshot_path = _extract_attr_or_key(observation, "screenshot_path", None)
        has_frame = (screenshot is not None and screenshot != b"") or bool(
            screenshot_path
        )

        with state._lock:
            seen_before = state.has_seen_frame

        if not has_frame and seen_before:
            return "Screenshot stream dropped (screenshot is None/empty after prior frames)."
        if screenshot is not None and _is_blank_frame(screenshot):
            return "Screenshot is a zero-variance blank frame."
        if has_frame and not seen_before:
            with state._lock:
                state.has_seen_frame = True
        return None

    def _build_wait_action(
        self, state: EpisodeState, observation: Any, reason: str
    ) -> Any:
        meta = {"guard": "STREAM_DISCONNECTED", "reason": reason}
        with state._lock:
            action_cls = state.action_cls
        if action_cls is not None:
            try:
                return action_cls(type="wait", raw_action=meta)
            except TypeError:
                try:
                    return action_cls(type="wait")
                except TypeError:
                    pass
        if type(observation).__name__ == "BenchmarkObservation":
            try:
                from openadapt_types import BenchmarkAction

                return BenchmarkAction(type="wait", raw_action=meta)
            except Exception:
                pass
        return TransducerAction(type="wait", raw_action=meta, is_commit=False)

    @staticmethod
    def _extract_verified_indices(
        res: Any, pending_records: list[SagaCommitRecord]
    ) -> list[int]:
        if isinstance(res, Mapping):
            explicit_indices = res.get("verified_commit_indices")
            if isinstance(explicit_indices, Sequence) and not isinstance(
                explicit_indices, (str, bytes)
            ):
                pending_set = {rec.commit_index for rec in pending_records}
                return [int(i) for i in explicit_indices if int(i) in pending_set]
            explicit_keys = res.get("verified_keys")
            if isinstance(explicit_keys, Sequence) and not isinstance(
                explicit_keys, (str, bytes)
            ):
                key_set = {str(k) for k in explicit_keys}
                return [
                    rec.commit_index
                    for rec in pending_records
                    if rec.idempotency_key and rec.idempotency_key in key_set
                ]
            if bool(res.get("verified", False)):
                return [rec.commit_index for rec in pending_records]
            return []
        if bool(res):
            return [rec.commit_index for rec in pending_records]
        return []

    def _reconcile_pending_commits(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
        *,
        include_verification_frame: bool = True,
    ) -> list[int]:
        """Reconcile ``PENDING_EFFECT``/``RECONCILE`` WAL commits via attached oracles."""
        pending = self.wal.pending_reconcile(state.run_id, state.task_id)
        if not pending:
            return []
        verified_indices: list[int] = []

        if self.rung3_verifier is not None:
            res = self.rung3_verifier(observation, task, state)
            matched = self._extract_verified_indices(res, pending)
            for idx in matched:
                self._mark_verified(state, idx, tier=2)
                verified_indices.append(idx)
            pending = self.wal.pending_reconcile(state.run_id, state.task_id)

        if pending and self.rung2_verifier is not None:
            res = self.rung2_verifier(observation, task, state)
            matched = self._extract_verified_indices(res, pending)
            for idx in matched:
                self._mark_verified(state, idx, tier=1)
                verified_indices.append(idx)
            pending = self.wal.pending_reconcile(state.run_id, state.task_id)

        if pending and self.reopen_executor is not None:
            for cart in self._active_cartridges():
                if not pending:
                    break
                for _path_key, steps in cart.reopen_paths.items():
                    frame = self.reopen_executor(steps, observation, state)
                    if frame is not None:
                        ok, _reason, strength = self._evaluate_verification_frame(frame)
                        if ok and strength is not None:
                            tier = 1 if strength == "independent_session" else 0
                            for rec in list(pending):
                                self._mark_verified(state, rec.commit_index, tier=tier)
                                verified_indices.append(rec.commit_index)
                            pending = self.wal.pending_reconcile(
                                state.run_id, state.task_id
                            )
                            break

        if pending and include_verification_frame:
            verif_frame = _extract_attr_or_key(observation, "verification_frame", None)
            if verif_frame is not None:
                ok, _reason, strength = self._evaluate_verification_frame(verif_frame)
                if ok and strength is not None:
                    tier = 1 if strength == "independent_session" else 0
                    reopen_steps = _extract_attr_or_key(
                        observation, "reopen_steps", None
                    )
                    if isinstance(reopen_steps, list):
                        self._compile_reopen_path(state, observation, reopen_steps)
                    elif state.last_reopen_steps:
                        self._compile_reopen_path(
                            state, observation, state.last_reopen_steps
                        )
                    for rec in list(pending):
                        self._mark_verified(state, rec.commit_index, tier=tier)
                        verified_indices.append(rec.commit_index)

        return verified_indices

    def _deliver_crash_resume_if_needed(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
    ) -> None:
        with state._lock:
            should_resume = (
                state.loaded_from_cold_wal
                and bool(state.saga_commits)
                and not state.crash_resume_delivered
            )
        if not should_resume:
            return

        # (a) Reconcile any PENDING_EFFECT entries via attached verifiers/reopen
        self._reconcile_pending_commits(
            state, observation, task, include_verification_frame=True
        )

        # (b) Deliver one KV-cache-safe WAL_CRASH_RESUME intervention
        with state._lock:
            state.saga_commits = self.wal.commits_for(state.run_id, state.task_id)
            state.next_commit_index = self.wal.resume_point(state.run_id, state.task_id)
            state.crash_resume_delivered = True
            commits_snapshot = list(state.saga_commits)
            unverified_obs = [
                ob.obligation_id for ob in state.accrued_obligations if not ob.verified
            ]
            resume_idx = state.next_commit_index

        verified_parts = [
            f"#{c.commit_index}(key={c.idempotency_key[:12] or 'none'})"
            for c in commits_snapshot
            if c.state is SagaState.VERIFIED
        ]
        pending_parts = [
            f"#{c.commit_index}(state={c.state.value},key={c.idempotency_key[:12] or 'none'})"
            for c in commits_snapshot
            if c.state
            in (SagaState.PENDING_EFFECT, SagaState.RECONCILE, SagaState.INTENT)
        ]
        hint = (
            f"WAL_CRASH_RESUME: Resumed from durable WAL at commit #{resume_idx}. "
            f"Already VERIFIED commits (skip these): {verified_parts or ['none']}. "
            f"Pending/reconcile commits: {pending_parts or ['none']}. "
            f"Remaining unverified obligations: {unverified_obs or ['none']}."
        )
        delta_note = (
            f"wal_crash_resume;verified={len(verified_parts)};"
            f"pending={len(pending_parts)};next={resume_idx}"
        )
        self._deliver_intervention(
            state,
            kind="WAL_CRASH_RESUME",
            hint=hint,
            delta_note=delta_note,
            fast_forward_obs=observation,
        )

    def _handle_terminal_action(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        if _action_type_str(action) == "infeasible":
            self._emit_receipt(
                state=state,
                outcome="HALTED",
                oracle_tier=0,
                observed_effect_strength=None,
                authorization_passed=True,
                identity_passed=True,
                postcondition_passed=False,
                effect_passed=False,
                delivery_uncertain=False,
                commit_index=max(0, state.next_commit_index - 1),
                reason="Agent reported task infeasible.",
            )
            return action

        # Check pre-commit guards (dirty buffer, missing anchors, scope bleed) with bounded repair
        current_action = action
        while self._is_terminal_action(current_action):
            trap_code, reason = self._check_pre_commit(
                state, current_action, observation
            )
            if trap_code is None:
                break
            resolution_kind, outcome = self._handle_trap(
                state=state,
                action=current_action,
                observation=observation,
                task=task,
                history=history,
                trap_code=trap_code,
                reason=reason,
                commit_index=state.next_commit_index,
            )
            if resolution_kind == "abstained":
                return outcome
            if resolution_kind == "approved":
                current_action = outcome
                break
            current_action = outcome
            if not self._is_terminal_action(current_action):
                return current_action

        if not self._is_terminal_action(current_action):
            return current_action

        if self.config.enable_verify_before_done:
            return self._verify_before_done(
                state, current_action, observation, task, history
            )

        self._emit_receipt(
            state=state,
            outcome="COMPLETED_UNVERIFIED",
            oracle_tier=0,
            observed_effect_strength=None,
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=True,
            effect_passed=False,
            delivery_uncertain=True,
            commit_index=max(0, state.next_commit_index - 1),
            reason="Verification before DONE disabled.",
        )
        return current_action

    def act(
        self,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None = None,
    ) -> Any:
        """Supervise one agent turn for ``(observation, task, history)``."""
        run_id, task_id = self._resolve_ids(observation, task)
        state = self.get_state(run_id, task_id)
        with state._lock:
            state.step_count += 1
        self._ingest_context(state, observation, task)

        # Native BenchmarkObservation surface guard: stream/VDI disconnect.
        # Hold commits and return a wait action directly without burning an LLM call.
        disconnect_reason = self._check_stream_disconnect(state, observation)
        if disconnect_reason is not None:
            with state._lock:
                state.intervention_log.append(
                    InterventionNote(
                        kind="STREAM_DISCONNECTED",
                        hint=disconnect_reason,
                        delta_note="stream_disconnected_wait",
                        step_index=state.step_count,
                    )
                )
            return self._build_wait_action(state, observation, disconnect_reason)

        # Cold-start WAL reconciliation + one-time WAL_CRASH_RESUME delivery
        self._deliver_crash_resume_if_needed(state, observation, task)

        # Step t+1 proactive reconciliation when a prior commit is pending verification
        if state.pending_verification:
            self._reconcile_pending_commits(
                state, observation, task, include_verification_frame=True
            )

        action = self._call_inner_act(observation, task, history)
        if not isinstance(action, (TransducerAction, dict)):
            with state._lock:
                state.action_cls = type(action)

        # Terminal action path: DONE / answer / infeasible
        if self._is_terminal_action(action):
            return self._handle_terminal_action(
                state, action, observation, task, history
            )

        # Commit boundary path
        if self._is_commit_boundary(action):
            # Content-keyed + index-keyed WAL deduplication loop
            skip_attempts = 0
            while self._is_commit_boundary(action):
                commit_index = state.next_commit_index
                fp = _commit_fingerprint(action)
                verified_by_idx = self.wal.is_commit_verified(
                    run_id, task_id, commit_index
                )
                matched_by_key = self.wal.find_by_idempotency_key(run_id, task_id, fp)
                verified_by_key = (
                    matched_by_key is not None
                    and matched_by_key.state is SagaState.VERIFIED
                )
                if not (verified_by_idx or verified_by_key):
                    break
                dup_idx = (
                    matched_by_key.commit_index
                    if verified_by_key and matched_by_key is not None
                    else commit_index
                )
                with state._lock:
                    state.next_commit_index = self.wal.resume_point(run_id, task_id)
                if skip_attempts >= max(1, self.config.max_repairs_per_commit):
                    return self._abstain_action(
                        state,
                        trap_code="WAL_DUPLICATE_COMMIT",
                        reason=(
                            f"Commit #{dup_idx} (key={fp[:12]}) is already VERIFIED in WAL; "
                            "halting to prevent duplicate execution."
                        ),
                        commit_index=dup_idx,
                    )
                skip_attempts += 1
                self._deliver_intervention(
                    state,
                    kind="WAL_SKIP_VERIFIED",
                    hint=(
                        f"Commit {dup_idx} (key={fp[:12]}) already verified in WAL; "
                        f"do not repeat it. Advance to commit {state.next_commit_index}."
                    ),
                    delta_note=f"skipped_verified_commit={dup_idx};key={fp[:12]}",
                    fast_forward_obs=observation,
                )
                action = self._call_inner_act(observation, task, history)
                if self._is_terminal_action(action):
                    return self._handle_terminal_action(
                        state, action, observation, task, history
                    )

            if not self._is_commit_boundary(action):
                return action

            commit_index = state.next_commit_index
            action = self._check_and_repair_commit(
                state=state,
                action=action,
                observation=observation,
                task=task,
                history=history,
                commit_index=commit_index,
            )
            if isinstance(action, TransducerAction) and action.is_abstained:
                return action

            if self._is_commit_boundary(action):
                fp = _commit_fingerprint(action)
                safe_summary = {
                    "type": _action_type_str(action),
                    "raw_action": self.vault.redact(_action_raw_dict(action)),
                }
                existing_rec = self.wal.get(run_id, task_id, commit_index)
                if existing_rec is None:
                    self.wal.record_intent(
                        run_id,
                        task_id,
                        commit_index,
                        safe_summary,
                        idempotency_key=fp,
                    )
                    self.wal.record_pending_effect(
                        run_id,
                        task_id,
                        commit_index,
                        safe_summary,
                        idempotency_key=fp,
                    )
                elif existing_rec.state is SagaState.INTENT:
                    self.wal.record_pending_effect(
                        run_id,
                        task_id,
                        commit_index,
                        safe_summary,
                        idempotency_key=fp,
                    )
                elif existing_rec.state in (
                    SagaState.PENDING_EFFECT,
                    SagaState.RECONCILE,
                ):
                    self.wal.record_outcome(
                        run_id,
                        task_id,
                        commit_index,
                        SagaState.RECONCILE,
                        evidence={"reattempted": True},
                        action_summary=safe_summary,
                        idempotency_key=fp,
                    )
                with state._lock:
                    state.saga_commits = self.wal.commits_for(run_id, task_id)
                    state.dirty_fields.clear()
                    state.pending_verification = True
                    state.verification_subgoal_issued = False
                    state.next_commit_index = commit_index + 1
            return action

        # Non-commit action (navigation / typing / reopen step)
        if state.verification_subgoal_issued:
            with state._lock:
                state.last_reopen_steps.append(
                    {
                        "type": _action_type_str(action),
                        "raw_action": self.vault.redact(_action_raw_dict(action)),
                    }
                )
        return action

    def _check_and_repair_commit(
        self,
        *,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
        commit_index: int,
    ) -> Any:
        current_action = action
        while True:
            trap_code, reason = self._check_pre_commit(
                state, current_action, observation
            )
            if trap_code is None:
                return current_action
            resolution_kind, outcome = self._handle_trap(
                state=state,
                action=current_action,
                observation=observation,
                task=task,
                history=history,
                trap_code=trap_code,
                reason=reason,
                commit_index=commit_index,
            )
            if resolution_kind in ("abstained", "approved"):
                return outcome
            # Agent produced a repaired action; loop to re-check it.
            current_action = outcome
            if not self._is_commit_boundary(current_action):
                return current_action

    def _handle_trap(
        self,
        *,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
        trap_code: str,
        reason: str,
        commit_index: int,
    ) -> tuple[Literal["repaired", "approved", "abstained"], Any]:
        # Indirect prompt injection from UNTRUSTED_DATA is never repaired by
        # retrying the injected command unless a human resolver overrides.
        can_self_repair = (
            self.config.enable_repair_hints
            and trap_code != "UNTRUSTED_INSTRUCTION_ORIGIN"
        )
        if can_self_repair:
            with state._lock:
                used = state.repairs_by_commit.get(commit_index, 0)
            if used < self.config.max_repairs_per_commit:
                with state._lock:
                    state.repairs_by_commit[commit_index] = used + 1
                self._deliver_intervention(
                    state,
                    kind="TRAP_REPAIR_HINT",
                    hint=f"TRAP_REPAIR_HINT ({trap_code}): {reason} Fix the mismatch and retry.",
                    delta_note=f"trap={trap_code};attempt={used + 1}",
                    fast_forward_obs=observation,
                )
                return "repaired", self._call_inner_act(observation, task, history)

        # Escalate to optional in-loop decision_resolver (AVC_H)
        decision = self._resolve_decision(
            state,
            commit_index=commit_index,
            trap_code=trap_code,
            reason=reason,
            proposed_action=action,
            observation=observation,
        )
        if decision is not None and decision.approved:
            if decision.replacement_action is not None:
                return "approved", decision.replacement_action
            return "approved", action

        # decision_resolver is None or denied -> clean abstain (0 SER)
        return "abstained", self._abstain_action(
            state,
            trap_code=trap_code,
            reason=reason,
            commit_index=commit_index,
        )

    def __getattr__(self, item: str) -> Any:
        return getattr(self.__wrapped__, item)

    def __repr__(self) -> str:
        return f"SimplexTransducer({self.__wrapped__!r})"
