"""JSON-file-based correction library for the correction flywheel.

Stores corrections as individual JSON files in a directory. Retrieval uses
exact task_id match + fuzzy string similarity on step descriptions.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class CorrectionEntry:
    """A single stored correction."""

    task_id: str
    step_description: str  # original step action text
    failure_screenshot_path: str
    failure_explanation: str
    correction_step: dict  # PlanStep as dict (think/action/expect)
    timestamp: str = ""  # ISO format
    run_id: str = ""
    entry_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.entry_id:
            self.entry_id = uuid.uuid4().hex[:12]


class CorrectionStore:
    """Manages a directory of correction JSON files."""

    def __init__(self, library_dir: str = "correction_library"):
        self.library_dir = library_dir
        os.makedirs(library_dir, exist_ok=True)

    def save(self, entry: CorrectionEntry) -> str:
        """Save correction, return entry ID."""
        path = os.path.join(self.library_dir, f"{entry.entry_id}.json")
        with open(path, "w") as f:
            json.dump(asdict(entry), f, indent=2)
        logger.info("Saved correction %s for task %s", entry.entry_id, entry.task_id)
        return entry.entry_id

    def find(
        self,
        task_id: str,
        step_description: str,
        top_k: int = 3,
        threshold: float = 0.6,
    ) -> list[CorrectionEntry]:
        """Find matching corrections by task_id + fuzzy step description match."""
        all_entries = self.load_all()

        # Filter to matching task_id
        candidates = [e for e in all_entries if e.task_id == task_id]
        if not candidates:
            return []

        # Score by string similarity on step_description
        scored = []
        for entry in candidates:
            ratio = difflib.SequenceMatcher(
                None, step_description.lower(), entry.step_description.lower()
            ).ratio()
            if ratio >= threshold:
                scored.append((ratio, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    def load_all(self) -> list[CorrectionEntry]:
        """Load all corrections from the library directory."""
        entries = []
        if not os.path.isdir(self.library_dir):
            return entries
        for fname in os.listdir(self.library_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.library_dir, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                entries.append(CorrectionEntry(**data))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.warning("Skipping invalid correction file %s: %s", fname, exc)
        return entries
