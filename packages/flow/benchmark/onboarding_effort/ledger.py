"""Append-only effort ledger for the onboarding benchmark.

One JSON line per human-equivalent action. Engineer minutes come from the
rate card in PROTOCOL.md; machine seconds, tokens and dollars are measured.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

# PROTOCOL.md "Accounting". Changing a value changes the protocol version.
RATE_CARD = {
    "demo_scripted_base": 2.0,
    "demo_scripted_per_action": 0.1,
    "demo_agent_prompt": 3.0,
    "question": 1.0,
    "config_line": 1.0,
    "triage_failure": 5.0,
    "command": 0.5,
}
PROTOCOL_VERSION = "onboarding-effort/v1"


def host_load() -> float:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, timeout=5
        ).stdout.split()
        return float(out[1])
    except Exception:  # noqa: BLE001 - load is context, never a result
        return -1.0


def free_gib(path: str = str(Path.home())) -> float:
    import shutil

    return shutil.disk_usage(path).free / 1024**3


@dataclass
class Row:
    workflow: str
    installation: str  # "first" | "second"
    phase: str  # demonstrate | compile | lint | config | qualify | correct | heldout | ...
    action: str
    demos_scripted: int = 0
    demos_agent: int = 0
    corrections: int = 0
    questions: int = 0
    config_lines: int = 0
    commands: int = 0
    failed_commands: int = 0
    engineer_min: float = 0.0
    machine_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model_usd: float = 0.0
    ok: Optional[bool] = None
    detail: dict[str, Any] = field(default_factory=dict)
    run: str = ""
    ts: float = field(default_factory=time.time)
    load1: float = field(default_factory=host_load)
    protocol: str = PROTOCOL_VERSION
    manifest_sha256: str = ""
    engine_commit: str = ""


def engineer_minutes(row: Row) -> float:
    rc = RATE_CARD
    minutes = (
        row.questions * rc["question"]
        + row.config_lines * rc["config_line"]
        + row.commands * rc["command"]
        + row.failed_commands * rc["triage_failure"]
        + row.demos_agent * rc["demo_agent_prompt"]
    )
    if row.demos_scripted:
        actions = int(row.detail.get("recorded_actions", 0))
        minutes += row.demos_scripted * rc["demo_scripted_base"] + actions * rc[
            "demo_scripted_per_action"
        ]
    return round(minutes, 2)


class Ledger:
    def __init__(self, path: Path, *, manifest_sha256: str = "", engine_commit: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_sha256 = manifest_sha256
        self.engine_commit = engine_commit

    def add(self, row: Row) -> Row:
        row.manifest_sha256 = row.manifest_sha256 or self.manifest_sha256
        row.engine_commit = row.engine_commit or self.engine_commit
        row.engineer_min = engineer_minutes(row)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(row), default=str, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]
