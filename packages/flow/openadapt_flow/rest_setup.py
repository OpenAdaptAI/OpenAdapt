"""Learn REST read data from declared OpenAPI semantics and independent probes.

OpenAPI shapes alone do not define pagination or filtering. The small annotation
vocabulary here makes those meanings explicit; vendor specs or tested data
extensions can supply it. Probes can refute a declaration, never invent one.
No write, model call, remote schema resolution, or application-specific rule is
performed. Observations cover reads under the supplied session only. Detached
output needs read-context revalidation before it can configure another reader.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

if TYPE_CHECKING:
    from openadapt_flow.record_check_synthesis import ReadSearch

_ROLE = "x-openadapt-read-role"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_MISSING = object()


@dataclass(frozen=True)
class ReadProbe:
    """A caller-owned sandbox and its independent full-store reader.

    ``snapshot`` must read the authoritative store independently of the HTTP
    endpoint under test. It is called before and after HTTP verification;
    unexpected changes refuse validation. ``identity`` is a synthetic query challenge,
    including an absent value. A ``snapshot_isolation`` challenge requires an
    independently arranged store change during a multi-page read; the learner
    never performs that change. Returned rows must still equal the initial
    snapshot. Values and records are not included in proof.
    """

    snapshot: Callable[[], list[dict[str, Any]]]
    identity: str | None = None
    snapshot_isolation: bool = False


@dataclass
class RestReadSetup:
    config: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def observations_match(self) -> bool:
        """The finite probes matched their independent store in the learning session.

        This does not bind a later reader, tenant, or authorization context.
        """
        return self.config is not None and not self.problems


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _rows(value: Any) -> Counter[str]:
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise ValueError("not a record list")
    return Counter(json.dumps(r, sort_keys=True, allow_nan=False) for r in value)


def _at(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _roles(schema: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    if not isinstance(schema, dict) or depth > 8:
        raise ValueError("unsupported response schema")
    if any(k in schema for k in ("$ref", "oneOf", "anyOf", "allOf")):
        raise ValueError("unresolved or ambiguous schema")
    found: dict[str, str] = {}
    role = schema.get(_ROLE)
    if role is not None:
        if role not in (
            "records",
            "matching-total",
            "next-url-null-terminal",
            "immutable-snapshot-token",
        ):
            raise ValueError("unsupported read role")
        expected = {"records": "array", "matching-total": "integer"}
        if role in expected and schema.get("type") != expected[role]:
            raise ValueError("role and type disagree")
        if role == "next-url-null-terminal" and not (
            schema.get("type") == ["string", "null"]
            and schema.get("format") == "uri-reference"
        ):
            raise ValueError("next URL must declare its null terminal")
        if role == "immutable-snapshot-token" and schema.get("type") not in (
            "string",
            "integer",
        ):
            raise ValueError("unsupported snapshot token type")
        if not prefix or (role == "records" and "." in prefix):
            raise ValueError("unsupported records location")
        found[role] = prefix
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 128:
        raise ValueError("unsupported properties")
    for name, child in properties.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("unsupported property name")
        nested = _roles(child, f"{prefix}.{name}" if prefix else name, depth + 1)
        if found.keys() & nested.keys():
            raise ValueError("ambiguous read role")
        found.update(nested)
    return found


def _read_get(session: Any, url: str, **kwargs: Any) -> Any:
    """Use the explicit redirect option of the actual client, without retries."""
    import httpx

    if isinstance(session, httpx.Client):
        kwargs.pop("verify", None)
        kwargs.pop("allow_redirects", None)
        return session.get(url, follow_redirects=False, **kwargs)
    return session.get(url, allow_redirects=False, **kwargs)


class _ContextSession:
    def __init__(self, context: RestReadContext, client: Any) -> None:
        self.context, self.client = context, client

    def get(self, url: str, **kwargs: Any) -> Any:
        if not self.context._active:
            raise ValueError("REST read context is closed")
        return _read_get(self.client, url, **kwargs)


@dataclass
class RestReadContext:
    """Caller-owned, in-process setup and consuming reader boundary.

    The factory opens the actual target client. The probe factory supplies
    independent current-store projections and any controlled setup challenges.
    Neither callable is read from candidate data. Each build revalidates with
    one client and uses that same client for consumption. This context must
    remain open until baseline and verification finish; detached data cannot
    restore it. No principal identity or durable authorization is inferred.
    """

    specification: Mapping[str, Any]
    base_url: str
    path: str
    session_factory: Callable[[], Any]
    probes: Callable[[], list[ReadProbe]]
    identity_parameter: str | None = None
    run_parameter: str = "record_identity"
    consistency: str | None = None
    quiescence: Any = None
    _active: bool = field(default=False, init=False, repr=False)
    _clients: list[Any] = field(default_factory=list, init=False, repr=False)

    def __enter__(self) -> RestReadContext:
        if self._active:
            raise ValueError("REST read context is already open")
        self._active = True
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._active = False
        clients, self._clients = self._clients, []
        first: Exception | None = None
        for client in clients:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - close every owned client
                first = first or exc
        if first is not None and not _exc[0]:
            raise first

    def build(self, cfg: EffectsConfig, params: Mapping[str, object] | None) -> Any:
        if not self._active or cfg.records_read_context != "revalidate":
            raise ValueError("an open REST read context is required")
        # Capture the exact candidate before any caller hook. A candidate can
        # request revalidation but cannot choose the factory or independent store.
        candidate = cfg.model_copy(deep=True)
        client = self.session_factory()
        if client is None:
            raise ValueError("REST read context opened no client")
        self._clients.append(client)
        fresh = learn_rest_read(
            self.specification,
            base_url=self.base_url,
            path=self.path,
            probes=self.probes(),
            session=client,
            identity_parameter=self.identity_parameter,
            run_parameter=self.run_parameter,
            consistency=self.consistency,
            quiescence=self.quiescence,
        )
        if not fresh.observations_match or fresh.config is None:
            raise ValueError("REST read context revalidation failed")
        # Only the content-only twin generated by synthesis may drop the native
        # ID. No other config/field/auth change inherits the observed contract.
        if candidate.records_drop_fields not in ([], ["id"]):
            raise ValueError("REST read context has unsupported projection")
        expected = EffectsConfig.model_validate(fresh.config).model_copy(
            update={
                "records_read_context": "revalidate",
                "records_drop_fields": list(candidate.records_drop_fields),
            }
        )
        if candidate.model_dump(mode="json") != expected.model_dump(mode="json"):
            raise ValueError("REST candidate differs from the current read contract")
        unbound = candidate.model_copy(update={"records_read_context": None})
        verifier = build_effect_verifier(
            unbound,
            params,
            session=_ContextSession(self, client),
            quiescence=self.quiescence,
        )
        if verifier is None:
            raise ValueError("REST read context built no verifier")
        return verifier


class _ObservedSession:
    """Audit responses while the ordinary verifier owns requests and paging."""

    def __init__(
        self,
        session: Any,
        base: str,
        roles: Mapping[str, str],
        snapshot_type: str | None = None,
        *,
        path: str,
    ) -> None:
        self.session, self.base, self.roles = session, base, roles
        self.snapshot_type = snapshot_type
        self.path = path
        self.urls: set[str] = set()
        self.total: int | None = None
        self.pages = 0

    def begin_read_pass(self) -> None:
        self.urls.clear()
        self.total = None

    def get(self, url: str, **kwargs: Any) -> Any:
        if (
            urlsplit(url).netloc != urlsplit(self.base).netloc
            or urlsplit(url).scheme != urlsplit(self.base).scheme
        ):
            raise ValueError("off-origin read")
        if urlsplit(url).path != self.path or urlsplit(url).fragment:
            raise ValueError("read leaves the declared endpoint path")
        if url in self.urls:
            raise ValueError("repeated page")
        self.urls.add(url)
        # requests and httpx both support explicit redirect refusal, with
        # different keyword names. The caller supplies a requests-style client.
        response = _read_get(self.session, url, **kwargs)
        if response.status_code != 200:
            raise ValueError("unexpected HTTP status")
        body = response.json()
        records = _at(body, self.roles["records"])
        _rows(records)
        total = _at(body, self.roles["matching-total"])
        if type(total) is not int or total < 0:
            raise ValueError("missing or invalid total")
        if self.total is not None and self.total != total:
            raise ValueError("contradictory totals")
        self.total = total
        if "next-url-null-terminal" in self.roles:
            following = _at(body, self.roles["next-url-null-terminal"])
            if following is not None and (
                not isinstance(following, str) or not following
            ):
                raise ValueError("missing or invalid next URL")
        if self.snapshot_type is not None:
            token = _at(body, self.roles["immutable-snapshot-token"])
            expected_type = str if self.snapshot_type == "string" else int
            if type(token) is not expected_type:
                raise ValueError("snapshot token contradicts declared type")
        self.pages += 1
        return response


def learn_rest_read(
    specification: Mapping[str, Any],
    *,
    base_url: str,
    path: str,
    probes: list[ReadProbe],
    session: Any,
    identity_parameter: str | None = None,
    run_parameter: str = "record_identity",
    consistency: str | None = None,
    quiescence: Any = None,
) -> RestReadSetup:
    """Propose ordinary verifier data after declarations survive finite probes.

    Supported OpenAPI 3.1 response property annotations are ``records``,
    ``matching-total`` (the complete matching count, repeated on every page),
    and ``next-url-null-terminal``. Paged candidates also need a declared
    ``immutable-snapshot-token`` and a successful independent isolation
    challenge. An observed constant value never supplies these semantics.
    A query parameter may declare
    ``x-openadapt-equals-field`` naming a top-level string record field.
    Unknown semantics, cursor tokens and incomplete probes remain drafts.
    The explicit base/path/session are the caller's authorized read scope;
    spec servers and remote references are never fetched. The result is a draft;
    no durable authenticated read-context binding is established by these inputs.
    """
    result = RestReadSetup()
    try:
        result.evidence = {
            "specification_sha256": _digest(specification),
            "scope": "learning-session-current-read",
            "status": "draft",
            "read_context": "revalidation-required",
            "probe_counts": [],
        }
        origin = urlsplit(base_url)
        if (
            origin.scheme not in ("http", "https")
            or not origin.netloc
            or origin.username
            or origin.password
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            raise ValueError("base must be an explicit origin")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(c in path for c in "?#{}")
        ):
            raise ValueError("unsupported endpoint path")
        if specification.get("openapi") != "3.1.0":
            raise ValueError("unsupported specification version")
        operation = specification["paths"][path]["get"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        roles = _roles(schema)
        if not {"records", "matching-total"} <= roles.keys():
            raise ValueError("explicit records and exhaustive total semantics required")
        config: dict[str, Any] = {
            "kind": "rest",
            "base_url": base_url.rstrip("/"),
            "records_path": path,
            "records_key": roles["records"],
            "records_total_path": roles["matching-total"],
        }
        paged = "next-url-null-terminal" in roles
        snapshot_type = None
        if consistency not in (None, "quiescent-two-pass"):
            raise ValueError("unsupported consistency mode")
        if consistency is not None:
            from openadapt_flow.runtime.effects.quiescence import QuiescenceAuthority

            if (
                not paged
                or "immutable-snapshot-token" in roles
                or not isinstance(quiescence, QuiescenceAuthority)
            ):
                raise ValueError(
                    "declared quiescent paging requires an actual exclusion authority"
                )
            config["records_next_path"] = roles["next-url-null-terminal"]
            config["records_consistency"] = consistency
        elif paged:
            if "immutable-snapshot-token" not in roles:
                raise ValueError("declared immutable snapshot semantics required")
            if "records_snapshot_path" not in EffectsConfig.model_fields:
                raise ValueError("runtime snapshot contract unavailable")
            config["records_next_path"] = roles["next-url-null-terminal"]
            config["records_snapshot_path"] = roles["immutable-snapshot-token"]
            token_schema = schema
            for segment in roles["immutable-snapshot-token"].split("."):
                token_schema = token_schema["properties"][segment]
            snapshot_type = token_schema["type"]
        identity_field = None
        if identity_parameter is not None:
            if not _NAME.fullmatch(identity_parameter) or not _NAME.fullmatch(
                run_parameter
            ):
                raise ValueError("unsupported identity parameter")
            candidates = [
                p
                for p in operation.get("parameters", [])
                if p.get("name") == identity_parameter and p.get("in") == "query"
            ]
            if (
                len(candidates) != 1
                or candidates[0].get("schema", {}).get("type") != "string"
            ):
                raise ValueError("ambiguous identity query")
            identity_field = candidates[0].get("x-openadapt-equals-field")
            if not isinstance(identity_field, str) or not _NAME.fullmatch(
                identity_field
            ):
                raise ValueError("query equality semantics required")
            config["records_path"] += (
                "?" + quote(identity_parameter, safe="") + "={" + run_parameter + "}"
            )
            config["path_params"] = {run_parameter: {"param": run_parameter}}
        if not probes or len(probes) > 32:
            raise ValueError("bounded independent probes required")
        present: set[str] = set()
        absent = False
        isolation_observed = False
        for probe in probes:
            if type(probe.snapshot_isolation) is not bool:
                raise ValueError("typed isolation challenge required")
            before = probe.snapshot()
            before_signature = _rows(before)
            expected = before
            params: dict[str, str] = {}
            if identity_field is not None:
                if type(probe.identity) is not str or any(
                    type(r.get(identity_field)) is not str for r in before
                ):
                    raise ValueError("typed identity challenges required")
                expected = [r for r in before if r[identity_field] == probe.identity]
                params[run_parameter] = probe.identity
                if expected:
                    present.add(probe.identity)
                else:
                    absent = True
            elif probe.identity is not None:
                raise ValueError("identity probe without equality semantics")
            observed = _ObservedSession(
                session, base_url, roles, snapshot_type, path=path
            )
            verifier = build_effect_verifier(
                EffectsConfig.model_validate(config),
                params,
                session=observed,
                quiescence=quiescence,
            )
            if verifier is None:
                raise ValueError("generated verifier unavailable")
            state = verifier.capture_pre_state()
            after = probe.snapshot()
            changed = _rows(after) != before_signature
            if probe.snapshot_isolation:
                after_matching = (
                    after
                    if identity_field is None
                    else [r for r in after if r.get(identity_field) == probe.identity]
                )
                matching_changed = _rows(after_matching) != _rows(expected)
                if (
                    not paged
                    or not changed
                    or not matching_changed
                    or observed.pages < 2
                ):
                    raise ValueError(
                        "snapshot challenge did not exercise concurrent multi-page reads"
                    )
                isolation_observed = True
            elif changed:
                raise ValueError("independent store changed during ordinary probe")
            if not state.reachable or _rows(state.records) != _rows(expected):
                raise ValueError("generated read disagrees with independent store")
            result.evidence["probe_counts"].append(
                {
                    "expected": len(expected),
                    "returned": len(state.records),
                    "pages": observed.pages,
                }
            )
        if identity_field is not None and (len(present) < 2 or not absent):
            raise ValueError(
                "two distinct present identities and an absent challenge required"
            )
        if paged and consistency is None and not isolation_observed:
            raise ValueError("independent snapshot isolation challenge required")
        result.evidence["snapshot_isolation_observed"] = isolation_observed
        result.evidence["consistency"] = (
            consistency or "declared-immutable-snapshot"
            if paged
            else "single-response-total"
        )
        result.evidence["config_sha256"] = _digest(config)
        result.evidence["semantics"] = roles
        result.config = config
    except (KeyError, TypeError, ValueError, AttributeError):
        result.problems.append(
            "REST setup is unsupported, contradictory, or lacks independent read evidence"
        )
    except Exception:  # noqa: BLE001 - provider errors leave a draft; no values logged
        result.problems.append("REST setup probe unavailable")
    return result


def apply_rest_setups(
    search: ReadSearch,
    setups: list[RestReadSetup],
    *,
    contexts: Mapping[str, RestReadContext] | None = None,
    params: Mapping[str, object] | None = None,
) -> None:
    """Retain detached observations without configuring an unbound reader.

    Endpoint equality and auth environment-variable names do not establish
    tenant or principal continuity. The current synthesis API has no trusted
    context handoff unless the caller explicitly supplies an open current
    context. Detached candidates remain drafts. An in-process promotion must
    rebuild and revalidate; serialized output retains a required-context marker.
    """
    search.rest_setup_observations.extend(
        {"evidence": deepcopy(setup.evidence), "problems": list(setup.problems)}
        for setup in setups
    )
    for surface in search.surfaces:
        if surface.config.get("kind") not in ("rest", "fhir"):
            continue
        from openadapt_flow.fhir_setup import FhirReadContext

        context = (contexts or {}).get(surface.name)
        candidates = [
            setup
            for setup in setups
            if setup.observations_match
            and setup.config is not None
            and (
                (
                    isinstance(context, FhirReadContext)
                    and setup.config.get("kind") == "fhir"
                )
                or (
                    all(
                        setup.config.get(key) == surface.config.get(key)
                        for key in ("base_url", "records_path", "records_key")
                    )
                    and setup.config.get("path_params", {})
                    == surface.config.get("path_params", {})
                )
            )
        ]
        if len(candidates) != 1:
            search.problems.append(
                f"{surface.name}: no unambiguous observed REST setup"
            )
            continue
        setup = candidates[0]
        assert setup.config is not None
        if setup.evidence.get("config_sha256") != _digest(setup.config):
            search.problems.append(
                f"{surface.name}: REST setup changed after observation"
            )
            continue
        context = (contexts or {}).get(surface.name)
        if context is None:
            search.problems.append(
                f"{surface.name}: REST setup requires read-context revalidation; "
                "the learning session is not bound to this read"
            )
            continue
        try:
            config = deepcopy(setup.config)
            config["records_read_context"] = "revalidate"
            build_effect_verifier(
                EffectsConfig.model_validate(config), params, read_context=context
            )
        except Exception:  # noqa: BLE001 - no credentials/provider errors in proof
            search.problems.append(
                f"{surface.name}: current REST context refused setup"
            )
            continue
        surface.config = config
        search.rest_read_contexts[surface.name] = context
        if surface.has_ids:
            search.rest_read_contexts[surface.content_name] = context
