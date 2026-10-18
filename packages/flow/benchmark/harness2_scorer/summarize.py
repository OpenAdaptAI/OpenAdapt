"""Summarise a harness2_scorer run and copy its raw data into the repository.

    python benchmark/harness2_scorer/summarize.py RUN_DIR [--copy-to DIR]

``RUN_DIR`` holds one sub-directory per arm (``base``, ``harness2``,
``harness2_readback``), each with the ``results.json`` that ``run.py``
wrote. The summary counts the fresh re-runs of each chosen harness, scored
by read-back. ``--copy-to`` copies the raw data, without the reward
workers' per-rollout private keys and tokens and without the proposers'
rebuilt context trees.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

ARMS = ("base", "harness2", "harness2_readback")
LABEL = {"base": "base harness", "harness2": "Harness²", "harness2_readback": "Harness² + read-back"}
#: Never copied: private signing keys and bearer tokens, and the context
#: trees Harness² rebuilds from the rollouts on every stage.
SKIP_NAMES = {"ed25519.pem", "token"}
SKIP_DIRS = {"context"}


def load(run_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for arm in ARMS:
        p = run_dir / arm / "results.json"
        if p.is_file():
            out[arm] = json.loads(p.read_text(encoding="utf-8"))
    return out


def right(r: dict[str, Any]) -> bool:
    """The task's right outcome, and no stray, duplicate or wrong write.

    Applied here as well as in the scorer, so runs scored before the scorer
    applied it count the same way.
    """

    return bool(r.get("all_pass")) and not (
        r.get("wrong_record") or r.get("duplicate") or r.get("wrong_value")
    )


def tally(rows: dict[str, Any]) -> dict[str, Any]:
    reruns = [r for row in rows.values() for r in row.get("reruns", [])]
    outcomes = Counter(r.get("outcome") for r in reruns)
    rewards = [r["reward"] for r in reruns if r.get("reward") is not None]
    return {
        "runs": len(reruns),
        "passed": sum(1 for r in reruns if right(r)),
        "verified": outcomes.get("verified", 0),
        "wrong_record": sum(1 for r in reruns if r.get("wrong_record")),
        "duplicate": sum(1 for r in reruns if r.get("duplicate")),
        "safe_stop": sum(1 for r in reruns if r.get("safe_stop")),
        "unscored": sum(1 for r in reruns if r.get("reward") is None),
        "mean_reward": round(sum(rewards) / len(rewards), 3) if rewards else None,
        "outcomes": dict(outcomes),
        "usd": round(sum(row.get("usd", 0.0) for row in rows.values()), 4),
    }


def render(data: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| Arm | Runs | Right outcome | Verified | Wrong record | Duplicate | Safe stop | Unscored | Mean reward | Spend |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm, res in data.items():
        t = tally(res["arms"].get(arm.replace("_readback", "+readback"), {}))
        lines.append(
            f"| {LABEL[arm]} | {t['runs']} | {t['passed']} | {t['verified']} | {t['wrong_record']} | "
            f"{t['duplicate']} | {t['safe_stop']} | {t['unscored']} | {t['mean_reward']} | ${t['usd']} |"
        )
    tasks = sorted({tid for res in data.values() for row in res["arms"].values() for tid in row})
    lines += ["", "Right outcomes per task (fresh re-runs):", ""]
    lines.append("| Task | " + " | ".join(LABEL[a] for a in data) + " |")
    lines.append("|---|" + "---|" * len(data))
    for tid in tasks:
        cells = []
        for arm, res in data.items():
            row = res["arms"].get(arm.replace("_readback", "+readback"), {}).get(tid, {})
            rr = row.get("reruns", [])
            cells.append(f"{sum(1 for r in rr if right(r))}/{len(rr)}")
        lines.append(f"| `{tid}` | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def copy_raw(run_dir: Path, dest: Path) -> int:
    n = 0
    for src in sorted(run_dir.rglob("*")):
        rel = src.relative_to(run_dir)
        if not src.is_file() or src.name in SKIP_NAMES or SKIP_DIRS & set(rel.parts):
            continue
        if src.suffix in (".log",):
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--copy-to", type=Path)
    args = ap.parse_args()
    data = load(args.run_dir)
    print(render(data))
    summary = {
        arm: tally(res["arms"].get(arm.replace("_readback", "+readback"), {}))
        for arm, res in data.items()
    }
    if args.copy_to:
        n = copy_raw(args.run_dir, args.copy_to / "raw")
        (args.copy_to / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
        print(f"copied {n} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
