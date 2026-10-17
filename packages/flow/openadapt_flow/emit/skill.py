"""Emit a workflow bundle as an Agent Skills folder (``SKILL.md``).

The bundle is COPIED into the skill folder (``<skill>/bundle/``) so the
emitted artifact is self-contained and portable: it can be shipped to
another machine or checked into a skills repository without referencing
any path on the emitting machine.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from openadapt_flow.ir import ActionKind, Step, Workflow

_BUNDLE_SUBDIR = "bundle"

# SKILL.md is loaded into a model's context and is often checked into a
# skills repository. It must never carry a value from the recording: typed
# text (a password or a patient value), a parameter's recorded example, or an
# on-screen label that repeats one of them. Steps are therefore described from
# their action kind and parameter name, never from ``step.intent`` (the
# compiler embeds the literal typed text there).
_WITHHELD_TEXT = "[recorded text withheld]"
_WITHHELD_TARGET = "recorded target"


def _slugify(name: str) -> str:
    """Lowercase, hyphen-separated slug safe for a skill folder name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workflow"


def declared_params(workflow: Workflow) -> list[str]:
    """Names of the workflow's non-secret parameters, in declaration order."""
    secret = set(workflow.secret_params) | {
        step.param for step in workflow.steps if step.secret and step.param
    }
    names: list[str] = []
    for name in [*workflow.params, *workflow.param_specs]:
        if name not in secret and name not in names:
            names.append(name)
    return names


def _secret_params(workflow: Workflow) -> list[str]:
    names: list[str] = []
    for name in [
        *workflow.secret_params,
        *(step.param for step in workflow.steps if step.secret and step.param),
    ]:
        if name and name not in names:
            names.append(name)
    return names


def _recorded_literals(workflow: Workflow) -> list[str]:
    """Every value the recording captured that SKILL.md must not repeat."""
    values: list[str] = [str(v) for v in workflow.params.values()]
    values += [
        str(spec.example)
        for spec in workflow.param_specs.values()
        if spec.example is not None
    ]
    values += [
        step.text
        for step in workflow.steps
        if step.text and step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)
    ]
    return [v.strip() for v in values if v and v.strip()]


def _label(text: str | None, literals: list[str]) -> str | None:
    """An on-screen target label, or None when it repeats a recorded value."""
    if not text or not text.strip():
        return None
    folded = text.casefold()
    if any(lit.casefold() in folded for lit in literals):
        return None
    return text.strip()


def _step_line(step: Step, literals: list[str]) -> str:
    """Describe one step from its action kind, never from its intent text."""
    action = step.action
    if action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
        verb = "type" if action is ActionKind.TYPE else "select"
        if step.param:
            return f"{verb} <{step.param}>" + (" (secret)" if step.secret else "")
        return f"{verb} {_WITHHELD_TEXT}"
    if action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK):
        verb = {
            ActionKind.CLICK: "click",
            ActionKind.DOUBLE_CLICK: "double-click",
            ActionKind.RIGHT_CLICK: "right-click",
        }[action]
        label = _label(step.anchor.ocr_text if step.anchor else None, literals)
        return f"{verb} '{label}'" if label else f"{verb} {_WITHHELD_TARGET}"
    if action is ActionKind.DRAG:
        src = _label(step.anchor.ocr_text if step.anchor else None, literals)
        dst = _label(
            step.drag_end_anchor.ocr_text if step.drag_end_anchor else None,
            literals,
        )
        return (
            "drag "
            + (f"'{src}'" if src else _WITHHELD_TARGET)
            + " to "
            + (f"'{dst}'" if dst else "recorded destination")
        )
    if action is ActionKind.KEY:
        key = step.key or ""
        # A single printable key can be part of typed data; name only keys.
        return f"press {key}" if len(key) > 1 else "press a recorded key"
    if action is ActionKind.HOTKEY:
        return "press " + "+".join([*step.modifiers, step.key or ""])
    if action is ActionKind.SCROLL:
        return f"scroll by ({step.scroll_dx or 0}, {step.scroll_dy or 0})"
    return action.value


def emit_skill(bundle_dir: Path | str, out_dir: Path | str) -> Path:
    """Write a self-contained Agent Skills folder for the bundle's workflow.

    Creates ``<out_dir>/<slug>/SKILL.md`` with YAML frontmatter (``name``,
    ``description``) and a body covering what the workflow does, when to
    use it, its parameters, and the exact CLI invocation. The workflow
    bundle is copied into ``<out_dir>/<slug>/bundle/`` and the invocation
    references it by that relative path, so the folder is portable.

    Args:
        bundle_dir: Workflow bundle directory (contains ``workflow.json``).
        out_dir: Parent directory to create the skill folder in.

    Returns:
        Path to the created skill folder (the directory containing
        ``SKILL.md`` and ``bundle/``).
    """
    bundle = Path(bundle_dir).resolve()
    workflow = Workflow.load(bundle)
    slug = _slugify(workflow.name)

    skill_dir = Path(out_dir) / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    bundle_copy = skill_dir / _BUNDLE_SUBDIR
    if bundle_copy.resolve() != bundle:
        shutil.copytree(bundle, bundle_copy, dirs_exist_ok=True)

    n_steps = len(workflow.steps)
    description = (
        f"Run the compiled '{workflow.name}' workflow "
        f"({n_steps} deterministic vision-anchored steps) against a live "
        f"app with self-healing on UI drift."
    )

    params = declared_params(workflow)
    secrets = _secret_params(workflow)
    literals = _recorded_literals(workflow)
    # Placeholders only: a recorded example is never a usable value for a new
    # request, and the governed ``run`` path (not the permissive ``replay``)
    # is the one an agent should call.
    param_flags = " ".join(f"--param {name}=<{name}>" for name in params)
    invocation = f"openadapt-flow run {_BUNDLE_SUBDIR} --url <APP_URL>"
    if param_flags:
        invocation += f" {param_flags}"

    lines: list[str] = []
    lines.append("---")
    lines.append(f"name: {slug}")
    lines.append(f"description: {description}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {workflow.name}")
    lines.append("")
    lines.append(
        f"Compiled workflow bundle: `{_BUNDLE_SUBDIR}/` (copied into this "
        f"skill folder; schema v{workflow.schema_version}, {n_steps} steps)."
    )
    lines.append("")
    lines.append("## What it does")
    lines.append("")
    if workflow.steps:
        for step in workflow.steps:
            lines.append(f"1. {_step_line(step, literals)}")
    else:
        lines.append("_No steps recorded._")
    lines.append("")
    lines.append("## When to use")
    lines.append("")
    lines.append(
        f"Use this skill whenever the user asks to perform the "
        f"'{workflow.name}' workflow (or an equivalent request) against a "
        f"running instance of the target app. Don't re-derive the steps "
        f"manually. Running the compiled bundle is deterministic, "
        f"verifies postconditions after every step, and heals minor UI "
        f"drift automatically."
    )
    lines.append("")
    lines.append("## Parameters")
    lines.append("")
    if params or secrets:
        lines.append("| Name | Type | Required |")
        lines.append("| --- | --- | --- |")
        for name in params:
            spec = workflow.param_specs.get(name)
            kind = spec.type.value if spec is not None else "string"
            lines.append(f"| `{name}` | {kind} | yes |")
        for name in secrets:
            lines.append(f"| `{name}` | secret | yes |")
    else:
        lines.append("_This workflow takes no parameters._")
    lines.append("")
    lines.append("## Usage")
    lines.append("")
    lines.append("```bash")
    lines.append(invocation)
    lines.append("```")
    lines.append("")
    usage_note = (
        f"Run the command from this skill folder (the `{_BUNDLE_SUBDIR}` "
        f"path is relative to it), or substitute the absolute path of "
        f"`{_BUNDLE_SUBDIR}/`. Replace `<APP_URL>` with the URL of the "
        f"running target app. "
    )
    if params:
        usage_note += (
            "Every parameter is required. Replace each `<name>` placeholder "
            "with the value for this request, and don't reuse a value from "
            "an earlier run. "
        )
    if secrets:
        from openadapt_flow.runtime.replayer import secret_env_var

        usage_note += (
            "Don't pass a secret on the command line. Set "
            + ", ".join(f"`{secret_env_var(name)}`" for name in secrets)
            + " in the environment instead. "
        )
    usage_note += (
        "`run` checks the bundle against its policy first and refuses, "
        "without acting, when the bundle isn't approved for this deployment. "
        "The run writes a `report.json` and `REPORT.md` into the run "
        "directory (`--run-dir`, default `runs/replay-<timestamp>/` under "
        "the current directory). A non-zero exit code means the run didn't "
        "finish, and the report names the step where it stopped. When the "
        "report's transaction outcome is `RECONCILIATION_REQUIRED`, a write "
        "may have landed, so check the record before you try again."
    )
    lines.append(usage_note)
    lines.append("")

    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir
