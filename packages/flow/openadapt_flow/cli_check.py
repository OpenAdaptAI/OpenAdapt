"""CLI: ``openadapt-flow check``, a read-back taught by demonstration.

- ``check mark``: say where the check starts in a recording and which fields
  it read. Writes ``check.json`` beside the recording; ``compile`` turns it
  into the save's read-back.
- ``check show``: list a bundle's demonstrated checks and whether a person
  has admitted each one.
- ``check admit``: a person admits the checks. It needs an interactive
  terminal and a typed confirmation, so neither a script nor the agent that
  drove the demonstration can admit them.

``record --with-check`` prints the prompt "Now show how you'd check it
saved." and, after the recording, asks where the check started and which
fields it read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.cli_hints import command
from openadapt_flow.compiler.readback_authoring import (
    CHECK_PROMPT,
    CheckError,
    DeclaredReset,
    MarkedField,
    admit_check,
    demonstrated_checks,
    describe_check,
    load_check,
    write_check,
)

#: What the person types to admit a check.
ADMIT_WORD = "admit"

RECORD_GUIDANCE = (
    f"{CHECK_PROMPT}\n"
    "Do the task and save. Then, in the same recording, show how an auditor "
    "would prove the save persisted:\n"
    "  1. throw away what the screen remembers: reload (F5), run a new "
    "search, or close and reopen the window;\n"
    "  2. open the saved record, or a read-only view of it;\n"
    "  3. stop recording with the saved values on screen.\n"
    "Flow then asks which fields show the values you typed."
)


def register_check_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "check",
        help=(
            "Teach a save's read-back by demonstration, and admit it. Nothing "
            "confirms a save until a person admits the check."
        ),
    )
    verbs = parser.add_subparsers(dest="check_cmd", required=True)

    mark = verbs.add_parser(
        "mark", help="Write check.json: where the check starts and what it reads"
    )
    mark.add_argument("recording", help="Recording directory")
    mark.add_argument(
        "--start-event",
        type=int,
        required=True,
        help="Index of the first recorded event of the check",
    )
    mark.add_argument(
        "--field",
        action="append",
        required=True,
        metavar="PARAM=X,Y,W,H",
        help="A field that shows the value typed for PARAM, in frame pixels",
    )
    mark.add_argument(
        "--kind",
        action="append",
        default=[],
        metavar="PARAM=KIND",
        help="Compare PARAM as text, code, date or currency (default: inferred)",
    )
    mark.add_argument(
        "--date-order",
        action="append",
        default=[],
        metavar="PARAM=dmy|mdy",
        help="Order of a numeric date field",
    )
    mark.add_argument(
        "--reset",
        default=None,
        metavar="KIND@EVENT",
        help=(
            "Name the state reset when Flow can't recognize it: reload, "
            "new_search, reopen_window or fresh_session, at a recorded event"
        ),
    )
    mark.set_defaults(func=cmd_check_mark)

    show = verbs.add_parser("show", help="List a bundle's demonstrated checks")
    show.add_argument("bundle", help="Bundle directory")
    show.add_argument("--json", action="store_true", dest="as_json")
    show.set_defaults(func=cmd_check_show)

    admit = verbs.add_parser(
        "admit",
        help="Admit a bundle's demonstrated checks (a person, at a terminal)",
    )
    admit.add_argument("bundle", help="Bundle directory")
    admit.add_argument("--step", default=None, help="Admit only this save step")
    admit.add_argument("--by", default=None, help="Your name (default: the login name)")
    admit.set_defaults(func=cmd_check_admit)

    accept = verbs.add_parser(
        "accept",
        help=(
            "Use the look-up found after the save (check.proposed.json) as the "
            "recording's check.json"
        ),
    )
    accept.add_argument("recording", help="Recording directory")
    accept.set_defaults(func=cmd_check_accept)


def _region(text: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"a region is X,Y,W,H, not {text!r}")
    return (parts[0], parts[1], parts[2], parts[3])


def _pairs(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ValueError(f"expected NAME=VALUE, got {item!r}")
        out[name] = value
    return out


def build_marks(
    fields: list[str],
    *,
    kinds: Optional[list[str]] = None,
    date_orders: Optional[list[str]] = None,
) -> list[MarkedField]:
    kind_of = _pairs(kinds or [])
    order_of = _pairs(date_orders or [])
    marks: list[MarkedField] = []
    for spec in fields:
        param, sep, region = spec.partition("=")
        if not sep or not param:
            raise ValueError(f"expected PARAM=X,Y,W,H, got {spec!r}")
        marks.append(
            MarkedField(
                param=param,
                region=_region(region),
                value_kind=kind_of.get(param),  # type: ignore[arg-type]
                date_order=order_of.get(param),  # type: ignore[arg-type]
            )
        )
    return marks


def parse_reset(text: Optional[str]) -> Optional[DeclaredReset]:
    if not text:
        return None
    kind, sep, event = text.partition("@")
    if not sep:
        raise ValueError(f"expected KIND@EVENT, got {text!r}")
    return DeclaredReset(kind=kind, event=int(event))  # type: ignore[arg-type]


def cmd_check_accept(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.lookup_detect import accept_check_proposal

    try:
        path = accept_check_proposal(args.recording)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"check accept: {exc}", file=sys.stderr)
        return 2
    marks = load_check(args.recording)
    assert marks is not None  # just written
    print(f"Wrote {path}: the check starts at event {marks.start_event} and reads")
    for field in marks.fields:
        print(f"  {field.param} at {field.region}")
    print("After compile, a person runs 'check admit'.")
    return 0


def cmd_check_mark(args: argparse.Namespace) -> int:
    try:
        marks = build_marks(args.field, kinds=args.kind, date_orders=args.date_order)
        reset = parse_reset(args.reset)
        path = write_check(
            Path(args.recording),
            start_event=args.start_event,
            fields=marks,
            reset=reset,
            prompt=CHECK_PROMPT,
        )
    except (ValueError, OSError) as exc:
        print(f"check mark: {exc}", file=sys.stderr)
        return 2
    admit = command("check admit")
    print(f"Wrote {path}. Compile the recording; a person then runs '{admit}'.")
    return 0


def _load(bundle: str):
    from openadapt_flow.ir import Workflow

    return Workflow.load(Path(bundle))


def cmd_check_show(args: argparse.Namespace) -> int:
    workflow = _load(args.bundle)
    found = demonstrated_checks(workflow)
    rows = []
    for step, effect in found:
        spec = effect.readback
        assert spec is not None
        rows.append(
            {
                "step": step.id,
                "reset": None if spec.reset is None else spec.reset.kind,
                "reset_evidence": None if spec.reset is None else spec.reset.evidence,
                "fields": [f.name for f in spec.fields],
                "admitted": spec.admitted(),
                "admitted_by": (spec.admission.admitted_by if spec.admission else None),
            }
        )
    if args.as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("No demonstrated check in this bundle.")
        return 0
    for (step, effect), row in zip(found, rows):
        print(describe_check(step, effect))
        state = (
            f"admitted by {row['admitted_by']}" if row["admitted"] else "NOT admitted"
        )
        print(f"  -> {state}\n")
    return 0


def cmd_check_admit(
    args: argparse.Namespace,
    *,
    ask: Callable[[str], str] = input,
    interactive: Optional[Callable[[], bool]] = None,
) -> int:
    is_tty = interactive or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    if not is_tty():
        print(
            "check admit: a person admits a check at an interactive terminal; "
            "it can't be admitted by a script or by the demonstration",
            file=sys.stderr,
        )
        return 2
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    workflow = Workflow.load(bundle)
    found = [
        (step, effect)
        for step, effect in demonstrated_checks(workflow)
        if args.step is None or step.id == args.step
    ]
    if not found:
        print("check admit: no demonstrated check to admit", file=sys.stderr)
        return 2
    for step, effect in found:
        print(describe_check(step, effect))
        print()
    answer = ask(
        f"Type '{ADMIT_WORD}' to accept these checks as proof that the save "
        "persisted (screen read-back, same session): "
    )
    if answer.strip().lower() != ADMIT_WORD:
        print("Not admitted. Nothing changed.")
        return 1
    try:
        updated, steps = admit_check(workflow, admitted_by=args.by, step_id=args.step)
    except CheckError as exc:
        print(f"check admit: {exc}", file=sys.stderr)
        return 2
    updated.save(bundle)
    print(f"Admitted the check on {', '.join(steps)}.")
    return 0


def _summarize_event(index: int, event: dict) -> str:
    kind = event.get("kind", "?")
    if kind == "type":
        what = f"type {event.get('param') or repr(event.get('text', ''))}"
    elif kind in ("key", "hotkey"):
        mods = "+".join([*event.get("modifiers", []), str(event.get("key", ""))])
        what = f"{kind} {mods}"
    elif "x" in event:
        what = f"{kind} at ({event.get('x')}, {event.get('y')})"
    else:
        what = str(kind)
    return f"  {index:3d}  {what}"


def guided_check(
    recording: Path,
    *,
    ask: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
) -> Optional[Path]:
    """After ``record --with-check``: ask where the check started and what it read.

    Returns the written ``check.json``, or None when the person skipped it.
    """
    lines = (recording / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    echo(CHECK_PROMPT)
    echo("Recorded events:")
    for index, event in enumerate(events):
        echo(_summarize_event(index, event))
    start = ask("Index of the first event of the check (empty to skip): ").strip()
    if not start:
        echo(f"No check marked. Mark it later with '{command('check mark')}'.")
        return None
    fields: list[str] = []
    while True:
        field = ask(
            "A field that shows a value you typed, PARAM=X,Y,W,H in frame "
            "pixels (empty when done): "
        ).strip()
        if not field:
            break
        fields.append(field)
    if not fields:
        echo("No field marked, so there is nothing to check.")
        return None
    reset = ask(
        "If Flow can't see your reset (a click on reload, a sign-in), name it "
        "as KIND@EVENT (empty to let Flow find it): "
    ).strip()
    path = write_check(
        recording,
        start_event=int(start),
        fields=build_marks(fields),
        reset=parse_reset(reset or None),
        prompt=CHECK_PROMPT,
    )
    echo(f"Wrote {path}. After compile, a person runs '{command('check admit')}'.")
    return path
