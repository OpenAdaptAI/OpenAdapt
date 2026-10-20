"""Public copy describes the product as it ships.

Never publish unreleased or in-progress work on a public surface: no "being
built", "not released", "in review", "coming soon", "being merged" or roadmap
lists. Track gaps internally (issues, private notes) and build them instead.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PUBLIC = [
    ROOT / "README.md",
    ROOT / "CAPABILITIES.md",
    ROOT / "llms.txt",
    *sorted((ROOT / "packages").glob("*/README.md")),
]

NEVER = re.compile(
    r"\bbeing built\b|\bnot (?:yet )?released\b|\bnot released yet\b"
    r"|\bcoming soon\b|\bin review\b|\bin progress\b|\bbeing merged\b"
    r"|\bbeing added\b|\bwhen it ships\b|\bnot done yet\b|\bon (?:our|the) roadmap\b",
    re.IGNORECASE,
)


def test_public_copy_lists_only_shipped_work() -> None:
    hits = []
    for path in PUBLIC:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = NEVER.search(line)
            if match:
                hits.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)!r}")
    assert hits == [], "unreleased-work copy on a public surface:\n" + "\n".join(hits)
