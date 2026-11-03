"""Capture a web recording's network traffic (``record --capture-network``).

Opt-in. While a browser recording runs, :class:`NetworkCapture` keeps one
line per finished page request: method, URL, form or text body, status,
content type, redirect target and, for HTML, JSON and text, the response
body. ``compile`` reads the file (``network.jsonl``) and may propose an API
step for a save whose requests it can explain
(:mod:`openadapt_flow.compiler.api_proposal`).

What is never written:

- headers, so no cookie or authorization value. Cookie values the browser
  sent, cookie values a response set (``Set-Cookie``) and ``Authorization``
  values are also withheld wherever a page echoes them in a body, URL or
  script (``<session:cookie>``, ``<session:authorization>``);
- values of password fields and of the recording's declared secret fields,
  in bodies, URLs and response text (replaced by ``<secret>``);
- session tokens. A field whose name looks like a CSRF, session or API token
  (``csrf``, ``xsrf``, ``token``, ``nonce``, ``authenticity``, ``session``,
  ``api_key``, ``jwt``, ``bearer``) has its value replaced by
  ``<session:NAME>`` everywhere it appears, including the HTML that carried
  it. The API step reads a fresh token from the live page each run.

Names are found in form bodies, URL queries, and JSON request and response
bodies at any depth. A JSON body has those values replaced field by field,
whatever their length, then every value found is withheld wherever else it
appears.

Patient or other business values the person typed stay in the file, as they
do in ``events.jsonl``. A recording made with capture is as sensitive as any
other recording.

Static assets (images, fonts, style sheets, scripts, media) are skipped.
Request and response bodies are cut at :data:`MAX_REQUEST_BYTES` and
:data:`MAX_RESPONSE_BYTES`; a cut body is marked ``truncated``.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse as up
from pathlib import Path
from typing import Any, Iterable, Optional

NETWORK_FILE = "network.jsonl"
SCHEMA = "openadapt.network-capture/1"

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _page_of(request: Any) -> Any:
    """The page a request came from, or None (a service worker has none)."""

    try:
        return request.frame.page
    except Exception:  # noqa: BLE001 - service-worker and detached-frame requests
        return None


_SKIP_TYPES = frozenset(
    {"image", "font", "stylesheet", "script", "media", "manifest", "texttrack"}
)
_TEXT_MIME = re.compile(
    r"text/html|application/json|text/plain|application/xhtml|\+json", re.I
)
TOKEN_NAME = re.compile(
    r"csrf|xsrf|token|nonce|authenticity|session|api[_-]?key|jwt|bearer"
    r"|authorization|^sid$|^auth$",
    re.I,
)
SECRET_NAME = re.compile(
    r"pass(word|wd|phrase)?\b|passwd|clearpass|pwd|secret|private[_-]?key|^pin$|^otp$",
    re.I,
)
SECRET = "<secret>"


def session_placeholder(name: str) -> str:
    return f"<session:{name}>"


class NetworkCapture:
    """Record a Playwright browser context's requests for one recording.

    Start it before the browser opens the form, so the form page itself is
    captured. Each line is stamped on the monotonic clock; :meth:`write`
    converts that to the recorder's ``t`` (seconds since the recording
    started), so lines and events share one timeline. Lines from before the
    recording started have a negative ``t``.

    ``secret_names`` are field names whose values are withheld in addition
    to password-like names (the recorder's declared secret fields).
    """

    def __init__(self, context: Any, *, secret_names: Iterable[str] = ()) -> None:
        self._context = context
        self._secret_names = frozenset(secret_names)
        # Cookie values the browser sent or a response set, and Authorization
        # values, kept in memory only so they can be withheld wherever a page
        # echoes one. Never written.
        self._cookie_values: set[str] = set()
        self._auth_values: set[str] = set()
        self._lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._attached = False
        # Writes the browser has sent and not seen finish or fail, with when
        # they were sent. A save the agent guard holds stays here until the
        # guard sends or refuses it.
        self._pending_writes: dict[Any, float] = {}

    def start(self) -> "NetworkCapture":
        if not self._attached:
            self._context.on("request", self._on_request)
            self._context.on("requestfinished", self._on_finished)
            self._context.on("requestfailed", self._on_failed)
            self._attached = True
        return self

    def stop(self) -> None:
        if self._attached:
            for event, handler in (
                ("request", self._on_request),
                ("requestfinished", self._on_finished),
                ("requestfailed", self._on_failed),
            ):
                try:
                    self._context.remove_listener(event, handler)
                except Exception:  # noqa: BLE001 - a closed context has no listeners
                    pass
            self._attached = False

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._lines)

    def pending_writes(
        self,
        pages: Optional[Iterable[Any]] = None,
        *,
        max_age_s: Optional[float] = None,
    ) -> list[Any]:
        """Writes (any method but GET, HEAD or OPTIONS) still in flight.

        ``pages`` keeps only requests from those pages, so another tab's
        long-poll in an attached browser is never waited for. ``max_age_s``
        drops requests sent longer ago than that: a request whose finish
        Playwright never reports (a beacon from a closing popup) must not
        stall the rest of a recording.
        """

        now = time.monotonic()
        with self._lock:
            pending = list(self._pending_writes.items())
        wanted = None if pages is None else list(pages)
        found = []
        for request, sent in pending:
            if max_age_s is not None and now - sent > max_age_s:
                continue
            if wanted is not None:
                page = _page_of(request)
                if page is None or not any(page is p for p in wanted):
                    continue
            found.append(request)
        return found

    # -- Playwright callbacks ------------------------------------------------

    def _on_request(self, request: Any) -> None:
        if request.resource_type in _SKIP_TYPES:
            return
        if str(request.method).upper() not in _READ_METHODS:
            with self._lock:
                self._pending_writes[request] = time.monotonic()

    def _settled(self, request: Any) -> None:
        with self._lock:
            self._pending_writes.pop(request, None)

    def _on_failed(self, request: Any) -> None:
        self._settled(request)
        if request.resource_type in _SKIP_TYPES:
            return
        self._append(self._request_line(request) | {"status": None, "failed": True})

    def _on_finished(self, request: Any) -> None:
        self._settled(request)
        if request.resource_type in _SKIP_TYPES:
            return
        line = self._request_line(request)
        try:
            response = request.response()
        except Exception:  # noqa: BLE001 - the page may already be gone
            response = None
        if response is not None:
            headers = {k.lower(): v for k, v in (response.headers or {}).items()}
            for cookie in _set_cookie_values(response, headers):
                self._cookie_values.add(cookie)
            line["status"] = response.status
            line["response_type"] = (
                (headers.get("content-type") or "").split(";")[0].strip()
            )
            if headers.get("location"):
                line["location"] = headers["location"]
            if _TEXT_MIME.search(line["response_type"]):
                try:
                    body = response.body()
                except Exception:  # noqa: BLE001 - redirect or evicted body
                    body = b""
                if len(body) > MAX_RESPONSE_BYTES:
                    line["response_truncated"] = True
                    body = body[:MAX_RESPONSE_BYTES]
                line["response_text"] = body.decode("utf-8", errors="replace")
        self._append(line)

    def _request_line(self, request: Any) -> dict[str, Any]:
        try:
            headers = {k.lower(): v for k, v in request.all_headers().items()}
        except Exception:  # noqa: BLE001 - the request is gone
            headers = {k.lower(): v for k, v in (request.headers or {}).items()}
        for part in (headers.get("cookie") or "").split(";"):
            value = part.partition("=")[2].strip()
            if len(value) >= 8:
                self._cookie_values.add(value)
        auth = (headers.get("authorization") or "").strip()
        if auth:
            # "Bearer abc", "Basic xyz": the credential is the last word.
            credential = auth.split()[-1]
            if len(credential) >= 4:
                self._auth_values.add(credential)
        line: dict[str, Any] = {
            "_mono": time.monotonic(),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "frame_url": _frame_url(request),
        }
        content_type = (headers.get("content-type") or "").split(";")[0].strip()
        if content_type:
            line["request_type"] = content_type
        try:
            raw = request.post_data_buffer
        except Exception:  # noqa: BLE001 - binary or unavailable body
            raw = None
        if raw:
            if len(raw) > MAX_REQUEST_BYTES:
                line["request_truncated"] = True
                raw = raw[:MAX_REQUEST_BYTES]
            text = raw.decode("utf-8", errors="replace")
            if content_type == "application/x-www-form-urlencoded":
                line["form"] = [
                    list(p) for p in up.parse_qsl(text, keep_blank_values=True)
                ]
            elif content_type.startswith("multipart/"):
                line["body_omitted"] = "multipart"
            else:
                line["body"] = text
        return line

    def _append(self, line: dict[str, Any]) -> None:
        with self._lock:
            line["seq"] = len(self._lines)
            self._lines.append(line)

    # -- writing ---------------------------------------------------------------

    def lines(self, t0_monotonic: float) -> list[dict[str, Any]]:
        """The captured lines, timed from ``t0_monotonic``, with secrets and
        session tokens withheld."""

        with self._lock:
            lines = [dict(line) for line in self._lines]
        for line in lines:
            line["t"] = round(line.pop("_mono") - t0_monotonic, 3)
        return redact(
            lines,
            secret_names=self._secret_names,
            session_values=set(self._cookie_values),
            auth_values=set(self._auth_values),
        )

    def write(self, recording_dir: Path | str, t0_monotonic: float) -> Path:
        from openadapt_flow import sealed_set

        path = Path(recording_dir) / NETWORK_FILE
        lines = self.lines(t0_monotonic)
        text = json.dumps({"schema": SCHEMA, "exchanges": len(lines)}) + "\n"
        text += "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
        # Sealed in memory when the recording is encrypted.
        sealed_set.write_text(path, text)
        return path


def _frame_url(request: Any) -> Optional[str]:
    try:
        return request.frame.url
    except Exception:  # noqa: BLE001 - service worker or detached frame
        return None


def _set_cookie_values(response: Any, headers: dict[str, str]) -> list[str]:
    """The values a response set with ``Set-Cookie``."""

    raw: list[str] = []
    try:
        for item in response.headers_array() or []:
            if str(item.get("name", "")).lower() == "set-cookie":
                raw.append(str(item.get("value", "")))
    except Exception:  # noqa: BLE001 - older drivers have no headers_array
        pass
    if not raw and headers.get("set-cookie"):
        raw = str(headers["set-cookie"]).split("\n")
    values = []
    for cookie in raw:
        value = cookie.split(";", 1)[0].partition("=")[2].strip()
        if len(value) >= 8:
            values.append(value)
    return values


def _json(text: Any) -> Any:
    if not isinstance(text, str) or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _json_pairs(value: Any) -> Iterable[tuple[str, str]]:
    """Every (key, scalar value) pair in a JSON document, at any depth."""

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from _json_pairs(item)
            elif item is not None and not isinstance(item, bool):
                yield str(key), str(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_pairs(item)


def redact(
    lines: list[dict[str, Any]],
    *,
    secret_names: Iterable[str] = (),
    session_values: Iterable[str] = (),
    auth_values: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Withhold secret values and session tokens from captured lines.

    Values are found by field name in request bodies, queries and JSON
    request and response bodies (at any depth), then replaced wherever the
    same value appears (other requests, URLs, response text): password-like
    and declared secret names become ``<secret>``, token-like names become
    ``<session:NAME>``. ``session_values`` (cookies) and ``auth_values``
    (``Authorization`` credentials) are withheld wherever they appear.
    """

    declared = {n.lower() for n in secret_names}

    def kind(name: str) -> Optional[str]:
        if SECRET_NAME.search(name) or name.lower() in declared:
            return SECRET
        if TOKEN_NAME.search(name):
            return session_placeholder(name)
        return None

    replace: dict[str, str] = {
        v: session_placeholder("cookie") for v in session_values if v
    }
    for v in auth_values:
        if v:
            replace.setdefault(v, session_placeholder("authorization"))
    for line in lines:
        pairs = list(line.get("form") or []) + up.parse_qsl(
            up.urlsplit(line["url"]).query, keep_blank_values=True
        )
        for key in ("body", "response_text"):
            pairs += list(_json_pairs(_json(line.get(key))))
        for name, value in pairs:
            if not value or len(value) < 4:
                continue
            placeholder = kind(name)
            if placeholder == SECRET:
                replace[value] = SECRET
            elif placeholder is not None and len(value) >= 8:
                replace.setdefault(value, placeholder)

    def scrub_json(value: Any) -> Any:
        # A JSON body's secret and token fields, replaced whatever their
        # length: the field name, not the value, says what it is.
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                placeholder = kind(str(key))
                if (
                    placeholder is not None
                    and item not in (None, "")
                    and not isinstance(item, (dict, list, bool))
                ):
                    out[key] = placeholder
                else:
                    out[key] = scrub_json(item)
            return out
        if isinstance(value, list):
            return [scrub_json(item) for item in value]
        return value

    structured = []
    for line in lines:
        line = dict(line)
        for key in ("body", "response_text"):
            document = _json(line.get(key))
            if document is not None:
                line[key] = json.dumps(scrub_json(document), separators=(",", ":"))
        structured.append(line)
    lines = structured
    if not replace:
        return lines
    # Longest first, so a value inside another is not replaced half-way.
    order = sorted(replace, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in order))
    encoded = {up.quote_plus(v): replace[v] for v in order if up.quote_plus(v) != v}
    enc_pattern = (
        re.compile(
            "|".join(re.escape(v) for v in sorted(encoded, key=len, reverse=True))
        )
        if encoded
        else None
    )

    def scrub(text: str) -> str:
        text = pattern.sub(lambda m: replace[m.group(0)], text)
        if enc_pattern is not None:
            text = enc_pattern.sub(lambda m: encoded[m.group(0)], text)
        return text

    out = []
    for line in lines:
        line = dict(line)
        for key in ("url", "frame_url", "location", "body", "response_text"):
            if isinstance(line.get(key), str):
                line[key] = scrub(line[key])
        if line.get("form"):
            line["form"] = [
                [
                    name,
                    SECRET
                    if (SECRET_NAME.search(name) or name.lower() in declared) and value
                    else replace.get(value, scrub(value)),
                ]
                for name, value in line["form"]
            ]
        out.append(line)
    return out


def load(recording_dir: Path | str) -> list[dict[str, Any]]:
    """Read ``network.jsonl``; an absent file is an empty capture."""

    from openadapt_flow import sealed_set

    path = Path(recording_dir) / NETWORK_FILE
    if not sealed_set.is_file(path):
        return []
    rows = [
        json.loads(line)
        for line in sealed_set.read_text(path).splitlines()
        if line.strip()
    ]
    if not rows or rows[0].get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} capture")
    return rows[1:]
