"""Measure compile-agent on MockMed: selective vision against screenshot-every-turn.

Run 1 is a live Claude computer-use run (``claude-opus-5-5``) on the operator's
own key. Each trial runs both arms, in alternating order, on the same task
against a freshly reset MockMed with its transactional system of record:

* ``selective``: the harness sends the page's structural listing; the model
  asks for a screenshot when it wants one, and gets one after a save.
* ``every_turn``: Claude's computer toolset, a screenshot after every action.

For each run it records the model's token usage per call (uncached input,
cache writes, cache reads, output), the estimated cost, wall time, the
screenshots sent, and whether run 1 passed the record check. Then one
passing program is admitted (a person's name confirms the checks; a
throwaway admitter key admits) and replayed ``--replays`` times, to measure
later runs: model calls, tokens and time.

    PYTHONPATH=. python benchmark/compile_agent/measure.py --env-file /path/to/.env --trials 3

The key is read from ``--env-file`` (``ANTHROPIC_API_KEY=...``) into this
process only, and never printed. ``--max-total-usd`` stops before a run that
could pass it. All data is synthetic (MockMed).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
REPLAY_NOTES = [
    "Recheck knee in ten days",
    "Synthetic note for replay two",
    "Ice and rest, review in a week",
    "Synthetic note for replay four",
    "Physio referral, synthetic",
]


def _load_key(env_file: Path) -> None:
    for line in env_file.read_text().splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "ANTHROPIC_API_KEY":
            os.environ["ANTHROPIC_API_KEY"] = value.strip().strip('"').strip("'")
            return
    raise SystemExit(f"no ANTHROPIC_API_KEY in {env_file}")


def _verify_yaml(dirpath: Path, base: str) -> Path:
    path = dirpath / "verify.yaml"
    path.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 3.0\n"
        "  poll_interval_s: 0.1\n"
    )
    return path


def _compiler(work: Path, base: str, task_id: str) -> Any:
    from openadapt_flow.compile_agent import FirstRunCompiler
    from openadapt_flow.jit.keys import load_customer_model_key
    from openadapt_flow.tutorial import _http_json

    return FirstRunCompiler(
        PROMPT,
        params=["note"],
        url=f"{base}/?fault=ok&idempotency=demo#tasks",
        verify=_verify_yaml(work, base),
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "MRN P1"],
        task_id=task_id,
        cache_dir=work / "cache",
        key_dir=work / "keys",
        reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
        on_warning=lambda text: None,
        model_key=load_customer_model_key("anthropic"),
    )


def _clean_run1(run1: dict[str, Any]) -> dict[str, Any]:
    """Run 1's record without local paths or gate screenshots."""

    gates = [
        {k: v for k, v in g.items() if k not in ("screenshot", "url", "at")}
        for g in run1.get("gates") or []
    ]
    return {
        "jit_status": run1.get("jit_status"),
        "seconds": run1.get("seconds"),
        "agent": run1.get("agent"),
        "harness": run1.get("harness"),
        "gates": gates,
        "record_check": run1.get("record_check"),
        "agent_error": run1.get("agent_error"),
    }


def _admitter(work: Path) -> Any:
    from openadapt_flow.jit import authority

    os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, "measurement-only")
    pem = work / "keyfiles" / "admitter.pem"
    authority.register_principal(
        work / "keys",
        role="admitter_key",
        principal_id="ci:measurement",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(pem), work / "keys"
    )


def _median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(r[key]) for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def _summary(
    runs: list[dict[str, Any]], replays: list[dict[str, Any]]
) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for arm in ("selective", "every_turn"):
        rows = []
        for run in runs:
            if run["arm"] != arm:
                continue
            agent = run["run1"].get("agent") or {}
            harness = run["run1"].get("harness") or {}
            rows.append(
                {
                    "passed": run["status"] == "draft",
                    "prompt_tokens": agent.get("prompt_tokens"),
                    "input_tokens": agent.get("input_tokens"),
                    "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": agent.get(
                        "cache_creation_input_tokens"
                    ),
                    "output_tokens": agent.get("output_tokens"),
                    "cost_usd": agent.get("cost_usd"),
                    "model_calls": agent.get("model_calls"),
                    "run_seconds": run["run1"].get("seconds"),
                    "model_seconds": agent.get("model_seconds"),
                    "screenshots_sent": (
                        harness.get("screenshots_requested", 0)
                        + harness.get("screenshots_at_gate", 0)
                        + harness.get("screenshots_every_turn", 0)
                    ),
                }
            )
        arms[arm] = {
            "runs": len(rows),
            "passed": sum(1 for r in rows if r["passed"]),
            **{
                f"median_{key}": _median(rows, key)
                for key in (
                    "prompt_tokens",
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "model_calls",
                    "run_seconds",
                    "model_seconds",
                    "screenshots_sent",
                )
            },
            "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        }
    sel, full = arms["selective"], arms["every_turn"]

    def ratio(key: str) -> float | None:
        a, b = sel.get(key), full.get(key)
        return round(b / a, 2) if a and b else None

    return {
        "arms": arms,
        "every_turn_over_selective": {
            key: ratio(key)
            for key in (
                "median_prompt_tokens",
                "median_output_tokens",
                "median_cost_usd",
                "median_run_seconds",
                "median_model_seconds",
            )
        },
        "replays": {
            "runs": len(replays),
            "verified": sum(1 for r in replays if r["verified"]),
            "model_calls_total": sum(r["model_calls"] or 0 for r in replays),
            "median_seconds": _median(replays, "seconds"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--replays", type=int, default=5)
    parser.add_argument("--max-run-usd", type=float, default=3.0)
    parser.add_argument("--max-total-usd", type=float, default=15.0)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args()
    _load_key(args.env_file)

    from openadapt_flow.compile_agent import ClaudeAgent
    from openadapt_flow.mockmed.fault_server import serve

    args.out.mkdir(parents=True, exist_ok=True)
    runs_path = args.out / "runs.jsonl"
    replays_path = args.out / "replays.jsonl"
    runs: list[dict[str, Any]] = []
    replays: list[dict[str, Any]] = []
    spent = 0.0
    work_root = Path(tempfile.mkdtemp(prefix="compile-agent-measure-"))
    base, _db, stop = serve()
    base = base.rstrip("/")
    passing: list[tuple[str, Any]] = []
    try:
        for trial in range(1, args.trials + 1):
            order = ["selective", "every_turn"]
            if trial % 2 == 0:
                order.reverse()
            for arm in order:
                if spent + args.max_run_usd > args.max_total_usd:
                    print(
                        f"stopping: ${spent:.2f} spent, cap ${args.max_total_usd:.2f}"
                    )
                    raise StopIteration
                work = work_root / f"t{trial}-{arm}"
                work.mkdir(parents=True)
                compiler = _compiler(work, base, f"mockmed.measure-{arm}-t{trial}")
                agent = ClaudeAgent(
                    vision=arm, effort=args.effort, max_cost_usd=args.max_run_usd
                )
                started = time.time()
                load_before = os.getloadavg()[0]
                # A distinct note per trial, as real tasks have distinct inputs:
                # with one note for every trial, later runs read earlier runs'
                # screenshots from the prompt cache and cost less than a real run.
                note = f"{NOTE}, trial {trial}"
                result = compiler.record(agent, {"note": note})
                run1 = _clean_run1(result.run1)
                cost = float(((run1.get("agent") or {}).get("cost_usd")) or 0.0)
                spent += cost
                row = {
                    "trial": trial,
                    "arm": arm,
                    "order": order.index(arm),
                    "started_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)
                    ),
                    "status": result.status,
                    "note": note,
                    "detail": result.detail if result.status != "draft" else None,
                    # The host is shared; wall time moves with its load.
                    "host_load_1m": [
                        round(load_before, 1),
                        round(os.getloadavg()[0], 1),
                    ],
                    "run1": run1,
                }
                runs.append(row)
                with runs_path.open("a") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                agent_row = run1.get("agent") or {}
                print(
                    f"trial {trial} {arm}: {result.status}; "
                    f"{agent_row.get('model_calls')} calls, "
                    f"{agent_row.get('prompt_tokens')} prompt tokens, "
                    f"${cost:.3f}, {run1.get('seconds')} s (total ${spent:.2f})",
                    flush=True,
                )
                if result.status == "draft":
                    passing.append((arm, compiler))
    except StopIteration:
        pass
    try:
        # Admit one passing program from each arm and replay it.
        for arm in ("selective", "every_turn"):
            found = [c for a, c in passing if a == arm]
            if not found:
                continue
            compiler = found[0]
            compiler.confirm_checks(
                principal_id="measurement.operator", drop_fields=["key", "source"]
            )
            admitted = compiler.admit(
                admitter=_admitter(compiler.dir.parent.parent),
                canary_params=[{"note": "Canary test note 1"}],
            )
            print(f"admit {arm}: {admitted.status} {admitted.detail or ''}", flush=True)
            if not admitted.admitted:
                continue
            compiler.emit()
            for i, note in enumerate(REPLAY_NOTES[: args.replays], start=1):
                replay = compiler.run(note=note)
                row = {
                    "program_from": arm,
                    "replay": i,
                    "status": replay.status,
                    "verified": replay.verified,
                    "model_calls": replay.model_calls,
                    "model_tokens": 0 if replay.model_calls == 0 else None,
                    "seconds": replay.seconds,
                    "receipt_outcome": replay.receipt_outcome,
                    "host_load_1m": round(os.getloadavg()[0], 1),
                }
                replays.append(row)
                with replays_path.open("a") as fh:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                print(f"replay {arm} {i}: {row}", flush=True)
    finally:
        stop()
    summary = _summary(runs, replays)
    summary["environment"] = {
        "model": "claude-opus-5-5",
        "effort": args.effort,
        "computer_tool": "computer_toolset_20260801 (every_turn arm)",
        "app": "MockMed fault server, ?fault=ok&idempotency=demo, 1280x800",
        "engine_commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=HERE,
        ).stdout.strip(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_cost_usd": round(spent, 4),
        "prices_per_mtok": "input 4.00, output 20.00, cache write 5.00, cache read 0.20",
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
