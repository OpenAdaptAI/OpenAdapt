"""Generate the read channel, and admit it only if it reads true and fails closed.

Candidates come from what the application already publishes:

- an OpenAPI document: every ``GET`` with no path parameter whose ``200``
  response is a list of objects, or an object holding one (``data``,
  ``records``, ...), with its next-page link when the schema has one;
- a FHIR CapabilityStatement: every resource type the server can search,
  with field paths taken from the resources it returns;
- the traffic captured while teaching (``network.jsonl``): the JSON reads the
  app made after the save (``record_check_synthesis.reads_from_network``).

A candidate is admitted only after the **channel test**, which doesn't trust
the generator:

1. **It reads the demonstrations' records.** Read before and after each
   demonstration, the records it added or changed must include exactly one
   holding the typed values.
2. **It reads every page.** A response that says there is more (a FHIR
   ``next`` link, a ``total`` larger than what came back, a next link in a
   REST body the channel doesn't follow) refuses the candidate.
3. **It fails closed.** Through a fault proxy the read must come back
   unreadable, never as an empty or partial set, when the credentials are
   missing, the server answers 401 or 500, the body is cut short, the body is
   not JSON, or a later page fails.

Content from the application is data. Descriptions, extension fields and
example values in a published document never change what is generated.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

#: Body keys that hold the records list, in order of preference.
RECORD_KEYS = ("data", "records", "items", "results", "entries", "rows", "entry")
#: Query parameters that set a page size, and the size the channel asks for.
PAGE_SIZE_PARAMS = ("_count", "_limit", "limit", "page_size", "per_page", "pageSize")
PAGE_SIZE = 200


@dataclass
class ChannelCandidate:
    """A proposed ``effects:`` verifier section and where it came from."""

    section: dict[str, Any]
    origin: str
    notes: list[str] = field(default_factory=list)


# -- candidates ---------------------------------------------------------------


def _resolve(doc: Mapping[str, Any], schema: Any, depth: int = 0) -> Any:
    if depth > 8 or not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: Any = doc
        for part in ref[2:].split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        return _resolve(doc, node, depth + 1)
    return schema


def _response_schema(doc: Mapping[str, Any], op: Mapping[str, Any]) -> Any:
    responses = op.get("responses") or {}
    ok = responses.get("200") or responses.get(200) or {}
    ok = _resolve(doc, ok)
    content = (ok or {}).get("content") or {}
    for media, body in content.items():
        if "json" in str(media):
            return _resolve(doc, (body or {}).get("schema"))
    return _resolve(doc, (ok or {}).get("schema"))  # Swagger 2


def _list_shape(
    doc: Mapping[str, Any], schema: Any
) -> Optional[tuple[Optional[str], Optional[str]]]:
    """(records_key, next_path) when ``schema`` is a list of objects."""

    schema = _resolve(doc, schema)
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "array":
        items = _resolve(doc, schema.get("items"))
        if isinstance(items, dict) and (
            items.get("type") == "object" or "properties" in items
        ):
            return (None, None)
        return None
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return None
    for key in RECORD_KEYS:
        inner = _resolve(doc, props.get(key))
        if isinstance(inner, dict) and inner.get("type") == "array":
            items = _resolve(doc, inner.get("items"))
            if isinstance(items, dict) and (
                items.get("type") == "object" or "properties" in items
            ):
                next_path = None
                links = _resolve(doc, props.get("links"))
                if isinstance(links, dict) and "next" in (
                    links.get("properties") or {}
                ):
                    next_path = "links.next"
                elif "next" in props:
                    next_path = "next"
                return (key, next_path)
    return None


def candidates_from_openapi(
    doc: Mapping[str, Any],
    base_url: str,
    *,
    auth: Optional[Mapping[str, Any]] = None,
) -> list[ChannelCandidate]:
    """List reads an OpenAPI (3.x or Swagger 2) document publishes."""

    out: list[ChannelCandidate] = []
    paths = doc.get("paths") or {}
    for path, item in sorted(paths.items()):
        if not isinstance(item, dict) or "{" in str(path):
            continue
        op = item.get("get")
        if not isinstance(op, dict):
            continue
        params = [_resolve(doc, p) for p in op.get("parameters") or []]
        if any(
            isinstance(p, dict) and p.get("required") and p.get("in") != "header"
            for p in params
        ):
            continue
        shape = _list_shape(doc, _response_schema(doc, op))
        if shape is None:
            continue
        records_key, next_path = shape
        query = ""
        names = {
            str(p.get("name"))
            for p in params
            if isinstance(p, dict) and p.get("in") == "query"
        }
        size = next((n for n in PAGE_SIZE_PARAMS if n in names), None)
        if size:
            query = f"?{size}={PAGE_SIZE}"
        section: dict[str, Any] = {
            "kind": "rest",
            "base_url": base_url.rstrip("/"),
            "records_path": f"{path}{query}",
            "records_key": records_key,
        }
        if next_path:
            section["records_next_path"] = next_path
        if auth:
            section["auth"] = dict(auth)
        out.append(ChannelCandidate(section, f"openapi GET {path}"))
    return out


def candidates_from_fhir(
    capability: Mapping[str, Any],
    base_url: str,
    *,
    auth: Optional[Mapping[str, Any]] = None,
) -> list[ChannelCandidate]:
    """One candidate per resource type the server can search."""

    out: list[ChannelCandidate] = []
    for rest in capability.get("rest") or []:
        if not isinstance(rest, dict) or rest.get("mode", "server") != "server":
            continue
        for resource in rest.get("resource") or []:
            if not isinstance(resource, dict):
                continue
            kinds = {
                i.get("code")
                for i in resource.get("interaction") or []
                if isinstance(i, dict)
            }
            if "search-type" not in kinds or not resource.get("type"):
                continue
            section: dict[str, Any] = {
                "kind": "fhir",
                "base_url": base_url.rstrip("/"),
                "resource_type": str(resource["type"]),
                "search_params": {"_count": str(PAGE_SIZE)},
            }
            if auth:
                section.update(dict(auth))
            out.append(ChannelCandidate(section, f"fhir search {resource['type']}"))
    return out


def leaf_paths(resource: Any, prefix: str = "", limit: int = 80) -> list[str]:
    """Dotted paths to the scalar fields of a FHIR resource (lists: first item,
    as :func:`openadapt_flow.runtime.effects.fhir.extract_path` reads them)."""

    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, list):
            if node:
                walk(node[0], path)
            return
        if isinstance(node, dict):
            for key in sorted(node):
                walk(node[key], f"{path}.{key}" if path else key)
            return
        if path:
            out.append(path)

    walk(resource, prefix)
    return out


def with_field_paths(
    candidate: ChannelCandidate, samples: Sequence[Mapping[str, Any]]
) -> ChannelCandidate:
    """A FHIR candidate with field paths for every scalar its resources hold."""

    paths: list[str] = []
    for sample in samples:
        for path in leaf_paths(sample):
            if path not in paths:
                paths.append(path)
    section = dict(candidate.section)
    section["field_paths"] = {p: p for p in paths if p != "meta.lastUpdated"}
    if "id" not in section["field_paths"]:
        section["field_paths"]["id"] = "id"
    return ChannelCandidate(section, candidate.origin, list(candidate.notes))


# -- reading ---------------------------------------------------------------------


Reader = Callable[[Mapping[str, Any]], Optional[list[dict[str, Any]]]]


def read_through(
    section: Mapping[str, Any], params: Mapping[str, object]
) -> Optional[list[dict[str, Any]]]:
    """Read the store once through ``section``; None when unreadable."""

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

    try:
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(dict(section)), params
        )
        if verifier is None:
            return None
        state = verifier.capture_pre_state()
    except Exception:  # noqa: BLE001 - a channel that can't be built can't read
        return None
    return list(state.records) if state.reachable else None


# -- the channel test -------------------------------------------------------------


@dataclass
class DemoReads:
    params: dict[str, str]
    before: Optional[list[dict[str, Any]]]
    after: Optional[list[dict[str, Any]]]


@dataclass
class ChannelReport:
    candidate: ChannelCandidate
    grounded: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    probes: dict[str, str] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return (
            not self.problems
            and bool(self.grounded)
            and all(v == "failed closed" for v in self.probes.values())
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.candidate.origin,
            "admitted": self.admitted,
            "grounded": self.grounded,
            "problems": self.problems,
            "probes": self.probes,
        }


def _norm(value: Any) -> str:
    return " ".join(("" if value is None else str(value)).split()).casefold()


def _key(record: Mapping[str, Any]) -> str:
    from openadapt_flow.runtime.effects.effect import stable_id

    return json.dumps(stable_id(dict(record)), default=str, sort_keys=True)


def ground(candidate: ChannelCandidate, demos: Sequence[DemoReads]) -> ChannelReport:
    """Check 1: the channel shows each demonstration's record, exactly once."""

    report = ChannelReport(candidate)
    for i, demo in enumerate(demos, start=1):
        if demo.before is None or demo.after is None:
            report.problems.append(f"demonstration {i}: the channel could not be read")
            continue
        before = {_key(r): r for r in demo.before}
        changed = [
            r for r in demo.after if _key(r) not in before or before[_key(r)] != r
        ]
        typed = {_norm(v) for v in demo.params.values() if _norm(v)}
        holding = [
            r for r in changed if sum(_norm(v) in typed for v in r.values()) >= 2
        ]
        if len(holding) == 1:
            report.grounded.append(f"demonstration {i}")
        elif not holding:
            report.problems.append(
                f"demonstration {i}: no record the save added or changed holds the "
                "typed values"
            )
        else:
            report.problems.append(
                f"demonstration {i}: {len(holding)} changed records hold the typed values"
            )
    return report


def paging_problem(raw: Any, section: Mapping[str, Any]) -> Optional[str]:
    """Check 2 on one raw response body: does it say there is more?"""

    if not isinstance(raw, dict):
        return None
    if raw.get("resourceType") == "Bundle":
        return None  # the FHIR verifier follows next links and checks total
    if section.get("records_next_path"):
        return None
    links = raw.get("links")
    nxt = (links or {}).get("next") if isinstance(links, dict) else raw.get("next")
    if nxt:
        return (
            "the response links a next page that this channel would not read; "
            "set records_next_path"
        )
    records = raw.get(section.get("records_key") or "")
    for total_key in ("total", "count", "totalCount", "total_count"):
        total = raw.get(total_key)
        if (
            isinstance(total, int)
            and isinstance(records, list)
            and total > len(records)
        ):
            return f"the response says {total} records and returned {len(records)}"
    return None


# -- fail-closed probes -----------------------------------------------------------

PROBES = (
    "no_auth",
    "status_401",
    "status_500",
    "truncated",
    "not_json",
    "next_page_fails",
)


class FaultProxy:
    """A local proxy in front of the store that breaks reads on purpose.

    ``mode`` is one of :data:`PROBES` (or ``pass``). Next-page links in a body
    that point at the store are rewritten to point at the proxy, so a failed
    later page can be injected.
    """

    def __init__(self, target: str, *, verify_tls: bool = True) -> None:
        self.target = target.rstrip("/")
        self.verify_tls = verify_tls
        self.mode = "pass"
        self.requests = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                proxy._handle(self)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address[:2]
        self.url = f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _send(self, handler: Any, status: int, body: bytes, ctype: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler: Any) -> None:
        import ssl
        import urllib.error
        import urllib.request

        self.requests += 1
        mode = self.mode
        if mode == "status_401":
            return self._send(
                handler, 401, b'{"error":"unauthorized"}', "application/json"
            )
        if mode == "status_500":
            return self._send(handler, 500, b'{"error":"boom"}', "application/json")
        if mode == "not_json":
            return self._send(
                handler, 200, b"<html><body>Sign in</body></html>", "text/html"
            )
        if mode == "next_page_fails" and self.requests > 1:
            return self._send(handler, 502, b"", "text/plain")
        target_path = urlsplit(self.target).path.rstrip("/")
        path = handler.path
        if target_path and path.startswith(target_path):
            path = path[len(target_path) :]
        url = self.target + path
        headers = {
            k: v
            for k, v in handler.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
            and not (mode == "no_auth" and k.lower() in ("authorization", "cookie"))
        }
        context = None if self.verify_tls else ssl._create_unverified_context()  # noqa: S323
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers),
                timeout=30,
                context=context,
            ) as resp:
                status, body, ctype = (
                    resp.status,
                    resp.read(),
                    resp.headers.get("Content-Type", "application/json"),
                )
        except urllib.error.HTTPError as exc:
            status, body, ctype = (
                exc.code,
                exc.read(),
                exc.headers.get("Content-Type", "application/json"),
            )
        except Exception:  # noqa: BLE001
            return self._send(handler, 502, b"", "text/plain")
        origin = urlunsplit(urlsplit(self.target)[:2] + ("", "", ""))
        body = body.replace(origin.encode(), self.url.encode())
        if mode == "truncated":
            body = body[: max(1, len(body) // 2)]
        self._send(handler, status, body, ctype)


def probe(
    candidate: ChannelCandidate,
    params: Mapping[str, object],
    *,
    reader: Optional[Reader] = None,
    verify_tls: bool = True,
) -> dict[str, str]:
    """Check 3: every probe must read as unreadable."""

    read: Reader = (
        reader
        if reader is not None
        else (lambda section: read_through(section, params))
    )
    base = str(candidate.section.get("base_url") or "")
    proxy = FaultProxy(base, verify_tls=verify_tls)
    results: dict[str, str] = {}
    try:
        for mode in PROBES:
            proxy.mode = mode
            proxy.requests = 0
            section = {**candidate.section, "base_url": proxy.url + urlsplit(base).path}
            if "verify_tls" in section:
                section["verify_tls"] = True
            if mode == "next_page_fails" and not (
                candidate.section.get("records_next_path")
                or candidate.section.get("kind") == "fhir"
            ):
                results[mode] = "failed closed"  # the channel reads one page only
                continue
            records = read(section)
            if records is None:
                results[mode] = "failed closed"
            elif mode == "next_page_fails" and proxy.requests <= 1:
                # Only one page existed, so a failing later page was not reached.
                results[mode] = "failed closed"
            else:
                results[mode] = f"READ {len(records)} RECORDS"
    finally:
        proxy.close()
    return results


def check_channel(
    candidate: ChannelCandidate,
    demos: Sequence[DemoReads],
    params: Mapping[str, object],
    *,
    raw_body: Any = None,
    reader: Optional[Reader] = None,
    verify_tls: bool = True,
) -> ChannelReport:
    """All three checks. ``raw_body`` is one response body, for the paging check."""

    report = ground(candidate, demos)
    problem = paging_problem(raw_body, candidate.section)
    if problem:
        report.problems.append(problem)
    report.probes = probe(candidate, params, reader=reader, verify_tls=verify_tls)
    for mode, result in report.probes.items():
        if result != "failed closed":
            report.problems.append(
                f"{mode}: the channel {result.lower()} instead of failing"
            )
    return report
