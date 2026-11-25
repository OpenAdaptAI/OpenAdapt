"""The front door: a reverse proxy between the agent's browser and the app.

Every request from the browser goes through it. The harness arms it for one
scenario with that scenario's fault; the fault applies once, to the first
request that is the task's save (method, path and an optional body
pattern from the app pack). What each kind does on that save:

=================  ===========================================================
``partial``        the save's body is rewritten so the named fields are blank
``wrong_value``    the save's body is rewritten so a field holds another value
``duplicate``      the save is forwarded twice; the browser gets the first answer
``no_persist``     forwarded; the app answers success; the new record is then
                   removed before the answer reaches the browser
``collateral_loss``  forwarded; another (seed) record is deleted
``collateral_edit``  forwarded; another (seed) record is changed
``late_revert``    forwarded unchanged; ``after_s`` seconds later the new record
                   is changed back (``set``) or deleted (``delete``)
``lost_response``  forwarded; the app commits; the browser gets a 504
``refused``        not forwarded; the browser gets a 503
=================  ===========================================================

A fault counts as delivered only if the app accepted the faulted save (the
pack's ``ok_status``) and the post-save step, if any, succeeded. A save the
app rejected is logged with ``accepted: false``. The hooks that act on the
new record (``no_persist``, ``late_revert``) find it by diffing the table
against the snapshot taken before the agent started.

Bodies may be form-encoded, multipart or JSON. Every non-GET request is
logged, without its body.
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

HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}


@dataclass
class Save:
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
                rb'(Content-Disposition: form-data; name="'
                + re.escape(k.encode())
                + rb'"\r\n(?:[^\r\n]+\r\n)*\r\n)(.*?)(\r\n--)',
                re.S,
            )
            out, n = pat.subn(
                lambda m: m.group(1) + str(v).encode("utf-8") + m.group(3), out, count=1
            )
            if not n:
                raise KeyError(k)
        return out
    pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    keys = {k for k, _ in pairs}
    for k in sets:
        if k not in keys:
            raise KeyError(k)
    return urlencode([(k, str(sets[k]) if k in sets else v) for k, v in pairs]).encode(
        "utf-8"
    )


@dataclass
class Armed:
    """One scenario's fault, compiled for the proxy."""

    kind: str = "none"
    body_set: Optional[dict[str, Any]] = None
    replay_headers: Optional[dict[str, str]] = None
    #: Runs after an accepted save, before the answer goes back.
    after_save: Optional[Callable[[], str]] = None
    #: Runs ``delay_s`` after an accepted save, on a timer.
    later: Optional[Callable[[], str]] = None
    delay_s: float = 0.0


_FAULT_REFUSED = "refused"
_HOOK_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TimeoutError)


class FrontDoor:
    """The proxy; one per app stack, armed per scenario."""

    def __init__(
        self,
        listen: int,
        upstream_host: str,
        upstream_port: int,
        listen_host: str = "127.0.0.1",
    ) -> None:
        self.listen, self.listen_host = listen, listen_host
        self.upstream_host, self.upstream_port = upstream_host, upstream_port
        self.lock = threading.Lock()
        self.save: Optional[Save] = None
        self.fault: Optional[Armed] = None
        self.log: list[dict[str, Any]] = []
        self.delivered = False
        self.saves = 0
        self.ok_status: frozenset[int] = frozenset()
        self.timers: list[threading.Timer] = []
        self.late_result = ""
        self.late_due = 0.0
        self._srv: Optional[ThreadingHTTPServer] = None

    # -- control -----------------------------------------------------------------
    def arm(self, save: Save, fault: Armed, ok_status: frozenset[int]) -> None:
        with self.lock:
            self.save, self.fault, self.ok_status = save, fault, ok_status
            self.log, self.delivered, self.saves = [], False, 0
            self.timers, self.late_result, self.late_due = [], "", 0.0

    def disarm(self) -> None:
        with self.lock:
            self.fault = None

    def accepted(self, status: int) -> bool:
        return status in self.ok_status if self.ok_status else status < 400

    def wait_late(self, max_s: float) -> str:
        """Wait (at most ``max_s``) for a pending late fault; its result."""

        deadline = time.time() + max_s
        for t in list(self.timers):
            t.join(timeout=max(0.0, deadline - time.time()))
        return self.late_result

    def cancel_late(self) -> None:
        for t in self.timers:
            t.cancel()

    def _take(
        self, method: str, path: str, body: bytes
    ) -> tuple[Optional[Armed], bool]:
        with self.lock:
            if not (self.save and self.save.matches(method, path, body)):
                return None, False
            self.saves += 1
            fault, self.fault = self.fault, None
            if fault is None or fault.kind == "none":
                return None, True
            return fault, True

    # -- server ------------------------------------------------------------------
    def start(self) -> "FrontDoor":
        door = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _forward(
                self, body: bytes, extra: Optional[dict[str, str]] = None
            ) -> tuple[int, str, list[tuple[str, str]], bytes]:
                conn = http.client.HTTPConnection(
                    door.upstream_host, door.upstream_port, timeout=180
                )
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-length"
                }
                headers.update(extra or {})
                headers["Connection"] = "close"
                if body or self.command in ("POST", "PUT", "PATCH"):
                    headers["Content-Length"] = str(len(body))
                conn.request(
                    self.command, self.path, body=body or None, headers=headers
                )
                r = conn.getresponse()
                data = r.read()
                out = (
                    r.status,
                    r.reason,
                    [(k, v) for k, v in r.getheaders() if k.lower() not in HOP],
                    data,
                )
                conn.close()
                return out

            def _reply(
                self,
                status: int,
                reason: str,
                headers: list[tuple[str, str]],
                data: bytes,
            ) -> None:
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
                fault, is_save = door._take(self.command, path, body)
                t0 = time.time()
                entry: dict[str, Any] = {
                    "t": round(t0, 3),
                    "method": self.command,
                    "path": path[:200],
                    "save": is_save,
                }
                ok = True
                try:
                    if fault is not None:
                        entry["fault"] = fault.kind
                    if fault is not None and fault.body_set:
                        try:
                            body = rewrite(
                                body,
                                self.headers.get("Content-Type", ""),
                                fault.body_set,
                            )
                            entry["rewritten"] = sorted(fault.body_set)
                        except (KeyError, ValueError) as exc:
                            entry["rewrite_failed"] = str(exc)
                            ok = False
                    if fault is not None and fault.kind == _FAULT_REFUSED:
                        entry["status"] = 503
                        with door.lock:
                            door.delivered = ok
                        self._reply(
                            503,
                            "Service Unavailable",
                            [("Content-Type", "text/plain")],
                            b"Service Unavailable",
                        )
                        return
                    st, reason, headers, data = self._forward(body)
                    entry["status"] = st
                    if fault is not None:
                        entry["accepted"] = door.accepted(st)
                        ok = ok and entry["accepted"]
                        if fault.kind == "duplicate":
                            st2, *_ = self._forward(body, fault.replay_headers)
                            entry["status"] = f"{st}+{st2}"
                            entry["second_accepted"] = door.accepted(st2)
                            ok = ok and entry["second_accepted"]
                        if fault.after_save is not None:
                            if ok:
                                try:
                                    entry["after_save"] = fault.after_save()
                                    ok = not entry["after_save"].startswith("failed")
                                except _HOOK_ERRORS as exc:
                                    entry["after_save"] = f"failed: {exc}"[:300]
                                    ok = False
                            else:
                                entry["after_save"] = (
                                    "skipped: the save was not accepted"
                                )
                        if fault.later is not None:
                            if ok:
                                later = fault.later

                                def fire() -> None:
                                    try:
                                        door.late_result = later()
                                    except _HOOK_ERRORS as exc:
                                        door.late_result = f"failed: {exc}"[:300]

                                t = threading.Timer(fault.delay_s, fire)
                                t.daemon = True
                                door.timers.append(t)
                                door.late_due = time.time() + fault.delay_s
                                t.start()
                                entry["late"] = f"scheduled in {fault.delay_s:.0f} s"
                            else:
                                entry["late"] = "skipped: the save was not accepted"
                        with door.lock:
                            door.delivered = ok
                    if fault is not None and fault.kind == "lost_response":
                        entry["status"] = f"{entry['status']}->504"
                        self._reply(
                            504,
                            "Gateway Timeout",
                            [("Content-Type", "text/plain")],
                            b"",
                        )
                        return
                    self._reply(st, reason, headers, data)
                except (ConnectionError, OSError, http.client.HTTPException) as exc:
                    entry["error"] = str(exc)[:200]
                    try:
                        self._reply(
                            502, "Bad Gateway", [("Content-Type", "text/plain")], b""
                        )
                    except OSError:
                        pass
                finally:
                    entry["ms"] = round((time.time() - t0) * 1000)
                    if is_save or self.command != "GET":
                        with door.lock:
                            door.log.append(entry)

            do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = (
                _handle
            )

        self._srv = ThreadingHTTPServer((self.listen_host, self.listen), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.cancel_late()
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
