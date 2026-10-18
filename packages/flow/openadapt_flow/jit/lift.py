"""Propose typed parameters from the task's input payload.

The agent was told the run's inputs, so a typed step whose text is exactly one
input's value is where that input goes. :func:`lift_params` writes each match
into the bundle's ``param_proposals.json`` under the input's name, next to the
compiler's own field-label proposals. Nothing is applied here. A proposal
becomes a parameter only through the existing confirm pass
(:mod:`openadapt_flow.compiler.param_confirm`), which a person runs when they
admit the candidate.

A value is not lifted when it was typed in more than one step, typed inside
longer text, or never typed whole. Those inputs are reported so the reviewer
sees them; the program would repeat the demonstrated text for them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.compiler.annotate import mask_value
from openadapt_flow.compiler.param_confirm import PROPOSALS_FILENAME
from openadapt_flow.ir import ActionKind, Workflow

__all__ = ["LiftReport", "LiftedParam", "lift_params"]


@dataclass(frozen=True)
class LiftedParam:
    name: str
    step_id: str
    field_label: Optional[str]
    masked_example: Optional[str]


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


def _match(workflow: Workflow, value: str) -> tuple[list[str], bool]:
    whole: list[str] = []
    inside = False
    for step in workflow.steps:
        if step.action != ActionKind.TYPE or step.param or step.text is None:
            continue
        if step.text == value:
            whole.append(step.id)
        elif value in step.text:
            inside = True
    return whole, inside


def lift_params(bundle_dir: Path | str, inputs: Mapping[str, str]) -> LiftReport:
    """Add one proposal per input typed as a whole value in exactly one step.

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
        if len(whole) > 1:
            report.unlifted[name] = f"typed in {len(whole)} steps"
            continue
        if not whole:
            report.unlifted[name] = (
                "typed inside longer text" if inside else "not typed in any step"
            )
            continue
        step_id = whole[0]
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
