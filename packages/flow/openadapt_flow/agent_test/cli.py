"""``openadapt test``: run an agent against stateful scenarios with planted faults.

    openadapt test list
    openadapt test up --app APP
    openadapt test run --app APP --agent scripted-readback --out out/
    openadapt test baseline --results out/results.json --out baseline.json
    openadapt test run --app APP --agent claude-cu --baseline baseline.json --out out2/
    openadapt test down --app APP

``run`` writes ``results.json``, ``report.html`` and ``receipt.json`` to
``--out``. With ``--baseline`` it also gates: exit 1 when silent corruption
or false claims of success rise above the baseline by more than
``--tolerance``, exit 2 when the run is not comparable with the baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from openadapt_flow.agent_test import gate as gate_mod
from openadapt_flow.agent_test import packs, report
from openadapt_flow.agent_test.store import Docker, wait_http


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--app",
        required=True,
        help="a built-in app (see `openadapt test list`) or an app pack .yaml",
    )
    p.add_argument(
        "--port",
        type=int,
        default=9870,
        help="the app's port; the front door listens on the next one (default 9870)",
    )
    p.add_argument(
        "--app-host",
        default="127.0.0.1",
        help="where the app's port is reachable from here (default 127.0.0.1)",
    )
    p.add_argument(
        "--docker",
        default=os.environ.get("OPENADAPT_TEST_DOCKER", "docker"),
        help="the docker command, e.g. one that runs docker on another host (default docker)",
    )
    p.add_argument(
        "--project",
        default="",
        help="the Compose project name (default oatest-<app>-<port>)",
    )


def _project(a: argparse.Namespace, app: packs.AppPack) -> str:
    return a.project or f"oatest-{app.name}-{a.port}"


def cmd_list(_: argparse.Namespace) -> int:
    for name in packs.builtin_apps():
        app = packs.load_app(name)
        try:
            sp = packs.load_scenarios(name, app)
            kinds = sorted({s.kind for s in sp.scenarios})
            n = len(sp.scenarios)
        except packs.PackError:
            kinds, n = [], 0
        print(
            f"{name:10s} {app.title:14s} {n:3d} scenarios  faults: {', '.join(kinds)}"
        )
        print(f"{'':10s} licence: {app.licence}")
    return 0


def cmd_up(a: argparse.Namespace) -> int:
    app = packs.load_app(a.app)
    project = _project(a, app)
    d = Docker(a.docker, project)
    bind = a.bind or (
        "127.0.0.1" if a.app_host in ("127.0.0.1", "localhost") else "0.0.0.0"
    )
    compose = packs.compose_yaml(app, project, a.port, a.port + 1, bind)
    print(
        f"starting {app.name} as {project} on port {a.port} (images: {', '.join(app.images())})",
        flush=True,
    )
    d.run(
        "compose",
        "-p",
        project,
        "-f",
        "-",
        "up",
        "-d",
        "--wait",
        input=compose,
        timeout=float(app.stack.get("ready_timeout_s", 900)) + 600,
    )
    wait_http(
        f"http://{a.app_host}:{a.port}{app.stack.get('ready_path', '/')}",
        float(app.stack.get("ready_timeout_s", 900)),
    )
    print(f"{app.name} is up at http://{a.app_host}:{a.port}", flush=True)
    return 0


def cmd_down(a: argparse.Namespace) -> int:
    app = packs.load_app(a.app)
    project = _project(a, app)
    d = Docker(a.docker, project)
    compose = packs.compose_yaml(app, project, a.port, a.port + 1, "127.0.0.1")
    d.run("compose", "-p", project, "-f", "-", "down", "-v", input=compose, timeout=600)
    print(f"stopped and removed {project}", flush=True)
    return 0


def _gate_and_write(
    out: Path,
    baseline: Optional[Path],
    tolerance: float,
    sign_key: Optional[Path],
    allow_pack_change: bool = False,
    min_scored: int = 1,
) -> int:
    doc = json.loads((out / "results.json").read_text())
    g = None
    if baseline is not None:
        g = gate_mod.compare(
            out / "results.json",
            baseline,
            tolerance=tolerance,
            allow_pack_change=allow_pack_change,
            min_scored=min_scored,
        )
        (out / "gate.json").write_text(json.dumps(g, indent=1) + "\n")
    paths = report.write_outputs(out, doc, g, sign_key)
    s = doc["summary"]
    print(
        f"\n{doc['meta']['agent']['id']} on {doc['meta']['app']}: {s['runs']} runs, {s['scored']} scored, {int(s.get('model_calls') or 0)} model calls"
    )
    for name in (
        "completed_correctly",
        "silent_corruption",
        "collateral_edit",
        "false_success",
        "safe_stop",
    ):
        r = s["rates"][name]
        print(
            f"  {report.LABELS[name]:26s} {r['rate'] * 100:5.1f}%  ({r['count']}/{r['of']})"
        )
    for k, p in paths.items():
        print(f"  {k}: {p}")
    if g is not None:
        print(f"\ngate: {g['verdict'].upper()}")
        for line in g["lines"]:
            print(f"  {line}")
        _summary(out, doc, g)
        return {"pass": 0, "fail": 1}.get(g["verdict"], 2)
    _summary(out, doc, None)
    return 0


def _summary(out: Path, doc: dict, g: Optional[dict]) -> None:
    """A Markdown summary for CI (``$GITHUB_STEP_SUMMARY`` when set)."""

    s = doc["summary"]
    lines = [
        f"### Agent test: `{doc['meta']['agent']['id']}` on {doc['meta']['app']}",
        "",
    ]
    if g is not None:
        lines += [
            f"**Gate: {g['verdict'].upper()}** against baseline `{g['baseline_agent']['id']}`",
            "",
        ]
        lines += [f"- {x}" for x in g["lines"]] + [""]
    lines += ["| Measure | Rate | Count |", "| --- | ---: | ---: |"]
    for name in (
        "completed_correctly",
        "silent_corruption",
        "collateral_edit",
        "false_success",
        "safe_stop",
    ):
        r = s["rates"][name]
        lines.append(
            f"| {report.LABELS[name]} | {r['rate'] * 100:.1f}% | {r['count']}/{r['of']} |"
        )
    lines += [
        "",
        f"{s['runs']} runs, {s['scored']} scored, {int(s.get('model_calls') or 0)} model calls.",
    ]
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as fh:
            fh.write(text)


def cmd_run(a: argparse.Namespace) -> int:
    from openadapt_flow.agent_test.agents import make_agent
    from openadapt_flow.agent_test.runner import RunConfig, run

    app = packs.load_app(a.app_pack or a.app)
    pack = packs.load_scenarios(a.scenarios or app.name, app)
    if not a.agent and not a.agent_cmd:
        raise SystemExit("pass --agent or --agent-cmd")
    agent = make_agent(
        a.agent or "", model=a.model, price=a.price, agent_cmd=a.agent_cmd
    )
    if (
        agent.paid
        and agent.kind in ("claude-cu", "claude-code-mcp")
        and not os.environ.get("ANTHROPIC_API_KEY")
    ):
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    if (
        agent.paid
        and agent.kind == "openai-cu"
        and not os.environ.get("OPENAI_API_KEY")
    ):
        raise SystemExit("OPENAI_API_KEY is not set")
    out = Path(a.out or f"openadapt-test-{app.name}")
    if a.up:
        cmd_up(a)
    instructions = Path(a.instructions).read_text() if a.instructions else ""
    total = os.environ.get("OPENADAPT_TEST_TOTAL_CAP_USD")
    cfg = RunConfig(
        app=app,
        pack=pack,
        agent=agent,
        out=out,
        docker=Docker(a.docker, _project(a, app)),
        app_host=a.app_host,
        app_port=a.port,
        proxy_port=a.port + 1,
        repetitions=a.repetitions,
        only=tuple(x for x in (a.only or "").split(",") if x),
        spend_cap_usd=a.spend_cap_usd,
        total_cap_usd=float(total) if total else None,
        max_load=a.max_load,
        instructions=instructions,
    )
    try:
        run(cfg)
    finally:
        if a.down_after:
            cmd_down(a)
    return _gate_and_write(
        out,
        Path(a.baseline) if a.baseline else None,
        a.tolerance,
        Path(a.sign_key) if a.sign_key else None,
        a.allow_pack_change,
        getattr(a, "min_scored", 1),
    )


def cmd_gate(a: argparse.Namespace) -> int:
    out = Path(a.results).parent
    return _gate_and_write(
        out,
        Path(a.baseline),
        a.tolerance,
        Path(a.sign_key) if a.sign_key else None,
        a.allow_pack_change,
        getattr(a, "min_scored", 1),
    )


def cmd_baseline(a: argparse.Namespace) -> int:
    b = gate_mod.make_baseline(Path(a.results))
    Path(a.out).write_text(json.dumps(b, indent=1) + "\n")
    print(f"baseline for {b['agent']['id']} on {b['app']} written to {a.out}")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from openadapt_flow.agent_test.runner import collect

    out = Path(a.out)
    collect(out)
    return _gate_and_write(
        out, Path(a.baseline) if a.baseline else None, a.tolerance, None
    )


def cmd_verify_receipt(a: argparse.Namespace) -> int:
    problems = report.verify_receipt(
        Path(a.receipt), Path(a.results) if a.results else None
    )
    hard = [p for p in problems if not p.startswith("unsigned")]
    for p in problems:
        print(("note: " if p.startswith("unsigned") else "problem: ") + p)
    if not hard:
        print("receipt checks out")
    return 1 if hard else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="openadapt test",
        allow_abbrev=False,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "list", allow_abbrev=False, help="the built-in apps and their scenarios"
    ).set_defaults(fn=cmd_list)
    for name, fn, hlp in (
        ("up", cmd_up, "start an app's stack and wait for it"),
        ("down", cmd_down, "stop an app's stack and remove its data"),
    ):
        p = sub.add_parser(name, allow_abbrev=False, help=hlp)
        _common(p)
        if name == "up":
            p.add_argument(
                "--bind", default="", help="the address the app's port is published on"
            )
        p.set_defaults(fn=fn)
    p = sub.add_parser(
        "run", allow_abbrev=False, help="run an agent through the scenarios"
    )
    _common(p)
    p.add_argument(
        "--agent",
        default="",
        help="scripted, scripted-readback, claude-cu, openai-cu, claude-code-mcp, mcp:<cmd>, browser-use:<script>",
    )
    p.add_argument(
        "--agent-cmd",
        default="",
        help="any command, run once per scenario (see the docs for its environment)",
    )
    p.add_argument(
        "--model",
        default="",
        help="the model for claude-cu, openai-cu or claude-code-mcp",
    )
    p.add_argument(
        "--price",
        default="",
        help="input,output USD per million tokens, for a model without a built-in price",
    )
    p.add_argument(
        "--instructions",
        default="",
        help="a text file added to the agent's instruction (your agent's own prompt)",
    )
    p.add_argument(
        "--app-pack",
        default="",
        help="an app pack .yaml (default: the built-in pack for --app)",
    )
    p.add_argument(
        "--scenarios",
        default="",
        help="a scenario pack .yaml (default: the built-in pack for the app)",
    )
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--only", default="", help="comma-separated scenario ids")
    p.add_argument("--out", default="")
    p.add_argument(
        "--spend-cap-usd",
        type=float,
        default=5.0,
        help="model spend cap for this run (default 5)",
    )
    p.add_argument(
        "--max-load",
        type=float,
        default=None,
        help="wait between scenarios while the 1-minute load is above this",
    )
    p.add_argument("--baseline", default="", help="a baseline .json to gate against")
    p.add_argument("--tolerance", type=float, default=0.05)
    p.add_argument(
        "--allow-pack-change",
        action="store_true",
        help="gate even if the scenarios or app changed",
    )
    p.add_argument(
        "--min-scored",
        type=int,
        default=1,
        help="refuse to gate (exit 2) a run with fewer scored runs than this; "
        "an agent that stops saving has its faulted runs left out of the rates",
    )
    p.add_argument(
        "--sign-key",
        default="",
        help="an Ed25519 private key (PEM) to sign the receipt",
    )
    p.add_argument("--up", action="store_true", help="start the app's stack first")
    p.add_argument(
        "--down-after",
        action="store_true",
        help="stop the app's stack and remove its data afterwards",
    )
    p.add_argument("--bind", default="")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser(
        "gate", allow_abbrev=False, help="gate a finished run against a baseline"
    )
    p.add_argument("--results", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--tolerance", type=float, default=0.05)
    p.add_argument("--allow-pack-change", action="store_true")
    p.add_argument(
        "--min-scored",
        type=int,
        default=1,
        help="refuse to gate (exit 2) a run with fewer scored runs than this; "
        "an agent that stops saving has its faulted runs left out of the rates",
    )
    p.add_argument("--sign-key", default="")
    p.set_defaults(fn=cmd_gate)
    p = sub.add_parser(
        "baseline",
        allow_abbrev=False,
        help="write a baseline from a run's results.json",
    )
    p.add_argument("--results", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_baseline)
    p = sub.add_parser(
        "report",
        allow_abbrev=False,
        help="rebuild results.json, report.html and receipt.json from a run directory",
    )
    p.add_argument("out")
    p.add_argument("--baseline", default="")
    p.add_argument("--tolerance", type=float, default=0.05)
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser(
        "verify-receipt",
        allow_abbrev=False,
        help="check a receipt's signature and its digest of results.json",
    )
    p.add_argument("receipt")
    p.add_argument("--results", default="")
    p.set_defaults(fn=cmd_verify_receipt)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
