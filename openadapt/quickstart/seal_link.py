"""A run's Seal, read back for the finale's Seal card.

The finale draws the first checked run's Seal from two small files the engine
wrote: ``receipt.json`` (an allow-listed summary: counts, outcomes and
digests, no typed value, URL or screenshot) and ``seal.json`` (the key and the
signature over that run's ``report.json``).

Putting a Seal online is ``openadapt seal publish`` (:mod:`openadapt.seal_publish`):
it uploads the run's signed public card, and only after the person says yes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

RECEIPT_SCHEMA = "openadapt.run-receipt/v2"
SEAL_SCHEMA = "openadapt.local-seal/v1"


@dataclass(frozen=True)
class SealCard:
    """What the finale's Seal card shows, read from one run's files."""

    outcome: str
    steps_ok: int
    steps_total: int
    effects_confirmed: int
    effects_required: int
    checks_confirmed: int
    checks_required: int
    model_calls: int
    key_id: str
    report_sha256: str
    receipt: Mapping[str, Any]
    seal: Mapping[str, Any]

    @property
    def short_digest(self) -> str:
        return self.report_sha256[:12]


def read_card(run_dir: Path) -> Optional[SealCard]:
    """The Seal card for ``run_dir``, or None when its files are missing or odd."""

    try:
        receipt = json.loads(_summary_path(run_dir).read_text(encoding="utf-8"))
        seal = json.loads((run_dir / "seal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or not isinstance(seal, dict):
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        return None
    if seal.get("schema") != SEAL_SCHEMA:
        return None
    try:
        return SealCard(
            outcome=str(receipt["outcome"]),
            steps_ok=int(receipt["steps_ok"]),
            steps_total=int(receipt["steps_total"]),
            effects_confirmed=int(receipt["effects_confirmed"]),
            effects_required=int(receipt["effects_required"]),
            checks_confirmed=int(receipt.get("postconditions_confirmed", 0)),
            checks_required=int(receipt.get("postconditions_required", 0)),
            model_calls=int(receipt["model_calls"]),
            key_id=str(seal["key_id"]),
            report_sha256=str(seal["subject_sha256"]),
            receipt=receipt,
            seal=seal,
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "SealCard",
    "read_card",
]


def _summary_path(run_dir: Path) -> Path:
    """``run-summary.json``; runs from before Flow 3.5 kept it as ``receipt.json``."""
    current = run_dir / "run-summary.json"
    return current if current.is_file() else run_dir / "receipt.json"
