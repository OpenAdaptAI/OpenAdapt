"""Run the UV-port judge on both fault sets, under the spend cap.

    python -m benchmark.verifier_baselines.run_judge medplum
    python -m benchmark.verifier_baselines.run_judge effect_e2e

Rubrics are generated once per task text and kept in ``results/rubrics/``.
Every judge output is written as soon as it returns, so a stopped run
resumes where it left off. Spend is appended to ``results/spend.jsonl``.
See PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from benchmark.verifier_baselines.uv_judge import (
    OpenAIJudgeClient,
    generate_rubric,
    judge,
)

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
#: Per-run rows, judge outputs and the spend ledger are private run-level
#: evidence (openadapt-corpus, paid_agent_evidence/2026-11-03-verifier-baselines);
#: point this at that folder. Aggregates, rubrics and judge views stay in RES.
DATA = Path(os.environ.get("VERIFIER_BASELINES_DATA") or RES)
CAP_USD = 65.0
SAMPLES_PER_VIEW = 9


def spent() -> float:
    f = DATA / "spend.jsonl"
    if not f.exists():
        return 0.0
    return sum(
        json.loads(line)["usd"] for line in f.read_text().splitlines() if line.strip()
    )


def log_spend(label: str, usd: float, tokens_in: int, tokens_out: int) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "spend.jsonl", "a") as f:
        f.write(
            json.dumps(
                {
                    "label": label,
                    "usd": round(usd, 6),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "at": time.time(),
                }
            )
            + "\n"
        )


_RUBRIC_LOCKS: dict[str, asyncio.Lock] = {}


async def rubric_for(task: str, client: Any) -> dict[str, Any]:
    h = hashlib.sha256(task.encode()).hexdigest()[:16]
    async with _RUBRIC_LOCKS.setdefault(h, asyncio.Lock()):
        return await _rubric_for(task, h, client)


async def _rubric_for(task: str, h: str, client: Any) -> dict[str, Any]:
    f = RES / "rubrics" / f"{h}.json"
    if f.exists():
        return json.loads(f.read_text())["rubric"]
    rubric, used = await generate_rubric(task, "", client, return_usage=True)
    log_spend(
        f"rubric {h}",
        used.get("usd") or 0.0,
        used.get("tokens_in", 0),
        used.get("tokens_out", 0),
    )
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"task": task, "rubric": rubric}, indent=1))
    return rubric


async def judge_one(
    view: dict[str, Any],
    shots: list[bytes],
    out: Path,
    label: str,
    client: Any,
    sem: asyncio.Semaphore,
) -> None:
    if out.exists():
        return
    async with sem:
        if spent() > CAP_USD:
            return
        rubric = await rubric_for(view["task"], client)
        started = time.time()
        try:
            res = await judge(
                view["task"],
                view.get("init_url", ""),
                view["steps"],
                view["predicted_output"],
                shots,
                client,
                rubric=rubric,
            )
            res = {k: v for k, v in res.items() if k != "rubric"}
        except Exception as exc:  # noqa: BLE001 - a judge failure is recorded, and scored as a flag
            res = {
                "output_success": None,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        res["seconds"] = round(time.time() - started, 1)
        log_spend(
            label,
            res.get("usd") or 0.0,
            res.get("tokens_in", 0),
            res.get("tokens_out", 0),
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1, default=str))
        print(
            f"{label}: success={res.get('output_success')} ${res.get('usd') or 0:.3f} total ${spent():.2f}",
            flush=True,
        )


async def medplum(client: Any, parallel: int) -> None:
    from benchmark.verifier_baselines import medplum_views

    sem = asyncio.Semaphore(parallel)
    jobs = []
    for run in medplum_views.runs():
        if run["view"] is None:
            continue
        view = dict(run["view"])
        shots = view.pop("screenshots")
        name = f"{run['agent']}-{run['run']}"
        jobs.append(
            judge_one(
                view,
                shots,
                DATA / "medplum" / "judge" / f"{name}.json",
                f"medplum {name}",
                client,
                sem,
            )
        )
    await asyncio.gather(*jobs)


async def effect_e2e(client: Any, parallel: int, views: list[str]) -> None:
    rows = [
        json.loads(line)
        for line in (DATA / "effect_e2e" / "rows.jsonl").read_text().splitlines()
    ]
    sem = asyncio.Semaphore(parallel)
    jobs = []
    for key in views:  # j1_view, then j2_view
        per_view: dict[str, int] = {}
        for r in rows:
            per_view[r[key]] = per_view.get(r[key], 0) + 1
        # Views of a save the app accepted first: the exception screen already
        # flags the others in the primary results, so the judge decides only these.
        accepted = {r[key]: not r["exception"] for r in rows}
        for h, count in sorted(
            per_view.items(), key=lambda kv: (not accepted[kv[0]], kv[0])
        ):
            d = RES / "effect_e2e" / "views" / h
            view = json.loads((d / "view.json").read_text())
            shots = [p.read_bytes() for p in sorted(d.glob("screen*.png"))]
            for i in range(min(count, SAMPLES_PER_VIEW)):
                out = DATA / "effect_e2e" / "judge" / h / f"sample{i}.json"
                jobs.append(
                    judge_one(
                        view, shots, out, f"effect_e2e {key} {h} #{i}", client, sem
                    )
                )
    await asyncio.gather(*jobs)


def main() -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("set", choices=["medplum", "effect_e2e"])
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--views", default="j1_view,j2_view")
    args = ap.parse_args()
    client = OpenAIJudgeClient()
    if args.set == "medplum":
        asyncio.run(medplum(client, args.parallel))
    else:
        asyncio.run(effect_e2e(client, args.parallel, args.views.split(",")))
    print(f"study total ${spent():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
