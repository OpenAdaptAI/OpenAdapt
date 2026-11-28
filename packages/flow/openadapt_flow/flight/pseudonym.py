"""Consistent, reversible pseudonyms for one flight run.

The scrubber (:mod:`openadapt_flow.flight.scrub`) replaces each personal
value it finds with a token of its kind, ``[PERSON_1]``, ``[ID_2]``,
``[DATE_1]``..., instead of a bare ``<PERSON>``. The same value (ignoring case
and spacing) always gets the same token, in text and in screenshots, in
every request of the run. The model can then match ``[PERSON_1]`` in its
instructions to the row that shows ``[PERSON_1]`` on screen, and ask to type
``[PERSON_1]`` into a field.

The map from token to value lives in memory and in ``pseudonyms.json``
(mode 0600) in the run directory. It is never forwarded. Responses are
rewritten on their way back to the agent: in tool-call inputs (a computer
``type`` action's text, ``function_call`` arguments, any tool input) each
token is replaced with its real value, so the agent types the real value
into the app. Text the model writes for itself keeps the tokens. When the
agent sends that tool call back in its history, the real value is turned
into the same token again before anything leaves the machine: every value
already in the map is matched by itself, whether or not the detectors would
find it again.

Streams: Anthropic tool-use input deltas and OpenAI Responses argument and
input deltas are held until their block or item is complete, then sent on
as one de-tokenized delta. A stream that can't be parsed ends with an error
event. Chat Completions streams with tool calls can't be rewritten safely;
the proxy refuses a streamed Chat Completions request once the map holds a
value (see :func:`stream_refusal`).

:class:`Placeholders` is the stand-in without a map: every value becomes a
bare ``<KIND>`` placeholder and nothing is reversed.
"""

from __future__ import annotations

import json
import os
import re
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "ORD_NUM_KIND",
    "TOKEN",
    "Placeholders",
    "Pseudonyms",
    "StreamDetokenizer",
    "detokenize_response",
    "normalise",
    "parse_canonical_number",
    "stream_refusal",
]

#: A pseudonym token: ``[PERSON_1]`` or ``[ORD_NUM_0001]``.
TOKEN = re.compile(r"\[([A-Z]+(?:_[A-Z]+)*)_(\d+)\]")
ORD_NUM_KIND = "ORD_NUM"
_ORD_NUM_DEFAULT_WIDTH = 4
FILE_NAME = "pseudonyms.json"
SCHEMA = "openadapt.flight-pseudonyms/v1"

_CURRENCY_PREFIX = re.compile(
    r"^(?:USD|CAD|EUR|GBP|AUD|CHF|JPY|CNY|\$|€|£|¥)\s*",
    re.IGNORECASE,
)
_NUMERIC_BODY = re.compile(
    r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$|^\.\d+$"
)


def parse_canonical_number(value: str | int | float | Decimal) -> Optional[Decimal]:
    """Parse a numeric, currency, or percentage value into a canonical Decimal.

    Handles leading/trailing whitespace, currency symbols/codes (``$``, ``USD``,
    ``€``, ``£``, ``¥``), thousands commas, decimals, leading/trailing signs,
    accounting parentheses (``($1,250.50)`` -> ``-1250.50``), and percentages
    (``12.5%`` -> ``0.125``). Returns ``None`` for non-numeric or non-finite
    inputs. Equal numeric values (e.g. ``"$25,000"`` and ``"25000.00"``) return
    equal normalized Decimals.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return Decimal(0) if value == 0 else value.normalize()
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        d = Decimal(str(value))
        return Decimal(0) if d == 0 else d.normalize()
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    is_percent = False
    if text.endswith("%"):
        is_percent = True
        text = text[:-1].strip()

    # Handle leading sign before or after currency symbol: "-$25,000" or "$-25,000"
    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    text = _CURRENCY_PREFIX.sub("", text, count=1).strip()

    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    if not text or not _NUMERIC_BODY.match(text):
        return None

    cleaned = text.replace(",", "")
    try:
        num = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not num.is_finite():
        return None
    if is_percent:
        num = num / Decimal(100)
    if negative:
        num = -num
    return Decimal(0) if num == 0 else num.normalize()


def normalise(value: str) -> str:
    """The key a value is matched by: case and spacing ignored."""
    return " ".join(value.split()).casefold()


class Placeholders:
    """No map: each value becomes ``<KIND>``; nothing is reversed."""

    active = False

    def token(self, kind: str, value: str) -> str:
        return f"<{kind}>"

    def known_spans(self, text: str) -> list[tuple[int, int, str]]:
        return []

    def detokenize(self, text: str) -> str:
        return text

    def seed(
        self,
        values: Sequence[tuple[str, str] | str],
        *,
        default_kind: str = "PERSON",
        split_person_parts: bool = True,
    ) -> int:
        return 0

    def seed_record(self, record: Mapping[str, Any]) -> int:
        return 0

    def seed_numeric(
        self,
        values: Sequence[str | int | float | Decimal],
        *,
        kind: str = ORD_NUM_KIND,
        width: int = _ORD_NUM_DEFAULT_WIDTH,
    ) -> dict[str, str]:
        return {str(v): f"<{kind}>" for v in values}

    def numeric_token(
        self,
        value: str | int | float | Decimal,
        *,
        kind: str = ORD_NUM_KIND,
    ) -> str:
        return f"<{kind}>"

    def __len__(self) -> int:
        return 0


class Pseudonyms:
    """The run's token map. Thread-safe; saved on every new token."""

    active = True

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._by_key: dict[str, str] = {}
        self._by_numeric: dict[Decimal, str] = {}
        self._numeric_width: int = _ORD_NUM_DEFAULT_WIDTH
        self._values: dict[str, str] = {}  # token -> first value seen
        self._counts: dict[str, int] = {}
        self._known: Optional[re.Pattern[str]] = None
        self._known_kinds: dict[str, str] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def _format_numeric_token(self, kind: str, rank: int, width: int) -> str:
        return f"[{kind}_{rank:0{width}d}]"

    def token(self, kind: str, value: str) -> str:
        key = normalise(value)
        cleaned = " ".join(value.split())
        if kind == ORD_NUM_KIND:
            num = parse_canonical_number(cleaned)
            if num is not None:
                return self._register_numeric_one(kind, cleaned, key, num)
        with self._lock:
            hit = self._by_key.get(key)
            if hit is not None:
                return hit
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"[{kind}_{self._counts[kind]}]"
            self._by_key[key] = token
            self._values[token] = cleaned
            self._known = None
            snapshot = dict(self._values)
        self._save(snapshot)
        return token

    def _register_numeric_one(
        self, kind: str, cleaned: str, key: str, num: Decimal
    ) -> str:
        snapshot: Optional[dict[str, str]] = None
        with self._lock:
            existing = self._by_numeric.get(num)
            if existing is not None:
                if key not in self._by_key:
                    self._by_key[key] = existing
                    self._known = None
                return existing
            hit = self._by_key.get(key)
            if hit is not None:
                self._by_numeric[num] = hit
                return hit
            rank = self._counts.get(kind, 0) + 1
            self._counts[kind] = rank
            token = self._format_numeric_token(kind, rank, self._numeric_width)
            self._by_numeric[num] = token
            self._by_key[key] = token
            self._values[token] = cleaned
            self._known = None
            snapshot = dict(self._values)
        if snapshot is not None:
            self._save(snapshot)
        return token

    def numeric_token(
        self,
        value: str | int | float | Decimal,
        *,
        kind: str = ORD_NUM_KIND,
    ) -> str:
        """Return the order-preserving rank token for a single numeric value.

        Raises ``ValueError`` if ``value`` cannot be parsed as a finite number.
        """
        raw = str(value)
        cleaned = " ".join(raw.split())
        num = parse_canonical_number(value)
        if num is None:
            raise ValueError(f"cannot parse numeric value: {value!r}")
        return self._register_numeric_one(kind, cleaned, normalise(cleaned), num)

    def seed_numeric(
        self,
        values: Sequence[str | int | float | Decimal],
        *,
        kind: str = ORD_NUM_KIND,
        width: int = _ORD_NUM_DEFAULT_WIDTH,
    ) -> dict[str, str]:
        """Pre-register a batch of numeric or currency values in ascending order.

        Distinct canonical numeric values are sorted strictly ascending and
        assigned zero-padded rank tokens (``[ORD_NUM_0001]``..``[ORD_NUM_N]``)
        so lexicographical and numeric threshold comparisons are preserved
        without exposing the underlying numbers. Equal numeric values (such as
        ``"$25,000"`` and ``"25000.00"``) receive the same rank token, and
        :meth:`detokenize` restores the first formatted representation seen for
        that rank.

        Raises ``ValueError`` if any item cannot be parsed as a finite number.
        """
        if width < 1:
            raise ValueError("width must be at least 1")
        parsed: list[tuple[str, str, str, Decimal]] = []
        for item in values:
            raw = str(item)
            cleaned = " ".join(raw.split())
            num = parse_canonical_number(item)
            if num is None:
                raise ValueError(f"cannot parse numeric value: {item!r}")
            parsed.append((raw, cleaned, normalise(cleaned), num))

        if not parsed:
            return {}

        snapshot: Optional[dict[str, str]] = None
        mapping: dict[str, str] = {}
        with self._lock:
            first_repr: dict[Decimal, str] = {}
            for _raw, cleaned, _key, num in parsed:
                if num not in first_repr:
                    first_repr[num] = cleaned

            new_nums = sorted(n for n in first_repr if n not in self._by_numeric)
            current_max = self._counts.get(kind, 0)
            projected_max = current_max + len(new_nums)
            effective_width = max(self._numeric_width, width, len(str(projected_max)))
            if current_max == 0:
                self._numeric_width = effective_width

            changed = False
            for num in new_nums:
                current_max += 1
                token = self._format_numeric_token(
                    kind, current_max, self._numeric_width
                )
                self._by_numeric[num] = token
                self._values[token] = first_repr[num]
                changed = True
            self._counts[kind] = current_max

            for raw, _cleaned, key, num in parsed:
                token = self._by_numeric[num]
                if key not in self._by_key:
                    self._by_key[key] = token
                    self._known = None
                mapping[raw] = token

            if changed:
                self._known = None
                snapshot = dict(self._values)

        if snapshot is not None:
            self._save(snapshot)
        return mapping

    @staticmethod
    def numeric_rank(token: str) -> Optional[int]:
        """Return the integer rank of an ``[ORD_NUM_NNNN]`` token, or None."""
        m = TOKEN.fullmatch(token)
        if m is None or m.group(1) != ORD_NUM_KIND:
            return None
        return int(m.group(2))

    def value(self, token: str) -> Optional[str]:
        with self._lock:
            return self._values.get(token)

    def seed(
        self,
        values: Sequence[tuple[str, str] | str],
        *,
        default_kind: str = "PERSON",
        split_person_parts: bool = True,
    ) -> int:
        """Pre-register known personal values before the first outbound request.

        Accepts either ``(kind, value)`` pairs or plain strings (registered as
        ``default_kind``). For multi-word ``PERSON`` names, each alphabetic
        part of at least 3 characters is also registered when
        ``split_person_parts`` is True so partial mentions are caught even if
        NER misses them.
        """
        added = 0
        for item in values:
            if isinstance(item, tuple) and len(item) == 2:
                kind, raw = str(item[0]).strip().upper() or default_kind, str(item[1])
            else:
                kind, raw = default_kind, str(item)
            cleaned = " ".join(raw.split())
            if len(cleaned) < 2:
                continue
            before = len(self)
            self.token(kind, cleaned)
            if len(self) > before:
                added += 1
            if split_person_parts and kind == "PERSON":
                parts = cleaned.split()
                if len(parts) > 1:
                    for part in parts:
                        word = part.strip(".,;:'\"()")
                        if len(word) >= 3 and word.isalpha():
                            b_part = len(self)
                            self.token(kind, word)
                            if len(self) > b_part:
                                added += 1
        return added

    def seed_record(self, record: Mapping[str, Any]) -> int:
        """Seed known identity values from a task/record mapping."""
        pairs: list[tuple[str, str]] = []

        def _kind_for_key(key: str) -> str:
            low = key.casefold()
            if any(tok in low for tok in ("dob", "birth", "date")):
                return "DATE"
            if any(tok in low for tok in ("phone", "tel", "mobile")):
                return "PHONE"
            if "mail" in low:
                return "EMAIL"
            if any(tok in low for tok in ("addr", "street")):
                return "ADDRESS"
            if any(
                tok in low for tok in ("mrn", "ssn", "acct", "account", "member", "id")
            ):
                return "ID"
            return "PERSON"

        def _walk(node: Any, key_hint: str = "") -> None:
            if isinstance(node, Mapping):
                for k, v in node.items():
                    _walk(v, str(k))
            elif isinstance(node, (list, tuple)):
                for item in node:
                    _walk(item, key_hint)
            elif isinstance(node, str):
                cleaned = " ".join(node.split())
                if len(cleaned) >= 2:
                    pairs.append((_kind_for_key(key_hint), cleaned))

        _walk(record)
        return self.seed(pairs)

    def known_spans(self, text: str) -> list[tuple[int, int, str]]:
        """Where a value already in the map occurs in ``text`` (any case,
        any spacing), with its token's kind."""
        with self._lock:
            if not self._by_key:
                return []
            if self._known is None:
                parts = sorted(self._by_key, key=len, reverse=True)
                alternatives = [
                    r"\s+".join(re.escape(word) for word in key.split())
                    for key in parts
                    if len(key) >= 2
                ]
                if not alternatives:
                    return []
                self._known = re.compile(
                    r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)",
                    re.IGNORECASE,
                )
                self._known_kinds = {
                    key: TOKEN.match(token).group(1)  # type: ignore[union-attr]
                    for key, token in self._by_key.items()
                }
            pattern, kinds = self._known, self._known_kinds
        return [
            (m.start(), m.end(), kinds.get(normalise(m.group(0)), "ID"))
            for m in pattern.finditer(text)
        ]

    def detokenize(self, text: str) -> str:
        """``text`` with each known token replaced by its real value."""
        with self._lock:
            values = dict(self._values)
        if not values or "[" not in text:
            return text
        return TOKEN.sub(lambda m: values.get(m.group(0), m.group(0)), text)

    def _save(self, values: dict[str, str]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            {
                "schema": SCHEMA,
                "note": "real values; local only, never forwarded",
                "tokens": values,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        from openadapt_flow.private_file import open_binary

        # A raw-byte descriptor (no CRT newline translation on Windows),
        # owner-only from the start.
        fd = open_binary(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)


# -- responses ---------------------------------------------------------------

#: Blocks and items that carry a tool call's input.
_TOOL_TYPES = {
    "tool_use",
    "server_tool_use",
    "mcp_tool_use",
    "function_call",
    "custom_tool_call",
    "computer_call",
    "mcp_call",
    "local_shell_call",
}


def _deep(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_deep(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _deep(v, fn) for k, v in value.items()}
    return value


def _json_text(text: str, fn: Callable[[str], str]) -> str:
    """A JSON-string argument list with ``fn`` applied to its values."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return fn(text)
    if not isinstance(parsed, (dict, list)):
        return fn(text)
    return json.dumps(_deep(parsed, fn), ensure_ascii=False)


def _tool_calls(node: Any, fn: Callable[[str], str]) -> Any:
    """``node`` with ``fn`` applied inside every tool call's input."""
    if isinstance(node, list):
        return [_tool_calls(v, fn) for v in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("type")
    out = {}
    for key, value in node.items():
        if kind in _TOOL_TYPES and key in ("input", "action", "actions"):
            out[key] = fn(value) if isinstance(value, str) else _deep(value, fn)
        elif kind in _TOOL_TYPES and key == "arguments" and isinstance(value, str):
            out[key] = _json_text(value, fn)
        elif key == "function" and isinstance(value, dict):  # Chat Completions
            args = value.get("arguments")
            out[key] = {
                **value,
                **(
                    {"arguments": _json_text(args, fn)} if isinstance(args, str) else {}
                ),
            }
        else:
            out[key] = _tool_calls(value, fn)
    return out


def detokenize_response(body: bytes, pseudonyms: Any) -> bytes:
    """A JSON response body with tool-call inputs de-tokenized. Raises
    ValueError when the body is not JSON (the caller refuses it)."""
    if not getattr(pseudonyms, "active", False) or not len(pseudonyms):
        return body
    data = json.loads(body)
    return json.dumps(
        _tool_calls(data, pseudonyms.detokenize), ensure_ascii=False
    ).encode("utf-8")


def stream_refusal(path: str, body: Any, pseudonyms: Any) -> Optional[str]:
    """Why a streamed request can't be sent, or None."""
    if not getattr(pseudonyms, "active", False) or not len(pseudonyms):
        return None
    if not (isinstance(body, dict) and body.get("stream")):
        return None
    if path.rstrip("/").endswith("chat/completions"):
        return (
            "a streamed Chat Completions response can't be de-tokenized safely; "
            "send stream=false"
        )
    return None


#: Stream events whose deltas carry tool-call input (held until done).
_DELTA_DONE = {
    "response.function_call_arguments.delta": (
        "response.function_call_arguments.done",
        "arguments",
    ),
    "response.custom_tool_call_input.delta": (
        "response.custom_tool_call_input.done",
        "input",
    ),
    "response.mcp_call_arguments.delta": (
        "response.mcp_call_arguments.done",
        "arguments",
    ),
}
_DONE = {done: (delta, field) for delta, (done, field) in _DELTA_DONE.items()}


def _event(name: Optional[str], data: dict[str, Any]) -> bytes:
    head = f"event: {name}\n" if name else ""
    return (head + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode(
        "utf-8"
    )


class StreamDetokenizer:
    """Rewrites an SSE stream so tool-call inputs reach the agent de-tokenized.

    ``feed`` takes raw bytes as they arrive and returns the bytes to pass on;
    ``close`` returns the rest. Tool-input deltas are held until their block
    or item is complete. When an event can't be handled safely, the stream
    is ended with an ``error`` event and everything after it is dropped.
    """

    def __init__(self, pseudonyms: Any) -> None:
        self.p = pseudonyms
        self._buffer = b""
        self._anthropic: dict[int, dict[str, Any]] = {}
        self._openai: dict[str, dict[str, Any]] = {}
        self.failed: Optional[str] = None

    def feed(self, chunk: bytes) -> bytes:
        if self.failed:
            return b""
        self._buffer += chunk.replace(b"\r\n", b"\n")
        out = b""
        while b"\n\n" in self._buffer and not self.failed:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            out += self._one(raw + b"\n\n")
        return out

    def close(self) -> bytes:
        out = b""
        if self._buffer.strip() and not self.failed:
            out += self._one(self._buffer + b"\n\n")
        self._buffer = b""
        if not self.failed and (self._anthropic or self._openai):
            out += self._fail("the stream ended inside a tool call")
        return out

    def _fail(self, why: str) -> bytes:
        self.failed = why
        return _event(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "openadapt_flight",
                    "message": f"the response could not be de-tokenized ({why}); "
                    "the stream was stopped",
                },
            },
        )

    def _one(self, raw: bytes) -> bytes:
        name = None
        data_lines = []
        for line in raw.decode("utf-8", "replace").split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        text = "\n".join(data_lines)
        if not data_lines or text.strip() == "[DONE]":
            return raw
        try:
            data = json.loads(text)
        except ValueError:
            return self._fail("an event is not JSON")
        if not isinstance(data, dict):
            return raw
        fn = self.p.detokenize
        kind = data.get("type")
        # Anthropic: hold a tool_use block until content_block_stop.
        if kind == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") in _TOOL_TYPES:
                self._anthropic[int(data.get("index", 0))] = {
                    "name": name,
                    "start": data,
                    "json": "",
                }
                return b""
        if kind == "content_block_delta":
            index = int(data.get("index", 0))
            held = self._anthropic.get(index)
            if held is not None:
                delta = data.get("delta") or {}
                if delta.get("type") != "input_json_delta":
                    return self._fail("an unexpected delta in a tool call")
                held["json"] += str(delta.get("partial_json", ""))
                return b""
        if kind == "content_block_stop":
            held = self._anthropic.pop(int(data.get("index", 0)), None)
            if held is not None:
                start = held["start"]
                block = dict(start.get("content_block") or {})
                try:
                    full = (
                        json.loads(held["json"])
                        if held["json"]
                        else block.get("input", {})
                    )
                except ValueError:
                    return self._fail("a tool call's input is not JSON")
                full = _deep(full, fn)
                block["input"] = {}
                out = _event(held["name"], {**start, "content_block": block})
                out += _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": start.get("index", 0),
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(full, ensure_ascii=False),
                        },
                    },
                )
                return out + _event(name, data)
        # OpenAI Responses: hold argument / input deltas until .done.
        if kind in _DELTA_DONE:
            item = str(data.get("item_id", ""))
            held = self._openai.setdefault(item, {"first": data, "name": name})
            held.setdefault("parts", []).append(str(data.get("delta", "")))
            return b""
        if kind in _DONE:
            delta_kind, field = _DONE[kind]
            item = str(data.get("item_id", ""))
            held = self._openai.pop(item, None)
            whole = data.get(field)
            if not isinstance(whole, str):
                whole = "".join(held["parts"]) if held else ""
            rewritten = _json_text(whole, fn) if field == "arguments" else fn(whole)
            out = b""
            if held is not None:
                out += _event(
                    held["name"] or delta_kind,
                    {**held["first"], "delta": rewritten},
                )
            return out + _event(name, {**data, field: rewritten})
        return _event(name, _tool_calls(data, fn))
