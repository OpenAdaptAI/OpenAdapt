"""What Desktop tells the person about patient data stored unencrypted.

Flow encrypts recordings, bundles and checkpoints by default. A few artifacts
can't be encrypted yet (run directories outside strict mode, desktop
recordings, approval requests, ``visualize`` and similar output), and Desktop
runs Flow under the recorded plaintext opt-out while its own per-workflow
keychain encryption covers certified workflows. Every such plaintext write
is a line in Flow's ledger, ``plaintext-at-rest.jsonl``. This module reads
that ledger so the window can show a notice, not only a line on stderr.

It reads the file directly, with the same location rule as
``openadapt_flow.at_rest``, so it works with any installed Flow version.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LEDGER_NAME = "plaintext-at-rest.jsonl"
OPT_OUT_ENV = "OPENADAPT_FLOW_PLAINTEXT_AT_REST"
MODE_ENV = "OPENADAPT_FLOW_AT_REST"
DESKTOP_OPT_OUT_REASON = (
    "OpenAdapt Desktop: certified workflows are encrypted with per-workflow "
    "keys in the system keychain; Flow's default encryption is not wired into "
    "Desktop yet"
)


def ledger_path(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    root = env.get("OPENADAPT_FLOW_AT_REST_HOME") or env.get("OPENADAPT_HOME")
    return (Path(root) if root else Path.home() / ".openadapt") / LEDGER_NAME


def strict(env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    return (env.get(MODE_ENV) or "").strip().lower() == "strict"


def flow_env(env: dict[str, str]) -> dict[str, str]:
    """The Flow subprocess environment, with Desktop's recorded opt-out.

    Strict mode and an opt-out the person set themselves are left alone.
    """
    if not strict(env) and not (env.get(OPT_OUT_ENV) or "").strip():
        env[OPT_OUT_ENV] = DESKTOP_OPT_OUT_REASON
    return env


def status(env: dict[str, str] | None = None, *, limit: int = 200) -> dict[str, Any]:
    """Counts and the latest unencrypted write, for the window's notice."""
    entries: list[dict[str, Any]] = []
    try:
        lines = ledger_path(env).read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    latest = entries[-1] if entries else {}
    return {
        "strict": strict(env),
        "unencrypted_writes": len(entries),
        "latest_purpose": str(latest.get("purpose") or ""),
        "latest_at": latest.get("recorded_at"),
        "ledger": str(ledger_path(env)),
    }
