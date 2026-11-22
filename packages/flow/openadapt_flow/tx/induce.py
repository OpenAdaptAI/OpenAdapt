"""Candidate lens induction from HAR traces with bounded sandbox probe checks.

Induces declarative ``EntityLensSpec`` candidates from raw browser/HTTP HAR 1.2
recordings and empirically falsifies/refines them against a live loopback
sandbox into a digest-bound ``CertifiedLensPack``. The digest is not a signature
or evidence of a trusted certification authority. Passing six known probes does
not establish completeness or performance on a third-party application.

Zero vision/GUI imports. Zero application names. Stdlib + Pydantic + tx.types.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Literal, Mapping, Optional

from openadapt_flow.tx.types import (
    CertificationProbeResult,
    CertifiedLensPack,
    EndpointTemplate,
    EntityLensSpec,
    FieldCodec,
    JudgeProtocol,
    JudgeVerdict,
    WriteMode,
    effective_volatile_fields,
    open_exact_http,
)


class SandboxSafetyError(ValueError):
    """Raised when sandbox certification targets a non-loopback origin without opt-in."""


class InductionRefused(ValueError):
    """The trace never shows a record at its own URL, so no lens is proposed.

    A lens reads the target record back at its item route. A trace in which no
    response carries a record whose id is the last segment of the request path
    gives no evidence for that route; a route invented from naming patterns
    would read the wrong thing (a search endpoint, a query API) and look like a
    lens.
    """


_STATIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".css",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".avif",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".mp4",
        ".webm",
        ".mp3",
        ".wav",
        ".pdf",
        ".html",
        ".htm",
    }
)

_IGNORED_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        "/livez",
        "/metrics",
        "/ping",
        "/status",
        "/favicon.ico",
        "/robots.txt",
        "/manifest.json",
    }
)

_IGNORED_PATH_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/assets/",
    "/_next/",
    "/sockjs-node/",
    "/__vite",
    "/build/",
)

_ID_CANDIDATE_KEYS: tuple[str, ...] = (
    "id",
    "uuid",
    "pid",
    "uid",
    "pk",
    "record_id",
    "item_id",
    "task_id",
    "entity_id",
)

_PAGINATION_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "limit",
        "offset",
        "page",
        "per_page",
        "pagesize",
        "page_size",
        "sort",
        "order",
        "order_by",
        "orderby",
        "cursor",
        "after",
        "before",
        "fields",
        "include",
        "expand",
        "format",
        "_count",
        "_offset",
        "_sort",
    }
)

_CANONICAL_DUP_KEYS: tuple[str, ...] = (
    "email",
    "username",
    "external_id",
    "code",
    "slug",
    "sku",
)

_CANONICAL_IDENTITY_KEYS: tuple[str, ...] = (
    "email",
    "username",
    "external_id",
    "code",
    "slug",
    "first_name",
    "last_name",
    "name",
    "title",
)

_COLLECTION_WRAPPER_KEYS: tuple[str, ...] = (
    "records",
    "items",
    "results",
    "data",
    "entries",
    "rows",
    "elements",
)

_ITEM_WRAPPER_KEYS: tuple[str, ...] = (
    "record",
    "item",
    "result",
    "data",
    "entity",
)

_VOLATILE_NAME_RE = re.compile(
    r"(^|_|\.)(created_at|createdat|updated_at|updatedat|modified_at|modifiedat|"
    r"last_modified|lastmodified|lastupdated|last_updated|versionid|version_id|"
    r"etag|_etag|version|_version|rev|_rev|revision|row_version|tx_ts|timestamp)$",
    re.IGNORECASE,
)

_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)

_UUID_OR_INT_SEGMENT_RE = re.compile(
    r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
    r"[0-9a-fA-F]{16,64})$"
)

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
)

_MISSING = object()


@dataclass(frozen=True)
class _HarExchange:
    seq: int
    method: str
    origin: str
    path: str
    segments: tuple[str, ...]
    query_params: dict[str, str]
    req_data: Optional[dict[str, Any]]
    resp_status: int
    resp_json: Any
    etag_header: Optional[str]


def _parse_flexible_date(value: str) -> Optional[date]:
    text = value.strip()
    if not text:
        return None
    if len(text) >= 10 and _ISO_TIMESTAMP_RE.match(text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _looks_like_phone_input(value: str) -> bool:
    digits = _digits_only(value)
    if len(digits) < 7 or len(digits) > 15:
        return False
    allowed = set("0123456789+()-. xXextEXT")
    return all(ch in allowed for ch in value) and any(not ch.isdigit() for ch in value)


def _capitalize_words_variants(value: str) -> tuple[str, str]:
    stripped = value.strip()
    by_space = " ".join(part.capitalize() for part in stripped.split())
    return (stripped.title(), by_space)


def apply_field_codec(codec: FieldCodec, intended: Any, stored: Any) -> bool:
    """Use the runtime comparison policy during qualification."""
    from openadapt_flow.tx.judge import apply_field_codec as shared_apply

    return shared_apply(codec, intended, stored)[0]


def infer_field_codec(
    raw_value: Any,
    stored_value: Any,
    existing: Optional[FieldCodec] = None,
) -> Optional[FieldCodec]:
    """Infer only supported normalizations; an arbitrary observed difference is not a rule."""
    from openadapt_flow.tx.judge import infer_field_codec as shared_infer

    if existing is not None:
        return (
            existing if apply_field_codec(existing, raw_value, stored_value) else None
        )
    return shared_infer(raw_value, stored_value)


def _flatten_record(
    data: Mapping[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, Mapping) and val:
            out.update(_flatten_record(val, prefix=path))
        else:
            out[path] = val
    return out


def _is_path_volatile(path: str, volatile_set: set[str]) -> bool:
    if path in volatile_set:
        return True
    parts = path.split(".")
    for idx in range(1, len(parts)):
        if ".".join(parts[:idx]) in volatile_set:
            return True
    return False


class DefaultFrameAndIntentJudge:
    """Compatibility wrapper around the runtime policy, with no qualification-only relaxations."""

    def evaluate(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_delta: Mapping[str, Any],
        *,
        volatile_fields: tuple[str, ...] = (),
        field_codecs: Optional[Mapping[str, FieldCodec]] = None,
        allowed_server_defaults: Optional[tuple[str, ...]] = None,
    ) -> JudgeVerdict:
        from openadapt_flow.tx.judge import FrameAndIntentJudge

        options: dict[str, Any] = {}
        if allowed_server_defaults is not None:
            options["allowed_server_defaults"] = allowed_server_defaults
        return FrameAndIntentJudge(**options).evaluate(
            r0,
            r1,
            intended_delta,
            volatile_fields=volatile_fields,
            field_codecs=field_codecs,
        )


def _singularize(segment: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", segment).strip("_").lower()
    if not clean:
        return "entity"
    if clean.endswith("ies") and len(clean) > 3:
        return clean[:-3] + "y"
    if clean.endswith("ses") and len(clean) > 3:
        return clean[:-2]
    if clean.endswith("ches") or clean.endswith("xes"):
        return clean[:-2]
    if clean.endswith("s") and not clean.endswith("ss") and len(clean) > 1:
        return clean[:-1]
    return clean


def _decode_har_text(content: Mapping[str, Any]) -> str:
    raw = content.get("text")
    if not isinstance(raw, str) or not raw:
        return ""
    if str(content.get("encoding", "")).lower() == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return raw


def _parse_request_body(request: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    post_data = request.get("postData")
    if not isinstance(post_data, Mapping):
        return None
    mime = str(post_data.get("mimeType", "")).lower()
    text = post_data.get("text")
    if isinstance(text, str) and text.strip():
        stripped = text.strip()
        if "json" in mime or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        if "x-www-form-urlencoded" in mime:
            pairs = urllib.parse.parse_qsl(stripped, keep_blank_values=True)
            if pairs:
                return dict(pairs)
    params = post_data.get("params")
    if isinstance(params, list) and params:
        form_dict: dict[str, Any] = {}
        for item in params:
            if isinstance(item, Mapping) and "name" in item:
                form_dict[str(item["name"])] = item.get("value", "")
        if form_dict:
            return form_dict
    return None


def _parse_response_json(response: Mapping[str, Any]) -> Any:
    content = response.get("content")
    if not isinstance(content, Mapping):
        return None
    mime = str(content.get("mimeType", "")).lower()
    text = _decode_har_text(content).strip()
    if not text:
        return None
    if "json" in mime or text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def _extract_etag_header_name(response: Mapping[str, Any]) -> Optional[str]:
    headers = response.get("headers")
    if not isinstance(headers, list):
        return None
    for hdr in headers:
        if isinstance(hdr, Mapping):
            name = str(hdr.get("name", ""))
            if name.lower() == "etag" and hdr.get("value"):
                return name
    return None


def _is_static_or_noise_path(path: str) -> bool:
    norm = path.rstrip("/") or "/"
    lower = norm.lower()
    if lower in _IGNORED_EXACT_PATHS:
        return True
    for prefix in _IGNORED_PATH_PREFIXES:
        if lower.startswith(prefix):
            return True
    last_dot = lower.rfind(".")
    last_slash = lower.rfind("/")
    if last_dot > last_slash:
        ext = lower[last_dot:]
        if ext in _STATIC_EXTENSIONS:
            return True
    return False


def _parse_har_exchanges(har: Mapping[str, Any]) -> list[_HarExchange]:
    log_obj = har.get("log")
    if not isinstance(log_obj, Mapping):
        raise ValueError("Invalid HAR 1.2 object: missing 'log' mapping")
    raw_entries = log_obj.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(
            "Invalid HAR 1.2 object: 'log.entries' must be a non-empty list"
        )

    exchanges: list[_HarExchange] = []
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, Mapping):
            continue
        req = entry.get("request")
        resp = entry.get("response")
        if not isinstance(req, Mapping) or not isinstance(resp, Mapping):
            continue
        method = str(req.get("method", "")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            continue
        url = str(req.get("url", "")).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        path = parsed.path or "/"
        if _is_static_or_noise_path(path):
            continue
        status = int(resp.get("status", 0) or 0)
        if status < 200 or status >= 300:
            continue

        req_data = _parse_request_body(req)
        resp_json = _parse_response_json(resp)
        if method != "DELETE" and req_data is None and resp_json is None:
            continue

        q_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query_params = {k: v for k, v in q_pairs}
        if isinstance(req.get("queryString"), list):
            for q_item in req["queryString"]:
                if isinstance(q_item, Mapping) and "name" in q_item:
                    query_params.setdefault(
                        str(q_item["name"]), str(q_item.get("value", ""))
                    )

        clean_path = path.rstrip("/") if path != "/" else "/"
        segments = tuple(seg for seg in clean_path.split("/") if seg)
        exchanges.append(
            _HarExchange(
                seq=idx,
                method=method,
                origin=f"{parsed.scheme}://{parsed.netloc}",
                path=clean_path,
                segments=segments,
                query_params=query_params,
                req_data=req_data,
                resp_status=status,
                resp_json=resp_json,
                etag_header=_extract_etag_header_name(resp),
            )
        )

    if not exchanges:
        raise ValueError("No JSON/form API exchanges found in HAR trace")
    return exchanges


def _unwrap_single_record(
    payload: Any,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not isinstance(payload, dict):
        return (None, None)
    for key in _ITEM_WRAPPER_KEYS:
        inner = payload.get(key)
        if isinstance(inner, dict) and len(payload) <= 3:
            return (inner, key)
    return (payload, None)


def _unwrap_collection_records(
    payload: Any,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    if isinstance(payload, list):
        dicts = [row for row in payload if isinstance(row, dict)]
        return (dicts, None)
    if isinstance(payload, dict):
        for key in _COLLECTION_WRAPPER_KEYS:
            val = payload.get(key)
            if isinstance(val, list):
                dicts = [row for row in val if isinstance(row, dict)]
                return (dicts, key)
    return (None, None)


def _pick_id_field_from_record(
    record: Mapping[str, Any],
    req_body: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    for cand in _ID_CANDIDATE_KEYS:
        if (
            cand in record
            and isinstance(record[cand], (str, int))
            and not isinstance(record[cand], bool)
        ):
            return cand
    for key, val in record.items():
        if (
            key.lower().endswith("_id")
            and isinstance(val, (str, int))
            and not isinstance(val, bool)
            and (req_body is None or key not in req_body)
        ):
            return key
    if req_body is not None:
        for key, val in record.items():
            if (
                key not in req_body
                and not _VOLATILE_NAME_RE.search(key)
                and isinstance(val, (str, int))
                and not isinstance(val, bool)
                and not (isinstance(val, str) and _ISO_TIMESTAMP_RE.match(val))
            ):
                return key
    return None


@dataclass(frozen=True)
class _EffectRoutes:
    """Route roles taken from what each request did, not from its method name.

    An *item* exchange returns one record whose id equals the last path
    segment. A mutation on an item path is an update, whatever its method. A
    mutation elsewhere whose response is a record with an id first seen there
    is a create, whatever its method. Both observations come from the trace
    itself, so an app that creates with ``PUT`` and updates with ``POST`` gets
    the same lens shape as one that does the reverse.
    """

    parent: tuple[str, ...]
    id_field: str
    records_key: Optional[str]
    members: frozenset[int]
    updates: tuple[_HarExchange, ...]
    create: Optional[_HarExchange]
    delete: Optional[_HarExchange]


def _canonical_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _record_id(rec: Mapping[str, Any], field_name: str) -> Optional[str]:
    val = rec.get(field_name)
    if isinstance(val, bool) or not isinstance(val, (str, int)):
        return None
    text = str(val)
    return text or None


def _shows_item_route(scoped: list[_HarExchange]) -> bool:
    """True when some 2xx response is one record served at its own URL."""
    for ex in scoped:
        if ex.method not in {"GET", "POST", "PUT", "PATCH"} or len(ex.segments) < 2:
            continue
        rec, _ = _unwrap_single_record(ex.resp_json)
        if rec is None:
            continue
        for cand in _ID_CANDIDATE_KEYS:
            if _record_id(rec, cand) == ex.segments[-1]:
                return True
    return False


def _infer_effect_routes(scoped: list[_HarExchange]) -> Optional[_EffectRoutes]:
    """Find the entity route whose roles the trace's effects establish."""
    groups: dict[tuple[tuple[str, ...], str], list[tuple[_HarExchange, str]]] = {}
    wrappers: dict[tuple[tuple[str, ...], str], Optional[str]] = {}
    for ex in scoped:
        if ex.method not in {"GET", "POST", "PUT", "PATCH"} or len(ex.segments) < 2:
            continue
        rec, wrapper = _unwrap_single_record(ex.resp_json)
        if rec is None:
            continue
        for cand in _ID_CANDIDATE_KEYS:
            rid = _record_id(rec, cand)
            if rid is not None and ex.segments[-1] == rid:
                key = (ex.segments[:-1], cand)
                groups.setdefault(key, []).append((ex, rid))
                wrappers.setdefault(key, wrapper)
                break

    def _find_create(
        id_field: str, members: list[tuple[_HarExchange, str]]
    ) -> Optional[_HarExchange]:
        known_ids = {rid for _, rid in members}
        first_seen: dict[str, int] = {}
        for ex, rid in members:
            first_seen.setdefault(rid, ex.seq)
        for ex in scoped:
            if ex.method not in {"POST", "PUT", "PATCH"}:
                continue
            rec, _ = _unwrap_single_record(ex.resp_json)
            if rec is None:
                continue
            created = _record_id(rec, id_field)
            if (
                created is not None
                and created in known_ids
                and ex.segments[-1:] != (created,)
                and ex.seq < first_seen[created]
            ):
                return ex
        return None

    # An entity needs a write the trace shows: an update on the item route,
    # or a create whose record the trace then reads at that route.
    best_key: Optional[tuple[tuple[str, ...], str]] = None
    best_score: tuple[int, int, int, int] = (0, 0, 0, -1)
    best_create: Optional[_HarExchange] = None
    for key, members in groups.items():
        n_mut = sum(1 for ex, _ in members if ex.method != "GET")
        n_read = sum(1 for ex, _ in members if ex.method == "GET")
        create_ex = _find_create(key[1], members)
        if n_mut == 0 and create_ex is None:
            continue
        score = (
            n_mut,
            int(create_ex is not None),
            n_read,
            max(ex.seq for ex, _ in members),
        )
        if score > best_score:
            best_key, best_score, best_create = key, score, create_ex
    if best_key is None:
        return None

    parent, id_field = best_key
    members = groups[best_key]
    create = best_create

    delete = next(
        (
            ex
            for ex in scoped
            if ex.method == "DELETE"
            and ex.segments[:-1] == parent
            and len(ex.segments) == len(parent) + 1
        ),
        None,
    )
    updates = tuple(ex for ex, _ in members if ex.method != "GET")
    member_seqs = {ex.seq for ex, _ in members}
    if create is not None:
        member_seqs.add(create.seq)
    if delete is not None:
        member_seqs.add(delete.seq)
    for ex in scoped:
        if ex.method == "GET" and ex.segments == parent:
            member_seqs.add(ex.seq)
    return _EffectRoutes(
        parent=parent,
        id_field=id_field,
        records_key=wrappers[best_key],
        members=frozenset(member_seqs),
        updates=updates,
        create=create,
        delete=delete,
    )


def _looks_like_metadata_or_volatile(key: str, value: Any) -> bool:
    if _VOLATILE_NAME_RE.search(key):
        return True
    if isinstance(value, str) and _ISO_TIMESTAMP_RE.match(value):
        lower = key.lower()
        if any(
            tok in lower
            for tok in ("time", "date", "_at", "at", "updated", "created", "modified")
        ):
            return True
    return False


def _null_or_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def learn_null_empty_codecs(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    written_paths: Optional[set[str]] = None,
) -> dict[str, FieldCodec]:
    """Fields whose value moved between ``None`` and ``""`` and nothing else.

    Many apps store an untouched optional field as ``NULL`` until a form save
    sends ``""`` back for it, or the reverse. Without a declaration the judge
    reads that as a clobber (OpenAdapt #2391). Each field this returns gets a
    ``null_empty`` codec, so only that field treats the two as one value; any
    non-empty change on it is still judged exactly. Fields in
    ``written_paths`` are skipped: a value the write asked for is not a server
    rewrite.
    """
    flat_before = _flatten_record(before)
    flat_after = _flatten_record(after)
    skip = written_paths or set()
    learned: dict[str, FieldCodec] = {}
    for path, old in flat_before.items():
        if path in skip or path not in flat_after:
            continue
        new = flat_after[path]
        if (
            old is not new
            and old != new
            and _null_or_empty(old)
            and _null_or_empty(new)
        ):
            learned[path] = FieldCodec(kind="null_empty")
    return learned


def induce_lens_from_har(
    har: Mapping[str, Any],
    *,
    entity_name: Optional[str] = None,
    intended_inputs: Optional[Mapping[str, Any]] = None,
    llm_proposer: Optional[Callable[[Mapping[str, Any]], EntityLensSpec]] = None,
    allow_unobserved_item_route: bool = False,
) -> EntityLensSpec:
    """Compile an ``EntityLensSpec`` from 1-2 browser/HTTP HAR 1.2 traces.

    A trace that never shows a record at its own URL raises
    ``InductionRefused``. ``allow_unobserved_item_route=True`` instead
    proposes an item route from the collection path; use it only when the
    lens is then certified on a sandbox, whose probes read the guessed route
    back and fail if it serves a different record.
    """
    exchanges = _parse_har_exchanges(har)

    if llm_proposer is not None:
        proposed = EntityLensSpec.model_validate(llm_proposer(har))
        updates: dict[str, Any] = {}
        if entity_name is not None:
            updates["entity_name"] = entity_name
        if intended_inputs:
            merged_codecs = dict(proposed.field_codecs)
            for ex in reversed(exchanges):
                rec, _ = _unwrap_single_record(ex.resp_json)
                if rec is not None:
                    flat_rec = _flatten_record(rec)
                    for k, raw_val in _flatten_record(intended_inputs).items():
                        if k in flat_rec:
                            codec = infer_field_codec(
                                raw_val, flat_rec[k], existing=merged_codecs.get(k)
                            )
                            if codec is not None:
                                merged_codecs[k] = codec
                    break
            updates["field_codecs"] = merged_codecs
        return proposed.model_copy(update=updates) if updates else proposed

    mutations = [
        ex for ex in exchanges if ex.method in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    anchor_pool = mutations if mutations else exchanges
    origin_counts: dict[str, int] = {}
    for ex in anchor_pool:
        origin_counts[ex.origin] = origin_counts.get(ex.origin, 0) + 1
    base_url = max(origin_counts.items(), key=lambda kv: kv[1])[0]
    scoped = [ex for ex in exchanges if ex.origin == base_url]
    effect = _infer_effect_routes(scoped)
    if effect is None and not allow_unobserved_item_route:
        raise InductionRefused(
            "no write the trace can attribute to a record: no update on a URL "
            "ending in the returned record's id, and no create whose record "
            "is then served at such a URL"
            if _shows_item_route(scoped)
            else "no item route in the trace: no response carries a record "
            "whose id is the last segment of its request path"
        )
    if effect is not None:
        # Learn only from the entity's own exchanges, so records of another
        # collection that share an id never feed its codecs or snapshots.
        scoped = [ex for ex in scoped if ex.seq in effect.members]

    # Step 1: Identify collection_path, id_field, item wrapper, and observed entity IDs.
    detected_id_field: Optional[str] = None
    collection_segments: Optional[tuple[str, ...]] = None
    item_records_key: Optional[str] = None
    collection_records_key: Optional[str] = None
    etag_header: Optional[str] = None

    post_creates = [ex for ex in scoped if ex.method == "POST" and ex.segments]
    item_updates = [ex for ex in scoped if ex.method in {"PUT", "PATCH", "DELETE"}]

    known_ids_by_collection: dict[tuple[str, ...], set[str]] = {}

    for ex in post_creates:
        rec, wrapper = _unwrap_single_record(ex.resp_json)
        if rec is not None:
            cand_id = _pick_id_field_from_record(rec, ex.req_data)
            if cand_id is not None:
                detected_id_field = detected_id_field or cand_id
                collection_segments = collection_segments or ex.segments
                item_records_key = item_records_key or wrapper
                known_ids_by_collection.setdefault(ex.segments, set()).add(
                    str(rec[cand_id])
                )
        elif collection_segments is None:
            collection_segments = ex.segments

    for ex in scoped:
        if ex.etag_header:
            etag_header = etag_header or ex.etag_header
        if len(ex.segments) >= 2 and ex.method in {"GET", "PUT", "PATCH", "DELETE"}:
            parent_segs = ex.segments[:-1]
            tail_seg = ex.segments[-1]
            rec, wrapper = _unwrap_single_record(ex.resp_json)
            matched_key: Optional[str] = None
            if rec is not None:
                for cand in _ID_CANDIDATE_KEYS + tuple(rec.keys()):
                    if cand in rec and str(rec[cand]) == tail_seg:
                        matched_key = cand
                        break
            if (
                matched_key is not None
                or tail_seg in known_ids_by_collection.get(parent_segs, set())
                or (ex.method in {"PUT", "PATCH", "DELETE"} and len(ex.segments) >= 1)
                or _UUID_OR_INT_SEGMENT_RE.match(tail_seg)
            ):
                collection_segments = collection_segments or parent_segs
                if parent_segs == collection_segments:
                    if matched_key is not None:
                        detected_id_field = matched_key
                    elif rec is not None and detected_id_field is None:
                        detected_id_field = _pick_id_field_from_record(rec, ex.req_data)
                    item_records_key = item_records_key or wrapper
                    known_ids_by_collection.setdefault(parent_segs, set()).add(tail_seg)

    if collection_segments is None:
        first_ex = scoped[0]
        if len(first_ex.segments) >= 2 and _UUID_OR_INT_SEGMENT_RE.match(
            first_ex.segments[-1]
        ):
            collection_segments = first_ex.segments[:-1]
        else:
            collection_segments = first_ex.segments

    if detected_id_field is None:
        for ex in scoped:
            rows, col_wrap = _unwrap_collection_records(ex.resp_json)
            if rows:
                collection_records_key = collection_records_key or col_wrap
                row_id_cand = _pick_id_field_from_record(rows[0])
                if row_id_cand:
                    detected_id_field = row_id_cand
                    break
    if effect is not None:
        collection_segments = effect.parent
        detected_id_field = effect.id_field
        item_records_key = effect.records_key
    id_field: str = detected_id_field or "id"

    collection_path = (
        "/" + "/".join(collection_segments) if collection_segments else "/"
    )
    item_path_template = (
        f"/{'/'.join(collection_segments)}/{{id}}" if collection_segments else "/{id}"
    )

    for ex in scoped:
        if ex.method == "GET" and ex.segments == collection_segments:
            _, col_wrap = _unwrap_collection_records(ex.resp_json)
            if col_wrap is not None:
                collection_records_key = col_wrap

    resolved_entity_name = entity_name or _singularize(
        collection_segments[-1] if collection_segments else "entity"
    )

    # Step 2: Synthesize get_one, write_endpoint, create_endpoint, delete_endpoint.
    effective_etag = etag_header or "ETag"
    get_one = EndpointTemplate(
        method="GET",
        path_template=item_path_template,
        records_key=item_records_key,
        id_field=id_field,
        etag_header=effective_etag,
    )

    observed_create = next(
        (
            ex
            for ex in scoped
            if ex.method == "POST" and ex.segments == collection_segments
        ),
        None,
    )
    observed_updates = [
        ex
        for ex in scoped
        if ex.method in {"PUT", "PATCH"}
        and len(ex.segments) == len(collection_segments) + 1
        and ex.segments[:-1] == collection_segments
    ]
    observed_delete = next(
        (
            ex
            for ex in scoped
            if ex.method == "DELETE"
            and len(ex.segments) == len(collection_segments) + 1
            and ex.segments[:-1] == collection_segments
        ),
        None,
    )

    create_endpoint: Optional[EndpointTemplate] = EndpointTemplate(
        method="POST",
        path_template=collection_path,
        records_key=item_records_key,
        id_field=id_field,
        etag_header=effective_etag,
    )

    delete_endpoint: Optional[EndpointTemplate] = (
        EndpointTemplate(
            method="DELETE",
            path_template=item_path_template,
            id_field=id_field,
            etag_header=effective_etag,
        )
        if observed_delete is not None
        else None
    )

    if effect is not None:
        observed_updates = list(effect.updates)
        observed_create = effect.create
        observed_delete = effect.delete
        if effect.create is not None:
            create_method = effect.create.method
            assert create_method in {"POST", "PUT", "PATCH"}
            create_endpoint = EndpointTemplate(
                method=create_method,  # type: ignore[arg-type]
                path_template=effect.create.path,
                records_key=item_records_key,
                id_field=id_field,
                etag_header=effective_etag,
            )
        delete_endpoint = (
            EndpointTemplate(
                method="DELETE",
                path_template=item_path_template,
                id_field=id_field,
                etag_header=effective_etag,
            )
            if effect.delete is not None
            else None
        )

    write_mode: WriteMode
    if observed_updates:
        last_upd = observed_updates[-1]
        upd_method: Literal["POST", "PUT", "PATCH"]
        if effect is not None:
            assert last_upd.method in {"POST", "PUT", "PATCH"}
            upd_method = last_upd.method  # type: ignore[assignment]
        else:
            upd_method = "PATCH" if last_upd.method == "PATCH" else "PUT"
        write_endpoint = EndpointTemplate(
            method=upd_method,
            path_template=item_path_template,
            records_key=item_records_key,
            id_field=id_field,
            etag_header=effective_etag,
        )
        write_mode = "patch"
    elif observed_create is not None:
        assert create_endpoint is not None
        write_endpoint = create_endpoint
        write_mode = "create"
    elif item_updates:
        upd = item_updates[-1]
        fallback_method: Literal["PUT", "PATCH"] = (
            "PATCH" if upd.method == "PATCH" else "PUT"
        )
        write_endpoint = EndpointTemplate(
            method=fallback_method,
            path_template=item_path_template,
            records_key=item_records_key,
            id_field=id_field,
            etag_header=effective_etag,
        )
        write_mode = "patch"
    else:
        write_endpoint = EndpointTemplate(
            method="PATCH",
            path_template=item_path_template,
            records_key=item_records_key,
            id_field=id_field,
            etag_header=effective_etag,
        )
        write_mode = "patch"

    # Step 3: Collect entity snapshots by ID across sequence to detect codecs,
    # volatile fields, clobbering in HAR, and duplicate_search.
    entity_snapshots: dict[str, list[tuple[_HarExchange, dict[str, Any]]]] = {}
    sample_fields: dict[str, Any] = {}

    for ex in scoped:
        rec, _ = _unwrap_single_record(ex.resp_json)
        if rec is None:
            continue
        eid: Optional[str] = None
        if id_field in rec and rec[id_field] is not None:
            eid = str(rec[id_field])
        elif (
            len(ex.segments) == len(collection_segments) + 1
            and ex.segments[:-1] == collection_segments
        ):
            eid = ex.segments[-1]
        if eid is not None:
            entity_snapshots.setdefault(eid, []).append((ex, rec))
            sample_fields.update(rec)

    if ex_req := next((ex.req_data for ex in scoped if ex.req_data), None):
        for k, v in ex_req.items():
            sample_fields.setdefault(k, v)

    # Step 4: Induce duplicate_search and duplicate_keys.
    duplicate_search: Optional[EndpointTemplate] = None
    duplicate_keys: list[str] = []
    first_mutation_seq = mutations[0].seq if mutations else len(exchanges) + 1

    all_write_values: dict[str, str] = {}
    for ex in scoped:
        if ex.req_data:
            for k, v in ex.req_data.items():
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    all_write_values[str(v)] = k
    if intended_inputs:
        for k, v in intended_inputs.items():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                all_write_values[str(v)] = k

    for ex in scoped:
        if (
            ex.method == "GET"
            and ex.segments == collection_segments
            and ex.query_params
            and ex.seq <= first_mutation_seq
        ):
            filter_params: dict[str, str] = {}
            for qk, qv in ex.query_params.items():
                if qk.lower() in _PAGINATION_QUERY_KEYS:
                    continue
                matched_field = all_write_values.get(qv)
                if matched_field is None and qk in sample_fields:
                    matched_field = qk
                field_ref = matched_field or qk
                filter_params[qk] = f"{{{field_ref}}}"
                if field_ref not in duplicate_keys:
                    duplicate_keys.append(field_ref)
            if filter_params:
                _, col_wrap = _unwrap_collection_records(ex.resp_json)
                duplicate_search = EndpointTemplate(
                    method="GET",
                    path_template=collection_path,
                    query_params=filter_params,
                    records_key=col_wrap or collection_records_key,
                    id_field=id_field,
                    etag_header=effective_etag,
                )
                break

    if duplicate_search is None:
        for dup_cand in _CANONICAL_DUP_KEYS:
            if dup_cand in sample_fields:
                duplicate_keys.append(dup_cand)
                duplicate_search = EndpointTemplate(
                    method="GET",
                    path_template=collection_path,
                    query_params={dup_cand: f"{{{dup_cand}}}"},
                    records_key=collection_records_key,
                    id_field=id_field,
                    etag_header=effective_etag,
                )
                break

    # Step 5: Induce volatile_fields and detect any HAR-visible PUT clobber.
    volatile_list: list[str] = [
        "updated_at",
        "updatedAt",
        "modified_at",
        "lastModified",
        "meta.lastUpdated",
        "meta.versionId",
        "etag",
        "version",
    ]
    user_supplied_keys: set[str] = set()
    for ex in scoped:
        if ex.req_data:
            user_supplied_keys.update(_flatten_record(ex.req_data).keys())
    if intended_inputs:
        user_supplied_keys.update(_flatten_record(intended_inputs).keys())

    create_defaults_list: list[str] = []
    # Only a metadata name makes a field volatile on sight. A field that merely
    # holds a timestamp stays in the frame unless the trace shows the server
    # changing it (the snapshot comparison below).
    pending_volatile: list[str] = []
    creates_seen = (
        ([effect.create] if effect.create is not None else [])
        if effect is not None
        else post_creates
    )
    for ex in creates_seen:
        rec, _ = _unwrap_single_record(ex.resp_json)
        if rec is None:
            continue
        flat_req = _flatten_record(ex.req_data or {})
        flat_resp = _flatten_record(rec)
        for path, val in flat_resp.items():
            if path == id_field or path in flat_req:
                continue
            if _VOLATILE_NAME_RE.search(path):
                if path not in volatile_list:
                    volatile_list.append(path)
            elif _looks_like_metadata_or_volatile(path, val):
                if path not in pending_volatile:
                    pending_volatile.append(path)
            elif path not in create_defaults_list:
                create_defaults_list.append(path)

    null_empty_paths: list[str] = []
    requested_values: set[tuple[str, str]] = set()
    for ex in scoped:
        if ex.method in {"POST", "PUT", "PATCH"} and ex.req_data:
            for req_path, req_val in _flatten_record(ex.req_data).items():
                requested_values.add((req_path, _canonical_value(req_val)))

    for _eid, history in entity_snapshots.items():
        for idx in range(1, len(history)):
            prev_rec = _flatten_record(history[idx - 1][1])
            curr_ex, curr_raw = history[idx]
            curr_rec = _flatten_record(curr_raw)
            flat_req = _flatten_record(curr_ex.req_data or {})
            for path, new_val in curr_rec.items():
                if path == id_field or path not in prev_rec:
                    continue
                old_val = prev_rec[path]
                if new_val == old_val:
                    continue
                # NULL became "" (or the reverse): one empty value, spelled two
                # ways. Learn it for this field; never a clobber or a replace.
                if _null_or_empty(old_val) and _null_or_empty(new_val):
                    if path not in null_empty_paths:
                        null_empty_paths.append(path)
                    continue
                # Auto-mutated server field: either echoed unchanged in request yet server
                # changed it, or server metadata key not in user-supplied fields. A
                # value some write in the trace asked for is the user's edit, not a
                # server change, so it never makes the field volatile.
                if (
                    _looks_like_metadata_or_volatile(path, new_val)
                    and (
                        path,
                        _canonical_value(new_val),
                    )
                    not in requested_values
                ):
                    if path not in volatile_list:
                        volatile_list.append(path)
                elif (
                    curr_ex.method in {"POST", "PUT", "PATCH"}
                    and (effect is None or curr_ex in effect.updates)
                    and path not in flat_req
                    and path in user_supplied_keys
                    and new_val in ("", 0, None, False, [], {})
                ):
                    write_mode = "replace"

    for path in pending_volatile:
        if path not in volatile_list and path not in create_defaults_list:
            create_defaults_list.append(path)

    # Step 6: Induce field_codecs from request->response, request->subsequent GET,
    # and intended_inputs->stored record.
    field_codecs: dict[str, FieldCodec] = {}
    for ex in scoped:
        if ex.method not in {"POST", "PUT", "PATCH"} or not ex.req_data:
            continue
        flat_req = _flatten_record(ex.req_data)
        target_rec, _ = _unwrap_single_record(ex.resp_json)
        eid = None
        if target_rec is not None and id_field in target_rec:
            eid = str(target_rec[id_field])
        elif (
            len(ex.segments) == len(collection_segments) + 1
            and ex.segments[:-1] == collection_segments
        ):
            eid = ex.segments[-1]

        candidate_stored_records: list[dict[str, Any]] = []
        if target_rec is not None:
            candidate_stored_records.append(_flatten_record(target_rec))
        if eid and eid in entity_snapshots:
            for snap_ex, snap_rec in entity_snapshots[eid]:
                if snap_ex.seq > ex.seq and snap_ex.method == "GET":
                    candidate_stored_records.append(_flatten_record(snap_rec))
                    break

        for flat_stored in candidate_stored_records:
            for path, raw_val in flat_req.items():
                if path == id_field or path in volatile_list or path not in flat_stored:
                    continue
                codec = infer_field_codec(
                    raw_val, flat_stored[path], existing=field_codecs.get(path)
                )
                if codec is not None:
                    field_codecs[path] = codec

    if intended_inputs:
        flat_intended = _flatten_record(intended_inputs)
        # Match against the latest stored snapshot containing each intended field.
        all_stored_chronological: list[dict[str, Any]] = []
        for ex in scoped:
            unwrapped_rec, _ = _unwrap_single_record(ex.resp_json)
            if unwrapped_rec is not None:
                all_stored_chronological.append(_flatten_record(unwrapped_rec))
        for path, raw_val in flat_intended.items():
            if path == id_field or path in volatile_list:
                continue
            for flat_stored in reversed(all_stored_chronological):
                if path in flat_stored:
                    codec = infer_field_codec(
                        raw_val, flat_stored[path], existing=field_codecs.get(path)
                    )
                    if codec is not None:
                        field_codecs[path] = codec
                    break

    for path in null_empty_paths:
        field_codecs.setdefault(path, FieldCodec(kind="null_empty"))

    # Step 7: Induce stable identity_fields.
    volatile_set = set(volatile_list)
    identity_candidates: list[str] = []
    for cand in _CANONICAL_IDENTITY_KEYS:
        if cand in sample_fields and cand != id_field and cand not in volatile_set:
            identity_candidates.append(cand)
    if not identity_candidates:
        for key in user_supplied_keys:
            if (
                key != id_field
                and key not in volatile_set
                and "." not in key
                and len(identity_candidates) < 2
            ):
                identity_candidates.append(key)

    return EntityLensSpec(
        entity_name=resolved_entity_name,
        base_url=base_url,
        id_field=id_field,
        write_mode=write_mode,
        get_one=get_one,
        write_endpoint=write_endpoint,
        create_endpoint=create_endpoint,
        duplicate_search=duplicate_search,
        delete_endpoint=delete_endpoint,
        identity_fields=tuple(identity_candidates),
        volatile_fields=tuple(volatile_list),
        create_defaults=tuple(create_defaults_list),
        field_codecs=field_codecs,
        duplicate_keys=tuple(duplicate_keys),
    )


def _assert_sandbox_url_allowed(
    base_url: str,
    *,
    allow_remote_sandbox: bool,
) -> None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SandboxSafetyError(f"Invalid sandbox base_url: {base_url!r}")
    if allow_remote_sandbox:
        return
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise SandboxSafetyError(
            f"Refusing non-loopback sandbox host {host!r}; pass "
            "allow_remote_sandbox=True only for isolated staging environments."
        ) from exc
    if not ip.is_loopback:
        raise SandboxSafetyError(
            f"Refusing non-loopback sandbox IP {host!r}; pass "
            "allow_remote_sandbox=True only for isolated staging environments."
        )


def _format_endpoint_url(
    base_url: str,
    endpoint: EndpointTemplate,
    *,
    entity_id: Optional[str] = None,
    params_ctx: Optional[Mapping[str, Any]] = None,
) -> str:
    ctx: dict[str, str] = {}
    if params_ctx:
        for k, v in params_ctx.items():
            ctx[str(k)] = urllib.parse.quote(str(v), safe="")
    if entity_id is not None:
        quoted_id = urllib.parse.quote(str(entity_id), safe="")
        ctx["id"] = quoted_id
        ctx[endpoint.id_field] = quoted_id

    path = endpoint.path_template
    for key, val in ctx.items():
        path = path.replace(f"{{{key}}}", val)
    if not path.startswith("/"):
        path = "/" + path

    url = base_url.rstrip("/") + path
    if endpoint.query_params:
        rendered_q: dict[str, str] = {}
        for qk, q_tmpl in endpoint.query_params.items():
            val_str = q_tmpl
            for key, raw_v in (params_ctx or {}).items():
                val_str = val_str.replace(f"{{{key}}}", str(raw_v))
            if entity_id is not None:
                val_str = val_str.replace("{id}", str(entity_id))
            rendered_q[qk] = val_str
        url = f"{url}?{urllib.parse.urlencode(rendered_q)}"
    return url


def _http_json(
    method: str,
    url: str,
    *,
    payload: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], Any]:
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body_bytes: Optional[bytes] = None
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(
        url=url,
        data=body_bytes,
        headers=req_headers,
        method=method.upper(),
    )
    try:
        with open_exact_http(req, timeout=timeout) as resp:
            status = int(resp.status)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw_bytes = resp.read()
    except urllib.error.HTTPError as err:
        status = int(err.code)
        resp_headers = (
            {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        )
        raw_bytes = err.read() if err.fp is not None else b""
    except urllib.error.URLError:
        return (0, {}, None)

    if not raw_bytes:
        return (status, resp_headers, None)
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return (status, resp_headers, None)
    try:
        return (status, resp_headers, json.loads(text))
    except json.JSONDecodeError:
        return (status, resp_headers, text)


def _extract_single_entity(
    body: Any,
    endpoint: EndpointTemplate,
) -> Optional[dict[str, Any]]:
    if isinstance(body, dict):
        if endpoint.records_key and isinstance(body.get(endpoint.records_key), dict):
            return dict(body[endpoint.records_key])
        return dict(body)
    return None


def _resolve_create_endpoint(lens: EntityLensSpec) -> EndpointTemplate:
    if lens.create_endpoint is not None:
        return lens.create_endpoint
    if lens.write_mode == "create":
        return lens.write_endpoint
    base_path = re.sub(r"/\{[^/}]+\}/?$", "", lens.get_one.path_template) or "/"
    return EndpointTemplate(
        method="POST",
        path_template=base_path,
        records_key=lens.get_one.records_key,
        id_field=lens.id_field,
        etag_header=lens.get_one.etag_header,
    )


def _uniquify_record_payload(
    base_record: Mapping[str, Any],
    *,
    tag: str,
    mutate_all_fields: bool = False,
    id_field: str = "id",
    preserve_codecs: Optional[Mapping[str, FieldCodec]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    codecs = preserve_codecs or {}
    for key, val in base_record.items():
        if key == id_field:
            continue
        lower_k = key.lower()
        codec = codecs.get(key)
        if not mutate_all_fields:
            if "email" in lower_k and isinstance(val, str) and "@" in val:
                local, domain = val.split("@", 1)
                out[key] = f"{local}+{tag}@{domain}"
            elif lower_k in {
                "username",
                "external_id",
                "slug",
                "sku",
                "code",
            } and isinstance(val, str):
                out[key] = f"{val}_{tag}"
            else:
                out[key] = val
            continue

        # Mutate all fields for distinct records (other concurrent records / wrong-record fault).
        if codec is not None and codec.kind == "bijection" and codec.mapping:
            out[key] = _pick_wrong_wire_value(val, codec)
        elif codec is not None and codec.kind == "iso_date":
            out[key] = "2024-01-19" if str(val) != "2024-01-19" else "2024-02-20"
        elif codec is not None and codec.kind == "phone_digits":
            out[key] = "(617) 555-0144"
        elif isinstance(val, str):
            if "email" in lower_k and "@" in val:
                _, domain = val.split("@", 1)
                out[key] = f"other.{tag}@{domain}"
            elif len(val) <= 3 and val.isalpha():
                out[key] = "NY" if val.upper() != "NY" else "CA"
            else:
                out[key] = f"other {tag}"
        elif isinstance(val, bool):
            out[key] = not val
        elif isinstance(val, int):
            out[key] = val + 37
        elif isinstance(val, float):
            out[key] = round(val + 37.5, 2)
        else:
            out[key] = val
    return out


def _create_probe_entity(
    lens: EntityLensSpec,
    payload: Mapping[str, Any],
    *,
    auth_headers: Optional[Mapping[str, str]],
    created_ids: list[str],
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    create_ep = _resolve_create_endpoint(lens)
    clean_payload = {k: v for k, v in payload.items() if k != lens.id_field}
    url = _format_endpoint_url(lens.base_url, create_ep)
    status, _hdrs, body = _http_json(
        create_ep.method, url, payload=clean_payload, headers=auth_headers
    )
    if status < 200 or status >= 300:
        return (None, None)
    rec = _extract_single_entity(body, create_ep)
    if rec is None or lens.id_field not in rec or rec[lens.id_field] is None:
        return (None, rec)
    eid = str(rec[lens.id_field])
    created_ids.append(eid)
    return (eid, rec)


def _read_one_entity(
    lens: EntityLensSpec,
    entity_id: str,
    *,
    auth_headers: Optional[Mapping[str, str]],
) -> tuple[int, Optional[str], Optional[dict[str, Any]]]:
    url = _format_endpoint_url(lens.base_url, lens.get_one, entity_id=entity_id)
    status, hdrs, body = _http_json("GET", url, headers=auth_headers)
    if status < 200 or status >= 300:
        return (status, None, None)
    rec = _extract_single_entity(body, lens.get_one)
    if rec is None:
        return (status, None, None)
    # Guard against broken get_one pointing at a collection endpoint or wrong ID.
    if lens.id_field in rec and str(rec[lens.id_field]) != str(entity_id):
        return (status, None, None)
    etag_name = (lens.get_one.etag_header or "ETag").lower()
    etag_val = hdrs.get(etag_name)
    return (status, etag_val, rec)


def _delete_probe_entities(
    lens: EntityLensSpec,
    entity_ids: list[str],
    *,
    auth_headers: Optional[Mapping[str, str]],
) -> None:
    if lens.delete_endpoint is None:
        return
    for eid in entity_ids:
        url = _format_endpoint_url(lens.base_url, lens.delete_endpoint, entity_id=eid)
        _http_json(lens.delete_endpoint.method, url, headers=auth_headers)


def _build_write_payload(
    lens: EntityLensSpec,
    r0: Mapping[str, Any],
    delta: Mapping[str, Any],
) -> dict[str, Any]:
    if lens.write_mode == "replace":
        volatile_set = set(lens.volatile_fields)
        merged = {
            k: v
            for k, v in r0.items()
            if k != lens.id_field and not _is_path_volatile(k, volatile_set)
        }
        merged.update(delta)
        return merged
    return dict(delta)


def _check_precommit_identity(
    lens: EntityLensSpec,
    r0: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
) -> bool:
    if not expected_identity:
        return False
    flat_r0 = _flatten_record(r0)
    for field_path, expected_val in expected_identity.items():
        if field_path not in flat_r0:
            return False
        actual_val = flat_r0[field_path]
        if actual_val == expected_val:
            continue
        codec = lens.field_codecs.get(field_path)
        if codec is not None and apply_field_codec(codec, expected_val, actual_val):
            continue
        return False
    return True


def _pick_wiped_value(before_val: Any) -> Any:
    if isinstance(before_val, str):
        return "" if before_val != "" else "__wiped__"
    if isinstance(before_val, bool):
        return not before_val
    if isinstance(before_val, int):
        return 0 if before_val != 0 else -1
    if isinstance(before_val, float):
        return 0.0 if before_val != 0.0 else -1.0
    if isinstance(before_val, list):
        return [] if before_val else ["__wiped__"]
    if isinstance(before_val, dict):
        return {} if before_val else {"__wiped__": True}
    return None


def _pick_wrong_wire_value(
    intended_val: Any,
    codec: Optional[FieldCodec],
) -> Any:
    if codec is not None and codec.kind == "bijection" and codec.mapping:
        mapped_vals = set(codec.mapping.values())
        for fallback in ("NY", "CA", "TX", "FL", "ZZ"):
            if fallback not in mapped_vals and fallback != str(intended_val):
                return fallback
            if codec.mapping.get(str(intended_val)) != fallback:
                return fallback
    if codec is not None and codec.kind == "iso_date":
        dt = _parse_flexible_date(str(intended_val))
        return (
            "1999-12-31"
            if (dt is None or dt.isoformat() != "1999-12-31")
            else "1998-11-30"
        )
    if codec is not None and codec.kind == "phone_digits":
        return "9995550199"
    if isinstance(intended_val, str):
        if len(intended_val) <= 3 and intended_val.isalpha():
            return "ZZ" if intended_val.upper() != "ZZ" else "YY"
        return f"{intended_val} tampered"
    if isinstance(intended_val, bool):
        return not intended_val
    if isinstance(intended_val, int):
        return intended_val + 97 if intended_val != 0 else 97
    if isinstance(intended_val, float):
        return round(intended_val + 97.25, 2)
    return "__wrong_wire_value__"


def _probe_inconclusive(candidate: EntityLensSpec) -> EntityLensSpec:
    """Fail safe when the probe could not create or read back its own record.

    Without a probe, nothing says whether the update route patches or replaces.
    A full-record write keeps every other field under either semantics, while
    a partial write can clear them on a replace route. So an update lens the
    probe could not check is returned in ``replace`` mode, never left as an
    unverified ``patch``.
    """
    if candidate.write_mode == "create":
        return candidate
    return candidate.model_copy(update={"write_mode": "replace"})


def probe_put_semantics(
    candidate: EntityLensSpec,
    *,
    sample_record: Mapping[str, Any],
    sample_update: Mapping[str, Any],
    auth_headers: Optional[Mapping[str, str]] = None,
    allow_remote_sandbox: bool = False,
) -> EntityLensSpec:
    """Empirically probe write quirks, field codecs, and volatile timestamps on sandbox."""
    _assert_sandbox_url_allowed(
        candidate.base_url, allow_remote_sandbox=allow_remote_sandbox
    )
    created_ids: list[str] = []
    tag = uuid.uuid4().hex[:8]
    try:
        seed = _uniquify_record_payload(
            sample_record, tag=f"quirk_{tag}", id_field=candidate.id_field
        )
        eid, created_rec = _create_probe_entity(
            candidate, seed, auth_headers=auth_headers, created_ids=created_ids
        )
        if eid is None:
            return _probe_inconclusive(candidate)

        status, etag_0, r0 = _read_one_entity(candidate, eid, auth_headers=auth_headers)
        if status < 200 or status >= 300 or r0 is None:
            return _probe_inconclusive(candidate)

        codecs = dict(candidate.field_codecs)
        volatile_list = list(candidate.volatile_fields)
        create_defaults_list = list(candidate.create_defaults)
        user_keys = set(_flatten_record(sample_record).keys()) | set(
            _flatten_record(sample_update).keys()
        )

        # Compare sample_record (seed) against initial read-back r0 for codecs & server metadata.
        flat_seed = _flatten_record(seed)
        flat_r0 = _flatten_record(r0)
        for path, raw_val in flat_seed.items():
            if path == candidate.id_field or path in volatile_list:
                continue
            if path in flat_r0 and flat_r0[path] != raw_val:
                inferred = infer_field_codec(
                    raw_val, flat_r0[path], existing=codecs.get(path)
                )
                if inferred is not None:
                    codecs[path] = inferred

        # A field whose name says it is server metadata is volatile at once. A
        # field that only looks like a timestamp (``due_date``, ``start_date``)
        # stays pending: it becomes volatile only if the probe sees it change
        # across a write that carried its old value. A volatile field leaves
        # the frame, so guessing from its name would hide a clobbered date.
        pending_volatile: list[str] = []
        for path, val in flat_r0.items():
            if path != candidate.id_field and path not in user_keys:
                if _VOLATILE_NAME_RE.search(path):
                    if path not in volatile_list:
                        volatile_list.append(path)
                elif _looks_like_metadata_or_volatile(path, val):
                    if path not in volatile_list and path not in pending_volatile:
                        pending_volatile.append(path)
                elif path not in create_defaults_list:
                    create_defaults_list.append(path)

        write_mode: WriteMode = candidate.write_mode
        if candidate.write_mode == "create" and sample_update:
            seed_upd = _uniquify_record_payload(
                {**sample_record, **sample_update},
                tag=f"quirk_c_{tag}",
                id_field=candidate.id_field,
            )
            eid_c, _ = _create_probe_entity(
                candidate, seed_upd, auth_headers=auth_headers, created_ids=created_ids
            )
            if eid_c is not None:
                st_c, _, r_c = _read_one_entity(
                    candidate, eid_c, auth_headers=auth_headers
                )
                if 200 <= st_c < 300 and r_c is not None:
                    flat_rc = _flatten_record(r_c)
                    for u_path, u_val in _flatten_record(seed_upd).items():
                        if (
                            u_path != candidate.id_field
                            and u_path not in volatile_list
                            and u_path in flat_rc
                            and flat_rc[u_path] != u_val
                        ):
                            inferred = infer_field_codec(
                                u_val, flat_rc[u_path], existing=codecs.get(u_path)
                            )
                            if inferred is not None:
                                codecs[u_path] = inferred
        elif (
            candidate.write_mode != "create"
            and candidate.write_endpoint.method in {"POST", "PUT", "PATCH"}
            and sample_update
        ):
            first_key = next(iter(sample_update.keys()))
            single_delta = {first_key: sample_update[first_key]}
            write_url = _format_endpoint_url(
                candidate.base_url, candidate.write_endpoint, entity_id=eid
            )
            hdrs = dict(auth_headers or {})
            if etag_0 and candidate.write_endpoint.etag_header:
                hdrs["If-Match"] = etag_0

            w_status, _, _ = _http_json(
                candidate.write_endpoint.method,
                write_url,
                payload=single_delta,
                headers=hdrs,
            )
            if w_status >= 400 or w_status == 0:
                # Server rejected partial payload (requires full replacement object).
                write_mode = "replace"
            else:
                r_status, etag_1, r_partial = _read_one_entity(
                    candidate, eid, auth_headers=auth_headers
                )
                if r_status >= 200 and r_status < 300 and r_partial is not None:
                    flat_partial = _flatten_record(r_partial)
                    # Check if the single updated field underwent server normalization.
                    for d_path, d_val in _flatten_record(single_delta).items():
                        if d_path in flat_partial and flat_partial[d_path] != d_val:
                            inferred = infer_field_codec(
                                d_val, flat_partial[d_path], existing=codecs.get(d_path)
                            )
                            if inferred is not None:
                                codecs[d_path] = inferred

                    # Check whether ANY omitted sample_record field was wiped/mutated!
                    for s_key in _flatten_record(sample_record).keys():
                        if (
                            s_key == candidate.id_field
                            or s_key == first_key
                            or s_key in single_delta
                            or s_key in volatile_list
                        ):
                            continue
                        if flat_partial.get(s_key, _MISSING) != flat_r0.get(
                            s_key, _MISSING
                        ):
                            write_mode = "replace"
                            break
                    etag_0 = etag_1 or etag_0

            # Now run a full merged update using r0 + sample_update to discover codecs on
            # remaining sample_update fields and detect genuine auto-changing volatile fields.
            merged_payload = {
                k: v
                for k, v in r0.items()
                if k != candidate.id_field
                and not _is_path_volatile(k, set(volatile_list))
            }
            merged_payload.update(sample_update)
            hdrs_merged = dict(auth_headers or {})
            hdrs_merged.setdefault("X-Actor-Id", "openadapt-probe-updater")
            if etag_0 and candidate.write_endpoint.etag_header:
                hdrs_merged["If-Match"] = etag_0
            m_status, _, _ = _http_json(
                candidate.write_endpoint.method,
                write_url,
                payload=merged_payload,
                headers=hdrs_merged,
            )
            if 200 <= m_status < 300:
                _, _, r_merged = _read_one_entity(
                    candidate, eid, auth_headers=auth_headers
                )
                if r_merged is not None:
                    flat_merged = _flatten_record(r_merged)
                    flat_upd = _flatten_record(sample_update)
                    for u_path, u_val in flat_upd.items():
                        if u_path in flat_merged and flat_merged[u_path] != u_val:
                            inferred = infer_field_codec(
                                u_val, flat_merged[u_path], existing=codecs.get(u_path)
                            )
                            if inferred is not None:
                                codecs[u_path] = inferred
                    for path, m_val in flat_merged.items():
                        if (
                            path != candidate.id_field
                            and path not in flat_upd
                            and path not in user_keys
                            and flat_r0.get(path, _MISSING) != m_val
                            and path not in volatile_list
                            and _looks_like_metadata_or_volatile(path, m_val)
                        ):
                            volatile_list.append(path)
                    # The merged write sent every untouched field back as read;
                    # one the server stored as the other empty spelling gets
                    # a null_empty codec.
                    for path, codec in learn_null_empty_codecs(
                        r0, r_merged, set(flat_upd)
                    ).items():
                        codecs.setdefault(path, codec)

        for path in pending_volatile:
            if path not in volatile_list and path not in create_defaults_list:
                create_defaults_list.append(path)

        identity_fields = list(candidate.identity_fields)
        if not identity_fields:
            volatile_set = set(volatile_list)
            untouched_keys = [
                k
                for k in sample_record.keys()
                if k != candidate.id_field
                and k not in volatile_set
                and k not in sample_update
            ]
            pool = (
                untouched_keys
                if untouched_keys
                else [
                    k
                    for k in sample_record.keys()
                    if k != candidate.id_field and k not in volatile_set
                ]
            )
            for cand in _CANONICAL_IDENTITY_KEYS:
                if cand in pool and cand not in identity_fields:
                    identity_fields.append(cand)
            for k in pool:
                if k not in identity_fields and len(identity_fields) < 2:
                    identity_fields.append(k)

        return candidate.model_copy(
            update={
                "write_mode": write_mode,
                "volatile_fields": tuple(volatile_list),
                "create_defaults": tuple(create_defaults_list),
                "field_codecs": codecs,
                "identity_fields": tuple(identity_fields),
            }
        )
    finally:
        _delete_probe_entities(candidate, created_ids, auth_headers=auth_headers)


def certify_lens_on_sandbox(
    candidate: EntityLensSpec,
    *,
    sample_record: Mapping[str, Any],
    sample_update: Mapping[str, Any],
    auth_headers: Optional[Mapping[str, str]] = None,
    allow_remote_sandbox: bool = False,
    judge: Optional[JudgeProtocol] = None,
    refine_quirks: bool = True,
) -> CertifiedLensPack:
    """Run quirk discovery + 6 adversarial/concurrency probes to certify a Lens."""
    _assert_sandbox_url_allowed(
        candidate.base_url, allow_remote_sandbox=allow_remote_sandbox
    )
    if not sample_record:
        raise ValueError("sample_record must be non-empty")
    if not sample_update:
        raise ValueError("sample_update must be non-empty")

    lens = (
        probe_put_semantics(
            candidate,
            sample_record=sample_record,
            sample_update=sample_update,
            auth_headers=auth_headers,
            allow_remote_sandbox=allow_remote_sandbox,
        )
        if refine_quirks
        else candidate
    )

    from openadapt_flow.tx.judge import FrameAndIntentJudge

    lens = lens.model_copy(update={"volatile_fields": effective_volatile_fields(lens)})
    active_judge: JudgeProtocol = judge or FrameAndIntentJudge(
        allowed_server_defaults=tuple(
            dict.fromkeys((lens.id_field, *lens.create_defaults))
        ),
    )
    created_ids: list[str] = []
    probes: list[CertificationProbeResult] = []
    run_tag = uuid.uuid4().hex[:8]

    def _make_expected_identity(r0: Mapping[str, Any]) -> dict[str, Any]:
        if lens.identity_fields:
            return {k: r0[k] for k in lens.identity_fields if k in r0}
        # Fallback if custom candidate had empty identity_fields and refine_quirks=False.
        return {}

    is_create_only = lens.write_mode == "create"

    try:
        # ------------------------------------------------------------------
        # Probe 1: clean_roundtrip (benign, expected_reject=False)
        # ------------------------------------------------------------------
        seed_1 = _uniquify_record_payload(
            {**sample_record, **sample_update} if is_create_only else sample_record,
            tag=f"p1_{run_tag}",
            id_field=lens.id_field,
        )
        eid_1, _ = _create_probe_entity(
            lens, seed_1, auth_headers=auth_headers, created_ids=created_ids
        )
        p1_reject = True
        p1_detail = ""
        if eid_1 is None:
            p1_detail = "Failed to create disposable sandbox record"
        elif is_create_only:
            st1, _, r1 = _read_one_entity(lens, eid_1, auth_headers=auth_headers)
            if st1 < 200 or st1 >= 300 or r1 is None:
                p1_detail = f"get_one post-read failed (status={st1})"
            else:
                verdict = active_judge.evaluate(
                    None,
                    r1,
                    seed_1,
                    volatile_fields=lens.volatile_fields,
                    field_codecs=lens.field_codecs,
                )
                p1_reject = not verdict.passed
                p1_detail = (
                    "Clean create-and-verify roundtrip passed"
                    if verdict.passed
                    else "; ".join(verdict.reasons)
                )
        else:
            st0, etag0, r0 = _read_one_entity(lens, eid_1, auth_headers=auth_headers)
            if st0 < 200 or st0 >= 300 or r0 is None:
                p1_detail = f"get_one pre-read failed (status={st0})"
            else:
                exp_id = _make_expected_identity(r0)
                if exp_id and not _check_precommit_identity(lens, r0, exp_id):
                    p1_detail = "Pre-commit identity check failed on target record"
                else:
                    w_payload = _build_write_payload(lens, r0, sample_update)
                    w_url = _format_endpoint_url(
                        lens.base_url, lens.write_endpoint, entity_id=eid_1
                    )
                    w_hdrs = dict(auth_headers or {})
                    if etag0 and lens.write_endpoint.etag_header:
                        w_hdrs["If-Match"] = etag0
                    wst, _, _ = _http_json(
                        lens.write_endpoint.method,
                        w_url,
                        payload=w_payload,
                        headers=w_hdrs,
                    )
                    if wst < 200 or wst >= 300:
                        p1_detail = f"write_endpoint failed (status={wst})"
                    else:
                        st1, _, r1 = _read_one_entity(
                            lens, eid_1, auth_headers=auth_headers
                        )
                        if st1 < 200 or st1 >= 300 or r1 is None:
                            p1_detail = f"get_one post-read failed (status={st1})"
                        else:
                            verdict = active_judge.evaluate(
                                r0,
                                r1,
                                sample_update,
                                volatile_fields=lens.volatile_fields,
                                field_codecs=lens.field_codecs,
                            )
                            p1_reject = not verdict.passed
                            p1_detail = (
                                "Clean read-merge-write verified"
                                if verdict.passed
                                else "; ".join(verdict.reasons)
                            )
        probes.append(
            CertificationProbeResult(
                probe_name="clean_roundtrip",
                polarity="benign",
                expected_reject=False,
                observed_reject=p1_reject,
                passed=(p1_reject is False),
                detail=p1_detail,
            )
        )

        # ------------------------------------------------------------------
        # Probe 2: benign_concurrent_other_record (benign, expected_reject=False)
        # ------------------------------------------------------------------
        seed_2 = _uniquify_record_payload(
            {**sample_record, **sample_update} if is_create_only else sample_record,
            tag=f"p2_{run_tag}",
            id_field=lens.id_field,
        )
        p2_reject = True
        p2_detail = ""
        if is_create_only:
            other_ids_c: list[str] = []
            for idx in range(5):
                other_seed = _uniquify_record_payload(
                    sample_record,
                    tag=f"bg_{run_tag}_{idx}",
                    mutate_all_fields=True,
                    id_field=lens.id_field,
                    preserve_codecs=lens.field_codecs,
                )
                oid, _ = _create_probe_entity(
                    lens, other_seed, auth_headers=auth_headers, created_ids=created_ids
                )
                if oid is not None:
                    other_ids_c.append(oid)
            eid_2, _ = _create_probe_entity(
                lens, seed_2, auth_headers=auth_headers, created_ids=created_ids
            )
            if eid_2 is not None:
                st1, _, r1 = _read_one_entity(lens, eid_2, auth_headers=auth_headers)
                if 200 <= st1 < 300 and r1 is not None:
                    verdict = active_judge.evaluate(
                        None,
                        r1,
                        seed_2,
                        volatile_fields=lens.volatile_fields,
                        field_codecs=lens.field_codecs,
                    )
                    p2_reject = not verdict.passed
                    p2_detail = (
                        f"O(1) verify unaffected by {len(other_ids_c)} concurrent creates"
                        if verdict.passed
                        else "; ".join(verdict.reasons)
                    )
        else:
            eid_2, _ = _create_probe_entity(
                lens, seed_2, auth_headers=auth_headers, created_ids=created_ids
            )
            if eid_2 is None:
                p2_detail = "Failed to create target record for concurrency probe"
            else:
                st0, etag0, r0 = _read_one_entity(
                    lens, eid_2, auth_headers=auth_headers
                )
                if st0 < 200 or st0 >= 300 or r0 is None:
                    p2_detail = f"get_one pre-read failed (status={st0})"
                else:
                    # Insert & mutate 5 OTHER records mid-transaction!
                    other_ids: list[str] = []
                    for idx in range(5):
                        other_seed = _uniquify_record_payload(
                            sample_record,
                            tag=f"bg_{run_tag}_{idx}",
                            mutate_all_fields=True,
                            id_field=lens.id_field,
                            preserve_codecs=lens.field_codecs,
                        )
                        oid, o_rec = _create_probe_entity(
                            lens,
                            other_seed,
                            auth_headers=auth_headers,
                            created_ids=created_ids,
                        )
                        if oid is not None:
                            other_ids.append(oid)
                            if o_rec is not None and lens.write_endpoint.method in {
                                "PUT",
                                "PATCH",
                            }:
                                bg_payload = _build_write_payload(
                                    lens, o_rec, other_seed
                                )
                                bg_url = _format_endpoint_url(
                                    lens.base_url, lens.write_endpoint, entity_id=oid
                                )
                                _http_json(
                                    lens.write_endpoint.method,
                                    bg_url,
                                    payload=bg_payload,
                                    headers=auth_headers,
                                )

                    w_payload = _build_write_payload(lens, r0, sample_update)
                    w_url = _format_endpoint_url(
                        lens.base_url, lens.write_endpoint, entity_id=eid_2
                    )
                    w_hdrs = dict(auth_headers or {})
                    if etag0 and lens.write_endpoint.etag_header:
                        w_hdrs["If-Match"] = etag0
                    wst, _, _ = _http_json(
                        lens.write_endpoint.method,
                        w_url,
                        payload=w_payload,
                        headers=w_hdrs,
                    )
                    if wst < 200 or wst >= 300:
                        p2_detail = f"write_endpoint failed under concurrent activity (status={wst})"
                    else:
                        st1, _, r1 = _read_one_entity(
                            lens, eid_2, auth_headers=auth_headers
                        )
                        if st1 < 200 or st1 >= 300 or r1 is None:
                            p2_detail = f"get_one post-read failed (status={st1})"
                        else:
                            verdict = active_judge.evaluate(
                                r0,
                                r1,
                                sample_update,
                                volatile_fields=lens.volatile_fields,
                                field_codecs=lens.field_codecs,
                            )
                            p2_reject = not verdict.passed
                            p2_detail = (
                                f"O(1) verify unaffected by {len(other_ids)} concurrent background mutations"
                                if verdict.passed
                                else "; ".join(verdict.reasons)
                            )
        probes.append(
            CertificationProbeResult(
                probe_name="benign_concurrent_other_record",
                polarity="benign",
                expected_reject=False,
                observed_reject=p2_reject,
                passed=(p2_reject is False),
                detail=p2_detail,
            )
        )

        # ------------------------------------------------------------------
        # Probe 3: fault_partial_clobber (harmful, expected_reject=True)
        # ------------------------------------------------------------------
        p3_reject = False
        p3_detail = ""
        if is_create_only:
            full_intended_3 = _uniquify_record_payload(
                {**sample_record, **sample_update},
                tag=f"p3_{run_tag}",
                id_field=lens.id_field,
            )
            drop_cands = [
                k
                for k in sample_record.keys()
                if k != lens.id_field and not _VOLATILE_NAME_RE.search(k)
            ]
            if drop_cands:
                clobber_k3 = drop_cands[0]
                partial_create = dict(full_intended_3)
                partial_create[clobber_k3] = _pick_wiped_value(
                    full_intended_3[clobber_k3]
                )
                eid_3c, _ = _create_probe_entity(
                    lens,
                    partial_create,
                    auth_headers=auth_headers,
                    created_ids=created_ids,
                )
                if eid_3c is not None:
                    st1, _, r1 = _read_one_entity(
                        lens, eid_3c, auth_headers=auth_headers
                    )
                    if 200 <= st1 < 300 and r1 is not None:
                        verdict = active_judge.evaluate(
                            None,
                            r1,
                            full_intended_3,
                            volatile_fields=lens.volatile_fields,
                            field_codecs=lens.field_codecs,
                        )
                        p3_reject = not verdict.passed
                        p3_detail = (
                            f"Rejected wiped create field {clobber_k3!r}"
                            if p3_reject
                            else f"FALSE ACCEPT: ignored wiped create field {clobber_k3!r}"
                        )
        else:
            seed_3 = _uniquify_record_payload(
                sample_record, tag=f"p3_{run_tag}", id_field=lens.id_field
            )
            eid_3, _ = _create_probe_entity(
                lens, seed_3, auth_headers=auth_headers, created_ids=created_ids
            )
            if eid_3 is not None:
                st0, etag0, r0 = _read_one_entity(
                    lens, eid_3, auth_headers=auth_headers
                )
                if 200 <= st0 < 300 and r0 is not None:
                    # Choose probe_delta and a user business field NOT in probe_delta to clobber.
                    untouched_business_keys = [
                        k
                        for k in sample_record.keys()
                        if k != lens.id_field
                        and k not in sample_update
                        and k in r0
                        and not _VOLATILE_NAME_RE.search(k)
                    ]
                    if untouched_business_keys:
                        probe_delta = dict(sample_update)
                        clobber_keys = untouched_business_keys
                    else:
                        first_upd_k = next(iter(sample_update.keys()))
                        probe_delta = {first_upd_k: sample_update[first_upd_k]}
                        clobber_keys = [
                            k
                            for k in sample_record.keys()
                            if k != lens.id_field
                            and k != first_upd_k
                            and k in r0
                            and not _VOLATILE_NAME_RE.search(k)
                        ]

                    w_url = _format_endpoint_url(
                        lens.base_url, lens.write_endpoint, entity_id=eid_3
                    )
                    w_hdrs = dict(auth_headers or {})
                    if etag0 and lens.write_endpoint.etag_header:
                        w_hdrs["If-Match"] = etag0
                    p3_detail = "fault not delivered: the server refused every clobber"
                    # A server may refuse the fault itself (a required field cannot
                    # be emptied). Only a clobber that actually landed tests the
                    # judge, so try the untouched fields until one lands.
                    for clobber_key in clobber_keys:
                        # First attempt raw unmerged partial payload; if server preserves
                        # frame (PATCH), explicitly inject a wiped untouched field.
                        wst, _, _ = _http_json(
                            lens.write_endpoint.method,
                            w_url,
                            payload=probe_delta,
                            headers=w_hdrs,
                        )
                        _, etag_mid, r_mid = _read_one_entity(
                            lens, eid_3, auth_headers=auth_headers
                        )
                        if (
                            wst < 200
                            or wst >= 300
                            or r_mid is None
                            or r_mid.get(clobber_key) == r0.get(clobber_key)
                        ):
                            clobbered_wire = _build_write_payload(lens, r0, probe_delta)
                            clobbered_wire[clobber_key] = _pick_wiped_value(
                                r0[clobber_key]
                            )
                            if etag_mid and lens.write_endpoint.etag_header:
                                w_hdrs["If-Match"] = etag_mid
                            _http_json(
                                lens.write_endpoint.method,
                                w_url,
                                payload=clobbered_wire,
                                headers=w_hdrs,
                            )

                        st1, etag_1, r1 = _read_one_entity(
                            lens, eid_3, auth_headers=auth_headers
                        )
                        if etag_1 and lens.write_endpoint.etag_header:
                            w_hdrs["If-Match"] = etag_1
                        if not (200 <= st1 < 300 and r1 is not None):
                            continue
                        if r1.get(clobber_key) == r0.get(clobber_key):
                            continue
                        verdict = active_judge.evaluate(
                            r0,
                            r1,
                            probe_delta,
                            volatile_fields=lens.volatile_fields,
                            field_codecs=lens.field_codecs,
                        )
                        p3_reject = not verdict.passed
                        p3_detail = (
                            f"Rejected clobbered frame field {clobber_key!r}"
                            if p3_reject
                            else f"FALSE ACCEPT: ignored clobbered field {clobber_key!r}"
                        )
                        break
        probes.append(
            CertificationProbeResult(
                probe_name="fault_partial_clobber",
                polarity="harmful",
                expected_reject=True,
                observed_reject=p3_reject,
                passed=(p3_reject is True),
                detail=p3_detail,
            )
        )

        # ------------------------------------------------------------------
        # Probe 4: fault_wrong_value (harmful, expected_reject=True)
        # ------------------------------------------------------------------
        p4_reject = False
        p4_detail = ""
        target_k = next(iter(sample_update.keys()))
        wrong_val = _pick_wrong_wire_value(
            sample_update[target_k], lens.field_codecs.get(target_k)
        )
        if is_create_only:
            full_intended_4 = _uniquify_record_payload(
                {**sample_record, **sample_update},
                tag=f"p4_{run_tag}",
                id_field=lens.id_field,
            )
            tampered_create = dict(full_intended_4)
            tampered_create[target_k] = wrong_val
            eid_4c, _ = _create_probe_entity(
                lens,
                tampered_create,
                auth_headers=auth_headers,
                created_ids=created_ids,
            )
            if eid_4c is not None:
                st1, _, r1 = _read_one_entity(lens, eid_4c, auth_headers=auth_headers)
                if 200 <= st1 < 300 and r1 is not None:
                    verdict = active_judge.evaluate(
                        None,
                        r1,
                        full_intended_4,
                        volatile_fields=lens.volatile_fields,
                        field_codecs=lens.field_codecs,
                    )
                    p4_reject = not verdict.passed
                    p4_detail = (
                        f"Rejected wire-corrupted create field {target_k!r}"
                        if p4_reject
                        else f"FALSE ACCEPT: accepted wrong value on {target_k!r}"
                    )
        else:
            seed_4 = _uniquify_record_payload(
                sample_record, tag=f"p4_{run_tag}", id_field=lens.id_field
            )
            eid_4, _ = _create_probe_entity(
                lens, seed_4, auth_headers=auth_headers, created_ids=created_ids
            )
            if eid_4 is not None:
                st0, etag0, r0 = _read_one_entity(
                    lens, eid_4, auth_headers=auth_headers
                )
                if 200 <= st0 < 300 and r0 is not None:
                    tampered_delta = dict(sample_update)
                    tampered_delta[target_k] = wrong_val
                    w_payload = _build_write_payload(lens, r0, tampered_delta)
                    w_url = _format_endpoint_url(
                        lens.base_url, lens.write_endpoint, entity_id=eid_4
                    )
                    w_hdrs = dict(auth_headers or {})
                    if etag0 and lens.write_endpoint.etag_header:
                        w_hdrs["If-Match"] = etag0
                    _http_json(
                        lens.write_endpoint.method,
                        w_url,
                        payload=w_payload,
                        headers=w_hdrs,
                    )
                    st1, _, r1 = _read_one_entity(
                        lens, eid_4, auth_headers=auth_headers
                    )
                    if 200 <= st1 < 300 and r1 is not None:
                        verdict = active_judge.evaluate(
                            r0,
                            r1,
                            sample_update,
                            volatile_fields=lens.volatile_fields,
                            field_codecs=lens.field_codecs,
                        )
                        p4_reject = not verdict.passed
                        p4_detail = (
                            f"Rejected wire-corrupted field {target_k!r}"
                            if p4_reject
                            else f"FALSE ACCEPT: accepted wrong value on {target_k!r}"
                        )
        probes.append(
            CertificationProbeResult(
                probe_name="fault_wrong_value",
                polarity="harmful",
                expected_reject=True,
                observed_reject=p4_reject,
                passed=(p4_reject is True),
                detail=p4_detail,
            )
        )

        # ------------------------------------------------------------------
        # Probe 5: fault_wrong_record (harmful, expected_reject=True)
        # ------------------------------------------------------------------
        seed_5_intended = _uniquify_record_payload(
            sample_record, tag=f"p5a_{run_tag}", id_field=lens.id_field
        )
        seed_5_wrong = _uniquify_record_payload(
            sample_record,
            tag=f"p5b_{run_tag}",
            mutate_all_fields=True,
            id_field=lens.id_field,
            preserve_codecs=lens.field_codecs,
        )
        eid_5a, _ = _create_probe_entity(
            lens, seed_5_intended, auth_headers=auth_headers, created_ids=created_ids
        )
        eid_5b, _ = _create_probe_entity(
            lens, seed_5_wrong, auth_headers=auth_headers, created_ids=created_ids
        )
        p5_reject = False
        p5_detail = ""
        if eid_5a is not None and eid_5b is not None:
            st_a, _, r_intended = _read_one_entity(
                lens, eid_5a, auth_headers=auth_headers
            )
            st_b, _, r_wrong = _read_one_entity(lens, eid_5b, auth_headers=auth_headers)
            if (
                200 <= st_a < 300
                and 200 <= st_b < 300
                and r_intended is not None
                and r_wrong is not None
            ):
                expected_identity = _make_expected_identity(r_intended)
                identity_matched = _check_precommit_identity(
                    lens, r_wrong, expected_identity
                )
                p5_reject = not identity_matched and bool(expected_identity)
                p5_detail = (
                    "Pre-commit identity gate blocked retargeted entity ID"
                    if p5_reject
                    else "FALSE ACCEPT: pre-commit gate failed to distinguish wrong record"
                )
        probes.append(
            CertificationProbeResult(
                probe_name="fault_wrong_record",
                polarity="harmful",
                expected_reject=True,
                observed_reject=p5_reject,
                passed=(p5_reject is True),
                detail=p5_detail,
            )
        )

        # ------------------------------------------------------------------
        # Probe 6: fault_no_persist (harmful, expected_reject=True)
        # ------------------------------------------------------------------
        seed_6 = _uniquify_record_payload(
            sample_record, tag=f"p6_{run_tag}", id_field=lens.id_field
        )
        # Ensure at least one field in seed_6 differs from sample_update (even under codecs).
        first_upd_key = next(iter(sample_update.keys()))
        already_matches_all = all(
            seed_6.get(k) == v
            or (
                k in lens.field_codecs
                and apply_field_codec(lens.field_codecs[k], v, seed_6.get(k))
            )
            for k, v in sample_update.items()
        )
        if already_matches_all:
            seed_6[first_upd_key] = _pick_wrong_wire_value(
                sample_update[first_upd_key], lens.field_codecs.get(first_upd_key)
            )
        eid_6, _ = _create_probe_entity(
            lens, seed_6, auth_headers=auth_headers, created_ids=created_ids
        )
        p6_reject = False
        p6_detail = ""
        if eid_6 is not None:
            st0, _, r0 = _read_one_entity(lens, eid_6, auth_headers=auth_headers)
            if 200 <= st0 < 300 and r0 is not None:
                # Claim write without mutating the store!
                st1, _, r1 = _read_one_entity(lens, eid_6, auth_headers=auth_headers)
                if 200 <= st1 < 300 and r1 is not None:
                    verdict = active_judge.evaluate(
                        r0,
                        r1,
                        sample_update,
                        volatile_fields=lens.volatile_fields,
                        field_codecs=lens.field_codecs,
                    )
                    p6_reject = not verdict.passed
                    p6_detail = (
                        "Rejected optimistic no-persist claim"
                        if p6_reject
                        else "FALSE ACCEPT: accepted unpersisted write claim"
                    )
        probes.append(
            CertificationProbeResult(
                probe_name="fault_no_persist",
                polarity="harmful",
                expected_reject=True,
                observed_reject=p6_reject,
                passed=(p6_reject is True),
                detail=p6_detail,
            )
        )
    finally:
        _delete_probe_entities(lens, created_ids, auth_headers=auth_headers)

    benign_probes = [p for p in probes if p.polarity == "benign"]
    harmful_probes = [p for p in probes if p.polarity == "harmful"]
    specificity = (
        sum(1 for p in benign_probes if not p.observed_reject) / len(benign_probes)
        if benign_probes
        else 0.0
    )
    sensitivity = (
        sum(1 for p in harmful_probes if p.observed_reject) / len(harmful_probes)
        if harmful_probes
        else 0.0
    )
    certified = sensitivity == 1.0 and specificity == 1.0

    pack = CertifiedLensPack(
        lens=lens,
        certified=False,
        probes_passed=certified,
        sensitivity=sensitivity,
        specificity=specificity,
        probes=tuple(probes),
    )
    return pack.with_computed_digest()


__all__ = [
    "DefaultFrameAndIntentJudge",
    "InductionRefused",
    "SandboxSafetyError",
    "apply_field_codec",
    "certify_lens_on_sandbox",
    "induce_lens_from_har",
    "infer_field_codec",
    "probe_put_semantics",
    "learn_null_empty_codecs",
]
