"""Fail-closed redaction for network saves from the optional Chrome extension.

Part of OpenAdapt#1872. The extension observes state-changing requests
(``POST``, ``PUT``, ``PATCH``, ``DELETE``) through ``chrome.webRequest`` while a
recording is active, and redacts them in ``chrome_extension/network_redact.js``
before anything reaches the native host. This module applies the same rules
again on ingest and on load, so a line that skipped the extension's redactor
(an old build, a bug, a hand-written file) still cannot store a secret.

Fail closed:

- Header values are kept only for a short allowlist (``ALLOWED_HEADERS``).
  Every other header keeps its name and gets ``<secret>``. A header whose name
  looks like a credential (``authorization``, ``cookie``, ``set-cookie``,
  ``x-api-key``, ``x-csrf-token`` ...) is withheld even if a caller adds it to
  the allowlist.
- Query values, path segments and JSON or form body values are withheld when
  their key looks secret or the value looks like a credential (bearer/basic
  schemes, JWTs, known key prefixes, long opaque tokens).
- Bodies are kept only as already-parsed JSON or form structures within size
  and depth limits. Anything else is dropped and only its status is kept.
- Unknown top-level keys are dropped. Non-http(s) URLs, non-save methods and
  any redaction error drop the whole record (``None``).
- Every withheld value is remembered by its complete scalar identity: the
  whole credential, each part of a credential header (alphabetic ``Bearer``
  tokens included), cookie and ``key=value`` parameter values, decoded
  ``Basic`` credentials, and the scalar leaves of a withheld JSON or form
  subtree. A final check drops the record if any of them still appears in
  the record, in its JSON text or in its percent-decoded (canonical) form.
  If that bookkeeping exceeds its limit the record is dropped.

No remote endpoints. The extension and this ingest stay optional.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlsplit

NETWORK_SAVE_KIND = "network_save"
PLACEHOLDER = "<secret>"
SAVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BODY_BYTES = 64 * 1024
MAX_DEPTH = 32
MAX_NODES = 5000
# Upper bound on remembered withheld identities per record. Past it the
# record is dropped rather than checked partially.
MAX_WITHHELD = 4096
MIN_WITHHELD_LEN = 6

ALLOWED_HEADERS = frozenset(
    {
        "accept",
        "content-encoding",
        "content-length",
        "content-type",
        "x-requested-with",
    }
)

SECRET_HEADER = re.compile(
    r"auth|cookie|token|secret|session|key|csrf|xsrf|signature|password|credential|bearer",
    re.I,
)

SECRET_KEY = re.compile(
    "|".join(
        [
            r"pass(word|wd|phrase)?",
            r"pwd",
            r"secret",
            r"token",
            r"auth",
            r"api[_-]?key",
            r"apikey",
            r"access[_-]?key",
            r"private[_-]?key",
            r"session",
            r"^sid$",
            r"cookie",
            r"csrf",
            r"xsrf",
            r"signature",
            r"^sig$",
            r"^otp$",
            r"^pin$",
            r"^ssn$",
            r"card[_-]?(number|num|no)",
            r"^cvv$",
            r"^cvc$",
            r"^code$",
            r"credential",
        ]
    ),
    re.I,
)

SECRET_VALUE = (
    re.compile(r"^\s*(bearer|basic|digest|token)\s+\S+", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    re.compile(r"\b(sk|pk|rk)[-_](live|test)?[-_]?[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

_HEADER_PARTS = re.compile(r"[\s;,]+")
# Words inside a credential header that are syntax, not secret: auth schemes
# and cookie / challenge attribute names. Their values are still withheld
# unless the attribute is purely descriptive (``Domain``, ``Path`` ...).
_HEADER_SYNTAX = frozenset(
    {
        "bearer",
        "basic",
        "digest",
        "token",
        "negotiate",
        "httponly",
        "secure",
        "partitioned",
    }
)
_DESCRIPTIVE_ATTRS = frozenset(
    {
        "path",
        "domain",
        "expires",
        "max-age",
        "samesite",
        "priority",
        "realm",
        "charset",
        "scope",
        "algorithm",
        "qop",
        "stale",
        "version",
        "error",
        "error_description",
    }
)
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9+/=_.-]+$")

# Top-level keys a stored network_save line may carry. Others are dropped.
_RECORD_KEYS = (
    "kind",
    "method",
    "url",
    "url_origin",
    "resource_type",
    "tab_id",
    "frame_id",
    "status",
    "request_headers",
    "response_headers",
    "request_body_status",
    "request_body",
    "redacted",
    "t",
    "schema",
    "seq",
)


class _TooComplex(Exception):
    pass


class _TooManyWithheld(Exception):
    pass


def _looks_like_token(value: str) -> bool:
    if len(value) < 32 or not _TOKEN_CHARS.match(value):
        return False
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def is_secret_value(value: object) -> bool:
    """True when a string value looks like a credential."""

    if not isinstance(value, str) or value == PLACEHOLDER:
        return False
    if _looks_like_token(value):
        return True
    return any(pattern.search(value) for pattern in SECRET_VALUE)


def is_secret_key(key: object) -> bool:
    """True when a query, form or JSON key names a secret."""

    return bool(SECRET_KEY.search(str(key or "")))


def is_secret_header(name: object) -> bool:
    """True when a header name always withholds its value."""

    return bool(SECRET_HEADER.search(str(name or "")))


class _Withheld:
    """Values withheld during one redaction, for the final leak check.

    Each entry is a complete scalar identity (never a JSON encoding of a
    removed subtree), so the check matches wherever that scalar resurfaces.
    """

    def __init__(self) -> None:
        self.values: list[str] = []
        self._seen: set[str] = set()

    def add(self, value: object) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            value = json.dumps(value)
        if not isinstance(value, str):
            return
        for candidate in (value, value.strip().strip("\"'")):
            if (
                candidate != PLACEHOLDER
                and len(candidate) >= MIN_WITHHELD_LEN
                and candidate not in self._seen
            ):
                if len(self.values) >= MAX_WITHHELD:
                    raise _TooManyWithheld()
                self._seen.add(candidate)
                self.values.append(candidate)

    def add_decoded(self, value: str) -> None:
        """Withhold a value and its percent-decoded form."""

        self.add(value)
        self.add(unquote(value))

    def add_header(self, value: str) -> None:
        """Withhold a non-allowlisted header value.

        ``value`` is not known to be a credential, so only the whole value and
        its opaque parts are remembered (alphabetic words would match
        ordinary text).
        """

        self.add(value)
        for part in _HEADER_PARTS.split(value):
            for piece in part.split("="):
                if len(piece) >= 8 and not piece.isalpha():
                    self.add(piece)

    def add_credential_header(self, value: str) -> None:
        """Withhold a credential header value by every scalar it carries.

        ``Bearer <token>`` keeps its token even when it is purely alphabetic;
        ``sid=<v>; Path=/`` keeps ``<v>``; ``Basic <b64>`` also keeps the
        decoded ``user:password`` and each half.
        """

        self.add(value)
        parts = [p for p in _HEADER_PARTS.split(value) if p]
        scheme = parts[0].lower() if parts else ""
        for part in parts:
            name, sep, rest = part.partition("=")
            if sep and name.strip().lower() in _DESCRIPTIVE_ATTRS:
                continue
            if not sep and part.lower() in _HEADER_SYNTAX:
                continue
            self.add_decoded(part)
            self.add(part.rstrip("="))  # base64 padding
            if sep:
                self.add_decoded(rest.strip("\"'"))
        if scheme == "basic" and len(parts) >= 2:
            try:
                decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                decoded = ""
            if decoded:
                self.add(decoded)
                for half in decoded.split(":", 1):
                    self.add(half)

    def add_leaves(self, value: Any, budget: list[int], depth: int) -> None:
        """Withhold every scalar leaf of a removed subtree, within limits."""

        budget[0] += 1
        if budget[0] > MAX_NODES or depth > MAX_DEPTH:
            raise _TooComplex()
        if isinstance(value, Mapping):
            for child in value.values():
                self.add_leaves(child, budget, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                self.add_leaves(child, budget, depth + 1)
        else:
            self.add(value)

    def leaked_into(self, record: Mapping[str, Any]) -> bool:
        """True when a withheld identity survives in the record.

        Compared against the JSON text (ASCII-escaped and raw) and its
        percent-decoded canonical forms, so an encoded copy of a withheld
        value in a retained URL or field also counts.
        """

        escaped = json.dumps(record, sort_keys=True)
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False)
        texts = (escaped, raw, unquote(raw), unquote_plus(raw))
        for value in self.values:
            forms = {
                value,
                json.dumps(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            }
            for form in forms:
                if any(form in text for text in texts):
                    return True
        return False


def _header_items(headers: object) -> list[tuple[str, str]]:
    if headers is None:
        return []
    items: list[tuple[str, str]] = []
    if isinstance(headers, Mapping):
        pairs: Iterable[Any] = ({"name": k, "value": v} for k, v in headers.items())
    elif isinstance(headers, list):
        pairs = headers
    else:
        raise ValueError("headers must be a list or object")
    for header in pairs:
        if not isinstance(header, Mapping):
            continue
        name = str(header.get("name") or "").strip().lower()
        if not name:
            continue
        value = header.get("value")
        items.append((name, "" if value is None else str(value)))
    return items


def redact_headers(
    headers: object,
    withheld: _Withheld,
    *,
    extra_allowed: Iterable[str] = (),
) -> list[dict[str, str]]:
    allowed = ALLOWED_HEADERS | {h.strip().lower() for h in extra_allowed}
    out: list[dict[str, str]] = []
    for name, value in _header_items(headers):
        keep = (
            name in allowed
            and not is_secret_header(name)
            and not is_secret_value(value)
        )
        if keep:
            out.append({"name": name, "value": value})
        elif is_secret_header(name) or is_secret_value(value):
            withheld.add_credential_header(value)
            out.append({"name": name, "value": PLACEHOLDER})
        else:
            withheld.add_header(value)
            out.append({"name": name, "value": PLACEHOLDER})
    return out


def _redact_tree(
    value: Any,
    key: Optional[str],
    withheld: _Withheld,
    budget: list[int],
    depth: int,
) -> Any:
    budget[0] += 1
    if budget[0] > MAX_NODES or depth > MAX_DEPTH:
        raise _TooComplex()
    if isinstance(value, (list, tuple, Mapping)):
        if key is not None and is_secret_key(key):
            # Remember the scalar leaves, not the subtree's JSON encoding:
            # a leaf copied elsewhere must still match.
            withheld.add_leaves(value, budget, depth)
            return PLACEHOLDER
    if isinstance(value, (list, tuple)):
        return [_redact_tree(v, key, withheld, budget, depth + 1) for v in value]
    if isinstance(value, Mapping):
        return {
            str(k): _redact_tree(v, str(k), withheld, budget, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, str):
        if (key is not None and is_secret_key(key)) or is_secret_value(value):
            withheld.add(value)
            return PLACEHOLDER
        return value
    if key is not None and is_secret_key(key) and value is not None:
        withheld.add(value)
        return PLACEHOLDER
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise _TooComplex()


def redact_body(status: object, body: object, withheld: _Withheld) -> tuple[str, Any]:
    """Return ``(status, body)``. Unsupported or oversized bodies are dropped."""

    status_text = str(status or "none")
    if body is None:
        if status_text.startswith("omitted:") or status_text == "none":
            return status_text, None
        return "none", None
    if status_text not in ("json", "form") or not isinstance(body, (Mapping, list)):
        return "omitted:unsupported_type", None
    try:
        if len(json.dumps(body)) > MAX_BODY_BYTES:
            return "omitted:too_large", None
        return status_text, _redact_tree(body, None, withheld, [0], 0)
    except (_TooComplex, RecursionError, TypeError, ValueError):
        return "omitted:too_complex", None


def redact_url(raw: object, withheld: _Withheld) -> Optional[tuple[str, str]]:
    """Return ``(url, origin)`` with secrets withheld, or ``None`` to drop."""

    try:
        parts = urlsplit(str(raw or ""))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.username:
            withheld.add(unquote(parts.username))
        if parts.password:
            withheld.add(unquote(parts.password))
        if parts.fragment:
            withheld.add_decoded(parts.fragment)
            for _, frag_value in parse_qsl(parts.fragment, keep_blank_values=True):
                withheld.add(frag_value)
        host = parts.hostname
        if ":" in host:  # IPv6 literal keeps its brackets
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
    except ValueError:
        return None
    segments = []
    for seg in parts.path.split("/"):
        decoded = unquote(seg)
        if is_secret_value(decoded):
            withheld.add(decoded)
            segments.append(PLACEHOLDER)
        else:
            segments.append(seg)
    params = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_secret_key(key) or is_secret_value(value):
            withheld.add(value)
            params.append(f"{quote(key, safe='')}={PLACEHOLDER}")
        else:
            params.append(f"{quote(key, safe='')}={quote(value, safe='')}")
    origin = f"{parts.scheme}://{host}"
    query = ("?" + "&".join(params)) if params else ""
    return origin + "/".join(segments) + query, origin


def redact_network_save(
    line: Mapping[str, Any],
    *,
    extra_allowed_headers: Iterable[str] = (),
) -> Optional[dict[str, Any]]:
    """Redact one ``network_save`` line. ``None`` means drop it (fail closed)."""

    try:
        if not isinstance(line, Mapping) or line.get("kind") != NETWORK_SAVE_KIND:
            return None
        method = str(line.get("method") or "").upper()
        if method not in SAVE_METHODS:
            return None
        withheld = _Withheld()
        url = redact_url(line.get("url"), withheld)
        if url is None:
            return None
        status, body = redact_body(
            line.get("request_body_status"), line.get("request_body"), withheld
        )

        def _int(name: str) -> Optional[int]:
            value = line.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else None
            )

        resource_type = line.get("resource_type")
        t = line.get("t")
        record: dict[str, Any] = {
            "kind": NETWORK_SAVE_KIND,
            "method": method,
            "url": url[0],
            "url_origin": url[1],
            "resource_type": str(resource_type) if resource_type is not None else None,
            "tab_id": _int("tab_id"),
            "frame_id": _int("frame_id"),
            "status": _int("status"),
            "request_headers": redact_headers(
                line.get("request_headers"),
                withheld,
                extra_allowed=extra_allowed_headers,
            ),
            "response_headers": redact_headers(
                line.get("response_headers"),
                withheld,
                extra_allowed=extra_allowed_headers,
            ),
            "request_body_status": status,
            "request_body": body,
            "redacted": True,
            "t": t if isinstance(t, (int, float)) and not isinstance(t, bool) else None,
        }
        for key in ("schema", "seq"):
            if key in line and isinstance(line[key], (str, int)):
                record[key] = line[key]
        if withheld.leaked_into(record) or not set(record) <= set(_RECORD_KEYS):
            return None
        return record
    except Exception:  # noqa: BLE001 - any failure drops the record
        return None


def network_saves(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The ``network_save`` lines of a capture, in order."""

    return [dict(line) for line in lines if line.get("kind") == NETWORK_SAVE_KIND]
