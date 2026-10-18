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
   checked against its own values), and must not lose a record that was there
   before. With a database read path, every other table gets a "no new rows"
   guard, so a write that lands somewhere the main read can't see is caught.
   Write targets nothing covers are listed.
3. **Prove it.** The check is judged against the fault classes of
   ``benchmark/effect_e2e`` applied to the recorded records: nothing saved, a
   field dropped, saved twice, saved on the wrong record, another record
   destroyed, and a stray write elsewhere. On a fixture (MockMed, the
   effect_e2e record service) it also runs end to end through the replayer
   (:mod:`openadapt_flow.record_check_proof`). A check that misses any fault,
   or stops the clean save, is written as a draft with the misses listed.
4. **A person approves.** Every effect carries
   ``needs_operator_confirmation: true``; ``verify-run`` and ``qualify``
   refuse the file until a person removes those lines.

No model calls, nothing sent anywhere. The only network read is the optional
``--verifier`` read of the system of record the operator named.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, quote, urlsplit

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

#: The network capture ``record --capture-network`` writes (PR #1261). Read
#: here without importing the recorder side, so the format has one owner.
NETWORK_FILE = "network.jsonl"
NETWORK_SCHEMA = "openadapt.network-capture/1"

PROPOSED_NAME = "record_check.proposed.yaml"
PROOF_NAME = "record_check.proof.json"
PROOF_SCHEMA = "openadapt.record-check-proof/v1"

#: Largest table ``--db`` scans for the saved row.
MAX_SCAN_ROWS = 20000

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LIST_KEYS = ("records", "data", "items", "results", "entries", "rows")
_SESSION_VALUE = re.compile(r"<session:[^>]*>|<secret>")
_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class SynthesisError(ValueError):
    """The inputs hold nothing a record check can be built from."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_network(recording_dir: Path) -> list[dict[str, Any]]:
    """The capture's request lines; an absent file is an empty capture."""

    path = Path(recording_dir) / NETWORK_FILE
    if not path.is_file():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows or rows[0].get("schema") != NETWORK_SCHEMA:
        raise SynthesisError(f"{path} is not a {NETWORK_SCHEMA} capture")
    return [row for row in rows[1:] if isinstance(row, dict)]


def recorded_params(recording_dir: Path) -> dict[str, str]:
    """The demonstration's parameter values from ``meta.json``."""

    try:
        meta = json.loads((Path(recording_dir) / "meta.json").read_text("utf-8"))
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
    #: Where the read came from, for the person reviewing it.
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


@dataclass
class ReadSearch:
    """What the search for the read found, including what it rejected."""

    surfaces: list[Surface] = field(default_factory=list)
    write_requests: list[dict[str, Any]] = field(default_factory=list)
    save_t: Optional[float] = None
    notes: list[str] = field(default_factory=list)
    #: Write targets the check does not read (plain sentences).
    uncovered: list[str] = field(default_factory=list)
    #: True when every table the save could touch is read (a database path),
    #: or a person declared the read complete (``--complete-read``).
    closed_world: bool = False
    #: ``"database"`` (every table read) or ``"declared"`` (a person said so).
    closed_world_basis: Optional[str] = None


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


def _templated_path(
    url: str, lookup: Mapping[str, str]
) -> tuple[str, dict[str, str], list[tuple[str, str]], dict[str, str]]:
    """``(path, path_params, literal_query, bound_query)`` for one GET URL.

    Path segments and query values equal to a recorded value become
    ``{param}`` placeholders, so the check reads the record each run writes.
    """

    parts = urlsplit(url)
    path_params: dict[str, str] = {}
    segments = []
    for segment in parts.path.split("/"):
        name = lookup.get(segment)
        if name and segment:
            segments.append("{" + name + "}")
            path_params[name] = name
        else:
            segments.append(segment.replace("{", "{{").replace("}", "}}"))
    literal_query: list[tuple[str, str]] = []
    bound_query: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        name = lookup.get(value)
        if name:
            bound_query[key] = name
        else:
            literal_query.append((key, value))
    return "/".join(segments), path_params, literal_query, bound_query


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


def _save_time(recording_dir: Path, network: list[dict[str, Any]]) -> Optional[float]:
    """When the save happened: the last write-shaped step, else the first write."""

    from openadapt_flow.scaffold_verifier import candidates_from_recording

    times: dict[int, float] = {}
    events_path = Path(recording_dir) / "events.jsonl"
    if events_path.is_file():
        for index, line in enumerate(events_path.read_text("utf-8").splitlines()):
            try:
                event = json.loads(line) if line.strip() else {}
            except ValueError:
                continue
            if isinstance(event.get("t"), (int, float)):
                times[index] = float(event["t"])
        try:
            candidates = candidates_from_recording(Path(recording_dir))
        except SystemExit:
            candidates = []
        stamped = [
            times[int(c.step_id.split("_")[1])]
            for c in candidates
            if int(c.step_id.split("_")[1]) in times
        ]
        if stamped:
            return max(stamped)
    writes = [
        float(line["t"])
        for line in network
        if str(line.get("method") or "").upper() in _WRITE_METHODS
        and isinstance(line.get("t"), (int, float))
    ]
    return min(writes) if writes else None


def _accepted(line: Mapping[str, Any]) -> bool:
    """A write the server accepted: 2xx, or a redirect after a form post."""

    status = line.get("status")
    return isinstance(status, int) and 200 <= status < 400 and not line.get("failed")


def _ok(line: Mapping[str, Any]) -> bool:
    status = line.get("status")
    return isinstance(status, int) and 200 <= status < 300 and not line.get("failed")


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
    path, path_params, literal_query, bound_query = _templated_path(url, lookup)
    if _SESSION_VALUE.search(url):
        return None, f"GET {urlsplit(url).path}: the URL carries a session token"

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
    if listing.kind == "fhir":
        resource_type = listing.resource_type or str(resource.get("resourceType") or "")
        marker = "/" + resource_type
        raw_path = urlsplit(url).path
        if not resource_type or marker not in raw_path:
            return None, f"GET {raw_path}: FHIR resource type not in the URL"
        base = origin + raw_path[: raw_path.rindex(marker)]
        taken_names: set[str] = {"id"}
        field_paths = {"id": "id"}
        bound: dict[str, str] = {}
        for dotted, param in sorted(exact.items()):
            if dotted == "id":
                continue
            name = _field_name(dotted, taken_names)
            field_paths[name] = dotted
            bound[name] = param
        config: dict[str, Any] = {
            "kind": "fhir",
            "base_url": base,
            "resource_type": resource_type,
            "field_paths": field_paths,
        }
        literal = dict(literal_query)
        if literal:
            config["search_params"] = literal
        if bound_query:
            config["search_param_exprs"] = {
                key: {"param": name} for key, name in bound_query.items()
            }
        from openadapt_flow.runtime.effects.fhir import extract_path

        def shape(res: dict[str, Any]) -> dict[str, Any]:
            flat = {key: extract_path(res, p) for key, p in field_paths.items()}
            return flat

        after = [shape(res) for res in listing.resources]
        target = shape(resource)
        name = _field_name(f"fhir_{resource_type}", taken)
        unscoped = sorted(
            set(reference.values()) - set(bound_query.values()) - set(bound.values())
        )
        before_records, recorded = _before_records(
            before_line, listing_kind="fhir", records_key=None, shape=shape
        )
        surface = Surface(
            name=name,
            config=config,
            after=after,
            before=before_records if recorded else _minus(after, target),
            before_recorded=recorded,
            source=f"GET {raw_path} (t={line.get('t')})",
            target=target,
            bound=bound,
        )
        surface.scope = sorted(set(bound_query.values()) | set(path_params))
        if unscoped:
            surface.config["_unscoped_references"] = unscoped
        return surface, None

    records_key = listing.records_key
    bound = {path_: param for path_, param in exact.items() if "." not in path_}
    if not bound:
        return None, (
            f"GET {urlsplit(url).path}: the recorded values sit in nested fields; "
            "the REST check reads top-level fields only"
        )
    records_path = path
    if literal_query or bound_query:
        query = [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in literal_query]
        query += [f"{quote(k, safe='')}={{{name}}}" for k, name in bound_query.items()]
        records_path += "?" + "&".join(query)
        path_params.update({name: name for name in bound_query.values()})
    config = {
        "kind": "rest",
        "base_url": origin,
        "records_path": records_path,
        "records_key": records_key,
    }
    if path_params:
        config["path_params"] = {key: {"param": key} for key in sorted(path_params)}
    after = [dict(r) for r in listing.resources]
    target = dict(resource)
    before_records, recorded = _before_records(
        before_line, listing_kind="rest", records_key=records_key, shape=dict
    )
    surface = Surface(
        name=_field_name(urlsplit(url).path.strip("/") or "records", taken),
        config=config,
        after=after,
        before=before_records if recorded else _minus(after, target),
        before_recorded=recorded,
        source=f"GET {urlsplit(url).path} (t={line.get('t')})",
        target=target,
        bound=bound,
        scope=sorted(path_params),
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
    for listing in _listings(_json_body(line)):
        if listing.kind == listing_kind and listing.records_key == records_key:
            return [shape(r) for r in listing.resources], True
    return [], False


def _minus(
    records: list[dict[str, Any]], target: Mapping[str, Any]
) -> list[dict[str, Any]]:
    key = stable_id(dict(target))
    return [dict(r) for r in records if stable_id(dict(r)) != key]


def reads_from_network(
    recording_dir: Path, params: Mapping[str, str], search: ReadSearch
) -> None:
    """Add the GET that returns the saved record to ``search``."""

    network = load_network(recording_dir)
    if not network:
        return
    lookup, _ = param_lookup(params)
    save_t = _save_time(recording_dir, network)
    search.save_t = save_t
    search.write_requests = [
        line
        for line in network
        if str(line.get("method") or "").upper() in _WRITE_METHODS
        and _accepted(line)
        and (save_t is None or float(line.get("t", 0.0)) >= save_t - 2.0)
    ]
    gets = [
        line
        for line in network
        if str(line.get("method") or "").upper() == "GET" and _ok(line)
    ]
    after_gets = [
        line for line in gets if save_t is None or float(line.get("t", 0.0)) > save_t
    ]
    taken: set[str] = set()
    candidates: list[tuple[tuple[Any, ...], Surface]] = []
    rejected: list[str] = []
    seen_urls: set[str] = set()
    for line in after_gets:
        url = str(line.get("url") or "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        before_line = None
        for earlier in gets:
            if earlier.get("url") == url and (
                save_t is not None and float(earlier.get("t", 0.0)) < save_t
            ):
                before_line = earlier
        for listing in _listings(_json_body(line)):
            surface, why = _surface_from_listing(
                line, listing, lookup, before_line=before_line, taken=taken
            )
            if why:
                rejected.append(why)
            if surface is None:
                continue
            rank = (
                len(surface.bound),
                "id" in (surface.target or {}),
                surface.before_recorded,
                -float(line.get("t", 0.0)),
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
    search.surfaces.append(chosen)
    for rank, other in candidates[1:]:
        search.notes.append(
            f"also returns the saved record: {other.source} (not used; the check "
            f"reads {chosen.source})"
        )
    for line in search.write_requests:
        sent = set(_write_values(line).values())
        read_back = {str((chosen.target or {}).get(fld)) for fld in chosen.bound}
        if not sent & read_back:
            search.uncovered.append(
                f"{line.get('method')} {urlsplit(str(line.get('url') or '')).path}: "
                "no read returns what this request sent"
            )


# -- database read path --------------------------------------------------------


def reads_from_sqlite(
    db_path: Path, params: Mapping[str, str], search: ReadSearch
) -> None:
    """Every table: the ones holding the saved row are read, the rest guarded."""

    lookup, _ = param_lookup(params)
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SynthesisError(f"cannot open {db_path} read-only: {exc}") from exc
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
            if not _TABLE_NAME.match(table):
                search.uncovered.append(
                    f"table {table!r}: not a plain name, so the check can't read it"
                )
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count > MAX_SCAN_ROWS:
                search.uncovered.append(
                    f"table {table}: {count} rows, too many to scan for the saved row"
                )
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
            if best is None:
                search.surfaces.append(
                    Surface(
                        name=table,
                        config=config,
                        after=rows,
                        before=list(rows),
                        before_recorded=False,
                        source=f"table {table} (no row holds the recorded values)",
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
                where = " AND ".join(
                    f'"{column}" = :{param}' for column, param in sorted(scope.items())
                )
                config["sql_query"] = f"{query} WHERE {where}"
                config["sql_query_params"] = {
                    param: {"param": param} for param in sorted(set(scope.values()))
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
                    name=table,
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
    config = dict(section)
    search.surfaces.append(
        Surface(
            name=str(cfg.kind or "records"),
            config=dict(config),
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


def effects_for(
    surfaces: Iterable[Surface], *, confirmed: bool = False
) -> list[Effect]:
    """The effect contract for a set of surfaces (targets, then guards)."""

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
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=match,
                    expected_count=1,
                    count_new_only=True,
                    forbid_collateral_loss=True,
                    risk="irreversible",
                    probe=surface_probe(
                        surface.name,
                        "exactly one new record with the run's values; no "
                        "record that was there before may disappear",
                    ),
                    needs_operator_confirmation=not confirmed,
                )
            )
        else:
            effects.append(
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={},
                    expected_count=0,
                    count_new_only=True,
                    forbid_collateral_loss=False,
                    risk="irreversible",
                    probe=surface_probe(surface.name, "the save adds no row here"),
                    needs_operator_confirmation=not confirmed,
                )
            )
    return effects


def verifier_section(surfaces: list[Surface]) -> dict[str, Any]:
    """The ``verifier:`` section: one read, or one read per surface."""

    def clean(config: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in config.items() if not k.startswith("_")}

    return {
        "kind": "surfaces",
        "surfaces": {surface.name: clean(surface.config) for surface in surfaces},
    }


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


def _judge(
    effects: list[Effect],
    surfaces: list[Surface],
    states: Mapping[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    params: Mapping[str, str],
) -> tuple[bool, list[str]]:
    from openadapt_flow.runtime.effects.surfaces import surface_of

    by_name = {surface.name: surface for surface in surfaces}
    reasons: list[str] = []
    for effect in effects:
        name = surface_of(effect) or ""
        if name not in by_name:
            reasons.append(f"effect routes to unknown surface {name!r}")
            continue
        before, after = states[name]
        verdict = judge_records(
            effect.resolve(params),
            EffectState(substrate="proof", reachable=True, records=before),
            after,
            substrate="proof",
        )
        if verdict.verdict is not Verdict.CONFIRMED:
            reasons.append(f"{name}: {verdict.reason}")
    return not reasons, reasons


def prove_against_faults(
    surfaces: list[Surface],
    effects: list[Effect],
    params: Mapping[str, str],
    *,
    closed_world: bool,
) -> list[FaultResult]:
    """Judge the check on the clean save and on each injected bad write.

    The bad writes are the persistence faults of ``benchmark/effect_e2e``,
    built from the records the recording saw. Transport faults (a rejected
    write, an expired session, a timeout) are handled by the actuator before
    any check runs, so they are not repeated here.
    """

    base = {s.name: (list(s.before), list(s.after)) for s in surfaces}
    results: list[FaultResult] = []

    def run(
        fault: str, detail: str, states: Mapping[str, Any], *, correct: bool = False
    ) -> None:
        passed, reasons = _judge(effects, surfaces, states, params)
        results.append(FaultResult(fault, detail, correct, passed, reasons))

    run("ok", "the clean save as recorded", base, correct=True)

    targets = [s for s in surfaces if s.is_target]
    guards = [s for s in surfaces if not s.is_target]
    for surface in targets:
        target = surface.target or {}
        before, after = base[surface.name]
        tid = stable_id(target)
        without = [r for r in after if stable_id(r) != tid]

        run(
            "no_persist",
            f"nothing saved in {surface.name}",
            {**base, surface.name: (before, without)},
        )

        for fld in sorted(surface.bound):
            dropped = dict(target, **{fld: ""})
            run(
                "partial",
                f"{surface.name}.{fld} dropped",
                {**base, surface.name: (before, without + [dropped])},
            )

        copy = dict(target)
        if "id" in copy:
            copy["id"] = _fresh_id(after)
        else:
            copy["_duplicate"] = "1"
        run(
            "duplicate",
            f"saved twice in {surface.name}",
            {**base, surface.name: (before, after + [copy])},
        )

        identity = [
            f for f, p in surface.bound.items() if _identity_like(params.get(p, ""))
        ]
        if not identity and not surface.scope:
            results.append(
                FaultResult(
                    "wrong_record",
                    f"{surface.name}: no identity field to move the save to another record",
                    False,
                    True,
                    ["no recorded value identifies the record"],
                    untestable=True,
                )
            )
        for fld in sorted(identity):
            moved = dict(target, **{fld: f"{target.get(fld)}-other"})
            run(
                "wrong_record",
                f"{surface.name}: saved on another {fld}",
                {**base, surface.name: (before, without + [moved])},
            )
        for param in sorted(set(surface.scope) - {surface.bound[f] for f in identity}):
            run(
                "wrong_record",
                f"{surface.name}: saved under another {param}, which the read "
                "is scoped by",
                {**base, surface.name: (before, without)},
            )

        payload = [f for f in surface.bound if f not in identity]
        concurrent = dict(target)
        concurrent["id" if "id" in target else "_concurrent"] = _fresh_id(
            after + before
        )
        for fld in payload or list(surface.bound):
            concurrent[fld] = f"concurrent {concurrent.get(fld)}"
        run(
            "stale",
            f"{surface.name}: the save destroyed another record of the same subject",
            {**base, surface.name: (before + [concurrent], after)},
        )

    for surface in guards:
        before, after = base[surface.name]
        stray: dict[str, Any] = dict(after[-1]) if after else {}
        stray["id"] = _fresh_id(after)
        run(
            "collateral_unaudited",
            f"a stray row written to {surface.name}",
            {**base, surface.name: (before, after + [stray])},
        )
    if not closed_world:
        results.append(
            FaultResult(
                "collateral_unaudited",
                "a stray row written to a table this check does not read",
                False,
                True,
                [
                    "the check reads only "
                    + ", ".join(s.name for s in surfaces)
                    + "; a write anywhere else passes it (give a database read "
                    "path with --db, or more reads)"
                ],
                untestable=False,
            )
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
        return {
            "schema": PROOF_SCHEMA,
            "status": "working" if self.working else "draft",
            "approved": False,
            "reads": [
                {
                    "surface": s.name,
                    "source": s.source,
                    "role": "saved record" if s.is_target else "no new rows",
                    "checked_fields": sorted(s.bound) + sorted(s.constants),
                    "baseline": "recorded" if s.before_recorded else "derived",
                }
                for s in self.search.surfaces
            ],
            "not_read_back": sorted(
                set(self.params) - {p for s in targets for p in s.bound.values()}
            ),
            "uncovered_write_targets": list(self.search.uncovered),
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
            if snap.is_file():
                try:
                    snapshots = json.loads(snap.read_text("utf-8"))
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
    check.write_text(render(proposal), encoding="utf-8")
    proof.write_text(
        json.dumps(proposal.proof(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
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
    for item in proposal.search.uncovered:
        lines.append(f"  not covered: {item}")
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
