"""Check an "ensure this record exists" write through a FHIR read path only.

The task: make sure one person (or other entity) has a record. If one is
already there, report it and write nothing; otherwise create exactly one,
with the given values. The agent's report is a claim. This module reads the
server before and after and gives one of three outcomes:

- ``found_existing``: exactly one record was already the person, it is the
  reported id, and the agent wrote nothing anywhere;
- ``created_verified``: no record was the person, the agent created exactly
  one, it holds every value, the agent wrote nothing else, nobody else
  changed it, and it is now the only record that is the person;
- ``needs_review``: anything else, with the reasons.

It reads only what a FHIR server offers a client: searches, ``_history`` and
``AuditEvent``, never the database. What that read path can see, and what it
can't, is stated in ``docs/ENSURE_RECORD.md``:

- **Who wrote what in the window** comes from the server's audit trail
  (``AuditEvent`` searched by date). Each written resource's ``_history``
  gives its versions in the window and their ``meta.author``, which the
  server sets. The audit and the history must name the same actor, or the
  change is unattributed.
- **A sweep** (optional) searches the declared resource types by
  ``_lastUpdated``, so a create or update the audit trail missed still shows.
  A sweep can't see a delete.
- A change no source attributes counts as the agent's own: a change nobody
  can explain is never assumed to be someone else's.

Fail-closed: an unreadable read, a page past the limit, a next link to
another origin, a resource with no id or a server time that can't be read
gives ``needs_review`` with ``unreadable``, never an outcome that proves
something.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

Outcome = Literal["found_existing", "created_verified", "needs_review"]
Compare = Literal["exact", "casefold", "digits", "date"]

#: A search longer than this many pages reads as unreadable.
MAX_PAGES = 50
#: FHIR AuditEvent ``action`` codes that write (create, update, delete).
WRITE_ACTIONS = ("C", "U", "D")
#: AuditEvent action codes: create, read, update, delete, execute. An
#: execute (an operation, such as starting a Bot) may write, so it counts as
#: a write; a missing or unknown code makes the window unreadable.
AUDIT_ACTIONS = ("C", "R", "U", "D", "E")


class EnsureRecordError(ValueError):
    """The contract can't be loaded or bound (never a failed check)."""


class Unreadable(RuntimeError):
    """A read the judgment needs did not give a complete answer."""


# -- the contract ----------------------------------------------------------------


class FieldRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Dotted path into the resource; a segment may filter a list
    #: (``telecom[system=email].value``), as in ``effects/fhir.py``.
    path: str = Field(min_length=1)
    compare: Compare = "exact"
    #: Printed form -> stored form (``Massachusetts: MA``).
    map: dict[str, str] = Field(default_factory=dict)


class AuditRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str = "AuditEvent"
    #: The search parameter that takes the event time (``date`` in R4).
    since_param: str = "date"
    actor_path: str = "agent.who.reference"
    entity_path: str = "entity.what.reference"
    action_path: str = "action"
    #: An empty window proves nothing when the server isn't logging (Medplum
    #: answers an empty search, not an error, with ``saveAuditEvents`` off).
    #: With ``heartbeat``, a found record counts only when the verifier's own
    #: read of it shows up in the trail; set it where reads by id are logged.
    heartbeat: bool = True


class ChangeFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit: Optional[AuditRead] = Field(default_factory=AuditRead)
    #: Read each written resource's ``_history`` for its window versions.
    history: bool = True
    #: Whether the server returns ``meta.author`` on each version, a second
    #: witness to who wrote it. Medplum 5.2 doesn't, so its contract sets
    #: false and attribution rests on the audit trail alone.
    history_author: bool = True
    #: Resource types searched by ``_lastUpdated`` as a second source.
    sweep_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _some_source(self) -> "ChangeFeed":
        if self.audit is None and not self.sweep_types:
            raise ValueError("the change feed needs an audit read or a sweep")
        return self


class EnsureContract(BaseModel):
    """``openadapt.ensure-record/v1``: what one ensure-record write must do."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["openadapt.ensure-record/v1"] = "openadapt.ensure-record/v1"
    resource_type: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    access_token_env: Optional[str] = None
    #: The agent's own actor references (``Practitioner/<id>``).
    self_actors: list[str] = Field(alias="self", min_length=1)
    #: Task parameter -> where and how the record stores it.
    fields: dict[str, FieldRule] = Field(min_length=1)
    #: Each entry is one way to be the same person: every parameter equal.
    identity: list[list[str]] = Field(min_length=1)
    #: Task parameter -> FHIR search parameter, to find candidates.
    search: dict[str, str] = Field(min_length=1)
    change_feed: ChangeFeed = Field(default_factory=ChangeFeed)
    #: Wait this long after the write before reading, for the audit trail.
    #: The created record may hold only what ``fields`` describe, one value
    #: each: an element the task didn't give (a second given name, a fax
    #: number, a district) is a wrong field. ``allow`` names extra leaf paths
    #: a correct record may carry, alone (``address.use``) or with the one
    #: value allowed, as JSON (``active=true``, ``address.use="home"``).
    #: ``id``, ``meta`` and ``resourceType`` always are.
    closed: bool = False
    allow: list[str] = Field(default_factory=list)
    #: The audit trail is written a little after the save (Medplum: up to
    #: about 0.1 s). The change feed is read until two reads ``settle_s``
    #: apart agree, at most ``stable_reads`` times; a feed still changing
    #: then reads as unreadable.
    settle_s: float = Field(default=2.0, ge=0, le=120)
    stable_reads: int = Field(default=3, ge=1, le=10)
    timeout_s: float = Field(default=15.0, gt=0, le=300)

    @model_validator(mode="after")
    def _bound(self) -> "EnsureContract":
        for rule in self.identity:
            if not rule:
                raise ValueError("an identity rule names no parameter")
            missing = [p for p in rule if p not in self.fields]
            if missing:
                raise ValueError(f"identity names unknown fields: {missing}")
            if not any(p in self.search for p in rule):
                raise ValueError(f"identity rule {rule} has no search parameter")
        return self


def load_contract(path: str | Path) -> EnsureContract:
    import yaml

    from openadapt_flow.qualify.config import _expand

    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise EnsureRecordError(f"cannot read {source}: {exc}") from exc
    missing: list[str] = []
    data = _expand(dict(data), missing)
    if missing:
        raise EnsureRecordError(
            "environment variable(s) not set: " + ", ".join(sorted(set(missing)))
        )
    try:
        return EnsureContract.model_validate(data)
    except Exception as exc:
        raise EnsureRecordError(f"invalid {source}: {exc}") from exc


# -- values ----------------------------------------------------------------------


def extract(resource: Mapping[str, Any], path: str) -> Any:
    from openadapt_flow.runtime.effects.fhir import extract_path

    return extract_path(dict(resource), path)


def normalize(value: Any, rule: FieldRule, *, task: bool = False) -> Optional[str]:
    """``value`` as the rule compares it. ``map`` turns a task's value into
    the stored form (Massachusetts to MA), so it applies to the task's side
    only: a record that holds "Massachusetts" holds the wrong value."""

    if value is None:
        return None
    text = str(value).strip()
    if task:
        text = rule.map.get(text, text)
    if rule.compare == "casefold":
        return text.casefold()
    if rule.compare == "digits":
        return re.sub(r"\D", "", text)
    if rule.compare == "date":
        return text[:10]
    return text


def same(contract: EnsureContract, name: str, stored: Any, wanted: Any) -> bool:
    rule = contract.fields[name]
    a, b = normalize(stored, rule), normalize(wanted, rule, task=True)
    return a is not None and b is not None and a == b


def extract_all(node: Any, path: str) -> list[Any]:
    """Every value at ``path``, across every list element (``name.given``
    reads each given name of each name; a filter keeps the matching ones)."""

    if not path:
        return (
            [node]
            if not isinstance(node, list)
            else [x for x in node if not isinstance(x, (dict, list))]
        )
    if isinstance(node, list):
        return [v for item in node for v in extract_all(item, path)]
    if not isinstance(node, dict):
        return []
    seg, _, rest = path.partition(".")
    name, _, cond = seg.partition("[")
    child = node.get(name)
    if cond:
        key, _, want = cond[:-1].partition("=")
        items = child if isinstance(child, list) else [child]
        child = [i for i in items if isinstance(i, dict) and str(i.get(key)) == want]
    return extract_all(child, rest) if child is not None else []


def is_person(
    contract: EnsureContract, resource: Mapping[str, Any], params: Mapping[str, Any]
) -> bool:
    """True when the resource meets any identity rule for these values.

    Every element counts: a record whose second name, or second email, is
    the person's is the person (a maiden name kept first doesn't hide it).
    """

    return any(
        all(
            any(
                same(contract, p, value, params.get(p))
                for value in extract_all(resource, contract.fields[p].path)
            )
            for p in rule
        )
        for rule in contract.identity
        if all(params.get(p) not in (None, "") for p in rule)
    )


def wrong_fields(
    contract: EnsureContract, resource: Mapping[str, Any], params: Mapping[str, Any]
) -> list[str]:
    return sorted(
        name
        for name, rule in contract.fields.items()
        if name in params
        and not same(contract, name, extract(resource, rule.path), params[name])
    )


def leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every value with its path, list positions dropped (``telecom.value`` twice)."""

    if isinstance(node, dict):
        return [x for k, v in node.items() for x in leaves(v, f"{prefix}{k}.")]
    if isinstance(node, list):
        return [x for v in node for x in leaves(v, prefix)]
    return [(prefix[:-1], node)]


def _allowed(contract: EnsureContract, path: str, value: Any) -> bool:
    for entry in contract.allow:
        name, eq, want = entry.partition("=")
        # The value is compared as JSON: ``active=true`` is the boolean, and a
        # string is quoted (``address.use="home"``).
        if name == path and (not eq or json.dumps(value, ensure_ascii=False) == want):
            return True
    return False


def _expected_leaves(
    contract: EnsureContract, params: Optional[Mapping[str, Any]] = None
) -> Counter[str]:
    want: Counter[str] = Counter()
    for name, rule in contract.fields.items():
        if params is not None and params.get(name) in (None, ""):
            continue  # the task didn't give it, so the record mustn't hold it
        parts = []
        for seg in rule.path.split("."):
            name, _, cond = seg.partition("[")
            if cond:
                want[".".join(parts + [name, cond.split("=", 1)[0]])] += 1
            parts.append(name)
        want[".".join(parts)] += 1
    return want


def extra_elements(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Leaf paths the record holds beyond the fields the task gave.

    The server's own ``meta`` is exempt, except ``meta.security`` and
    ``meta.tag``: a security label can hide the record from other users.
    """

    def exempt(path: str) -> bool:
        top = path.split(".")[0]
        if top == "meta":
            return not path.startswith(("meta.security", "meta.tag"))
        return top in ("id", "resourceType")

    have = Counter(
        p
        for p, value in leaves(dict(resource))
        if not exempt(p) and not _allowed(contract, p, value)
    )
    return sorted((have - _expected_leaves(contract, params)).elements())


# -- the read path ---------------------------------------------------------------

Getter = Callable[
    [str, Optional[Mapping[str, Any]]], tuple[int, Mapping[str, str], Any]
]


def _ref(value: str) -> str:
    """``Type/id``, whether the server wrote it relative or absolute."""

    parts = value.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else value


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class FhirReader:
    """Read-only FHIR client: every page of a search, or :class:`Unreadable`.

    ``get`` is injectable for tests: ``get(url, params) -> (status, headers,
    json)``. The default uses ``httpx`` with the contract's bearer token.
    """

    def __init__(self, contract: EnsureContract, get: Optional[Getter] = None) -> None:
        self.base_url = contract.base_url.rstrip("/")
        self.timeout_s = contract.timeout_s
        self._token_env = contract.access_token_env
        self._get = get or self._httpx_get
        self._client: Any = None

    def _httpx_get(
        self, url: str, params: Optional[Mapping[str, str]]
    ) -> tuple[int, Mapping[str, str], Any]:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        headers = {"Accept": "application/fhir+json"}
        if self._token_env:
            token = os.environ.get(self._token_env)
            if not token:
                raise Unreadable(f"no access token in {self._token_env}")
            headers["Authorization"] = f"Bearer {token}"
        resp = self._client.get(url, params=dict(params or {}), headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, dict(resp.headers), body

    def fetch(
        self, url: str, params: Optional[Mapping[str, Any]] = None
    ) -> tuple[Mapping[str, str], Any]:
        try:
            status, headers, body = self._get(url, params)
        except Unreadable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure is unreadable
            raise Unreadable(f"GET {url} failed: {type(exc).__name__}") from exc
        if status // 100 != 2:
            raise Unreadable(f"GET {url} returned HTTP {status}")
        return headers, body

    def bundle(
        self, path: str, params: Optional[Mapping[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """Every entry of a search or history Bundle, across all pages."""

        url: Optional[str] = f"{self.base_url}/{path}"
        query: Optional[Mapping[str, Any]] = params
        entries: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            assert url is not None
            _, body = self.fetch(url, query)
            if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
                raise Unreadable(f"{path}: the reply is not a Bundle")
            page = body.get("entry") or []
            if not isinstance(page, list):
                raise Unreadable(f"{path}: entry is not a list")
            entries += [e for e in page if isinstance(e, dict)]
            nxt = next(
                (
                    link.get("url")
                    for link in body.get("link") or []
                    if isinstance(link, dict) and link.get("relation") == "next"
                ),
                None,
            )
            if not nxt:
                total = body.get("total")
                if isinstance(total, int) and body.get("type") == "searchset":
                    found = len(
                        {
                            json.dumps(
                                [
                                    (e.get("resource") or {}).get("resourceType"),
                                    (e.get("resource") or {}).get("id"),
                                ]
                            )
                            for e in entries
                            if (e.get("search") or {}).get("mode", "match") == "match"
                        }
                    )
                    if total > found:
                        raise Unreadable(f"{path}: total {total} but read {found}")
                return entries
            if _origin(nxt) != _origin(self.base_url):
                raise Unreadable(f"{path}: next page on another origin")
            url, query = nxt, None
        raise Unreadable(f"{path}: more than {MAX_PAGES} pages")

    def resources(
        self, path: str, params: Optional[Mapping[str, Any]] = None
    ) -> list[dict[str, Any]]:
        out = []
        for entry in self.bundle(path, params):
            if (entry.get("search") or {}).get("mode", "match") != "match":
                continue
            res = entry.get("resource")
            if not isinstance(res, dict) or not res.get("id"):
                raise Unreadable(f"{path}: an entry has no resource id")
            out.append(res)
        return out

    def server_time(self) -> datetime:
        """The server's clock, from the ``Date`` header of a metadata read."""

        headers, _ = self.fetch(f"{self.base_url}/metadata", {"_summary": "true"})
        raw = {k.lower(): v for k, v in headers.items()}.get("date")
        if not raw:
            raise Unreadable("the server sent no Date header")
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise Unreadable("the server's Date header can't be read") from exc


def _instant(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        raise Unreadable(f"a timestamp with no time zone: {value}")
    return when


# -- snapshots -------------------------------------------------------------------


@dataclass
class Baseline:
    """What was true before the write: the window start and the matches."""

    started: datetime
    #: id -> resource, every record that was the person before the write.
    matches: dict[str, dict[str, Any]]
    #: The candidate searches' result count (for evidence only).
    candidates: int = 0


@dataclass
class Change:
    """One write in the window, by resource."""

    ref: str  # "Type/id"
    actions: set[str] = field(default_factory=set)
    #: Actors who wrote it, from history (server-set) and the audit trail.
    authors: set[str] = field(default_factory=set)
    attributed: bool = True
    #: True when only the sweep saw it (the audit trail did not log it).
    unlogged: bool = False


def find_matches(
    contract: EnsureContract, reader: FhirReader, params: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], int]:
    """Every live record that is the person, by each identity rule's search."""

    seen: dict[str, dict[str, Any]] = {}
    queries: list[dict[str, str]] = []
    for rule in contract.identity:
        given = [p for p in rule if params.get(p) not in (None, "")]
        if len(given) < len(rule):
            continue  # the task doesn't give every value this rule needs
        # The whole rule, and each value alone: a server's search may compare
        # differently from the contract, so the candidates are wide and the
        # contract's own comparison decides.
        queries.append({contract.search[p]: str(params[p]).strip() for p in rule})
        if len(rule) > 1:
            queries += [{contract.search[p]: str(params[p]).strip()} for p in rule]
    if not queries:
        raise Unreadable("the task gives the values of no identity rule")
    for query in queries:
        for res in reader.resources(contract.resource_type, {**query, "_count": "200"}):
            seen[str(res["id"])] = res
    matches = {i: r for i, r in seen.items() if is_person(contract, r, params)}
    return matches, len(seen)


def capture_baseline(
    contract: EnsureContract,
    params: Mapping[str, Any],
    reader: Optional[FhirReader] = None,
) -> Baseline:
    reader = reader or FhirReader(contract)
    # One second before the server's clock: an instant search is to the second.
    started = reader.server_time() - timedelta(seconds=1)
    matches, candidates = find_matches(contract, reader, params)
    return Baseline(started=started, matches=matches, candidates=candidates)


def read_changes(
    contract: EnsureContract,
    reader: FhirReader,
    since: datetime,
    until: Optional[datetime] = None,
) -> dict[str, Change]:
    """Every resource written in ``[since, until]``, with who wrote it."""

    def window(param: str) -> dict[str, Any]:
        bounds = [f"ge{_instant(since)}"]
        if until is not None:
            bounds.append(f"le{_instant(until)}")
        return {param: bounds, "_count": "200"}

    feed = contract.change_feed
    selves = {_ref(x) for x in contract.self_actors}
    changes: dict[str, Change] = {}
    if feed.audit is not None:
        a = feed.audit
        for event in reader.resources(a.resource_type, window(a.since_param)):
            action = extract(event, a.action_path)
            if action not in AUDIT_ACTIONS:
                raise Unreadable(f"an audit event has action {action!r}")
            if action == "R":
                continue
            actors = [
                _ref(x)
                for x in extract_all(event, a.actor_path)
                if isinstance(x, str) and x
            ]
            ref = extract(event, a.entity_path)
            if not isinstance(ref, str) or "/" not in ref:
                if action == "E" and actors and not set(actors) & selves:
                    continue  # another user's operation on no resource
                # A write the log can't tie to a resource: the window can't be
                # read completely.
                raise Unreadable("an audit write names no resource")
            ref = _ref(ref)
            change = changes.setdefault(ref, Change(ref=ref))
            change.actions.add(str(action))
            if actors:
                change.authors.update(actors)
            else:
                change.attributed = False
    for rtype in feed.sweep_types:
        for res in reader.resources(rtype, window("_lastUpdated")):
            ref = f"{rtype}/{res['id']}"
            if ref not in changes:
                changes[ref] = Change(ref=ref, actions={"U"}, unlogged=True)
    if feed.history:
        for ref, change in changes.items():
            authors: set[str] = set()
            for entry in reader.bundle(f"{ref}/_history", {"_count": "200"}):
                res = entry.get("resource")
                req = entry.get("request") or {}
                if isinstance(res, dict):
                    meta = res.get("meta") or {}
                    when = _when(meta.get("lastUpdated"))
                    if when is None or when < since or (until and when > until):
                        continue
                    if not feed.history_author:
                        continue
                    who = (meta.get("author") or {}).get("reference")
                    if isinstance(who, str) and who:
                        authors.add(_ref(who))
                    else:
                        change.attributed = False
                elif str(req.get("method", "")).upper() == "DELETE":
                    change.actions.add("D")
            if authors:
                if change.authors and change.authors != authors:
                    # The log and the record disagree on who wrote it.
                    change.attributed = False
                change.authors |= authors
            elif change.unlogged:
                # Neither the log nor the record says who wrote it.
                change.attributed = False
    return changes


# -- the judgment ----------------------------------------------------------------


@dataclass
class EnsureResult:
    outcome: Outcome
    record_id: Optional[str]
    reasons: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "id": self.record_id,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


def _review(reasons: list[str], evidence: dict[str, Any]) -> EnsureResult:
    return EnsureResult("needs_review", None, reasons, evidence)


def judge(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    changes: Mapping[str, Change],
    after_matches: Mapping[str, Mapping[str, Any]],
    created: Optional[Mapping[str, Any]],
    existing_now: Optional[Mapping[str, Any]] = None,
) -> EnsureResult:
    """Decide the outcome from the snapshots. Reads nothing itself.

    ``created`` is the current version of the one resource the agent
    created (or None when it created none, or more than one).
    ``existing_now`` is the current version of the one record that was
    already the person (None when it is gone): someone else may have edited
    it during the run, which doesn't change what the agent found.
    """

    selfs = {_ref(a) for a in contract.self_actors}
    mine = {
        ref: c
        for ref, c in changes.items()
        if not c.attributed or not c.authors or c.authors & selfs
    }
    others = {ref: c for ref, c in changes.items() if ref not in mine}
    evidence: dict[str, Any] = {
        "before_matches": len(baseline.matches),
        "after_matches": len(after_matches),
        "agent_writes": len(mine),
        "other_writes": len(others),
        "unattributed_writes": sum(
            1 for c in mine.values() if not c.attributed or not c.authors
        ),
        "unlogged_writes": sum(1 for c in changes.values() if c.unlogged),
        "agent_write_types": sorted({r.split("/")[0] for r in mine}),
    }
    own_type = contract.resource_type
    if not mine:
        if len(baseline.matches) == 1:
            (rid,) = baseline.matches
            dupes = sorted(set(after_matches) - {rid})
            if dupes:
                return _review(
                    [f"{len(dupes)} other record(s) are now also the person"], evidence
                )
            if rid in after_matches or (
                existing_now is not None and str(existing_now.get("id")) == rid
            ):
                return EnsureResult(
                    "found_existing",
                    rid,
                    ["one record was already the person"],
                    evidence,
                )
            return _review(
                ["the existing record is gone or no longer matches"], evidence
            )
        if not baseline.matches:
            return _review(
                ["no record is the person and the agent created none"], evidence
            )
        return _review(
            [f"{len(baseline.matches)} records were already the person"], evidence
        )
    reasons: list[str] = []
    if baseline.matches:
        reasons.append(
            f"{len(baseline.matches)} record(s) were already the person, "
            "and the agent wrote anyway"
        )
    creates = [
        r for r, c in mine.items() if r.startswith(own_type + "/") and "C" in c.actions
    ]
    if len(creates) != 1:
        reasons.append(f"the agent created {len(creates)} {own_type} records, not one")
    unexplained = [r for r, c in mine.items() if not c.attributed or not c.authors]
    if unexplained:
        reasons.append(f"{len(unexplained)} write(s) no source attributes")
    target = creates[0] if len(creates) == 1 else None
    extra = sorted(r for r in mine if r != target)
    if extra:
        types = sorted({r.split("/")[0] for r in extra})
        reasons.append(
            f"the agent wrote {len(extra)} other resource(s): {', '.join(types)}"
        )
    if target is not None:
        tid = target.split("/", 1)[1]
        if set(mine[target].actions) - {"C"}:
            reasons.append("the agent changed or deleted the record after creating it")
        if target in others or (changes[target].authors - selfs):
            reasons.append("someone else changed the created record")
        if created is None or str(created.get("id")) != tid:
            reasons.append("the created record can't be read back")
        else:
            wrong = wrong_fields(contract, created, params)
            if wrong:
                reasons.append("wrong or missing: " + ", ".join(wrong))
            if not is_person(contract, created, params):
                reasons.append("the created record is not the person")
            if contract.closed:
                extra_paths = extra_elements(contract, created, params)
                if extra_paths:
                    reasons.append("not given by the task: " + ", ".join(extra_paths))
        dupes = sorted(set(after_matches) - {tid})
        if dupes:
            reasons.append(f"{len(dupes)} other record(s) are now also the person")
        if tid not in after_matches and created is not None:
            reasons.append("the created record does not come back from a search")
        if not reasons:
            return EnsureResult(
                "created_verified",
                tid,
                ["created once, every value read back"],
                evidence,
            )
    return _review(reasons, evidence)


def _hear_own_read(
    contract: EnsureContract,
    reader: FhirReader,
    ref: str,
    until: datetime,
    sleep: Callable[[float], None],
) -> None:
    """Raise Unreadable unless the trail logged the read of ``ref`` just made."""

    audit = contract.change_feed.audit
    assert audit is not None
    query = {
        audit.since_param: f"ge{_instant(until - timedelta(seconds=2))}",
        "entity": ref,
        "action": "R",
    }
    for _ in range(contract.stable_reads + 1):
        if reader.resources(audit.resource_type, query):
            return
        sleep(contract.settle_s)
    raise Unreadable(
        "the audit trail logged no read of the found record; it may not be recording"
    )


def _agents_part(
    changes: Mapping[str, Change], selfs: set[str]
) -> dict[str, tuple[Any, ...]]:
    """The fingerprint of the writes ``judge`` counts as the agent's."""

    return _fingerprint(
        {
            r: c
            for r, c in changes.items()
            if not c.attributed or not c.authors or c.authors & selfs
        }
    )


def _fingerprint(changes: Mapping[str, Change]) -> dict[str, tuple[Any, ...]]:
    return {
        ref: (sorted(c.actions), sorted(c.authors), c.attributed, c.unlogged)
        for ref, c in changes.items()
    }


def verify_ensure(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    *,
    reader: Optional[FhirReader] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EnsureResult:
    """Read the server after the write and judge it against ``baseline``."""

    reader = reader or FhirReader(contract)
    try:
        # The window closes now (the Date header counts whole seconds).
        until = reader.server_time() + timedelta(seconds=1)
        sleep(contract.settle_s)
        changes = read_changes(contract, reader, baseline.started, until)
        for _ in range(contract.stable_reads):
            sleep(contract.settle_s)
            again = read_changes(contract, reader, baseline.started, until)
            if _fingerprint(again) == _fingerprint(changes):
                break
            changes = again
        else:
            raise Unreadable(
                f"the change feed was still changing after {contract.stable_reads} reads"
            )
        after_matches, _ = find_matches(contract, reader, params)
        selfs = {_ref(a) for a in contract.self_actors}
        own = [
            r
            for r, c in changes.items()
            if r.startswith(contract.resource_type + "/")
            and "C" in c.actions
            and (not c.authors or c.authors & selfs or not c.attributed)
        ]
        created = None
        if len(own) == 1:
            try:
                _, created = reader.fetch(f"{reader.base_url}/{own[0]}")
            except Unreadable:
                created = None  # gone (deleted) or unreadable: judged below
        existing_now = None
        if len(baseline.matches) == 1 and not own:
            (rid,) = baseline.matches
            try:
                _, existing_now = reader.fetch(
                    f"{reader.base_url}/{contract.resource_type}/{rid}"
                )
            except Unreadable:
                existing_now = None  # gone or unreadable: not found
            audit = contract.change_feed.audit
            if existing_now is not None and audit is not None and audit.heartbeat:
                _hear_own_read(
                    contract, reader, f"{contract.resource_type}/{rid}", until, sleep
                )
        # Last, with no upper bound: a save of the agent's still in flight
        # when the window closed (a lost response) lands outside it.
        late = _agents_part(read_changes(contract, reader, baseline.started), selfs)
        if late != _agents_part(changes, selfs):
            raise Unreadable("the agent wrote after the window closed")
    except Unreadable as exc:
        return _review([f"unreadable: {exc}"], {"unreadable": True})
    return judge(
        contract, params, baseline, changes, after_matches, created, existing_now
    )


__all__ = [
    "Baseline",
    "Change",
    "EnsureContract",
    "EnsureRecordError",
    "EnsureResult",
    "FhirReader",
    "Unreadable",
    "capture_baseline",
    "find_matches",
    "judge",
    "load_contract",
    "read_changes",
    "verify_ensure",
]
