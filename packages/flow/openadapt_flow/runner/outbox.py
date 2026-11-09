"""Durable on-disk evidence outbox — a run that finishes offline reports late
rather than never.

Layout: ``<root>/<run_id>/<n>.json`` where ``n`` is a zero-padded batch index
and each file holds ``{"events": [...]}`` (≤50 events, already in final wire
shape). Batches flush strictly in order per run; the server upserts by
``(run_id, seq)`` and drops duplicates, so a crash between POST and unlink is
harmless.

Permanently rejected batches (422 PHI schema, 409 terminal conflict, 403/404)
move to ``<root>-rejected/<run_id>/`` for operator inspection instead of
poisoning the queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


class EvidenceOutbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rejected_root = root.parent / f"{root.name}-rejected"

    def enqueue(self, run_id: str, events: list[dict[str, Any]]) -> Path:
        """Append one batch for ``run_id``; returns the batch file path."""
        if not events:
            raise ValueError("refusing to enqueue an empty evidence batch")
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        existing = [int(p.stem) for p in run_dir.glob("*.json") if p.stem.isdigit()]
        index = max(existing, default=-1) + 1
        path = run_dir / f"{index:06d}.json"
        tmp = run_dir / f".{index:06d}.json.tmp"
        tmp.write_text(json.dumps({"events": events}), encoding="utf-8")
        tmp.replace(path)  # atomic: a torn write never becomes a batch
        return path

    def pending(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(run_id, batch_path)`` in per-run order, oldest run first."""
        if not self.root.is_dir():
            return
        run_dirs = sorted(
            (d for d in self.root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
        )
        for run_dir in run_dirs:
            for path in sorted(run_dir.glob("*.json")):
                yield run_dir.name, path

    @staticmethod
    def load(path: Path) -> list[dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            raise ValueError(f"outbox batch {path} is malformed")
        return events

    def mark_sent(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self._prune(path.parent)

    def mark_rejected(self, run_id: str, path: Path, detail: str) -> Path:
        """Move a permanently rejected batch aside, with the rejection note."""
        dest_dir = self.rejected_root / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        path.replace(dest)
        dest.with_suffix(".reason.txt").write_text(detail[:2000], encoding="utf-8")
        self._prune(path.parent)
        return dest

    def _prune(self, run_dir: Path) -> None:
        try:
            next(run_dir.iterdir())
        except StopIteration:
            run_dir.rmdir()
        except OSError:
            pass

    def depth(self) -> int:
        """Number of unflushed batches (for the status line)."""
        return sum(1 for _ in self.pending())
