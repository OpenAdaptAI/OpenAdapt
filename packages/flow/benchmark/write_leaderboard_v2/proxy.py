"""The front door: a reverse proxy between the browser and the app that injects faults.

Every request from the agent's browser goes through it. The harness arms it
for one run with the case's faults; they apply once, to the first request
that is the task's save (``Save``: method, path, and optionally a body
pattern). Fault kinds, the same families as version 1's Medplum proxy:

- ``set: {field: value}`` (wrong_value): the save's body is rewritten so
  ``field`` holds ``value``; the app stores the changed value and shows it.
- ``duplicate: true``: the save is forwarded twice; the browser gets the
  first answer. ``replay_headers`` are added to the second delivery only
  (for an app that would otherwise refuse a repeated save as a duplicate).
- ``drop_response: true`` (lost_response): forwarded; the app commits; the
  browser gets a 504 with an empty body.
- ``refuse: true`` (refused_write): not forwarded; the browser gets a 503.
- ``side_write: true`` (collateral): forwarded unchanged, then the harness's
  ``side_write`` hook changes another record before the answer is returned.

Bodies may be form-encoded, multipart or JSON. Every request is logged.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode

HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade"}


@dataclass
class Save:
    """Which request is the task's save."""

    path_regex: str
    method: str = "POST"
    body_require: str = ""
    body_exclude: str = ""

    def matches(self, method: str, path: str, body: bytes) -> bool:
        if method != self.method or not re.search(self.path_regex, path):
            return False
        text = body.decode("latin-1")
        if self.body_require and not re.search(self.body_require, text):
            return False
        if self.body_exclude and re.search(self.body_exclude, text):
            return False
        return True


def rewrite(body: bytes, content_type: str, sets: dict[str, Any]) -> bytes:
    """``body`` with each field in ``sets`` replaced (form, multipart or JSON)."""

    ct = (content_type or "").lower()
    if "json" in ct:
        doc = json.loads(body.decode("utf-8") or "{}")
        for k, v in sets.items():
            doc[k] = v
        return json.dumps(doc).encode("utf-8")
    if "multipart/form-data" in ct:
        out = body
        for k, v in sets.items():
            pat = re.compile(
                rb'(Content-Disposition: form-data; name="' + re.escape(k.encode()) + rb'"\r\n(?:[^\r\n]+\r\n)*\r\n)(.*?)(\r\n--)',
                re.S,
            )
            out, n = pat.subn(lambda m: m.group(1) + str(v).encode("utf-8") + m.group(3), out, count=1)
            if not n:
                raise KeyError(k)
        return out
    pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    keys = {k for k, _ in pairs}
    for k in sets:
        if k not in keys:
            raise KeyError(k)
    return urlencode([(k, str(sets[k]) if k in sets else v) for k, v in pairs]).encode("ascii")


class FrontDoor:
    """The proxy; one per app instance, armed per run."""

    def __init__(self, listen: int, upstream: int, upstream_host: str = "127.0.0.1") -> None:
        self.listen, self.upstream, self.upstream_host = listen, upstream, upstream_host
        self.lock = threading.Lock()
        self.save: Optional[Save] = None
        self.faults: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        self.delivered = False
        self.saves = 0
        self.side_write: Optional[Callable[[], None]] = None
        self._srv: Optional[ThreadingHTTPServer] = None

    # -- control --------------------------------------------------------------------
    def arm(self, save: Save, faults: list[dict[str, Any]]) -> None:
        with self.lock:
            self.save, self.faults = save, list(faults)
            self.log, self.delivered, self.saves = [], False, 0

    def disarm(self) -> None:
        with self.lock:
            self.faults = []

    def _take(self, method: str, path: str, body: bytes) -> tuple[list[dict[str, Any]], bool]:
        with self.lock:
            is_save = bool(self.save and self.save.matches(method, path, body))
            if not is_save:
                return [], False
            self.saves += 1
            if not self.faults:
                return [], True
            faults, self.faults = self.faults, []
            self.delivered = True
            return faults, True

    # -- server ---------------------------------------------------------------------
    def start(self) -> "FrontDoor":
        door = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _forward(self, body: bytes, extra: Optional[dict[str, str]] = None) -> tuple[int, str, list[tuple[str, str]], bytes]:
                conn = http.client.HTTPConnection(door.upstream_host, door.upstream, timeout=180)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "content-length"}
                headers.update(extra or {})
                headers["Connection"] = "close"
                if body or self.command in ("POST", "PUT", "PATCH"):
                    headers["Content-Length"] = str(len(body))
                conn.request(self.command, self.path, body=body or None, headers=headers)
                r = conn.getresponse()
                data = r.read()
                out = (r.status, r.reason, [(k, v) for k, v in r.getheaders() if k.lower() not in HOP], data)
                conn.close()
                return out

            def _reply(self, status: int, reason: str, headers: list[tuple[str, str]], data: bytes) -> None:
                self.send_response(status, reason)
                for k, v in headers:
                    if k.lower() != "content-length":
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                self.close_connection = True

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = self.path.split("?")[0]
                faults, is_save = door._take(self.command, path, body)
                t0 = time.time()
                entry: dict[str, Any] = {"t": round(t0, 3), "method": self.command, "path": path[:200], "save": is_save}
                try:
                    f: dict[str, Any] = {}
                    for x in faults:
                        f.update(x)
                    if f:
                        entry["fault"] = sorted(f)
                    if f.get("set"):
                        try:
                            body = rewrite(body, self.headers.get("Content-Type", ""), f["set"])
                            entry["rewritten"] = sorted(f["set"])
                        except (KeyError, ValueError) as exc:
                            entry["rewrite_failed"] = str(exc)
                            with door.lock:
                                door.delivered = False
                    if f.get("refuse"):
                        entry["status"] = 503
                        self._reply(503, "Service Unavailable", [("Content-Type", "text/plain")], b"Service Unavailable")
                        return
                    st, reason, headers, data = self._forward(body)
                    entry["status"] = st
                    if f.get("duplicate"):
                        st2, *_ = self._forward(body, f.get("replay_headers"))
                        entry["status"] = f"{st}+{st2}"
                    if f.get("side_write") and door.side_write is not None and st < 400:
                        try:
                            door.side_write()
                            entry["side_write"] = "done"
                        except Exception as exc:  # noqa: BLE001
                            entry["side_write"] = f"failed: {exc}"[:300]
                            with door.lock:
                                door.delivered = False
                    elif f.get("side_write"):
                        entry["side_write"] = "skipped: the save failed"
                        with door.lock:
                            door.delivered = False
                    if f.get("drop_response"):
                        entry["status"] = f"{st}->504"
                        self._reply(504, "Gateway Timeout", [("Content-Type", "text/plain")], b"")
                        return
                    self._reply(st, reason, headers, data)
                except (ConnectionError, OSError, http.client.HTTPException) as exc:
                    entry["error"] = str(exc)[:200]
                    try:
                        self._reply(502, "Bad Gateway", [("Content-Type", "text/plain")], b"")
                    except OSError:
                        pass
                finally:
                    entry["ms"] = round((time.time() - t0) * 1000)
                    if is_save or self.command != "GET":
                        with door.lock:
                            door.log.append(entry)

            do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.listen), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
