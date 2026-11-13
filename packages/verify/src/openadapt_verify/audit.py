"""Audit your automation's success check: how many wrong saves does it pass?

``openadapt-verify audit`` starts an HTTP proxy in front of your app, runs your
command (a Playwright test, a browser-agent script, any program) against the
proxy several times, and on each run changes one write in a named way: the
save never reaches the app, half of it does, it lands twice, the reply is
lost. It records whether your command said it succeeded, then reads the store
through the contract's independent read path (never through the proxy) to
find out what actually happened.

Every fault is applied at the HTTP layer alone, so the audit needs no access
to the app's code or database. Kinds that need more than that are listed in
:data:`NOT_RUN` and reported as not run.

The command learns the proxy address and the values it should save from
``OPENADAPT_AUDIT_URL``, ``OPENADAPT_AUDIT_VALUES`` (JSON) and
``OPENADAPT_AUDIT_RUN``. It is never told which fault a run carries.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

FORMAT = "openadapt.verify.audit/v1"

#: Fault kinds, applied to the first write a run sends through the proxy.
FAULTS: dict[str, str] = {
    "no_persist": "the write never reaches the app; the proxy answers with the "
    "app's last real success reply",
    "rejected": "the write never reaches the app; the proxy answers 409",
    "partial": "one declared value is blanked before the write is forwarded",
    "wrong_value": "one declared value is changed before the write is forwarded",
    "duplicate": "the write is forwarded twice; the caller sees the first reply",
    "extra_write": "the write is forwarded, then a copy with one value changed",
    "lost_response": "the write is forwarded and lands, then the connection is "
    "closed without a reply",
    "wrong_record": "one field naming the target record is rewritten "
    "(needs --wrong-record FIELD=VALUE)",
}

#: Runs by default: every kind that needs nothing beyond HTTP.
DEFAULT_FAULTS: tuple[str, ...] = tuple(k for k in FAULTS if k != "wrong_record")

#: Kinds from the fault set that HTTP alone can't produce.
NOT_RUN: dict[str, str] = {
    "stale": "overwrites another user's concurrent change; needs the app's "
    "concurrency, not one request",
    "collateral_unaudited": "a change to a table the read path doesn't cover; "
    "needs database access",
}

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


# -- body edits ----------------------------------------------------------------


def _parse(body: bytes, content_type: str) -> tuple[str, Any]:
    if not body:
        return "", None
    kind = content_type.split(";")[0].strip().lower()
    if kind == "application/x-www-form-urlencoded":
        return "form", parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    try:
        return "json", json.loads(body)
    except ValueError:
        return "", None


def _dump(kind: str, value: Any) -> bytes:
    if kind == "form":
        return urlencode(value).encode()
    return json.dumps(value).encode()


def _changed(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    text = str(value)
    return str(int(text) + 1) if text.isdigit() else text + "-changed"


def _edit(
    kind: str, value: Any, declared: Sequence[str], edit: Any
) -> tuple[Any, bool]:
    """Apply ``edit`` to the first declared value found in the body."""

    if kind == "form":
        pairs = list(value)
        for want in declared:
            for i, (name, got) in enumerate(pairs):
                if got == want:
                    pairs[i] = (name, edit(got))
                    return pairs, True
        return pairs, False

    def walk(node: Any, want: str) -> tuple[Any, bool]:
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(child, (dict, list)) and str(child) == want:
                    return {**node, key: edit(child)}, True
                new, done = walk(child, want)
                if done:
                    return {**node, key: new}, True
        elif isinstance(node, list):
            for i, child in enumerate(node):
                new, done = walk(child, want)
                if done:
                    return node[:i] + [new] + node[i + 1 :], True
        return node, False

    for want in declared:
        new, done = walk(value, want)
        if done:
            return new, True
    return value, False


def _set_field(kind: str, value: Any, name: str, new: str) -> tuple[Any, bool]:
    if kind == "form":
        pairs = list(value)
        hit = [i for i, (k, _) in enumerate(pairs) if k == name]
        for i in hit:
            pairs[i] = (name, new)
        return pairs, bool(hit)
    if isinstance(value, dict) and name in value:
        old = value[name]
        return {**value, name: type(old)(new) if isinstance(old, int) else new}, True
    return value, False


# -- the proxy -----------------------------------------------------------------


@dataclass
class Injection:
    """What the proxy did to one run's write."""

    kind: str
    applied: bool = False
    request: str = ""
    detail: str = ""
    writes_seen: int = 0


@dataclass
class _Reply:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


class FaultProxy:
    """A loopback HTTP proxy that changes one write per armed run."""

    def __init__(
        self,
        upstream: str,
        listen: tuple[str, int] = ("127.0.0.1", 0),
        *,
        write_methods: Sequence[str] = WRITE_METHODS,
        write_path: Optional[str] = None,
        wrong_record: Optional[tuple[str, str]] = None,
        timeout_s: float = 60.0,
    ) -> None:
        split = urlsplit(upstream)
        if split.scheme not in ("http", "https") or not split.hostname:
            raise ValueError(f"--upstream must be an http(s) URL, got {upstream!r}")
        self.upstream = split
        self.host: str = split.hostname
        self.prefix = split.path.rstrip("/")
        self.write_methods = tuple(m.upper() for m in write_methods)
        self.write_path = re.compile(write_path) if write_path else None
        self.wrong_record = wrong_record
        self.timeout_s = timeout_s
        self.lock = threading.Lock()
        self.injection: Optional[Injection] = None
        self.declared: list[str] = []
        self.last_success: dict[tuple[str, str], _Reply] = {}
        self.httpd = ThreadingHTTPServer(listen, self._handler())
        self.httpd.daemon_threads = True
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host!s}:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def arm(self, kind: Optional[str], values: Mapping[str, Any]) -> None:
        with self.lock:
            self.injection = Injection(kind or "clean")
            self.declared = [str(v) for v in values.values() if str(v)]

    def disarm(self) -> Injection:
        with self.lock:
            done, self.injection = self.injection, None
        return done or Injection("clean")

    # -- forwarding

    def _is_write(self, method: str, path: str) -> bool:
        if method not in self.write_methods:
            return False
        return self.write_path is None or bool(
            self.write_path.search(urlsplit(path).path)
        )

    def _forward(
        self, method: str, path: str, headers: list[tuple[str, str]], body: bytes
    ) -> _Reply:
        cls = (
            http.client.HTTPSConnection
            if self.upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = cls(self.host, self.upstream.port, timeout=self.timeout_s)
        try:
            sent = [(k, v) for k, v in headers if k.lower() not in _HOP_BY_HOP]
            sent.append(("Host", self.upstream.netloc))
            if body or method in WRITE_METHODS:
                sent.append(("Content-Length", str(len(body))))
            conn.putrequest(
                method, self.prefix + path, skip_host=True, skip_accept_encoding=True
            )
            for key, value in sent:
                conn.putheader(key, value)
            conn.endheaders(body or None)
            resp = conn.getresponse()
            return _Reply(resp.status, resp.reason, resp.getheaders(), resp.read())
        finally:
            conn.close()

    def _handle(
        self, method: str, path: str, headers: list[tuple[str, str]], body: bytes
    ) -> Optional[_Reply]:
        """The reply to send, or None to drop the connection."""

        with self.lock:
            injection = self.injection
            fire = False
            if injection is not None and self._is_write(method, path):
                injection.writes_seen += 1
                # Only the run's first write: a retry always passes through.
                fire = injection.kind != "clean" and injection.writes_seen == 1
                if fire:
                    injection.applied = True
                    injection.request = f"{method} {urlsplit(path).path}"
            declared = list(self.declared)
        key = (method, urlsplit(path).path)
        if not fire:
            reply = self._forward(method, path, headers, body)
            if self._is_write(method, path) and 200 <= reply.status < 300:
                with self.lock:
                    self.last_success[key] = reply
            return reply
        assert injection is not None
        kind = injection.kind
        if kind == "no_persist":
            with self.lock:
                cached = self.last_success.get(key)
            if cached is not None:
                injection.detail = "answered with the last real success reply"
                return cached
            injection.detail = "no earlier success reply; answered 201 with the body"
            return _Reply(
                201,
                "Created",
                [("Content-Type", "application/json")],
                body if body.strip()[:1] in (b"{", b"[") else b"{}",
            )
        if kind == "rejected":
            return _Reply(
                409,
                "Conflict",
                [("Content-Type", "application/json")],
                b'{"error": "conflict"}',
            )
        if kind == "duplicate":
            first = self._forward(method, path, headers, body)
            self._forward(method, path, headers, body)
            return first
        if kind == "lost_response":
            self._forward(method, path, headers, body)
            return None
        content_type = dict((k.lower(), v) for k, v in headers).get("content-type", "")
        parsed_kind, parsed = _parse(body, content_type)
        if not parsed_kind:
            injection.applied = False
            injection.detail = "the write's body is not JSON or a form; sent unchanged"
            return self._forward(method, path, headers, body)
        if kind == "wrong_record":
            assert self.wrong_record is not None
            edited, done = _set_field(parsed_kind, parsed, *self.wrong_record)
            missing = f"the body has no field {self.wrong_record[0]!r}"
        else:
            edit = (lambda _v: "") if kind == "partial" else _changed
            edited, done = _edit(parsed_kind, parsed, declared, edit)
            missing = "no declared value appears in the write's body"
        if not done:
            injection.applied = False
            injection.detail = missing + "; sent unchanged"
            return self._forward(method, path, headers, body)
        new_body = _dump(parsed_kind, edited)
        if kind == "extra_write":
            first = self._forward(method, path, headers, body)
            self._forward(method, path, headers, new_body)
            return first
        return self._forward(method, path, headers, new_body)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    chunks: list[bytes] = []
                    while True:
                        size = int(self.rfile.readline().split(b";")[0], 16)
                        if size == 0:
                            self.rfile.readline()
                            return b"".join(chunks)
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _serve(self) -> None:
                body = self._body()
                try:
                    reply = proxy._handle(
                        self.command, self.path, list(self.headers.items()), body
                    )
                except OSError as exc:
                    reply = _Reply(502, "Bad Gateway", [], str(exc).encode())
                if reply is None:
                    self.close_connection = True
                    return
                self.send_response(reply.status, reply.reason)
                origin = f"{proxy.upstream.scheme}://{proxy.upstream.netloc}"
                for key, value in reply.headers:
                    if key.lower() in _HOP_BY_HOP:
                        continue
                    if key.lower() == "location" and value.startswith(origin):
                        value = proxy.base + value[len(origin) + len(proxy.prefix) :]
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(reply.body)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815
            do_HEAD = do_OPTIONS = _serve  # noqa: N815

        return Handler


# -- the audit -----------------------------------------------------------------


@dataclass
class Run:
    index: int
    fault: str
    applied: bool
    detail: str
    request: str
    command_returncode: Optional[int]
    reported_success: bool
    outcome: str
    observed: str
    reasons: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def measured(self) -> bool:
        """The fault was applied (or the run is clean) and the store was read."""

        if self.outcome == "NOT READ":
            return False
        if self.outcome == "UNCERTAIN" and self.observed == "unknown":
            return False
        return self.fault == "clean" or self.applied

    @property
    def bad(self) -> bool:
        return self.outcome != "CONFIRMED"

    @property
    def missed(self) -> bool:
        return self.measured and self.bad and self.reported_success

    @property
    def false_alarm(self) -> bool:
        return self.measured and not self.bad and not self.reported_success


@dataclass
class Report:
    command: list[str]
    upstream: str
    faults: list[str]
    runs: list[Run]
    not_run: dict[str, str]
    success_rule: str

    def summary(self) -> dict[str, dict[str, int]]:
        rows: dict[str, dict[str, int]] = {}
        for name in ["clean", *self.faults]:
            runs = [r for r in self.runs if r.fault == name]
            measured = [r for r in runs if r.measured]
            rows[name] = {
                "runs": len(runs),
                "measured": len(measured),
                "bad_saves": sum(r.bad for r in measured),
                "missed": sum(r.missed for r in measured),
                "caught": sum(r.bad and not r.reported_success for r in measured),
                "false_alarms": sum(r.false_alarm for r in measured),
            }
        return rows

    def totals(self) -> dict[str, int]:
        rows = self.summary().values()
        return {
            key: sum(row[key] for row in rows)
            for key in ("measured", "bad_saves", "missed", "caught", "false_alarms")
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "command": self.command,
            "upstream": self.upstream,
            "success_rule": self.success_rule,
            "faults": self.faults,
            "not_run": self.not_run,
            "summary": self.summary(),
            "totals": self.totals(),
            "runs": [
                {
                    **asdict(r),
                    "measured": r.measured,
                    "bad": r.bad,
                    "missed": r.missed,
                    "false_alarm": r.false_alarm,
                }
                for r in self.runs
            ],
        }


def schedule(faults: Sequence[str], runs: int) -> list[str]:
    """Clean first (it gives ``no_persist`` a real reply to replay), then the
    kinds in turn, a clean run starting each round."""

    order = ["clean", *faults]
    return [order[i % len(order)] for i in range(runs)]


def _values(params: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        k: v.replace("{run}", str(index)) if isinstance(v, str) else v
        for k, v in params.items()
    }


def _proxied(contract: Mapping[str, Any], proxy: FaultProxy) -> bool:
    base = str((contract.get("verifier") or {}).get("base_url") or "")
    if not base:
        return False
    split = urlsplit(base)
    host, port = proxy.httpd.server_address[:2]
    same_host = split.hostname in (host, "localhost", "127.0.0.1", "::1", "0.0.0.0")
    return same_host and split.port == port


def audit(
    contract: Mapping[str, Any],
    params: Mapping[str, Any],
    command: Sequence[str],
    *,
    upstream: str,
    listen: tuple[str, int] = ("127.0.0.1", 0),
    runs: Optional[int] = None,
    faults: Sequence[str] = DEFAULT_FAULTS,
    success_regex: Optional[str] = None,
    write_methods: Sequence[str] = WRITE_METHODS,
    write_path: Optional[str] = None,
    wrong_record: Optional[tuple[str, str]] = None,
    command_timeout_s: float = 300.0,
    progress: Any = None,
) -> Report:
    """Run ``command`` ``runs`` times through a fault proxy and judge each run."""

    from openadapt_verify import (
        Actor,
        Contract,
        WriteBlocked,
        begin_write,
        verify_write,
    )
    from openadapt_verify.quickstart import _STATE_ENV, isolated_state

    faults = list(dict.fromkeys(faults))
    for name in faults:
        if name not in FAULTS:
            raise ValueError(f"unknown fault {name!r}; choose from {', '.join(FAULTS)}")
    if "wrong_record" in faults and wrong_record is None:
        raise ValueError("wrong_record needs --wrong-record FIELD=VALUE")
    if not command:
        raise ValueError("audit needs a command after --")
    checked = Contract.from_dict(dict(contract))
    pattern = re.compile(success_regex) if success_regex else None
    total = runs if runs is not None else 2 * (len(faults) + 1)
    if total < 1:
        raise ValueError("--runs must be at least 1")
    proxy = FaultProxy(
        upstream,
        listen,
        write_methods=write_methods,
        write_path=write_path,
        wrong_record=wrong_record,
    )
    if _proxied(contract, proxy):
        proxy.close()
        raise ValueError(
            "the contract reads through the audit proxy; point its read path at "
            "the app or its database directly"
        )
    child_env = {k: v for k, v in os.environ.items() if k not in _STATE_ENV}
    child_env["OPENADAPT_AUDIT_URL"] = proxy.base
    results: list[Run] = []
    try:
        for index, fault in enumerate(schedule(faults, total)):
            values = _values(params, index)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="openadapt-verify-audit-") as tmp:
                with isolated_state(Path(tmp) / "state"):
                    try:
                        pending = begin_write(
                            checked,
                            values,
                            actor=Actor("agent", "audited-command"),
                            workdir=Path(tmp) / "writes",
                        )
                    except WriteBlocked as exc:
                        results.append(
                            Run(
                                index,
                                fault,
                                False,
                                "",
                                "",
                                None,
                                False,
                                "NOT READ",
                                "unknown",
                                [str(exc)],
                            )
                        )
                        continue
                    proxy.arm(None if fault == "clean" else fault, values)
                    env = {
                        **child_env,
                        "OPENADAPT_AUDIT_RUN": str(index),
                        "OPENADAPT_AUDIT_VALUES": json.dumps(values),
                    }
                    returncode: Optional[int]
                    try:
                        done = subprocess.run(
                            list(command),
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=command_timeout_s,
                            check=False,
                        )
                        returncode, output = done.returncode, done.stdout + done.stderr
                    except subprocess.TimeoutExpired as exc:
                        returncode, output = None, str(exc.output or "")
                    injection = proxy.disarm()
                    result = verify_write(pending, write_attempted=True, seal=False)
            if pattern is not None:
                success = returncode is not None and bool(pattern.search(output))
            else:
                success = returncode == 0
            detail = injection.detail
            if fault != "clean" and not injection.applied and not detail:
                detail = "the command sent no write through the proxy"
            run = Run(
                index=index,
                fault=fault,
                applied=injection.applied,
                detail=detail,
                request=injection.request,
                command_returncode=returncode,
                reported_success=success,
                outcome=result.outcome.value,
                observed=result.observed,
                reasons=list(result.reasons),
                seconds=round(time.monotonic() - started, 3),
            )
            results.append(run)
            if progress is not None:
                progress(run)
    finally:
        proxy.close()
    return Report(
        command=list(command),
        upstream=upstream,
        faults=faults,
        runs=results,
        not_run={
            **NOT_RUN,
            **(
                {}
                if "wrong_record" in faults
                else {
                    "wrong_record": "needs --wrong-record FIELD=VALUE naming another "
                    "record"
                }
            ),
        },
        success_rule=f"output matches {success_regex!r}"
        if success_regex
        else "exit code 0",
    )


def render(report: Report) -> str:
    lines = [
        f"{'fault':<15}{'runs':>5}{'bad saves':>11}{'check said OK':>15}"
        f"{'caught':>8}{'false alarms':>14}"
    ]
    for name, row in report.summary().items():
        runs = str(row["measured"])
        if row["measured"] != row["runs"]:
            runs += f"/{row['runs']}"
        lines.append(
            f"{name:<15}{runs:>5}{row['bad_saves']:>11}{row['missed']:>15}"
            f"{row['caught']:>8}{row['false_alarms']:>14}"
        )
    total = report.totals()
    clean = report.summary()["clean"]
    lines += [
        "",
        f"Your check ({report.success_rule}) reported success on "
        f"{total['missed']} of {total['bad_saves']} bad saves.",
        f"False alarms: {clean['false_alarms']} of {clean['measured']} clean runs, "
        f"{total['false_alarms'] - clean['false_alarms']} on faulted runs whose "
        "write still landed correctly.",
    ]
    skipped = [r for r in report.runs if not r.measured]
    if skipped:
        lines.append(
            f"{len(skipped)} run(s) not measured (fault not applied or store "
            "unreadable); see the JSON report."
        )
    lines.append("Not run: " + ", ".join(sorted(report.not_run)) + ".")
    return "\n".join(lines)


def exit_code(report: Report) -> int:
    """0 when every bad save was caught with no false alarms, else 1."""

    total = report.totals()
    if total["measured"] == 0:
        return 2
    return 0 if total["missed"] == 0 and total["false_alarms"] == 0 else 1


def print_progress(run: Run) -> None:
    said = "success" if run.reported_success else "failure"
    note = "" if run.measured else "  (not measured)"
    print(
        f"run {run.index + 1}: {run.fault}: command said {said}, store says "
        f"{run.outcome}{note}",
        file=sys.stderr,
    )
