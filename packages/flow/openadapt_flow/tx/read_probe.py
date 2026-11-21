"""Learn which ``POST`` routes only read, by replaying them on a sandbox.

Many apps read with ``POST``: chart fragments, search forms, query APIs. The
wire tap treats every ``POST`` it cannot classify as a mutation, so those reads
use up the write budget and the real save is blocked (OpenAdapt #2389).

Guessing from names would be fail-open: one RPC endpoint can carry both reads
and writes. This module takes evidence instead. For each ``POST`` route in a
recorded trace it replays every observed request against a sandbox copy of
the app, and reads a caller-supplied digest of the sandbox's whole store
before and after. Before every replay the caller's ``store_reset`` puts the
sandbox back in its state from before the trace. Without that, replaying a
recorded save would write the value the store already holds and look like a
read. A route is learned as a read only when

- the store was idle (two digests with no request in between agree),
- every replay returned 2xx, and
- no replay changed the digest.

What it cannot see: a write route whose every recorded sample wrote values
the pre-trace store already held. Such a request changed nothing in the
trace either. The engine's read-back of the target record still judges any
write that reaches it.

The learned ``ReadRoute`` keys on the method, the exact path and every
top-level body field whose value was the same in all samples, so a write on
the same route with a different operation field never matches it. Fields
that look like credentials or anti-forgery tokens are never stored.

The report is data for ``WirePolicy.read_routes``. Nothing here runs against
anything but a loopback sandbox unless the caller opts in.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, ConfigDict

from openadapt_flow.tx.induce import _assert_sandbox_url_allowed
from openadapt_flow.tx.wire_tap import (
    ReadRoute,
    _top_level_body_fields,
    is_mutation_request,
)

_CREDENTIAL_KEY_RE = re.compile(
    r"(pass|secret|token|csrf|xsrf|nonce|session|auth|api[_-]?key|cookie|signature)",
    re.IGNORECASE,
)

#: Request headers replayed from the trace. Anything that could carry a
#: credential comes from ``headers`` instead, supplied by the caller.
_REPLAYED_HEADERS = frozenset({"content-type", "accept", "x-requested-with"})


class ReadRouteProbe(BaseModel):
    """What the probe saw for one route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str
    path: str
    samples: int
    read_only: bool
    reason: str
    body_equals: tuple[tuple[str, str], ...] = ()


class ReadRouteReport(BaseModel):
    """Learned read routes plus the evidence for every route probed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_idle: bool
    routes: tuple[ReadRouteProbe, ...]

    @property
    def read_routes(self) -> tuple[ReadRoute, ...]:
        if not self.store_idle:
            return ()
        return tuple(
            ReadRoute(
                method=r.method, path=re.escape(r.path), body_equals=r.body_equals
            )
            for r in self.routes
            if r.read_only
        )


class _Sample:
    __slots__ = ("method", "path", "query", "body", "headers")

    def __init__(
        self,
        method: str,
        path: str,
        query: str,
        body: bytes,
        headers: list[tuple[str, str]],
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers


def _har_body(request: Mapping[str, Any]) -> bytes:
    post = request.get("postData")
    if not isinstance(post, Mapping):
        return b""
    text = str(post.get("text") or "")
    if post.get("encoding") == "base64":
        try:
            return base64.b64decode(text)
        except ValueError:
            return b""
    return text.encode("utf-8")


def _post_samples(har: Mapping[str, Any]) -> dict[tuple[str, str], list[_Sample]]:
    log = har.get("log")
    entries = log.get("entries") if isinstance(log, Mapping) else None
    groups: dict[tuple[str, str], list[_Sample]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        req = entry.get("request")
        resp = entry.get("response")
        if not isinstance(req, Mapping) or not isinstance(resp, Mapping):
            continue
        method = str(req.get("method", "")).upper()
        if method != "POST":
            continue
        status = int(resp.get("status") or 0)
        if not 200 <= status < 300:
            continue
        parts = urllib.parse.urlsplit(str(req.get("url", "")))
        headers = [
            (str(h.get("name", "")), str(h.get("value", "")))
            for h in req.get("headers") or []
            if isinstance(h, Mapping)
        ]
        body = _har_body(req)
        is_mut, is_gql_mut = is_mutation_request(method, req["url"], body, headers)
        if not is_mut or is_gql_mut:
            continue  # already a read, or a declared GraphQL mutation
        path = parts.path or "/"
        groups.setdefault((method, path), []).append(
            _Sample(method, path, parts.query, body, headers)
        )
    return groups


def _constant_fields(samples: list[_Sample]) -> Optional[tuple[tuple[str, str], ...]]:
    common: Optional[dict[str, str]] = None
    for sample in samples:
        fields = _top_level_body_fields(sample.body, sample.headers)
        if fields is None:
            return None
        if common is None:
            common = dict(fields)
        else:
            common = {k: v for k, v in common.items() if fields.get(k) == v}
    return tuple(
        sorted(
            (k, v)
            for k, v in (common or {}).items()
            if not _CREDENTIAL_KEY_RE.search(k)
        )
    )


def _replay(
    base_url: str,
    sample: _Sample,
    headers: Mapping[str, str],
    timeout: float,
) -> int:
    url = base_url.rstrip("/") + sample.path
    if sample.query:
        url += "?" + sample.query
    req_headers = {
        k: v for k, v in sample.headers if k.lower() in _REPLAYED_HEADERS and v
    }
    req_headers.update(headers)
    request = urllib.request.Request(
        url, data=sample.body or None, headers=req_headers, method=sample.method
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as resp:
            resp.read()
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except urllib.error.URLError:
        return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def probe_read_routes(
    har: Mapping[str, Any],
    *,
    sandbox_base_url: str,
    store_digest: Callable[[], str],
    store_reset: Callable[[], None],
    headers: Optional[Mapping[str, str]] = None,
    allow_remote_sandbox: bool = False,
    timeout: float = 15.0,
    routes: Optional[Iterable[tuple[str, str]]] = None,
) -> ReadRouteReport:
    """Replay a trace's ``POST`` routes on a sandbox; learn the read-only ones.

    ``store_digest`` must hash the sandbox's whole persistent store (every
    table, or the data directory), not one record: a read route must change
    nothing anywhere. ``store_reset`` must restore the sandbox to its state
    from before the trace was recorded; it runs before every replay. ``routes`` limits the probe to some ``(method, path)``
    pairs from the trace.
    """
    _assert_sandbox_url_allowed(
        sandbox_base_url, allow_remote_sandbox=allow_remote_sandbox
    )
    groups = _post_samples(har)
    if routes is not None:
        wanted = {(m.upper(), p) for m, p in routes}
        groups = {k: v for k, v in groups.items() if k in wanted}
    auth = dict(headers or {})

    store_reset()
    first = store_digest()
    idle = store_digest() == first
    probes: list[ReadRouteProbe] = []
    for (method, path), samples in sorted(groups.items()):
        if not idle:
            probes.append(
                ReadRouteProbe(
                    method=method,
                    path=path,
                    samples=len(samples),
                    read_only=False,
                    reason="store not idle: digests differ with no request",
                )
            )
            continue
        body_equals = _constant_fields(samples)
        if body_equals is None:
            probes.append(
                ReadRouteProbe(
                    method=method,
                    path=path,
                    samples=len(samples),
                    read_only=False,
                    reason="body is neither a JSON object nor a form",
                )
            )
            continue
        reason = "every replay returned 2xx and left the store unchanged"
        read_only = True
        for sample in samples:
            store_reset()
            before = store_digest()
            status = _replay(sandbox_base_url, sample, auth, timeout)
            after = store_digest()
            if not 200 <= status < 300:
                read_only, reason = False, f"replay returned {status}"
                break
            if after != before:
                read_only, reason = False, "replay changed the store"
                break
        probes.append(
            ReadRouteProbe(
                method=method,
                path=path,
                samples=len(samples),
                read_only=read_only,
                reason=reason,
                body_equals=body_equals if read_only else (),
            )
        )
    return ReadRouteReport(store_idle=idle, routes=tuple(probes))


def dump_read_routes(report: ReadRouteReport) -> str:
    """Serialize learned routes as JSON data for a policy file."""
    return json.dumps(
        [route.model_dump() for route in report.read_routes], indent=2, sort_keys=True
    )


__all__ = [
    "ReadRouteProbe",
    "ReadRouteReport",
    "dump_read_routes",
    "probe_read_routes",
]
