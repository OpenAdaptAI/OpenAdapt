"""The legacy (v0.46.0) install instructions name a command that works.

README.md said v0.46.0 was "still installable with `pip install
openadapt==0.46.0`", and docs/LEGACY_FREEZE.md gave the same bare command.
It fails on every Python that 0.46.0 supports: its `openai-whisper`
dependency ships only as source and imports `pkg_resources`, which pip's
isolated build environment no longer has with setuptools 81 and later. The
freeze doc also told readers to clone `main` and run `poetry install`, but
`main` now holds the 1.x launcher.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
FREEZE = (ROOT / "docs" / "LEGACY_FREEZE.md").read_text(encoding="utf-8")
BARE_INSTALL = re.compile(
    r"pip install(?![^\n]*--build-constraint)[^\n]*openadapt==0\.46\.0"
)


def _recipe() -> str:
    match = re.search(
        r"<!-- legacy-install:start -->(.*?)<!-- legacy-install:end -->",
        FREEZE,
        re.S,
    )
    assert match, "docs/LEGACY_FREEZE.md must mark its install recipe"
    return match.group(1)


def test_readme_does_not_offer_the_bare_legacy_install():
    assert "still installable" not in README
    assert not BARE_INSTALL.search(README)
    assert "docs/LEGACY_FREEZE.md" in README


def test_freeze_doc_recipe_caps_setuptools_for_the_build():
    recipe = _recipe()

    assert "setuptools<81" in recipe
    assert "--build-constraint" in recipe
    assert "python3.11 -m venv" in recipe
    assert "pip install --upgrade pip" in recipe


def test_freeze_doc_offers_no_bare_install_or_main_branch_poetry_install():
    # Prose may name the failing command to explain it; a code block may not.
    for block in re.findall(r"```.*?```", FREEZE, re.S):
        assert not BARE_INSTALL.search(block), block
    assert "poetry install" not in FREEZE
    assert "--branch v0.46.0" in FREEZE
