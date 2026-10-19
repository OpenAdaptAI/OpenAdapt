"""Capture a web recording's network traffic (``record --capture-network``).

Opt-in. While a browser recording runs, :class:`NetworkCapture` keeps one
line per finished page request: method, URL, form or text body, status,
content type, redirect target and, for HTML, JSON and text, the response
body. ``compile`` reads the file (``network.jsonl``) and may propose an API
step for a save whose requests it can explain
(:mod:`openadapt_flow.compiler.api_proposal`).

What is never written:

- headers, so no cookie or authorization value. Cookie values the browser
  sent are also withheld where a page echoes them in a body or script
  (``<session:cookie>``);
- values of password fields and of the recording's declared secret fields,
  in bodies, URLs and response text (replaced by ``<secret>``);
- session tokens. A body or query field whose name looks like a CSRF or
  session token (``csrf``, ``xsrf``, ``token``, ``nonce``, ``authenticity``)
  has its value replaced by ``<session:NAME>`` everywhere it appears,
  including the HTML that carried it. The API step reads a fresh token from
  the live page each run.

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

_SKIP_TYPES = frozenset(
    {"image", "font", "stylesheet", "script", "media", "manifest", "texttrack"}
)
_TEXT_MIME = re.compile(
    r"text/html|application/json|text/plain|application/xhtml|\+json", re.I
)
TOKEN_NAME = re.compile(r"csrf|xsrf|token|nonce|authenticity", re.I)
SECRET_NAME = re.compile(r"pass(word|wd)?\b|clearpass|pwd|secret", re.I)
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
        # Cookie values the browser sent, kept in memory only so they can be
        # withheld wherever a page echoes one. Never written.
        self._cookie_values: set[str] = set()
        self._lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._attached = False

    def start(self) -> "NetworkCapture":
        if not self._attached:
            self._context.on("requestfinished", self._on_finished)
            self._context.on("requestfailed", self._on_failed)
            self._attached = True
        return self

    def stop(self) -> None:
        if self._attached:
            for event, handler in (
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

    # -- Playwright callbacks ------------------------------------------------

    def _on_failed(self, request: Any) -> None:
        if request.resource_type in _SKIP_TYPES:
            return
        self._append(self._request_line(request) | {"status": None, "failed": True})

    def _on_finished(self, request: Any) -> None:
        if request.resource_type in _SKIP_TYPES:
            return
        line = self._request_line(request)
        try:
            response = request.response()
        except Exception:  # noqa: BLE001 - the page may already be gone
            response = None
        if response is not None:
            headers = {k.lower(): v for k, v in (response.headers or {}).items()}
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


def redact(
    lines: list[dict[str, Any]],
    *,
    secret_names: Iterable[str] = (),
    session_values: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Withhold secret values and session tokens from captured lines.

    Values are found by field name in request bodies and queries, then
    replaced wherever the same value appears (other requests, URLs, response
    text): password-like and declared secret names become ``<secret>``,
    token-like names become ``<session:NAME>``.
    """

    declared = {n.lower() for n in secret_names}
    replace: dict[str, str] = {
        v: session_placeholder("cookie") for v in session_values if v
    }
    for line in lines:
        pairs = list(line.get("form") or []) + up.parse_qsl(
            up.urlsplit(line["url"]).query, keep_blank_values=True
        )
        for name, value in pairs:
            if not value or len(value) < 4:
                continue
            if SECRET_NAME.search(name) or name.lower() in declared:
                replace[value] = SECRET
            elif TOKEN_NAME.search(name) and len(value) >= 8:
                replace.setdefault(value, session_placeholder(name))
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
