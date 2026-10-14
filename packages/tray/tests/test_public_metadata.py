"""Keep public package metadata aligned with the shipped release boundary."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_identifies_unreleased_supporting_surface() -> None:
    # Collapse wrapping so an assertion matches the sentence rather than the
    # line breaks a reflow happens to leave behind.
    readme = re.sub(r"\s+", " ", (ROOT / "README.md").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    # The README must keep saying that the tray only relays commands and
    # status, and that the tray and openadapt-desktop have not been proven
    # together end to end. These assert the claims, not one particular
    # heading, so the wording can improve without weakening the guard.
    assert "The tray doesn't record or run anything itself." in readme
    assert "doesn't record, compile, replay, or check workflow results" in readme
    assert "haven't been run together end to end" in readme
    assert "openadapt-flow" in readme
    assert "## Limits" in readme
    assert "Development Status :: 2 - Pre-Alpha" in pyproject
    assert 'description = "Menu-bar and system-tray controls and status' in (
        pyproject
    )
    assert "openadapt train" not in readme
    assert "Training Control" not in readme
    assert "monitoring training" not in readme


def test_first_screen_uses_no_internal_process_language() -> None:
    # A stranger reading the README or the PyPI summary should learn what the
    # tray does, not the release process behind it.
    readme = (ROOT / "README.md").read_text().lower()
    pyproject = (ROOT / "pyproject.toml").read_text().lower()
    description = re.search(r'^description = "(.*)"$', pyproject, re.MULTILINE)
    assert description is not None
    for text in (readme, description.group(1)):
        for term in (
            "admission",
            "admitted",
            "signed ledger",
            "evidence authority",
            "product target",
            "lifecycle state",
            "governed",
            "honest",
        ):
            assert term not in text, term


def test_readme_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)

    for link in links:
        target = link.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (ROOT / target).exists(), f"README link does not exist: {link}"
