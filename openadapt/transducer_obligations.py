"""Spec-to-contract obligations and structured human handoff.

Obligations are extracted from a task prompt or structured intake fields with
no per-app code. A run may only be called verified when every required
obligation appears in a non-editable, non-toast read-back.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = [
    "CompiledFieldObligation",
    "MicroHandoffResolution",
    "MicroHandoffTicket",
    "ObligationDiffResult",
    "ObligationStatus",
    "apply_human_resolution",
    "build_hold_done_prompt",
    "build_micro_handoff",
    "compile_obligations_from_spec",
    "diff_readback_against_obligations",
    "evaluate_readback_with_completeness",
    "is_multi_record_list_view",
    "mark_drafted_from_action",
    "merge_obligations_from_document",
]


class ObligationStatus(enum.Enum):
    PENDING = "pending"
    DRAFTED = "drafted"
    PENDING_EFFECT = "pending_effect"
    VERIFIED = "verified"
    MISSING = "missing"


@dataclass
class CompiledFieldObligation:
    field_id: str
    kind: str
    label: str
    expected_value: str
    exact_match: bool = True
    required: bool = True
    status: ObligationStatus = ObligationStatus.PENDING
    observed_value: str = ""


@dataclass
class ObligationDiffResult:
    all_verified: bool
    verified_ids: list[str]
    missing_obligations: list[CompiledFieldObligation]
    coverage_ratio: float
    reason: str


_ID_RE = re.compile(
    r"\b(?:REF|CLM|PA|ORD|MRN|POL|DEN|FAX|AUTH)-[A-Za-z0-9][A-Za-z0-9-]*\b"
)
_STRUCTURED_ID_RE = re.compile(
    r"(?<![\w-])([A-Za-z][A-Za-z0-9]*)-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![\w-])"
)
_NPI_RE = re.compile(r"\bNPI\s*[:#]?\s*(\d{10})\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_US_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
_AMOUNT_RE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
_QUOTED_RE = re.compile(r'"([^"\n]+)"')
_CPT_CTX_RE = re.compile(r"(?:CPT|procedure|HCPCS)\D{0,25}?\b(\d{5})\b", re.IGNORECASE)
_HCPCS_RE = re.compile(r"\b[A-V]\d{4}\b")
_ICD_RE = re.compile(r"\b[A-TV-Z]\d{2}(?:\.[0-9A-Za-z]{1,4})?\b")

# Free-text kinds compare after whitespace/case normalisation.
_LOOSE_KINDS = frozenset({"date", "phrase", "field"})


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _is_loose(ob: CompiledFieldObligation) -> bool:
    return (not ob.exact_match) or ob.kind in _LOOSE_KINDS


def _value_in_text(ob: CompiledFieldObligation, text: str) -> bool:
    if not ob.expected_value:
        return False
    if _is_loose(ob):
        return _norm(ob.expected_value) in _norm(text)
    return ob.expected_value in text


def compile_obligations_from_spec(
    task_instruction: str,
    structured_fields: Mapping[str, Any] | None = None,
) -> list[CompiledFieldObligation]:
    """Extract typed, verifiable obligations from a spec or prompt."""
    out: list[CompiledFieldObligation] = []
    seen: set[tuple[str, str]] = set()

    def add(
        kind: str, label: str, value: str, *, exact: bool, field_id: str = ""
    ) -> None:
        value = str(value).strip()
        if not value or (kind, value) in seen:
            return
        seen.add((kind, value))
        out.append(
            CompiledFieldObligation(
                field_id=field_id or f"{kind}:{value}",
                kind=kind,
                label=label,
                expected_value=value,
                exact_match=exact,
            )
        )

    for key, val in (structured_fields or {}).items():
        if val is None or isinstance(val, (Mapping, list, tuple, set)):
            continue
        sval = str(val).strip()
        if not sval or isinstance(val, bool):
            continue
        has_code = bool(
            _ID_RE.fullmatch(sval)
            or _HCPCS_RE.fullmatch(sval)
            or _ICD_RE.fullmatch(sval)
        )
        add(
            "field",
            str(key),
            sval,
            exact=has_code or sval.isdigit(),
            field_id=f"field:{key}",
        )

    text = task_instruction or ""
    consumed: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(span[0] < e and s < span[1] for s, e in consumed)

    for m in _QUOTED_RE.finditer(text):
        consumed.append(m.span())
        add("phrase", "required phrase", m.group(1), exact=False)
    for m in _ID_RE.finditer(text):
        if free(m.span()):
            consumed.append(m.span())
            add("identifier", m.group(0).split("-")[0], m.group(0), exact=True)
    for m in _NPI_RE.finditer(text):
        if free(m.span(1)):
            consumed.append(m.span(1))
            add("identifier", "NPI", m.group(1), exact=True)
    for rx in (_ISO_DATE_RE, _US_DATE_RE):
        for m in rx.finditer(text):
            if free(m.span()):
                consumed.append(m.span())
                add("date", "date", m.group(0), exact=False)
    for m in _AMOUNT_RE.finditer(text):
        if free(m.span()):
            consumed.append(m.span())
            add("amount", "amount", m.group(0), exact=True)
    for m in _CPT_CTX_RE.finditer(text):
        if free(m.span(1)):
            consumed.append(m.span(1))
            add("cpt", "CPT code", m.group(1), exact=True)
    for m in _HCPCS_RE.finditer(text):
        if free(m.span()):
            consumed.append(m.span())
            add("hcpcs", "HCPCS code", m.group(0), exact=True)
    for m in _ICD_RE.finditer(text):
        if free(m.span()):
            consumed.append(m.span())
            add("icd10", "ICD-10 code", m.group(0), exact=True)
    return out


def _ob_key(ob: CompiledFieldObligation) -> tuple[str, str]:
    return (ob.kind, _norm(ob.expected_value))


def _structured_ids(text: str) -> Iterator[tuple[str, str]]:
    for match in _STRUCTURED_ID_RE.finditer(text):
        prefix, suffix = match.groups()
        if prefix.isupper() or any(char.isdigit() for char in suffix):
            yield prefix.upper(), match[0]


def is_multi_record_list_view(text: str) -> bool:
    """Identify worklists by four distinct IDs sharing one structured prefix."""
    ids_by_prefix: dict[str, set[str]] = {}
    for prefix, value in _structured_ids(text):
        ids = ids_by_prefix.setdefault(prefix, set())
        ids.add(value.lower())
        if len(ids) >= 4:
            return True
    return False


def merge_obligations_from_document(
    existing: list[CompiledFieldObligation],
    document_text: str,
    *,
    source_label: str = "document",
    structured_fields: Mapping[str, Any] | None = None,
    task_instruction: str = "",
) -> list[CompiledFieldObligation]:
    """Add obligations visible in a mid-episode document to ``existing``.

    Existing obligations keep their status; duplicates (same kind and
    case-insensitive value) are not added. Returns a new list.
    On worklists, only values already in scope can become obligations.
    """
    merged = list(existing)
    seen = {_ob_key(o) for o in merged}
    candidates = compile_obligations_from_spec(document_text, structured_fields)
    if is_multi_record_list_view(document_text):
        existing_values = {value for _, value in seen}
        instruction = _norm(task_instruction)

        def in_scope(value: str) -> bool:
            value = _norm(value)
            return bool(value) and (
                value in existing_values
                or re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", instruction)
                is not None
            )

        candidates = [ob for ob in candidates if in_scope(ob.expected_value)]
        candidates.extend(
            CompiledFieldObligation(f"identifier:{value}", "identifier", prefix, value)
            for prefix, value in _structured_ids(document_text)
            if in_scope(value)
        )
    for ob in candidates:
        key = _ob_key(ob)
        if key in seen:
            continue
        seen.add(key)
        if source_label and source_label not in ob.label:
            ob.label = f"{ob.label} ({source_label})"
        merged.append(ob)
    return merged


def mark_drafted_from_action(
    obligations: Sequence[CompiledFieldObligation], action_text_or_value: str
) -> list[str]:
    """Mark pending obligations present in an input action as DRAFTED."""
    marked: list[str] = []
    for ob in obligations:
        if ob.status in (
            ObligationStatus.PENDING,
            ObligationStatus.MISSING,
        ) and _value_in_text(ob, action_text_or_value or ""):
            ob.status = ObligationStatus.DRAFTED
            marked.append(ob.field_id)
    return marked


def build_hold_done_prompt(missing: Sequence[CompiledFieldObligation]) -> str:
    lines = [
        "HOLD_DONE: do not finish yet. These required values are not visible in the saved result:"
    ]
    lines += [f"- {o.label}: {o.expected_value}" for o in missing]
    lines.append(
        "Enter or correct exactly these values, save, then re-check the saved view."
    )
    return "\n".join(lines)


def diff_readback_against_obligations(
    obligations: Sequence[CompiledFieldObligation],
    readback_text: str,
    *,
    is_editable: bool = False,
    is_toast: bool = False,
) -> ObligationDiffResult:
    """Compare a read-back to obligations; verified only at 100% coverage."""
    required = [o for o in obligations if o.required]
    if is_toast or is_editable:
        what = "a toast" if is_toast else "an editable field"
        return ObligationDiffResult(
            all_verified=False,
            verified_ids=[],
            missing_obligations=list(required),
            coverage_ratio=0.0,
            reason=f"read-back is {what}, not committed state; {build_hold_done_prompt(required)}",
        )
    verified: list[str] = []
    missing: list[CompiledFieldObligation] = []
    for ob in required:
        if _value_in_text(ob, readback_text or ""):
            ob.status = ObligationStatus.VERIFIED
            ob.observed_value = ob.expected_value
            verified.append(ob.field_id)
        else:
            if ob.status is ObligationStatus.VERIFIED:
                ob.observed_value = ""
            ob.status = (
                ObligationStatus.PENDING_EFFECT
                if ob.status
                in (ObligationStatus.DRAFTED, ObligationStatus.PENDING_EFFECT)
                else ObligationStatus.MISSING
            )
            missing.append(ob)
    ratio = 1.0 if not required else len(verified) / len(required)
    ok = not missing
    return ObligationDiffResult(
        all_verified=ok,
        verified_ids=verified,
        missing_obligations=missing,
        coverage_ratio=ratio,
        reason="all required obligations verified"
        if ok
        else build_hold_done_prompt(missing),
    )


@dataclass
class MicroHandoffTicket:
    ticket_id: str
    run_id: str
    task_id: str
    reason: str
    blocker_kind: str
    current_url: str
    missing_obligations: list[str]
    stalled_selectors: list[str]
    suggested_human_action: str
    created_at: str


@dataclass
class MicroHandoffResolution:
    ticket_id: str
    resolved: bool
    human_action: dict[str, Any] | None = None
    learned_selector: str = ""
    notes: str = ""


def build_micro_handoff(
    *,
    run_id: str,
    task_id: str,
    reason: str,
    blocker_kind: str,
    current_url: str = "",
    missing_obligations: Sequence[CompiledFieldObligation | str] = (),
    stalled_selectors: Sequence[str] = (),
    suggested_human_action: str = "",
) -> MicroHandoffTicket:
    """Create a narrow ticket naming exactly what the human must do."""
    missing = [
        o if isinstance(o, str) else f"{o.label}: {o.expected_value}"
        for o in missing_obligations
    ]
    action = suggested_human_action or (
        "Complete these values in the page, then mark resolved: " + "; ".join(missing)
        if missing
        else "Clear the blocker in the page, then mark resolved."
    )
    return MicroHandoffTicket(
        ticket_id=f"handoff-{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        task_id=task_id,
        reason=reason,
        blocker_kind=blocker_kind,
        current_url=current_url,
        missing_obligations=missing,
        stalled_selectors=list(stalled_selectors),
        suggested_human_action=action,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def apply_human_resolution(
    ticket: MicroHandoffTicket, resolution: MicroHandoffResolution
) -> dict[str, Any]:
    """Turn a human fix into an app-cartridge hint patch for the next run."""
    if resolution.ticket_id != ticket.ticket_id:
        raise ValueError("resolution does not match ticket")
    patch: dict[str, Any] = {
        "ticket_id": ticket.ticket_id,
        "task_id": ticket.task_id,
        "resolved": resolution.resolved,
        "applied": False,
        "selector_hints": [],
        "blocker_kind": ticket.blocker_kind,
        "url": ticket.current_url,
        "avoid_selectors": [],
        "notes": resolution.notes,
    }
    if not resolution.resolved:
        return patch
    if resolution.learned_selector:
        patch["selector_hints"].append(
            {
                "selector": resolution.learned_selector,
                "blocker_kind": ticket.blocker_kind,
                "obligations": list(ticket.missing_obligations),
            }
        )
        patch["avoid_selectors"] = [
            s for s in ticket.stalled_selectors if s != resolution.learned_selector
        ]
    if resolution.human_action:
        patch["human_action"] = dict(resolution.human_action)
    patch["applied"] = bool(patch["selector_hints"] or resolution.human_action)
    return patch


def evaluate_readback_with_completeness(
    obligations: Sequence[CompiledFieldObligation],
    readback_text: str,
    *,
    intake_context: str = "",
    is_editable: bool = False,
    is_toast: bool = False,
    completeness_auditor: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> ObligationDiffResult:
    """Deterministic read-back diff plus an optional independent completeness audit."""
    base = diff_readback_against_obligations(
        obligations, readback_text, is_editable=is_editable, is_toast=is_toast
    )
    if is_toast or is_editable:
        return base
    if completeness_auditor is None or not intake_context:
        return base
    try:
        verdict = completeness_auditor(intake_context, readback_text)
        complete = bool(verdict.get("complete", False))
        items = [
            str(m).strip() for m in (verdict.get("missing") or []) if str(m).strip()
        ]
        reason = str(verdict.get("reason", ""))
    except Exception as exc:  # refuse when the audit cannot be trusted
        return ObligationDiffResult(
            all_verified=False,
            verified_ids=base.verified_ids,
            missing_obligations=base.missing_obligations,
            coverage_ratio=base.coverage_ratio,
            reason=f"completeness audit failed ({type(exc).__name__}); cannot verify",
        )
    if complete and not items:
        return base
    narrative = [
        CompiledFieldObligation(
            field_id=f"narrative_criterion:{i}:{item}",
            kind="narrative_criterion",
            label="clinical_completeness",
            expected_value=item,
            exact_match=False,
            required=True,
            status=ObligationStatus.MISSING,
        )
        for i, item in enumerate(items)
    ]
    missing = list(base.missing_obligations) + narrative
    if not missing:
        return ObligationDiffResult(
            all_verified=False,
            verified_ids=base.verified_ids,
            missing_obligations=[],
            coverage_ratio=base.coverage_ratio,
            reason=f"completeness audit says incomplete: {reason or 'no detail'}",
        )
    total = len(base.verified_ids) + len(missing)
    return ObligationDiffResult(
        all_verified=False,
        verified_ids=base.verified_ids,
        missing_obligations=missing,
        coverage_ratio=len(base.verified_ids) / total if total else 0.0,
        reason=build_hold_done_prompt(missing),
    )
