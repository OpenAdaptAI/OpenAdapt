"""Run ExtraDup cells. Gold for a mutant is FAIL."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from openadapt_evals.extradup.checkers import (
    content_only_check,
    field_inclusion_check,
    identity_check,
    sor_check,
    visual_only_check,
)
from openadapt_evals.extradup.gold import GOLD_SPECS, WriteSpec
from openadapt_evals.extradup.mutations import (
    ALL_OPERATORS,
    EVAL_ONLY_OPERATORS,
    MUTANTS,
    OPERATORS,
    apply,
)
from openadapt_evals.extradup.seal import REFUSED, VERIFIED, seal_verdict
from openadapt_evals.extradup.store import store_for

SUITE_NAME = "ExtraDup"
SUITE_LABEL = "this trajectory is wrong; does your checker notice?"


@dataclass(frozen=True)
class CellReport:
    id: str
    env: str
    operator: str
    gold: str
    sor: str
    sor_reasons: tuple[str, ...]
    field_inclusion: str
    content_only: str
    identity: str
    visual_only: str
    seal: str
    new_count: int
    spec_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gold_for(operator: str) -> str:
    return "PASS" if operator == "control" else "FAIL"


def cell_id(spec: WriteSpec, operator: str) -> str:
    env = "mockmed" if spec.env == "mockmed" else "openemr"
    return f"{env}:{operator}"


def cells() -> dict[str, tuple[WriteSpec, str]]:
    """The frozen corpus: ``OPERATORS`` only.

    ``kill_scan`` digests this, and the Phase-1 M-freeze pins the operator
    list, so an eval-only family must not appear here. Use ``all_cells``.
    """
    found: dict[str, tuple[WriteSpec, str]] = {}
    for spec in GOLD_SPECS:
        for operator in OPERATORS:
            found[cell_id(spec, operator)] = (spec, operator)
    return found


def eval_only_cells() -> dict[str, tuple[WriteSpec, str]]:
    """Families added after the freeze. Suite and eval dataset, never training."""
    return {
        cell_id(spec, operator): (spec, operator)
        for spec in GOLD_SPECS
        for operator in EVAL_ONLY_OPERATORS
    }


def all_cells() -> dict[str, tuple[WriteSpec, str]]:
    return {**cells(), **eval_only_cells()}


def run_cell(cell: str) -> CellReport:
    spec, operator = all_cells()[cell]
    store = store_for(spec.env)
    before, after, screen = apply(store, spec, operator)
    added = [row for row in after if row["id"] not in {r["id"] for r in before}]
    sor = sor_check(spec, before, after)
    inclusion = field_inclusion_check(spec, after)
    content = content_only_check(spec, after)
    identity = identity_check(spec, before, after)
    visual = visual_only_check(screen)
    return CellReport(
        id=cell,
        env=spec.env,
        operator=operator,
        gold=_gold_for(operator),
        sor=sor.verdict,
        sor_reasons=sor.reasons,
        field_inclusion=inclusion.verdict,
        content_only=content.verdict,
        identity=identity.verdict,
        visual_only=visual.verdict,
        seal=seal_verdict(spec, before, after),
        new_count=len(added),
        spec_count=spec.expected_new,
    )


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def cmd_list(_args: argparse.Namespace) -> int:
    print(f"{SUITE_NAME}: {SUITE_LABEL}")
    for cell, (spec, operator) in all_cells().items():
        gold = _gold_for(operator)
        print(f"{cell}\t{spec.env}\t{operator}\tgold={gold}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    report = run_cell(args.id)
    print(_dump(report.as_dict()))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    catalog = all_cells()
    if args.id not in catalog:
        raise SystemExit(f"unknown mutant {args.id!r}")
    report = run_cell(args.id)
    print(_dump(report.as_dict()))
    if report.gold == "FAIL" and report.sor != "FAIL":
        print(f"MISS: {report.id} is a wrong-effect mutant; the SoR oracle returned PASS.")
        return 1
    if report.gold == "PASS" and report.sor != "PASS":
        print(f"FALSE FAIL: {report.id} is the gold write; the SoR oracle returned FAIL.")
        return 1
    print(f"HIT: {report.id} SoR={report.sor} gold={report.gold} seal={report.seal}")
    return 0


def check_invariants(reports: Optional[dict[str, CellReport]] = None) -> list[str]:
    """Return problems. Empty means the kit holds."""
    catalog = all_cells()
    reports = reports or {cell: run_cell(cell) for cell in catalog}
    problems: list[str] = []

    expected_ids = {
        f"{env}:{op}"
        for env in ("mockmed", "openemr")
        for op in ALL_OPERATORS
    }
    if set(reports) != expected_ids:
        problems.append(f"cell set drifted: {sorted(reports)}")

    for cell, report in reports.items():
        if report.gold == "FAIL" and report.sor != "FAIL":
            problems.append(f"{cell}: mutant gold is FAIL but SoR={report.sor}")
        if report.gold == "PASS" and report.sor != "PASS":
            problems.append(f"{cell}: control gold is PASS but SoR={report.sor}")
        if report.gold == "PASS" and report.seal != VERIFIED:
            problems.append(f"{cell}: gold write must be VERIFIED, seal={report.seal}")
        if report.gold == "FAIL" and report.seal == VERIFIED:
            problems.append(f"{cell}: mutant must not be VERIFIED")

    for env in ("mockmed", "openemr"):
        dup = reports[f"{env}:dup"]
        extra = reports[f"{env}:extra"]
        if dup.field_inclusion != "PASS" or dup.visual_only != "PASS":
            problems.append(
                f"{env}:dup: field-inclusion/visual must PASS "
                f"(got inclusion={dup.field_inclusion} visual={dup.visual_only})"
            )
        if dup.new_count == dup.spec_count:
            problems.append(f"{env}:dup: |new| must differ from |spec|")
        if extra.field_inclusion != "PASS" or extra.visual_only != "PASS":
            problems.append(
                f"{env}:extra: field-inclusion/visual must PASS "
                f"(got inclusion={extra.field_inclusion} visual={extra.visual_only})"
            )
        if extra.sor != "FAIL":
            problems.append(f"{env}:extra: SoR must FAIL an extra field")
        extra_ni = reports[f"{env}:dup"]
        if extra_ni.seal != REFUSED:
            problems.append(f"{env}:dup: Seal must REFUSE Extra-NI, got {extra_ni.seal}")

    for env in ("mockmed", "openemr"):
        wrong = reports[f"{env}:wrong_record"]
        # The whole point: gold cardinality, gold screen, gold content.
        if wrong.new_count != wrong.spec_count:
            problems.append(
                f"{env}:wrong_record: |new| must equal |spec| "
                f"(got {wrong.new_count} != {wrong.spec_count})"
            )
        if wrong.visual_only != "PASS":
            problems.append(f"{env}:wrong_record: the screen must look like success")
        if wrong.content_only != "PASS":
            problems.append(
                f"{env}:wrong_record: a content check must PASS; the content is correct"
            )
        if wrong.identity != "FAIL":
            problems.append(
                f"{env}:wrong_record: identity resolution must FAIL, got {wrong.identity}"
            )
        if wrong.sor != "FAIL" or wrong.seal != REFUSED:
            problems.append(
                f"{env}:wrong_record: SoR must FAIL and the Seal must REFUSE "
                f"(got sor={wrong.sor} seal={wrong.seal})"
            )
        control = reports[f"{env}:control"]
        if control.identity != "PASS":
            problems.append(f"{env}:control: identity resolution must PASS")

    mock_dup = reports["mockmed:dup"]
    if mock_dup.seal == VERIFIED:
        problems.append("mockmed:dup: Seal/VERIFIED path must refuse Extra-NI")

    if not MUTANTS:
        problems.append("no ExtraDup mutants")
    return problems


def cmd_check(_args: argparse.Namespace) -> int:
    reports = {cell: run_cell(cell) for cell in all_cells()}
    problems = check_invariants(reports)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print(
        "PASS: ExtraDup SoR fails every mutant; field-inclusion still PASSes "
        "dup/extra; a content check still PASSes wrong_record; Seal refuses "
        "Extra-NI on MockMed"
    )
    return 0


def cmd_kill_scan(args: argparse.Namespace) -> int:
    from openadapt_evals.extradup.kill_scan import cmd_kill_scan as _run

    return _run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m openadapt_evals.extradup",
        description=f"{SUITE_NAME}: {SUITE_LABEL}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list cell ids").set_defaults(func=cmd_list)
    show = sub.add_parser("show", help="run one cell and print the report")
    show.add_argument("id")
    show.set_defaults(func=cmd_show)
    run = sub.add_parser("run", help="score one cell; exit 1 if SoR misses a mutant")
    run.add_argument("id")
    run.set_defaults(func=cmd_run)
    sub.add_parser("check", help="verify ExtraDup invariants").set_defaults(
        func=cmd_check
    )
    scan = sub.add_parser(
        "kill-scan",
        help="run the frozen ExtraDup kill-scan; print SIS vs honest-write",
    )
    scan.add_argument(
        "--target",
        choices=("both", "visual_only", "certified_sor"),
        default="both",
        help="built-in rewards (ignored when --verdicts is set)",
    )
    scan.add_argument(
        "--verdicts",
        type=Path,
        help="JSON paid/not-paid map from someone else's checker or agent",
    )
    scan.add_argument("--json", type=Path, help="write the summary object")
    scan.add_argument(
        "--dump-corpus",
        type=Path,
        help="write the frozen cells (gold, store snapshots, screen)",
    )
    scan.set_defaults(func=cmd_kill_scan)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
