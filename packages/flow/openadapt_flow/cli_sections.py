"""``openadapt-flow sections``: show and review a program's named sections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sections",
        help=(
            "Show a compiled program's named sections (the groups a program "
            "map folds), or rename, merge or split them in review"
        ),
        description=(
            "Every compiled bundle carries sections.json: contiguous, named "
            "groups of steps, found without a model from the save, the "
            "recorded form structure, the fields and any narration. They are "
            "display metadata; execution ignores them and they are outside "
            "the bundle digests, so editing them never changes a "
            "certification."
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--rename",
        nargs=2,
        action="append",
        default=[],
        metavar=("SECTION", "TITLE"),
        help="Give a section a new title",
    )
    p.add_argument(
        "--merge",
        nargs=2,
        action="append",
        default=[],
        metavar=("SECTION", "OTHER"),
        help="Merge two adjacent sections (the save section stays separate)",
    )
    p.add_argument(
        "--split",
        nargs=2,
        action="append",
        default=[],
        metavar=("SECTION", "STEP"),
        help="Split a section so STEP starts a new one",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Drop every review edit and keep the compiled sections",
    )
    p.add_argument(
        "--recording",
        default=None,
        help=(
            "Recompute from a recording of the same demonstration, reading "
            "its recorded form structure (only for steps whose target matches)"
        ),
    )
    p.add_argument("--json", action="store_true", help="Print sections.json")
    p.set_defaults(func=cmd_sections)


def cmd_sections(args: argparse.Namespace) -> int:
    from openadapt_flow import sealed_set
    from openadapt_flow import sections as program_sections
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    key = sealed_set.passphrase_for("bundle")
    workflow = Workflow.load(bundle, key=key)
    edits = [
        *(
            program_sections.SectionEdit(op="rename", section=s, title=t)
            for s, t in args.rename
        ),
        *(
            program_sections.SectionEdit(op="merge", section=s, other=o)
            for s, o in args.merge
        ),
        *(
            program_sections.SectionEdit(op="split", section=s, at=a)
            for s, a in args.split
        ),
    ]
    try:
        if args.reset:
            doc = program_sections.reset(bundle, workflow, key=key)
        else:
            doc = program_sections.load(bundle, workflow)
        if args.recording:
            from openadapt_flow.narration import read_events

            recording = Path(args.recording)
            doc = program_sections.build(
                workflow,
                context=program_sections.context_for_workflow(
                    read_events(recording), workflow
                ),
                narration=program_sections.narration_by_step(recording),
                edits=doc.edits,
            )
            program_sections.write(bundle, doc, key=key)
        if edits:
            doc = program_sections.edit(bundle, workflow, edits, key=key)
    except program_sections.SectionsError as exc:
        raise SystemExit(f"REFUSED: {exc}") from exc
    if args.json:
        print(json.dumps(doc.model_dump(mode="json", by_alias=True), indent=2))
        return 0
    for section in doc.sections:
        span = (
            section.steps[0]
            if len(section.steps) == 1
            else f"{section.steps[0]}..{section.steps[-1]}"
        )
        print(f"{section.id:<24} {section.title}  [{section.kind}; {span}]")
    if doc.edits:
        print(
            f"{len(doc.edits)} review edit(s) stored in {program_sections.SECTIONS_FILE}"
        )
    return 0
