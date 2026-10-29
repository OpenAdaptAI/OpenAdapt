"""The agent's way to Medplum: a local FHIR proxy that injects the case's faults.

The agent talks to ``http://127.0.0.1:<port>/fhir/R4`` with its own bearer
token. Reads pass through unchanged. The case's faults apply once, to the
agent's first write of the task's resource type, and model what goes wrong
between an agent and a system of record: a tool that changes a value, a
retry that sends twice, a reply lost after the save, a refused save, or a
tool with a side effect. Side writes use the agent's own token, so the
server's audit trail attributes them to the agent, as it would a real tool's.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from benchmark.fhir_medplum.public.model import instant

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
FORWARDED_HEADERS = (
    "Authorization",
    "Content-Type",
    "Accept",
    "If-None-Exist",
    "If-Match",
    "Prefer",
)


def _send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[bytes],
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _outcome(code: str, text: str) -> bytes:
    return json.dumps(
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": text}],
        }
    ).encode()


# -- body mutations ------------------------------------------------------------------


def _replace_strings(node: Any, old: str, new: str) -> Any:
    if isinstance(node, dict):
        return {k: _replace_strings(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_strings(v, old, new) for v in node]
    return new if node == old else node


def _swap_last_digits(value: str) -> str:
    pos = [i for i, ch in enumerate(value) if ch.isdigit()]
    if len(pos) < 2:
        return value
    chars = list(value)
    a, b = pos[-2], pos[-1]
    chars[a], chars[b] = chars[b], chars[a]
    return "".join(chars)


def _shift(value: Any, hours: float) -> Any:
    when = instant(value)
    if when is None:
        return value
    return (when + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mutate(
    body: dict[str, Any], fault: dict[str, Any], refs: dict[str, str]
) -> dict[str, Any]:
    """The body after one fault's change (a copy); unchanged if it doesn't apply."""

    out = copy.deepcopy(body)
    for key, value in (fault.get("set") or {}).items():
        out[key] = value
    if fault.get("phone_swap"):
        for t in out.get("telecom") or []:
            if isinstance(t, dict) and t.get("system") == "phone" and t.get("value"):
                t["value"] = _swap_last_digits(str(t["value"]))
                break
    if fault.get("shift_hours"):
        for key in ("start", "end"):
            if key in out:
                out[key] = _shift(out[key], float(fault["shift_hours"]))
    if fault.get("replace"):
        out = _replace_strings(out, fault["replace"]["from"], fault["replace"]["to"])
    if fault.get("retarget"):
        old, new = refs[fault["retarget"]["from"]], refs[fault["retarget"]["to"]]
        out = _replace_strings(out, old, new)
    return out


class FaultProxy:
    """A threaded HTTP proxy for one run; ``arm(case)`` sets the next case's faults."""

    def __init__(self, upstream_fhir: str, port: int = 0) -> None:
        self.upstream = upstream_fhir.rstrip("/")
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.server.server_address[1]
        self._lock = threading.Lock()
        self._faults: list[dict[str, Any]] = []
        self._rtype = ""
        self._refs: dict[str, str] = {}
        self._fired = False
        self._token = ""
        self.after_check: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        #: Called as side_write(op, refs, token) for a write the fault adds.
        self.side_write: Optional[
            Callable[[dict[str, Any], dict[str, str], str], None]
        ] = None
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def fhir_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/fhir/R4"

    def start(self) -> "FaultProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def arm(
        self, faults: list[dict[str, Any]], resource_type: str, refs: dict[str, str]
    ) -> None:
        with self._lock:
            self._faults = [copy.deepcopy(f) for f in faults]
            self._rtype = resource_type
            self._refs = dict(refs)
            self._fired = False
            self._token = ""
            self.after_check = []
            self.log = []

    def run_after_check(self) -> None:
        """Make the side writes a fault holds for after the check."""

        with self._lock:
            pending, self.after_check = self.after_check, []
            token, refs = self._token, dict(self._refs)
        for op in pending:
            if self.side_write is not None and token:
                self.side_write(op, refs, token)
                self.log.append({"side_write": op.get("op"), "at": "after_check"})

    # -- the handler --------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # quiet
                pass

            def _do(self) -> None:
                proxy._handle(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

        return Handler

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        if not h.path.startswith("/fhir/R4"):
            self._reply(
                h,
                404,
                {"Content-Type": "application/fhir+json"},
                _outcome("not-found", "this proxy serves /fhir/R4 only"),
            )
            return
        rest = h.path[len("/fhir/R4") :]
        url = self.upstream + rest
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else None
        headers = {k: h.headers[k] for k in FORWARDED_HEADERS if h.headers.get(k)}
        method = h.command
        rtype = rest.lstrip("/").split("?")[0].split("/")[0]
        faults: list[dict[str, Any]] = []
        with self._lock:
            if headers.get("Authorization"):
                self._token = headers["Authorization"].split(" ", 1)[-1]
            if method in WRITE_METHODS and rtype == self._rtype and not self._fired:
                self._fired = True
                faults = self._faults
            refs = dict(self._refs)
        self.log.append({"method": method, "type": rtype, "faulted": bool(faults)})
        if not faults:
            self._reply(h, *_send(method, url, headers, body))
            return
        if any(f.get("refuse") for f in faults):
            self.log.append({"fault": "refuse"})
            self._reply(
                h,
                503,
                {"Content-Type": "application/fhir+json"},
                _outcome("transient", "service unavailable"),
            )
            return
        if body:
            try:
                doc = json.loads(body)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                for f in faults:
                    doc = mutate(doc, f, refs)
                body = json.dumps(doc).encode()
                headers["Content-Length"] = str(len(body))
        status, rheaders, rbody = _send(method, url, headers, body)
        if any(f.get("duplicate") for f in faults):
            _send(method, url, headers, body)
            self.log.append({"fault": "duplicate"})
        token = headers.get("Authorization", "").split(" ", 1)[-1]
        for f in faults:
            op = f.get("side_write")
            if not op:
                continue
            if op.get("at") == "after_check":
                with self._lock:
                    self.after_check.append(op)
            elif self.side_write is not None:
                self.side_write(op, refs, token)
                self.log.append({"side_write": op.get("op")})
        if any(f.get("drop_response") for f in faults):
            self.log.append({"fault": "drop_response", "upstream_status": status})
            self._reply(
                h,
                504,
                {"Content-Type": "application/fhir+json"},
                _outcome("timeout", "gateway timeout: no reply from the server"),
            )
            return
        self._reply(h, status, rheaders, rbody)

    def _reply(
        self,
        h: BaseHTTPRequestHandler,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        h.send_response(status)
        for key, value in headers.items():
            if key.lower() in ("content-type", "location", "etag", "last-modified"):
                h.send_header(key, value)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
