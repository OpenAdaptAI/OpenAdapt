"""Session-scoped HTTP wire tap, inline mutation gate, and HAR 1.2 utilities.

Counts and gates requests routed through this observer. Traffic that bypasses
this route and downstream server effects are outside its evidence scope.
No vision/GUI dependencies are imported.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import re
import ssl
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, ConfigDict

from openadapt_flow.secret_redaction import MASK, credential_header, mask_header
from openadapt_flow.tx.receipt import write_private_record
from openadapt_flow.tx.types import (
    WireMutationRecord,
    WireSessionSummary,
    WireTapProtocol,
    canonical_sha256,
)
from openadapt_flow.wire_check import READ_METHODS, carries_secret, mask_secrets

__all__ = [
    "HAR_VERSION",
    "SessionWireTap",
    "WirePolicy",
    "classify_graphql_post",
    "dump_har",
    "extract_entity_id",
    "extract_etag",
    "extract_version",
    "har_to_summary",
    "is_mutation_request",
    "load_har",
    "matches_write_route",
]

HAR_VERSION = "1.2"

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

_TEMPLATE_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.-]*\}")
_LOCAL_BROWSER_SCHEMES = ("about:", "blob:", "data:", "javascript:")


class WirePolicy(BaseModel):
    """Declarative inline gating and extraction policy for ``SessionWireTap``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_origins: tuple[str, ...] = ()
    max_mutations: Optional[int] = 1
    allowed_write_routes: tuple[tuple[str, str], ...] = ()
    id_fields: tuple[str, ...] = ("id", "uuid", "pid")
    version_fields: tuple[str, ...] = ("version", "etag", "meta.versionId")
    secrets: tuple[str, ...] = ()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_origin(url_or_origin: str) -> str:
    parts = urlsplit(url_or_origin.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if (
        port is None
        or (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _status_text(status: Optional[int]) -> str:
    if status is None:
        return ""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _to_bytes(data: bytes | bytearray | str | None) -> bytes:
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    return data.encode("utf-8")


def _header_pairs(
    headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return [(str(k), str(v)) for k, v in headers.items()]
    return [(str(k), str(v)) for k, v in headers]


def _get_header(
    headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
    target_name: str,
) -> str:
    needle = target_name.lower()
    for k, v in _header_pairs(headers):
        if k.lower() == needle:
            return v
    return ""


def _decompress_for_inspection(
    body: bytes,
    headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
) -> bytes:
    if not body:
        return b""
    encoding = _get_header(headers, "content-encoding").strip().lower()
    try:
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        return body
    return body


def _parse_json_body(
    body: bytes,
    headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]] = None,
) -> Any:
    decoded = _decompress_for_inspection(body, headers)
    if not decoded:
        return None
    stripped = decoded.lstrip()
    if not stripped or stripped[:1] not in (b"{", b"["):
        return None
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _get_dotted_scalar(mapping: Mapping[str, Any], dotted_key: str) -> Optional[str]:
    if dotted_key in mapping:
        direct = mapping[dotted_key]
        if direct is not None and not isinstance(direct, (dict, list, tuple, bool)):
            text = str(direct).strip()
            if text:
                return text
    cur: Any = mapping
    for part in dotted_key.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return None
    if cur is None or isinstance(cur, (dict, list, tuple, bool)):
        return None
    text = str(cur).strip()
    return text if text else None


def _scan_graphql_operations(
    doc: str,
    *,
    allow_shorthand_or_headless: bool,
) -> list[tuple[str, Optional[str]]]:
    """Extract top-level GraphQL ``(operation_type, operation_name)`` definitions."""
    ops: list[tuple[str, Optional[str]]] = []
    n = len(doc)
    i = 0
    if doc.startswith("\ufeff"):
        i = 1
    brace_depth = 0
    paren_depth = 0
    pending_kind: Optional[str] = None
    pending_name: Optional[str] = None
    awaiting_name = False
    saw_selection_set = False

    while i < n:
        ch = doc[i]
        # Line comments
        if ch == "#":
            nl = doc.find("\n", i + 1)
            i = n if nl == -1 else nl + 1
            continue
        # Block strings """...""" or regular strings "..."
        if ch == '"':
            if doc[i : i + 3] == '"""':
                end = doc.find('"""', i + 3)
                i = n if end == -1 else end + 3
                continue
            i += 1
            while i < n:
                if doc[i] == "\\":
                    i += 2
                    continue
                if doc[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            awaiting_name = False
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue
        if ch == "{":
            saw_selection_set = True
            awaiting_name = False
            if brace_depth == 0 and paren_depth == 0 and pending_kind is None:
                if allow_shorthand_or_headless:
                    pending_kind = "query"
                    pending_name = None
            brace_depth += 1
            i += 1
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            if brace_depth == 0 and pending_kind is not None:
                if pending_kind != "fragment":
                    ops.append((pending_kind, pending_name))
                pending_kind = None
                pending_name = None
                awaiting_name = False
            i += 1
            continue
        if brace_depth == 0 and paren_depth == 0:
            if ch == "@":
                awaiting_name = False
                i += 1
                while i < n and (doc[i].isalnum() or doc[i] == "_"):
                    i += 1
                continue
            if ch.isalpha() or ch == "_":
                start = i
                while i < n and (doc[i].isalnum() or doc[i] == "_"):
                    i += 1
                ident = doc[start:i]
                if pending_kind is None:
                    if ident in ("query", "mutation", "subscription", "fragment"):
                        pending_kind = ident
                        pending_name = None
                        awaiting_name = True
                    else:
                        # Non-GraphQL free text at top level.
                        return []
                elif awaiting_name:
                    pending_name = ident
                    awaiting_name = False
                continue
        i += 1

    if pending_kind is not None and pending_kind != "fragment":
        if saw_selection_set or allow_shorthand_or_headless:
            ops.append((pending_kind, pending_name))

    return ops


def _classify_single_graphql_doc(
    doc: str,
    *,
    operation_name: Optional[str],
    is_graphql_endpoint: bool,
) -> Optional[tuple[bool, bool]]:
    """Return ``(is_mutation, is_graphql_mutation)`` if ``doc`` is GraphQL, else ``None``."""
    stripped = doc.strip()
    if not stripped:
        return None
    if not is_graphql_endpoint and "{" not in stripped:
        return None

    ops = _scan_graphql_operations(
        stripped,
        allow_shorthand_or_headless=is_graphql_endpoint,
    )
    if not ops:
        return None

    if operation_name:
        matched = [kind for kind, name in ops if name == operation_name]
        if len(matched) == 1:
            selected = matched[0]
            if selected == "mutation":
                return (True, True)
            if selected == "query":
                return (False, False)
            return (True, False)

    if any(kind == "mutation" for kind, _ in ops):
        return (True, True)
    if all(kind == "query" for kind, _ in ops):
        return (False, False)
    return (True, False)


def classify_graphql_post(
    path_or_url: str,
    request_body: bytes,
    request_headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]] = None,
) -> tuple[bool, bool]:
    """Classify an HTTP ``POST`` as ``(is_mutation, is_graphql_mutation)``.

    A ``POST`` is treated as a state mutation unless it is provably a GraphQL
    ``query``-only request. GraphQL ``mutation`` operations return ``(True, True)``.
    """
    clean_path = (urlsplit(path_or_url).path or "/").rstrip("/").lower()
    is_graphql_endpoint = clean_path.endswith("/graphql")
    ctype = _get_header(request_headers, "content-type").lower()

    decoded = _decompress_for_inspection(request_body, request_headers)
    if not decoded:
        return (True, False)

    if ctype.startswith("application/graphql"):
        try:
            doc_text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            return (True, False)
        classified = _classify_single_graphql_doc(
            doc_text,
            operation_name=None,
            is_graphql_endpoint=True,
        )
        return classified if classified is not None else (True, False)

    parsed = _parse_json_body(request_body, request_headers)
    if isinstance(parsed, dict) and isinstance(parsed.get("query"), str):
        op_name = parsed.get("operationName")
        classified = _classify_single_graphql_doc(
            parsed["query"],
            operation_name=op_name if isinstance(op_name, str) else None,
            is_graphql_endpoint=is_graphql_endpoint,
        )
        if classified is not None:
            return classified
        return (True, False)

    if (
        isinstance(parsed, list)
        and parsed
        and all(
            isinstance(item, dict) and isinstance(item.get("query"), str)
            for item in parsed
        )
    ):
        batch_results: list[tuple[bool, bool]] = []
        for item in parsed:
            op_name = item.get("operationName")
            res = _classify_single_graphql_doc(
                item["query"],
                operation_name=op_name if isinstance(op_name, str) else None,
                is_graphql_endpoint=is_graphql_endpoint,
            )
            if res is None:
                return (True, False)
            batch_results.append(res)
        if any(is_gql_mut for _, is_gql_mut in batch_results):
            return (True, True)
        if all(not is_mut for is_mut, _ in batch_results):
            return (False, False)
        return (True, False)

    return (True, False)


def is_mutation_request(
    method: str,
    path_or_url: str,
    request_body: bytes = b"",
    request_headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]] = None,
) -> tuple[bool, bool]:
    """Return ``(is_mutation, is_graphql_mutation)`` for any HTTP request."""
    upper = method.strip().upper()
    if upper in READ_METHODS:
        return (False, False)
    if upper == "POST":
        return classify_graphql_post(path_or_url, request_body, request_headers)
    return (True, False)


def matches_write_route(
    method: str,
    path: str,
    allowed_method: str,
    pattern: str,
) -> bool:
    """Match ``(method, path)`` against an allowed ``(METHOD, path_regex_or_template)``."""
    want_method = allowed_method.strip().upper()
    if want_method != "*" and want_method != method.strip().upper():
        return False

    clean_path = urlsplit(path).path or "/"
    raw_pattern = pattern.strip()
    if not raw_pattern:
        return False

    if _TEMPLATE_PARAM_RE.search(raw_pattern):
        parts = _TEMPLATE_PARAM_RE.split(raw_pattern)
        placeholders = _TEMPLATE_PARAM_RE.findall(raw_pattern)
        regex_pieces: list[str] = ["^"]
        for idx, literal in enumerate(parts):
            regex_pieces.append(re.escape(literal))
            if idx < len(placeholders):
                regex_pieces.append("[^/]+")
        regex_pieces.append("$")
        compiled = "".join(regex_pieces)
        return re.match(compiled, clean_path) is not None

    try:
        return re.fullmatch(raw_pattern, clean_path) is not None
    except re.error:
        return raw_pattern == clean_path


def extract_entity_id(
    method: str,
    path: str,
    response_headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
    response_json: Any,
    id_fields: tuple[str, ...] = ("id", "uuid", "pid"),
) -> Optional[str]:
    """Extract mutated entity ID from JSON body, ``Location`` header, or resource URL."""
    if isinstance(response_json, Mapping):
        for field_name in id_fields:
            found = _get_dotted_scalar(response_json, field_name)
            if found is not None:
                return found
        data_node = response_json.get("data")
        if isinstance(data_node, Mapping):
            for field_name in id_fields:
                found = _get_dotted_scalar(data_node, field_name)
                if found is not None:
                    return found
            if len(data_node) == 1:
                inner = next(iter(data_node.values()))
                if isinstance(inner, Mapping):
                    for field_name in id_fields:
                        found = _get_dotted_scalar(inner, field_name)
                        if found is not None:
                            return found

    location = _get_header(response_headers, "location").strip()
    if location:
        loc_path = urlsplit(location).path.rstrip("/")
        loc_segments = [seg for seg in loc_path.split("/") if seg]
        if loc_segments:
            return unquote(loc_segments[-1])

    if method.strip().upper() in {"PUT", "PATCH", "DELETE"}:
        req_path = (urlsplit(path).path or "/").rstrip("/")
        req_segments = [seg for seg in req_path.split("/") if seg]
        if len(req_segments) >= 2:
            return unquote(req_segments[-1])

    return None


def extract_etag(
    response_headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
) -> Optional[str]:
    """Extract ``ETag`` header from response headers."""
    raw = _get_header(response_headers, "etag").strip()
    return raw if raw else None


def extract_version(
    response_json: Any,
    version_fields: tuple[str, ...] = ("version", "etag", "meta.versionId"),
) -> Optional[str]:
    """Extract entity version token from parsed JSON response using ``version_fields``."""
    if not isinstance(response_json, Mapping):
        return None
    for vfield in version_fields:
        found = _get_dotted_scalar(response_json, vfield)
        if found is not None:
            return found
    data_node = response_json.get("data")
    if isinstance(data_node, Mapping):
        for vfield in version_fields:
            found = _get_dotted_scalar(data_node, vfield)
            if found is not None:
                return found
        if len(data_node) == 1:
            inner = next(iter(data_node.values()))
            if isinstance(inner, Mapping):
                for vfield in version_fields:
                    found = _get_dotted_scalar(inner, vfield)
                    if found is not None:
                        return found
    return None


def _mask_if_needed(text: str, secrets: tuple[str, ...]) -> str:
    if not secrets or not text:
        return text
    return mask_secrets(text, secrets)


def _encode_har_body(
    raw_bytes: bytes,
    headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]],
    secrets: tuple[str, ...],
) -> tuple[str, Optional[str]]:
    decoded_bytes = _decompress_for_inspection(raw_bytes, headers)
    if not decoded_bytes:
        return ("", None)
    try:
        text = decoded_bytes.decode("utf-8")
        return (_mask_if_needed(text, secrets), None)
    except UnicodeDecodeError:
        safe_bytes = (
            MASK.encode("utf-8")
            if carries_secret(decoded_bytes, secrets)
            else decoded_bytes
        )
        b64 = base64.b64encode(safe_bytes).decode("ascii")
        return (b64, "base64")


def _build_har_entry(
    *,
    started_utc: str,
    duration_ms: float,
    method: str,
    url: str,
    request_headers: list[tuple[str, str]],
    request_body: bytes,
    response_status: Optional[int],
    response_headers: list[tuple[str, str]],
    response_body: Optional[bytes],
    secrets: tuple[str, ...],
    tx_meta: dict[str, Any],
) -> dict[str, Any]:
    parts = urlsplit(url)
    query_items = [
        {
            "name": _mask_if_needed(k, secrets),
            "value": _mask_if_needed(v, secrets),
        }
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    req_hdr_list = [
        {
            "name": _mask_if_needed(k, secrets),
            "value": mask_header(k, v, secrets),
        }
        for k, v in request_headers
    ]
    req_mime = (
        _get_header(request_headers, "content-type") or "application/octet-stream"
    )
    req_obj: dict[str, Any] = {
        "method": method,
        "url": _mask_if_needed(url, secrets),
        "httpVersion": "HTTP/1.1",
        "cookies": [],
        "headers": req_hdr_list,
        "queryString": query_items,
        "headersSize": -1,
        "bodySize": len(request_body),
    }
    if request_body:
        req_text, req_encoding = _encode_har_body(
            request_body, request_headers, secrets
        )
        post_data: dict[str, Any] = {
            "mimeType": _mask_if_needed(req_mime, secrets),
            "text": req_text,
        }
        if req_encoding is not None:
            post_data["encoding"] = req_encoding
        req_obj["postData"] = post_data

    resp_bytes = response_body or b""
    resp_hdr_list = [
        {
            "name": _mask_if_needed(k, secrets),
            "value": mask_header(k, v, secrets),
        }
        for k, v in response_headers
    ]
    resp_mime = (
        _get_header(response_headers, "content-type") or "application/octet-stream"
    )
    resp_text, resp_encoding = _encode_har_body(resp_bytes, response_headers, secrets)
    content_obj: dict[str, Any] = {
        "size": len(resp_bytes),
        "mimeType": _mask_if_needed(resp_mime, secrets),
        "text": resp_text,
    }
    if resp_encoding is not None:
        content_obj["encoding"] = resp_encoding

    status_code = response_status if response_status is not None else 0
    redirect_url = _mask_if_needed(_get_header(response_headers, "location"), secrets)
    resp_obj: dict[str, Any] = {
        "status": status_code,
        "statusText": _status_text(response_status),
        "httpVersion": "HTTP/1.1",
        "cookies": [],
        "headers": resp_hdr_list,
        "content": content_obj,
        "redirectURL": redirect_url,
        "headersSize": -1,
        "bodySize": len(resp_bytes) if response_body is not None else -1,
    }

    safe_ms = max(0.0, round(duration_ms, 3))
    return {
        "startedDateTime": started_utc,
        "time": safe_ms,
        "request": req_obj,
        "response": resp_obj,
        "cache": {},
        "timings": {
            "send": 0,
            "wait": safe_ms,
            "receive": 0,
        },
        "_tx": tx_meta,
    }


def _scrub_har_value(obj: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(obj, str):
        return mask_secrets(obj, secrets)
    if isinstance(obj, list):
        return [_scrub_har_value(item, secrets) for item in obj]
    if isinstance(obj, dict):
        scrubbed = {
            _mask_if_needed(k, secrets) if isinstance(k, str) else k: (
                MASK
                if isinstance(k, str) and credential_header(k)
                else _scrub_har_value(v, secrets)
            )
            for k, v in obj.items()
        }
        name = obj.get("name")
        if isinstance(name, str) and credential_header(name) and "value" in obj:
            scrubbed["value"] = MASK
        if obj.get("encoding") == "base64" and isinstance(obj.get("text"), str):
            if carries_secret(obj["text"], secrets):
                scrubbed["text"] = base64.b64encode(MASK.encode("utf-8")).decode(
                    "ascii"
                )
        if isinstance(obj.get("cookies"), list):
            scrubbed["cookies"] = [
                {**_scrub_har_value(cookie, secrets), "value": MASK}
                if isinstance(cookie, dict)
                else MASK
                for cookie in obj["cookies"]
            ]
        return scrubbed
    return obj


class _TapHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], tap: "SessionWireTap") -> None:
        self.tap = tap
        super().__init__(server_address, _TapProxyRequestHandler)


class _TapProxyRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_request_body(self) -> bytes:
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                size_str = line.split(b";", 1)[0].strip()
                if not size_str:
                    continue
                chunk_size = int(size_str, 16)
                if chunk_size == 0:
                    # Consume trailing CRLF / trailer headers
                    while True:
                        trailer = self.rfile.readline()
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                chunks.append(self.rfile.read(chunk_size))
                self.rfile.read(2)  # CRLF after chunk
            return b"".join(chunks)

        length_header = self.headers.get("Content-Length")
        if not length_header:
            return b""
        try:
            length = int(length_header)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_raw_response(
        self,
        status: int,
        reason: str,
        headers: list[tuple[str, str]],
        body: bytes,
        *,
        is_head: bool,
    ) -> None:
        self.close_connection = True
        self.send_response(status, reason or _status_text(status))
        upstream_cl: Optional[str] = None
        for k, v in headers:
            lower = k.lower()
            if lower in _HOP_BY_HOP_HEADERS:
                continue
            if lower == "content-length":
                upstream_cl = v
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        if status in (204, 304):
            self.end_headers()
            return
        if is_head:
            if upstream_cl is not None:
                self.send_header("Content-Length", upstream_cl)
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle_proxy_request(self) -> None:
        tap = cast(_TapHTTPServer, self.server).tap
        req_body = self._read_request_body()
        req_headers = [(k, v) for k, v in self.headers.items()]
        status, reason, resp_headers, resp_body = tap._proxy_http_exchange(
            method=self.command,
            raw_target=self.path,
            request_headers=req_headers,
            request_body=req_body,
        )
        self._send_raw_response(
            status,
            reason,
            resp_headers,
            resp_body,
            is_head=(self.command.upper() == "HEAD"),
        )

    def do_GET(self) -> None:
        self._handle_proxy_request()

    def do_HEAD(self) -> None:
        self._handle_proxy_request()

    def do_OPTIONS(self) -> None:
        self._handle_proxy_request()

    def do_POST(self) -> None:
        self._handle_proxy_request()

    def do_PUT(self) -> None:
        self._handle_proxy_request()

    def do_PATCH(self) -> None:
        self._handle_proxy_request()

    def do_DELETE(self) -> None:
        self._handle_proxy_request()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._handle_proxy_request
        raise AttributeError(name)


class SessionWireTap:
    """Thread-safe HTTP wire tap, reverse proxy, and inline mutation gate.

    Conforms to ``WireTapProtocol`` in ``openadapt_flow.tx.types``.
    """

    def __init__(
        self,
        upstream_url: str,
        policy: Optional[WirePolicy] = None,
        *,
        session_id: Optional[str] = None,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        upstream_timeout_s: float = 30.0,
    ) -> None:
        parsed = urlsplit(upstream_url.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"upstream_url must be an http(s) URL, got: {upstream_url!r}"
            )

        self._upstream_url = upstream_url.rstrip("/")
        self._upstream_scheme = parsed.scheme.lower()
        self._upstream_netloc = parsed.netloc
        self._upstream_host = parsed.hostname or "127.0.0.1"
        self._upstream_port = parsed.port or (
            443 if self._upstream_scheme == "https" else 80
        )
        self._upstream_base_path = parsed.path.rstrip("/")
        self._upstream_origin = _normalize_origin(upstream_url)

        self._policy = policy or WirePolicy()
        self._allowed_origins = (
            frozenset(
                _normalize_origin(o)
                for o in self._policy.allowed_origins
                if _normalize_origin(o)
            )
            if self._policy.allowed_origins
            else frozenset({self._upstream_origin})
        )

        self._listen_host = listen_host
        self._listen_port = listen_port
        self._upstream_timeout_s = upstream_timeout_s

        self._lock = threading.RLock()
        self._server: Optional[_TapHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._bound_base_url: Optional[str] = None
        self._closed: bool = False

        self._session_id: str = ""
        self._total_requests: int = 0
        self._read_requests: int = 0
        self._mutations_allowed: int = 0
        self._mutations_blocked: int = 0
        self._collateral_mutations: int = 0
        self._next_seq: int = 1
        self._mutations: list[Optional[WireMutationRecord]] = []
        self._har_entries: list[dict[str, Any]] = []
        self.reset_session(session_id=session_id)

    @property
    def policy(self) -> WirePolicy:
        return self._policy

    @property
    def upstream_url(self) -> str:
        return self._upstream_url

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    @property
    def base_url(self) -> str:
        """Local ``http://127.0.0.1:<port>`` reverse-proxy base URL."""
        with self._lock:
            if self._server is None and not self._closed:
                self.start()
            return self._bound_base_url or self._upstream_url

    def start(self) -> str:
        """Start the local daemon ``ThreadingHTTPServer`` if not already running."""
        with self._lock:
            if self._server is not None and self._bound_base_url is not None:
                return self._bound_base_url
            self._closed = False
            server = _TapHTTPServer((self._listen_host, self._listen_port), self)
            host, port = server.server_address[:2]
            bound_host = "127.0.0.1" if host in ("0.0.0.0", "") else str(host)
            self._server = server
            self._bound_base_url = f"http://{bound_host}:{port}"
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name=f"openadapt-tx-wire-tap-{port}",
                daemon=True,
            )
            self._server_thread = thread
            thread.start()
            return self._bound_base_url

    def stop(self) -> None:
        """Shut down the local reverse proxy server cleanly."""
        with self._lock:
            server = self._server
            thread = self._server_thread
            self._server = None
            self._server_thread = None
            self._closed = True
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    close = stop

    def __enter__(self) -> "SessionWireTap":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def reset_session(self, session_id: Optional[str] = None) -> str:
        """Clear counters, mutation ledger, and HAR buffer for a fresh session."""
        with self._lock:
            new_id = session_id if session_id else uuid.uuid4().hex
            self._session_id = new_id
            self._total_requests = 0
            self._read_requests = 0
            self._mutations_allowed = 0
            self._mutations_blocked = 0
            self._collateral_mutations = 0
            self._next_seq = 1
            self._mutations = []
            self._har_entries = []
            return new_id

    def summary(self) -> WireSessionSummary:
        """Compute O(1) session-scoped write completeness summary and SHA-256 ledger."""
        with self._lock:
            completed_mutations = tuple(m for m in self._mutations if m is not None)
            digest = canonical_sha256([m.model_dump() for m in completed_mutations])
            return WireSessionSummary(
                session_id=self._session_id,
                total_requests=self._total_requests,
                read_requests=self._read_requests,
                mutations_allowed=self._mutations_allowed,
                mutations_blocked=self._mutations_blocked,
                collateral_mutations=self._collateral_mutations,
                mutations=completed_mutations,
                ledger_sha256=digest,
            )

    def export_har(self) -> dict[str, Any]:
        """Export recorded session exchanges as a secret-masked HAR 1.2 dictionary."""
        with self._lock:
            entries_copy = list(self._har_entries)
        har_doc: dict[str, Any] = {
            "log": {
                "version": HAR_VERSION,
                "creator": {
                    "name": "openadapt-flow.tx.wire_tap",
                    "version": "1.0",
                },
                "entries": entries_copy,
            }
        }
        return _scrub_har_value(har_doc, self._policy.secrets)

    def save_har(self, path: str | Path) -> Path:
        """Write ``export_har()`` to ``path`` as UTF-8 JSON."""
        out_path = Path(path)
        dump_har(self, out_path, secrets=self._policy.secrets)
        return out_path

    def import_har(
        self, source: str | Path | bytes | Mapping[str, Any]
    ) -> WireSessionSummary:
        """Replay entries from a HAR 1.2 document into the current wire-tap session."""
        har = load_har(source, secrets=self._policy.secrets)
        entries = har.get("log", {}).get("entries", [])
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            req = entry.get("request")
            resp = entry.get("response")
            if not isinstance(req, Mapping):
                continue
            method = str(req.get("method") or "GET").upper()
            url = str(req.get("url") or self._upstream_url)
            req_headers = [
                (str(h.get("name", "")), str(h.get("value", "")))
                for h in (req.get("headers") or [])
                if isinstance(h, Mapping)
            ]
            req_body = b""
            post_data = req.get("postData")
            if isinstance(post_data, Mapping):
                raw_text = str(post_data.get("text") or "")
                if post_data.get("encoding") == "base64":
                    try:
                        req_body = base64.b64decode(raw_text)
                    except Exception:
                        req_body = raw_text.encode("utf-8")
                else:
                    req_body = raw_text.encode("utf-8")

            resp_status: Optional[int] = None
            resp_headers: list[tuple[str, str]] = []
            resp_body: Optional[bytes] = None
            if isinstance(resp, Mapping):
                raw_status = resp.get("status")
                if isinstance(raw_status, int) and raw_status > 0:
                    resp_status = raw_status
                resp_headers = [
                    (str(h.get("name", "")), str(h.get("value", "")))
                    for h in (resp.get("headers") or [])
                    if isinstance(h, Mapping)
                ]
                content = resp.get("content")
                if isinstance(content, Mapping) and "text" in content:
                    c_text = str(content.get("text") or "")
                    if content.get("encoding") == "base64":
                        try:
                            resp_body = base64.b64decode(c_text)
                        except Exception:
                            resp_body = c_text.encode("utf-8")
                    else:
                        resp_body = c_text.encode("utf-8")

            duration_ms = float(entry.get("time") or 0.0)
            self.record_exchange(
                method=method,
                url=url,
                request_body=req_body,
                response_status=resp_status,
                response_body=resp_body,
                request_headers=dict(req_headers),
                response_headers=dict(resp_headers),
                duration_ms=duration_ms,
            )
        return self.summary()

    @classmethod
    def from_har(
        cls,
        source: str | Path | bytes | Mapping[str, Any],
        *,
        upstream_url: Optional[str] = None,
        policy: Optional[WirePolicy] = None,
        session_id: Optional[str] = None,
    ) -> "SessionWireTap":
        """Construct a ``SessionWireTap`` populated from an existing HAR 1.2 document."""
        effective_secrets = policy.secrets if policy is not None else ()
        har = load_har(source, secrets=effective_secrets)
        resolved_upstream = upstream_url
        if not resolved_upstream:
            for entry in har.get("log", {}).get("entries", []):
                if isinstance(entry, Mapping) and isinstance(
                    entry.get("request"), Mapping
                ):
                    candidate = str(entry["request"].get("url") or "")
                    origin = _normalize_origin(candidate)
                    if origin:
                        resolved_upstream = origin
                        break
        tap = cls(
            resolved_upstream or "http://127.0.0.1:80",
            policy=policy,
            session_id=session_id,
        )
        tap.import_har(har)
        return tap

    def _resolve_target_url(self, url_or_target: str) -> tuple[str, str, str, str]:
        """Resolve incoming URL or proxy request-target into ``(full_url, origin, path, query)``."""
        parts = urlsplit(url_or_target)
        if parts.scheme and parts.netloc:
            req_origin = _normalize_origin(url_or_target)
            tap_origin = (
                _normalize_origin(self._bound_base_url) if self._bound_base_url else ""
            )
            path = parts.path or "/"
            query = parts.query
            if tap_origin and req_origin == tap_origin:
                upstream_target = self._join_upstream_path(path, query)
                return (
                    f"{self._upstream_scheme}://{self._upstream_netloc}{upstream_target}",
                    self._upstream_origin,
                    urlsplit(upstream_target).path or "/",
                    query,
                )
            full_url = f"{parts.scheme}://{parts.netloc}{path}"
            if query:
                full_url = f"{full_url}?{query}"
            return (full_url, req_origin, path, query)

        rel_path = parts.path or "/"
        if not rel_path.startswith("/"):
            rel_path = "/" + rel_path
        query = parts.query
        upstream_target = self._join_upstream_path(rel_path, query)
        full_url = f"{self._upstream_scheme}://{self._upstream_netloc}{upstream_target}"
        return (
            full_url,
            self._upstream_origin,
            urlsplit(upstream_target).path or "/",
            query,
        )

    def _join_upstream_path(self, rel_path: str, query: str) -> str:
        base_path = self._upstream_base_path
        if (
            base_path
            and rel_path != base_path
            and not rel_path.startswith(base_path + "/")
        ):
            combined = f"{base_path}/{rel_path.lstrip('/')}"
        else:
            combined = rel_path or "/"
        if query:
            return f"{combined}?{query}"
        return combined

    def _evaluate_policy_locked(
        self,
        *,
        method: str,
        target_origin: str,
        path: str,
        query: str,
        raw_target: str,
        is_mutation: bool,
    ) -> Optional[str]:
        """Return ``block_reason`` if the request must be blocked inline, else ``None``."""
        if self._policy.secrets:
            target_for_secret_check = f"{path}?{query}" if query else path
            if carries_secret(query, self._policy.secrets) or carries_secret(
                target_for_secret_check, self._policy.secrets
            ):
                return "secret_in_url"
            raw_parts = urlsplit(raw_target)
            if raw_parts.fragment and carries_secret(
                raw_parts.fragment, self._policy.secrets
            ):
                return "secret_in_url"

        if target_origin and target_origin not in self._allowed_origins:
            return "disallowed_origin"

        if not is_mutation:
            return None

        if self._policy.allowed_write_routes:
            candidate_paths = [path]
            if self._upstream_base_path and path.startswith(
                self._upstream_base_path + "/"
            ):
                candidate_paths.append(path[len(self._upstream_base_path) :])
            elif self._upstream_base_path and path == self._upstream_base_path:
                candidate_paths.append("/")
            matched = any(
                matches_write_route(method, cand, allow_m, allow_pat)
                for cand in candidate_paths
                for allow_m, allow_pat in self._policy.allowed_write_routes
            )
            if not matched:
                return "undeclared_write_route"

        if (
            self._policy.max_mutations is not None
            and self._mutations_allowed >= self._policy.max_mutations
        ):
            return "max_mutations_exceeded"

        return None

    def _begin_request(
        self,
        *,
        method: str,
        raw_target: str,
        request_headers: list[tuple[str, str]],
        request_body: bytes,
    ) -> dict[str, Any]:
        """Atomically classify and gate a request before any upstream network I/O."""
        upper_method = method.strip().upper()
        full_url, target_origin, path, query = self._resolve_target_url(raw_target)
        is_mut, is_gql_mut = is_mutation_request(
            upper_method,
            path,
            request_body,
            request_headers,
        )
        started_utc = _utc_now_iso()
        req_sha = hashlib.sha256(request_body).hexdigest()
        masked_url = _mask_if_needed(full_url, self._policy.secrets)
        masked_path = _mask_if_needed(path, self._policy.secrets)

        with self._lock:
            session_snapshot = self._session_id
            self._total_requests += 1
            block_reason = self._evaluate_policy_locked(
                method=upper_method,
                target_origin=target_origin,
                path=path,
                query=query,
                raw_target=raw_target,
                is_mutation=is_mut,
            )

            if not is_mut:
                self._read_requests += 1
                return {
                    "session_id": session_snapshot,
                    "started_utc": started_utc,
                    "method": upper_method,
                    "full_url": full_url,
                    "masked_url": masked_url,
                    "path": path,
                    "masked_path": masked_path,
                    "query": query,
                    "is_mutation": False,
                    "is_graphql_mutation": False,
                    "blocked": block_reason is not None,
                    "block_reason": block_reason or "",
                    "seq": None,
                    "slot_index": None,
                    "req_sha": req_sha,
                }

            seq = self._next_seq
            self._next_seq += 1

            if block_reason is not None:
                self._mutations_blocked += 1
                self._collateral_mutations += 1
                slot_index = len(self._mutations)
                self._mutations.append(
                    WireMutationRecord(
                        seq=seq,
                        method=upper_method,
                        url=masked_url,
                        path=masked_path,
                        request_body_sha256=req_sha,
                        response_status=403,
                        response_body_sha256=None,
                        extracted_entity_id=None,
                        etag=None,
                        version=None,
                        blocked=True,
                        block_reason=block_reason,
                        is_graphql_mutation=is_gql_mut,
                        timestamp_utc=started_utc,
                    )
                )
                return {
                    "session_id": session_snapshot,
                    "started_utc": started_utc,
                    "method": upper_method,
                    "full_url": full_url,
                    "masked_url": masked_url,
                    "path": path,
                    "masked_path": masked_path,
                    "query": query,
                    "is_mutation": True,
                    "is_graphql_mutation": is_gql_mut,
                    "blocked": True,
                    "block_reason": block_reason,
                    "seq": seq,
                    "slot_index": slot_index,
                    "req_sha": req_sha,
                }

            # Reserve allowed mutation slot immediately while holding _lock so
            # concurrent callers cannot race past max_mutations.
            if self._mutations_allowed >= 1:
                self._collateral_mutations += 1
            self._mutations_allowed += 1
            slot_index = len(self._mutations)
            self._mutations.append(
                WireMutationRecord(
                    seq=seq,
                    method=upper_method,
                    url=masked_url,
                    path=masked_path,
                    request_body_sha256=req_sha,
                    response_status=None,
                    response_body_sha256=None,
                    extracted_entity_id=None,
                    etag=None,
                    version=None,
                    blocked=False,
                    block_reason="",
                    is_graphql_mutation=is_gql_mut,
                    timestamp_utc=started_utc,
                )
            )
            return {
                "session_id": session_snapshot,
                "started_utc": started_utc,
                "method": upper_method,
                "full_url": full_url,
                "masked_url": masked_url,
                "path": path,
                "masked_path": masked_path,
                "query": query,
                "is_mutation": True,
                "is_graphql_mutation": is_gql_mut,
                "blocked": False,
                "block_reason": "",
                "seq": seq,
                "slot_index": slot_index,
                "req_sha": req_sha,
            }

    def _finalize_request(
        self,
        ticket: dict[str, Any],
        *,
        request_headers: list[tuple[str, str]],
        request_body: bytes,
        response_status: Optional[int],
        response_headers: list[tuple[str, str]],
        response_body: Optional[bytes],
        duration_ms: float,
    ) -> Optional[WireMutationRecord]:
        resp_sha = (
            hashlib.sha256(response_body).hexdigest()
            if response_body is not None
            else None
        )
        blocked: bool = ticket["blocked"]
        block_reason: str = ticket["block_reason"]
        is_mut: bool = ticket["is_mutation"]
        is_gql_mut: bool = ticket["is_graphql_mutation"]

        entity_id: Optional[str] = None
        etag_val: Optional[str] = None
        version_val: Optional[str] = None

        if is_mut and not blocked:
            parsed_json = (
                _parse_json_body(response_body, response_headers)
                if response_body is not None
                else None
            )
            entity_id = extract_entity_id(
                ticket["method"],
                ticket["path"],
                response_headers,
                parsed_json,
                self._policy.id_fields,
            )
            etag_val = extract_etag(response_headers)
            version_val = extract_version(parsed_json, self._policy.version_fields)
            if entity_id is not None:
                entity_id = _mask_if_needed(entity_id, self._policy.secrets)
            if etag_val is not None:
                etag_val = _mask_if_needed(etag_val, self._policy.secrets)
            if version_val is not None:
                version_val = _mask_if_needed(version_val, self._policy.secrets)

        record: Optional[WireMutationRecord] = None
        if is_mut:
            record = WireMutationRecord(
                seq=ticket["seq"],
                method=ticket["method"],
                url=ticket["masked_url"],
                path=ticket["masked_path"],
                request_body_sha256=ticket["req_sha"],
                response_status=response_status,
                response_body_sha256=resp_sha,
                extracted_entity_id=entity_id,
                etag=etag_val,
                version=version_val,
                blocked=blocked,
                block_reason=block_reason,
                is_graphql_mutation=is_gql_mut,
                timestamp_utc=ticket["started_utc"],
            )

        har_entry = _build_har_entry(
            started_utc=ticket["started_utc"],
            duration_ms=duration_ms,
            method=ticket["method"],
            url=ticket["full_url"],
            request_headers=request_headers,
            request_body=request_body,
            response_status=response_status,
            response_headers=response_headers,
            response_body=response_body,
            secrets=self._policy.secrets,
            tx_meta={
                "blocked": blocked,
                "block_reason": block_reason,
                "is_mutation": is_mut,
                "is_graphql_mutation": is_gql_mut,
                "extracted_entity_id": entity_id,
                "etag": etag_val,
                "version": version_val,
            },
        )

        with self._lock:
            if self._session_id == ticket["session_id"]:
                slot_idx = ticket["slot_index"]
                if (
                    record is not None
                    and slot_idx is not None
                    and slot_idx < len(self._mutations)
                ):
                    self._mutations[slot_idx] = record
                self._har_entries.append(har_entry)

        return record

    def _blocked_http_payload(
        self, reason: str
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        safe_reason = _mask_if_needed(reason, self._policy.secrets)
        body = json.dumps(
            {"error": "blocked_by_wire_tap", "reason": safe_reason},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8")]
        return (403, "Forbidden", headers, body)

    def _forward_to_upstream(
        self,
        *,
        method: str,
        path: str,
        query: str,
        request_headers: list[tuple[str, str]],
        request_body: bytes,
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        target_path = f"{path}?{query}" if query else path
        forward_headers: dict[str, str] = {}
        for k, v in request_headers:
            lower = k.lower()
            if lower in _HOP_BY_HOP_HEADERS or lower in ("host", "content-length"):
                continue
            forward_headers[k] = v
        forward_headers["Host"] = self._upstream_netloc
        forward_headers["Connection"] = "close"
        if request_body:
            forward_headers["Content-Length"] = str(len(request_body))

        conn: http.client.HTTPConnection
        if self._upstream_scheme == "https":
            conn = http.client.HTTPSConnection(
                self._upstream_host,
                self._upstream_port,
                timeout=self._upstream_timeout_s,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                self._upstream_host,
                self._upstream_port,
                timeout=self._upstream_timeout_s,
            )

        try:
            conn.request(
                method,
                target_path,
                body=request_body if request_body else None,
                headers=forward_headers,
            )
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = list(resp.msg.items())
            return (
                resp.status,
                resp.reason or _status_text(resp.status),
                resp_headers,
                resp_body,
            )
        except Exception as exc:
            detail = _mask_if_needed(str(exc), self._policy.secrets)
            err_body = json.dumps(
                {"error": "upstream_error", "detail": detail},
                separators=(",", ":"),
            ).encode("utf-8")
            return (
                502,
                "Bad Gateway",
                [("Content-Type", "application/json; charset=utf-8")],
                err_body,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _proxy_http_exchange(
        self,
        *,
        method: str,
        raw_target: str,
        request_headers: list[tuple[str, str]],
        request_body: bytes,
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        t0 = time.perf_counter()
        ticket = self._begin_request(
            method=method,
            raw_target=raw_target,
            request_headers=request_headers,
            request_body=request_body,
        )
        if ticket["blocked"]:
            status, reason, resp_headers, resp_body = self._blocked_http_payload(
                ticket["block_reason"]
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._finalize_request(
                ticket,
                request_headers=request_headers,
                request_body=request_body,
                response_status=status,
                response_headers=resp_headers,
                response_body=resp_body,
                duration_ms=elapsed_ms,
            )
            return (status, reason, resp_headers, resp_body)

        status, reason, resp_headers, resp_body = self._forward_to_upstream(
            method=ticket["method"],
            path=ticket["path"],
            query=ticket["query"],
            request_headers=request_headers,
            request_body=request_body,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._finalize_request(
            ticket,
            request_headers=request_headers,
            request_body=request_body,
            response_status=status,
            response_headers=resp_headers,
            response_body=resp_body,
            duration_ms=elapsed_ms,
        )
        return (status, reason, resp_headers, resp_body)

    def record_exchange(
        self,
        method: str,
        url: str,
        request_body: bytes | bytearray | str | None = None,
        response_status: Optional[int] = 200,
        response_body: bytes | bytearray | str | None = None,
        *,
        request_headers: Optional[Mapping[str, str] | Iterable[tuple[str, str]]] = None,
        response_headers: Optional[
            Mapping[str, str] | Iterable[tuple[str, str]]
        ] = None,
        duration_ms: float = 0.0,
    ) -> Optional[WireMutationRecord]:
        """Programmatically gate and record an HTTP exchange in-process."""
        req_bytes = _to_bytes(request_body)
        req_hdrs = _header_pairs(request_headers)
        ticket = self._begin_request(
            method=method,
            raw_target=url,
            request_headers=req_hdrs,
            request_body=req_bytes,
        )
        if ticket["blocked"]:
            status, _reason, err_hdrs, err_body = self._blocked_http_payload(
                ticket["block_reason"]
            )
            return self._finalize_request(
                ticket,
                request_headers=req_hdrs,
                request_body=req_bytes,
                response_status=status,
                response_headers=err_hdrs,
                response_body=err_body,
                duration_ms=duration_ms,
            )

        resp_bytes = _to_bytes(response_body) if response_body is not None else None
        resp_hdrs = _header_pairs(response_headers)
        return self._finalize_request(
            ticket,
            request_headers=req_hdrs,
            request_body=req_bytes,
            response_status=response_status,
            response_headers=resp_hdrs,
            response_body=resp_bytes,
            duration_ms=duration_ms,
        )

    def attach_playwright_route(self, page_or_context: Any) -> Callable[[Any], None]:
        """Attach inline gating & recording to a Playwright ``Page`` or ``BrowserContext``.

        Duck-typed on ``.route("**/*", handler)`` with zero ``playwright`` import.
        Blocked requests call ``route.abort()`` before leaving the browser.
        """

        def _extract_route_request(
            request: Any,
        ) -> tuple[str, str, list[tuple[str, str]], bytes]:
            url = str(getattr(request, "url", ""))
            method = str(getattr(request, "method", "GET")).upper()
            raw_headers = getattr(request, "headers", None)
            if callable(raw_headers):
                raw_headers = raw_headers()
            headers = _header_pairs(raw_headers)

            raw_body: Any = getattr(request, "post_data_buffer", None)
            if callable(raw_body):
                raw_body = raw_body()
            if raw_body is None:
                raw_body = getattr(request, "post_data", None)
                if callable(raw_body):
                    raw_body = raw_body()
            if raw_body is None:
                raw_body = getattr(request, "body", None)
                if callable(raw_body):
                    raw_body = raw_body()
            return (method, url, headers, _to_bytes(raw_body))

        def _extract_route_response(
            resp_obj: Any,
        ) -> tuple[Optional[int], list[tuple[str, str]], Optional[bytes]]:
            if resp_obj is None:
                return (None, [], None)
            status_attr = getattr(resp_obj, "status", None)
            status_val = (
                int(status_attr() if callable(status_attr) else status_attr)
                if status_attr is not None
                else None
            )
            hdrs_attr = getattr(resp_obj, "headers", None)
            if callable(hdrs_attr):
                hdrs_attr = hdrs_attr()
            hdrs = _header_pairs(hdrs_attr)
            body_attr = getattr(resp_obj, "body", None)
            if callable(body_attr):
                raw_resp_body = body_attr()
            else:
                raw_resp_body = body_attr
            resp_bytes = _to_bytes(raw_resp_body) if raw_resp_body is not None else None
            return (status_val, hdrs, resp_bytes)

        def _handle_route(route: Any) -> None:
            request = route.request
            url = str(getattr(request, "url", ""))
            if url.lower().startswith(_LOCAL_BROWSER_SCHEMES):
                route.continue_()
                return

            t0 = time.perf_counter()
            method, req_url, req_headers, req_body = _extract_route_request(request)
            ticket = self._begin_request(
                method=method,
                raw_target=req_url,
                request_headers=req_headers,
                request_body=req_body,
            )
            if ticket["blocked"]:
                status, _reason, err_hdrs, err_body = self._blocked_http_payload(
                    ticket["block_reason"]
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self._finalize_request(
                    ticket,
                    request_headers=req_headers,
                    request_body=req_body,
                    response_status=status,
                    response_headers=err_hdrs,
                    response_body=err_body,
                    duration_ms=elapsed_ms,
                )
                route.abort()
                return

            fetch_fn = getattr(route, "fetch", None)
            fulfill_fn = getattr(route, "fulfill", None)
            if callable(fetch_fn) and callable(fulfill_fn):
                fetched = fetch_fn()
                resp_status, resp_headers, resp_body = _extract_route_response(fetched)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self._finalize_request(
                    ticket,
                    request_headers=req_headers,
                    request_body=req_body,
                    response_status=resp_status,
                    response_headers=resp_headers,
                    response_body=resp_body,
                    duration_ms=elapsed_ms,
                )
                fulfill_fn(response=fetched)
                return

            continued_resp = route.continue_()
            if continued_resp is None:
                continued_resp = getattr(route, "response", None)
            resp_status, resp_headers, resp_body = _extract_route_response(
                continued_resp
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._finalize_request(
                ticket,
                request_headers=req_headers,
                request_body=req_body,
                response_status=resp_status,
                response_headers=resp_headers,
                response_body=resp_body,
                duration_ms=elapsed_ms,
            )

        page_or_context.route("**/*", _handle_route)
        return _handle_route


def dump_har(
    har_or_tap: SessionWireTap | Mapping[str, Any],
    path: Optional[str | Path] = None,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Serialize a ``SessionWireTap`` or HAR dictionary to secret-masked HAR 1.2 JSON."""
    raw_har: Mapping[str, Any] = (
        har_or_tap.export_har()
        if isinstance(har_or_tap, SessionWireTap)
        else har_or_tap
    )
    secret_tuple = tuple(s for s in secrets if s)
    scrubbed = _scrub_har_value(dict(raw_har), secret_tuple)
    payload = json.dumps(scrubbed, indent=2, sort_keys=False)
    if path is not None:
        out = Path(path)
        write_private_record(out, payload.encode("utf-8"))
    return payload


def load_har(
    source: str | Path | bytes | Mapping[str, Any],
    *,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Load, validate, and optionally secret-mask a HAR 1.2 structure."""

    def parse_bytes(raw: bytes) -> Any:
        from openadapt_flow import crypto

        if crypto.is_encrypted(raw):
            raw = crypto.decrypt_bytes(
                raw,
                crypto.resolve_read_key(None),
                aad=b"openadapt-flow/tx-private-state",
            )
        return json.loads(raw.decode("utf-8"))

    data: Any
    if isinstance(source, Mapping):
        data = dict(source)
    elif isinstance(source, (bytes, bytearray)):
        data = parse_bytes(bytes(source))
    elif isinstance(source, Path):
        data = parse_bytes(source.read_bytes())
    elif isinstance(source, str):
        stripped = source.lstrip()
        if stripped.startswith("{"):
            data = json.loads(source)
        else:
            data = parse_bytes(Path(source).read_bytes())
    else:
        raise TypeError(f"Unsupported HAR source type: {type(source)!r}")

    if not isinstance(data, dict) or not isinstance(data.get("log"), Mapping):
        raise ValueError("Invalid HAR payload: missing top-level 'log' object")

    log = dict(data["log"])
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid HAR payload: 'log.entries' must be a list")

    normalized: dict[str, Any] = {
        "log": {
            "version": str(log.get("version") or HAR_VERSION),
            "creator": dict(log["creator"])
            if isinstance(log.get("creator"), Mapping)
            else {"name": "openadapt-flow.tx.wire_tap", "version": "1.0"},
            "entries": list(entries),
        }
    }
    secret_tuple = tuple(s for s in secrets if s)
    return _scrub_har_value(normalized, secret_tuple)


def har_to_summary(
    source: str | Path | bytes | Mapping[str, Any],
    *,
    policy: Optional[WirePolicy] = None,
    upstream_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> WireSessionSummary:
    """Compute a ``WireSessionSummary`` directly from a HAR 1.2 document."""
    tap = SessionWireTap.from_har(
        source,
        upstream_url=upstream_url,
        policy=policy,
        session_id=session_id,
    )
    return tap.summary()


def _assert_protocol_conformance(tap: SessionWireTap) -> WireTapProtocol:
    return tap
