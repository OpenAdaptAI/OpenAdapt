"""Keep public package metadata aligned with the shipped release boundary."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_identifies_unreleased_supporting_surface() -> None:
    # Collapse wrapping so an assertion matches the sentence rather than the
    # line breaks a reflow happens to leave behind.
    readme = re.sub(r"\s+", " ", (ROOT / "README.md").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    # The README must keep saying that this package is a status surface rather
    # than an integrated, generally available desktop product, and it must keep
    # saying that the tray and openadapt-desktop have not been proven together.
    # These assert the claims, not one particular heading, so the wording can
    # improve without weakening the guard.
    assert "status surface, not an integrated desktop product" in readme
    assert "records nothing, compiles nothing, and replays nothing" in readme
    assert "not been validated together end to end" in readme
    assert "openadapt-flow" in readme
    assert "Development Status :: 2 - Pre-Alpha" in pyproject
    assert "Experimental status and launcher companion" in pyproject
    assert "openadapt train" not in readme
    assert "Training Control" not in readme
    assert "monitoring training" not in readme


def test_readme_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)

    for link in links:
        target = link.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (ROOT / target).exists(), f"README link does not exist: {link}"
