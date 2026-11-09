#!/usr/bin/env python3
"""Split Flow's unit test files into shards of about equal run time.

flow-unit used to deal files round-robin from a sorted list. Run times per
file range from under a second to over four minutes, so one 2026-11-03 pull
request run had shards of 4.9, 5.5, 9.0 and 9.4 minutes. This script packs
files by their measured time instead (longest first, each to the shard with
the least so far), so every shard ends at about the same time and fewer
shards fit the ten-minute `ci` budget.

Each file lands in exactly one shard. A shard lists its files longest first:
pytest-xdist's loadfile scheduler hands files to workers in that order, so a
long file starts early instead of leaving one worker running after the rest
are done. A file missing from the durations (a new test) counts as the median.
Stale durations only unbalance the shards; they never drop a file.

    find tests -name 'test_*.py' | python ../../scripts/ci/flow_shards.py --shard 1 --of 3

Refresh the durations from one run's flow-unit job logs, one file per shard
(`gh api repos/OWNER/REPO/actions/jobs/ID/logs`, or `gh run view --log`):

    python scripts/ci/flow_shards.py --from-logs shard1.log shard2.log ...
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

DURATIONS = Path(__file__).with_name("flow_test_durations.json")

# A pytest-xdist -v result line in a GitHub Actions log, after any
# `gh run view --log` "job<TAB>step<TAB>" prefix:
# 2026-11-03T22:24:26.5383055Z [gw0] [  0%] PASSED tests/test_x.py::test_y
LOG_LINE = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6})\d*Z \[(gw\d+)\] \[\s*\d+%\] \S+ (tests/[^:\s]+\.py)"
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def load(path: Path = DURATIONS) -> dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8"))["seconds"]


def plan(files: list[str], shards: int, seconds: dict[str, float]) -> list[list[str]]:
    """Shards of `files`, each longest first, with about equal total time."""
    known = [seconds[f] for f in files if f in seconds]
    default = statistics.median(known) if known else 1.0
    cost = {f: seconds.get(f, default) for f in files}
    loads = [0.0] * shards
    out: list[list[str]] = [[] for _ in range(shards)]
    for f in sorted(set(files), key=lambda f: (-cost[f], f)):
        i = min(range(shards), key=lambda i: (loads[i], i))
        out[i].append(f)
        loads[i] += cost[f]
    return out


def from_logs(paths: list[Path]) -> dict[str, float]:
    """Seconds per test file, from flow-unit job logs.

    Each xdist worker runs one file at a time (--dist loadfile), so the time
    from a worker's previous result line to each of a file's lines is that
    file's.
    """
    seconds: dict[str, float] = {}
    for path in paths:
        last: dict[str, datetime] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = LOG_LINE.search(ANSI.sub("", line))
            if not m:
                continue
            ts = datetime.fromisoformat(m.group(1))
            worker, test_file = m.group(2), m.group(3)
            if worker in last:
                gap = (ts - last[worker]).total_seconds()
                seconds[test_file] = seconds.get(test_file, 0.0) + gap
            else:
                seconds.setdefault(test_file, 0.0)
            last[worker] = ts
    return {f: round(s, 1) for f, s in sorted(seconds.items())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    ap.add_argument("--shard", type=int, help="1-based shard to print")
    ap.add_argument("--of", type=int, help="number of shards")
    ap.add_argument("--durations", type=Path, default=DURATIONS)
    ap.add_argument(
        "--from-logs",
        nargs="+",
        type=Path,
        metavar="LOG",
        help="rewrite the durations file from these job logs",
    )
    args = ap.parse_args(argv)
    if args.from_logs:
        seconds = from_logs(args.from_logs)
        if not seconds:
            print("no pytest-xdist result lines in these logs", file=sys.stderr)
            return 1
        args.durations.write_text(
            json.dumps(
                {"source": [p.name for p in args.from_logs], "seconds": seconds},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"{len(seconds)} files, {sum(seconds.values()) / 60:.1f} worker-minutes")
        return 0
    if not args.shard or not args.of or not 1 <= args.shard <= args.of:
        ap.error("--shard N --of M, with 1 <= N <= M")
    files = [line.strip() for line in sys.stdin if line.strip()]
    shard = plan(files, args.of, load(args.durations))[args.shard - 1]
    # An empty shard prints nothing (a lone newline would reach pytest as "").
    if shard:
        print("\n".join(shard))
    return 0


if __name__ == "__main__":
    sys.exit(main())
