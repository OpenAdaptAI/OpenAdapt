"""Compile a multi-surface demonstration trace into a layout-independent CheckpointSpec.

A recorded demonstration trace contains raw UI coordinates (``x``, ``y``,
``viewport``, ``field_rect``), observed entities read from source surfaces,
staged writes on destination surfaces, and irreversible commit requests with
their responses.

:func:`compile_checkpoint_spec` strips all pixel coordinates, viewports, and
DOM selectors and emits an ``openadapt.checkpoint-spec/v1`` specification with
four semantic checkpoint classes:

1. ``required_source_reads``: source sections and fields that must be observed
   before a downstream commit.
2. ``cross_tab_invariants``: entity values read on one surface that must match
   values submitted on another surface (using format-aware normalization).
3. ``approval_rules``: pre-commit policy thresholds and stop-on-uncertainty
   gates attached to irreversible commits.
4. ``confirmation_checks``: post-commit confirmation status/field checks and
   optional cross-surface confirmation writeback checks.

Zero per-application hardcoding: all rules are inferred generically from
cross-surface dataflow and explicit operator policy declarations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

SPEC_SCHEMA = "openadapt.checkpoint-spec/v1"

#: Layout keys that must never appear in a compiled CheckpointSpec.
FORBIDDEN_LAYOUT_KEYS: frozenset[str] = frozenset(
    {"x", "y", "end_x", "end_y", "viewport", "field_rect", "selector", "xpath", "bbox"}
)

_ISO_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_US_DATE_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")


def normalize_date(value: Any) -> str | None:
    """Normalize ``YYYY-MM-DD``, ``YYYY/MM/DD``, or ``MM/DD/YYYY`` into ``YYYY-MM-DD``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _ISO_DATE_RE.match(text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    match = _US_DATE_RE.match(text)
    if match:
        month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def normalize_scalar(value: Any) -> str:
    """Case-insensitive, whitespace-collapsed scalar normalization."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).upper()


def normalize_token_set(value: Any) -> frozenset[str]:
    """Normalize a list, set, or delimited string of tokens into an uppercase set."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(
            normalize_scalar(item) for item in value if normalize_scalar(item)
        )
    if isinstance(value, str) and value.strip():
        return frozenset(
            normalize_scalar(part)
            for part in re.split(r"[,;\s]+", value.strip())
            if normalize_scalar(part)
        )
    return frozenset()


def values_match(source_val: Any, dest_val: Any, match_mode: str) -> bool:
    """Return True when ``dest_val`` satisfies ``match_mode`` relative to ``source_val``."""
    if match_mode == "exact_ci":
        return bool(normalize_scalar(source_val)) and (
            normalize_scalar(source_val) == normalize_scalar(dest_val)
        )
    if match_mode == "date_iso":
        src_date = normalize_date(source_val)
        dst_date = normalize_date(dest_val)
        return src_date is not None and dst_date is not None and src_date == dst_date
    if match_mode == "set_exact":
        src_set = normalize_token_set(source_val)
        dst_set = normalize_token_set(dest_val)
        return bool(src_set) and src_set == dst_set
    if match_mode == "subset_ci":
        src_set = normalize_token_set(source_val)
        dst_set = normalize_token_set(dest_val)
        return bool(dst_set) and dst_set.issubset(src_set)
    if match_mode == "contains_ci":
        src_s = normalize_scalar(source_val)
        dst_s = normalize_scalar(dest_val)
        return bool(src_s) and src_s in dst_s
    return False


def _detect_match_mode(source_val: Any, dest_val: Any) -> str | None:
    """Infer whether ``dest_val`` came from ``source_val`` and select a match mode."""
    if source_val is None or dest_val is None:
        return None

    if isinstance(source_val, (list, tuple, set)) and isinstance(
        dest_val, (list, tuple, set)
    ):
        src_set = normalize_token_set(source_val)
        dst_set = normalize_token_set(dest_val)
        if src_set and dst_set and dst_set.issubset(src_set):
            return "set_exact" if dst_set == src_set else "subset_ci"
        return None

    src_date = normalize_date(source_val)
    dst_date = normalize_date(dest_val)
    if src_date is not None and dst_date is not None and src_date == dst_date:
        return "date_iso"

    src_s = normalize_scalar(source_val)
    dst_s = normalize_scalar(dest_val)
    if not src_s or not dst_s or len(src_s) < 3:
        return None
    if src_s == dst_s:
        return "exact_ci"
    if len(src_s) >= 6 and src_s in dst_s:
        return "contains_ci"
    return None


@dataclass(frozen=True)
class SourceReadCheckpoint:
    """A source-surface read that must occur before a downstream commit."""

    id: str
    surface: str
    section: str
    required_fields: tuple[str, ...]
    before_commit: str


@dataclass(frozen=True)
class CrossTabInvariant:
    """An anchor entity that must match between a source surface and a commit surface."""

    id: str
    source_surface: str
    source_field: str
    dest_surface: str
    dest_field: str
    match_mode: str
    enforced_on_commit: str


@dataclass(frozen=True)
class ApprovalRule:
    """Pre-commit policy threshold or approval gate on an irreversible commit."""

    id: str
    surface: str
    commit_action: str
    field_name: str
    operator: str  # lte | gte | in
    threshold: float | tuple[str, ...]
    on_violation: str = "escalate"  # escalate | block
    description: str = ""


@dataclass(frozen=True)
class ConfirmationCheck:
    """Post-commit confirmation and optional cross-surface writeback check."""

    id: str
    surface: str
    commit_action: str
    expected_status_codes: tuple[int, ...]
    required_response_fields: tuple[str, ...]
    writeback_surface: str | None = None
    writeback_field: str | None = None
    writeback_before_commit: str | None = None


@dataclass(frozen=True)
class CheckpointSpec:
    """Compiled, UI-layout-independent checkpoint specification."""

    schema: str
    workflow_id: str
    source_trace_id: str
    surfaces: tuple[str, ...]
    commit_sequence: tuple[str, ...]
    required_source_reads: tuple[SourceReadCheckpoint, ...]
    cross_tab_invariants: tuple[CrossTabInvariant, ...]
    approval_rules: tuple[ApprovalRule, ...]
    confirmation_checks: tuple[ConfirmationCheck, ...]
    stop_on_uncertainty: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Serialize to formatted JSON."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckpointSpec:
        """Deserialize and validate layout-independence."""
        assert_layout_independent(data)
        rules: list[ApprovalRule] = []
        for raw_rule in data["approval_rules"]:
            raw_threshold = raw_rule["threshold"]
            threshold: float | tuple[str, ...] = (
                tuple(str(item) for item in raw_threshold)
                if isinstance(raw_threshold, (list, tuple))
                else float(raw_threshold)
            )
            rules.append(
                ApprovalRule(
                    id=str(raw_rule["id"]),
                    surface=str(raw_rule["surface"]),
                    commit_action=str(raw_rule["commit_action"]),
                    field_name=str(raw_rule["field_name"]),
                    operator=str(raw_rule["operator"]),
                    threshold=threshold,
                    on_violation=str(raw_rule.get("on_violation", "escalate")),
                    description=str(raw_rule.get("description", "")),
                )
            )
        return cls(
            schema=str(data["schema"]),
            workflow_id=str(data["workflow_id"]),
            source_trace_id=str(data["source_trace_id"]),
            surfaces=tuple(str(s) for s in data["surfaces"]),
            commit_sequence=tuple(str(c) for c in data["commit_sequence"]),
            required_source_reads=tuple(
                SourceReadCheckpoint(
                    id=str(r["id"]),
                    surface=str(r["surface"]),
                    section=str(r["section"]),
                    required_fields=tuple(str(f) for f in r["required_fields"]),
                    before_commit=str(r["before_commit"]),
                )
                for r in data["required_source_reads"]
            ),
            cross_tab_invariants=tuple(
                CrossTabInvariant(
                    id=str(inv["id"]),
                    source_surface=str(inv["source_surface"]),
                    source_field=str(inv["source_field"]),
                    dest_surface=str(inv["dest_surface"]),
                    dest_field=str(inv["dest_field"]),
                    match_mode=str(inv["match_mode"]),
                    enforced_on_commit=str(inv["enforced_on_commit"]),
                )
                for inv in data["cross_tab_invariants"]
            ),
            approval_rules=tuple(rules),
            confirmation_checks=tuple(
                ConfirmationCheck(
                    id=str(c["id"]),
                    surface=str(c["surface"]),
                    commit_action=str(c["commit_action"]),
                    expected_status_codes=tuple(
                        int(code) for code in c["expected_status_codes"]
                    ),
                    required_response_fields=tuple(
                        str(f) for f in c["required_response_fields"]
                    ),
                    writeback_surface=c.get("writeback_surface"),
                    writeback_field=c.get("writeback_field"),
                    writeback_before_commit=c.get("writeback_before_commit"),
                )
                for c in data["confirmation_checks"]
            ),
            stop_on_uncertainty=bool(data.get("stop_on_uncertainty", True)),
        )


def assert_layout_independent(obj: Any, path: str = "$") -> None:
    """Raise ValueError if any pixel coordinate, viewport, or selector key appears."""
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if key in FORBIDDEN_LAYOUT_KEYS:
                raise ValueError(f"Layout-dependent key {key!r} leaked at {path}.{key}")
            assert_layout_independent(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            assert_layout_independent(item, f"{path}[{idx}]")


def compile_checkpoint_spec(
    trace: Mapping[str, Any],
    policy_rules: Sequence[Mapping[str, Any]] | None = None,
) -> CheckpointSpec:
    """Compile a recorded multi-surface demonstration trace into a CheckpointSpec.

    Parameters
    ----------
    trace:
        Mapping with ``meta`` (``id``, ``workflow_id``) and ``events`` (ordered
        list of recorded multi-surface events). Raw UI coordinates are ignored.
    policy_rules:
        Optional operator policy rules (thresholds or allowed value sets)
        attached to matching irreversible commits.
    """
    meta = trace.get("meta") or {}
    events = list(trace.get("events") or [])
    workflow_id = str(meta.get("workflow_id", meta.get("id", "unknown-workflow")))
    source_trace_id = str(meta.get("id", "demo-trace"))

    surfaces_seen: list[str] = []
    reads_by_key: dict[tuple[str, str], tuple[str, Any]] = {}
    section_fields: dict[tuple[str, str], list[str]] = {}
    staged_writes: dict[str, dict[str, Any]] = {}
    commit_outputs: dict[tuple[str, str], tuple[str, Any]] = {}

    commit_sequence: list[str] = []
    source_read_checkpoints: list[SourceReadCheckpoint] = []
    invariants: list[CrossTabInvariant] = []
    confirmation_checks: list[ConfirmationCheck] = []
    seen_invariants: set[tuple[str, str, str, str, str]] = set()
    seen_source_sections: set[tuple[str, str, str]] = set()
    pending_confirmations: dict[str, dict[str, Any]] = {}

    for ev in events:
        surface = str(ev.get("surface", "primary"))
        if surface not in surfaces_seen:
            surfaces_seen.append(surface)

        kind = str(ev.get("kind", ""))
        section = str(ev.get("section", "default"))

        read_entities = ev.get("read_entities") or {}
        if isinstance(read_entities, Mapping) and read_entities:
            sec_key = (surface, section)
            section_fields.setdefault(sec_key, [])
            for fname, fval in read_entities.items():
                reads_by_key[(surface, str(fname))] = (section, fval)
                if str(fname) not in section_fields[sec_key]:
                    section_fields[sec_key].append(str(fname))

        if kind in ("type", "attach", "note", "select"):
            fname = ev.get("field_name") or ev.get("field_label")
            fval = ev.get("value") if "value" in ev else ev.get("text")
            if fname is not None and fval is not None:
                staged_writes.setdefault(surface, {})[str(fname)] = fval

                for (c_surf, _c_field), (c_action, c_val) in commit_outputs.items():
                    if c_surf == surface:
                        continue
                    mode = _detect_match_mode(c_val, fval)
                    if mode is not None and c_action in pending_confirmations:
                        pending_confirmations[c_action]["writeback_surface"] = surface
                        pending_confirmations[c_action]["writeback_field"] = str(fname)

        if kind == "submit" or ev.get("action_type") == "irreversible_commit":
            commit_action = str(ev.get("commit_action", f"{surface}:submit"))
            commit_sequence.append(commit_action)

            payload: dict[str, Any] = dict(staged_writes.get(surface, {}))
            if isinstance(ev.get("payload"), Mapping):
                payload.update(ev["payload"])

            matched_source_surfaces: set[str] = set()
            for dst_field, dst_val in payload.items():
                for (src_surf, src_field), (
                    _src_section,
                    src_val,
                ) in reads_by_key.items():
                    if src_surf == surface:
                        continue
                    mode = _detect_match_mode(src_val, dst_val)
                    if mode is None:
                        continue
                    inv_key = (src_surf, src_field, surface, dst_field, commit_action)
                    if inv_key not in seen_invariants:
                        seen_invariants.add(inv_key)
                        invariants.append(
                            CrossTabInvariant(
                                id=f"inv_{len(invariants) + 1:02d}",
                                source_surface=src_surf,
                                source_field=src_field,
                                dest_surface=surface,
                                dest_field=dst_field,
                                match_mode=mode,
                                enforced_on_commit=commit_action,
                            )
                        )
                    matched_source_surfaces.add(src_surf)

            if matched_source_surfaces:
                for (src_surf, src_sec), fields_in_sec in section_fields.items():
                    if src_surf not in matched_source_surfaces:
                        continue
                    req_key = (src_surf, src_sec, commit_action)
                    if req_key not in seen_source_sections:
                        seen_source_sections.add(req_key)
                        source_read_checkpoints.append(
                            SourceReadCheckpoint(
                                id=f"read_{len(source_read_checkpoints) + 1:02d}",
                                surface=src_surf,
                                section=src_sec,
                                required_fields=tuple(fields_in_sec),
                                before_commit=commit_action,
                            )
                        )

            for conf_meta in pending_confirmations.values():
                if (
                    conf_meta.get("writeback_surface") == surface
                    and conf_meta.get("writeback_before_commit") is None
                ):
                    conf_meta["writeback_before_commit"] = commit_action

            response = ev.get("response") or {}
            if isinstance(response, Mapping) and response:
                status = int(response.get("status", 200))
                resp_fields = tuple(
                    str(k)
                    for k, v in response.items()
                    if k != "status" and v not in (None, "", False)
                )
                pending_confirmations[commit_action] = {
                    "surface": surface,
                    "commit_action": commit_action,
                    "expected_status_codes": (status,)
                    if 200 <= status < 300
                    else (200, 201),
                    "required_response_fields": resp_fields,
                    "writeback_surface": None,
                    "writeback_field": None,
                    "writeback_before_commit": None,
                }
                for rf in resp_fields:
                    commit_outputs[(surface, rf)] = (commit_action, response[rf])

    for idx, c_action in enumerate(commit_sequence, start=1):
        if c_action in pending_confirmations:
            meta_c = pending_confirmations[c_action]
            confirmation_checks.append(
                ConfirmationCheck(
                    id=f"confirm_{idx:02d}",
                    surface=meta_c["surface"],
                    commit_action=meta_c["commit_action"],
                    expected_status_codes=tuple(meta_c["expected_status_codes"]),
                    required_response_fields=tuple(meta_c["required_response_fields"]),
                    writeback_surface=meta_c["writeback_surface"],
                    writeback_field=meta_c["writeback_field"],
                    writeback_before_commit=meta_c["writeback_before_commit"],
                )
            )

    approval_rules: list[ApprovalRule] = []
    for idx, rule in enumerate(policy_rules or (), start=1):
        raw_threshold = rule["threshold"]
        threshold: float | tuple[str, ...] = (
            tuple(str(item) for item in raw_threshold)
            if isinstance(raw_threshold, (list, tuple))
            else float(raw_threshold)
        )
        approval_rules.append(
            ApprovalRule(
                id=str(rule.get("id", f"policy_{idx:02d}")),
                surface=str(rule["surface"]),
                commit_action=str(rule["commit_action"]),
                field_name=str(rule["field_name"]),
                operator=str(rule["operator"]),
                threshold=threshold,
                on_violation=str(rule.get("on_violation", "escalate")),
                description=str(rule.get("description", "")),
            )
        )

    spec = CheckpointSpec(
        schema=SPEC_SCHEMA,
        workflow_id=workflow_id,
        source_trace_id=source_trace_id,
        surfaces=tuple(surfaces_seen),
        commit_sequence=tuple(commit_sequence),
        required_source_reads=tuple(source_read_checkpoints),
        cross_tab_invariants=tuple(invariants),
        approval_rules=tuple(approval_rules),
        confirmation_checks=tuple(confirmation_checks),
        stop_on_uncertainty=True,
    )
    assert_layout_independent(spec.to_dict())
    return spec


__all__ = [
    "ApprovalRule",
    "CheckpointSpec",
    "ConfirmationCheck",
    "CrossTabInvariant",
    "FORBIDDEN_LAYOUT_KEYS",
    "SPEC_SCHEMA",
    "SourceReadCheckpoint",
    "assert_layout_independent",
    "compile_checkpoint_spec",
    "normalize_date",
    "normalize_scalar",
    "normalize_token_set",
    "values_match",
]
