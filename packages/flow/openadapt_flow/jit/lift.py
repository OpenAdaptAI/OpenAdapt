"""Propose typed parameters from the task's input payload.

The agent was told the run's inputs, so a typed step whose text is exactly one
input's value is where that input goes. :func:`lift_params` writes each match
into the bundle's ``param_proposals.json`` under the input's name, next to the
compiler's own field-label proposals. Nothing is applied here. A proposal
becomes a parameter only through the existing confirm pass
(:mod:`openadapt_flow.compiler.param_confirm`), which a person runs when they
admit the candidate.

When a mined record check exists, a value is lifted only at a step whose
field the saved record holds it in, whether it was typed once or more (an
agent typed it into the wrong field, then the right one). The other steps are
listed in ``also_typed_in``: they still type the demonstrated text. A short
value, a number or a flag ("1") needs more: the step's field is named like
the input. When the record holds the value in no
typed field, more than one of the steps writes such a field, or a repeated
value has no record check, nothing is lifted and the reason names the steps.

A value is not lifted when it was typed inside longer text or never typed
whole. Those inputs are reported so the reviewer sees them; the program would
repeat the demonstrated text for them. That includes a value typed whole in one
step and inside longer text in another (a name typed into its field and again
into a note): lifting only the whole step would leave the note repeating the
demonstrated value on every run, with a constant that every check accepts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.compiler.annotate import mask_value
from openadapt_flow.compiler.distill import field_key
from openadapt_flow.compiler.param_confirm import PROPOSALS_FILENAME
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.record_sor import distinctive_value as _distinctive

__all__ = ["LiftReport", "LiftedParam", "lift_params"]


@dataclass(frozen=True)
class LiftedParam:
    name: str
    step_id: str
    field_label: Optional[str]
    masked_example: Optional[str]
    #: Other steps that typed the same value; they keep the demonstrated text.
    also_typed_in: tuple[str, ...] = ()


@dataclass
class LiftReport:
    lifted: list[LiftedParam] = field(default_factory=list)
    #: Input name -> why it was not lifted.
    unlifted: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifted": [asdict(item) for item in self.lifted],
            "unlifted": dict(self.unlifted),
        }


def _match(workflow: Workflow, value: str) -> tuple[list[str], list[str]]:
    whole: list[str] = []
    inside: list[str] = []
    for step in workflow.steps:
        if step.action != ActionKind.TYPE or step.param or step.text is None:
            continue
        if step.text == value:
            whole.append(step.id)
        elif value in step.text:
            inside.append(step.id)
    return whole, inside


def _bound_fields(workflow: Workflow, param: str) -> set[str]:
    """Record fields (``field_key``) a mined check already binds to ``param``."""

    bound: set[str] = set()
    for record_field, expr in _check_pairs(workflow):
        if getattr(expr, "param", None) == param:
            bound.add(field_key(record_field))
    return bound - {""}


def _check_pairs(workflow: Workflow) -> list[tuple[str, Any]]:
    """(record field, value expression) of every mined record check.

    Only mined checks count: a placeholder the operator must still bind says
    nothing about what was saved.
    """

    pairs: list[tuple[str, Any]] = []
    for step in workflow.steps:
        for effect in step.effects:
            if effect.needs_operator_confirmation:
                continue
            pairs.extend((effect.match or {}).items())
            if effect.field and effect.value is not None:
                pairs.append((effect.field, effect.value))
    return pairs


def _saved_fields(workflow: Workflow) -> dict[str, set[str]]:
    """Record field -> demonstrated values, from the mined record checks."""

    held: dict[str, set[str]] = {}
    for name, expr in _check_pairs(workflow):
        literal = getattr(expr, "literal", None)
        if literal is not None:
            held.setdefault(name, set()).add(str(literal))
    return held


def _field_names(step: Step) -> set[str]:
    names = {field_key(step.field_label)}
    structural = step.anchor.structural if step.anchor is not None else None
    if structural is not None:
        names |= {field_key(structural.selector), field_key(structural.name)}
    return names - {""}


def _pick_match(
    workflow: Workflow, name: str, step_ids: list[str], value: str
) -> tuple[Optional[str], str]:
    """The one step whose field the saved record holds ``value`` in, or why not.

    With no mined record check, a value typed in one step is lifted there.
    Otherwise the record must confirm the step: its field holds the value.
    A short value, number or flag ("1") also sits in fields it was never
    typed into (quantity and refills), so the step's field must also be
    named like the input. Among confirmed steps, the one named like the
    input wins. A step with no field name is lifted only for a distinctive
    value that one record field holds.
    """

    listed = ", ".join(step_ids)
    where = (
        f"typed in {listed}"
        if len(step_ids) == 1
        else f"typed in {len(step_ids)} steps ({listed})"
    )
    steps = {step.id: step for step in workflow.steps}
    bound = _bound_fields(workflow, name)
    if bound:
        # A mined check already binds a record field to this input: that
        # field is where the input goes, and nowhere else.
        at = [sid for sid in step_ids if _field_names(steps[sid]) & bound]
        if len(at) == 1:
            return at[0], ""
        return None, (
            f"{where}; the record check binds {name!r} to field(s) "
            f"{', '.join(sorted(bound))}, and "
            + ("no" if not at else "more than one")
            + " typed step writes them"
        )
    held = _saved_fields(workflow)
    if not held:
        if len(step_ids) == 1:
            return step_ids[0], ""
        return None, f"{where}; no mined record check shows which field kept it"
    holders = {field_key(field) for field, values in held.items() if value in values}
    if not holders:
        return None, f"{where}; the saved record holds it in no field"
    distinctive = _distinctive(value)

    def confirmed(step_id: str) -> bool:
        names = _field_names(steps[step_id])
        if not names:
            return len(step_ids) == 1 and distinctive and len(holders) == 1
        if not names & holders:
            return False
        return distinctive or field_key(name) in names

    hits = [sid for sid in step_ids if confirmed(sid)]
    named = [sid for sid in hits if field_key(name) in _field_names(steps[sid])]
    if len(named) == 1:
        hits = named
    if len(hits) == 1:
        return hits[0], ""
    if (
        not hits
        and not distinctive
        and any(_field_names(steps[sid]) & holders for sid in step_ids)
    ):
        return None, (
            f"{where}; a short value, number or flag is lifted only at a "
            f"record field named {name!r} that holds it "
            f"(held by {', '.join(sorted(holders))})"
        )
    if not hits:
        fields = "its field is not" if len(step_ids) == 1 else "none of their fields is"
        return None, (
            f"{where}; {fields} a record field that holds it "
            f"({', '.join(sorted(holders))})"
        )
    return None, (
        f"{where}; steps {', '.join(hits)} all write a record field that holds it"
    )


def lift_params(bundle_dir: Path | str, inputs: Mapping[str, str]) -> LiftReport:
    """Add one proposal per input typed as a whole value.

    Args:
        bundle_dir: A compiled bundle (``workflow.json``).
        inputs: Input name -> the text the value is typed as.

    Returns:
        What was lifted and what was not. The merged proposals are written to
        ``bundle_dir/param_proposals.json``.
    """

    bundle = Path(bundle_dir)
    workflow = Workflow.load(bundle)
    steps = {step.id: step for step in workflow.steps}
    path = bundle / PROPOSALS_FILENAME
    raw = json.loads(path.read_text()) if path.is_file() else {}
    existing: list[dict[str, Any]] = [
        dict(entry) for entry in raw.get("proposals", []) if isinstance(entry, dict)
    ]
    report = LiftReport()
    for name, value in sorted(inputs.items()):
        if not value:
            report.unlifted[name] = "empty value"
            continue
        whole, inside = _match(workflow, value)
        if not whole:
            report.unlifted[name] = (
                "typed inside longer text" if inside else "not typed in any step"
            )
            continue
        if inside:
            report.unlifted[name] = (
                f"typed whole in {', '.join(whole)} and inside longer text in "
                f"{', '.join(inside)}; the longer text would repeat the "
                "demonstrated value"
            )
            continue
        step_id, why = _pick_match(workflow, name, whole, value)
        if step_id is None:
            report.unlifted[name] = why
            continue
        clash = [
            e for e in existing if e.get("name") == name and e.get("step_id") != step_id
        ]
        if clash:
            report.unlifted[name] = (
                f"name already proposed for step {clash[0].get('step_id')}"
            )
            continue
        report.lifted.append(
            LiftedParam(
                name=name,
                step_id=step_id,
                field_label=steps[step_id].field_label,
                masked_example=mask_value(value),
                also_typed_in=tuple(sid for sid in whole if sid != step_id),
            )
        )
    lifted_steps = {item.step_id for item in report.lifted}
    merged = [e for e in existing if e.get("step_id") not in lifted_steps]
    merged.extend(
        {**asdict(item), "secret": False, "source": "task_input"}
        for item in report.lifted
    )
    if merged:
        path.write_text(
            json.dumps(
                {
                    "note": (
                        "Parameter proposals, NOT applied. Proposals with source "
                        "task_input name a run input the agent typed. A person "
                        "confirms them when admitting the candidate; unconfirmed "
                        "proposals stay demonstrated constants."
                    ),
                    "proposals": merged,
                },
                indent=2,
            )
            + "\n"
        )
    return report
