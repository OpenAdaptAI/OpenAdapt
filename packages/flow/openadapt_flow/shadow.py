"""Shadow mode: check an agent's writes against the system of record, never
blocking or changing one. See ``docs/SHADOW_MODE.md``.

A pass-through HTTP proxy sits between the agent and its server. A write is
held only while the store is read before it (at most ``hold_ms``), then sent
on byte for byte. Writes to one record form an episode, checked once the
agent goes quiet by the verifier that already exists for the read path:
``ensure_record.verify_ensure`` (FHIR) or ``verifier.verify_write`` (SQL,
with the whole-database frame). Anything that can't be read is UNCERTAIN.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, quote, urlsplit

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
CONFIRMED, REFUTED, UNCERTAIN = "CONFIRMED", "REFUTED", "UNCERTAIN"
#: Characters a request target keeps as they are (RFC 3986 reserved,
#: unreserved and "%").
_SAFE_URL = "/?#[]@!$&'()*+,;=:%-._~"


class ShadowConfigError(ValueError):
    """The shadow's configuration can't be used."""


@dataclass
class Route:
    """SQL mode: which writes a ``verify.yaml`` contract checks."""

    write: re.Pattern[str]  # matched against "METHOD /path"
    contract: Any  # verifier.Contract
    #: parameter -> dotted path in the request body, or ``{"literal": v}``
    #: for a value the operator pins (one the body doesn't carry).
    params: dict[str, Any]
    name: str


@dataclass
class Write:
    seq: int
    method: str
    path: str
    status: int = 0
    started: float = 0.0
    held_ms: float = 0.0
    proxy_ms: float = 0.0
    upstream_ms: float = 0.0
    error: str = ""


@dataclass
class Episode:
    id: int
    kind: str  # "fhir", "sql" or "none"
    name: str  # resource type or route name
    contract: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    writes: list[Write] = field(default_factory=list)
    refs: set[str] = field(default_factory=set)
    baseline: Optional[Future] = None
    baseline_note: str = ""
    in_flight: int = 0
    last_end: float = 0.0
    checked: bool = False
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: float = 0.0
    #: The verifier's first result and baseline, kept for the recheck.
    result: Any = None
    base: Any = None
    #: When a CONFIRMED is checked again (epoch seconds; 0: never).
    recheck_at: float = 0.0
    recheck_from: Any = None  # fhir: the server time the recheck reads from
    late_verdict: str = ""
    late_reasons: list[str] = field(default_factory=list)
    #: The recheck read the record itself and found it changed or gone (a
    #: finding no later write of the agent's can explain away).
    late_moved: bool = False
    #: The recheck's read time (sql), for the recheck's write receipt.
    late_read_at: str = ""
    #: Signed write receipts (``--receipts``): paths, newest last, and why
    #: one wasn't issued. A receipt never changes a verdict.
    receipts: list[str] = field(default_factory=list)
    receipt_sha256: str = ""
    receipt_note: str = ""


def _body_doc(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    if "x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        return json.loads(body)
    except ValueError:
        return None


_MISSING = object()


def _dotted(doc: Any, path: str) -> Any:
    """The value at a dotted path, or ``_MISSING`` (a null is a value)."""

    node = doc
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Shadow:
    """One shadow session: the proxy, the episodes and the checks."""

    def __init__(
        self,
        *,
        upstream: str,
        mode: str,
        contracts: Optional[list[Any]] = None,
        routes: Optional[list[Route]] = None,
        actor_id: str = "agent",
        out: Path,
        listen: tuple[str, int] = ("127.0.0.1", 0),
        hold_ms: float = 2000.0,
        upstream_timeout_s: float = 300.0,
        recheck_s: Optional[float] = 300.0,
        quiet_s: float = 5.0,
        receipts: bool = False,
    ) -> None:
        if mode not in ("fhir", "sql"):
            raise ShadowConfigError(f"mode {mode!r} is not fhir or sql")
        if listen[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ShadowConfigError("the shadow listens on the loopback interface only")
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ShadowConfigError(f"upstream {upstream!r} is not an http(s) URL")
        self.mode = mode
        self.upstream = parts
        self.contracts: dict[str, Any] = {}
        self._ensure_sources: dict[str, Any] = {}
        for source in contracts or []:
            binding = getattr(getattr(source, "config", None), "ensure_record", None)
            ensure = binding.contract if binding is not None else source
            self.contracts[ensure.resource_type] = ensure
            if binding is not None:
                self._ensure_sources[ensure.resource_type] = source
        self.routes = list(routes or [])
        if mode == "fhir" and not self.contracts:
            raise ShadowConfigError(
                "fhir mode needs at least one ensure-record contract"
            )
        if mode == "sql" and not self.routes:
            raise ShadowConfigError("sql mode needs at least one route")
        self.actor_id = actor_id
        self.out = Path(out)
        self.hold_s = max(0.0, hold_ms) / 1000.0
        self.quiet_s = quiet_s
        self.upstream_timeout_s = upstream_timeout_s
        #: fhir: check each CONFIRMED again this long after (None: never).
        #: sql: the contract's ``frame.database.recheck_after_s`` decides.
        self.recheck_s = recheck_s
        #: Called as ``on_checked(episode, result)`` after each verifier call,
        #: with the verifier's own result (``EnsureResult`` or ``WriteResult``),
        #: for a step that learns from checked traffic. Its errors go in
        #: ``errors``; it never changes a verdict.
        self.on_checked: Optional[Callable[[Episode, Any], None]] = None
        #: Sign each verdict as a write receipt under ``out/receipts``.
        self.receipts = receipts
        self.session = secrets.token_hex(6)
        self.started = time.time()
        self.episodes: list[Episode] = []
        self.writes: list[Write] = []
        self.errors: list[str] = []
        self._by_key: dict[Any, Episode] = {}
        self._by_ref: dict[str, Episode] = {}
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._seq = 0
        self._last_write = 0.0
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="shadow-read")
        self._stop = threading.Event()
        self.server = _QuietServer(listen, self._handler())
        self.server.daemon_threads = True
        self._threads = [
            threading.Thread(target=self.server.serve_forever, daemon=True),
            threading.Thread(target=self._checker, daemon=True),
        ]

    # -- lifecycle -------------------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else host
        return f"http://{name}:{port}"

    def start(self) -> "Shadow":
        for t in self._threads:
            t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.flush()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def idle_s(self) -> float:
        with self._lock:
            return time.time() - (self._last_write or self.started)

    def flush(self, wait_s: float = 120.0) -> None:
        """Check every episode now, once its writes have their replies."""

        deadline = time.time() + wait_s
        while time.time() < deadline:
            with self._lock:
                busy = any(e.in_flight for e in self.episodes if not e.checked)
            if not busy:
                break
            time.sleep(0.05)
        self._check_due()

    # -- the proxy -------------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        shadow = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:
                pass

            def _do(self) -> None:
                shadow._handle(self)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _do
            do_OPTIONS = _do

        return Handler

    def _read_body(self, h: BaseHTTPRequestHandler) -> bytes:
        if "chunked" in (h.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            while True:
                size = int(h.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while h.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(h.rfile.read(size))
                h.rfile.readline()
        length = int(h.headers.get("Content-Length") or 0)
        return h.rfile.read(length) if length else b""

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        t0 = time.perf_counter()
        method = h.command
        write: Optional[Write] = None
        episode: Optional[Episode] = None
        try:
            body = self._read_body(h)
        except Exception as exc:  # noqa: BLE001 - a body we can't read can't be sent
            self._note_error(f"unreadable request body for {method}: {exc!r}")
            h.close_connection = True
            return
        if method in WRITE_METHODS:
            try:
                write, episode = self._observe(method, h.path, h.headers, body)
            except Exception as exc:  # noqa: BLE001 - the write goes on regardless
                self._note_error(f"observe {method}: {exc!r}")
        upstream_s = 0.0
        reply: Optional[tuple[int, str, list[tuple[str, str]], bytes]] = None
        failure = ""
        try:
            held = time.perf_counter()
            if episode is not None and episode.baseline is not None:
                self._wait_baseline(episode)
            if write is not None:
                write.held_ms = (time.perf_counter() - held) * 1000
            t_up = time.perf_counter()
            try:
                reply = self._forward(method, h.path, h.headers, body)
            except Exception as exc:  # noqa: BLE001 - mirrored as a dropped connection
                failure = f"upstream failed: {type(exc).__name__}"
                self._note_error(f"{method} {h.path[:200]}: {failure}")
            upstream_s = time.perf_counter() - t_up
            if reply is None:
                # The agent sees what it would see without the shadow: the
                # connection ends with no reply. Never a made-up status.
                h.close_connection = True
            else:
                status, reason, headers, rbody = reply
                try:
                    h.send_response_only(status, reason or None)
                    for key, value in headers:
                        h.send_header(key, value)
                    h.end_headers()
                    if method != "HEAD":
                        h.wfile.write(rbody)
                except OSError:
                    pass  # the agent hung up; the write already went on
        finally:
            if write is not None and episode is not None:
                write.upstream_ms = upstream_s * 1000
                write.proxy_ms = (
                    time.perf_counter() - t0 - upstream_s
                ) * 1000 - write.held_ms
                write.status = reply[0] if reply else 0
                write.error = failure
                try:
                    self._answered(episode, write, reply)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"answered {method}: {exc!r}")
                    with self._lock:
                        episode.in_flight = max(0, episode.in_flight - 1)

    def _forward(
        self, method: str, path: str, headers: Any, body: bytes
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        """Send the request on unchanged and return the reply unchanged.

        Hop-by-hop headers aside, every header goes on in order, repeats
        included. A chunked body goes on with its length instead. Raises on
        a transport failure.
        """

        up = self.upstream
        cls = (
            http.client.HTTPSConnection
            if up.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = cls(up.hostname or "", up.port, timeout=self.upstream_timeout_s)
        target = (up.path.rstrip("/") + path) if up.path not in ("", "/") else path
        # http.server decodes the request line as Latin-1; send the same bytes,
        # with any byte outside ASCII percent-encoded (the same URL).
        target = quote(target.encode("latin-1"), safe=_SAFE_URL)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", up.netloc)
            for key, value in headers.items():
                if key.lower() not in HOP_BY_HOP:
                    conn.putheader(key, value)
            if body or method in WRITE_METHODS or "Content-Length" in headers:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            resp = conn.getresponse()
            data = resp.read()
            kept = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
            if resp.status not in (204, 304) and not 100 <= resp.status < 200:
                length = resp.getheader("Content-Length")
                kept.append(
                    (
                        "Content-Length",
                        length if method == "HEAD" and length else str(len(data)),
                    )
                )
            return resp.status, resp.reason, kept, data
        finally:
            conn.close()

    # -- observing a write -------------------------------------------------------------

    def _observe(
        self, method: str, path: str, headers: Any, body: bytes
    ) -> tuple[Write, Episode]:
        doc = _body_doc(body, headers.get("Content-Type") or "")
        with self._lock:
            self._seq += 1
            write = Write(self._seq, method, path, started=time.time())
            self.writes.append(write)
            self._last_write = write.started
            episode = (
                self._fhir_episode(method, path, doc)
                if self.mode == "fhir"
                else self._sql_episode(method, path, doc)
            )
            episode.writes.append(write)
            episode.in_flight += 1
        return write, episode

    def _new(self, kind: str, name: str, **kw: Any) -> Episode:
        ep = Episode(id=len(self.episodes) + 1, kind=kind, name=name, **kw)
        self.episodes.append(ep)
        return ep

    def _unchecked(self, name: str, why: str) -> Episode:
        ep = self._new("none", name)
        ep.baseline_note = why
        return ep

    def _fhir_episode(self, method: str, path: str, doc: Any) -> Episode:
        from openadapt_flow.ensure_record import extract, normalize

        segs = [s for s in urlsplit(path).path.split("/") if s]
        if any(s == "_search" or s.startswith("$") for s in segs):
            return self._unchecked(path, "an operation or a search sent as a write")
        rtype, rid = "", ""
        if segs and segs[-1] in self.contracts:
            rtype = segs[-1]
        elif len(segs) >= 2 and segs[-2] in self.contracts:
            rtype, rid = segs[-2], segs[-1]
        if not rtype:
            name = segs[-1] if segs else "/"
            if len(segs) >= 2 and segs[-2][:1].isupper():
                name = segs[-2]
            return self._unchecked(name, f"no contract for {name}")
        contract = self.contracts[rtype]
        ref = f"{rtype}/{rid}" if rid else ""
        params: dict[str, Any] = {}
        if isinstance(doc, dict) and doc.get("resourceType") == rtype:
            for name, rule in contract.fields.items():
                value = extract(doc, rule.path)
                if value is not None and not isinstance(value, (dict, list)):
                    params[name] = value
        done = self._by_ref.get(ref) if ref else None
        if done is not None and done.checked:
            ep = self._unchecked(
                rtype, f"a later write to a record already checked (check {done.id})"
            )
        elif done is not None:
            ep = done
        else:
            key = None
            for rule in contract.identity:
                if all(p in params for p in rule):
                    key = (
                        rtype,
                        tuple(
                            (p, normalize(params[p], contract.fields[p], task=True))
                            for p in rule
                        ),
                    )
                    break
            found = self._by_key.get(key) if key else None
            if found is not None and not found.checked:
                ep = found  # (a checked one: a new write starts a new episode)
            else:
                if ref or method != "POST":
                    ep = self._unchecked(
                        rtype,
                        f"{method} of a record this session didn't create: an "
                        "ensure-record contract checks creates",
                    )
                elif key is None:
                    ep = self._unchecked(rtype, "the write gives no identity values")
                else:
                    ep = self._new("fhir", rtype, contract=contract, params=params)
                    ep.baseline = self._pool.submit(
                        self._fhir_baseline, contract, params
                    )
                    self._by_key[key] = ep
        if ep.kind == "fhir" and params:
            ep.params = params  # the agent's latest values (its correction)
        if ref:
            ep.refs.add(ref)
            self._by_ref[ref] = ep
        return ep

    def _fhir_baseline(self, contract: Any, params: dict[str, Any]) -> Any:
        from openadapt_flow.ensure_record import capture_baseline, Unreadable
        from openadapt_flow.verifier import Actor

        source = self._ensure_sources.get(contract.resource_type)
        targeted = None
        if source is not None:
            if source.params != sorted(params):
                raise Unreadable("ensure_baseline_parameter_names_changed")
            targeted = getattr(source.config.frame, "targeted", None)
        return capture_baseline(contract, params, actor=Actor("agent", self.actor_id),
                                workdir=self.out / "writes", targeted=targeted)

    def _sql_episode(self, method: str, path: str, doc: Any) -> Episode:
        line = f"{method} {urlsplit(path).path}"
        route = next((r for r in self.routes if r.write.fullmatch(line)), None)
        if route is None:
            return self._unchecked(line, "no route matches this write")
        params = {
            p: where["literal"] if isinstance(where, dict) else _dotted(doc, where)
            for p, where in route.params.items()
        }
        missing = sorted(p for p, v in params.items() if v is _MISSING)
        if missing:
            return self._unchecked(
                route.name,
                "the write doesn't give "
                + ", ".join(f"{p} (at {route.params[p]})" for p in missing)
                + ": the route doesn't fit this write",
            )
        key = (route.name, json.dumps(params, sort_keys=True, default=str))
        ep = self._by_key.get(key)
        if ep is None or ep.checked:
            ep = self._new("sql", route.name, contract=route.contract, params=params)
            ep.baseline = self._pool.submit(self._sql_baseline, route.contract, params)
            self._by_key[key] = ep
        return ep

    def _sql_baseline(self, contract: Any, params: dict[str, Any]) -> Any:
        from openadapt_flow.verifier import Actor, begin_write

        work = self.out / "writes"
        work.mkdir(parents=True, exist_ok=True)
        # No ledger: the shadow must never hold or refuse the agent's writes.
        return begin_write(
            contract,
            params,
            actor=Actor("agent", self.actor_id),
            ledger=None,
            workdir=work,
        )

    def _wait_baseline(self, ep: Episode) -> None:
        fut = ep.baseline
        if fut is None or fut.done() or ep.baseline_note:
            return
        try:
            fut.result(timeout=self.hold_s)
        except Exception:  # noqa: BLE001 - a timeout or a read failure
            pass
        with self._lock:
            if not fut.done() and not ep.baseline_note:
                # It may now finish after the write landed: it is no baseline.
                ep.baseline_note = (
                    f"the store wasn't read within {self.hold_s * 1000:.0f} ms, "
                    "so the write went on without a baseline"
                )

    def _answered(self, ep: Episode, write: Write, reply: Optional[tuple]) -> None:
        with self._lock:
            ep.in_flight -= 1
            ep.last_end = time.time()
            self._last_write = max(self._last_write, ep.last_end)
            if self.mode != "fhir" or reply is None or write.status // 100 != 2:
                return
            _, _, headers, rbody = reply
            refs = []
            try:
                doc = json.loads(rbody) if rbody else None
            except ValueError:
                doc = None
            if isinstance(doc, dict) and doc.get("resourceType") and doc.get("id"):
                refs.append(f"{doc['resourceType']}/{doc['id']}")
            for k, v in headers:
                if k.lower() == "location":
                    parts = urlsplit(v).path.split("/")
                    if "_history" in parts:
                        parts = parts[: parts.index("_history")]
                    if len(parts) >= 2:
                        refs.append("/".join(parts[-2:]))
            for ref in refs:
                ep.refs.add(ref)
                self._by_ref.setdefault(ref, ep)

    # -- checking ----------------------------------------------------------------------

    def _checker(self) -> None:
        while not self._stop.wait(0.25):
            if self.idle_s() >= self.quiet_s:
                try:
                    self._check_due()
                    self.run_rechecks()
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"checker: {exc!r}")

    def _check_due(self) -> None:
        with self._check_lock:
            with self._lock:
                if any(e.in_flight for e in self.episodes):
                    return  # a write still in flight would land in every window
                due = [e for e in self.episodes if not e.checked]
                seq = self._seq
            for ep in due:
                try:
                    self._check(ep)
                except Exception as exc:  # noqa: BLE001 - fail closed on the verdict
                    ep.verdict, ep.reasons = UNCERTAIN, [f"the check failed: {exc!r}"]
                    self._note_error(traceback.format_exc(limit=3))
                with self._lock:
                    if self._seq != seq and ep.verdict != UNCERTAIN:
                        ep.reasons.insert(
                            0,
                            f"the agent wrote while this was checked; the check "
                            f"alone said {ep.verdict}",
                        )
                        ep.verdict = UNCERTAIN
                    ep.checked = True
                    ep.checked_at = time.time()
                    self._schedule_recheck(ep)
                self._receipt(ep)

    def _canonical_result(self, ep: Episode) -> Any:
        if ep.kind == "fhir":
            from openadapt_flow.ensure_bridge import canonical_result

            return canonical_result(ep.base) if ep.base is not None else None
        return ep.result

    def _schedule_recheck(self, ep: Episode) -> None:
        """Retain every nonfinal obligation, including non-executable UNKNOWN."""
        from datetime import datetime

        result = self._canonical_result(ep)
        if ep.verdict == REFUTED or (result is not None and getattr(result, "final", False)):
            return
        state = getattr(result, "recheck", None)
        due = getattr(state, "due_at", "") or getattr(result, "recheck_due_at", None)
        obligation = {"status": "owed", "purpose": "read_only_reconciliation",
                      "reason": "independent_target_identity_unknown" if result is None
                      else "recheck_proof_owed", "executable": bool(state and due)}
        ep.evidence["reconciliation"] = obligation
        if due:
            try:
                ep.recheck_at = datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp()
                return
            except ValueError:
                obligation.update(reason="recheck_due_time_unreadable", executable=False)
        # Unknown/no-state is still an owed obligation. A bounded scheduled
        # attempt records its refusal; it never guesses a target or polls a row.
        if self.recheck_s is not None:
            ep.recheck_at = ep.checked_at + self.recheck_s
        else:
            obligation["reason"] = "reconciliation_schedule_unconfigured"

    def pending_rechecks(self) -> int:
        """Checks or rechecks still to run: an unchecked episode may
        schedule a recheck once it is checked."""

        with self._lock:
            return sum(
                1
                for e in self.episodes
                if not e.checked or (e.recheck_at and not e.late_verdict)
                or (e.evidence.get("reconciliation") or {}).get("status") == "owed"
            )

    def run_rechecks(self, *, now: bool = False, only: Optional[list] = None) -> None:
        """Attempt each owed nonfinal reconciliation once it is due. ``now`` runs whatever
        is scheduled without waiting for ``recheck_s`` (fhir), as a test
        harness does before it resets the store; a SQL recheck still waits
        for the contract's due time. The first verdict stays as it was."""

        with self._check_lock:
            with self._lock:
                if any(e.in_flight for e in self.episodes):
                    return
                due = [
                    e
                    for e in (only if only is not None else self.episodes)
                    if e.recheck_at
                    and not e.late_verdict
                    and (now or time.time() >= e.recheck_at)
                ]
                seq = self._seq
            for ep in due:
                try:
                    self._recheck(ep)
                except Exception as exc:  # noqa: BLE001 - fail closed
                    ep.late_verdict = UNCERTAIN
                    ep.late_reasons = [f"the recheck failed: {exc!r}"]
                    self._note_error(traceback.format_exc(limit=3))
                with self._lock:
                    if ep.late_verdict == REFUTED and not ep.late_moved:
                        unknown = self._unexplained_later(ep)
                        if unknown:
                            ep.late_verdict = UNCERTAIN
                            ep.late_reasons.insert(
                                0,
                                f"the agent made {unknown} later write(s) through "
                                "the shadow that this recheck can't tell apart "
                                "from a change to this record",
                            )
                    if self._seq != seq and ep.late_verdict != UNCERTAIN:
                        ep.late_reasons.insert(
                            0,
                            "the agent wrote while this was rechecked; the "
                            f"recheck alone said {ep.late_verdict}",
                        )
                        ep.late_verdict = UNCERTAIN
                    ep.evidence["recheck"] = {
                        "verdict": ep.late_verdict,
                        "reasons": ep.late_reasons,
                        "at": time.time(),
                    }
                self._receipt(ep, late=True)

    def _later(self, ep: Episode) -> list[Episode]:
        return [
            o
            for o in self.episodes
            if o is not ep and o.writes and o.writes[0].started > ep.checked_at
        ]

    def _unexplained_later(self, ep: Episode) -> int:
        """Later writes through the shadow a recheck can't excuse: in fhir,
        a write whose record isn't known (a lost reply or a 2xx write that
        names no record); in sql, a write under no contract or another one,
        or with no result."""

        n = 0
        for o in self._later(ep):
            if ep.kind == "fhir":
                # Per write: a create whose reply didn't come back may have
                # made a record no one knows, even in an episode that has one.
                # A 2xx reply with no known record (minimal return without
                # Location, transaction bundle, conditional write) is also
                # unexplained.
                n += sum(
                    1
                    for w in o.writes
                    if (w.status // 100 != 2 and (w.method == "POST" or not o.refs))
                    or (w.status // 100 == 2 and not o.refs)
                )
            else:
                n += not (o.kind == "sql" and o.contract is ep.contract and o.result)
        return n

    def _recheck(self, ep: Episode) -> None:
        from openadapt_flow.verifier import Actor, recheck

        result = self._canonical_result(ep)
        if ep.verdict == REFUTED:
            ep.late_verdict, ep.late_reasons = REFUTED, ["initial_refutation_is_terminal"]
            return
        if result is None or getattr(result, "recheck", None) is None:
            ep.late_verdict = UNCERTAIN
            ep.late_reasons = ["independent_target_identity_unknown"]
            return
        contract = ep.contract
        if ep.kind == "fhir":
            from openadapt_flow.ensure_bridge import canonical_contract

            contract = canonical_contract(ep.base)
        with self._lock:
            later = [self._canonical_result(o) for o in self._later(ep)
                     if o.kind == ep.kind and o.contract is ep.contract
                     and self._canonical_result(o) is not None]
        late = recheck(contract, result, params=ep.params,
                       actor=Actor("agent", self.actor_id), excused_by=later,
                       wait=True, ledger=None)
        ep.late_verdict = str(late.outcome)
        ep.late_read_at = late.read_at
        ep.late_reasons = [r for r in late.reasons if "ledger" not in r]
        hard = late.evidence.get("hard_invariants")
        if isinstance(hard, dict):
            ep.evidence["late_hard_invariants"] = hard
        if ep.verdict == UNCERTAIN and ep.late_verdict == CONFIRMED:
            ep.late_verdict = UNCERTAIN
            ep.late_reasons.append("initial_proof_unknown_requires_review")
        if ep.late_verdict != UNCERTAIN:
            (ep.evidence.get("reconciliation") or {}).update(status="complete")

    def _recheck_fhir(self, ep: Episode) -> None:
        """Compatibility entry point; the canonical fence owns every read."""
        self._recheck(ep)

    def _check(self, ep: Episode) -> None:
        statuses = [w.status for w in ep.writes]
        ep.evidence["replies"] = statuses
        if ep.kind == "none":
            ep.verdict, ep.reasons = UNCERTAIN, [ep.baseline_note or "not checked"]
            return
        if ep.baseline_note:
            ep.verdict, ep.reasons = UNCERTAIN, [ep.baseline_note]
            return
        assert ep.baseline is not None
        try:
            base = ep.baseline.result(timeout=0)
        except Exception as exc:  # noqa: BLE001
            ep.verdict = UNCERTAIN
            ep.reasons = [f"the store couldn't be read before the write: {exc}"]
            return
        if ep.kind == "fhir":
            self._check_fhir(ep, base, statuses)
        else:
            self._check_sql(ep, base)

    def _check_fhir(self, ep: Episode, base: Any, statuses: list[int]) -> None:
        from openadapt_flow.ensure_record import extra_elements, extract, same, verify_ensure
        from openadapt_flow.ensure_bridge import canonical_observation, canonical_result

        contract = ep.contract
        # Response/request IDs remain grouping/report hints only. They cannot
        # authorize excluding another episode's writes from this judgment.
        result = verify_ensure(contract, ep.params, base)
        ep.result, ep.base = result, base
        self._notify(ep, result)
        canonical = canonical_result(base)
        ep.verdict = canonical.outcome.value if canonical is not None else UNCERTAIN
        ep.reasons = list(result.reasons)
        ep.evidence["verifier"] = {"outcome": result.outcome, **result.evidence}
        if canonical is not None:
            ep.evidence["frame"] = dict(canonical.evidence.get("frame") or {})
            ep.evidence["readback"] = dict(canonical.evidence.get("readback") or {})
        # Reporting reuses the independent collector; no second read path.
        ep.evidence["before"] = sorted(f"{contract.resource_type}/{i}" for i in base.matches)
        observation = canonical_observation(base)
        stored = {} if observation is None else {
            f"{contract.resource_type}/{i}": dict(resource)
            for i, resource in observation.after_matches.items()
        }
        if observation is not None and observation.created is not None:
            resource = observation.created
            stored[f"{contract.resource_type}/{resource.get('id')}"] = dict(resource)
        ep.evidence["after"] = stored
        fields = []
        for ref, res in stored.items():
            if not isinstance(res, dict) or "unreadable" in res:
                continue
            for name, rule in contract.fields.items():
                got = extract(res, rule.path)
                sent = ep.params.get(name)
                if sent is None and got is None:
                    continue
                fields.append(
                    {
                        "record": ref,
                        "field": name,
                        "sent": sent,
                        "stored": got,
                        "same": name in ep.params
                        and same(contract, name, got, ep.params[name]),
                    }
                )
            if contract.closed:
                extra = extra_elements(contract, res, ep.params)
                if extra:
                    fields.append(
                        {
                            "record": ref,
                            "field": "(not sent)",
                            "sent": None,
                            "stored": ", ".join(extra),
                            "same": False,
                        }
                    )
        ep.evidence["fields"] = fields

    def _check_sql(self, ep: Episode, pending: Any) -> None:
        from openadapt_flow.verifier import verify_write

        result = verify_write(pending, seal=False)
        ep.result = result
        self._notify(ep, result)
        ep.verdict = result.outcome.value
        ep.reasons = list(result.reasons)
        if result.provisional:
            ep.reasons.append(
                f"provisional: the contract's recheck is due at {result.recheck_due_at}; "
                "shadow mode runs it then"
            )
            ep.evidence["recheck_due_at"] = result.recheck_due_at
        ep.evidence["verifier"] = {
            "checked": result.checked,
            "observed": result.observed,
            "receipt_outcome": result.receipt_outcome,
            "not_covered": list(result.scope.not_covered),
        }
        frame = (result.evidence or {}).get("frame") or {}
        if isinstance(frame, dict):
            ep.evidence["frame"] = {
                k: frame[k] for k in ("database", "violations", "moved") if k in frame
            }
        delta = (result.evidence or {}).get("delta")
        if delta is not None:
            ep.evidence["delta"] = delta

    def _receipt(self, ep: Episode, *, late: bool = False) -> None:
        """Sign the episode's verdict (``late``: its recheck's) as a write
        receipt. A failure is noted; the verdict stands either way."""

        if not self.receipts:
            return
        from openadapt_flow import write_receipt as wr

        try:
            fields = self._receipt_fields(ep, late)
            kind = "recheck" if late else "check"
            name = f"{self.session}-{ep.id:04d}-{kind}.receipt.cbor"
            issued = wr.issue(
                self.out / "receipts" / name,
                evidence={
                    "sent": ep.params,
                    "verdict": ep.verdict,
                    "reasons": ep.reasons,
                    "late": {"verdict": ep.late_verdict, "reasons": ep.late_reasons},
                    "evidence": ep.evidence,
                },
                supersedes=ep.receipt_sha256 if late and ep.receipt_sha256 else None,
                **fields,
            )
        except Exception as exc:  # noqa: BLE001 - never change a verdict
            ep.receipt_note = f"not issued: {exc}"
            self._note_error(f"receipt for check {ep.id}: {exc!r}")
            return
        ep.receipts.append(str(issued.path))
        ep.receipt_sha256 = issued.sha256
        ep.receipt_note = ""

    def _receipt_fields(self, ep: Episode, late: bool) -> dict[str, Any]:
        from datetime import datetime, timezone

        from openadapt_flow import write_receipt as wr
        from openadapt_flow.shadow_report import _final

        verdict = ep.verdict or UNCERTAIN
        due = None
        if late:
            verdict = _final(verdict, {"verdict": ep.late_verdict or UNCERTAIN})
            if verdict == CONFIRMED and ep.late_verdict != CONFIRMED:
                verdict = UNCERTAIN  # no recheck verdict: never a pass
        elif ep.late_verdict:
            # The recheck already failed to schedule: never a plain pass.
            verdict = _final(verdict, {"verdict": ep.late_verdict})
        elif verdict == CONFIRMED and ep.recheck_at:
            due = wr.iso(datetime.fromtimestamp(ep.recheck_at, timezone.utc))
        write_id = f"shadow-{self.session}-{ep.id}"
        canonical = self._canonical_result(ep)
        if canonical is not None:
            scope = wr.scope_of_result(canonical)
            write_id = canonical.write_id or write_id
            subject = {
                "contract_sha256": None,
                "verify_run_receipt_sha256": canonical.receipt_sha256 or None,
            }
            value = str((canonical.receipt or {}).get("workflow_digest") or "")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                subject["contract_sha256"] = value[7:]
            checked = dict(canonical.checked)
        elif ep.kind == "fhir" and ep.result is not None:
            ev = ep.result.evidence or {}
            started = getattr(ep.base, "started", None)
            until = ev.get("window_until")
            scope = wr.fhir_scope(
                unreadable=bool(ev.get("unreadable")),
                started=wr.iso(started),
                read_at=wr.iso(datetime.fromisoformat(until)) if until else None,
                audit=ep.contract.change_feed.audit is not None,
            )
            subject = {"contract_sha256": None, "verify_run_receipt_sha256": None}
            checked = {}
            hard = (
                ep.evidence.get("late_hard_invariants", ev.get("hard_invariants"))
                if late else ev.get("hard_invariants")
            )
            if isinstance(hard, dict):
                status = hard.get("status")
                if late and "late_hard_invariants" not in ep.evidence:
                    status = "not_checked"
                checked["frame"] = status if status in {"held", "refuted"} else "not_checked"
                # Named omissions belong in each signed receipt's visible
                # scope, in addition to its committed evidence attachment.
                omissions = hard.get("outside_guarantee")
                if isinstance(omissions, list):
                    names = {
                        row.get("name") for row in omissions
                        if isinstance(row, dict) and isinstance(row.get("name"), str)
                        and row.get("status") == "UNKNOWN"
                    }
                    for name in (
                        "outside_receipt_intermediate_history",
                        "receipt_record_intermediate_history",
                    ):
                        if name in names:
                            scope["not_covered"].append(f"{name}: UNKNOWN")
        else:
            scope = {
                "read_paths": [],
                "stores": [],
                "unread": [],
                "window": {
                    "baseline_read_at": None,
                    "read_at": None,
                    "max_execution_window_s": None,
                },
                "strength": "unchecked",
                "not_covered": [
                    "this write: the store couldn't be read before it, or no "
                    "contract covers it, so nothing about it was checked"
                ],
            }
            subject = {"contract_sha256": None, "verify_run_receipt_sha256": None}
            checked = {}
            verdict = UNCERTAIN
        outside_history = "outside_receipt_intermediate_history: UNKNOWN"
        if outside_history not in scope["not_covered"]:
            scope["not_covered"].append(outside_history)
        if late:
            # The window stays the check's; a line says what the recheck
            # read again, and until when.
            read_at = ep.late_read_at
            if verdict == CONFIRMED and not read_at:
                verdict = UNCERTAIN
            when = (
                wr.iso(datetime.fromisoformat(read_at.replace("Z", "+00:00")))
                if read_at
                else None
            )
            reread = ["frame.database"] if ep.kind == "sql" else list(scope["stores"])
            scope = wr.recheck_scope(scope, reread, when)
        return {
            "source": f"shadow.{self.mode}",
            "verdict": verdict,
            "scope": scope,
            "checked": checked,
            "write_id": write_id,
            "recheck_due_at": due,
            **subject,
        }

    def _notify(self, ep: Episode, result: Any) -> None:
        if self.on_checked is None:
            return
        try:
            self.on_checked(ep, result)
        except Exception as exc:  # noqa: BLE001 - a hook never changes a verdict
            self._note_error(f"on_checked: {exc!r}")

    def _note_error(self, text: str) -> None:
        with self._lock:
            self.errors.append(text[:2000])

    # -- results -----------------------------------------------------------------------

    def results(self) -> dict[str, Any]:
        from openadapt_flow.shadow_report import summarize

        with self._lock:
            return summarize(self)
