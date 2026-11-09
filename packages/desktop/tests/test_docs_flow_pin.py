"""Docs that name the exact bundled Flow release must name the current pin.

The pin lives in the ``build`` extra of ``pyproject.toml``. When it moves and
the docs do not, a reader gets two different answers to "which Flow does this
installer run", and version-specific claims in the docs point at the wrong
release. ``CHANGELOG.md`` is history and is not checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.smoke_test_native_installer import bundled_flow_version

ROOT = Path(__file__).resolve().parents[1]
EXACT_FLOW_PIN = re.compile(r"openadapt-flow(?:\[[^\]]*\])?==(\d+\.\d+\.\d+)")
# Docs that state the current pin and must keep stating it.
REQUIRED = ("RELEASES.md", "docs/DECISION_PORTAL.md", "docs/RELEASE_CANDIDATE_INSTALLERS.md")


def _current_docs() -> list[Path]:
    docs = [ROOT / "README.md", ROOT / "RELEASES.md", ROOT / "DESIGN.md"]
    docs += sorted((ROOT / "docs").rglob("*.md"))
    return [path for path in docs if path.is_file()]


@pytest.mark.parametrize("relative", REQUIRED)
def test_doc_names_the_bundled_flow_pin(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert EXACT_FLOW_PIN.search(text), f"{relative} no longer names the bundled Flow pin"


def test_no_current_doc_names_a_different_flow_pin() -> None:
    pinned = bundled_flow_version()
    stale = []
    for path in _current_docs():
        text = path.read_text(encoding="utf-8")
        for match in EXACT_FLOW_PIN.finditer(text):
            if match.group(1) != pinned:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{path.relative_to(ROOT)}:{line} says {match.group(0)}")
    assert not stale, f"docs disagree with the bundled Flow {pinned}: {stale}"
