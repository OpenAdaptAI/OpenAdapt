"""Write ``RESULTS.md`` from ``results.json``.

python -m benchmark.agent_guard.report
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median
from typing import Any, Optional

from benchmark.agent_guard.run import SCENARIOS

HERE = Path(__file__).resolve().parent

WORDS = {
    "clean": "right save, read back VERIFIED",
    "blocked": "blocked; nothing wrong saved",
    "caught_by_readback": "wrong or missing save, reported (not VERIFIED)",
    "got_through": "wrong save, reported VERIFIED",
    "silent_wrong": "wrong save; the screen said saved",
    "visible_error": "wrong or missing save; the screen showed an error",
    "false_alarm": "right save, reported as not verified",
}
SHORT = {
    "clean": "clean",
    "blocked": "**blocked**",
    "caught_by_readback": "caught by read-back",
    "got_through": "**got through**",
    "silent_wrong": "**silent wrong**",
    "visible_error": "error on screen",
    "false_alarm": "false alarm",
}


def _cell(outcomes: list[str]) -> str:
    kinds = sorted(set(outcomes))
    text = " / ".join(SHORT.get(k, k) for k in kinds)
    return text if len(kinds) == 1 else f"mixed: {text}"


def _med(values: list[Any]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(median(values), 2) if values else None


def render(results: dict[str, Any]) -> str:
    rows = results.get("runs") or []
    arms = results.get("arms") or []
    out: list[str] = []
    out.append("# Guard an agent you already run: MockMed results\n")
    out.append(
        f"Date: {results.get('date')}. Synthetic MockMed, localhost only, no model "
        f"calls anywhere. {results.get('n')} trials per scenario and arm, one "
        "browser at a time. The outside agent is a scripted Playwright agent "
        f"with {results.get('think_s')} s between actions. Every run is judged by "
        "the `effect_e2e` ground truth, which reads the SQLite file directly.\n"
    )
    loads = [r.get("load_avg_1m") for r in rows] + [
        r.get("load_avg_1m") for r in (results.get("run2") or {}).get("runs") or []
    ]
    loads = [v for v in loads if v is not None]
    if loads:
        out.append(
            f"**Load.** The host was shared with other work. The 1-minute load "
            f"average during the runs was {min(loads):.1f} to {max(loads):.1f} "
            f"(median {median(loads):.1f}); every row in `results.json` has its "
            "own. Read the times as run 1 against run 2 under the same load, not "
            "as absolute speed.\n"
        )

    lines = results.get("setup_lines") or {}
    out.append("## Setup the developer adds\n")
    out.append("| What | Lines |\n| --- | --- |")
    out.append(
        f"| Agent code (the DevTools port on the browser it launches) | "
        f"{lines.get('agent_code')} |"
    )
    out.append(f"| The guard command | {lines.get('command')} |")
    out.append(f"| `guard.yaml`, once per app | {lines.get('guard_yaml')} |")
    out.append(
        f"| `verify.yaml`, once per app (the read verify-run uses) | "
        f"{lines.get('verify_yaml')} |"
    )
    out.append(
        f"| Task file, per task, from whoever assigns the work | "
        f"{lines.get('task_json_per_task')} |\n"
    )

    out.append("## What happened to each save\n")
    head = "| Scenario | What goes wrong | " + " | ".join(arms) + " |"
    out.append(head)
    out.append("| --- | --- | " + " | ".join("---" for _ in arms) + " |")
    for scenario in SCENARIOS:
        cells = []
        for arm in arms:
            got = [
                r["outcome"]
                for r in rows
                if r["scenario"] == scenario.name and r["arm"] == arm
            ]
            cells.append(_cell(got) if got else "")
        out.append(
            f"| `{scenario.name}` ({scenario.family}) | {scenario.blurb} | "
            + " | ".join(cells)
            + " |"
        )
    out.append("")
    out.append("Outcomes: " + "; ".join(f"{SHORT[k]}: {v}" for k, v in WORDS.items()))
    out.append("")

    out.append("## The effect-test faults, counted\n")
    wrong = [s for s in SCENARIOS if s.family == "effect_e2e" and s.name != "ok"]
    out.append(
        "The nine `effect_e2e` fault classes other than `ok`, injected at the store. "
        "`timeout` leaves the right record (it commits, then answers late), so it "
        "is a wrong save only if it is reported as one.\n"
    )
    out.append(
        "| Arm | Blocked by the guard | Caught by verify-run | Got through | "
        "Right save, verified | Right save, flagged |"
    )
    out.append("| --- | --- | --- | --- | --- | --- |")
    for arm in arms:
        counts = {k: 0 for k in WORDS}
        for scenario in wrong:
            got = [
                r["outcome"]
                for r in rows
                if r["scenario"] == scenario.name and r["arm"] == arm
            ]
            if got:
                # Deterministic in practice; a mixed scenario counts its worst.
                order = [
                    "got_through",
                    "silent_wrong",
                    "false_alarm",
                    "visible_error",
                    "caught_by_readback",
                    "blocked",
                    "clean",
                ]
                worst = next(k for k in order if k in got)
                counts[worst] += 1
        if arm == "agent_alone":
            out.append(
                f"| {arm} | n/a | n/a | {counts['silent_wrong']} silent, "
                f"{counts['visible_error']} with an error on screen | "
                f"{counts['clean']} | {counts['false_alarm']} |"
            )
        else:
            out.append(
                f"| {arm} | {counts['blocked']} | {counts['caught_by_readback']} | "
                f"{counts['got_through']} | {counts['clean']} | "
                f"{counts['false_alarm']} |"
            )
    out.append("")

    out.append("## Run 1 against run 2\n")
    ok = [r for r in rows if r["scenario"] == "ok"]
    run2 = results.get("run2") or {}
    ok2 = [r for r in run2.get("runs") or [] if r["scenario"] == "ok"]
    out.append("| | Wall time (median) | Model calls | Agent actions | Load (1 min) |")
    out.append("| --- | --- | --- | --- | --- |")
    for arm in arms:
        mine = [r for r in ok if r["arm"] == arm]
        if not mine:
            continue
        out.append(
            f"| Run 1, {arm} | {_med([r['agent_seconds'] for r in mine])} s | 0 "
            f"(scripted; a model-driven agent makes about one call per action) | "
            f"{_med([r['agent_actions'] for r in mine])} | "
            f"{_med([r.get('load_avg_1m') for r in mine])} |"
        )
    if ok2:
        out.append(
            f"| Run 2, the `taught_*` tool over MCP | {_med([r['seconds'] for r in ok2])}"
            f" s | {ok2[0].get('model_calls')} | 0 | "
            f"{_med([r.get('load_avg_1m') for r in ok2])} |"
        )
    out.append("")
    pairs = run2.get("paired") or []
    if pairs:
        out.append(
            f"**Same load.** Then {len(pairs)} pairs, back to back: a guarded run 1 of "
            "`ok` by the agent, then one call of the run-2 tool, with the load "
            "taken at each.\n"
        )
        out.append(
            "| Pair | Run 1 (agent, guarded) | Run 2 (tool) | Run 2 model calls | "
            "Load (1 min) |"
        )
        out.append("| --- | --- | --- | --- | --- |")
        for k, pair in enumerate(pairs):
            out.append(
                f"| {k + 1} | {pair['run1_agent_seconds']} s ({pair['run1_outcome']}) | "
                f"{pair['run2_seconds']} s ({pair['run2_outcome']}) | "
                f"{pair['run2_model_calls']} | {pair['run1_load_avg_1m']} |"
            )
        out.append(
            f"| Median | {_med([p['run1_agent_seconds'] for p in pairs])} s | "
            f"{_med([p['run2_seconds'] for p in pairs])} s | 0 | "
            f"{_med([p['run1_load_avg_1m'] for p in pairs])} |\n"
        )
        out.append(
            "Run 1's time is mostly the scripted agent's 1 s pause before each of "
            "its 6 actions; a model-driven agent spends a model call there "
            "instead, so its run 1 is longer and costs about 6 model calls. Run 2 "
            "makes none.\n"
        )
    guarded = [r for r in ok if r["arm"] == "guard"]
    holds = [h for r in guarded for h in r.get("hold_ms") or []]
    reads = [r.get("readback_ms") for r in guarded]
    out.append(
        "Run 1 is the agent's time from its first action to seeing the result, "
        "with the guard (if any) holding the save: "
        f"{results.get('think_s')} s of thinking before each action stands in for "
        "a model. The guard held the save for its "
        f"checks for a median {_med(holds)} ms and read it back in {_med(reads)} ms. "
        "Compiling run 1 into the draft (when the guard has a `teach:` section) "
        "happens after that, in the guard process: "
        f"{_med([r.get('guard_seconds') for r in guarded if r.get('handoff')])} s "
        "for the whole guard process on the run that was handed over. Run 2 "
        "includes starting Flow's own headless browser, the replay and its "
        "read-back.\n"
    )
    if run2:
        out.append("## Run 2 against the same faults\n")
        out.append(
            "After a person confirmed the record check and admitted the program "
            f"(`compile-agent admit`, one attended canary, {run2.get('admit_seconds')}"
            " s), the program was called as an MCP tool once per store fault.\n"
        )
        check = ((results.get("handoff") or {}).get("proposed_check")) or {}
        if check:
            out.append(
                f"Its record check came from scaffold-verifier "
                f"({'used' if check.get('proposed') else 'not used'}; "
                f"caught {check.get('faults_caught')} of {check.get('faults')} faults "
                f"in its own test). Reads: {', '.join(check.get('reads') or [])}.\n"
            )
        out.append("| Fault | Tool outcome | Ground truth | Result | Seconds |")
        out.append("| --- | --- | --- | --- | --- |")
        for r in run2.get("runs") or []:
            out.append(
                f"| `{r['scenario']}` | {r.get('tool_outcome')} | "
                f"{r['truth']['fault_class']} | {SHORT.get(r['outcome'], r['outcome'])} "
                f"| {r['seconds']} |"
            )
        out.append("")
        api = (results.get("handoff") or {}).get("api_step") or {}
        if api:
            out.append(
                "API step: "
                + (
                    "proposed."
                    if api.get("proposed")
                    else "not proposed ("
                    + "; ".join(api.get("reasons") or [])
                    + "). The program runs its GUI steps."
                )
                + "\n"
            )
    out.append("## Reproduce\n")
    out.append("```bash\npython -m benchmark.agent_guard.run --n 2 --pairs 5\npython -m benchmark.agent_guard.report\n```\n")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    path = Path((argv or sys.argv[1:] or [str(HERE / "results.json")])[0])
    results = json.loads(path.read_text())
    (path.parent / "RESULTS.md").write_text(render(results))
    print(path.parent / "RESULTS.md")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
