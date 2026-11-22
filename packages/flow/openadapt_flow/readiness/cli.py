"""CLI: ``openadapt-flow readiness`` -- shadow a candidate on tasks a person
did, stopped before every save, and score it. See ``docs/READINESS.md``.

    openadapt-flow readiness import --from human-tasks --out episodes
    openadapt-flow readiness run --episodes episodes --out runs \\
        --app-url https://sandbox.example.test/ --agent-cmd "python my_agent.py" \\
        --reset-cmd "./reset-sandbox.sh"
    openadapt-flow readiness compare --episodes episodes --runs runs --out cards
    openadapt-flow readiness report --cases cards/cases.json --out cards \\
        --hourly-cost 38
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

__all__ = ["cmd_readiness", "register"]


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "readiness",
        help="Run a candidate agent or program on tasks a person did, stopped "
        "before every save, and write a one-page Readiness Scorecard",
        allow_abbrev=False,
    )
    verbs = p.add_subparsers(dest="readiness_cmd", required=True)

    imp = verbs.add_parser(
        "import",
        help="Turn recorded human tasks into reference episodes",
        allow_abbrev=False,
    )
    imp.add_argument("--from", dest="source", required=True, help="one folder per task")
    imp.add_argument("--out", required=True, help="the episodes folder")
    imp.add_argument("--app-url", default=None, help="only saves to this origin count")
    _lens_flags(imp)
    imp.add_argument(
        "--har",
        action="append",
        default=[],
        help="induce a lens from the person's HAR traces (repeatable)",
    )

    run = verbs.add_parser(
        "run",
        help="Run the candidate on every episode, stopped before save",
        allow_abbrev=False,
    )
    run.add_argument("--episodes", required=True)
    run.add_argument("--out", required=True, help="the runs folder")
    run.add_argument("--app-url", required=True, help="where each task's tab opens")
    who = run.add_mutually_exclusive_group(required=True)
    who.add_argument("--agent-cmd", help="the agent command, run once per task")
    who.add_argument("--program", help="a compiled program bundle")
    run.add_argument(
        "--label", default=None, help="the candidate's name on the scorecard"
    )
    run.add_argument("--mode", choices=("sandbox", "run-ahead"), default="sandbox")
    run.add_argument(
        "--not-save",
        action="append",
        default=[],
        metavar="METHOD:PATH",
        help="a write that is let through (a sign-in form), fnmatch path (repeatable)",
    )
    run.add_argument(
        "--screen-selector", default=None, help="CSS selector of the record header"
    )
    run.add_argument("--timeout", type=float, default=300.0, help="seconds per task")
    run.add_argument("--linger", type=float, default=1.5, help="seconds after the hold")
    run.add_argument(
        "--reset-cmd", default=None, help="puts the sandbox back before each task"
    )
    run.add_argument(
        "--verify", default=None, help="verify.yaml: identity through GuardedSave.begin"
    )
    run.add_argument(
        "--storage-state",
        default=None,
        help="a Playwright storage state (cookies, local storage) loaded into each task",
    )
    run.add_argument(
        "--egress",
        choices=("scrubbed", "blocked", "unscrubbed"),
        default="scrubbed",
        help="the candidate's model traffic: scrubbed through the flight proxy "
        "(default; refuses if it can't start), blocked (no model calls at all), "
        "or unscrubbed (synthetic data only; recorded on the scorecard)",
    )
    run.add_argument(
        "--spend-cap",
        type=float,
        default=None,
        help="USD of model calls per task; the proxy answers 429 past it",
    )
    run.add_argument(
        "--upstream",
        action="append",
        default=[],
        metavar="PROVIDER=URL",
        help="where the proxy forwards a provider's calls (anthropic, openai; repeatable)",
    )
    run.add_argument(
        "--browser-allow",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="an origin the browser may reach besides the app's (a sign-in "
        "host, a CDN); every other origin is unreachable (repeatable)",
    )
    run.add_argument(
        "--keep-session",
        action="store_true",
        help="carry the signed-in session from each task to the next",
    )
    _lens_flags(run)

    cmp_ = verbs.add_parser(
        "compare",
        help="Compare each proposed write with the person's save",
        allow_abbrev=False,
    )
    cmp_.add_argument("--episodes", required=True)
    cmp_.add_argument("--runs", required=True)
    cmp_.add_argument(
        "--out", required=True, help="cases.json goes here (values, 0600)"
    )
    cmp_.add_argument(
        "--ignore-field",
        action="append",
        default=[],
        help="a field the agent may send that the person did not (repeatable)",
    )
    _lens_flags(cmp_)

    rep = verbs.add_parser(
        "report", help="Write scorecard.json and scorecard.html", allow_abbrev=False
    )
    rep.add_argument("--cases", required=True, help="cases.json from compare")
    rep.add_argument("--out", required=True)
    rep.add_argument(
        "--hourly-cost",
        type=float,
        default=None,
        help="USD per hour of the person's time",
    )
    rep.add_argument("--label", default=None)
    rep.add_argument(
        "--task-type",
        default=None,
        help="the kind of task, a lowercase identifier (default: unspecified)",
    )
    p.set_defaults(func=cmd_readiness)


def _lens_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lens", default=None, help="an openadapt.entity-lens/v1 JSON file")


def _load_lens(path: Optional[str]) -> Any:
    if not path:
        return None
    from openadapt_flow.tx.types import EntityLensSpec

    return EntityLensSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _not_saves(values: list[str]) -> list[Any]:
    from openadapt_flow.agent_guard.requests import NotASave

    rules = []
    for value in values:
        method, sep, path = value.partition(":")
        if not sep or not method or not path.startswith("/"):
            raise SystemExit(
                f"readiness: --not-save must be METHOD:/path, got {value!r}"
            )
        rules.append(NotASave(method, path))
    return rules


def _write_private(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def cmd_readiness(args: argparse.Namespace) -> int:
    verb = args.readiness_cmd
    if verb == "import":
        return _cmd_import(args)
    if verb == "run":
        return _cmd_run(args)
    if verb == "compare":
        return _cmd_compare(args)
    return _cmd_report(args)


def _cmd_import(args: argparse.Namespace) -> int:
    from openadapt_flow.readiness.reference import import_episodes

    lens = _load_lens(args.lens)
    out = Path(args.out)
    if args.har and lens is None:
        from openadapt_flow.tx.induce import induce_lens_from_har

        har: dict[str, Any] = {"log": {"entries": []}}
        for path in args.har:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            har["log"]["entries"] += list((doc.get("log") or {}).get("entries") or [])
        lens = induce_lens_from_har(har)
        out.mkdir(parents=True, exist_ok=True)
        (out / "lens.json").write_text(lens.model_dump_json(indent=2), encoding="utf-8")
    result = import_episodes(args.source, out, app_url=args.app_url, lens=lens)
    print(f"readiness import: {len(result.imported)} episode(s) in {out}")
    for item in result.refused:
        print(
            f"  refused {item['folder']}: {item['code']} ({item['why']})",
            file=sys.stderr,
        )
    return 0 if result.imported else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from openadapt_flow.readiness.egress import EgressRefused
    from openadapt_flow.readiness.launcher import (
        EgressOptions,
        ProgramLauncher,
        SubprocessLauncher,
    )
    from openadapt_flow.readiness.runner import RunnerConfig, run_episodes

    upstreams: dict[str, str] = {}
    for value in args.upstream:
        name, sep, url = value.partition("=")
        if not sep or name not in ("anthropic", "openai") or not url.startswith("http"):
            raise SystemExit(
                f"readiness: --upstream must be anthropic=URL or openai=URL, got {value!r}"
            )
        upstreams[name] = url
    egress = EgressOptions(
        mode=args.egress, spend_cap_usd=args.spend_cap, upstreams=upstreams
    )
    launcher: Any = (
        SubprocessLauncher(args.agent_cmd, label=args.label, egress=egress)
        if args.agent_cmd
        else ProgramLauncher(args.program, label=args.label, egress=egress)
    )
    cfg = RunnerConfig(
        app_url=args.app_url,
        mode=args.mode,
        not_saves=_not_saves(args.not_save),
        screen_selector=args.screen_selector,
        timeout_s=args.timeout,
        linger_s=args.linger,
        reset_cmd=shlex.split(args.reset_cmd) if args.reset_cmd else None,
        verify=Path(args.verify) if args.verify else None,
        lens=_load_lens(args.lens),
        storage_state=Path(args.storage_state) if args.storage_state else None,
        keep_session=args.keep_session,
        allow_origins=tuple(args.browser_allow),
    )
    try:
        manifest = run_episodes(args.episodes, args.out, launcher, cfg)
    except EgressRefused as exc:
        print(f"readiness run: refused, {exc}", file=sys.stderr)
        return 2
    refused = (manifest.get("egress") or {}).get("refused_tasks") or 0
    print(
        f"readiness run: {len(manifest['tasks'])} task(s), "
        f"{manifest['landed_writes']} write(s) reached the app, "
        f"{refused} refused for egress (egress: {args.egress})"
    )
    return 0 if manifest["landed_writes"] == 0 and not refused else 2


def _cmd_compare(args: argparse.Namespace) -> int:
    from openadapt_flow.readiness.compare import compare_runs, write_case_files

    doc = compare_runs(
        args.episodes,
        args.runs,
        lens=_load_lens(args.lens),
        ignore_fields=args.ignore_field,
    )
    out = Path(args.out) / "cases.json"
    _write_private(out, doc)
    write_case_files(doc, args.runs)
    counts: dict[str, int] = {}
    for case in doc["cases"]:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    print(f"readiness compare: {json.dumps(counts, sort_keys=True)} -> {out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from openadapt_flow.readiness.scorecard import check_task_type, write_scorecard

    try:
        task_type = check_task_type(args.task_type)
    except ValueError as exc:
        raise SystemExit(f"readiness report: {exc}") from None
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    card = write_scorecard(
        cases,
        args.out,
        hourly_cost=args.hourly_cost,
        agent_label=args.label,
        task_type=task_type,
    )
    print(
        f"readiness report: matched {card['matched']} of {card['cases_total']}; "
        f"{Path(args.out) / 'scorecard.html'}"
    )
    return 0 if card["scope_ok"] else 2
