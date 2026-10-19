"""One row per attempt, the same for every task and arm.

The outcome of an attempt is decided by an independent read-back of the
system of record after the attempt, never by what the arm reported:

``correct``
    The intended record exists exactly once with the intended values (and,
    for a fault item where no correct save is possible, the arm stopped and
    nothing wrong landed: see ``safe_stop``).
``silent_wrong``
    The arm reported success, but the read-back shows the record missing,
    duplicated or wrong.
``safe_stop``
    The arm stopped or said it could not finish, and the read-back shows no
    wrong record.
``flagged_wrong``
    A wrong or partial record landed, and the arm flagged it (halted, or said
    so) instead of reporting success.
``paused``
    Not attempted: the batch was paused for a person before this item.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
# JIT caches (recordings, screenshots, bundles, a throwaway admission key) live
# outside the package tree: the release check walks the source tree whatever
# .gitignore says, and none of this is published.
CACHE = Path(
    os.environ.get("SCOREBOARD_CACHE", Path.home() / ".cache" / "openadapt-scoreboard")
)
PROTOCOL_VERSION = "2026-10-18.1"
TIMED_LOAD_MAX = 12.0

OUTCOMES = ("correct", "silent_wrong", "safe_stop", "flagged_wrong", "paused")


@dataclass
class Row:
    task: str
    arm: str
    run_id: str
    item: int
    attempt: int
    fault: str  # none | optimistic | wrong_record | no_persist | ui_change
    reported_success: bool
    readback: str  # correct | wrong | missing | duplicate | not_checked
    outcome: str  # one of OUTCOMES
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    item_s: float  # the arm's own work: first action to done, sign-in excluded
    wall_s: float  # including browser start and sign-in
    approvals_asked: int
    host_load_1m: float
    timed: bool  # host_load_1m <= TIMED_LOAD_MAX at the start and the end
    detail: str = ""


FIELDS = [f.name for f in fields(Row)]


def classify(reported_success: bool, readback: str, fault: str) -> str:
    if readback == "correct":
        return "correct"
    if reported_success:
        return "silent_wrong"
    if readback == "missing":
        return "safe_stop"
    return "flagged_wrong"


def load_1m() -> float:
    return round(os.getloadavg()[0], 2)


def run_dir(task: str, arm: str, run_id: str) -> Path:
    return RAW / task / arm / run_id


def cache_dir(task: str, arm: str, run_id: str) -> Path:
    return CACHE / task / arm / run_id / "jit-cache"


def append(path: Path, row: Row) -> None:
    new = not path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(asdict(row))


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return list(csv.DictReader(f))


def git_head(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def engine() -> dict[str, str]:
    import openadapt_flow

    root = Path(openadapt_flow.__file__).resolve().parent
    return {"path": str(root), "git_head": git_head(root)}


def write_meta(out: Path, **extra: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "protocol_version": PROTOCOL_VERSION,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": f"{platform.machine()} {platform.system()} {platform.release()}",
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "engine": engine(),
        "benchmark_git_head": git_head(HERE),
        **extra,
    }
    path = out / "meta.json"
    if path.is_file():
        old = json.loads(path.read_text())
        old.setdefault("sessions", []).append(meta)
        meta = old
    path.write_text(json.dumps(meta, indent=2, default=str) + "\n")


def timed(start_load: float, end_load: Optional[float] = None) -> bool:
    loads = [start_load] + ([end_load] if end_load is not None else [])
    return max(loads) <= TIMED_LOAD_MAX
