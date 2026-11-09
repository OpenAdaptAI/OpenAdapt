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

Every probe (URL with record ids masked, status, outcome) is recorded in
``search.read_discovery`` and from there in the proof.
"""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow import record_check_synthesis as rcs

SCHEMA = "openadapt.read-discovery/v1"
#: Largest probe response read; a longer body is cut and can't confirm.
MAX_PROBE_BYTES = 2 * 1024 * 1024
PROBE_TIMEOUT_S = 5.0
#: Most probes for one save.
MAX_PROBES = 12

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
        raise ProbeRefused(f"probe refused: {urlsplit(url).netloc} is not local")
    opener = urllib.request.build_opener(_NoRedirect)
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
            f"the probe base {urlsplit(probe_base).netloc} is not the local "
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
                f"the write went to {urlsplit(base).netloc}, not the local "
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
