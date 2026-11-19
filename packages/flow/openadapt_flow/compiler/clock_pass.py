"""Compile the clock-derived screen texts of an existing bundle.

``compile_recording`` already mines a clock-derived text as a clock template
(``Postcondition.text_clock``). A bundle compiled before that has the
recorded literal instead: its date picker check expects "October-2026-" and
halts every run from November on. This pass applies the same rule to such a
bundle: each text check whose date fragments all equal the recording date at
their own precision gets the clock template, and nothing else changes. The
result is a new bundle (a new content digest) that must be qualified again
before a certified run will take it.

    python -m openadapt_flow.compiler.clock_pass BUNDLE OUT --recorded-on 2026-10-20

``--recorded-on`` is the recording's date (the recording's ``meta.json``
``created_at``); it defaults to the bundle's ``created_at``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from openadapt_flow import clock_text, volatility
from openadapt_flow.bundle_validation import derive_verified_workflow
from openadapt_flow.ir import PostconditionKind, Step, Workflow

_TEXT_KINDS = (PostconditionKind.TEXT_PRESENT, PostconditionKind.TEXT_ABSENT)


def clock_text_template(text: str, recorded_on: date) -> Optional[str]:
    """The template the miner would give ``text``, or None (the miner's rule)."""

    if volatility.classify_text(text, reference_date=recorded_on) != "near_date":
        return None
    if volatility.residual_reason(text) is not None:
        return None
    return clock_text.compile_clock_text(text, recorded_on)


def relativize_clock_texts(
    workflow: Workflow, recorded_on: date
) -> tuple[list[Step], list[str]]:
    """``workflow``'s steps with each clock-derived text check templated.

    Exact checks (``Postcondition.exact``, the empty-result markers) and
    checks that already carry a template are left alone. ``workflow`` is not
    changed.

    Returns:
        The new steps, and ``"<step id>: <recorded text> -> <template>"`` for
        each change.
    """

    steps: list[Step] = []
    changed: list[str] = []
    for step in workflow.steps:
        expect = list(step.expect)
        for index, condition in enumerate(expect):
            if (
                condition.kind not in _TEXT_KINDS
                or not condition.text
                or condition.exact
                or condition.text_clock is not None
            ):
                continue
            template = clock_text_template(condition.text, recorded_on)
            if template is None:
                continue
            expect[index] = condition.model_copy(update={"text_clock": template})
            changed.append(f"{step.id}: {condition.text!r} -> {template!r}")
        steps.append(step.model_copy(update={"expect": expect}) if expect != step.expect else step)
    return steps, changed


def _recorded_on(value: Optional[str], workflow: Workflow) -> date:
    raw = value or str(workflow.created_at or "")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return date.fromisoformat(raw)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--recorded-on", default=None)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"{args.out} exists")
    workflow = Workflow.load(args.bundle)
    recorded_on = _recorded_on(args.recorded_on, workflow)
    steps, changes = relativize_clock_texts(workflow, recorded_on)
    if not changes:
        print("no clock-derived text checks; nothing written", file=sys.stderr)
        return 1
    # A changed contract keeps no prior certification: qualify it again.
    target = derive_verified_workflow(workflow, update={"steps": steps})
    shutil.copytree(args.bundle, args.out)
    target.save(args.out)
    from openadapt_flow.compiler.codegen import render_workflow_py

    (args.out / "workflow.py").write_text(render_workflow_py(target), encoding="utf-8")
    for line in changes:
        print(line)
    assert target.manifest is not None
    print(f"content digest {target.manifest.content_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
