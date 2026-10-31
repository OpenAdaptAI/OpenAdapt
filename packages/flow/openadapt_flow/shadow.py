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
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

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


class ShadowConfigError(ValueError):
    """The shadow's configuration can't be used."""


@dataclass
class Route:
    """SQL mode: which writes a ``verify.yaml`` contract checks."""

    write: re.Pattern[str]  # matched against "METHOD /path"
    contract: Any  # verifier.Contract
    params: dict[str, str]  # parameter -> dotted path in the request body
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


def _body_doc(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    if "x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        return json.loads(body)
    except ValueError:
        return None


def _dotted(doc: Any, path: str) -> Any:
    node = doc
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


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
        quiet_s: float = 5.0,
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
        self.contracts = {c.resource_type: c for c in contracts or []}
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
        self.server = ThreadingHTTPServer(listen, self._handler())
        self.server.daemon_threads = True
        self._threads = [
            threading.Thread(target=self.server.serve_forever, daemon=True),
            threading.Thread(target=self._checker, daemon=True),
        ]

    # -- lifecycle -------------------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

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
        self._check_due(force=True)

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
            chunks = []
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
        body = self._read_body(h)
        method = h.command
        write: Optional[Write] = None
        episode: Optional[Episode] = None
        if method in WRITE_METHODS:
            try:
                write, episode = self._observe(method, h.path, h.headers, body)
            except Exception as exc:  # noqa: BLE001 - the write goes on regardless
                self._note_error(f"observe {method}: {exc!r}")
        held = time.perf_counter()
        if episode is not None and episode.baseline is not None:
            self._wait_baseline(episode)
        if write is not None:
            write.held_ms = (time.perf_counter() - held) * 1000
        t_up = time.perf_counter()
        status, headers, rbody, failure = self._forward(method, h.path, h.headers, body)
        upstream_s = time.perf_counter() - t_up
        try:
            h.send_response_only(status)
            for key, value in headers:
                h.send_header(key, value)
            h.send_header("Content-Length", str(len(rbody)))
            h.end_headers()
            if method != "HEAD":
                h.wfile.write(rbody)
        except OSError:
            pass  # the agent hung up; the write already went on
        if write is not None and episode is not None:
            write.upstream_ms = upstream_s * 1000
            write.proxy_ms = (time.perf_counter() - t0 - upstream_s) * 1000
            write.status = status
            write.error = failure
            try:
                self._answered(episode, write, rbody, headers)
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"answered {method}: {exc!r}")

    def _forward(
        self, method: str, path: str, headers: Any, body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes, str]:
        up = self.upstream
        cls = (
            http.client.HTTPSConnection
            if up.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = cls(up.hostname, up.port, timeout=300)
        target = (up.path.rstrip("/") + path) if up.path not in ("", "/") else path
        sent = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        sent["Host"] = up.netloc
        if body or method in WRITE_METHODS:
            sent["Content-Length"] = str(len(body))
        try:
            conn.request(method, target, body=body or None, headers=sent)
            resp = conn.getresponse()
            data = resp.read()
            kept = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
            return resp.status, kept, data, ""
        except (OSError, http.client.HTTPException) as exc:
            msg = f"upstream unreachable: {type(exc).__name__}"
            return 502, [("Content-Type", "text/plain")], msg.encode(), msg
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
        done = self._by_ref.get(ref) if ref else None
        if done is not None and done.checked:
            ep = self._unchecked(
                rtype, f"a later write to a record already checked (check {done.id})"
            )
        elif done is not None:
            ep = done
        else:
            params: dict[str, Any] = {}
            if isinstance(doc, dict) and doc.get("resourceType") == rtype:
                for name, rule in contract.fields.items():
                    value = extract(doc, rule.path)
                    if value is not None and not isinstance(value, (dict, list)):
                        params[name] = value
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
            ep = self._by_key.get(key) if key else None
            if ep is not None and ep.checked:
                ep = None  # checked already: a new write starts a new episode
            if ep is None:
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
        if ref and ep.kind == "fhir":
            ep.refs.add(ref)
            self._by_ref[ref] = ep
        return ep

    @staticmethod
    def _fhir_baseline(contract: Any, params: dict[str, Any]) -> Any:
        from openadapt_flow.ensure_record import capture_baseline

        return capture_baseline(contract, params)

    def _sql_episode(self, method: str, path: str, doc: Any) -> Episode:
        line = f"{method} {urlsplit(path).path}"
        route = next((r for r in self.routes if r.write.fullmatch(line)), None)
        if route is None:
            return self._unchecked(line, "no route matches this write")
        params = {p: _dotted(doc, where) for p, where in route.params.items()}
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
        if fut is None or fut.done():
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

    def _answered(
        self, ep: Episode, write: Write, rbody: bytes, headers: list[tuple[str, str]]
    ) -> None:
        with self._lock:
            ep.in_flight -= 1
            ep.last_end = time.time()
            self._last_write = max(self._last_write, ep.last_end)
            if self.mode != "fhir" or write.status // 100 != 2:
                return
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
                    self._check_due(force=False)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"checker: {exc!r}")

    def _check_due(self, force: bool) -> None:
        with self._check_lock:
            with self._lock:
                due = [e for e in self.episodes if not e.checked and not e.in_flight]
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
        from openadapt_flow.ensure_record import (
            FhirReader,
            Unreadable,
            extra_elements,
            extract,
            verify_ensure,
        )

        contract = ep.contract
        with self._lock:
            declared = [r for o in self.episodes if o is not ep for r in o.refs]
        result = verify_ensure(contract, ep.params, base, declared=declared)
        ev = result.evidence
        ep.evidence["verifier"] = {"outcome": result.outcome, **ev}
        landed = int(ev.get("agent_writes") or 0)
        if result.outcome == "created_verified":
            ep.verdict, ep.reasons = CONFIRMED, list(result.reasons)
            if any(s // 100 != 2 for s in statuses):
                ep.reasons.append(
                    f"the agent got an error reply ({', '.join(map(str, statuses))}) "
                    "but the write landed"
                )
        elif ev.get("unreadable"):
            ep.verdict, ep.reasons = UNCERTAIN, list(result.reasons)
        elif result.outcome == "needs_review" and landed == 0 and "agent_writes" in ev:
            if all(s // 100 in (4,) for s in statuses):
                ep.verdict = CONFIRMED
                ep.reasons = ["the server refused the write and nothing was written"]
            else:
                ep.verdict = UNCERTAIN
                ep.reasons = [
                    f"the reply was {', '.join(map(str, statuses))} but the store "
                    "shows no write by the agent"
                ] + list(result.reasons)
        else:
            ep.verdict, ep.reasons = REFUTED, list(result.reasons)
        # Evidence: what the agent sent, and what the store holds now.
        reader = FhirReader(contract)
        ep.evidence["before"] = sorted(
            f"{contract.resource_type}/{i}" for i in base.matches
        )
        stored: dict[str, Any] = {}
        for ref in sorted(ep.refs):
            try:
                _, stored[ref] = reader.fetch(f"{reader.base_url}/{ref}")
            except Unreadable as exc:
                stored[ref] = {"unreadable": str(exc)}
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
                        "same": str(sent) == str(got),
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
        ep.verdict = result.outcome.value
        ep.reasons = list(result.reasons)
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

    def _note_error(self, text: str) -> None:
        with self._lock:
            self.errors.append(text[:2000])

    # -- results -----------------------------------------------------------------------

    def results(self) -> dict[str, Any]:
        from openadapt_flow.shadow_report import summarize

        with self._lock:
            return summarize(self)
