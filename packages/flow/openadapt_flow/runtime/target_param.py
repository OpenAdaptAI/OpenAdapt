"""Point a record-selecting click at the run's record (``Step.target_param``).

A demonstrated click on a search result or a worklist row targets the row the
person clicked. When a run (or a loop iteration) works on a different record,
the click must land on that record's row instead. ``Step.target_param`` names
the run parameter whose value identifies the row.

:func:`bind_target_param` returns the step the runtime acts on:

- the run's value equals the demonstrated one: the step is unchanged;
- otherwise the anchor looks for the run's value: its OCR label becomes the
  value, the demonstrated crop is dropped (it shows the demonstrated row), and
  a structural locator is kept only when its accessible name was exactly the
  demonstrated value (a CSS selector or automation id points at the old row,
  so it is dropped).

It refuses (the step halts before any input) when:

- the run has no value, or an empty one, for the parameter;
- the anchor carries no identity evidence, or its identity evidence does not
  embed the parameter: the identity check could then not tell the run's
  record from the demonstrated one;
- the identity band embeds another parameter the run gives no value for.

The identity check that runs before every click then verifies the live row in
parameter mode: the run's value must be in the row, and the rest of the row
must match the recorded row with every embedded parameter substituted. No
model calls.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

from openadapt_flow.ir import Anchor, Step, Workflow

#: Template path used for a rebound anchor: no crop, so the template rungs are
#: skipped (the recorded crop shows the demonstrated row).
NO_TEMPLATE = ""


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _squash(text: str) -> str:
    return "".join(text.casefold().split())


def demo_value(workflow: Workflow, name: str) -> str:
    """The demonstrated value of ``name`` (empty when there is none)."""

    if name in workflow.params:
        return _text(workflow.params[name])
    spec = workflow.param_specs.get(name)
    if spec is not None and spec.example is not None:
        return _text(spec.example)
    return ""


def embedded_identity_params(anchor: Anchor, examples: Mapping[str, str]) -> set[str]:
    """Parameters whose demonstrated value the anchor's identity band embeds."""

    from openadapt_flow.runtime import identity as _id

    names: set[str] = set()
    tmpl = anchor.identity_template
    if tmpl is not None:
        names.update(tmpl.param_token_indices)
        names.update(tmpl.structured_params)
        names.update(getattr(tmpl, "context_params", None) or [])
    for band in (anchor.context_text, anchor.structured_identity):
        if band:
            names.update(
                _id.embedded_params(band, {k: v for k, v in examples.items() if v})
            )
    return names


def has_identity_evidence(anchor: Anchor) -> bool:
    tmpl = anchor.identity_template
    return bool(
        anchor.context_text
        or anchor.structured_identity
        or (tmpl is not None and (tmpl.tokens or tmpl.structured))
    )


def bind_target_param(
    step: Step,
    params: Mapping[str, object],
    workflow: Workflow,
) -> tuple[Step, Optional[str]]:
    """Return ``(step to act on, refusal)``. A refusal means: halt, no input."""

    name = step.target_param
    if name is None:
        return step, None
    anchor = step.anchor
    if anchor is None:  # the IR validator already refuses this
        return step, f"Step '{step.id}' selects a record but has no anchor"
    value = _text(params.get(name)).strip()
    if not value:
        return step, (
            f"Step '{step.id}' ({step.intent}) selects the record named by "
            f"parameter '{name}', but this run gives no value for it; "
            "refusing to click"
        )
    if not has_identity_evidence(anchor):
        return step, (
            f"Step '{step.id}' ({step.intent}) selects a record by parameter "
            f"'{name}' but carries no identity evidence to verify the row; "
            "refusing to click"
        )
    examples = {
        key: demo_value(workflow, key)
        for key in set(workflow.params) | set(workflow.param_specs)
    }
    embedded = embedded_identity_params(anchor, examples)
    if name not in embedded:
        return step, (
            f"Step '{step.id}' ({step.intent}) selects a record by parameter "
            f"'{name}', but its identity evidence does not contain that "
            "parameter, so the row could not be verified; refusing to click"
        )
    missing = sorted(
        other
        for other in embedded
        if not _text(params.get(other)).strip() and not examples.get(other)
    )
    if missing:
        return step, (
            f"Step '{step.id}' ({step.intent}) verifies the row by "
            f"{', '.join(repr(m) for m in missing)}, which this run does not "
            "supply; refusing to click"
        )
    demo = examples.get(name, "")
    if demo and _squash(value) == _squash(demo):
        return step, None

    ocr_text = value
    structural = None
    if anchor.structural is not None and anchor.structural.name and demo:
        if _squash(anchor.structural.name) == _squash(demo):
            structural = anchor.structural.model_copy(
                update={"name": value, "selector": None, "automation_id": None}
            )
    if anchor.ocr_text and demo and _squash(anchor.ocr_text) != _squash(demo):
        # Keep the label's own words when the demonstrated value is a whole
        # word run inside it ("Open Alice" -> "Open Bob"); otherwise look for
        # the value alone.
        pattern = re.compile(re.escape(demo), re.IGNORECASE)
        if len(pattern.findall(anchor.ocr_text)) == 1:
            ocr_text = pattern.sub(lambda _m: value, anchor.ocr_text)
    bound_anchor = anchor.model_copy(
        update={
            "template": NO_TEMPLATE,
            "ocr_text": ocr_text,
            "structural": structural,
        }
    )
    return step.model_copy(update={"anchor": bound_anchor}), None
