"""Find a read for a save the demonstration never read back.

``scaffold-verifier`` builds a record check from a read that returns the saved
record. Many apps show the result from page memory and never read it back, so
the recording holds the write and no read. This module derives candidate reads
from the captured write and tries them, read-only, on a local copy of the app.

For an accepted ``POST``/``PUT``/``PATCH`` to ``<prefix>/<collection>`` or
``<prefix>/<collection>/<id>`` the candidates are the collection
(``GET <prefix>/<collection>``, and its plural or singular spelling) and the
item (``GET <prefix>/<collection>/<id>``, the id taken from the URL or from the
write's response). Each candidate gets one ``GET`` on the recorded origin (or
on ``probe_base``), and only when that host is the local machine: a probe
never leaves localhost and never sends anything but ``GET``. No cookies or
credentials are sent, and redirects are not followed.

A collection read is promoted to the check's read only when its response holds
a record carrying every parameter value the write sent. An item read that
confirms the write is recorded, but it can't count new records, so it is not
promoted. When nothing confirms, the read stays UNAVAILABLE: the result says
so, lists every probe, and needs a person to name the read. Nothing is
invented.

A promoted collection read sees only the written collection, so a write
elsewhere in the store would pass it (an open ``collateral_unaudited`` miss)
and the check is refused. Before that, :func:`cover` looks for a read that
covers the whole save: the sibling collections under the same prefix that the
app itself read (taken from its captured ``GET`` requests, path only), and the
prefix root (``GET <prefix>``). The root is promoted in place of the
collection only when it holds the written record, holds every record the
collection and each sibling returned, is one unpaged response, and every
captured write went under the prefix. The check then reads every list the
root returns: the one with the saved record may gain exactly one row, the
others none. When nothing covers the save, the refusal stands and its reason
says what was probed.

Every probe (URL with record ids masked, status, outcome) is recorded in
``search.read_discovery`` and from there in the proof.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from openadapt_flow import record_check_synthesis as rcs

SCHEMA = "openadapt.read-discovery/v1"
#: Largest probe response read; a longer body is cut and can't confirm.
MAX_PROBE_BYTES = 2 * 1024 * 1024
PROBE_TIMEOUT_S = 5.0
#: Most probes for one save.
MAX_PROBES = 12
#: Most probes when looking for a read that covers the whole save.
MAX_COVER_PROBES = 8

_PROBE_METHODS = frozenset({"POST", "PUT", "PATCH"})
_ID_KEYS = ("id", "_id", "uuid")
_WRAPPERS = ("record", "data", "item", "result", "resource", "created")


class ProbeRefused(ValueError):
    """The probe target is not the local machine."""


def is_local_origin(url: str) -> bool:
    """True when ``url``'s host is this machine (a loopback name or address)."""

    host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get(url: str) -> tuple[Optional[int], Optional[str], bool]:
    """``(status, text, truncated)`` for one GET on the local machine."""

    if not is_local_origin(url):
        raise ProbeRefused(f"probe refused: {urlsplit(url).hostname} is not local")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        # Never send credentials: drop any user:password from the URL.
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        url = parts._replace(netloc=netloc).geturl()
    # No proxy (a proxy would carry the probe off this machine), no cookies,
    # no redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT_S) as resp:
            raw = resp.read(MAX_PROBE_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None, False
    except (urllib.error.URLError, OSError, ValueError):
        return None, None, False
    truncated = len(raw) > MAX_PROBE_BYTES
    return status, raw[:MAX_PROBE_BYTES].decode("utf-8", errors="replace"), truncated


@dataclass(frozen=True)
class Candidate:
    path: str
    #: ``"collection"`` or ``"item"``.
    kind: str


def _spellings(name: str) -> list[str]:
    """The collection name, then its plural or singular spelling."""

    out = [name]
    low = name.lower()
    if low.endswith("ies") and len(name) > 3:
        out.append(name[:-3] + "y")
    elif low.endswith(("ses", "xes", "ches", "shes")):
        out.append(name[:-2])
    elif low.endswith("s") and not low.endswith("ss"):
        out.append(name[:-1])
    elif low.endswith("y") and len(name) > 1 and low[-2] not in "aeiou":
        out.append(name[:-1] + "ies")
    elif low.endswith(("s", "x", "ch", "sh")):
        out.append(name + "es")
    else:
        out.append(name + "s")
    return list(dict.fromkeys(out))


def written_id(line: Mapping[str, Any]) -> Optional[str]:
    """The id the write's response gave the saved record, if it named one."""

    body = rcs._json_body(line)
    nodes = [body]
    if isinstance(body, dict):
        nodes += [body.get(key) for key in _WRAPPERS]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in _ID_KEYS:
            value = node.get(key)
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value).strip()
            if text and "/" not in text:
                return text
    return None


def candidates_for(
    line: Mapping[str, Any], lookup: Mapping[str, str]
) -> list[Candidate]:
    """Collection and item reads that would return what ``line`` wrote."""

    path = urlsplit(str(line.get("url") or "")).path.rstrip("/")
    segments = path.split("/")
    if len(segments) < 2 or not segments[-1]:
        return []
    new_id = written_id(line)
    last = segments[-1]
    item: Optional[str]
    if (
        rcs._RECORD_SEGMENT.search(last)
        or (new_id is not None and last == new_id)
        or last in lookup
    ):
        if len(segments) < 3 or not segments[-2]:
            return []
        prefix, name, item = "/".join(segments[:-2]), segments[-2], last
    else:
        prefix, name, item = "/".join(segments[:-1]), last, new_id
    out: list[Candidate] = []
    for spelling in _spellings(name):
        out.append(Candidate(f"{prefix}/{spelling}", "collection"))
    if item:
        for spelling in _spellings(name):
            out.append(Candidate(f"{prefix}/{spelling}/{item}", "item"))
    return list(dict.fromkeys(out))


def _sent_params(line: Mapping[str, Any], params: Mapping[str, str]) -> dict[str, str]:
    """Parameter name -> value, for each parameter value the write sent."""

    lookup, _ = rcs.param_lookup(params)
    sent = rcs._write_values(line, excluded_values=params.values())
    return {lookup[v]: v for v in sent.values() if v in lookup}


def _confirms_item(body: Any, sent: Mapping[str, str]) -> bool:
    nodes = [body]
    if isinstance(body, dict):
        nodes += [body.get(key) for key in _WRAPPERS]
    for node in nodes:
        if isinstance(node, dict):
            values = {str(v) for v in rcs._flat(node).values()}
            if set(sent.values()) <= values:
                return True
    return False


def discover(
    params: Mapping[str, str],
    search: rcs.ReadSearch,
    *,
    probe_base: Optional[str] = None,
    get: Any = http_get,
) -> Optional[rcs.Surface]:
    """Probe the reads the save's writes imply; promote one that confirms.

    Returns the promoted surface (also appended to ``search.surfaces``), or
    ``None`` with ``search.read_discovery["status"] == "unavailable"``.
    """

    lookup, _ = rcs.param_lookup(params)
    probes: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "method": "GET only, local machine only, after the save",
        "probes": probes,
    }
    search.read_discovery = result

    def unavailable(reason: str) -> None:
        result.update(
            status="unavailable",
            needs_operator_confirmation=True,
            reason=reason,
        )
        search.notes.append(f"read discovery: {reason}")

    writes = [
        line
        for line in search.write_requests
        if str(line.get("method") or "").upper() in _PROBE_METHODS
    ]
    if not writes:
        unavailable("the capture holds no accepted create or update request")
        return None
    if probe_base is not None and not is_local_origin(probe_base):
        unavailable(
            f"the probe base {urlsplit(probe_base).hostname} is not the local "
            "machine; probes run only on a local copy of the app"
        )
        return None
    item_confirmed = False
    for line in writes:
        sent = _sent_params(line, params)
        url = str(line.get("url") or "")
        base = (probe_base or rcs._origin(url)).rstrip("/")
        if not sent:
            probes.append(
                {
                    "write": f"{line.get('method')} {rcs._templated(url, lookup).safe}",
                    "outcome": "skipped: the write sent no recorded parameter value",
                }
            )
            continue
        if not is_local_origin(base):
            unavailable(
                f"the write went to {urlsplit(base).hostname}, not the local "
                "machine; give --probe-base with a local copy of the app"
            )
            return None
        for cand in candidates_for(line, lookup):
            if len(probes) >= MAX_PROBES:
                break
            probe_url = base + cand.path
            safe = rcs._templated(probe_url, lookup).safe
            row: dict[str, Any] = {"method": "GET", "path": safe, "kind": cand.kind}
            probes.append(row)
            status, text, truncated = get(probe_url)
            row["status"] = status
            if status is None or not 200 <= status < 300 or text is None:
                row["outcome"] = "no read here"
                continue
            fake = {
                "method": "GET",
                "url": probe_url,
                "status": status,
                "response_text": text,
                "response_truncated": truncated,
                "t": None,
            }
            body = rcs._json_body(fake)
            if body is None:
                row["outcome"] = "not JSON (or cut)"
                continue
            if cand.kind == "item":
                if _confirms_item(body, sent):
                    item_confirmed = True
                    row["outcome"] = (
                        "confirms the written values, but an item read can't "
                        "count new records; not promoted"
                    )
                else:
                    row["outcome"] = "does not hold the written values"
                continue
            best: Optional[rcs.Surface] = None
            for listing in rcs._listings(body):
                surface, why = rcs._surface_from_listing(
                    fake,
                    listing,
                    lookup,
                    before_line=None,
                    taken=set(search.taken),
                    excluded_values=params.values(),
                )
                if surface is None:
                    if why:
                        row.setdefault("rejected", []).append(why)
                    continue
                if not set(sent) <= set(surface.bound.values()):
                    continue
                if best is None or len(surface.bound) > len(best.bound):
                    best = surface
            if best is None:
                row["outcome"] = "no record holds every written value"
                continue
            best.source = f"probe GET {safe} (read-only, after the save)"
            search.taken.update({best.name, best.content_name})
            search.surfaces.append(best)
            row["outcome"] = "confirmed: holds a record with every written value"
            row["confirmed_fields"] = sorted(best.bound)
            result.update(
                status="promoted",
                needs_operator_confirmation=True,
                read=safe,
                surface=best.name,
            )
            search.notes.append(
                f"read discovery: GET {safe} was found by probing and confirmed "
                "the written values; a person confirms it is the system of record"
            )
            return best
    unavailable(
        "no probed collection read returned a record with every written value"
        + (
            " (an item read did, but it can't count new records)"
            if item_confirmed
            else ""
        )
        + "; a person must name the read"
    )
    return None


# ---------------------------------------------------------------------------
# A read that covers the whole save
# ---------------------------------------------------------------------------


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _holds(records: list[dict[str, Any]], record: Mapping[str, Any]) -> bool:
    """True when some record of ``records`` carries every field of ``record``."""

    items = {k: _json_key(v) for k, v in record.items()}
    return any(
        all(k in r and _json_key(r[k]) == v for k, v in items.items()) for r in records
    )


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def sibling_paths(
    captured: Sequence[Mapping[str, Any]],
    *,
    origin: str,
    prefix: str,
    written: str,
) -> list[str]:
    """Collections under ``prefix`` the app itself read (path only).

    Only ``<prefix>/<name>`` on the app's own origin, from a successful
    ``GET`` the app made, other than the written collection's spellings. The query string is
    dropped (it may carry a session value), and a segment that looks like a
    record id or holds a session marker is skipped.
    """

    skip = {s.lower() for s in _spellings(written)}
    out: list[str] = []
    for line in captured:
        if str(line.get("method") or "").upper() != "GET" or not rcs._ok(line):
            continue
        url = str(line.get("url") or "")
        if rcs._origin(url) != origin:
            continue
        path = urlsplit(url).path.rstrip("/")
        if not path.startswith(prefix + "/"):
            continue
        name = path[len(prefix) + 1 :]
        if not name or "/" in name or name.lower() in skip:
            continue
        if rcs._RECORD_SEGMENT.search(name) or rcs._SESSION_VALUE.search(name):
            continue
        out.append(f"{prefix}/{name}")
    return list(dict.fromkeys(out))


def cover(
    params: Mapping[str, str],
    search: rcs.ReadSearch,
    promoted: rcs.Surface,
    *,
    captured: Sequence[Mapping[str, Any]],
    probe_base: Optional[str] = None,
    get: Any = None,
) -> Optional[rcs.Surface]:
    """Replace ``promoted`` with a read that covers the whole save, if one is found.

    Returns the new saved-record surface, or ``None`` with
    ``search.read_discovery["covering_read"]`` saying why nothing covered it.
    Same rules as :func:`discover`: ``GET`` only, the local machine only, no
    credentials, no redirects, every probe recorded.
    """

    get = get or http_get
    found = search.read_discovery
    if found is None:
        return None
    probes: list[dict[str, Any]] = found.setdefault("probes", [])
    covering: dict[str, Any] = {"status": "not found"}
    found["covering_read"] = covering
    lookup, _ = rcs.param_lookup(params)
    path = str(promoted.config.get("records_path") or "")

    def none(reason: str) -> None:
        covering["reason"] = reason

    if "{" in path or "?" in path or "/" not in path:
        none(f"the probed read {path} is not a plain collection path")
        return None
    prefix, written = path.rsplit("/", 1)
    writes = list(search.write_requests)
    outside = [
        line
        for line in writes
        if not _under(urlsplit(str(line.get("url") or "")).path, prefix or "/")
    ]
    if outside:
        none(
            "a captured write went outside "
            + (prefix or "/")
            + ", so no read under it can see the whole save"
        )
        return None
    origins = {rcs._origin(str(line.get("url") or "")) for line in writes}
    base = str(promoted.config.get("base_url") or "").rstrip("/")
    if probe_base is not None:
        base = probe_base.rstrip("/")
    if not is_local_origin(base):
        none(f"{urlsplit(base).hostname} is not the local machine")
        return None
    sent = _sent_params(writes[0], params) if writes else {}
    for line in writes[1:]:
        sent.update(_sent_params(line, params))

    budget = [MAX_COVER_PROBES]

    def probe(cand_path: str, kind: str) -> tuple[dict[str, Any], Any, str]:
        url = base + cand_path
        safe = rcs._templated(url, lookup).safe
        row: dict[str, Any] = {
            "method": "GET",
            "path": safe,
            "kind": kind,
            "purpose": "a read that covers the whole save",
        }
        probes.append(row)
        budget[0] -= 1
        status, text, truncated = get(url)
        row["status"] = status
        if status is None or not 200 <= status < 300 or text is None:
            row["outcome"] = "no read here"
            return row, None, url
        fake = {
            "method": "GET",
            "url": url,
            "status": status,
            "response_text": text,
            "response_truncated": truncated,
            "t": None,
        }
        body = rcs._json_body(fake)
        if body is None:
            row["outcome"] = "not JSON (or cut)"
            return row, None, url
        return row, fake, url

    # 1. The collections the app read beside the written one.
    seen: dict[str, list[dict[str, Any]]] = {path: [dict(r) for r in promoted.after]}
    siblings: list[str] = []
    for origin in sorted(origins):
        siblings += sibling_paths(
            captured, origin=origin, prefix=prefix, written=written
        )
    siblings = list(dict.fromkeys(siblings))
    unread: list[str] = []
    for sib in siblings:
        if budget[0] <= 0:
            unread.append(sib)
            continue
        row, fake, _ = probe(sib, "sibling")
        if fake is None:
            unread.append(sib)
            continue
        records = [
            dict(r)
            for listing in rcs._listings(rcs._json_body(fake))
            for r in listing.resources
        ]
        seen[sib] = records
        row["outcome"] = f"read: {len(records)} record(s); the root must hold them all"
    if unread:
        none(
            "the app read "
            + ", ".join(unread)
            + " beside the written collection, but a probe could not read it"
        )
        return None

    # 2. The prefix root: one read of every collection under the prefix.
    roots = list(dict.fromkeys([prefix or "/", (prefix or "") + "/"]))
    why: list[str] = []
    for root in roots:
        if budget[0] <= 0:
            why.append(f"GET {root}: not probed (probe limit)")
            break
        row, fake, url = probe(root, "root")
        if fake is None:
            why.append(f"GET {row['path']}: {row['outcome']} (status {row['status']})")
            continue
        body = rcs._json_body(fake)
        listings = rcs._listings(body)
        if not listings:
            row["outcome"] = "holds no record list"
            why.append(f"GET {row['path']}: holds no record list")
            continue
        taken = set(search.taken) - {promoted.name, promoted.content_name}
        target: Optional[rcs.Surface] = None
        target_listing = None
        for listing in listings:
            surface, _why = rcs._surface_from_listing(
                fake,
                listing,
                lookup,
                before_line=None,
                taken=set(taken),
                excluded_values=params.values(),
            )
            if surface is None or not set(sent) <= set(surface.bound.values()):
                continue
            if target is None or len(surface.bound) > len(target.bound):
                target, target_listing = surface, listing
        if target is None or target_listing is None:
            row["outcome"] = "no record holds every written value"
            why.append(f"GET {row['path']}: does not hold the written record")
            continue
        everything = [dict(r) for listing in listings for r in listing.resources]
        missing = [
            name
            for name, records in seen.items()
            if not all(_holds(everything, r) for r in records)
        ]
        if missing:
            row["outcome"] = "does not hold every record of " + ", ".join(missing)
            why.append(
                f"GET {row['path']}: does not hold every record of "
                + ", ".join(missing)
                + ", so it is not the whole store"
            )
            continue
        if not target.observed_complete:
            row["outcome"] = "not one whole response: " + "; ".join(target.incomplete)
            why.append(f"GET {row['path']}: {row['outcome']}")
            continue
        # Promote: the root replaces the collection; every list it returns
        # is read (the saved record's may gain one row, the others none).
        safe = row["path"]
        taken.update({target.name, target.content_name})
        target.source = f"probe GET {safe} (read-only, after the save)"
        target.store_wide = True
        guarded: list[rcs.Surface] = []
        for listing in listings:
            if listing is target_listing:
                continue
            if listing.kind != "rest":
                continue
            records = [dict(r) for r in listing.resources]
            config: dict[str, Any] = {
                "kind": "rest",
                "base_url": target.config["base_url"],
                "records_path": target.config["records_path"],
                "records_key": listing.records_key,
            }
            guarded.append(
                rcs.Surface(
                    name=rcs._unique_name(
                        f"{target.name}_{listing.records_key or 'records'}", taken
                    ),
                    config=config,
                    after=records,
                    before=[dict(r) for r in records],
                    before_recorded=False,
                    source=f"probe GET {safe} (read-only, after the save), "
                    f"list {listing.records_key!r}",
                    observed_complete=target.observed_complete,
                    incomplete=[],
                )
            )
        search.surfaces = [s for s in search.surfaces if s is not promoted]
        search.surfaces += [target] + guarded
        search.taken = taken
        search.closed_world = True
        search.closed_world_basis = "probed"
        row["outcome"] = (
            "confirmed: holds the written record and every record of " + ", ".join(seen)
        )
        row["confirmed_fields"] = sorted(target.bound)
        covering.update(
            status="promoted",
            read=safe,
            lists=[listing.records_key for listing in listings],
            basis=(
                f"GET {safe} is the root of every captured write; it holds the "
                "written record and every record of "
                + ", ".join(seen)
                + " in one unpaged response"
            ),
        )
        found.update(
            read=safe,
            surface=target.name,
            collection_read=found.get("read"),
            needs_operator_confirmation=True,
        )
        search.notes.append(
            f"read discovery: GET {safe} covers the whole save (it holds every "
            f"record of {', '.join(seen)}); the check reads it instead of "
            f"{path}. A person confirms it is the whole store"
        )
        return target
    none(
        "; ".join(why)
        + (
            "; the app read no other collection under " + (prefix or "/")
            if not siblings
            else ""
        )
    )
    return None
