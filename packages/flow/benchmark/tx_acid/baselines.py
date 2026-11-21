"""Fair comparison baselines and reference O(1) tx arm for the ACID benchmark.

Implements two honest legacy/ungoverned baselines head-to-head against ``tx``:
1. ``NaiveAgent2xxArm``: Executes the intended HTTP write (e.g., partial ``PUT`` on
   replace-style endpoints, unguarded ``POST``, no ``If-Match``) and trusts any
   HTTP ``2xx`` status code without System-of-Record readback.
2. ``LegacyWholeTableContractArm``: Implements the legacy whole-table contract
   pattern faithfully:
   - Enforces the ``<4`` character short-parameter identity halt (``"Bo"``, ``"Li"``),
   - Refuses ``INDETERMINATE`` when ``GET /api/...?_limit=200`` returns ``links.next``
     (``>200`` rows without snapshot tokens) or when whole-table before/after diffs
     contain concurrent background worker writes,
   - Uses literal string equality on fields (``"Massachusetts" != "MA"``,
     ``"7 mill rd" != "7 MILL RD"``, ``"(416) 555-0199" != "+14165550199"``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping, Optional, Protocol, runtime_checkable
from urllib.parse import urlencode, urlparse, urlsplit

from openadapt_flow.tx.types import (
    EntityLensSpec,
    FieldCodec,
    FieldVerdict,
    JudgeVerdict,
    OCCStatus,
    TxCommitResult,
    TxOutcome,
    TxTask,
    WireMutationRecord,
    WireSessionSummary,
    canonical_sha256,
)

from .servers import (
    PAGE_LIMIT_CAP,
    BaseSorServer,
    FaultInjectingProxy,
    _FastThreadingHTTPServer,
    utc_iso_now,
)


@dataclass(frozen=True)
class ArmExecutionResult:
    """Standardized per-transaction execution record across all benchmark arms."""

    arm_name: str
    task_id: str
    accepted: bool
    outcome_label: str
    rows_read: int
    duration_ms: float
    reason: str = ""
    occ_status: str = "NOT_APPLICABLE"


@runtime_checkable
class BenchmarkArm(Protocol):
    """Protocol implemented by NaiveAgent2xxArm, LegacyWholeTableContractArm, and tx arms."""

    name: str

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult: ...


def _http_json_request(
    base_url: str,
    method: str,
    path_and_query: str,
    *,
    body: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    parts = urlsplit(base_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    payload = (
        json.dumps(body, separators=(",", ":")).encode("utf-8")
        if body is not None
        else b""
    )
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
        req_headers["Content-Length"] = str(len(payload))
    if headers:
        req_headers.update(headers)

    conn = http.client.HTTPConnection(host, port, timeout=15.0)
    try:
        conn.request(
            method,
            path_and_query,
            body=payload if body is not None else None,
            headers=req_headers,
        )
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        parsed: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw.decode("utf-8"))
                if isinstance(loaded, dict):
                    parsed = loaded
            except json.JSONDecodeError:
                parsed = {}
        return resp.status, resp_headers, parsed
    finally:
        conn.close()


def _format_endpoint_path(path_template: str, entity_id: Optional[str]) -> str:
    if "{id}" in path_template:
        if not entity_id:
            raise ValueError(f"entity_id required for template {path_template}")
        return path_template.replace("{id}", entity_id)
    return path_template


class NaiveAgent2xxArm:
    """Baseline 1: Fires the intended HTTP mutation and trusts any 2xx status code."""

    name: str = "naive_2xx"

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult:
        t0 = time.perf_counter()
        lens = server.lens_spec()
        if fault_proxy is not None:
            fault_proxy.set_upstream(server.base_url)
            target_base = fault_proxy.base_url
        else:
            target_base = server.base_url

        if task.operation == "create":
            ep = lens.create_endpoint
            if ep is None:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="NO_CREATE_ENDPOINT",
                    rows_read=0,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            method = ep.method
            path = ep.path_template
        else:
            ep = lens.write_endpoint
            method = ep.method
            path = _format_endpoint_path(ep.path_template, task.entity_id)

        status, _, _ = _http_json_request(
            target_base,
            method,
            path,
            body=dict(task.intended_delta),
            headers={"X-Actor-Id": task.actor_id},
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        accepted = 200 <= status < 300
        return ArmExecutionResult(
            arm_name=self.name,
            task_id=task.task_id,
            accepted=accepted,
            outcome_label="CONFIRMED_2XX" if accepted else f"HTTP_{status}",
            rows_read=0,
            duration_ms=elapsed_ms,
            reason="" if accepted else f"http_status_{status}",
        )


def _find_short_identity_param(
    task: TxTask,
    lens: EntityLensSpec,
) -> Optional[tuple[str, str]]:
    """Return (field, value) if a short (<4 char) identity parameter triggers legacy halt."""
    for key, val in task.expected_identity.items():
        if isinstance(val, str):
            stripped = val.strip()
            if 0 < len(stripped) < 4:
                return (key, stripped)
            # Multi-token short person names in expected_identity (e.g. "Bo Li")
            if key in ("fname", "lname", "claimant_name", "assignee"):
                for token in stripped.split():
                    if 0 < len(token) < 4:
                        return (key, token)

    for key in lens.identity_fields:
        val = task.intended_delta.get(key)
        if isinstance(val, str):
            stripped = val.strip()
            if 0 < len(stripped) < 4:
                return (key, stripped)

    for name_key in ("fname", "lname", "claimant_name", "assignee"):
        val = task.intended_delta.get(name_key)
        if isinstance(val, str):
            for token in val.strip().split():
                if 0 < len(token) < 4:
                    return (name_key, token)
    return None


class LegacyWholeTableContractArm:
    """Baseline 2: Honest whole-table before/after snapshot diff + literal string equality."""

    name: str = "legacy_whole_table"

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult:
        t0 = time.perf_counter()
        lens = server.lens_spec()

        # 1. Legacy <4-char short-parameter identity guard
        short_hit = _find_short_identity_param(task, lens)
        if short_hit is not None:
            field_name, bad_val = short_hit
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label="SHORT_PARAM_HALT",
                rows_read=0,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason=f"short_param_lt_4_chars:{field_name}={bad_val!r}",
            )

        rows_read = 0
        col_query = f"{server.collection_path}?_limit={PAGE_LIMIT_CAP}"

        # 2. Whole-table pre-snapshot
        pre_status, _, pre_env = _http_json_request(server.base_url, "GET", col_query)
        pre_items: list[dict[str, Any]] = list(pre_env.get("items", []))
        rows_read += len(pre_items)
        if pre_status != 200:
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label=f"PRE_READ_HTTP_{pre_status}",
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason="pre_snapshot_http_error",
            )
        if "next" in pre_env.get("links", {}):
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label="INDETERMINATE_PAGINATION_CAP",
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason="collection_exceeds_200_rows_without_snapshot_token",
            )

        pre_by_id: dict[str, dict[str, Any]] = {
            str(r[lens.id_field]): r for r in pre_items if lens.id_field in r
        }

        if task.operation == "update":
            if not task.entity_id or task.entity_id not in pre_by_id:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="TARGET_NOT_IN_SNAPSHOT",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    reason="target_entity_missing_from_pre_snapshot",
                )
            pre_target = pre_by_id[task.entity_id]
            for id_k, id_v in task.expected_identity.items():
                if pre_target.get(id_k) != id_v:
                    return ArmExecutionResult(
                        arm_name=self.name,
                        task_id=task.task_id,
                        accepted=False,
                        outcome_label="IDENTITY_MISMATCH",
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        reason=f"pre_identity_mismatch:{id_k}",
                    )

        # 3. Execute write call
        if fault_proxy is not None:
            fault_proxy.set_upstream(server.base_url)
            write_base = fault_proxy.base_url
        else:
            write_base = server.base_url

        if task.operation == "create":
            ep = lens.create_endpoint
            if ep is None:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="NO_CREATE_ENDPOINT",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            write_method = ep.method
            write_path = ep.path_template
            write_payload = dict(task.intended_delta)
        else:
            ep = lens.write_endpoint
            write_method = ep.method
            write_path = _format_endpoint_path(ep.path_template, task.entity_id)
            if lens.write_mode == "replace" and task.entity_id in pre_by_id:
                # Fair whole-table baseline: when <200 rows allowed pre-snapshot, merge onto pre_row
                write_payload = {**pre_by_id[task.entity_id], **task.intended_delta}
            else:
                write_payload = dict(task.intended_delta)

        w_status, _, _ = _http_json_request(
            write_base,
            write_method,
            write_path,
            body=write_payload,
            headers={"X-Actor-Id": task.actor_id},
        )
        if not (200 <= w_status < 300):
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label=f"WRITE_HTTP_{w_status}",
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason=f"write_http_{w_status}",
            )

        # 4. Whole-table post-snapshot
        post_status, _, post_env = _http_json_request(server.base_url, "GET", col_query)
        post_items: list[dict[str, Any]] = list(post_env.get("items", []))
        rows_read += len(post_items)
        if post_status != 200:
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label=f"POST_READ_HTTP_{post_status}",
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason="post_snapshot_http_error",
            )
        if "next" in post_env.get("links", {}):
            return ArmExecutionResult(
                arm_name=self.name,
                task_id=task.task_id,
                accepted=False,
                outcome_label="INDETERMINATE_PAGINATION_CAP",
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                reason="post_collection_exceeds_200_rows_without_snapshot_token",
            )

        post_by_id: dict[str, dict[str, Any]] = {
            str(r[lens.id_field]): r for r in post_items if lens.id_field in r
        }

        # 5. Whole-table isolation check (fails on concurrent background writes or collateral writes)
        if task.operation == "update":
            assert task.entity_id is not None
            if set(pre_by_id.keys()) != set(post_by_id.keys()):
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="INDETERMINATE_CONCURRENT_DIFF",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    reason="row_count_changed_during_whole_table_diff",
                )
            for rid, pre_row in pre_by_id.items():
                if rid == task.entity_id:
                    continue
                if post_by_id.get(rid) != pre_row:
                    return ArmExecutionResult(
                        arm_name=self.name,
                        task_id=task.task_id,
                        accepted=False,
                        outcome_label="INDETERMINATE_CONCURRENT_DIFF",
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        reason=f"unrelated_row_modified:{rid}",
                    )
            r0: Optional[dict[str, Any]] = pre_by_id[task.entity_id]
            r1 = post_by_id[task.entity_id]
        else:
            added_ids = sorted(set(post_by_id.keys()) - set(pre_by_id.keys()))
            removed_ids = set(pre_by_id.keys()) - set(post_by_id.keys())
            if len(added_ids) != 1 or removed_ids:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="INDETERMINATE_CONCURRENT_DIFF",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    reason=f"unexpected_added_count:{len(added_ids)}",
                )
            for rid, pre_row in pre_by_id.items():
                if post_by_id.get(rid) != pre_row:
                    return ArmExecutionResult(
                        arm_name=self.name,
                        task_id=task.task_id,
                        accepted=False,
                        outcome_label="INDETERMINATE_CONCURRENT_DIFF",
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        reason=f"unrelated_row_modified:{rid}",
                    )
            r0 = None
            r1 = post_by_id[added_ids[0]]

        # 6. Target row verification using literal equality (no codecs)
        volatile = set(lens.volatile_fields) | {lens.id_field}
        if r0 is not None:
            if int(r1.get("version", 0)) != int(r0.get("version", 0)) + 1:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="VERSION_JUMP_RACE",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    reason="version_increment_mismatch",
                )
            for k, old_val in r0.items():
                if k in volatile or k in task.intended_delta:
                    continue
                if r1.get(k) != old_val:
                    return ArmExecutionResult(
                        arm_name=self.name,
                        task_id=task.task_id,
                        accepted=False,
                        outcome_label="FRAME_CLOBBERED",
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        reason=f"frame_field_changed:{k}",
                    )

        for k, expected_val in task.intended_delta.items():
            if r1.get(k) != expected_val:
                return ArmExecutionResult(
                    arm_name=self.name,
                    task_id=task.task_id,
                    accepted=False,
                    outcome_label="LITERAL_MISMATCH",
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    reason=f"literal_mismatch:{k}:{expected_val!r}!={r1.get(k)!r}",
                )

        return ArmExecutionResult(
            arm_name=self.name,
            task_id=task.task_id,
            accepted=True,
            outcome_label="CONFIRMED",
            rows_read=rows_read,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )


def apply_field_codec_match(
    intended: Any,
    stored: Any,
    codec: Optional[FieldCodec],
) -> tuple[bool, bool]:
    """Evaluate (matched, used_codec) under openadapt_flow.tx.types.FieldCodec."""
    if stored == intended:
        return True, False
    if codec is None or codec.kind == "exact":
        return False, False

    s_in = str(intended)
    s_st = str(stored)
    kind = codec.kind
    if kind == "casefold":
        return s_in.casefold() == s_st.casefold(), True
    if kind == "upper":
        return s_in.strip().upper() == s_st.strip().upper(), True
    if kind == "lower":
        return s_in.strip().lower() == s_st.strip().lower(), True
    if kind == "strip_whitespace":
        return s_in.strip() == s_st.strip(), True
    if kind == "capitalize_words":
        norm_in = " ".join(p.capitalize() for p in s_in.strip().split())
        norm_st = " ".join(p.capitalize() for p in s_st.strip().split())
        return norm_in == norm_st, True
    if kind == "phone_digits":
        d_in = "".join(ch for ch in s_in if ch.isdigit())
        d_st = "".join(ch for ch in s_st if ch.isdigit())
        if len(d_in) == 11 and d_in.startswith("1"):
            d_in = d_in[1:]
        if len(d_st) == 11 and d_st.startswith("1"):
            d_st = d_st[1:]
        return bool(d_in) and d_in == d_st, True
    if kind == "iso_date":
        date_in = s_in.strip()[:10]
        date_st = s_st.strip()[:10]
        return len(date_in) == 10 and date_in == date_st, True
    if kind == "bijection":
        cf_map = {k.casefold(): v for k, v in codec.mapping.items()}
        mapped = cf_map.get(s_in.strip().casefold(), s_in.strip())
        return mapped.upper() == s_st.strip().upper(), True
    return False, False


class _ReferenceJudge:
    """Stdlib Pierce-Lens Judge implementing openadapt_flow.tx.types.JudgeProtocol."""

    def evaluate(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_delta: Mapping[str, Any],
        *,
        volatile_fields: tuple[str, ...] = (),
        field_codecs: Optional[Mapping[str, FieldCodec]] = None,
    ) -> JudgeVerdict:
        codecs = field_codecs or {}
        volatile_set = set(volatile_fields) | {"id"}
        clobbered: list[str] = []
        mismatched: list[str] = []
        verdicts: list[FieldVerdict] = []
        reasons: list[str] = []

        if r0 is not None:
            for k, old_val in r0.items():
                if k in intended_delta:
                    continue
                if k in volatile_set:
                    verdicts.append(
                        FieldVerdict(
                            field_path=k,
                            category="volatile",
                            passed=True,
                            tier_used="volatile_skip",
                        )
                    )
                    continue
                same = r1.get(k) == old_val
                if not same:
                    clobbered.append(k)
                    reasons.append(f"frame_clobbered:{k}")
                verdicts.append(
                    FieldVerdict(
                        field_path=k,
                        category="frame",
                        passed=same,
                        tier_used="frame_eq" if same else "failed",
                        reason="" if same else f"{old_val!r} -> {r1.get(k)!r}",
                    )
                )

        for k, intended_val in intended_delta.items():
            matched, used_codec = apply_field_codec_match(
                intended_val,
                r1.get(k),
                codecs.get(k),
            )
            if not matched:
                mismatched.append(k)
                reasons.append(f"intent_mismatch:{k}")
            verdicts.append(
                FieldVerdict(
                    field_path=k,
                    category="intended",
                    passed=matched,
                    tier_used=(
                        ("codec" if used_codec else "exact") if matched else "failed"
                    ),
                    reason=""
                    if matched
                    else f"expected {intended_val!r}, got {r1.get(k)!r}",
                )
            )

        frame_ok = len(clobbered) == 0
        intent_ok = len(mismatched) == 0
        return JudgeVerdict(
            passed=frame_ok and intent_ok,
            frame_preserved=frame_ok,
            intent_satisfied=intent_ok,
            clobbered_fields=tuple(clobbered),
            mismatched_fields=tuple(mismatched),
            field_verdicts=tuple(verdicts),
            reasons=tuple(reasons),
        )


class _LocalWireTap:
    """Lightweight session-scoped wire tap implementing WireTapProtocol on stdlib."""

    def __init__(self, upstream_url: str) -> None:
        self._upstream_url = upstream_url.rstrip("/")
        self._lock = threading.Lock()
        self._session_id = "session-0"
        self._expected_entity_id: Optional[str] = None
        self._total_requests = 0
        self._read_requests = 0
        self._mutations: list[WireMutationRecord] = []
        self._httpd: Optional[_FastThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        if self._httpd is None or self.port == 0:
            raise RuntimeError("_LocalWireTap has not been started")
        return f"http://127.0.0.1:{self.port}"

    def reset_session(
        self,
        session_id: Optional[str] = None,
        *,
        expected_entity_id: Optional[str] = None,
    ) -> str:
        with self._lock:
            self._session_id = session_id or f"tx-{int(time.time() * 1000000)}"
            self._expected_entity_id = expected_entity_id
            self._total_requests = 0
            self._read_requests = 0
            self._mutations.clear()
            return self._session_id

    def summary(self) -> WireSessionSummary:
        with self._lock:
            muts = tuple(self._mutations)
            allowed = sum(1 for m in muts if not m.blocked)
            blocked = sum(1 for m in muts if m.blocked)
            collateral = 0
            for idx, m in enumerate(muts):
                if idx > 0:
                    collateral += 1
                elif (
                    self._expected_entity_id is not None
                    and m.extracted_entity_id is not None
                    and m.extracted_entity_id != self._expected_entity_id
                ):
                    collateral += 1
            digest = canonical_sha256([m.model_dump() for m in muts])
            return WireSessionSummary(
                session_id=self._session_id,
                total_requests=self._total_requests,
                read_requests=self._read_requests,
                mutations_allowed=allowed,
                mutations_blocked=blocked,
                collateral_mutations=collateral,
                mutations=muts,
                ledger_sha256=digest,
            )

    def _proxy(
        self,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(self._upstream_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        conn = http.client.HTTPConnection(host, port, timeout=15.0)
        try:
            fwd_headers = {
                k: v
                for k, v in headers.items()
                if k.lower()
                not in ("host", "content-length", "connection", "transfer-encoding")
            }
            fwd_headers["Content-Length"] = str(len(raw_body))
            conn.request(method, raw_path, body=raw_body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = {
                k: v
                for k, v in resp.getheaders()
                if k.lower()
                not in (
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "date",
                    "server",
                )
            }
        finally:
            conn.close()

        parsed_path = urlparse(raw_path).path
        segs = [s for s in parsed_path.split("/") if s]
        extracted_id: Optional[str] = (
            segs[2] if len(segs) >= 3 and segs[0] == "api" else None
        )
        etag_val = None
        for hk, hv in resp_headers.items():
            if hk.lower() == "etag":
                etag_val = hv
                break
        if extracted_id is None and resp_body:
            try:
                resp_json = json.loads(resp_body.decode("utf-8"))
                if isinstance(resp_json, dict) and "id" in resp_json:
                    extracted_id = str(resp_json["id"])
            except json.JSONDecodeError:
                pass

        with self._lock:
            self._total_requests += 1
            if method in ("GET", "HEAD", "OPTIONS"):
                self._read_requests += 1
            else:
                seq = len(self._mutations) + 1
                self._mutations.append(
                    WireMutationRecord(
                        seq=seq,
                        method=method,
                        url=f"{self._upstream_url}{raw_path}",
                        path=parsed_path,
                        request_body_sha256=hashlib.sha256(raw_body).hexdigest(),
                        response_status=resp.status,
                        response_body_sha256=hashlib.sha256(resp_body).hexdigest(),
                        extracted_entity_id=extracted_id,
                        etag=etag_val,
                        timestamp_utc=utc_iso_now(),
                    )
                )
        return resp.status, resp_headers, resp_body

    def start(self) -> "_LocalWireTap":
        if self._httpd is not None:
            return self
        tap_ref = self

        class _TapHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                hdrs = {k: v for k, v in self.headers.items()}
                status, resp_headers, resp_body = tap_ref._proxy(
                    method, self.path, hdrs, raw_body
                )
                self.send_response(status)
                self.send_header("Content-Length", str(len(resp_body)))
                for hk, hv in resp_headers.items():
                    self.send_header(hk, hv)
                self.end_headers()
                self.wfile.write(resp_body)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_PATCH(self) -> None:
                self._dispatch("PATCH")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._httpd = _FastThreadingHTTPServer(("127.0.0.1", 0), _TapHandler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="tx-reference-wire-tap",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


class ReferenceTxArm:
    """Reference O(1) Transactional Commit Layer built strictly on openadapt_flow.tx.types."""

    name: str = "tx_acid"

    def __init__(self) -> None:
        self._judge = _ReferenceJudge()
        self._tap_cache: dict[str, _LocalWireTap] = {}

    def _get_tap(self, server: BaseSorServer) -> _LocalWireTap:
        key = server.base_url
        tap = self._tap_cache.get(key)
        if tap is None:
            tap = _LocalWireTap(server.base_url).start()
            self._tap_cache[key] = tap
        return tap

    def close(self) -> None:
        for tap in self._tap_cache.values():
            tap.close()
        self._tap_cache.clear()

    def commit_tx(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> TxCommitResult:
        t0 = time.perf_counter()
        lens = server.lens_spec()
        tap = self._get_tap(server)
        tap.reset_session(task.task_id, expected_entity_id=task.entity_id)

        if fault_proxy is not None:
            fault_proxy.set_upstream(tap.base_url)
            action_base = fault_proxy.base_url
        else:
            action_base = tap.base_url

        rows_read = 0
        r0: Optional[dict[str, Any]] = None
        r0_etag: Optional[str] = None

        if task.operation == "update":
            if not task.entity_id:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="BLOCKED_PRECOMMIT",
                    reasons=("missing_entity_id",),
                    rows_read=0,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            get_path = _format_endpoint_path(lens.get_one.path_template, task.entity_id)
            st0, hdr0, body0 = _http_json_request(server.base_url, "GET", get_path)
            if st0 != 200:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="BLOCKED_PRECOMMIT",
                    entity_id=task.entity_id,
                    reasons=(f"r0_read_http_{st0}",),
                    rows_read=0,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            rows_read += 1
            r0 = body0
            r0_etag = hdr0.get("etag") or (
                str(r0.get("etag")) if "etag" in r0 else None
            )

            # Identity check using codec-aware matching (never halts on 2-3 char names!)
            for id_k, id_v in task.expected_identity.items():
                ok, _ = apply_field_codec_match(
                    id_v, r0.get(id_k), lens.field_codecs.get(id_k)
                )
                if not ok:
                    return TxCommitResult(
                        task_id=task.task_id,
                        outcome="BLOCKED_PRECOMMIT",
                        entity_id=task.entity_id,
                        reasons=(f"precommit_identity_mismatch:{id_k}",),
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )

            write_ep = lens.write_endpoint
            write_path = _format_endpoint_path(write_ep.path_template, task.entity_id)
            if lens.write_mode == "replace":
                # Read-Merge-Write synthesis prevents partial PUT clobbers!
                payload = {**r0, **task.intended_delta}
            else:
                payload = dict(task.intended_delta)

            write_headers = {"X-Actor-Id": task.actor_id}
            if r0_etag:
                write_headers["If-Match"] = r0_etag

            w_status, _, _ = _http_json_request(
                action_base,
                write_ep.method,
                write_path,
                body=payload,
                headers=write_headers,
            )
            wire_sum = tap.summary()
            if w_status == 412:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="BLOCKED_PRECOMMIT",
                    occ_status="CONCURRENT_MODIFICATION",
                    entity_id=task.entity_id,
                    wire_summary=wire_sum,
                    reasons=("occ_precondition_failed_412",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            if not (200 <= w_status < 300):
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="REFUTED",
                    entity_id=task.entity_id,
                    wire_summary=wire_sum,
                    reasons=(f"write_http_{w_status}",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )

            if wire_sum.collateral_mutations > 0 or wire_sum.mutations_allowed != 1:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="REFUTED",
                    occ_status="UNCONTESTED",
                    entity_id=task.entity_id,
                    wire_summary=wire_sum,
                    reasons=("wire_completeness_violation",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )

            st1, _, r1 = _http_json_request(server.base_url, "GET", get_path)
            if st1 != 200:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="UNCERTAIN",
                    entity_id=task.entity_id,
                    wire_summary=wire_sum,
                    reasons=(f"r1_read_http_{st1}",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            rows_read += 1

            occ: OCCStatus = "UNCONTESTED"
            if (
                lens.get_one.version_field
                and lens.get_one.version_field in r0
                and lens.get_one.version_field in r1
            ):
                v0 = int(r0[lens.get_one.version_field])
                v1 = int(r1[lens.get_one.version_field])
                if v1 > v0 + 1:
                    occ = "CONCURRENT_MODIFICATION"
                    return TxCommitResult(
                        task_id=task.task_id,
                        outcome="REFUTED",
                        occ_status=occ,
                        entity_id=task.entity_id,
                        wire_summary=wire_sum,
                        reasons=("version_jumped_concurrent_modification",),
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )

            verdict = self._judge.evaluate(
                r0,
                r1,
                task.intended_delta,
                volatile_fields=lens.volatile_fields,
                field_codecs=lens.field_codecs,
            )
            outcome: TxOutcome = "CONFIRMED" if verdict.passed else "REFUTED"
            return TxCommitResult(
                task_id=task.task_id,
                outcome=outcome,
                occ_status=occ,
                entity_id=task.entity_id,
                wire_summary=wire_sum,
                judge_verdict=verdict,
                reasons=verdict.reasons,
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # Create path: O(1) indexed pre-duplicate check -> POST -> wire check -> O(1) R1 + dup check
        dup_query_path: Optional[str] = None
        if lens.duplicate_search is not None and lens.duplicate_keys:
            q_params: dict[str, str] = {}
            for qk, q_tmpl in lens.duplicate_search.query_params.items():
                field_key = q_tmpl.strip("{}")
                if field_key in task.intended_delta:
                    q_params[qk] = str(task.intended_delta[field_key])
            if q_params:
                dup_query_path = (
                    f"{lens.duplicate_search.path_template}?{urlencode(q_params)}"
                )
                st_dup0, _, dup0 = _http_json_request(
                    server.base_url, "GET", dup_query_path
                )
                if st_dup0 == 200:
                    rec_key = lens.duplicate_search.records_key or "items"
                    existing_matches = list(dup0.get(rec_key, []))
                    rows_read += len(existing_matches)
                    if existing_matches:
                        return TxCommitResult(
                            task_id=task.task_id,
                            outcome="BLOCKED_PRECOMMIT",
                            reasons=("duplicate_already_exists_precommit",),
                            rows_read=rows_read,
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )

        create_ep = lens.create_endpoint
        if create_ep is None:
            return TxCommitResult(
                task_id=task.task_id,
                outcome="BLOCKED_PRECOMMIT",
                reasons=("no_create_endpoint",),
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        c_status, _, c_body = _http_json_request(
            action_base,
            create_ep.method,
            create_ep.path_template,
            body=dict(task.intended_delta),
            headers={"X-Actor-Id": task.actor_id},
        )
        wire_sum = tap.summary()
        if not (200 <= c_status < 300):
            return TxCommitResult(
                task_id=task.task_id,
                outcome="REFUTED",
                wire_summary=wire_sum,
                reasons=(f"create_http_{c_status}",),
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        if wire_sum.collateral_mutations > 0 or wire_sum.mutations_allowed != 1:
            return TxCommitResult(
                task_id=task.task_id,
                outcome="REFUTED",
                wire_summary=wire_sum,
                reasons=("wire_duplicate_or_collateral_mutation",),
                rows_read=rows_read,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        created_id = str(c_body.get(lens.id_field, "")) if c_body else ""
        if dup_query_path is not None and lens.duplicate_search is not None:
            st_dup1, _, dup1 = _http_json_request(
                server.base_url, "GET", dup_query_path
            )
            if st_dup1 == 200:
                rec_key = lens.duplicate_search.records_key or "items"
                post_matches = list(dup1.get(rec_key, []))
                rows_read += len(post_matches)
                if len(post_matches) != 1:
                    return TxCommitResult(
                        task_id=task.task_id,
                        outcome="REFUTED",
                        entity_id=created_id or None,
                        wire_summary=wire_sum,
                        reasons=(f"post_create_match_count:{len(post_matches)}",),
                        rows_read=rows_read,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                r1_create = post_matches[0]
                created_id = str(r1_create.get(lens.id_field, created_id))
            else:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="UNCERTAIN",
                    wire_summary=wire_sum,
                    reasons=(f"dup_readback_http_{st_dup1}",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
        else:
            if not created_id:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="REFUTED",
                    wire_summary=wire_sum,
                    reasons=("missing_created_entity_id",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            get_created_path = _format_endpoint_path(
                lens.get_one.path_template, created_id
            )
            st_c1, _, r1_create = _http_json_request(
                server.base_url, "GET", get_created_path
            )
            if st_c1 != 200:
                return TxCommitResult(
                    task_id=task.task_id,
                    outcome="REFUTED",
                    entity_id=created_id,
                    wire_summary=wire_sum,
                    reasons=(f"created_r1_http_{st_c1}",),
                    rows_read=rows_read,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            rows_read += 1

        verdict = self._judge.evaluate(
            None,
            r1_create,
            task.intended_delta,
            volatile_fields=lens.volatile_fields,
            field_codecs=lens.field_codecs,
        )
        outcome_c: TxOutcome = "CONFIRMED" if verdict.passed else "REFUTED"
        return TxCommitResult(
            task_id=task.task_id,
            outcome=outcome_c,
            occ_status="NOT_APPLICABLE",
            entity_id=created_id or None,
            wire_summary=wire_sum,
            judge_verdict=verdict,
            reasons=verdict.reasons,
            rows_read=rows_read,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult:
        res = self.commit_tx(server, task, fault_proxy=fault_proxy)
        accepted = res.outcome == "CONFIRMED"
        return ArmExecutionResult(
            arm_name=self.name,
            task_id=task.task_id,
            accepted=accepted,
            outcome_label=res.outcome,
            rows_read=res.rows_read,
            duration_ms=res.duration_ms,
            reason=";".join(res.reasons),
            occ_status=res.occ_status,
        )
