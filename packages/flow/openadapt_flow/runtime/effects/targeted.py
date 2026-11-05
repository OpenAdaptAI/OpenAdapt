"""Receipt-bound late-read facts. Raw identities and bodies stay in memory.

Readers produce these observations only after validating the existing issued
recheck state, contract, actor, parameters and target identity commitments.
These objects are not an authority or a serialized receipt format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ensure_record import EnsureContract
from openadapt_flow.runtime.effects.effect import ValueExpr

OUTSIDE_INTERMEDIATE_HISTORY = (
    "intermediate writes or deletions outside receipt-bound records and "
    "the plan's identifying-search observations: UNKNOWN"
)

HistoryStatus = Literal["complete", "incomplete", "unavailable"]
CurrentStatus = Literal["present", "absent", "unreadable"]
TargetedStatus = Literal["held", "refuted", "not_checked"]


@dataclass(frozen=True)
class SearchObservation:
    """The same schema-key-bound identifying search at write and late read.

    matched_ids are canonical logical Type/id references. Their origin and
    resource type must be bound by the validated contract and issued state.
    Counts never establish wider historical coverage.
    """

    contract_sha256: str
    schema_sha256: str
    matched_ids: frozenset[str] = field(repr=False)
    candidate_count: Optional[int]
    complete: bool
    observed_at: str


@dataclass(frozen=True)
class VersionObservation:
    """One authenticated version after the receipt's exact written anchor.

    by_run is None unless server provenance and the acting identity binding
    establish membership. Unknown provenance is never another-user evidence.
    A version-specific reference pin is distinct from logical record identity.
    """

    version_id: str = field(repr=False)
    operation: Literal["write", "delete"]
    body: Optional[Mapping[str, Any]] = field(repr=False)
    author: Optional[str] = field(repr=False)
    author_trusted: bool
    by_run: Optional[bool]
    occurred_at: Optional[str]
    matches_receipt: Optional[bool]


@dataclass(frozen=True)
class TargetObservation:
    """Current and per-record history facts for one exact issued target.

    identity is canonical logical Type/id, never display text. The target MAC
    also binds its source/surface. complete means all versions are anchored to
    the exact issued version/content and ordered/bounded through read_at;
    gaps, changed anchors and ambiguous ordering are incomplete. An unavailable
    history is named explicitly, never represented by an empty complete tuple.
    """

    identity: str = field(repr=False)
    target_id_mac: str
    receipt_bound: bool
    current_status: CurrentStatus
    matches_receipt: Optional[bool]
    current: Optional[Mapping[str, Any]] = field(repr=False)
    history_status: HistoryStatus
    history_anchor_bound: bool
    versions: tuple[VersionObservation, ...] = field(repr=False)


@dataclass(frozen=True)
class TargetedObservation:
    """Memory-only facts shared with the fixed targeted invariant judge.

    Positive wider historical absence cannot follow from these observations.
    Every receipt using this domain names OUTSIDE_INTERMEDIATE_HISTORY even
    when all in-scope observations are complete.
    """

    receipt_sha256: str
    targets: tuple[TargetObservation, ...] = field(repr=False)
    write_search: SearchObservation = field(repr=False)
    late_search: SearchObservation = field(repr=False)
    read_at: str
    history_required: bool = True
    phase: Literal["write", "late"] = "late"


@dataclass(frozen=True)
class ReceiptObservation:
    """A complete declared native record read, without an invented search.

    Produced from an existing SQL projection or an already-declared database
    snapshot. These commitments do not establish undeclared schema, side-record
    or intermediate-history coverage and cannot issue recheck authority.
    """

    contract_sha256: str
    source_sha256: str
    target_id_macs: tuple[str, ...]
    read_at: str
    receipt_sha256: str = ""
    phase: Literal["write", "late"] = "write"
    scope: Literal["declared_projection", "declared_database"] = "declared_projection"
    schema_sha256: Optional[str] = None
    identity_binding: Literal["declared_identity", "projected_tuple", "native_primary_key"] = "declared_identity"


# The issued post-state contains these commitments, never the raw observations.
TARGETED_SCHEMA = "openadapt.targeted-recheck/v1"
COMPARES = "receipt_records_and_identifying_search"
UNRESOLVED_TARGET_IDENTITY = "independent_target_identity_unknown"


class TargetedUnreadable(ValueError):
    """A required targeted observation could not be established."""


def _digest(label: str, value: Any, salt: bytes) -> str:
    from openadapt_flow.verify_run import salted_digest

    return salted_digest(salt, "targeted:" + label, value)


def _sha(value: Any) -> str:
    from openadapt_flow.verify_run import sha256_tagged

    return sha256_tagged(value)


def _identity(value: str, resource_type: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*/[A-Za-z0-9.-]{1,64}", value):
        raise TargetedUnreadable("target identity is not a logical Type/id")
    if value.split("/", 1)[1] in (".", ".."):
        raise TargetedUnreadable("target identity is a path segment")
    if value.split("/", 1)[0] != resource_type:
        raise TargetedUnreadable("target identity names another resource type")
    return value


def _project(reader: Any, body: Mapping[str, Any], ignore: list[str]) -> dict[str, Any]:
    return {
        k: v
        for k, v in reader._flatten(dict(body)).items()
        if k not in ignore and k not in reader.drop_fields
    }


def _matches_receipt(
    spec: Any,
    reader: Any,
    resource: Mapping[str, Any],
    bound: Mapping[str, Any],
    salt: bytes,
    ignore: list[str],
    params: Mapping[str, object],
) -> Optional[bool]:
    """Keep exact anchor binding while reusing the internal Ensure field judge.

    This helper is used only after the producer's issued-state/locator checks.
    It accepts no public callback, and never replaces a raw body or its MAC.
    """
    from openadapt_flow.ensure_bridge import EnsureEffectVerifier
    from openadapt_flow.ensure_record import Unreadable
    from openadapt_flow.fhir_reference import ReferenceUnreadable

    if not isinstance(reader, EnsureEffectVerifier):
        return (
            _digest("contents", _project(reader, resource, ignore), salt)
            == bound["contents_mac"]
        )
    if reader.binding.contract != spec.search or dict(params) != reader.params:
        raise TargetedUnreadable(
            "ensure field predicate names another contract or parameters"
        )
    if not bound["version_mac"] or not bound["anchor_mac"]:
        return None
    meta = resource.get("meta") or {}
    version = meta.get("versionId") if isinstance(meta, Mapping) else None
    if not isinstance(version, str) or not version:
        return None
    if (
        _digest("version", version, salt) == bound["version_mac"]
        and _digest("anchor", resource, salt) != bound["anchor_mac"]
    ):
        return None
    try:
        references = reader._targeted_reference_evidence([resource], params)
        return reader._matches_targeted_resource(resource, params, references)
    except (Unreadable, ReferenceUnreadable):
        return None


def _transport(reader: Any, url: str, params: Any = None) -> tuple[int, Any, Any]:
    """Use the existing adapter's authentication, TLS and redirect boundary."""
    from urllib.parse import urlsplit

    base = urlsplit(reader.base_url)
    target = urlsplit(url)
    if (
        (base.scheme, base.netloc) != (target.scheme, target.netloc)
        or not target.path.startswith(base.path.rstrip("/") + "/")
        or target.fragment
        or target.username
        or target.password
    ):
        raise TargetedUnreadable("targeted read crossed the configured source")
    headers = reader._headers()
    if headers is None:
        raise TargetedUnreadable("targeted read authentication is unavailable")
    session = reader._get_session()
    kw: dict[str, Any] = {
        "headers": {**headers, "Cache-Control": "no-cache, no-store"},
        "timeout": reader.timeout_s,
    }
    if reader._verify_per_request:
        kw["verify"] = reader.verify_tls
    if (type(session).__module__ or "").startswith("requests"):
        kw["allow_redirects"] = False
    else:
        kw["follow_redirects"] = False
    response = session.get(url, params=params, **kw)
    if response.status_code == 401 and reader.token_source is not None:
        reader.token_source.invalidate()
    landed = str(getattr(response, "url", url))
    if landed != url and landed.split("?", 1)[0] != url.split("?", 1)[0]:
        raise TargetedUnreadable("targeted read redirected")
    return response.status_code, response.headers, response.json()


def _reader(spec: Any, verifier: Any) -> Any:
    from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier

    reader = verifier
    if spec.surface is not None:
        reader = getattr(verifier, "verifiers", {}).get(spec.surface)
    if not isinstance(reader, FhirEffectVerifier):
        raise TargetedUnreadable("schema-key identifying search is unavailable")
    if (
        reader.base_url.rstrip("/") != spec.search.base_url.rstrip("/")
        or reader.resource_type != spec.search.resource_type
    ):
        raise TargetedUnreadable("search and record read name different sources")
    return reader


def _search(
    spec: Any, reader: Any, params: Mapping[str, object], at: str
) -> SearchObservation:
    from openadapt_flow.ensure_record import FhirReader, find_matches

    client = FhirReader(
        spec.search, get=lambda url, query: _transport(reader, url, query)
    )
    matches, count = find_matches(spec.search, client, params)
    from openadapt_flow.verify_run import _now

    at = _now()
    identities = frozenset(
        _identity(f"{reader.resource_type}/{i}", reader.resource_type) for i in matches
    )
    return SearchObservation(
        _sha(spec.search.model_dump(mode="json", by_alias=True)),
        "sha256:" + spec.search.reference_schema.sha256,
        identities,
        count,
        True,
        at,
    )


def _sealed_search(search: SearchObservation, salt: bytes) -> dict[str, Any]:
    if not search.complete or search.candidate_count is None:
        raise TargetedUnreadable(
            "an incomplete identifying search cannot be sealed as the written search"
        )
    return {
        "contract_sha256": search.contract_sha256,
        "schema_sha256": search.schema_sha256,
        "matched_id_macs": sorted(_digest("id", i, salt) for i in search.matched_ids),
        "candidate_count": search.candidate_count,
        "complete": search.complete,
        "observed_at": search.observed_at,
    }


def _current(spec: Any, reader: Any, identity: str) -> Optional[Mapping[str, Any]]:
    _identity(identity, reader.resource_type)
    status, _, body = _transport(reader, f"{reader.base_url}/{identity}")
    if status in (404, 410) and spec.not_found_qualified:
        if isinstance(body, Mapping) and body.get("resourceType") == "OperationOutcome":
            return None
    if status != 200 or not isinstance(body, Mapping):
        raise TargetedUnreadable("receipt-bound record could not be read")
    if f"{body.get('resourceType')}/{body.get('id')}" != identity:
        raise TargetedUnreadable("record read returned another identity")
    return body


def capture_targeted(
    spec: Any,
    verifier: Any,
    targets: list[Mapping[str, Any]],
    params: Mapping[str, object],
    salt: bytes,
    ignore: list[str],
    at: str,
) -> tuple[dict[str, Any], tuple[str, ...], TargetedStatus]:
    """Bind independently matched plan targets; keep their locators in memory.

    Missing history anchors still permit a read-only reconciliation obligation,
    but never a history claim. No caller-supplied locator creates a target.
    """
    reader = _reader(spec, verifier)
    if not targets:
        raise TargetedUnreadable("no independent landed target evidence")
    search = _search(spec, reader, params, at)
    entries = []
    identities = []
    for target in targets:
        raw_id = target.get("id")
        identity = _identity(f"{reader.resource_type}/{raw_id}", reader.resource_type)
        if identity in identities:
            continue
        identities.append(identity)
        projected = {k: v for k, v in target.items() if k not in ignore}
        body = _current(spec, reader, identity)
        anchor = body is not None and _project(reader, body, ignore) == projected
        meta = body.get("meta") or {} if body is not None else {}
        version = meta.get("versionId") if isinstance(meta, Mapping) else None
        entries.append(
            {
                "id_mac": _digest("id", identity, salt),
                "contents_mac": _digest("contents", projected, salt),
                "version_mac": _digest("version", str(version), salt)
                if anchor and version
                else None,
                "anchor_mac": _digest("anchor", body, salt)
                if anchor and version
                else None,
            }
        )
    # A successful empty query cannot contradict an independently read target.
    if not set(identities) <= search.matched_ids:
        raise TargetedUnreadable("identifying search omitted a receipt-bound target")
    post = {
        "schema_version": TARGETED_SCHEMA,
        "targets": sorted(entries, key=lambda x: x["id_mac"]),
        "search": _sealed_search(search, salt),
        "ignore_fields": list(ignore),
        "params_mac": _digest("params", dict(params), salt),
    }
    status: TargetedStatus = (
        "refuted"
        if len(search.matched_ids) > 1
        else "not_checked"
        if spec.history_required
        and (
            not spec.history_available
            or not spec.history_retention_qualified
            or any(e["anchor_mac"] is None for e in entries)
        )
        else "held"
    )
    return post, tuple(identities), status


def unresolved_targeted(
    params: Mapping[str, object],
    salt: bytes,
    initial_frame_status: str,
) -> dict[str, Any]:
    """Keep an unknown target as an owed observation, never invented authority.

    The canonical scheduler binds the receipt and deadline independently. This
    closed post-state contains no search success, guessed ID or landed claim.
    Even caller-supplied locators cannot make this state observation-eligible.
    """
    if initial_frame_status not in ("held", "refuted", "not_checked"):
        raise TargetedUnreadable("the initial frame status is malformed")
    return {
        "schema_version": TARGETED_SCHEMA,
        "status": "unresolved",
        "reason": UNRESOLVED_TARGET_IDENTITY,
        "targets": [],
        "params_mac": _digest("params", dict(params), salt),
        "initial_frame_status": initial_frame_status,
    }


def targeted_state_problem(value: Any) -> Optional[str]:
    """Validate closed commitment fields before any locator or network read."""
    import re

    from openadapt_flow.runtime.effects.late_write import parse_time

    mac = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
    sha = re.compile(r"^sha256:[0-9a-f]{64}$")
    if isinstance(value, Mapping) and value.get("status") == "unresolved":
        if set(value) != {
            "schema_version",
            "status",
            "reason",
            "targets",
            "params_mac",
            "initial_frame_status",
        }:
            return "the unresolved targeted post-state has unknown or missing fields"
        if (
            value["schema_version"] != TARGETED_SCHEMA
            or value["reason"] != UNRESOLVED_TARGET_IDENTITY
            or value["targets"] != []
        ):
            return "the unresolved target identity claim is malformed"
        if value["initial_frame_status"] not in ("held", "refuted", "not_checked"):
            return "the initial frame status is malformed"
        if not isinstance(value["params_mac"], str) or not mac.fullmatch(
            value["params_mac"]
        ):
            return "the targeted parameters are not bound"
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "targets",
        "search",
        "ignore_fields",
        "params_mac",
        "initial_frame_status",
    }:
        return "the targeted post-state has unknown or missing fields"
    if value["initial_frame_status"] not in ("held", "refuted", "not_checked"):
        return "the initial frame status is malformed"
    if value["schema_version"] != TARGETED_SCHEMA:
        return "the targeted post-state schema is unknown"
    if not isinstance(value["params_mac"], str) or not mac.fullmatch(
        value["params_mac"]
    ):
        return "the targeted parameters are not bound"
    targets = value["targets"]
    if not isinstance(targets, list) or not targets:
        return "the targeted post-state has no independently bound targets"
    seen = set()
    for target in targets:
        if not isinstance(target, Mapping) or set(target) != {
            "id_mac",
            "contents_mac",
            "version_mac",
            "anchor_mac",
        }:
            return "the targeted commitments are malformed"
        if any(
            not isinstance(target[k], str) or not mac.fullmatch(target[k])
            for k in ("id_mac", "contents_mac")
        ):
            return "the targeted commitments are not keyed MACs"
        if target["id_mac"] in seen:
            return "the targeted commitments repeat an identity"
        seen.add(target["id_mac"])
        if (target["version_mac"] is None) != (target["anchor_mac"] is None):
            return "the target anchor is only partly bound"
        if any(
            target[k] is not None
            and (not isinstance(target[k], str) or not mac.fullmatch(target[k]))
            for k in ("version_mac", "anchor_mac")
        ):
            return "the target anchor commitments are malformed"
    search = value["search"]
    if not isinstance(search, Mapping) or set(search) != {
        "contract_sha256",
        "schema_sha256",
        "matched_id_macs",
        "candidate_count",
        "complete",
        "observed_at",
    }:
        return "the identifying search observation is malformed"
    if any(
        not isinstance(search[k], str) or not sha.fullmatch(search[k])
        for k in ("contract_sha256", "schema_sha256")
    ):
        return "the identifying search is not schema bound"
    ids = search["matched_id_macs"]
    if (
        not isinstance(ids, list)
        or any(not isinstance(i, str) or not mac.fullmatch(i) for i in ids)
        or len(ids) != len(set(ids))
    ):
        return "the identifying search identities are malformed"
    if (
        not isinstance(search["complete"], bool)
        or isinstance(search["candidate_count"], bool)
        or not isinstance(search["candidate_count"], int)
        or search["candidate_count"] < len(ids)
    ):
        return "the identifying search coverage is malformed"
    if not seen <= set(ids):
        return "the identifying search does not include the written targets"
    try:
        parse_time(search["observed_at"])
    except (ValueError, TypeError):
        return "the identifying search time is malformed"
    if not isinstance(value["ignore_fields"], list) or any(
        not isinstance(k, str) for k in value["ignore_fields"]
    ):
        return "the target projection is malformed"
    return None


def _versions(
    spec: Any,
    reader: Any,
    identity: str,
    bound: Mapping[str, Any],
    salt: bytes,
    ignore: list[str],
    current: Optional[Mapping[str, Any]],
    params: Mapping[str, object],
    run_actor: str,
) -> tuple[bool, tuple[VersionObservation, ...]]:
    """A bounded single-page retained history anchored to both exact endpoints.

    Source retention/actor qualification is explicit setup evidence. A total,
    HTTP 200 or an empty page alone never supplies that qualification.
    Multi-page or unstable reads remain incomplete; no offset frontier is invented.
    """
    from openadapt_flow.runtime.effects.history import _path, instant

    if (
        not spec.history_retention_qualified
        or not bound["anchor_mac"]
        or current is None
    ):
        return False, ()
    url = f"{reader.base_url}/{identity}/_history"
    bodies = []
    for _ in range(2):
        status, _, body = _transport(reader, url, {"_count": str(spec.max_versions)})
        if (
            status != 200
            or not isinstance(body, Mapping)
            or body.get("type") != "history"
            or body.get("resourceType") != "Bundle"
        ):
            return False, ()
        entries = body.get("entry")
        total = body.get("total")
        links = body.get("link", [])
        if (
            not isinstance(entries, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total != len(entries)
            or total > spec.max_versions
            or not isinstance(links, list)
            or any(
                not isinstance(x, Mapping) or x.get("relation") == "next" for x in links
            )
        ):
            return False, ()
        bodies.append(entries)
    if bodies[0] != bodies[1] or _current(spec, reader, identity) != current:
        return False, ()
    anchor_index = None
    events = []
    seen = set()
    for entry in bodies[0]:
        resource = entry.get("resource") if isinstance(entry, Mapping) else None
        request = entry.get("request") or {} if isinstance(entry, Mapping) else {}
        if not isinstance(resource, Mapping) or not isinstance(request, Mapping):
            return False, ()  # resource-less tombstone has unknown version/actor
        if f"{resource.get('resourceType')}/{resource.get('id')}" != identity:
            return False, ()
        meta = resource.get("meta") or {}
        version = meta.get("versionId") if isinstance(meta, Mapping) else None
        if not isinstance(version, str) or not version or version in seen:
            return False, ()
        seen.add(version)
        try:
            at = instant(meta.get("lastUpdated"))
        except (ValueError, TypeError):
            return False, ()
        operation: Literal["write", "delete"] = (
            "delete" if request.get("method") == "DELETE" else "write"
        )
        events.append((at, str(version), resource, operation))
    from openadapt_flow.verify_run import _now

    if any(e[0] > instant(_now()) for e in events):
        return False, ()
    events.sort(key=lambda e: e[0])
    if len({e[0] for e in events}) != len(events):
        return False, ()
    for i, (_, version, resource, _) in enumerate(events):
        if _digest("version", version, salt) == bound["version_mac"]:
            if _digest("anchor", resource, salt) != bound["anchor_mac"]:
                return False, ()
            anchor_index = i
    if anchor_index is None or not events or events[-1][2] != current:
        return False, ()
    own = {str(v.resolve(params)) for v in spec.self_actors}
    if run_actor:
        own.add(run_actor)
    if "" in own or "None" in own:
        return False, ()
    observed = []
    for at, version, resource, operation in events[anchor_index + 1 :]:
        author = _path(resource, spec.author_path) if spec.author_path else None
        trusted = bool(spec.author_server_set and isinstance(author, str) and author)
        observed.append(
            VersionObservation(
                version,
                operation,
                resource,
                author if isinstance(author, str) else None,
                trusted,
                author in own if trusted else None,
                at.isoformat(),
                False
                if operation == "delete"
                else _matches_receipt(
                    spec, reader, resource, bound, salt, ignore, params
                ),
            )
        )
    return True, tuple(observed)


def targeted_locators_problem(
    spec: Any,
    post: Mapping[str, Any],
    raw_locators: tuple[str, ...],
    params: Mapping[str, object],
    salt: bytes,
) -> Optional[str]:
    """Validate caller commitments before the canonical durable read fence.

    This performs no I/O and grants no authority. Canonical preparation must
    independently authenticate the issued receipt/state, actor, contract and
    due time before accepting its result. The same check is repeated by the
    reader so direct internal calls cannot skip target/parameter binding.
    """
    problem = targeted_state_problem(post)
    if problem is not None:
        return problem
    if post["params_mac"] != _digest("params", dict(params), salt):
        return "targeted parameters differ from the issued observation"
    if post.get("status") == "unresolved":
        return UNRESOLVED_TARGET_IDENTITY
    if not isinstance(raw_locators, tuple) or any(
        not isinstance(i, str) for i in raw_locators
    ):
        return "private locators are not an exact logical identity tuple"
    try:
        by_mac = {
            _digest("id", _identity(i, spec.search.resource_type), salt): i
            for i in raw_locators
        }
    except TargetedUnreadable:
        return "private locators are not exact logical identities for the issued source"
    if len(by_mac) != len(raw_locators) or set(by_mac) != {
        b["id_mac"] for b in post["targets"]
    }:
        return "private locators do not resolve the exact issued target MAC set"
    return None


def observe_targeted(
    spec: Any,
    verifier: Any,
    post: Mapping[str, Any],
    locators: tuple[str, ...],
    params: Mapping[str, object],
    salt: bytes,
    receipt_digest: str,
    at: str,
    run_actor: str,
) -> TargetedObservation:
    problem = targeted_locators_problem(spec, post, locators, params, salt)
    if problem is not None:
        raise TargetedUnreadable(problem)
    reader = _reader(spec, verifier)
    by_mac = {
        _digest("id", _identity(i, reader.resource_type), salt): i for i in locators
    }
    stored = post["search"]
    if not set(stored["matched_id_macs"]) <= set(by_mac):
        raise TargetedUnreadable("the written search includes an unexplained duplicate")
    write_ids = frozenset(by_mac[m] for m in stored["matched_id_macs"])
    write_search = SearchObservation(
        stored["contract_sha256"],
        stored["schema_sha256"],
        write_ids,
        stored["candidate_count"],
        stored["complete"],
        stored["observed_at"],
    )
    targets = []
    for bound in post["targets"]:
        identity = by_mac[bound["id_mac"]]
        try:
            current = _current(spec, reader, identity)
            status: CurrentStatus = "present" if current is not None else "absent"
            matches = (
                False
                if current is None
                else _matches_receipt(
                    spec, reader, current, bound, salt, post["ignore_fields"], params
                )
            )
            complete, versions = (
                _versions(
                    spec,
                    reader,
                    identity,
                    bound,
                    salt,
                    post["ignore_fields"],
                    current,
                    params,
                    run_actor,
                )
                if spec.history_available
                else (False, ())
            )
        except Exception:  # noqa: BLE001 - one unreadable target cannot pass
            current, status, matches, complete, versions = (
                None,
                "unreadable",
                None,
                False,
                (),
            )
        targets.append(
            TargetObservation(
                identity,
                bound["id_mac"],
                True,
                status,
                matches,
                current,
                "complete"
                if complete
                else "incomplete"
                if spec.history_available
                else "unavailable",
                complete,
                versions,
            )
        )
    from dataclasses import replace

    from openadapt_flow.verify_run import _now

    try:
        late_search = _search(spec, reader, params, at)
        if (
            late_search.contract_sha256 != stored["contract_sha256"]
            or late_search.schema_sha256 != stored["schema_sha256"]
        ):
            raise TargetedUnreadable("identifying search changed contract or schema")
        if any(
            t.current_status == "present"
            and t.matches_receipt is True
            and t.identity not in late_search.matched_ids
            for t in targets
        ):
            late_search = replace(late_search, candidate_count=None, complete=False)
    except Exception:  # noqa: BLE001 - search failure must not erase observed harm
        late_search = SearchObservation(
            stored["contract_sha256"],
            stored["schema_sha256"],
            frozenset(),
            None,
            False,
            _now(),
        )
    # Keep the original endpoint-stability requirement across the new order.
    # A changed/unreadable final endpoint blocks positive history coverage,
    # while already authenticated versions remain observed negative evidence.
    if late_search.complete:
        for i, target in enumerate(targets):
            if target.current_status == "unreadable":
                continue
            try:
                final = _current(spec, reader, target.identity)
                if final == target.current:
                    continue
                targets[i] = replace(
                    target,
                    current_status="present" if final is not None else "absent",
                    current=final,
                    matches_receipt=False
                    if final is None
                    else _matches_receipt(
                        spec,
                        reader,
                        final,
                        post["targets"][i],
                        salt,
                        post["ignore_fields"],
                        params,
                    ),
                    history_status="incomplete"
                    if spec.history_available
                    else "unavailable",
                    history_anchor_bound=False,
                )
            except Exception:  # noqa: BLE001 - never discard earlier proven harm
                targets[i] = replace(
                    target,
                    current_status="unreadable",
                    current=None,
                    matches_receipt=None,
                    history_status="incomplete"
                    if spec.history_available
                    else "unavailable",
                    history_anchor_bound=False,
                )

    return TargetedObservation(
        receipt_digest,
        tuple(targets),
        write_search,
        late_search,
        _now(),
        history_required=spec.history_required,
    )


def judge_targeted(
    observation: TargetedObservation,
) -> tuple[TargetedStatus, dict[str, Any]]:
    """Judge final state and attributed post-anchor edits, without performing I/O.

    This does not replace the fixed scope/cardinality/delete predicates. A
    trusted later colleague edit proves a separate write, not a wrong agent
    save; unknown authors never receive that treatment.
    """
    problems = []
    refuted = int(len(observation.write_search.matched_ids) > 1) + int(
        len(observation.late_search.matched_ids) > 1
    )
    if not observation.write_search.complete or not observation.late_search.complete:
        problems.append("identifying_search_unknown")
    other = 0
    for target in observation.targets:
        if not target.receipt_bound:
            problems.append("target_read_unavailable")
            continue
        if target.current_status == "unreadable":
            problems.append("target_read_unavailable")
        complete = target.history_status == "complete" and target.history_anchor_bound
        if observation.history_required and not complete:
            problems.append("required_record_history_unknown")
        if target.current_status != "unreadable" and target.matches_receipt is None:
            problems.append("target_value_unknown")
        if target.current_status != "unreadable" and target.matches_receipt is False:
            later = target.versions[-1] if complete and target.versions else None
            if later is not None and later.author_trusted and later.by_run is False:
                other += 1
            elif later is not None and later.author_trusted and later.by_run is True:
                refuted += 1
            else:
                problems.append("changed_target_provenance_unknown")
        for version in target.versions:
            if version.matches_receipt is None and observation.history_required:
                problems.append("later_value_unknown")
            if version.matches_receipt is False:
                if version.author_trusted and version.by_run is False:
                    continue
                if not version.author_trusted or version.by_run is None:
                    problems.append("later_actor_unknown")
                else:
                    refuted += 1
    if not observation.targets:
        problems.append("no_bound_targets")
    status: TargetedStatus = (
        "refuted" if refuted else "not_checked" if problems else "held"
    )
    return status, {
        "domain": COMPARES,
        "status": status,
        "phase": observation.phase,
        "future_late_search": "owed" if observation.phase == "write" else "observed",
        "targets": len(observation.targets),
        "target_mismatches": refuted,
        "legitimate_later_user_edits": other,
        "history_required": observation.history_required,
        "write_search_status": "complete"
        if observation.write_search.complete
        else "UNKNOWN",
        "late_search_status": "complete"
        if observation.late_search.complete
        else "UNKNOWN",
        "history_status": "complete"
        if all(t.history_status == "complete" for t in observation.targets)
        and observation.targets
        else "UNKNOWN/BLOCKED",
        "problems": sorted(set(problems)),
        "outside_intermediate_history": OUTSIDE_INTERMEDIATE_HISTORY,
    }


RECEIPT_RECORDS_SCHEMA = "openadapt.receipt-records/v1"


def _native_projection(record: Mapping[str, Any]) -> dict[str, list[str]]:
    """Commit exact native projected values without changing field matching."""
    from openadapt_flow.runtime.effects.db_snapshot import canonical

    return {
        name: [type(value).__name__, canonical(value)]
        for name, value in sorted(record.items())
    }


def receipt_records_problem(value: Any) -> Optional[str]:
    """Closed private commitments, never a caller-supplied completeness flag."""
    import re

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "contract_sha256", "source_sha256", "params_mac", "targets"
    }:
        return "native receipt commitments are malformed"
    if value["schema_version"] != RECEIPT_RECORDS_SCHEMA:
        return "native receipt commitment version is unsupported"
    for name in ("contract_sha256", "source_sha256"):
        if not isinstance(value[name], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[name]):
            return "native receipt digest is malformed"
    if not isinstance(value["params_mac"], str) or not re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", value["params_mac"]):
        return "native receipt parameters are malformed"
    targets = value["targets"]
    if not isinstance(targets, list) or not targets:
        return "native receipt targets are missing"
    seen: set[tuple[str, str]] = set()
    for target in targets:
        if not isinstance(target, Mapping) or set(target) != {
            "table", "row_key_mac", "row_mac", "schema"
        }:
            return "native receipt target is malformed"
        if not isinstance(target["table"], str) or not target["table"]:
            return "native receipt table is malformed"
        for name in ("row_key_mac", "row_mac"):
            if not isinstance(target[name], str) or not re.fullmatch(r"[0-9a-f]{32}", target[name]):
                return "native receipt row commitment is malformed"
        if not isinstance(target["schema"], str) or not re.fullmatch(r"[0-9a-f]{64}", target["schema"]):
            return "native receipt schema is malformed"
        identity = (target["table"], target["row_key_mac"])
        if identity in seen:
            return "native receipt target is duplicated"
        seen.add(identity)
    return None


def receipt_records_binding_problem(
    issued: Any,
    contract_sha256: str,
    source_sha256: str,
    params: Mapping[str, object],
    salt: bytes,
    previous: Any,
) -> Optional[str]:
    """Pure issued-anchor validation before the common durable read fence."""
    from openadapt_flow.runtime.effects.db_snapshot import Snapshot

    problem = receipt_records_problem(issued)
    if problem is not None:
        return problem
    if issued["contract_sha256"] != contract_sha256 or issued["source_sha256"] != source_sha256 or issued["params_mac"] != _digest("native-params", dict(params), salt):
        return "native receipt source, contract or parameters changed"
    if not isinstance(previous, Snapshot):
        return "the issued native snapshot is unavailable"
    for bound in issued["targets"]:
        old = previous.tables.get(bound["table"])
        if old is None or old.schema != bound["schema"] or old.rows.get(bound["row_key_mac"], [None])[0] != bound["row_mac"]:
            return "the native receipt row or schema is not the issued anchor"
    return None


def observe_receipt_records(
    *,
    contract_sha256: str,
    source_sha256: str,
    params: Mapping[str, object],
    salt: bytes,
    at: str,
    reads: Optional[Mapping[str, tuple[Any, Optional[list[dict[str, Any]]]]]] = None,
    targets: Sequence[tuple[str, Mapping[str, Any]]] = (),
    identity_field: Optional[str] = None,
    ignore_fields: Sequence[str] = (),
    snapshot: Any = None,
    tables: Optional[Mapping[str, str]] = None,
    ignore_columns: Optional[Mapping[str, Sequence[str]]] = None,
    issued: Optional[Mapping[str, Any]] = None,
    previous: Any = None,
    receipt_digest: str = "",
) -> tuple[ReceiptObservation, Optional[dict[str, Any]]]:
    """Bind existing complete native reads through the shared producer.

    This function performs no I/O. A SQL adapter's successful fetchall proves
    only its declared projection. A declared settled Snapshot supplies its own
    schema/key/whole-database proof; no synthetic FHIR state, search, history,
    additional query or second per-field judge is constructed.
    """
    from openadapt_flow.runtime.effects.db_snapshot import (
        Snapshot, cell_hash, key_text, mac_key, row_key_hash, unpack,
    )
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    def table_read(name: str) -> Any:
        if not isinstance(snapshot, Snapshot) or not snapshot.code_visible:
            raise TargetedUnreadable("the declared native database read is unavailable")
        table = snapshot.tables.get(name)
        if table is None or table.mode != "compare" or table.non_transactional or len(table.rows) != table.count:
            raise TargetedUnreadable("the declared native table read is incomplete")
        return table

    entries: list[dict[str, Any]] = []
    native_macs: set[str] = set()
    if issued is not None:
        problem = receipt_records_binding_problem(issued, contract_sha256, source_sha256, params, salt, previous)
        if problem is not None:
            raise TargetedUnreadable(problem)
        for bound in issued["targets"]:
            name, row_key = bound["table"], bound["row_key_mac"]
            old = previous.tables.get(name)
            current = table_read(name)
            if old is None or old.schema != bound["schema"] or current.schema != bound["schema"] or old.rows.get(row_key, [None])[0] != bound["row_mac"]:
                raise TargetedUnreadable("the native receipt row or schema is not the issued anchor")
            # Presence or absence is observed by the complete current read.
            # The unchanged frame judge adjudicates any change and its actor.
            entries.append(dict(bound))
            native_macs.add(_digest("native-id", [source_sha256, name, row_key], salt))
    else:
        if not targets or reads is None:
            raise TargetedUnreadable("no independently matched native receipt records")
        for surface, target in targets:
            adapter, records = reads.get(surface, (None, None))
            if not isinstance(adapter, SqlRecordVerifier) or records is None:
                raise TargetedUnreadable("the declared native SQL projection is unavailable")
            ignored = set(ignore_fields) - ({identity_field} if identity_field else set())
            projected = {k: v for k, v in target.items() if k not in ignored}
            wanted = _native_projection(projected)
            found = [row for row in records if _native_projection({k: v for k, v in row.items() if k not in ignored}) == wanted]
            if len(found) != 1:
                raise TargetedUnreadable("the native receipt projection is missing or ambiguous")
            if identity_field:
                if target.get(identity_field) is None:
                    raise TargetedUnreadable("the declared native identity is unavailable")
                identity: Any = [identity_field, _native_projection({identity_field: target[identity_field]})]
            else:
                identity = ["exact_projection", wanted]
            native_macs.add(_digest("native-id", [source_sha256, surface, identity], salt))
            if snapshot is None:
                continue
            names = [name for name, own in (tables or {}).items() if own == surface]
            if len(names) != 1:
                raise TargetedUnreadable("the native receipt table mapping is ambiguous")
            name = names[0]
            table = table_read(name)
            if not table.primary_key or any(target.get(k) is None for k in table.primary_key):
                raise TargetedUnreadable("the declared snapshot cannot bind the receipt key")
            row_key = row_key_hash(mac_key(salt), key_text(table.primary_key, target), name)
            stored = table.rows.get(row_key)
            cells = unpack(table.cell_columns, stored[1]) if stored is not None else {}
            ignored_columns = set((ignore_columns or {}).get(name, ())) | set((ignore_columns or {}).get("*", ()))
            if stored is None or any(k not in cells or cells[k] != cell_hash(mac_key(salt), v, name, k) for k, v in projected.items() if k not in ignored_columns):
                raise TargetedUnreadable("the native receipt projection differs from the settled row")
            entry = {"table": name, "row_key_mac": row_key, "row_mac": stored[0], "schema": table.schema}
            if entry not in entries:
                entries.append(entry)
        if entries:
            native_macs = {_digest("native-id", [source_sha256, e["table"], e["row_key_mac"]], salt) for e in entries}
    if not native_macs:
        raise TargetedUnreadable("native receipt identity is unavailable")
    checkpoint = None
    if entries:
        checkpoint = {
            "schema_version": RECEIPT_RECORDS_SCHEMA,
            "contract_sha256": contract_sha256,
            "source_sha256": source_sha256,
            "params_mac": _digest("native-params", dict(params), salt),
            "targets": sorted(entries, key=lambda e: (e["table"], e["row_key_mac"])),
        }
        problem = receipt_records_problem(checkpoint)
        if problem is not None:
            raise TargetedUnreadable(problem)
    observation = ReceiptObservation(
        contract_sha256, source_sha256, tuple(sorted(native_macs)), at,
        receipt_digest, "late" if issued is not None else "write",
        "declared_database" if entries else "declared_projection",
        _sha({e["table"]: e["schema"] for e in entries}) if entries else None,
        "native_primary_key" if entries else "declared_identity" if identity_field else "projected_tuple",
    )
    return observation, checkpoint


class FrameTargeted(BaseModel):
    """The receipt-ID, identifying-search and final-state guarantee.

    Qualification is selected before evaluation: W comes from setup behavior;
    history_retention_qualified and author_server_set require independent
    source/permission controls, never an HTTP success or declared author.
    No whole-type historical absence is asserted.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    quiet_s: float = Field(gt=0, allow_inf_nan=False)
    setup_evidence_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    search: EnsureContract
    surface: Optional[str] = None
    history_available: bool = True
    history_required: bool = True
    history_retention_qualified: bool = False
    author_path: Optional[str] = None
    author_server_set: bool = False
    self_actors: list[ValueExpr] = Field(default_factory=list, alias="self")
    not_found_qualified: bool = False
    max_versions: int = Field(default=1000, ge=1, le=1000)

    @model_validator(mode="after")
    def _qualified_shape(self) -> "FrameTargeted":
        if self.search.reference_schema is None:
            raise ValueError("targeted identifying search needs pinned schema metadata")
        if bool(self.author_path) != self.author_server_set:
            raise ValueError("targeted attribution requires a server-set author path")
        if self.author_server_set and not self.self_actors:
            raise ValueError(
                "targeted attribution needs independently bound run actors"
            )
        if not self.history_available and self.history_retention_qualified:
            raise ValueError("unavailable history cannot be qualified as retained")
        return self
