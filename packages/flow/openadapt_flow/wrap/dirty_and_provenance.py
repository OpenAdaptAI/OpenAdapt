"""LTL_f No-Dirty-Exit Automaton and SSA Opaque-ID Provenance Conservation.

Two zero-model-cost deterministic safety monitors for ``openadapt wrap``:

1. :class:`DirtyBufferMonitor` (**LTL_f No-Dirty-Exit Automaton**):
   Tracks per-surface/tab dirty fields as the agent edits forms
   (``type``/``fill``/``select``/``attach`` marks dirty; ``submit``/``save``/
   ``discard``/``reset`` clears dirty). Mechanically blocks ``DONE`` exit
   whenever any surface still has unsubmitted dirty fields, returning the
   exact dirty surface and field names.

2. :class:`SsaIdentifierGuard` (**SSA Opaque-ID Provenance Conservation**):
   Records opaque structured identifiers (alphanumeric record/tracking/auth
   IDs, 10-digit numeric IDs, 5-digit procedure/code tokens, dotted
   classification codes like ``H35.32``, dates, phone/fax numbers) observed
   on source surfaces or in the trusted user prompt, tagged with their source
   surface and optional ``subject_key``. Before a destination submit, blocks:
   - **Unsourced hallucinated IDs**: opaque identifiers in the submitted
     payload that were never observed in any source surface or trusted prompt.
   - **Cross-subject tab bleed**: identifiers sourced from a different
     ``subject_key`` than ``active_subject_key``, or a single submission that
     mixes identifiers belonging to two distinct ``subject_key`` records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"

_DIRTY_EVENT_KINDS = frozenset({"type", "fill", "select", "attach", "note", "edit"})
_CLEAR_EVENT_KINDS = frozenset({"submit", "save", "discard", "reset", "cancel"})

_ISO_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
_PHONE_FAX_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_DOTTED_CODE_RE = re.compile(r"\b[A-Za-z]\d{2}(?:\.\d{1,4})?\b")
_ALNUM_OPAQUE_ID_RE = re.compile(
    r"\b(?:[A-Za-z]{2,8}[-_]?\d{3,12}(?:[-_][A-Za-z0-9]{2,12})?|[A-Za-z]\d{4})\b"
)
_LONG_NUMERIC_ID_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")


@dataclass(frozen=True)
class DirtyExitDecision:
    """Decision returned when the agent attempts to exit (``DONE``)."""

    decision: str  # DECISION_ALLOW | DECISION_BLOCK
    code: str
    detail: str
    dirty_by_surface: tuple[tuple[str, tuple[str, ...]], ...] = ()


class DirtyBufferMonitor:
    """LTL_f No-Dirty-Exit Automaton tracking unsubmitted form edits per surface."""

    def __init__(self) -> None:
        self._dirty: dict[str, set[str]] = {}

    def mark_dirty(self, surface: str, field_name: str) -> None:
        """Mark ``field_name`` on ``surface`` as having an unsubmitted edit."""
        surf = str(surface).strip() or "primary"
        fname = str(field_name).strip()
        if fname:
            self._dirty.setdefault(surf, set()).add(fname)

    def clear_surface(
        self, surface: str, *, fields: Iterable[str] | None = None
    ) -> None:
        """Clear all dirty fields (or a specific subset) on ``surface``."""
        surf = str(surface).strip() or "primary"
        if surf not in self._dirty:
            return
        if fields is None:
            self._dirty.pop(surf, None)
            return
        for fname in fields:
            self._dirty[surf].discard(str(fname).strip())
        if not self._dirty[surf]:
            self._dirty.pop(surf, None)

    def observe_event(self, event: Mapping[str, Any]) -> None:
        """Update dirty buffer state from a generic action event."""
        surface = str(event.get("surface", "primary"))
        kind = str(event.get("kind", "")).lower()
        if kind in _DIRTY_EVENT_KINDS:
            fname = event.get("field_name") or event.get("field_label")
            if fname:
                self.mark_dirty(surface, str(fname))
        elif (
            kind in _CLEAR_EVENT_KINDS
            or event.get("action_type") == "irreversible_commit"
        ):
            cleared = event.get("cleared_fields")
            if isinstance(cleared, (list, tuple, set)):
                self.clear_surface(surface, fields=[str(f) for f in cleared])
            else:
                self.clear_surface(surface)

    def dirty_snapshot(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return sorted ``((surface, (field1, field2, ...)), ...)`` of dirty buffers."""
        items: list[tuple[str, tuple[str, ...]]] = []
        for surf in sorted(self._dirty):
            fields = tuple(sorted(self._dirty[surf]))
            if fields:
                items.append((surf, fields))
        return tuple(items)

    def check_exit(self) -> DirtyExitDecision:
        """Mechanically block ``DONE`` while any surface has unsubmitted dirty fields."""
        snapshot = self.dirty_snapshot()
        if snapshot:
            summary = ", ".join(
                f"{surf}[{', '.join(fields)}]" for surf, fields in snapshot
            )
            return DirtyExitDecision(
                decision=DECISION_BLOCK,
                code="dirty_unsubmitted_fields",
                detail=f"Blocked DONE exit: unsubmitted dirty fields remain on {summary}.",
                dirty_by_surface=snapshot,
            )
        return DirtyExitDecision(
            decision=DECISION_ALLOW,
            code="all_buffers_clean",
            detail="All surface buffers are clean; exit is allowed.",
            dirty_by_surface=(),
        )


@dataclass(frozen=True)
class IdentifierProvenance:
    """Recorded origin of an opaque identifier observed in a trusted source."""

    canonical: str
    raw_value: str
    kind: str
    source_surface: str
    subject_key: str | None = None


@dataclass(frozen=True)
class ProvenanceViolation:
    """A single opaque-ID provenance or cross-subject bleed violation."""

    field_name: str
    raw_value: str
    canonical: str
    kind: str
    code: str  # unsourced_identifier | cross_subject_bleed
    detail: str


@dataclass(frozen=True)
class ProvenanceDecision:
    """Pre-submit SSA provenance decision."""

    decision: str  # DECISION_ALLOW | DECISION_BLOCK
    code: str
    detail: str
    violations: tuple[ProvenanceViolation, ...] = ()
    checked_tokens: int = 0


def _canonicalize_date(year: int, month: int, day: int) -> str | None:
    if 1 <= month <= 12 and 1 <= day <= 31:
        return f"DATE:{year:04d}-{month:02d}-{day:02d}"
    return None


def extract_opaque_identifiers(text: str) -> tuple[tuple[str, str, str], ...]:
    """Extract ``(canonical, raw_value, kind)`` opaque identifier tuples from text."""
    if not isinstance(text, str) or not text.strip():
        return ()

    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    consumed_spans: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        for s0, e0 in consumed_spans:
            if start < e0 and end > s0:
                return True
        return False

    def _add(canonical: str, raw: str, kind: str, start: int, end: int) -> None:
        consumed_spans.append((start, end))
        if canonical not in seen:
            seen.add(canonical)
            found.append((canonical, raw, kind))

    # 1. Dates (ISO and US formats canonicalized to DATE:YYYY-MM-DD)
    for match in _ISO_DATE_RE.finditer(text):
        canon = _canonicalize_date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
        if canon:
            _add(canon, match.group(0), "date", match.start(), match.end())

    for match in _US_DATE_RE.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        canon = _canonicalize_date(
            int(match.group(3)), int(match.group(1)), int(match.group(2))
        )
        if canon:
            _add(canon, match.group(0), "date", match.start(), match.end())

    # 2. Phone / Fax numbers (canonicalized to 10-digit PHONE:XXXXXXXXXX)
    for match in _PHONE_FAX_RE.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            _add(
                f"PHONE:{digits}",
                match.group(0),
                "phone_fax",
                match.start(),
                match.end(),
            )

    # 3. Dotted classification codes (e.g. H35.32, E11.9)
    for match in _DOTTED_CODE_RE.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        token = match.group(0).upper()
        if "." in token:
            _add(
                f"CODE:{token}",
                match.group(0),
                "classification_code",
                match.start(),
                match.end(),
            )

    # 4. Alphanumeric opaque IDs (e.g. MRN12345678, REF-2025-001, AET123456789, J2778, PA-2025-88421)
    for match in _ALNUM_OPAQUE_ID_RE.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        token = match.group(0).upper()
        _add(f"ID:{token}", match.group(0), "opaque_id", match.start(), match.end())

    # 5. Standalone 5-12 digit numeric IDs (e.g. 5-digit procedure codes, 10-digit NPIs)
    for match in _LONG_NUMERIC_ID_RE.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        digits = match.group(0)
        _add(f"NUM:{digits}", digits, "numeric_id", match.start(), match.end())

    return tuple(found)


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_string_values(item)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_string_values(item)


class SsaIdentifierGuard:
    """SSA Opaque-ID Provenance Conservation guard (0% model cost)."""

    def __init__(self) -> None:
        self._provenance: dict[str, list[IdentifierProvenance]] = {}

    def record_source_text(
        self,
        surface: str,
        text: str,
        *,
        subject_key: str | None = None,
    ) -> int:
        """Extract and register all opaque identifiers observed in ``text``."""
        extracted = extract_opaque_identifiers(text)
        for canonical, raw, kind in extracted:
            rec = IdentifierProvenance(
                canonical=canonical,
                raw_value=raw,
                kind=kind,
                source_surface=surface,
                subject_key=subject_key,
            )
            bucket = self._provenance.setdefault(canonical, [])
            if rec not in bucket:
                bucket.append(rec)
        return len(extracted)

    def record_trusted_prompt(
        self, text: str, *, subject_key: str | None = None
    ) -> int:
        """Register opaque identifiers supplied directly in the trusted user prompt."""
        return self.record_source_text("trusted_prompt", text, subject_key=subject_key)

    def record_source_entities(
        self,
        surface: str,
        entities: Mapping[str, Any],
        *,
        subject_key: str | None = None,
    ) -> int:
        """Register all opaque identifiers present in a structured entity dict."""
        total = 0
        for val in entities.values():
            for text in _iter_string_values(val):
                total += self.record_source_text(surface, text, subject_key=subject_key)
        return total

    def check_before_submit(
        self,
        dest_surface: str,
        payload: Mapping[str, Any],
        *,
        active_subject_key: str | None = None,
    ) -> ProvenanceDecision:
        """Verify every opaque identifier in ``payload`` has valid SSA provenance."""
        violations: list[ProvenanceViolation] = []
        checked_tokens = 0
        observed_subjects: set[str] = set()
        subject_sources: list[tuple[str, str, str, str, set[str]]] = []

        for field_name, val in payload.items():
            for text in _iter_string_values(val):
                for canonical, raw, kind in extract_opaque_identifiers(text):
                    checked_tokens += 1
                    records = self._provenance.get(canonical)
                    if not records:
                        violations.append(
                            ProvenanceViolation(
                                field_name=str(field_name),
                                raw_value=raw,
                                canonical=canonical,
                                kind=kind,
                                code="unsourced_identifier",
                                detail=(
                                    f"Identifier {raw!r} ({kind}) in {dest_surface}.{field_name} "
                                    f"has no provenance in any observed source surface or prompt."
                                ),
                            )
                        )
                        continue

                    record_subjects = {
                        r.subject_key for r in records if r.subject_key is not None
                    }
                    if active_subject_key is not None and record_subjects:
                        if active_subject_key not in record_subjects:
                            violations.append(
                                ProvenanceViolation(
                                    field_name=str(field_name),
                                    raw_value=raw,
                                    canonical=canonical,
                                    kind=kind,
                                    code="cross_subject_bleed",
                                    detail=(
                                        f"Identifier {raw!r} in {dest_surface}.{field_name} "
                                        f"belongs to subject(s) {sorted(record_subjects)}, "
                                        f"not active subject {active_subject_key!r}."
                                    ),
                                )
                            )
                            continue
                    if len(record_subjects) == 1:
                        only_subject = next(iter(record_subjects))
                        observed_subjects.add(only_subject)
                        subject_sources.append(
                            (str(field_name), raw, canonical, kind, record_subjects)
                        )

        # Even without active_subject_key, mixing identifiers uniquely bound to
        # two different subjects in one submission is cross-subject bleed.
        if active_subject_key is None and len(observed_subjects) > 1:
            sorted_subjects = sorted(observed_subjects)
            for field_name, raw, canonical, kind, rec_subjects in subject_sources:
                violations.append(
                    ProvenanceViolation(
                        field_name=field_name,
                        raw_value=raw,
                        canonical=canonical,
                        kind=kind,
                        code="cross_subject_bleed",
                        detail=(
                            f"Submission on {dest_surface} mixes identifiers from multiple "
                            f"subjects {sorted_subjects} (field {field_name}={raw!r} -> "
                            f"{sorted(rec_subjects)})."
                        ),
                    )
                )

        if violations:
            codes = {v.code for v in violations}
            top_code = (
                "cross_subject_bleed"
                if "cross_subject_bleed" in codes
                else "unsourced_identifier"
            )
            return ProvenanceDecision(
                decision=DECISION_BLOCK,
                code=top_code,
                detail=f"Blocked submit on {dest_surface}: {len(violations)} provenance violation(s).",
                violations=tuple(violations),
                checked_tokens=checked_tokens,
            )

        return ProvenanceDecision(
            decision=DECISION_ALLOW,
            code="provenance_conserved",
            detail=f"All {checked_tokens} opaque identifier(s) conserved from trusted sources.",
            violations=(),
            checked_tokens=checked_tokens,
        )


__all__ = [
    "DECISION_ALLOW",
    "DECISION_BLOCK",
    "DirtyBufferMonitor",
    "DirtyExitDecision",
    "IdentifierProvenance",
    "ProvenanceDecision",
    "ProvenanceViolation",
    "SsaIdentifierGuard",
    "extract_opaque_identifiers",
]
