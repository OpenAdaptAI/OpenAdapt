"""Bridge between Chrome MV3 extension_evidence.jsonl logs and openadapt-kernel.

Ingests passive human recordings emitted by the OpenAdapt Co-Pilot extension
(DOM input/click events, redacted network_save POST/PUT/PATCH/DELETE events,
and frame_focus transitions) to induce zero-PII PortalPacks and drive
Shadow -> Draft-to-Commit -> Verified Autopilot workflows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openadapt_kernel.copilot import PortalPack, ShadowObserver, ShadowTrace
from openadapt_kernel.interceptor import CommitInterceptor, NetworkEvent, is_search_or_filter_field


@dataclass
class ExtensionSessionSummary:
    portal_origin: str
    submitted_fields: Dict[str, str]
    mutating_requests: List[NetworkEvent]
    readback_url: str
    readback_text: str
    secret_fields_skipped: int = 0


def parse_extension_evidence_jsonl(path: Path) -> ExtensionSessionSummary:
    """Parse one extension_evidence.jsonl recording into fields and wire saves."""
    origin = ""
    submitted_fields: Dict[str, str] = {}
    mutating_requests: List[NetworkEvent] = []
    readback_url = ""
    readback_text = ""
    secret_skipped = 0

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ev: Dict[str, Any] = json.loads(line)
        kind = str(ev.get("kind") or ev.get("type") or "").lower()
        frame_origin = str(ev.get("frame_origin") or ev.get("origin") or "")
        if frame_origin and not origin:
            origin = frame_origin

        if kind == "network_save":
            method = str(ev.get("method") or "POST").upper()
            url = str(ev.get("url") or "")
            status = ev.get("status")
            mutating_requests.append(
                NetworkEvent(
                    method=method,
                    url=url,
                    status=int(status) if status is not None else None,
                    resource_type=str(ev.get("resource_type") or "fetch"),
                    post_data_preview=str(ev.get("body_preview") or "")[:200],
                )
            )
            if url:
                readback_url = url
        elif kind in ("input", "change", "field_edit", "select"):
            fname = str(ev.get("field") or ev.get("name") or ev.get("testid") or ev.get("target") or "").strip()
            val = str(ev.get("value") or "")
            if val == "<secret>":
                secret_skipped += 1
                continue
            if not fname:
                continue
            if is_search_or_filter_field(
                fname,
                input_type=str(ev.get("input_type") or ""),
                aria_role=str(ev.get("role") or ""),
                label=str(ev.get("label") or ""),
            ):
                continue
            submitted_fields[fname] = val
        elif kind in ("readback", "confirmation", "page_summary"):
            if ev.get("url"):
                readback_url = str(ev["url"])
            if ev.get("text"):
                readback_text = str(ev["text"])

    return ExtensionSessionSummary(
        portal_origin=origin or "unknown",
        submitted_fields=submitted_fields,
        mutating_requests=mutating_requests,
        readback_url=readback_url,
        readback_text=readback_text,
        secret_fields_skipped=secret_skipped,
    )


def induce_pack_from_extension_recordings(
    pack_id: str,
    sessions: Sequence[tuple[Path, Dict[str, str], Optional[str]]],
) -> PortalPack:
    """Induce a zero-PII PortalPack from (evidence_jsonl_path, source_dossier, ref) tuples."""
    traces: List[ShadowTrace] = []
    for idx, (jsonl_path, dossier, ref) in enumerate(sessions, start=1):
        summary = parse_extension_evidence_jsonl(jsonl_path)
        traces.append(
            ShadowTrace(
                trace_id=f"ext-{idx}",
                portal_origin=summary.portal_origin,
                source_dossier=dossier,
                submitted_fields=summary.submitted_fields,
                readback_url=summary.readback_url,
                readback_text=summary.readback_text,
                confirmation_ref=ref,
            )
        )
    return ShadowObserver.induce_portal_pack(pack_id, traces)


def replay_evidence_into_interceptor(
    summary: ExtensionSessionSummary,
    interceptor: CommitInterceptor,
) -> bool:
    """Feed parsed extension evidence into CommitInterceptor and return True if a commit occurred."""
    interceptor.begin_step()
    for fname, val in summary.submitted_fields.items():
        interceptor.mark_field_edited(fname, val)
    for req in summary.mutating_requests:
        interceptor.record_network_event(req)
    return interceptor.classify_step().is_commit
