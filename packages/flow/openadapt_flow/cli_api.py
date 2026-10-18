"""CLI: ``openadapt-flow api``, API steps compiled from network capture.

- ``api show``: list a bundle's proposed API steps, what each sends and
  whether a person has admitted it.
- ``api admit``: a person admits them. It needs an interactive terminal and
  a typed confirmation, so neither a script nor the agent that drove the
  recording can admit one.

``record --capture-network`` keeps the page's requests; ``compile`` proposes
the API step (see ``docs/API_TIER.md``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.compiler.api_proposal import (
    ApiProposalError,
    admit_api_step,
    describe_api_step,
    proposed_api_steps,
)

#: What the person types to admit an API step.
ADMIT_WORD = "admit"


def register_api_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "api",
        help=(
            "Show or admit API steps compiled from a recording's network "
            "capture. None runs until a person admits it."
        ),
    )
    verbs = parser.add_subparsers(dest="api_cmd", required=True)
    show = verbs.add_parser("show", help="List a bundle's proposed API steps")
    show.add_argument("bundle", help="Bundle directory")
    show.add_argument("--json", action="store_true", dest="as_json")
    show.set_defaults(func=cmd_api_show)
    propose = verbs.add_parser(
        "propose",
        help=(
            "Propose the API step again from the recording (after editing the "
            "program's checks); drops any earlier admission"
        ),
    )
    propose.add_argument("bundle", help="Bundle directory")
    propose.add_argument(
        "--recording", required=True, help="The recording made with --capture-network"
    )
    propose.set_defaults(func=cmd_api_propose)
    admit = verbs.add_parser(
        "admit", help="Admit a bundle's API steps (a person, at a terminal)"
    )
    admit.add_argument("bundle", help="Bundle directory")
    admit.add_argument("--step", default=None, help="Admit only this save step")
    admit.add_argument("--by", default=None, help="Your name (default: the login name)")
    admit.set_defaults(func=cmd_api_admit)


def cmd_api_show(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    found = proposed_api_steps(Workflow.load(bundle))
    reasons: list[str] = []
    sidecar = bundle / "api_proposal.json"
    if sidecar.is_file():
        reasons = json.loads(sidecar.read_text()).get("reasons") or []
    if args.as_json:
        rows = [
            {
                "step": s.id,
                "method": s.api_binding.method,
                "path": s.api_binding.url_template,
                "replaces_steps": s.api_binding.web_form.replaces_steps,
                "admitted": s.api_binding.admitted(),
                "admitted_by": s.api_binding.admission.admitted_by
                if s.api_binding.admission
                else None,
            }
            for s in found
        ]
        print(
            json.dumps(
                {"api_steps": rows, "not_proposed_because": reasons},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not found:
        print(
            "No API step in this bundle."
            + (f" Not proposed: {'; '.join(reasons)}." if reasons else "")
        )
        return 0
    for step in found:
        print(describe_api_step(step))
        binding = step.api_binding
        state = (
            f"admitted by {binding.admission.admitted_by}"
            if binding.admitted()
            else "NOT admitted"
        )
        print(f"  -> {state}\n")
    return 0


def cmd_api_propose(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.api_proposal import propose_api_step
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    workflow, step, reasons = propose_api_step(
        Workflow.load(bundle), Path(args.recording), replace=True
    )
    (bundle / "api_proposal.json").write_text(
        json.dumps(
            {
                "proposed": step is not None,
                "step": None if step is None else step.id,
                "reasons": reasons,
            },
            indent=2,
        )
        + "\n"
    )
    if step is None:
        print(f"No API step proposed: {'; '.join(reasons)}.")
        return 1
    workflow.save(bundle)
    print(describe_api_step(step))
    print("\nNot admitted yet: a person runs 'openadapt-flow api admit'.")
    return 0


def cmd_api_admit(
    args: argparse.Namespace,
    *,
    ask: Callable[[str], str] = input,
    interactive: Optional[Callable[[], bool]] = None,
) -> int:
    is_tty = interactive or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    if not is_tty():
        print(
            "api admit: a person admits an API step at an interactive terminal; "
            "it can't be admitted by a script or by the recording",
            file=sys.stderr,
        )
        return 2
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    workflow = Workflow.load(bundle)
    found = [
        s
        for s in proposed_api_steps(workflow)
        if args.step is None or s.id == args.step
    ]
    if not found:
        print("api admit: no proposed API step to admit", file=sys.stderr)
        return 2
    for step in found:
        print(describe_api_step(step))
        print()
    answer = ask(
        f"Type '{ADMIT_WORD}' to let these requests save in place of the GUI steps "
        "(the record check still confirms every save): "
    )
    if answer.strip().lower() != ADMIT_WORD:
        print("Not admitted. Nothing changed.")
        return 1
    try:
        updated, steps = admit_api_step(
            workflow, admitted_by=args.by, step_id=args.step
        )
    except ApiProposalError as exc:
        print(f"api admit: {exc}", file=sys.stderr)
        return 2
    updated.save(bundle)
    print(f"Admitted the API step on {', '.join(steps)}.")
    return 0
