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

from openadapt_flow.ir import Workflow

_BUNDLE_SUBDIR = "bundle"


def _slugify(name: str) -> str:
    """Lowercase, hyphen-separated slug safe for a skill folder name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workflow"


def _shell_quote(value: str) -> str:
    """Quote a parameter value for a copy-pasteable shell example."""
    if re.fullmatch(r"[A-Za-z0-9_.:/@-]+", value or ""):
        return value
    return '"' + value.replace('"', '\\"') + '"'


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
        f"Replay the recorded '{workflow.name}' workflow "
        f"({n_steps} deterministic vision-anchored steps) against a live "
        f"app with self-healing on UI drift."
    )

    param_flags = " ".join(
        f"--param {name}={_shell_quote(example)}"
        for name, example in workflow.params.items()
    )
    invocation = f"openadapt-flow replay {_BUNDLE_SUBDIR} --url <APP_URL>"
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
            lines.append(f"1. {step.intent}")
    else:
        lines.append("_No steps recorded._")
    lines.append("")
    lines.append("## When to use")
    lines.append("")
    lines.append(
        f"Use this skill whenever the user asks to perform the "
        f"'{workflow.name}' workflow (or an equivalent request) against a "
        f"running instance of the target app. Do not re-derive the steps "
        f"manually — replaying the compiled bundle is deterministic, "
        f"verifies postconditions after every step, and heals minor UI "
        f"drift automatically."
    )
    lines.append("")
    lines.append("## Parameters")
    lines.append("")
    if workflow.params:
        lines.append("| Name | Example value |")
        lines.append("| --- | --- |")
        for name, example in workflow.params.items():
            lines.append(f"| `{name}` | `{example}` |")
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
    if workflow.params:
        usage_note += (
            "Each `--param k=v` substitutes a recorded parameter "
            "(omitted parameters fall back to the recorded example "
            "values). "
        )
    usage_note += (
        "The run writes a `report.json` and `REPORT.md` into the run "
        "directory (`--run-dir`, default `runs/replay-<timestamp>/` under "
        "the current directory); a non-zero exit code means the replay "
        "failed and the report names the failing step."
    )
    lines.append(usage_note)
    lines.append("")

    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir
