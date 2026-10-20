"""Build a record check from the reads the app made, and fault-test it.

``openadapt-flow scaffold-verifier`` used to write a draft that checked nothing
until a person filled it in. This module fills it in from evidence the
recording already holds, then tries to break it before anyone reviews it.

1. **Find the read.** After the save, the app reads the saved record back to
   show it: a REST, FHIR or XHR ``GET``. Those reads are in the recording's
   network capture (``network.jsonl``, the ``openadapt.network-capture/1``
   format ``record --capture-network`` writes). A database read path
   (``--db``) or an existing verifier section (``--verifier``) works too. The
   read chosen is the one that returns a record holding the recorded
   parameter values.
2. **Fill in the check.** The check reads that path, expects exactly one new
   record with the recorded values (bound to run parameters, so each run is
   checked against its own values), and must not lose or change a record that
   was there before. With a database read path, every other table gets a
   guard: no row added, changed or removed. Write targets nothing covers, and
   tables that could not be read, are listed and keep the check a draft.
3. **Prove it.** The check is judged against the fault classes of
   ``benchmark/effect_e2e`` applied to the recorded records (nothing saved, a
   field dropped, saved twice, saved on the wrong record, another record of
   the same subject destroyed, a stray write elsewhere), plus another record
   changed or deleted. Every generated read is built, and a database read is
   run, so a read that can't work is caught too. On a fixture (MockMed, the
   effect_e2e record service) the check also runs end to end through the
   replayer (:mod:`openadapt_flow.record_check_proof`). A check that misses
   any fault, or stops the clean save, is written as a draft with the misses
   listed.
4. **A person approves.** Every effect carries
   ``needs_operator_confirmation: true``; ``verify-run`` and ``qualify``
   refuse the file until a person removes those lines.

Reads whose records carry an ``id`` get a second, content read
(``<name>.content``, ``records_drop_fields: [id]``): the id read counts new
records, so a stray row identical to an existing one still counts; the content
read sees a record whose id stayed the same but whose fields changed.

No model calls, nothing sent anywhere. The only network read is the optional
``--verifier`` read of the system of record the operator named.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
    Verdict,
    stable_id,
)
from openadapt_flow.runtime.effects.surfaces import surface_probe

#: The network capture ``record --capture-network`` writes. Read here without
#: importing the recorder side, so the format has one owner
#: (``docs/NETWORK_CAPTURE.md``).
NETWORK_FILE = "network.jsonl"
NETWORK_SCHEMA = "openadapt.network-capture/1"

PROPOSED_NAME = "record_check.proposed.yaml"
PROOF_NAME = "record_check.proof.json"
PROOF_SCHEMA = "openadapt.record-check-proof/v1"

#: Largest table ``--db`` scans for the saved row.
MAX_SCAN_ROWS = 20000

#: The content twin of a read whose records carry an ``id``.
CONTENT_SUFFIX = ".content"
#: A selector no record matches: with ``forbid_collateral_loss`` it makes
#: every record that was there before a record that must still be there.
_NO_RECORD = {"_openadapt_no_such_field": ValueExpr(literal="-")}

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LIST_KEYS = ("records", "data", "items", "results", "entries", "rows")
_SESSION_VALUE = re.compile(r"<session:[^>]*>|<secret>")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: A URL path segment that looks like it names one record (an MRN, a numeric
#: id, a UUID), as opposed to a resource or version name (``api``, ``R4``).
_RECORD_SEGMENT = re.compile(r"\d{3,}|^[0-9a-fA-F]{8,}$|^[0-9a-fA-F-]{16,}$")


class SynthesisError(ValueError):
    """The inputs hold nothing a record check can be built from."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_network(recording_dir: Path) -> list[dict[str, Any]]:
    """The capture's request lines; an absent file is an empty capture."""

    path = Path(recording_dir) / NETWORK_FILE
    if not _sealed.is_file(path):
        return []
    try:
        rows = [
            json.loads(line)
            for line in _sealed.read_text(path).splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        raise SynthesisError(f"{path} could not be read: {exc}") from exc
    if (
        not rows
        or not isinstance(rows[0], dict)
        or rows[0].get("schema") != NETWORK_SCHEMA
    ):
        raise SynthesisError(f"{path} is not a {NETWORK_SCHEMA} capture")
    return [row for row in rows[1:] if isinstance(row, dict)]


def recorded_params(recording_dir: Path) -> dict[str, str]:
    """The demonstration's parameter values from ``meta.json``."""

    try:
        meta = json.loads(_sealed.read_text(Path(recording_dir) / "meta.json"))
    except _sealed.SealedSetError:
        raise
    except (OSError, ValueError):
        return {}
    params = meta.get("params") if isinstance(meta, dict) else None
    if not isinstance(params, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in params.items()
        if value is not None and str(value).strip()
    }


def param_lookup(params: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Recorded value -> parameter name; values two parameters share are skipped."""

    owners: dict[str, list[str]] = {}
    for name, value in params.items():
        owners.setdefault(str(value), []).append(name)
    lookup = {value: names[0] for value, names in owners.items() if len(names) == 1}
    notes = [
        f"parameters {', '.join(sorted(names))} share one value, so neither is checked"
        for names in owners.values()
        if len(names) > 1
    ]
    return lookup, notes


def _identity_like(value: str) -> bool:
    """A short single-token value (an id, a name), not free text."""

    return bool(value) and len(value) <= 64 and not any(ch.isspace() for ch in value)


def _t(line: Mapping[str, Any]) -> float:
    value = line.get("t")
    return float(value) if isinstance(value, (int, float)) else 0.0


# ---------------------------------------------------------------------------
# The check being built
# ---------------------------------------------------------------------------


@dataclass
class Surface:
    """One read the check makes, and what it saw while recording."""

    name: str
    #: The read's ``effects`` settings (a ``deployment.yaml`` ``effects:``).
    config: dict[str, Any]
    #: Records the read returns after the save, in the shape the check sees.
    after: list[dict[str, Any]]
    #: Records before the save; derived (after minus the saved record) when
    #: no read before the save was recorded.
    before: list[dict[str, Any]]
    before_recorded: bool
    #: Where the read came from, for the person reviewing it (no values).
    source: str
    #: The record the save wrote, when this surface holds it.
    target: Optional[dict[str, Any]] = None
    #: Record field -> parameter name, for fields equal to a recorded value.
    bound: dict[str, str] = field(default_factory=dict)
    #: Record field -> value the save sent that is not a parameter.
    constants: dict[str, str] = field(default_factory=dict)
    #: Parameters the read itself is scoped by (a URL path or query value, a
    #: SQL ``WHERE``): a record saved under another value is not returned.
    scope: list[str] = field(default_factory=list)

    @property
    def is_target(self) -> bool:
        return self.target is not None

    @property
    def has_ids(self) -> bool:
        return any("id" in record for record in self.after + self.before)

    @property
    def content_name(self) -> str:
        return self.name + CONTENT_SUFFIX


@dataclass
class ReadSearch:
    """What the search for the read found, including what it rejected."""

    surfaces: list[Surface] = field(default_factory=list)
    write_requests: list[dict[str, Any]] = field(default_factory=list)
    save_t: Optional[float] = None
    notes: list[str] = field(default_factory=list)
    #: Write targets and tables the check does not read (plain sentences).
    #: Any entry keeps the check a draft.
    uncovered: list[str] = field(default_factory=list)
    #: Reads that would not work as generated. Any entry keeps it a draft.
    problems: list[str] = field(default_factory=list)
    #: True when every table the save could touch is read (a database path),
    #: or a person declared the read complete (``--complete-read``).
    closed_world: bool = False
    #: ``"database"`` (every table read) or ``"declared"`` (a person said so).
    closed_world_basis: Optional[str] = None
    #: Surface names in use, across every source.
    taken: set[str] = field(default_factory=set)


# -- network capture ---------------------------------------------------------


def _json_body(line: Mapping[str, Any]) -> Any:
    text = line.get("response_text")
    if not isinstance(text, str) or not text.strip():
        return None
    if line.get("response_truncated"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _flat(node: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted scalar paths of a JSON node; a list contributes its first item."""

    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.update(_flat(value, path))
            elif value is not None:
                out[path] = value
    elif isinstance(node, list) and node:
        out.update(_flat(node[0], prefix))
    return out


@dataclass
class _Listing:
    kind: str  # "rest" | "fhir"
    records_key: Optional[str]
    resources: list[dict[str, Any]]
    resource_type: Optional[str] = None


def _listings(body: Any) -> list[_Listing]:
    """The record lists a JSON response holds, in a shape a check can read."""

    if isinstance(body, dict) and body.get("resourceType") == "Bundle":
        resources: list[dict[str, Any]] = [
            entry["resource"]
            for entry in body.get("entry") or []
            if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
        ]
        types = {res.get("resourceType") for res in resources}
        resource_type = next(iter(types)) if len(types) == 1 else None
        return [_Listing("fhir", None, resources, resource_type)]
    if isinstance(body, list):
        records = [item for item in body if isinstance(item, dict)]
        return [_Listing("rest", None, records)] if records else []
    if isinstance(body, dict):
        found = []
        keys = sorted(
            body,
            key=lambda k: (
                k not in _LIST_KEYS,
                _LIST_KEYS.index(k) if k in _LIST_KEYS else 0,
            ),
        )
        for key in keys:
            value = body[key]
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                found.append(
                    _Listing(
                        "rest", str(key), [v for v in value if isinstance(v, dict)]
                    )
                )
        return found
    return []


def _bind_fields(
    flat: Mapping[str, Any], lookup: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """``(exact, reference)``: fields equal to a recorded value, and FHIR
    references (``Patient/<value>``) that end in one."""

    exact: dict[str, str] = {}
    reference: dict[str, str] = {}
    for path, value in flat.items():
        if isinstance(value, bool):
            continue
        text = str(value).strip()
        if text in lookup:
            exact[path] = lookup[text]
        elif "/" in text and text.rsplit("/", 1)[1] in lookup:
            reference[path] = lookup[text.rsplit("/", 1)[1]]
    return exact, reference


def _unique_name(raw: str, taken: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", raw.replace(".", "_")).strip("_") or "records"
    base, n = name, 2
    while name in taken or name + CONTENT_SUFFIX in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    taken.add(name + CONTENT_SUFFIX)
    return name


def _field_name(path: str, taken: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", path.replace(".", "_")) or "field"
    base, n = name, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class _Templated:
    #: The path with recorded values as ``{param}`` placeholders, braces in
    #: literal segments doubled only when a placeholder makes it a template.
    path: str
    path_params: list[str]
    literal_query: list[tuple[str, str]]
    bound_query: dict[str, str]
    #: The path for names and messages: placeholders kept, any segment that
    #: looks like a record id replaced by ``*``.
    safe: str
    #: Literal segments that look like one record's id (a frozen value).
    pinned: int


def _templated(url: str, lookup: Mapping[str, str]) -> _Templated:
    parts = urlsplit(url)
    raw = parts.path.split("/")
    bound = [lookup.get(segment) if segment else None for segment in raw]
    templated = any(bound)
    segments: list[str] = []
    safe: list[str] = []
    pinned = 0
    for segment, name in zip(raw, bound):
        if name:
            segments.append("{" + name + "}")
            safe.append("{" + name + "}")
            continue
        segments.append(
            segment.replace("{", "{{").replace("}", "}}") if templated else segment
        )
        if _RECORD_SEGMENT.search(segment):
            pinned += 1
            safe.append("*")
        else:
            safe.append(re.sub(r"[^A-Za-z0-9_.-]", "_", segment))
    literal_query: list[tuple[str, str]] = []
    bound_query: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        name = lookup.get(value)
        if name:
            bound_query[key] = name
        else:
            literal_query.append((key, value))
    return _Templated(
        path="/".join(segments),
        path_params=sorted({n for n in bound if n}),
        literal_query=literal_query,
        bound_query=bound_query,
        safe="/".join(safe),
        pinned=pinned,
    )


def _write_values(line: Mapping[str, Any]) -> dict[str, str]:
    """Field -> value the write sent (form or JSON body)."""

    values: dict[str, str] = {}
    for pair in line.get("form") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            values[str(pair[0])] = str(pair[1])
    body = line.get("body")
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key, value in _flat(parsed).items():
                values[key] = str(value)
    for key, value in parse_qsl(urlsplit(str(line.get("url") or "")).query):
        values.setdefault(key, value)
    return values


def _save_event_times(recording_dir: Path) -> tuple[Optional[float], Optional[float]]:
    """``(save, previous)``: the last write-shaped step's ``t`` and the ``t``
    of the event before it."""

    from openadapt_flow.scaffold_verifier import candidates_from_recording

    times: dict[int, float] = {}
    events_path = Path(recording_dir) / "events.jsonl"
    if not _sealed.is_file(events_path):
        return None, None
    for index, line in enumerate(_sealed.read_text(events_path).splitlines()):
        try:
            event = json.loads(line) if line.strip() else {}
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("t"), (int, float)):
            times[index] = float(event["t"])
    try:
        candidates = candidates_from_recording(Path(recording_dir))
    except SystemExit:
        candidates = []
    indices = [
        int(c.step_id.split("_")[1])
        for c in candidates
        if int(c.step_id.split("_")[1]) in times
    ]
    if not indices:
        return None, None
    save = max(indices)
    earlier = [times[i] for i in times if i < save]
    return times[save], (max(earlier) if earlier else None)


def _accepted(line: Mapping[str, Any]) -> bool:
    """A write the server accepted: 2xx, or a redirect after a form post."""

    status = line.get("status")
    return isinstance(status, int) and 200 <= status < 400 and not line.get("failed")


def _ok(line: Mapping[str, Any]) -> bool:
    status = line.get("status")
    return isinstance(status, int) and 200 <= status < 300 and not line.get("failed")


def save_window(
    recording_dir: Path, network: list[dict[str, Any]]
) -> tuple[Optional[float], Optional[float], list[dict[str, Any]]]:
    """``(first, last, writes)``: the save's accepted write requests.

    An event's ``t`` is stamped after the screen settles, so the save's own
    requests come BEFORE the Save step's ``t``: the window runs from the
    previous event to the Save step. A write stamped after the Save step (a
    slow settle on the recorder's side) is taken when the window is empty.
    With no write request, the Save step's ``t`` is the only time there is.
    """

    event_t, previous_t = _save_event_times(recording_dir)
    writes = sorted(
        (
            line
            for line in network
            if str(line.get("method") or "").upper() in _WRITE_METHODS
            and _accepted(line)
        ),
        key=_t,
    )
    if not writes:
        return event_t, event_t, []
    low = previous_t if previous_t is not None else float("-inf")
    high = event_t if event_t is not None else float("inf")
    chosen = [w for w in writes if low < _t(w) <= high]
    if not chosen:
        later = [w for w in writes if _t(w) > low]
        chosen = later[:1] if later else writes[-1:]
    return _t(chosen[0]), _t(chosen[-1]), chosen


def _surface_from_listing(
    line: Mapping[str, Any],
    listing: _Listing,
    lookup: Mapping[str, str],
    *,
    before_line: Optional[Mapping[str, Any]],
    taken: set[str],
) -> tuple[Optional[Surface], Optional[str]]:
    """A surface for one listing that holds the saved record, or why not."""

    url = str(line.get("url") or "")
    tpl = _templated(url, lookup)
    if _SESSION_VALUE.search(url):
        return None, f"GET {tpl.safe}: the URL carries a session token"

    best: Optional[tuple[dict[str, Any], dict[str, str], dict[str, str]]] = None
    for resource in listing.resources:
        exact, reference = _bind_fields(_flat(resource), lookup)
        if not exact:
            continue
        if best is None or len(exact) > len(best[1]):
            best = (resource, exact, reference)
    if best is None:
        return None, None
    resource, exact, reference = best
    origin = _origin(url)
    source = f"GET {tpl.safe} (t={line.get('t')})"

    if listing.kind == "fhir":
        resource_type = listing.resource_type or str(resource.get("resourceType") or "")
        marker = "/" + resource_type
        if not resource_type or marker not in tpl.path:
            return None, f"GET {tpl.safe}: FHIR resource type not in the URL"
        base = origin + tpl.path[: tpl.path.rindex(marker)]
        if tpl.path_params:
            return None, (
                f"GET {tpl.safe}: the FHIR base path holds a recorded value; "
                "the FHIR check takes a fixed base and search parameters"
            )
        field_paths = {"id": "id"}
        names: set[str] = {"id"}
        bound: dict[str, str] = {}
        for dotted, param in sorted(exact.items()):
            if dotted == "id":
                continue
            name = _field_name(dotted, names)
            field_paths[name] = dotted
            bound[name] = param
        config: dict[str, Any] = {
            "kind": "fhir",
            "base_url": base,
            "resource_type": resource_type,
            "field_paths": field_paths,
        }
        if tpl.literal_query:
            config["search_params"] = dict(tpl.literal_query)
        if tpl.bound_query:
            config["search_param_exprs"] = {
                key: {"param": name} for key, name in tpl.bound_query.items()
            }
        from openadapt_flow.runtime.effects.fhir import extract_path

        def shape(res: dict[str, Any]) -> dict[str, Any]:
            return {key: extract_path(res, p) for key, p in field_paths.items()}

        after = [shape(res) for res in listing.resources]
        target = shape(resource)
        before_records, recorded = _before_records(
            before_line, listing_kind="fhir", records_key=None, shape=shape
        )
        surface = Surface(
            name=_unique_name(f"fhir_{resource_type}", taken),
            config=config,
            after=after,
            before=before_records if recorded else _minus(after, target),
            before_recorded=recorded,
            source=source,
            target=target,
            bound=bound,
            scope=sorted(set(tpl.bound_query.values())),
        )
        unscoped = sorted(
            set(reference.values())
            - set(tpl.bound_query.values())
            - set(bound.values())
        )
        if unscoped:
            surface.config["_unscoped_references"] = unscoped
        if tpl.pinned:
            surface.config["_pinned"] = tpl.pinned
        return surface, None

    bound = {path_: param for path_, param in exact.items() if "." not in path_}
    if not bound:
        return None, (
            f"GET {tpl.safe}: the recorded values sit in nested fields; "
            "the REST check reads top-level fields only"
        )
    records_path = tpl.path
    placeholders = set(tpl.path_params)
    if tpl.literal_query or tpl.bound_query:
        query = [
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in tpl.literal_query
        ]
        query += [
            f"{quote(k, safe='')}={{{name}}}" for k, name in tpl.bound_query.items()
        ]
        if tpl.bound_query and not tpl.path_params:
            # The query makes the path a template: double literal braces.
            records_path = records_path.replace("{", "{{").replace("}", "}}")
        records_path += "?" + "&".join(query)
        placeholders.update(tpl.bound_query.values())
    config = {
        "kind": "rest",
        "base_url": origin,
        "records_path": records_path,
        "records_key": listing.records_key,
    }
    if placeholders:
        config["path_params"] = {key: {"param": key} for key in sorted(placeholders)}
    if tpl.pinned:
        config["_pinned"] = tpl.pinned
    after = [dict(r) for r in listing.resources]
    target = dict(resource)
    before_records, recorded = _before_records(
        before_line, listing_kind="rest", records_key=listing.records_key, shape=dict
    )
    surface = Surface(
        name=_unique_name(tpl.safe.replace("*", "x").strip("/") or "records", taken),
        config=config,
        after=after,
        before=before_records if recorded else _minus(after, target),
        before_recorded=recorded,
        source=source,
        target=target,
        bound=bound,
        scope=sorted(placeholders),
    )
    return surface, None


def _before_records(
    line: Optional[Mapping[str, Any]],
    *,
    listing_kind: str,
    records_key: Optional[str],
    shape: Any,
) -> tuple[list[dict[str, Any]], bool]:
    if line is None:
        return [], False
    body = _json_body(line)
    records: Any = None
    if listing_kind == "fhir":
        if isinstance(body, dict) and body.get("resourceType") == "Bundle":
            records = [
                entry["resource"]
                for entry in body.get("entry") or []
                if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
            ]
    elif records_key is None:
        records = body if isinstance(body, list) else None
    elif isinstance(body, dict):
        records = body.get(records_key)
    if not isinstance(records, list):
        return [], False
    return [shape(r) for r in records if isinstance(r, dict)], True


def _minus(
    records: list[dict[str, Any]], target: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """``records`` without one copy of ``target``."""

    key = stable_id(dict(target))
    out: list[dict[str, Any]] = []
    removed = False
    for record in records:
        if not removed and stable_id(dict(record)) == key:
            removed = True
            continue
        out.append(dict(record))
    return out


def reads_from_network(
    recording_dir: Path, params: Mapping[str, str], search: ReadSearch
) -> None:
    """Add the GET that returns the saved record to ``search``."""

    network = load_network(recording_dir)
    if not network:
        return
    lookup, _ = param_lookup(params)
    first, last, writes = save_window(recording_dir, network)
    search.save_t = last
    search.write_requests = writes
    if not writes:
        search.notes.append(
            "the capture holds no accepted write request; reads are taken after "
            "the Save step's time"
        )
    gets = [
        line
        for line in network
        if str(line.get("method") or "").upper() == "GET" and _ok(line)
    ]
    after_gets = [line for line in gets if last is None or _t(line) > last]
    candidates: list[tuple[tuple[Any, ...], Surface]] = []
    rejected: list[str] = []
    seen_urls: set[str] = set()
    names: set[str] = set(search.taken)
    for line in after_gets:
        url = str(line.get("url") or "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        before_line = None
        if first is not None:
            for earlier in gets:
                if earlier.get("url") == url and _t(earlier) < first:
                    before_line = earlier
        for listing in _listings(_json_body(line)):
            surface, why = _surface_from_listing(
                line, listing, lookup, before_line=before_line, taken=names
            )
            if why:
                rejected.append(why)
            if surface is None:
                continue
            rank = (
                len(surface.bound),
                "id" in (surface.target or {}),
                surface.before_recorded,
                -_t(line),
            )
            candidates.append((rank, surface))
    if not candidates:
        if after_gets:
            search.notes.append(
                f"{len(after_gets)} read(s) after the save, none returning a "
                "record with the recorded values"
            )
        search.notes.extend(rejected)
        return
    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = candidates[0][1]
    search.taken.update({chosen.name, chosen.content_name})
    search.surfaces.append(chosen)
    for _rank, other in candidates[1:]:
        search.notes.append(
            f"also returns the saved record: {other.source} (not used; the check "
            f"reads {chosen.source})"
        )
    read_back = {str((chosen.target or {}).get(fld)) for fld in chosen.bound}
    for line in writes:
        sent = set(_write_values(line).values())
        if not sent & read_back:
            tpl = _templated(str(line.get("url") or ""), lookup)
            search.uncovered.append(
                f"{line.get('method')} {tpl.safe}: no read returns what this "
                "request sent"
            )


# -- database read path --------------------------------------------------------


def reads_from_sqlite(
    db_path: Path, params: Mapping[str, str], search: ReadSearch
) -> None:
    """Every table: the ones holding the saved row are read, the rest guarded.

    A table that can't be read (an irregular name or column, too many rows)
    is listed in ``uncovered`` and the read is not complete.
    """

    lookup, _ = param_lookup(params)
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SynthesisError(f"cannot open {db_path} read-only: {exc}") from exc
    complete = True
    try:
        conn.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            if not _PLAIN_NAME.match(table):
                search.uncovered.append(
                    "a table with an irregular name: the check can't read it"
                )
                complete = False
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count > MAX_SCAN_ROWS:
                search.uncovered.append(
                    f"table {table}: {count} rows, too many to scan for the saved row"
                )
                complete = False
                continue
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            if not all(_PLAIN_NAME.match(str(c)) for c in columns):
                search.uncovered.append(
                    f"table {table}: a column with an irregular name; the check "
                    "can't read it"
                )
                complete = False
                continue
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
            best: Optional[tuple[dict[str, Any], dict[str, str]]] = None
            for row in rows:
                exact, _ = _bind_fields(
                    {k: v for k, v in row.items() if k != "id"}, lookup
                )
                if exact and (best is None or len(exact) > len(best[1])):
                    best = (row, exact)
            query = f'SELECT * FROM "{table}"'
            config: dict[str, Any] = {
                "kind": "sql",
                "sqlite_database": str(db_path),
                "sql_query": query,
            }
            name = _unique_name(table, search.taken)
            if best is None:
                search.surfaces.append(
                    Surface(
                        name=name,
                        config=config,
                        after=rows,
                        before=list(rows),
                        before_recorded=False,
                        source=f"table {table}",
                    )
                )
                continue
            row, bound = best
            scope = {
                column: param
                for column, param in bound.items()
                if _identity_like(params.get(param, ""))
            }
            if scope:
                slots = {
                    param: f"p{i}"
                    for i, param in enumerate(sorted(set(scope.values())))
                }
                where = " AND ".join(
                    f'"{column}" = :{slots[param]}'
                    for column, param in sorted(scope.items())
                )
                config["sql_query"] = f"{query} WHERE {where}"
                config["sql_query_params"] = {
                    slot: {"param": param} for param, slot in sorted(slots.items())
                }
                visible = [
                    r
                    for r in rows
                    if all(str(r.get(c)) == params[p] for c, p in scope.items())
                ]
            else:
                visible = rows
            search.surfaces.append(
                Surface(
                    name=name,
                    config=config,
                    after=visible,
                    before=_minus(visible, row),
                    before_recorded=False,
                    source=f"table {table}",
                    target=row,
                    bound=bound,
                    scope=sorted(set(scope.values())),
                )
            )
    finally:
        conn.close()
    if complete:
        search.closed_world = True
        search.closed_world_basis = "database"


# -- an existing verifier section ----------------------------------------------


def reads_from_verifier(
    section: Mapping[str, Any],
    params: Mapping[str, str],
    search: ReadSearch,
    *,
    snapshots: Optional[Mapping[str, Any]] = None,
) -> None:
    """Read once through a reviewed verifier section and find the saved record.

    ``snapshots`` is ``sor_snapshots.json`` from ``record --verify`` when the
    recording has one; its ``before`` read becomes the baseline.
    """

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

    cfg = EffectsConfig.model_validate(dict(section))
    verifier = build_effect_verifier(cfg, params)
    if verifier is None:
        raise SynthesisError("the verifier section builds no verifier")
    state = verifier.capture_pre_state()
    if not state.reachable:
        raise SynthesisError("the verifier could not read the system of record")
    lookup, _ = param_lookup(params)
    best: Optional[tuple[dict[str, Any], dict[str, str]]] = None
    for record in state.records:
        exact, _ = _bind_fields(
            {
                k: v
                for k, v in record.items()
                if k != "id" and not isinstance(v, (dict, list))
            },
            lookup,
        )
        if exact and (best is None or len(exact) > len(best[1])):
            best = (record, exact)
    if best is None:
        search.notes.append(
            f"the {cfg.kind} read returns {len(state.records)} record(s), none "
            "holding the recorded values"
        )
        return
    before: list[dict[str, Any]] = []
    recorded = False
    if snapshots and isinstance(snapshots.get("before"), dict):
        prior = snapshots["before"].get("records")
        if isinstance(prior, list):
            before = [r for r in prior if isinstance(r, dict)]
            recorded = True
    record, bound = best
    search.surfaces.append(
        Surface(
            name=_unique_name(f"{cfg.kind or 'records'}_read", search.taken),
            config=dict(section),
            after=list(state.records),
            before=before if recorded else _minus(list(state.records), record),
            before_recorded=recorded,
            source=f"the {cfg.kind} verifier section",
            target=record,
            bound=bound,
        )
    )


def _apply_sent_constants(search: ReadSearch, params: Mapping[str, str]) -> None:
    """Match also on fields the save sent with a fixed (non-parameter) value,
    under the same field name: they tell the saved record apart from other
    records of the same subject."""

    lookup, _ = param_lookup(params)
    sent = [_write_values(line) for line in search.write_requests]
    for surface in search.surfaces:
        if not surface.is_target:
            continue
        for fld, value in (surface.target or {}).items():
            if fld in surface.bound or fld == "id" or value is None:
                continue
            if str(value) in lookup or not str(value).strip():
                continue
            if any(str(values.get(fld)) == str(value) for values in sent):
                surface.constants[fld] = str(value)


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


def _effect(
    surface_name: str,
    note: str,
    *,
    match: Mapping[str, ValueExpr],
    expected_count: int,
    forbid_collateral_loss: bool,
    confirmed: bool,
) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=dict(match),
        expected_count=expected_count,
        count_new_only=True,
        forbid_collateral_loss=forbid_collateral_loss,
        risk="irreversible",
        probe=surface_probe(surface_name, note),
        needs_operator_confirmation=not confirmed,
    )


def effects_for(
    surfaces: Iterable[Surface], *, confirmed: bool = False
) -> list[Effect]:
    """The effect contract for a set of surfaces (each surface in turn)."""

    effects: list[Effect] = []
    for surface in surfaces:
        if surface.is_target:
            match = {
                fld: ValueExpr(param=p) for fld, p in sorted(surface.bound.items())
            }
            match.update(
                {
                    fld: ValueExpr(literal=v)
                    for fld, v in sorted(surface.constants.items())
                }
            )
            effects.append(
                _effect(
                    surface.name,
                    "exactly one new record with the run's values; no record "
                    "that was there before may disappear",
                    match=match,
                    expected_count=1,
                    forbid_collateral_loss=True,
                    confirmed=confirmed,
                )
            )
        else:
            effects.append(
                _effect(
                    surface.name,
                    "the save adds no row here",
                    match={},
                    expected_count=0,
                    forbid_collateral_loss=False,
                    confirmed=confirmed,
                )
            )
            if not surface.has_ids:
                effects.append(
                    _effect(
                        surface.name,
                        "no row here changes or disappears",
                        match=_NO_RECORD,
                        expected_count=0,
                        forbid_collateral_loss=True,
                        confirmed=confirmed,
                    )
                )
        if surface.has_ids:
            effects.append(
                _effect(
                    surface.content_name,
                    "no record that was there before changes or disappears "
                    "(records compared by content, not id)",
                    match=_NO_RECORD,
                    expected_count=0,
                    forbid_collateral_loss=True,
                    confirmed=confirmed,
                )
            )
    return effects


def _clean(config: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in config.items() if not k.startswith("_")}


def verifier_section(surfaces: list[Surface]) -> dict[str, Any]:
    """The ``verifier:`` section: one read per surface, plus content twins."""

    out: dict[str, Any] = {}
    for surface in surfaces:
        names = [surface.name] + ([surface.content_name] if surface.has_ids else [])
        for name in names:
            if name in out:
                raise SynthesisError(f"two reads share the name {name!r}")
        out[surface.name] = _clean(surface.config)
        if surface.has_ids:
            twin = _clean(surface.config)
            twin["records_drop_fields"] = ["id"]
            out[surface.content_name] = twin
    return {"kind": "surfaces", "surfaces": out}


def _without_ids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "id"} for r in records]


def _signature(records: list[dict[str, Any]]) -> Counter:
    return Counter(json.dumps(r, sort_keys=True, default=str) for r in records)


def check_reads(
    surfaces: list[Surface], section: Mapping[str, Any], params: Mapping[str, str]
) -> list[str]:
    """Build every generated read; run each database read and compare it with
    what the scan saw. Returns the problems (no record values)."""

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

    problems: list[str] = []
    configs = section.get("surfaces", {})
    for surface in surfaces:
        if surface.config.get("_pinned"):
            problems.append(
                f"{surface.name}: the read's path names one fixed record; bind it "
                "to a run input or confirm it is not the demonstration's record"
            )
        for name in (surface.name, surface.content_name):
            if name not in configs:
                continue
            try:
                verifier = build_effect_verifier(
                    EffectsConfig.model_validate(configs[name]), params
                )
            except Exception as exc:  # noqa: BLE001 - any build failure is a problem
                problems.append(f"{name}: the generated read does not build ({exc})")
                continue
            if configs[name].get("kind") != "sql" or verifier is None:
                continue
            state = verifier.capture_pre_state()
            expected = surface.after
            if name == surface.content_name:
                expected = _without_ids(surface.after)
            if not state.reachable:
                problems.append(f"{name}: the generated query does not run")
            elif _signature(state.records) != _signature(expected):
                problems.append(
                    f"{name}: the generated query returns {len(state.records)} "
                    f"row(s), the scan saw {len(expected)}"
                )
    return problems


# ---------------------------------------------------------------------------
# Proof against injected bad writes (recorded-state)
# ---------------------------------------------------------------------------


@dataclass
class FaultResult:
    fault: str
    detail: str
    #: True when the save was correct (only the clean case).
    correct: bool
    #: True when the check passed every effect.
    passed: bool
    reasons: list[str] = field(default_factory=list)
    #: True when the fault could not be built (the check is not credited).
    untestable: bool = False

    @property
    def ok(self) -> bool:
        if self.untestable:
            return False
        return self.passed if self.correct else not self.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault": self.fault,
            "detail": self.detail,
            "correct_save": self.correct,
            "check_passed": self.passed,
            "result": (
                "untestable"
                if self.untestable
                else "clean pass"
                if self.correct and self.passed
                else "false stop"
                if self.correct
                else "caught"
                if not self.passed
                else "MISSED"
            ),
            "reasons": self.reasons,
        }


def _fresh_id(records: list[dict[str, Any]]) -> Any:
    ids = [int(r["id"]) for r in records if isinstance(r.get("id"), int)]
    if ids:
        return max(ids) + 1000
    return "fault-" + str(len(records) + 1000)


_State = tuple[list[dict[str, Any]], list[dict[str, Any]]]


def _judge(
    effects: list[Effect],
    surfaces: list[Surface],
    states: Mapping[str, _State],
    params: Mapping[str, str],
) -> tuple[bool, list[str]]:
    from openadapt_flow.runtime.effects.surfaces import surface_of

    views: dict[str, _State] = {}
    for surface in surfaces:
        before, after = states[surface.name]
        views[surface.name] = (before, after)
        if surface.has_ids:
            views[surface.content_name] = (_without_ids(before), _without_ids(after))
    reasons: list[str] = []
    for effect in effects:
        name = surface_of(effect) or ""
        if name not in views:
            reasons.append(f"effect routes to unknown surface {name!r}")
            continue
        before, after = views[name]
        verdict = judge_records(
            effect.resolve(params),
            EffectState(substrate="proof", reachable=True, records=before),
            after,
            substrate="proof",
        )
        if verdict.verdict is not Verdict.CONFIRMED:
            reasons.append(f"{name}: {verdict.reason}")
    return not reasons, reasons


def _changed(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` with one non-id field changed (or one added)."""

    out = dict(record)
    for key, value in out.items():
        if key != "id" and isinstance(value, str):
            out[key] = value + " (changed)"
            return out
    out["_changed"] = "1"
    return out


def prove_against_faults(
    surfaces: list[Surface],
    effects: list[Effect],
    params: Mapping[str, str],
    *,
    closed_world: bool,
) -> list[FaultResult]:
    """Judge the check on the clean save and on each injected bad write.

    The bad writes are the persistence faults of ``benchmark/effect_e2e``,
    built from the records the recording saw, plus another record changed or
    deleted. Transport faults (a rejected write, an expired session, a
    timeout) are handled by the actuator before any check runs, so they are
    not repeated here.
    """

    base: dict[str, _State] = {
        s.name: (list(s.before), list(s.after)) for s in surfaces
    }
    results: list[FaultResult] = []

    def run(
        fault: str, detail: str, states: Mapping[str, _State], *, correct: bool = False
    ) -> None:
        passed, reasons = _judge(effects, surfaces, states, params)
        results.append(FaultResult(fault, detail, correct, passed, reasons))

    def miss(fault: str, detail: str, reason: str) -> None:
        results.append(FaultResult(fault, detail, False, True, [reason]))

    def other_records(
        surface: Surface,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
        """A record that was there before (injected when there was none)."""

        before, after = base[surface.name]
        target_key = stable_id(surface.target) if surface.target else None
        for record in before:
            if stable_id(record) != target_key and record in after:
                return before, record, False
        seed = dict(surface.target or (after[-1] if after else {}))
        for fld in list(surface.bound) + list(surface.constants):
            seed[fld] = f"other {seed.get(fld)}"
        if "id" in seed or surface.has_ids:
            seed["id"] = _fresh_id(before + after)
        else:
            seed["_other"] = "1"
        return before + [seed], seed, True

    run("ok", "the clean save as recorded", base, correct=True)

    for surface in surfaces:
        before, after = base[surface.name]
        if surface.is_target:
            target = surface.target or {}
            without = _minus(after, target)
            run(
                "no_persist",
                f"nothing saved in {surface.name}",
                {**base, surface.name: (before, without)},
            )
            for fld in sorted(surface.bound):
                run(
                    "partial",
                    f"{surface.name}.{fld} dropped",
                    {
                        **base,
                        surface.name: (before, without + [dict(target, **{fld: ""})]),
                    },
                )
            copy = dict(target)
            if "id" in copy:
                copy["id"] = _fresh_id(after)
            run(
                "duplicate",
                f"saved twice in {surface.name}",
                {**base, surface.name: (before, after + [copy])},
            )

            identity = [
                f for f, p in surface.bound.items() if _identity_like(params.get(p, ""))
            ]
            if not identity and not surface.scope:
                miss(
                    "wrong_record",
                    f"{surface.name}: saved on another record",
                    "no recorded value identifies the record, and the read is "
                    "not scoped by one",
                )
            for fld in sorted(identity):
                moved = dict(target, **{fld: f"{target.get(fld)}-other"})
                run(
                    "wrong_record",
                    f"{surface.name}: saved on another {fld}",
                    {**base, surface.name: (before, without + [moved])},
                )
            for param in sorted(
                set(surface.scope) - {surface.bound[f] for f in identity}
            ):
                run(
                    "wrong_record",
                    f"{surface.name}: saved under another {param}, which the read "
                    "is scoped by",
                    {**base, surface.name: (before, without)},
                )

            telling = [f for f in surface.bound if f not in identity]
            telling += sorted(surface.constants)
            if not telling:
                miss(
                    "stale",
                    f"{surface.name}: the save destroyed another record of the same "
                    "subject",
                    "every checked field identifies the subject, so another record "
                    "of the same subject looks like the saved one",
                )
            else:
                concurrent = dict(target)
                if "id" in concurrent:
                    concurrent["id"] = _fresh_id(after + before)
                for fld in telling:
                    concurrent[fld] = f"concurrent {concurrent.get(fld)}"
                run(
                    "stale",
                    f"{surface.name}: the save destroyed another record of the same "
                    "subject",
                    {**base, surface.name: (before + [concurrent], after)},
                )
        else:
            stray: dict[str, Any] = dict(after[-1]) if after else {}
            if "id" in stray or surface.has_ids:
                stray["id"] = _fresh_id(after + before)
            else:
                stray["_stray"] = "1"
            run(
                "collateral_unaudited",
                f"a stray row written to {surface.name}",
                {**base, surface.name: (before, after + [stray])},
            )

        prior, other, injected = other_records(surface)
        current = after + [dict(other)] if injected else list(after)
        at = current.index(other)
        changed = current[:at] + [_changed(other)] + current[at + 1 :]
        run(
            "changed",
            f"{surface.name}: another record changed by the save",
            {**base, surface.name: (prior, changed)},
        )
        run(
            "deleted",
            f"{surface.name}: another record deleted by the save",
            {**base, surface.name: (prior, current[:at] + current[at + 1 :])},
        )

    if not closed_world:
        miss(
            "collateral_unaudited",
            "a stray row written to a table this check does not read",
            "the check reads only "
            + ", ".join(s.name for s in surfaces)
            + "; a write anywhere else passes it (give a database read path with "
            "--db, or more reads)",
        )
    return results


# ---------------------------------------------------------------------------
# One call
# ---------------------------------------------------------------------------


@dataclass
class Proposal:
    """The proposed check and everything the reviewer needs to judge it."""

    params: dict[str, str]
    search: ReadSearch
    effects: list[Effect]
    verifier: dict[str, Any]
    faults: list[FaultResult]
    live: Optional[dict[str, Any]] = None
    identity: Optional[dict[str, Any]] = None

    @property
    def misses(self) -> list[str]:
        out = [
            f"{r.fault}: {r.detail}"
            + (f" ({'; '.join(r.reasons)})" if r.reasons else "")
            for r in self.faults
            if not r.ok
        ]
        out += [f"not covered: {item}" for item in self.search.uncovered]
        out += [f"read: {item}" for item in self.search.problems]
        if self.live is not None:
            out += [f"live {m}" for m in self.live.get("misses", [])]
        return out

    @property
    def working(self) -> bool:
        if not self.effects or not any(s.is_target for s in self.search.surfaces):
            return False
        if self.live is not None and not self.live.get("passed"):
            return False
        return not self.misses

    def proof(self) -> dict[str, Any]:
        targets = [s for s in self.search.surfaces if s.is_target]
        reads = []
        for s in self.search.surfaces:
            reads.append(
                {
                    "surface": s.name,
                    "source": s.source,
                    "role": "saved record" if s.is_target else "no row added",
                    "checked_fields": sorted(s.bound) + sorted(s.constants),
                    "baseline": "recorded" if s.before_recorded else "derived",
                }
            )
            if s.has_ids:
                reads.append(
                    {
                        "surface": s.content_name,
                        "source": s.source + ", compared by content",
                        "role": "no record changed or removed",
                        "checked_fields": [],
                        "baseline": "recorded" if s.before_recorded else "derived",
                    }
                )
        return {
            "schema": PROOF_SCHEMA,
            "status": "working" if self.working else "draft",
            "approved": False,
            "reads": reads,
            "not_read_back": sorted(
                set(self.params) - {p for s in targets for p in s.bound.values()}
            ),
            "uncovered_write_targets": list(self.search.uncovered),
            "read_problems": list(self.search.problems),
            "closed_world": self.search.closed_world_basis,
            "faults": [r.as_dict() for r in self.faults],
            "live": self.live,
            "misses": self.misses,
            "notes": list(self.search.notes),
            "identity": self.identity,
        }


def synthesize(
    recording_dir: Optional[Path],
    *,
    params: Optional[Mapping[str, str]] = None,
    db_path: Optional[Path] = None,
    verifier: Optional[Mapping[str, Any]] = None,
    complete_read: bool = False,
) -> Proposal:
    """Find the read, build the check, and run the recorded-state fault test.

    ``complete_read`` records a person's statement that the reads return
    everything the save can change (one store, no other tables). It is not
    proven: the proof says it was declared.
    """

    if params is None:
        params = recorded_params(recording_dir) if recording_dir else {}
    params = dict(params)
    if not params:
        raise SynthesisError(
            "the recording holds no parameter values (meta.json params), so "
            "there is nothing to find the saved record by"
        )
    search = ReadSearch()
    _, shared = param_lookup(params)
    search.notes.extend(shared)
    if recording_dir is not None:
        reads_from_network(Path(recording_dir), params, search)
    if verifier is not None:
        snapshots = None
        if recording_dir is not None:
            snap = Path(recording_dir) / "sor_snapshots.json"
            if _sealed.is_file(snap):
                try:
                    snapshots = json.loads(_sealed.read_text(snap))
                except ValueError:
                    snapshots = None
        reads_from_verifier(verifier, params, search, snapshots=snapshots)
    if db_path is not None:
        reads_from_sqlite(Path(db_path), params, search)
    if not any(s.is_target for s in search.surfaces):
        return Proposal(params, search, [], {}, [])
    _apply_sent_constants(search, params)
    if complete_read and not search.closed_world:
        search.closed_world = True
        search.closed_world_basis = "declared"
        search.notes.append(
            "a person declared that the reads return everything the save can "
            "change; writes elsewhere are not tested"
        )
    for surface in search.surfaces:
        for name in surface.config.get("_unscoped_references", []):
            search.notes.append(
                f"{surface.name}: the record names parameter {name!r} only as a "
                "reference the read is not scoped by; that link is not checked"
            )
    effects = effects_for(search.surfaces)
    section = verifier_section(search.surfaces)
    search.problems.extend(check_reads(search.surfaces, section, params))
    faults = prove_against_faults(
        search.surfaces,
        effects_for(search.surfaces, confirmed=True),
        params,
        closed_world=search.closed_world,
    )
    return Proposal(params, search, effects, section, faults)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_HEADER_WORKING = """\
# Record check built by `openadapt-flow scaffold-verifier` from the reads the
# app made after the save. It caught every injected bad write in its fault test
# and passed the clean save ({proof}). It is NOT approved: a person must
#   - confirm each read is the system of record, with its own read-only
#     credential (the recording's reads used the browser's sign-in);
#   - check that each {{param: ...}} is the input that should fill that field;
#   - delete every `needs_operator_confirmation: true` line.
# verify-run refuses this file, and qualify reports a problem, until then.
"""

_HEADER_DRAFT = """\
# DRAFT record check from `openadapt-flow scaffold-verifier`. It is NOT working:
# its fault test found misses (listed below and in {proof_file}). Fix the reads
# or add the missing ones, then run scaffold-verifier again.
#
{misses}
#
# verify-run refuses this file until every `needs_operator_confirmation: true`
# line is gone; do not remove them while misses remain.
"""


def render(proposal: Proposal) -> str:
    import yaml

    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": proposal.verifier,
        "effects": [
            effect.model_dump(mode="json", exclude_defaults=True)
            | {"needs_operator_confirmation": True}
            for effect in proposal.effects
        ],
    }
    proof = "recorded-state faults"
    if proposal.live is not None:
        proof += f" and the {proposal.live.get('fixture')} fault suite"
    if proposal.working:
        header = _HEADER_WORKING.format(proof=proof)
    else:
        misses = (
            "\n".join(f"# MISS: {m}" for m in proposal.misses)
            or "# MISS: no read found"
        )
        header = _HEADER_DRAFT.format(proof_file=PROOF_NAME, misses=misses)
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def write(proposal: Proposal, out_dir: Path) -> tuple[Path, Path]:
    """Write the proposed check and its proof; return both paths."""

    out_dir.mkdir(parents=True, exist_ok=True)
    check = out_dir / PROPOSED_NAME
    proof = out_dir / PROOF_NAME
    # Sealed in memory when ``out_dir`` is an encrypted recording or bundle.
    _sealed.write_text(check, render(proposal))
    _sealed.write_text(
        proof,
        json.dumps(proposal.proof(), indent=2, sort_keys=True, default=str) + "\n",
    )
    return check, proof


def summary(proposal: Proposal, check: Path, proof: Path) -> str:
    """What the operator sees: status, reads, faults, misses, next step."""

    lines: list[str] = []
    status = "WORKING (not approved)" if proposal.working else "DRAFT (not working)"
    lines.append(f"Record check: {status}")
    for item in proposal.proof()["reads"]:
        lines.append(f"  read {item['surface']}: {item['source']} -> {item['role']}")
    caught = sum(1 for r in proposal.faults if r.ok and not r.correct)
    bad = sum(1 for r in proposal.faults if not r.correct)
    lines.append(
        f"Fault test: {caught} of {bad} bad writes caught; clean save "
        + ("passed" if all(r.ok for r in proposal.faults if r.correct) else "STOPPED")
    )
    if proposal.live is not None:
        lines.append(
            f"Live fault suite ({proposal.live.get('fixture')}): "
            + ("passed" if proposal.live.get("passed") else "FAILED")
        )
    for miss in proposal.misses:
        lines.append(f"  MISS: {miss}")
    if proposal.identity is not None:
        ident = proposal.identity
        lines.append(
            f"Identity checks: {ident['armed_before']} of {ident['applicable']} "
            f"clicks armed now; {ident['armed_after']} with the proposed evidence"
        )
    lines.append(f"Wrote {check} and {proof}")
    if proposal.working:
        lines.append(
            "Next: a person reviews it and deletes each "
            "`needs_operator_confirmation: true` line, then "
            "`openadapt-flow qualify <bundle> --verify " + check.name + "`."
        )
    else:
        lines.append("Next: fix the misses above, then run scaffold-verifier again.")
    return "\n".join(lines)
