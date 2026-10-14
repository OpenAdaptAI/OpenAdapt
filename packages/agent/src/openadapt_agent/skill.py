"""Agent Skill export: what a workflow does and how to call it, nothing more.

``openadapt-agent emit-skill BUNDLE --out DIR`` writes ``DIR/<name>/SKILL.md``
from the workflow card (see :mod:`openadapt_agent.cards`): its name, purpose,
what done means, typed inputs, and how to read the four outcomes. A skill is
loaded into model context, which for most clients means a hosted model, so the
file never carries recorded example values, step intents, observed text,
secrets, or local paths. Each skill's description names its own workflow and
inputs, so an agent with several skills can tell them apart.

The compiled bundle stays with the operator's server. ``--include-bundle``
copies it next to ``SKILL.md`` for an operator who wants one portable folder;
that copy is protected workflow data and SKILL.md tells the agent not to read
it. Flow's own ``openadapt-flow emit-skill`` remains available for replay
demos.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from openadapt_agent.bundles import load_workflow_info
from openadapt_agent.cards import WorkflowCard, card_for_bundle, inputs_schema
from openadapt_agent.copy import OUTCOME_RULES, SKILL_NAME

__all__ = ["emit_agent_skill", "render_skill"]

_FORBIDDEN_SKILL_NAMES = frozenset({"computer-use", "computer_use", "computeruse"})
_DESCRIPTION_LIMIT = 1024


def _skill_name(card_name: str) -> str:
    name = card_name.replace("_", "-").lower()[:64].strip("-")
    if name in _FORBIDDEN_SKILL_NAMES or name.replace("-", "") == "computeruse":
        return SKILL_NAME
    return name or SKILL_NAME


def _input_line(name: str, schema: dict[str, Any], required: bool) -> str:
    kind = schema.get("type", "string")
    shape = {"string": "text", "number": "number", "boolean": "true or false"}.get(kind, kind)
    if schema.get("format") == "date":
        shape = "date, YYYY-MM-DD"
    elif schema.get("format") == "date-time":
        shape = "date and time, ISO 8601"
    if schema.get("enum"):
        shape = "one of " + ", ".join(f"`{value}`" for value in schema["enum"])
    details = [shape]
    if "maxLength" in schema:
        details.append(f"up to {schema['maxLength']} characters")
    if "pattern" in schema:
        details.append(f"matches `{schema['pattern']}`")
    if not required:
        details.append("optional")
    description = schema.get("description", "")
    return f"- `{name}` ({'; '.join(details)}): {description}".rstrip(": ")


def _description(card: WorkflowCard) -> str:
    inputs = ", ".join(sorted(card.inputs)) or "none"
    text = (
        f"{card.purpose} Runs the OpenAdapt workflow {card.name} through the "
        f"run_workflow tool and reports done, needs_review, not_sure_if_saved, "
        f"or did_not_run. Inputs: {inputs}."
    )
    return text[:_DESCRIPTION_LIMIT]


def render_skill(card: WorkflowCard, *, include_bundle: bool = False) -> str:
    """Render SKILL.md for one workflow card. Pure: no I/O."""
    schema = inputs_schema(card)
    required = set(schema["required"])
    inputs = "\n".join(
        _input_line(name, prop, name in required) for name, prop in schema["properties"].items()
    ) or "- This workflow takes no inputs. Send an empty object."
    example_inputs = {name: f"<{name}>" for name in sorted(required)}
    call = json.dumps(
        {
            "workflow": card.name,
            "inputs": example_inputs,
            "request_id": "<your id for this piece of work>",
        },
        indent=2,
    )
    bundle_note = ""
    if include_bundle:
        bundle_note = (
            "\nThe `bundle` folder next to this file is the compiled workflow for the "
            "operator's server. It is protected workflow data. Don't open or quote it.\n"
        )
    return f"""---
name: {_skill_name(card.name)}
description: {json.dumps(_description(card))}
---

# {card.name}

{card.purpose}

Done means: {card.done_means}

## When to use it

Use this skill when you have decided what to enter and the user wants it entered
with the `{card.name}` workflow. OpenAdapt does the entry in the app on the
computer that runs it, then reads the record back to check that it saved.

## How to run it

1. Call `run_workflow` on the OpenAdapt MCP server:

   ```json
{_indent(call, 3)}
   ```

   Inputs:

{_indent(inputs, 3)}

   `request_id` is your own id for this piece of work, such as the referral id.
   Send the same id if you retry; the same id never writes twice. Don't put
   patient details in it.
2. If the outcome is `running`, call `get_run` with the `run_id` until it isn't.
3. Tell the user `what_happened` and follow `next_action`.

## What the outcome means

| outcome | What happened | What to do |
| --- | --- | --- |
| `done` | Saved and checked. | Nothing more for this request. |
| `needs_review` | Stopped before saving. Nothing was written. A person decides. | Don't start the same work again. Check back with `get_run`. |
| `not_sure_if_saved` | It may or may not have saved. | Never retry. A person checks the record. |
| `did_not_run` | Nothing was written. | Fix the problem in `what_happened`, then retry with the same `request_id` if `safe_to_retry` is true. |

{OUTCOME_RULES}

## Setup for the operator

The operator serves this workflow on the computer that can open the app:

```bash
openadapt-agent serve --mode production --bundles <bundles-dir>
```
{bundle_note}"""


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def emit_agent_skill(
    bundle_dir: Path | str, out_dir: Path | str, *, include_bundle: bool = False
) -> Path:
    """Write a PHI-safe skill folder for one bundle and return its path."""
    bundle_dir = Path(bundle_dir)
    info = load_workflow_info(bundle_dir)
    if not info.ok:
        raise ValueError("the workflow bundle could not be loaded safely")
    card = card_for_bundle(info, default_name=info.public_id)
    skill_dir = Path(out_dir) / _skill_name(card.name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        render_skill(card, include_bundle=include_bundle), encoding="utf-8"
    )
    if include_bundle:
        shutil.copytree(bundle_dir, skill_dir / "bundle", dirs_exist_ok=True)
    return skill_dir
