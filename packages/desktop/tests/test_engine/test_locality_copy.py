"""Product copy must not claim that data can't leave the computer.

Backup, sync, and endpoint security software can copy local files, so "nothing
leaves this computer" and "stays on this computer" aren't true. Say what
OpenAdapt does instead: "OpenAdapt doesn't send this recording anywhere unless
you choose to share or upload it."
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The absolute claims that were replaced. Matched across line breaks, so a
#: wrapped sentence in JSX or Markdown still counts.
ABSOLUTE_CLAIMS = re.compile(
    r"nothing\s+(?:\w+\s+){0,2}leaves\s+(?:this|the|your)\s+(?:computer|machine|device)"
    r"|\bstays?\s+on\s+(?:this|your)\s+(?:computer|machine|device)"
    r"|\b(?:shown|saved)\s+on\s+this\s+computer\s+only"
    r"|\bstays\s+local\b",
    re.IGNORECASE,
)


def _copy_files() -> list[Path]:
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for pattern in ("*.ts", "*.tsx"):
        files += sorted(p for p in (ROOT / "src").rglob(pattern) if "node_modules" not in p.parts)
    files += sorted((ROOT / "openadapt_desktop").rglob("*.py"))
    files += sorted((ROOT / "src-tauri" / "src").rglob("*.rs"))
    return files


def test_no_absolute_locality_claims_in_product_copy():
    hits = []
    for path in _copy_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ABSOLUTE_CLAIMS.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    assert hits == []


def test_the_guard_catches_each_old_phrase():
    for phrase in (
        "nothing leaves this computer",
        "Nothing leaves the machine",
        "Everything stays on this computer.",
        "Recordings stay on your computer unless",
        "Patient data stays on\n this computer",
        "Shown on this computer only.",
        "Saved on this computer only",
        "Correct · stays local",
    ):
        assert ABSOLUTE_CLAIMS.search(phrase), phrase
    # Accurate statements about where something runs or who can reach it pass.
    for phrase in (
        "OpenAdapt doesn't send this recording anywhere unless you choose to share or upload it.",
        "The portal is available on this computer only.",
        "OpenAdapt finds buttons and fields on this computer only.",
    ):
        assert not ABSOLUTE_CLAIMS.search(phrase), phrase
