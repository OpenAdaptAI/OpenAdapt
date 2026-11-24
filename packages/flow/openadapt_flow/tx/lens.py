"""Target-row observations and storage-enforced mutation coordination.

HTTP prototypes retain read-back evidence and encrypted undo proposals without
asserting atomicity. The separate SQLite backend compares and writes one row
inside a storage transaction. Receipts state that narrow qualification scope.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from openadapt_types.check_level import Basis

from openadapt_flow.tx.api_readback import (
    CACHE_BYPASS_HEADERS,
    api_readback_basis,
    tx_evidence_scope,
)
from openadapt_flow.tx.receipt import (
    issue_tx_receipt,
    load_or_create_signing_key,
    read_private_record,
    write_private_record,
)
from openadapt_flow.tx.request_index import (
    RequestIndex,
    RequestIndexHeld,
    default_request_workdir,
)
from openadapt_flow.tx.sqlite_authority import SQLiteAtomicRowAuthority
from openadapt_flow.tx.types import (
    CertifiedLensPack,
    EndpointTemplate,
    EntityLensSpec,
    FieldCodec,
    FieldVerdict,
    JudgeProtocol,
    JudgeTier,
    JudgeVerdict,
    OCCStatus,
    TxCommitResult,
    TxOutcome,
    TxTask,
    WireSessionSummary,
    WireTapProtocol,
    canonical_sha256,
    effective_volatile_fields,
    open_exact_http,
)
from openadapt_flow.write_ledger import FileLedger, Lease, Ledger

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


# ---------------------------------------------------------------------------
# Dotted-path, codec, and merge helpers
# ---------------------------------------------------------------------------


def _get_path(obj: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve a flat or dotted field path inside ``obj``."""
    if path in obj:
        return True, obj[path]
    if "." not in path:
        return False, None
    cur: Any = obj
    for part in path.split("."):
        if isinstance(cur, Mapping):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            idx = int(part)
            if idx < 0 or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


def _flatten_leaves(
    obj: Mapping[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten nested mappings to dotted leaf paths (lists and ``{}`` stay leaves)."""
    out: dict[str, Any] = {}
    for raw_key, val in obj.items():
        key = str(raw_key)
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(val, Mapping) and len(val) > 0:
            out.update(_flatten_leaves(val, full))
        else:
            out[full] = val
    return out


def _normalize_iso_date(val: str) -> str:
    text = val.strip()
    if len(text) >= 10:
        candidate = text[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    return text


def _normalize_with_codec(val: Any, codec: FieldCodec) -> Any:
    kind = codec.kind
    if kind == "exact":
        return val
    if kind == "null_empty":
        return "" if val is None else val
    if kind == "bijection":
        s = str(val)
        return codec.mapping.get(s, s)
    if not isinstance(val, str):
        return val
    if kind == "casefold":
        return val.casefold()
    if kind == "upper":
        return val.upper()
    if kind == "lower":
        return val.lower()
    if kind == "capitalize_words":
        return " ".join(part.capitalize() for part in val.split())
    if kind == "strip_whitespace":
        return val.strip()
    if kind == "phone_digits":
        return "".join(ch for ch in val if ch.isdigit())
    if kind == "iso_date":
        return _normalize_iso_date(val)
    return val


def _codec_matches(
    expected_val: Any,
    actual_val: Any,
    codec: Optional[FieldCodec],
) -> tuple[bool, JudgeTier]:
    """Compare ``expected_val`` and ``actual_val`` exactly or via ``codec``."""
    from openadapt_flow.tx.judge import apply_field_codec, strict_equal

    if strict_equal(expected_val, actual_val):
        return True, "exact"
    if codec is None:
        return False, "failed"
    passed, _ = apply_field_codec(codec, expected_val, actual_val)
    return passed, "codec" if passed else "failed"


def _is_volatile_path(path: str, volatile_fields: tuple[str, ...]) -> bool:
    for vol in volatile_fields:
        if path == vol or path.startswith(f"{vol}."):
            return True
    return False


def _delete_dotted_path(target: dict[str, Any], path: str) -> None:
    if path in target:
        del target[path]
        return
    if "." not in path:
        return
    parts = path.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    cur: Any = target
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        parents.append((cur, part))
        cur = cur[part]
    if isinstance(cur, dict) and parts[-1] in cur:
        del cur[parts[-1]]
        for parent_dict, key in reversed(parents):
            child = parent_dict.get(key)
            if isinstance(child, dict) and len(child) == 0:
                del parent_dict[key]
            else:
                break


def strip_readonly_volatile(
    record: Mapping[str, Any],
    volatile_fields: tuple[str, ...],
    *,
    preserve_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a deep copy of ``record`` without volatile/read-only metadata fields."""
    cleaned: dict[str, Any] = copy.deepcopy(dict(record))
    preserved = set(preserve_paths)
    for vol in volatile_fields:
        if vol in preserved:
            continue
        _delete_dotted_path(cleaned, vol)
    return cleaned


def deep_merge(base: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``delta`` onto ``base`` without mutating either input.

    Supports both nested dictionaries and dotted field paths in ``delta`` so
    untouched sibling fields are never wiped by ``PUT``/``replace`` endpoints.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, val in delta.items():
        if "." in key and key not in merged:
            parts = key.split(".")
            cur: Any = merged
            can_nest = True
            for part in parts[:-1]:
                if part not in cur or cur[part] is None:
                    cur[part] = {}
                if not isinstance(cur[part], dict):
                    can_nest = False
                    break
                cur = cur[part]
            if can_nest:
                leaf = parts[-1]
                existing_leaf = cur.get(leaf)
                if isinstance(existing_leaf, Mapping) and isinstance(val, Mapping):
                    cur[leaf] = deep_merge(existing_leaf, val)
                else:
                    cur[leaf] = copy.deepcopy(val)
                continue

        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(val, Mapping):
            merged[key] = deep_merge(existing, val)
        else:
            merged[key] = copy.deepcopy(val)
    return merged


def _to_strict_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("v", "V")) and stripped[1:].isdigit():
            return int(stripped[1:])
        if stripped.lstrip("-").isdigit():
            try:
                return int(stripped)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Built-in standalone fallback Judge (used until tx.judge merges)
# ---------------------------------------------------------------------------


class _FallbackExactAndFrameJudge:
    """Standalone Pierce-Lens Frame + Intent Judge implementing ``JudgeProtocol``."""

    def evaluate(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_delta: Mapping[str, Any],
        *,
        volatile_fields: tuple[str, ...] = (),
        field_codecs: Optional[Mapping[str, FieldCodec]] = None,
    ) -> JudgeVerdict:
        from openadapt_flow.tx.judge import null_empty_equivalent

        codecs = dict(field_codecs or {})
        flat_r1 = _flatten_leaves(r1)
        flat_delta = _flatten_leaves(intended_delta)
        flat_r0 = _flatten_leaves(r0) if r0 is not None else None

        verdicts: list[FieldVerdict] = []
        mismatched: list[str] = []
        clobbered: list[str] = []
        extra: list[str] = []
        reasons: list[str] = []

        for path in sorted(flat_delta):
            intended_val = flat_delta[path]
            if path in flat_r1:
                found, stored_val = True, flat_r1[path]
            else:
                found, stored_val = _get_path(r1, path)

            if not found:
                reason = f"intended field '{path}' missing in post-commit state"
                mismatched.append(path)
                reasons.append(reason)
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="intended",
                        passed=False,
                        tier_used="failed",
                        reason=reason,
                    )
                )
                continue

            matched, tier = _codec_matches(intended_val, stored_val, codecs.get(path))
            if matched:
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="intended",
                        passed=True,
                        tier_used=tier,
                    )
                )
            else:
                reason = (
                    f"intended field '{path}' mismatch: "
                    f"expected {intended_val!r}, got {stored_val!r}"
                )
                mismatched.append(path)
                reasons.append(reason)
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="intended",
                        passed=False,
                        tier_used="failed",
                        reason=reason,
                    )
                )

        def _covered_by_delta(candidate: str) -> bool:
            for d_path in flat_delta:
                if (
                    candidate == d_path
                    or candidate.startswith(f"{d_path}.")
                    or d_path.startswith(f"{candidate}.")
                ):
                    return True
            return False

        all_paths = set(flat_r1)
        if flat_r0 is not None:
            all_paths |= set(flat_r0)

        for path in sorted(all_paths):
            if _covered_by_delta(path):
                continue
            if _is_volatile_path(path, volatile_fields):
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="volatile",
                        passed=True,
                        tier_used="volatile_skip",
                    )
                )
                continue

            if flat_r0 is None:
                extra.append(path)
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=True,
                        tier_used="frame_eq",
                        reason="created_default",
                    )
                )
                continue

            in_r0 = path in flat_r0
            in_r1 = path in flat_r1
            if (
                in_r0
                and in_r1
                and null_empty_equivalent(path, flat_r0[path], flat_r1[path], codecs)
            ):
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=True,
                        tier_used="codec",
                    )
                )
            elif in_r0 and in_r1 and flat_r0[path] == flat_r1[path]:
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=True,
                        tier_used="frame_eq",
                    )
                )
            else:
                clobbered.append(path)
                if in_r0 and not in_r1:
                    reason = (
                        f"frame field '{path}' wiped/deleted (was {flat_r0[path]!r})"
                    )
                elif not in_r0 and in_r1:
                    extra.append(path)
                    reason = (
                        f"unexpected non-volatile field '{path}' added "
                        f"({flat_r1[path]!r})"
                    )
                else:
                    reason = (
                        f"frame field '{path}' clobbered: "
                        f"{flat_r0[path]!r} -> {flat_r1[path]!r}"
                    )
                reasons.append(reason)
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=False,
                        tier_used="failed",
                        reason=reason,
                    )
                )

        frame_preserved = len(clobbered) == 0
        intent_satisfied = len(mismatched) == 0
        return JudgeVerdict(
            passed=frame_preserved and intent_satisfied,
            frame_preserved=frame_preserved,
            intent_satisfied=intent_satisfied,
            clobbered_fields=tuple(clobbered),
            mismatched_fields=tuple(mismatched),
            extra_fields=tuple(extra),
            field_verdicts=tuple(verdicts),
            reasons=tuple(reasons),
        )


def _resolve_default_judge(judge: Optional[JudgeProtocol]) -> JudgeProtocol:
    if judge is not None:
        return judge
    try:
        from openadapt_flow.tx.judge import FrameAndIntentJudge

        return FrameAndIntentJudge()
    except ImportError:
        return _FallbackExactAndFrameJudge()


# ---------------------------------------------------------------------------
# O(1) HTTP Entity Lens Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LensHttpResponse:
    """Normalized HTTP response returned by ``EntityLensClient``."""

    status_code: Optional[int]
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    etag: Optional[str] = None
    extracted_entity_id: Optional[str] = None
    network_error: Optional[str] = None
    #: The response's ``error_path`` value when the call failed inside a 2xx.
    call_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (
            self.status_code is not None
            and 200 <= self.status_code < 300
            and self.call_error is None
        )


def _render_template(
    template: str,
    context: Mapping[str, Any],
    *,
    url_quote: bool = False,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        found, val = _get_path(context, key)
        if not found or val is None:
            raise ValueError(f"missing template parameter '{key}'")
        text = str(val)
        return urllib.parse.quote(text, safe="") if url_quote else text

    return _PLACEHOLDER_RE.sub(_replace, template)


_INT_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z0-9_.-]+):int\}$")
_WHOLE_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z0-9_.-]+)\}$")


def render_body_template(
    template: Any,
    context: Mapping[str, Any],
    payload: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Fill an ``EndpointTemplate.body_template`` for one request.

    Containers are copied; ``"{payload}"`` becomes the write payload object,
    ``"{name:int}"`` an integer (or an error), a whole ``"{name}"`` keeps the
    value's type, and other strings substitute text.
    """
    if isinstance(template, Mapping):
        return {
            str(k): render_body_template(v, context, payload)
            for k, v in template.items()
        }
    if isinstance(template, list):
        return [render_body_template(v, context, payload) for v in template]
    if not isinstance(template, str):
        return template
    if template == "{payload}":
        if payload is None:
            raise ValueError("body template needs a payload")
        return dict(payload)
    as_int = _INT_PLACEHOLDER_RE.match(template)
    if as_int:
        found, val = _get_path(context, as_int.group(1))
        if not found or isinstance(val, bool):
            raise ValueError(f"missing integer template parameter '{as_int.group(1)}'")
        try:
            return int(str(val))
        except ValueError as exc:
            raise ValueError(
                f"template parameter '{as_int.group(1)}' is not an integer"
            ) from exc
    whole = _WHOLE_PLACEHOLDER_RE.match(template)
    if whole:
        found, val = _get_path(context, whole.group(1))
        if not found or val is None:
            raise ValueError(f"missing template parameter '{whole.group(1)}'")
        return val
    return _render_template(template, context)


def get_record_path(body: Any, path: str) -> tuple[bool, Any]:
    """Follow a dotted path with list indexes (``result.0``) into JSON."""
    cur = body
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


class EntityLensClient:
    """Declarative O(1) HTTP client driven strictly by an ``EntityLensSpec``."""

    def __init__(
        self,
        lens: EntityLensSpec,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.lens = lens
        self._base_headers: dict[str, str] = {
            "Accept": "application/json",
            **dict(headers or {}),
        }
        self.timeout_s = timeout_s

    def _build_url(
        self,
        endpoint: EndpointTemplate,
        context: Mapping[str, Any],
        extra_query: Optional[Mapping[str, str]] = None,
    ) -> str:
        base = self.lens.base_url.rstrip("/")
        rendered_path = _render_template(
            endpoint.path_template, context, url_quote=True
        )
        if not rendered_path.startswith("/"):
            rendered_path = "/" + rendered_path

        query: dict[str, str] = {}
        for qk, qv in endpoint.query_params.items():
            query[qk] = _render_template(qv, context, url_quote=False)
        if extra_query:
            for qk, qv in extra_query.items():
                if qk not in query:
                    query[qk] = qv

        url = f"{base}{rendered_path}"
        if query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(query)}"
        return url

    def _request(
        self,
        endpoint: EndpointTemplate,
        *,
        context: Mapping[str, Any],
        json_payload: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        extra_query: Optional[Mapping[str, str]] = None,
    ) -> LensHttpResponse:
        url = self._build_url(endpoint, context, extra_query=extra_query)
        req_headers = dict(self._base_headers)
        if extra_headers:
            req_headers.update(extra_headers)

        body_bytes: Optional[bytes] = None
        if endpoint.body_template is not None:
            try:
                rendered = render_body_template(
                    endpoint.body_template, context, json_payload
                )
            except ValueError as exc:
                return LensHttpResponse(status_code=None, network_error=str(exc))
            body_bytes = json.dumps(rendered).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif json_payload is not None:
            body_bytes = json.dumps(dict(json_payload)).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers=req_headers,
            method=endpoint.method,
        )

        status_code: Optional[int] = None
        raw_bytes = b""
        resp_headers: dict[str, str] = {}
        try:
            with open_exact_http(req, timeout=self.timeout_s) as resp:
                status_code = int(resp.status)
                raw_bytes = resp.read()
                resp_headers = {k: v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            try:
                raw_bytes = exc.read()
            except OSError:
                raw_bytes = b""
            if exc.headers is not None:
                resp_headers = {k: v for k, v in exc.headers.items()}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return LensHttpResponse(status_code=None, network_error=str(exc))

        parsed_json: Any = None
        if raw_bytes.strip():
            try:
                parsed_json = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed_json = None

        etag = self._extract_header(resp_headers, endpoint.etag_header or "ETag")
        extracted_id = self._extract_entity_id(endpoint, parsed_json, resp_headers)
        call_error: Optional[str] = None
        if endpoint.error_path:
            found_err, err_val = get_record_path(parsed_json, endpoint.error_path)
            if found_err and err_val is not None:
                call_error = json.dumps(err_val, default=str)[:500]
        return LensHttpResponse(
            status_code=status_code,
            json_body=parsed_json,
            headers=resp_headers,
            etag=etag,
            extracted_entity_id=extracted_id,
            call_error=call_error,
        )

    @staticmethod
    def _extract_header(
        headers: Mapping[str, str],
        target_name: str,
    ) -> Optional[str]:
        needle = target_name.lower()
        for k, v in headers.items():
            if k.lower() == needle and v.strip():
                return v.strip()
        return None

    def _extract_entity_id(
        self,
        endpoint: EndpointTemplate,
        parsed_json: Any,
        headers: Mapping[str, str],
    ) -> Optional[str]:
        if endpoint.record_path:
            found_rec, rec = get_record_path(parsed_json, endpoint.record_path)
            if found_rec and isinstance(rec, Mapping):
                for id_key in (endpoint.id_field, self.lens.id_field):
                    found, val = _get_path(rec, id_key)
                    if found and val is not None and str(val).strip():
                        return str(val)
            return None
        if isinstance(parsed_json, Mapping):
            target_obj: Mapping[str, Any] = parsed_json
            if endpoint.records_key and isinstance(
                parsed_json.get(endpoint.records_key), Mapping
            ):
                target_obj = parsed_json[endpoint.records_key]
            for id_key in (endpoint.id_field, self.lens.id_field):
                found, val = _get_path(target_obj, id_key)
                if found and val is not None and str(val).strip():
                    return str(val)

        location = self._extract_header(headers, "Location")
        if location:
            clean = location.split("?")[0].rstrip("/")
            if "/" in clean:
                tail = clean.rsplit("/", 1)[-1]
                if tail:
                    return urllib.parse.unquote(tail)
        return None

    def unwrap_single_record(
        self,
        endpoint: EndpointTemplate,
        body: Any,
    ) -> Optional[dict[str, Any]]:
        if endpoint.record_path:
            found, rec = get_record_path(body, endpoint.record_path)
            return dict(rec) if found and isinstance(rec, Mapping) else None
        if not isinstance(body, Mapping):
            return None
        if endpoint.records_key:
            inner = body.get(endpoint.records_key)
            return dict(inner) if isinstance(inner, Mapping) else None
        return dict(body)

    def get_one(
        self, entity_id: str, extra_headers: Optional[Mapping[str, str]] = None
    ) -> tuple[LensHttpResponse, Optional[dict[str, Any]], int]:
        """Fetch a single entity by ID in O(1). Returns ``(response, record, rows_read)``."""
        endpoint = self.lens.get_one
        ctx = {
            "id": entity_id,
            "entity_id": entity_id,
            self.lens.id_field: entity_id,
            endpoint.id_field: entity_id,
        }
        resp = self._request(endpoint, context=ctx, extra_headers=extra_headers)
        if not resp.ok:
            return resp, None, 0
        record = self.unwrap_single_record(endpoint, resp.json_body)
        if record is None:
            return resp, None, 0
        found, actual_id = _get_path(record, self.lens.id_field)
        if (
            not found
            or isinstance(actual_id, (bool, dict, list))
            or str(actual_id) != entity_id
        ):
            return resp, None, 1
        return resp, record, 1

    def search_duplicates(
        self,
        criteria: Mapping[str, Any],
    ) -> tuple[Optional[LensHttpResponse], list[dict[str, Any]], int]:
        """Run targeted duplicate check when ``duplicate_search`` and ``duplicate_keys`` exist.

        Returns ``(response, matching_records, rows_read)``.
        """
        endpoint = self.lens.duplicate_search
        if endpoint is None or not self.lens.duplicate_keys:
            return None, [], 0

        referenced: set[str] = set(_PLACEHOLDER_RE.findall(endpoint.path_template))
        for qv in endpoint.query_params.values():
            referenced.update(_PLACEHOLDER_RE.findall(qv))

        extra_query: dict[str, str] = {}
        for dk in self.lens.duplicate_keys:
            found, val = _get_path(criteria, dk)
            if not found or val is None:
                return None, [], 0
            if dk not in referenced:
                extra_query[dk] = str(val)

        resp = self._request(endpoint, context=criteria, extra_query=extra_query)
        if not resp.ok:
            return resp, [], 0

        raw_records = self._unwrap_record_list(endpoint, resp.json_body)
        rows_scanned = len(raw_records)

        matches: list[dict[str, Any]] = []
        for rec in raw_records:
            all_match = True
            for dk in self.lens.duplicate_keys:
                _, expected_val = _get_path(criteria, dk)
                found_actual, actual_val = _get_path(rec, dk)
                if not found_actual:
                    all_match = False
                    break
                ok_match, _ = _codec_matches(
                    expected_val,
                    actual_val,
                    self.lens.field_codecs.get(dk),
                )
                if not ok_match:
                    all_match = False
                    break
            if all_match:
                matches.append(rec)

        return resp, matches, rows_scanned

    @staticmethod
    def _unwrap_record_list(
        endpoint: EndpointTemplate,
        body: Any,
    ) -> list[dict[str, Any]]:
        candidates: Any = body
        if isinstance(body, Mapping):
            if endpoint.records_key and endpoint.records_key in body:
                candidates = body[endpoint.records_key]
            else:
                for fallback_key in ("items", "records", "results", "data", "entry"):
                    if isinstance(body.get(fallback_key), list):
                        candidates = body[fallback_key]
                        break
                else:
                    return [dict(body)]

        if not isinstance(candidates, list):
            return []
        out: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, Mapping):
                if (
                    "resource" in item
                    and isinstance(item["resource"], Mapping)
                    and endpoint.id_field not in item
                ):
                    out.append(dict(item["resource"]))
                else:
                    out.append(dict(item))
        return out

    def write(
        self,
        operation: Literal["create", "update"],
        payload: Mapping[str, Any],
        *,
        entity_id: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> LensHttpResponse:
        """Execute a ``create`` or ``update`` HTTP write against the system of record."""
        if operation == "create":
            endpoint = self.lens.create_endpoint or self.lens.write_endpoint
            ctx: dict[str, Any] = dict(payload)
        else:
            if not entity_id:
                raise ValueError("entity_id is required for update writes")
            endpoint = self.lens.write_endpoint
            ctx = {
                **dict(payload),
                "id": entity_id,
                "entity_id": entity_id,
                self.lens.id_field: entity_id,
                endpoint.id_field: entity_id,
            }

        extra_headers: dict[str, str] = {}
        if if_match:
            extra_headers["If-Match"] = if_match

        return self._request(
            endpoint,
            context=ctx,
            json_payload=payload,
            extra_headers=extra_headers,
        )

    def delete(
        self, entity_id: str, *, if_match: Optional[str] = None
    ) -> LensHttpResponse:
        """Execute ``DELETE`` via ``lens.delete_endpoint`` for create compensation."""
        endpoint = self.lens.delete_endpoint
        if endpoint is None:
            raise ValueError("delete_endpoint is not configured on EntityLensSpec")
        ctx = {
            "id": entity_id,
            "entity_id": entity_id,
            self.lens.id_field: entity_id,
            endpoint.id_field: entity_id,
        }
        return self._request(
            endpoint,
            context=ctx,
            extra_headers={"If-Match": if_match} if if_match else None,
        )


# ---------------------------------------------------------------------------
# Transactional Commit Engine
# ---------------------------------------------------------------------------


class TransactionalCommitEngine:
    """Target-record checks, durable write holds and guarded compensation over a lens.

    A confirmed target read does not establish complete server-side effects,
    atomicity across systems or a remotely witnessed transaction.
    """

    def __init__(
        self,
        lens: EntityLensSpec | CertifiedLensPack,
        workdir: Optional[Path | str] = None,
        *,
        judge: Optional[JudgeProtocol] = None,
        wire_tap: Optional[WireTapProtocol] = None,
        client: Optional[EntityLensClient] = None,
        headers: Optional[Mapping[str, str]] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
        ledger: Optional[Ledger] = None,
        pre_send_reread: bool = False,
        timeout_s: float = 10.0,
        atomic_authority: Optional[SQLiteAtomicRowAuthority] = None,
        allow_unqualified_prototype: bool = False,
    ) -> None:
        if isinstance(lens, CertifiedLensPack):
            # Revalidate copied models as well as parsed JSON; model_copy can bypass validation.
            report = CertifiedLensPack.model_validate(lens.model_dump())
            errors = report.probe_integrity_errors()
            if errors:
                raise ValueError(
                    "Refusing inconsistent probe report: " + ",".join(errors)
                )
            self.lens = report.lens
        else:
            self.lens = lens

        if atomic_authority is not None:
            if type(atomic_authority) is not SQLiteAtomicRowAuthority:
                raise ValueError("unrecognized atomic mutation authority")
            if atomic_authority.id_column != self.lens.id_field:
                raise ValueError("atomic authority identity differs from the lens")
        self.atomic_authority = atomic_authority
        self.allow_unqualified_prototype = allow_unqualified_prototype

        # Fail before reads, writes or local state when receipt integrity is unavailable.
        import importlib

        try:
            importlib.import_module("cbor2")
            importlib.import_module("cwt")
            from openadapt_flow.receipt_log import decode_sign1, encode_sign1

            decode_sign1(encode_sign1({}, {}, b"codec-readiness", lambda _: bytes(64)))
        except (ImportError, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                "Transaction receipts require compatible openadapt-flow[receipts] dependencies; install that extra before executing writes"
            ) from exc

        if workdir is None:
            from openadapt_flow.at_rest import local_key_path

            self.workdir = default_request_workdir(local_key_path().parent.parent)
        else:
            self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.judge: JudgeProtocol = _resolve_default_judge(judge)
        self.wire_tap: Optional[WireTapProtocol] = wire_tap
        self.client: EntityLensClient = client or EntityLensClient(
            self.lens,
            headers=headers,
            timeout_s=timeout_s,
        )
        self._ledger: Ledger = (
            ledger
            if ledger is not None
            else Ledger(FileLedger(self.workdir / "ledger"))
        )
        self._request_index = RequestIndex(self.workdir, self._ledger.backend)
        self._signing_key: Ed25519PrivateKey = (
            signing_key
            if signing_key is not None
            else load_or_create_signing_key(self.workdir)
        )
        self.pre_send_reread = pre_send_reread

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._signing_key.public_key()

    @property
    def log_dir(self) -> Path:
        return self.workdir / "log"

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    # -- internal helpers -----------------------------------------------------

    def _read_one(
        self, entity_id: str
    ) -> tuple[LensHttpResponse, Optional[dict[str, Any]], int]:
        if self.atomic_authority is None:
            return self.client.get_one(entity_id, extra_headers=CACHE_BYPASS_HEADERS)
        try:
            record = self.atomic_authority.read(entity_id)
        except (OSError, ValueError, sqlite3.Error):
            return (
                LensHttpResponse(status_code=None, network_error="atomic_read_failed"),
                None,
                0,
            )
        if record is None:
            return LensHttpResponse(status_code=404), None, 0
        response = LensHttpResponse(
            status_code=200,
            json_body=record,
            etag='"' + canonical_sha256(record) + '"',
            extracted_entity_id=entity_id,
        )
        return response, record, 1

    def _unqualified_block(self, task: TxTask, t0: float) -> TxCommitResult:
        return self._finalize_result(
            task=task,
            outcome="BLOCKED_PRECOMMIT",
            occ_status="UNKNOWN",
            entity_id=task.entity_id,
            r0=None,
            r1=None,
            wire_summary=None,
            judge_verdict=None,
            reasons=("enforcing_mutation_authority_required",),
            can_compensate=False,
            rows_read=0,
            t0=t0,
        )

    def _effective_volatile_fields(self) -> tuple[str, ...]:
        fields = effective_volatile_fields(self.lens)
        if self.atomic_authority is not None:
            fields = (*fields, self.atomic_authority.revision_column)
        return tuple(dict.fromkeys(fields))

    def _version_field_name(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
    ) -> Optional[str]:
        if self.atomic_authority is not None:
            return self.atomic_authority.revision_column
        configured = (
            self.lens.get_one.version_field or self.lens.write_endpoint.version_field
        )
        if configured:
            return configured
        if (
            "version" in self.lens.volatile_fields
            and r0 is not None
            and "version" in r0
            and "version" in r1
        ):
            return "version"
        return None

    def _author_field_name(self, r1: Mapping[str, Any]) -> Optional[str]:
        configured = (
            self.lens.get_one.author_field or self.lens.write_endpoint.author_field
        )
        if configured:
            return configured
        for fallback in ("updated_by", "author", "modified_by"):
            if fallback in r1:
                return fallback
        return None

    def _ledger_key(self, task: TxTask) -> str:
        return self._request_index.ledger_key(task.task_id)

    def _acquire_gate_or_block(
        self,
        task: TxTask,
        ledger_key: str,
        t0: float,
    ) -> tuple[Optional[Lease], Optional[TxCommitResult]]:
        lease = self._ledger.backend.lease(ledger_key)
        if lease is None:
            return None, self._finalize_result(
                task=task,
                outcome="BLOCKED_PRECOMMIT",
                occ_status="NOT_APPLICABLE",
                entity_id=task.entity_id,
                r0=None,
                r1=None,
                wire_summary=None,
                judge_verdict=None,
                reasons=("ledger_hold_active", "in_flight_lease"),
                can_compensate=False,
                rows_read=0,
                t0=t0,
            )

        prior = self._ledger.latest(ledger_key)
        if prior is not None and not prior.released:
            state = self._ledger.state(prior)
            prior_outcome = (prior.resolved or {}).get("outcome", "OPEN")
            lease.release()
            return None, self._finalize_result(
                task=task,
                outcome="BLOCKED_PRECOMMIT",
                occ_status="NOT_APPLICABLE",
                entity_id=task.entity_id,
                r0=None,
                r1=None,
                wire_summary=None,
                judge_verdict=None,
                reasons=(
                    "ledger_hold_active",
                    f"ledger_state:{state}",
                    f"prior_outcome:{prior_outcome}",
                ),
                can_compensate=False,
                rows_read=0,
                t0=t0,
            )

        return lease, None

    def _check_precommit_identity(
        self,
        r0: Mapping[str, Any],
        expected_identity: Mapping[str, Any],
    ) -> list[str]:
        mismatches: list[str] = []
        for field_path, expected_val in expected_identity.items():
            found, actual_val = _get_path(r0, field_path)
            if not found:
                mismatches.append(field_path)
                continue
            codec = (
                self.lens.field_codecs.get(field_path)
                if self.atomic_authority is None
                else None
            )
            matched, _ = _codec_matches(expected_val, actual_val, codec)
            if not matched:
                mismatches.append(field_path)
        return mismatches

    def _persist_undo_state(
        self,
        task_id: str,
        *,
        operation: Literal["create", "update"],
        entity_id: Optional[str],
        r0: Optional[Mapping[str, Any]],
    ) -> None:
        base_dir = self.workdir / self._request_index.task_key(task_id)
        base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if r0 is not None:
            r0_bytes = (
                json.dumps(dict(r0), sort_keys=True, separators=(",", ":"), default=str)
                + "\n"
            ).encode("utf-8")
            write_private_record(base_dir / "undo_r0.json", r0_bytes, overwrite=True)
        meta = {
            "task_id": task_id,
            "entity_name": self.lens.entity_name,
            "operation": operation,
            "entity_id": entity_id,
            "atomic_binding": self.atomic_authority.binding_sha256
            if self.atomic_authority
            else None,
        }
        meta_bytes = (
            json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        write_private_record(base_dir / "undo_meta.json", meta_bytes, overwrite=True)

    def _precommit_guard(
        self,
        task: TxTask,
        t0: float,
    ) -> tuple[
        Optional[dict[str, Any]],
        Optional[str],
        int,
        Optional[TxCommitResult],
    ]:
        """Run O(1) pre-commit reads and guards before any network write occurs.

        Returns ``(r0, r0_etag, rows_read, early_block_result)``.
        """
        rows_read = 0
        if task.operation == "update":
            if not task.entity_id:
                return (
                    None,
                    None,
                    0,
                    self._finalize_result(
                        task=task,
                        outcome="BLOCKED_PRECOMMIT",
                        occ_status="NOT_APPLICABLE",
                        entity_id=None,
                        r0=None,
                        r1=None,
                        wire_summary=None,
                        judge_verdict=None,
                        reasons=("missing_entity_id",),
                        can_compensate=False,
                        rows_read=0,
                        t0=t0,
                    ),
                )

            resp, r0, fetched = self._read_one(task.entity_id)
            rows_read += fetched
            if r0 is None:
                detail = (
                    f"http_{resp.status_code}"
                    if resp.status_code is not None
                    else "network_unreachable"
                )
                return (
                    None,
                    None,
                    rows_read,
                    self._finalize_result(
                        task=task,
                        outcome="BLOCKED_PRECOMMIT",
                        occ_status="NOT_APPLICABLE",
                        entity_id=task.entity_id,
                        r0=None,
                        r1=None,
                        wire_summary=None,
                        judge_verdict=None,
                        reasons=("precommit_target_unreadable", detail),
                        can_compensate=False,
                        rows_read=rows_read,
                        t0=t0,
                    ),
                )

            if task.expected_identity:
                mismatches = self._check_precommit_identity(r0, task.expected_identity)
                if mismatches:
                    return (
                        r0,
                        resp.etag,
                        rows_read,
                        self._finalize_result(
                            task=task,
                            outcome="BLOCKED_PRECOMMIT",
                            occ_status="NOT_APPLICABLE",
                            entity_id=task.entity_id,
                            r0=r0,
                            r1=None,
                            wire_summary=None,
                            judge_verdict=None,
                            reasons=(
                                "precommit_identity_mismatch",
                                *(f"mismatch:{m}" for m in mismatches),
                            ),
                            can_compensate=False,
                            rows_read=rows_read,
                            t0=t0,
                        ),
                    )

            self._persist_undo_state(
                task.task_id,
                operation="update",
                entity_id=task.entity_id,
                r0=r0,
            )
            return r0, resp.etag, rows_read, None

        # operation == "create"
        if self.lens.duplicate_search is not None and self.lens.duplicate_keys:
            if all(
                _get_path(task.intended_delta, k)[0] for k in self.lens.duplicate_keys
            ):
                dup_resp, matches, scanned = self.client.search_duplicates(
                    task.intended_delta
                )
                rows_read += scanned
                if dup_resp is not None and not dup_resp.ok:
                    return (
                        None,
                        None,
                        rows_read,
                        self._finalize_result(
                            task=task,
                            outcome="BLOCKED_PRECOMMIT",
                            occ_status="NOT_APPLICABLE",
                            entity_id=None,
                            r0=None,
                            r1=None,
                            wire_summary=None,
                            judge_verdict=None,
                            reasons=("duplicate_search_failed",),
                            can_compensate=False,
                            rows_read=rows_read,
                            t0=t0,
                        ),
                    )
                if matches:
                    dup_id: Optional[str] = None
                    found_id, raw_id = _get_path(matches[0], self.lens.id_field)
                    if found_id and raw_id is not None:
                        dup_id = str(raw_id)
                    return (
                        None,
                        None,
                        rows_read,
                        self._finalize_result(
                            task=task,
                            outcome="BLOCKED_PRECOMMIT",
                            occ_status="NOT_APPLICABLE",
                            entity_id=dup_id,
                            r0=None,
                            r1=None,
                            wire_summary=None,
                            judge_verdict=None,
                            reasons=("duplicate_record_exists",),
                            can_compensate=False,
                            rows_read=rows_read,
                            t0=t0,
                        ),
                    )

        return None, None, rows_read, None

    def _build_write_payload(
        self,
        task: TxTask,
        r0: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if task.operation == "create":
            payload = deep_merge({}, task.intended_delta)
        elif self.lens.write_mode == "replace":
            assert r0 is not None
            preserve = tuple(task.intended_delta.keys())
            stripped_r0 = strip_readonly_volatile(
                r0,
                self._effective_volatile_fields(),
                preserve_paths=preserve,
            )
            payload = deep_merge(stripped_r0, task.intended_delta)
        else:
            payload = deep_merge({}, task.intended_delta)

        if (
            self.lens.expected_author is not None
            and self.lens.write_endpoint.author_field is not None
            and self.lens.write_endpoint.author_field not in payload
        ):
            payload[self.lens.write_endpoint.author_field] = self.lens.expected_author
        return payload

    def _evaluate_postcommit(
        self,
        *,
        task: TxTask,
        entity_id: Optional[str],
        r0: Optional[Mapping[str, Any]],
        wire_summary: Optional[WireSessionSummary],
        action_error: Optional[str],
        rows_read: int,
        ledger_key: str,
        attempt_id: str,
        t0: float,
        atomic_postimage: Optional[Mapping[str, Any]] = None,
    ) -> TxCommitResult:
        reasons: list[str] = []
        unblocked_collateral = False
        incomplete_wire = action_error == "wire_evidence_missing"

        if wire_summary is not None:
            observed = tuple(m for m in wire_summary.mutations if not m.blocked)
            incomplete_wire = (
                wire_summary.session_id != task.task_id
                or wire_summary.mutations_allowed != 1
                or len(observed) != 1
                or observed[0].response_status is None
                or not 200 <= observed[0].response_status < 300
            )
            if incomplete_wire:
                reasons.append("wire_evidence_incomplete")

            endpoint = (
                self.lens.create_endpoint or self.lens.write_endpoint
                if task.operation == "create"
                else self.lens.write_endpoint
            )
            context = dict(task.intended_delta)
            if task.entity_id is not None:
                context.update(
                    {
                        "id": task.entity_id,
                        "entity_id": task.entity_id,
                        self.lens.id_field: task.entity_id,
                        endpoint.id_field: task.entity_id,
                    }
                )
            expected = urllib.parse.urlsplit(
                EntityLensClient(self.lens)._build_url(endpoint, context)
            )
            for mutation in observed:
                actual = urllib.parse.urlsplit(mutation.url)
                if (
                    mutation.method != endpoint.method
                    or (actual.scheme, actual.netloc, actual.path, actual.query)
                    != (expected.scheme, expected.netloc, expected.path, expected.query)
                    or (
                        task.operation == "update"
                        and mutation.extracted_entity_id is not None
                        and mutation.extracted_entity_id != task.entity_id
                    )
                ):
                    unblocked_collateral = True
                    reasons.append("wire_target_mismatch")

            if wire_summary.collateral_mutations > 0:
                unblocked_collateral = True
                reasons.append("wire_collateral_mutation")
            if wire_summary.mutations_blocked > 0:
                reasons.append("wire_mutation_blocked")

        if action_error is not None:
            reasons.append(f"action_error:{action_error}")

        if not entity_id:
            outcome: TxOutcome = "REFUTED" if unblocked_collateral else "UNCERTAIN"
            reasons.append("postcommit_entity_id_unknown")
            self._ledger.resolved(
                ledger_key,
                attempt_id,
                outcome=outcome,
                write_key_bound=True,
                delivered=True,
            )
            return self._finalize_result(
                task=task,
                outcome=outcome,
                occ_status="UNKNOWN",
                entity_id=None,
                r0=r0,
                r1=None,
                wire_summary=wire_summary,
                judge_verdict=None,
                reasons=tuple(reasons),
                can_compensate=False,
                rows_read=rows_read,
                t0=t0,
            )

        r1_resp, r1, fetched = self._read_one(entity_id)
        rows_read += fetched

        if r1 is None:
            if r1_resp.status_code == 404:
                outcome = "REFUTED"
                reasons.append("postcommit_target_missing")
            else:
                outcome = "REFUTED" if unblocked_collateral else "UNCERTAIN"
                reasons.append("postcommit_readback_failed")
            self._ledger.resolved(
                ledger_key,
                attempt_id,
                outcome=outcome,
                write_key_bound=True,
                delivered=True,
            )
            return self._finalize_result(
                task=task,
                outcome=outcome,
                occ_status="UNKNOWN",
                entity_id=entity_id,
                r0=r0,
                r1=None,
                wire_summary=wire_summary,
                judge_verdict=None,
                reasons=tuple(reasons),
                can_compensate=False,
                rows_read=rows_read,
                t0=t0,
            )

        if task.operation == "create":
            self._persist_undo_state(
                task.task_id,
                operation="create",
                entity_id=entity_id,
                r0=None,
            )

        # OCC & author attribution check
        occ_status: OCCStatus = (
            "NOT_APPLICABLE" if task.operation == "create" else "UNKNOWN"
        )
        if wire_summary is not None and any(
            (m.response_status in (409, 412)) and not m.blocked
            for m in wire_summary.mutations
        ):
            occ_status = "CONCURRENT_MODIFICATION"
            reasons.append("wire_occ_conflict")

        if self.lens.expected_author is not None:
            author_field = self._author_field_name(r1)
            found_author, actual_author = (
                _get_path(r1, author_field) if author_field else (False, None)
            )
            if not found_author or str(actual_author) != self.lens.expected_author:
                occ_status = "CONCURRENT_MODIFICATION"
                reasons.append("concurrent_author_mismatch")

        v_field = self._version_field_name(r0, r1)
        if v_field is not None and r0 is not None:
            found_v0, raw_v0 = _get_path(r0, v_field)
            found_v1, raw_v1 = _get_path(r1, v_field)
            if found_v0 and found_v1:
                int_v0 = _to_strict_int(raw_v0)
                int_v1 = _to_strict_int(raw_v1)
                if int_v0 is not None and int_v1 is not None:
                    if int_v1 != int_v0 + 1:
                        occ_status = "CONCURRENT_MODIFICATION"
                        reasons.append(
                            "concurrent_version_jump"
                            if int_v1 > int_v0 + 1
                            else "concurrent_version_not_next"
                        )
                    elif (
                        occ_status != "CONCURRENT_MODIFICATION"
                        and atomic_postimage is not None
                    ):
                        occ_status = "UNCONTESTED"

        from openadapt_flow.tx.judge import strict_equal

        if atomic_postimage is not None:
            if not strict_equal(r1, atomic_postimage):
                occ_status = "CONCURRENT_MODIFICATION"
                reasons.append("postcommit_state_changed")
            elif occ_status != "CONCURRENT_MODIFICATION":
                occ_status = "UNCONTESTED"
        else:
            reasons.append("unqualified_observation_only")

        eval_kwargs: dict[str, Any] = {
            "volatile_fields": self._effective_volatile_fields(),
            "field_codecs": self.lens.field_codecs,
        }
        if self.lens.create_defaults:
            import inspect as _inspect

            if (
                "allowed_server_defaults"
                in _inspect.signature(self.judge.evaluate).parameters
            ):
                eval_kwargs["allowed_server_defaults"] = tuple(
                    dict.fromkeys((self.lens.id_field, *self.lens.create_defaults))
                )

        verdict = self.judge.evaluate(
            r0,
            r1,
            task.intended_delta,
            **eval_kwargs,
        )

        for clobbered_field in verdict.clobbered_fields:
            reasons.append(f"clobbered:{clobbered_field}")
        for mismatched_field in verdict.mismatched_fields:
            reasons.append(f"mismatched:{mismatched_field}")
        for judge_reason in verdict.reasons:
            if judge_reason not in reasons:
                reasons.append(judge_reason)

        basis = (
            "owned_connection"
            if atomic_postimage is not None
            else api_readback_basis(
                operation=task.operation,
                r0=r0,
                r1=r1,
                headers=r1_resp.headers,
                status=r1_resp.status_code,
                version_field=v_field,
                occ_status=occ_status,
                cache_bypassed=self.atomic_authority is None,
            )
        )
        if unblocked_collateral:
            outcome = "REFUTED"
        elif occ_status == "CONCURRENT_MODIFICATION":
            outcome = "UNCERTAIN"
        elif not verdict.passed:
            outcome = "REFUTED"
        elif action_error is not None or incomplete_wire:
            outcome = "UNCERTAIN"
        elif basis is None:
            outcome = "UNCERTAIN"
        else:
            outcome = "CONFIRMED"

        self._ledger.resolved(
            ledger_key,
            attempt_id,
            outcome=outcome,
            write_key_bound=True,
            delivered=True,
        )

        can_compensate = bool(
            atomic_postimage is not None
            and self.atomic_authority is not None
            and occ_status == "UNCONTESTED"
            and task.operation == "update"
            and r0 is not None
        )
        postimage = {
            "entity_id": entity_id,
            "lens_sha256": canonical_sha256(self.lens.model_dump()),
            "record": dict(atomic_postimage) if atomic_postimage is not None else r1,
            "atomic_binding": self.atomic_authority.binding_sha256
            if can_compensate and self.atomic_authority is not None
            else None,
            "automatic_execution_allowed": can_compensate,
        }
        write_private_record(
            self.workdir
            / self._request_index.task_key(task.task_id)
            / "undo_postcommit.json",
            json.dumps(postimage, sort_keys=True).encode("utf-8"),
        )

        return self._finalize_result(
            task=task,
            outcome=outcome,
            occ_status=occ_status,
            entity_id=entity_id,
            r0=r0,
            r1=r1,
            wire_summary=wire_summary,
            judge_verdict=verdict,
            reasons=tuple(reasons),
            can_compensate=can_compensate,
            rows_read=rows_read,
            t0=t0,
            atomic_enforced=atomic_postimage is not None
            and occ_status == "UNCONTESTED",
            basis=basis,
        )

    def _finalize_result(
        self,
        *,
        task: TxTask,
        outcome: TxOutcome,
        occ_status: OCCStatus,
        entity_id: Optional[str],
        r0: Optional[Mapping[str, Any]],
        r1: Optional[Mapping[str, Any]],
        wire_summary: Optional[WireSessionSummary],
        judge_verdict: Optional[JudgeVerdict],
        reasons: tuple[str, ...],
        can_compensate: bool,
        rows_read: int,
        t0: float,
        atomic_enforced: bool = False,
        basis: Optional[Basis] = None,
    ) -> TxCommitResult:
        scope = tx_evidence_scope(
            outcome=outcome,
            basis=basis if outcome == "CONFIRMED" else None,
            read_back=r1 is not None,
            wire_observed=wire_summary is not None,
        )
        rec_id, rec_path = issue_tx_receipt(
            self.workdir,
            task_id=task.task_id,
            entity_name=self.lens.entity_name,
            operation=task.operation,
            outcome=outcome,
            occ_status=occ_status,
            intended_delta=task.intended_delta,
            r0=r0,
            r1=r1,
            wire_summary=wire_summary,
            rows_read=rows_read,
            private_key=self._signing_key,
            task_key=self._request_index.task_key(task.task_id),
            qualification_state="atomic_row_enforced"
            if atomic_enforced
            else "unqualified",
            evidence_scope=scope,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return TxCommitResult(
            task_id=task.task_id,
            outcome=outcome,
            occ_status=occ_status,
            entity_id=entity_id,
            wire_summary=wire_summary,
            judge_verdict=judge_verdict,
            reasons=reasons,
            can_compensate=can_compensate,
            qualification_state="atomic_row_enforced"
            if atomic_enforced
            else "unqualified",
            receipt_path=str(rec_path),
            receipt_id=rec_id,
            rows_read=rows_read,
            duration_ms=elapsed_ms,
            evidence_scope=scope,
        )

    # -- public execution API -------------------------------------------------

    def execute(self, task: TxTask) -> TxCommitResult:
        """Execute a storage-enforced update or an explicit unqualified HTTP prototype."""
        t0 = time.perf_counter()
        if (
            self.atomic_authority is None or task.operation != "update"
        ) and not self.allow_unqualified_prototype:
            return self._unqualified_block(task, t0)
        if (
            self.atomic_authority is not None
            and self.atomic_authority.revision_column in task.intended_delta
        ):
            return self._finalize_result(
                task=task,
                outcome="BLOCKED_PRECOMMIT",
                occ_status="UNKNOWN",
                entity_id=task.entity_id,
                r0=None,
                r1=None,
                wire_summary=None,
                judge_verdict=None,
                reasons=("reserved_atomic_revision_field",),
                can_compensate=False,
                rows_read=0,
                t0=t0,
            )
        ledger_key = self._ledger_key(task)
        lease, blocked = self._acquire_gate_or_block(task, ledger_key, t0)
        if blocked is not None:
            return blocked
        assert lease is not None

        try:
            r0, r0_etag, rows_read, early_block = self._precommit_guard(task, t0)
            if early_block is not None:
                return early_block

            if (
                self.pre_send_reread
                and task.operation == "update"
                and task.entity_id is not None
                and r0 is not None
            ):
                recheck_resp, r0_recheck, recheck_rows = self._read_one(task.entity_id)
                rows_read += recheck_rows
                v_field = self._version_field_name(r0, r0_recheck or {})
                etag_changed = (
                    r0_etag is not None
                    and recheck_resp.etag is not None
                    and recheck_resp.etag != r0_etag
                )
                ver_changed = (
                    r0_recheck is not None
                    and v_field is not None
                    and _get_path(r0, v_field) != _get_path(r0_recheck, v_field)
                )
                if r0_recheck is None or etag_changed or ver_changed:
                    return self._finalize_result(
                        task=task,
                        outcome="BLOCKED_PRECOMMIT",
                        occ_status="CONCURRENT_MODIFICATION",
                        entity_id=task.entity_id,
                        r0=r0,
                        r1=r0_recheck,
                        wire_summary=None,
                        judge_verdict=None,
                        reasons=("precommit_concurrent_modification",),
                        can_compensate=False,
                        rows_read=rows_read,
                        t0=t0,
                    )

            # Native storage enforces the caller's exact delta, independently of
            # candidate HTTP payload transformations or metadata author hints.
            payload = (
                dict(task.intended_delta)
                if self.atomic_authority is not None and task.operation == "update"
                else self._build_write_payload(task, r0)
            )
            attempt_id = (
                f"{self._request_index.task_key(task.task_id)}-{secrets.token_hex(6)}"
            )
            self._ledger.begun(ledger_key, attempt_id, write_key_bound=True)

            atomic_postimage = None
            if self.atomic_authority is not None and task.operation == "update":
                assert r0 is not None and task.entity_id is not None
                try:
                    changed = self.atomic_authority.compare_update(
                        task.entity_id, expected=r0, values=payload
                    )
                    atomic_postimage = changed.record if changed.applied else None
                    write_resp = LensHttpResponse(
                        status_code=200 if changed.applied else 412,
                        json_body=changed.record,
                        extracted_entity_id=task.entity_id,
                    )
                except (OSError, ValueError, sqlite3.Error):
                    write_resp = LensHttpResponse(
                        status_code=None, network_error="atomic_mutation_failed"
                    )
            else:
                write_resp = self.client.write(
                    task.operation,
                    payload,
                    entity_id=task.entity_id,
                    if_match=r0_etag if task.operation == "update" else None,
                )

            if write_resp.status_code in (409, 412):
                self._ledger.resolved(
                    ledger_key,
                    attempt_id,
                    outcome="UNCERTAIN",
                    write_key_bound=True,
                    delivered=True,
                )
                return self._finalize_result(
                    task=task,
                    outcome="BLOCKED_PRECOMMIT",
                    occ_status="CONCURRENT_MODIFICATION",
                    entity_id=task.entity_id,
                    r0=r0,
                    r1=None,
                    wire_summary=None,
                    judge_verdict=None,
                    reasons=(
                        "concurrent_modification",
                        f"http_{write_resp.status_code}",
                    ),
                    can_compensate=False,
                    rows_read=rows_read,
                    t0=t0,
                )

            if write_resp.network_error is not None:
                self._ledger.resolved(
                    ledger_key,
                    attempt_id,
                    outcome="UNCERTAIN",
                    write_key_bound=True,
                    delivered=True,
                )
                return self._finalize_result(
                    task=task,
                    outcome="UNCERTAIN",
                    occ_status="UNKNOWN",
                    entity_id=task.entity_id,
                    r0=r0,
                    r1=None,
                    wire_summary=None,
                    judge_verdict=None,
                    reasons=("write_network_error",),
                    can_compensate=False,
                    rows_read=rows_read,
                    t0=t0,
                )

            resolved_entity_id = task.entity_id or write_resp.extracted_entity_id
            if (
                resolved_entity_id is None
                and task.operation == "create"
                and self.lens.duplicate_search is not None
            ):
                _, dup_matches, dup_rows = self.client.search_duplicates(
                    task.intended_delta
                )
                rows_read += dup_rows
                if len(dup_matches) == 1:
                    found_id, raw_id = _get_path(dup_matches[0], self.lens.id_field)
                    if found_id and raw_id is not None:
                        resolved_entity_id = str(raw_id)

            action_err = f"http_{write_resp.status_code}" if not write_resp.ok else None
            return self._evaluate_postcommit(
                task=task,
                entity_id=resolved_entity_id,
                r0=r0,
                wire_summary=None,
                action_error=action_err,
                atomic_postimage=atomic_postimage,
                rows_read=rows_read,
                ledger_key=ledger_key,
                attempt_id=attempt_id,
                t0=t0,
            )
        finally:
            lease.release()

    def wrap_action(
        self,
        task: TxTask,
        action_fn: Callable[[], Any],
    ) -> TxCommitResult:
        """Wrap an external CUA/browser/agent callable inside O(1) transactional guards."""
        t0 = time.perf_counter()
        if not self.allow_unqualified_prototype:
            return self._unqualified_block(task, t0)
        ledger_key = self._ledger_key(task)
        lease, blocked = self._acquire_gate_or_block(task, ledger_key, t0)
        if blocked is not None:
            return blocked
        assert lease is not None

        try:
            r0, _, rows_read, early_block = self._precommit_guard(task, t0)
            if early_block is not None:
                return early_block

            attempt_id = (
                f"{self._request_index.task_key(task.task_id)}-{secrets.token_hex(6)}"
            )
            self._ledger.begun(ledger_key, attempt_id, write_key_bound=True)

            if self.wire_tap is not None:
                self.wire_tap.reset_session(task.task_id)

            action_error: Optional[str] = (
                "wire_evidence_missing" if self.wire_tap is None else None
            )
            action_ret: Any = None
            try:
                action_ret = action_fn()
                if hasattr(action_ret, "returncode") and action_ret.returncode != 0:
                    action_error = "external_exit_nonzero"
            except Exception as exc:  # noqa: BLE001
                action_error = type(exc).__name__

            wire_summary = (
                self.wire_tap.summary() if self.wire_tap is not None else None
            )

            resolved_entity_id = task.entity_id
            if resolved_entity_id is None and task.operation == "create":
                if wire_summary is not None and wire_summary.mutations:
                    first = wire_summary.mutations[0]
                    if first.extracted_entity_id and not first.blocked:
                        resolved_entity_id = first.extracted_entity_id
                    else:
                        for mut in wire_summary.mutations:
                            if mut.extracted_entity_id and not mut.blocked:
                                resolved_entity_id = mut.extracted_entity_id
                                break
                if (
                    resolved_entity_id is None
                    and self.lens.duplicate_search is not None
                ):
                    _, matches, scanned = self.client.search_duplicates(
                        task.intended_delta
                    )
                    rows_read += scanned
                    if len(matches) == 1:
                        found_id, raw_id = _get_path(matches[0], self.lens.id_field)
                        if found_id and raw_id is not None:
                            resolved_entity_id = str(raw_id)
                if resolved_entity_id is None and isinstance(action_ret, Mapping):
                    found_id, raw_id = _get_path(action_ret, self.lens.id_field)
                    if found_id and raw_id is not None:
                        resolved_entity_id = str(raw_id)

            return self._evaluate_postcommit(
                task=task,
                entity_id=resolved_entity_id,
                r0=r0,
                wire_summary=wire_summary,
                action_error=action_error,
                rows_read=rows_read,
                ledger_key=ledger_key,
                attempt_id=attempt_id,
                t0=t0,
            )
        finally:
            lease.release()

    def compensation_proposal(self, task_id: str) -> Optional[Path]:
        """Retain an encrypted review proposal; this path grants no mutation authority."""
        try:
            task_dir = self.workdir / self._request_index.task_key(task_id)
        except RequestIndexHeld:
            return None
        try:
            meta = json.loads(read_private_record(task_dir / "undo_meta.json"))
            post = json.loads(read_private_record(task_dir / "undo_postcommit.json"))
            before = (
                json.loads(read_private_record(task_dir / "undo_r0.json"))
                if meta.get("operation") == "update"
                else None
            )
        except (OSError, ValueError):
            return None
        proposal = task_dir / "undo_proposal.json"
        write_private_record(
            proposal,
            json.dumps(
                {
                    "schema": "openadapt.undo-proposal/v1",
                    "meta": meta,
                    "observed_after": post,
                    "before": before,
                    "automatic_execution_authority": False,
                },
                sort_keys=True,
            ).encode("utf-8"),
        )
        return proposal

    def compensate(self, task_id: str) -> bool:
        """Only the enforcing backend may compare and undo; HTTP undo is refused."""
        if self.atomic_authority is None:
            self.compensation_proposal(task_id)
            return False
        try:
            task_dir = self.workdir / self._request_index.task_key(task_id)
        except RequestIndexHeld:
            return False
        try:
            meta = json.loads(read_private_record(task_dir / "undo_meta.json"))
            post = json.loads(read_private_record(task_dir / "undo_postcommit.json"))
            before = json.loads(read_private_record(task_dir / "undo_r0.json"))
        except (OSError, ValueError):
            return False
        if (
            meta.get("operation") != "update"
            or not post.get("automatic_execution_allowed")
            or meta.get("atomic_binding") != self.atomic_authority.binding_sha256
            or post.get("atomic_binding") != self.atomic_authority.binding_sha256
            or post.get("lens_sha256") != canonical_sha256(self.lens.model_dump())
            or post.get("entity_id") != meta.get("entity_id")
            or not isinstance(before, dict)
            or not isinstance(post.get("record"), dict)
        ):
            return False
        entity_id = meta.get("entity_id")
        if not isinstance(entity_id, str):
            return False
        try:
            changed = self.atomic_authority.compare_update(
                entity_id, expected=post["record"], values=before
            )
        except (OSError, ValueError, sqlite3.Error):
            return False
        if not changed.applied or changed.record is None:
            return False
        from openadapt_flow.tx.judge import strict_equal

        revision = self.atomic_authority.revision_column
        return strict_equal(
            {k: v for k, v in before.items() if k != revision},
            {k: v for k, v in changed.record.items() if k != revision},
        )
