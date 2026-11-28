"""Atomic form batching, data-only SOP contracts, and human-touch compounding.

* ``FormSubmitPlan`` checks every field of a form before any fill or click is
  sent, then expands to fill steps plus one commit click.
* ``DeclarativeSopContract`` holds SOP rules as plain data. One engine checks a
  non-editable read-back against them; no rule names an application.
* ``MicroHandoffCompoundingController`` stores a human's fix as a macro so the
  next run needs no human.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openadapt.transducer_macros import (
    ISOLATION_PERSISTENT,
    CompiledCommitMacro,
    CompiledMacroStep,
    MacroStore,
)
from openadapt.transducer_obligations import (
    MicroHandoffResolution,
    MicroHandoffTicket,
    apply_human_resolution,
    build_micro_handoff,
)

__all__ = [
    "ControllerOutcome",
    "DeclarativeSopContract",
    "FormFieldWrite",
    "FormPlanValidationResult",
    "FormSubmitPlan",
    "MicroHandoffCompoundingController",
    "SopEvaluationResult",
    "SopRule",
    "evaluate_readback_against_sop",
    "expand_form_plan_to_steps",
    "validate_form_plan",
]

_CODE_KEY_RE = re.compile(
    r"(?:^|[^a-z0-9])(mrn|cpt|hcpcs|icd[-_ ]?\d*|ref|policy[-_ ]?id|npi|"
    r"claim[-_ ]?id|auth(?:orization)?[-_ ]?(?:id|number))(?:$|[^a-z0-9])"
)


def _norm_key(text: str) -> str:
    return " ".join(str(text).lower().replace("_", " ").split())


def _is_code_key(name: str) -> bool:
    return (
        _CODE_KEY_RE.search("_" + _norm_key(name).replace(" ", "_") + "_") is not None
    )


@dataclass
class FormFieldWrite:
    selector: str
    field_name: str
    value: str
    exact_code: bool = False


@dataclass
class FormSubmitPlan:
    fields: list[FormFieldWrite]
    submit_selector: str = ""
    target_url: str = ""


@dataclass
class FormPlanValidationResult:
    ok: bool
    mismatched_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    blank_fields: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    reason: str = ""


def _ob_attr(ob: Any, name: str, default: Any = "") -> Any:
    if isinstance(ob, Mapping):
        return ob.get(name, default)
    return getattr(ob, name, default)


def validate_form_plan(
    plan: FormSubmitPlan,
    *,
    pinned_entities: Mapping[str, str] | Sequence[Any] = (),
    required_obligations: Sequence[Any] = (),
) -> FormPlanValidationResult:
    """Check every field before anything is dispatched. Any problem blocks all."""
    pinned: dict[str, str] = {}
    if isinstance(pinned_entities, Mapping):
        pinned = {_norm_key(k): str(v) for k, v in pinned_entities.items()}
    mismatched: list[str] = []
    blank: list[str] = []
    missing: list[str] = []
    problems: list[str] = []
    by_name: dict[str, list[FormFieldWrite]] = {}
    for fw in plan.fields:
        by_name.setdefault(_norm_key(fw.field_name), []).append(fw)
        if not str(fw.selector).strip():
            problems.append(f"{fw.field_name}: empty selector")
        if not str(fw.value).strip():
            blank.append(fw.field_name)
            continue
        key = _norm_key(fw.field_name)
        if key in pinned and (fw.exact_code or _is_code_key(fw.field_name)):
            if fw.value != pinned[key]:
                mismatched.append(fw.field_name)
                problems.append(
                    f"{fw.field_name}: expected exactly {pinned[key]!r}, got {fw.value!r}"
                )
        elif key in pinned and fw.value != pinned[key]:
            if _norm_key(fw.value) != _norm_key(pinned[key]):
                mismatched.append(fw.field_name)
                problems.append(f"{fw.field_name}: expected {pinned[key]!r}")
    for ob in required_obligations:
        if not _ob_attr(ob, "required", True):
            continue
        label = str(_ob_attr(ob, "label"))
        expected = str(_ob_attr(ob, "expected_value"))
        field_id = str(_ob_attr(ob, "field_id"))
        exact = bool(_ob_attr(ob, "exact_match", True))
        hits = by_name.get(_norm_key(label), []) + by_name.get(_norm_key(field_id), [])
        found = False
        for fw in hits:
            if not expected:
                found = bool(str(fw.value).strip())
            elif exact:
                found = fw.value == expected
            else:
                found = _norm_key(expected) in _norm_key(fw.value)
            if found:
                break
        if not found:
            missing.append(label or field_id)
    if not plan.fields:
        problems.append("plan has no fields")
    for name in blank:
        problems.append(f"{name}: blank value")
    for name in missing:
        problems.append(f"{name}: required value missing")
    bad = bool(problems)
    return FormPlanValidationResult(
        ok=not bad,
        mismatched_fields=mismatched,
        missing_fields=missing,
        blank_fields=blank,
        problems=problems,
        reason="; ".join(problems) if bad else "all fields valid",
    )


def expand_form_plan_to_steps(plan: FormSubmitPlan) -> list[dict[str, Any]]:
    """Fill steps in order, then one commit click (when a submit selector is set)."""
    steps: list[dict[str, Any]] = [
        {
            "action": "fill",
            "selector": fw.selector,
            "value": fw.value,
            "field_name": fw.field_name,
            "is_commit": False,
        }
        for fw in plan.fields
    ]
    if plan.submit_selector:
        steps.append(
            {
                "action": "click",
                "selector": plan.submit_selector,
                "value": "",
                "field_name": "",
                "is_commit": True,
            }
        )
    return steps


# -- Declarative SOP contract ------------------------------------------------


@dataclass(frozen=True)
class SopRule:
    rule_id: str
    label: str
    kind: str
    required_tokens: tuple[str, ...] = ()
    required_patterns: tuple[str, ...] = ()
    min_length: int = 0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "label": self.label,
            "kind": self.kind,
            "required_tokens": list(self.required_tokens),
            "required_patterns": list(self.required_patterns),
            "min_length": self.min_length,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SopRule:
        return cls(
            rule_id=str(data["rule_id"]),
            label=str(data.get("label", data["rule_id"])),
            kind=str(data.get("kind", "phrase")),
            required_tokens=tuple(str(t) for t in data.get("required_tokens", ())),
            required_patterns=tuple(str(p) for p in data.get("required_patterns", ())),
            min_length=int(data.get("min_length", 0)),
            description=str(data.get("description", "")),
        )


@dataclass
class DeclarativeSopContract:
    sop_id: str
    domain: str
    version: str
    rules: list[SopRule]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sop_id": self.sop_id,
            "domain": self.domain,
            "version": self.version,
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeclarativeSopContract:
        return cls(
            sop_id=str(data["sop_id"]),
            domain=str(data.get("domain", "")),
            version=str(data.get("version", "1")),
            rules=[SopRule.from_dict(r) for r in data.get("rules", ())],
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> DeclarativeSopContract:
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass
class SopEvaluationResult:
    passed: bool
    satisfied_rules: list[str] = field(default_factory=list)
    missing_rules: list[str] = field(default_factory=list)
    repair_hint: str = ""
    reason: str = ""


def _rule_satisfied(rule: SopRule, text: str) -> bool:
    if len(text.strip()) < rule.min_length:
        return False
    low = text.lower()
    for token in rule.required_tokens:
        if token.lower() not in low:
            return False
    for pattern in rule.required_patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE) is None:
                return False
        except re.error:
            return False
    return True


def _hold_done(rules: Sequence[SopRule]) -> str:
    lines = ["HOLD_DONE: do not finish yet. These SOP rules are not satisfied:"]
    for r in rules:
        lines.append(f"- {r.rule_id} ({r.label}): {r.description or r.kind}")
    lines.append("Fix exactly these, save, then re-check the saved view.")
    return "\n".join(lines)


def evaluate_readback_against_sop(
    contract: DeclarativeSopContract,
    readback_text: str,
    *,
    is_editable: bool = False,
    is_toast: bool = False,
) -> SopEvaluationResult:
    """Pass only when every rule holds in a non-editable, non-toast read-back."""
    if is_toast or is_editable:
        what = "a toast" if is_toast else "an editable field"
        return SopEvaluationResult(
            passed=False,
            missing_rules=[r.rule_id for r in contract.rules],
            repair_hint=_hold_done(contract.rules),
            reason=f"read-back is {what}, not committed state",
        )
    text = readback_text or ""
    satisfied: list[str] = []
    unmet: list[SopRule] = []
    for rule in contract.rules:
        if _rule_satisfied(rule, text):
            satisfied.append(rule.rule_id)
        else:
            unmet.append(rule)
    if unmet:
        return SopEvaluationResult(
            passed=False,
            satisfied_rules=satisfied,
            missing_rules=[r.rule_id for r in unmet],
            repair_hint=_hold_done(unmet),
            reason=f"{len(unmet)} of {len(contract.rules)} SOP rules unsatisfied",
        )
    return SopEvaluationResult(
        passed=True,
        satisfied_rules=satisfied,
        reason=f"all {len(contract.rules)} SOP rules satisfied",
    )


# -- Human-touch compounding -------------------------------------------------


@dataclass
class ControllerOutcome:
    task_id: str
    completed: bool
    human_touches: int
    autonomy: str  # "AVC_0" no human, "AVC_H" human resolved, "BLOCKED" otherwise
    used_macro: bool = False
    macro_id: str = ""
    patch: dict[str, Any] = field(default_factory=dict)


class MicroHandoffCompoundingController:
    """Turn each human resolution into a stored macro so the next run needs none."""

    def __init__(self, store: MacroStore) -> None:
        if store.isolation_mode != ISOLATION_PERSISTENT:
            raise ValueError("compounding needs a continual_persistent MacroStore")
        self.store = store
        self.human_touches_by_task: dict[str, int] = {}

    def handle_stall(
        self,
        *,
        run_id: str,
        task_id: str,
        task_kind: str,
        origin_path_pattern: str,
        reason: str,
        blocker_kind: str,
        missing_obligations: Sequence[Any] = (),
        stalled_selectors: Sequence[str] = (),
        human_resolver: Callable[[MicroHandoffTicket], MicroHandoffResolution],
    ) -> ControllerOutcome:
        macro = self.store.get(
            task_id=task_id,
            origin_path_pattern=origin_path_pattern,
            task_kind=task_kind,
        )
        if macro is not None:
            self.store.record_macro_outcome(macro.macro_id, True)
            return ControllerOutcome(
                task_id=task_id,
                completed=True,
                human_touches=0,
                autonomy="AVC_0",
                used_macro=True,
                macro_id=macro.macro_id,
            )
        ticket = build_micro_handoff(
            run_id=run_id,
            task_id=task_id,
            reason=reason,
            blocker_kind=blocker_kind,
            current_url=origin_path_pattern,
            missing_obligations=missing_obligations,
            stalled_selectors=stalled_selectors,
        )
        self.human_touches_by_task[task_id] = (
            self.human_touches_by_task.get(task_id, 0) + 1
        )
        resolution = human_resolver(ticket)
        patch = apply_human_resolution(ticket, resolution)
        if not patch["applied"]:
            return ControllerOutcome(
                task_id=task_id,
                completed=False,
                human_touches=self.human_touches_by_task[task_id],
                autonomy="BLOCKED",
                patch=patch,
            )
        macro = self._compile(task_kind, origin_path_pattern, patch)
        self.store.put(
            macro,
            task_id=task_id,
            origin_path_pattern=origin_path_pattern,
            task_kind=task_kind,
        )
        return ControllerOutcome(
            task_id=task_id,
            completed=True,
            human_touches=self.human_touches_by_task[task_id],
            autonomy="AVC_H",
            macro_id=macro.macro_id,
            patch=patch,
        )

    @staticmethod
    def _compile(
        task_kind: str, origin_path_pattern: str, patch: Mapping[str, Any]
    ) -> CompiledCommitMacro:
        steps: list[CompiledMacroStep] = []
        hints = patch.get("selector_hints") or []
        action = patch.get("human_action") or {}
        for i, hint in enumerate(hints):
            last = i == len(hints) - 1
            steps.append(
                CompiledMacroStep(
                    action=str(action.get("action") or "click"),
                    selector=str(hint["selector"]),
                    value_template=str(action.get("value") or ""),
                    is_commit=last,
                )
            )
        if not steps and action:
            steps.append(
                CompiledMacroStep(
                    action=str(action.get("action") or "click"),
                    selector=str(action.get("selector") or ""),
                    value_template=str(action.get("value") or ""),
                    is_commit=True,
                )
            )
        digest = hashlib.sha256(
            json.dumps([origin_path_pattern, [s.selector for s in steps]]).encode()
        ).hexdigest()[:10]
        macro_id = f"human-{task_kind}-{digest}"
        return CompiledCommitMacro(
            macro_id=macro_id,
            route_pattern=origin_path_pattern,
            steps=steps,
            slot_names=[],
        )
