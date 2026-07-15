"""Keep public package metadata aligned with the hosted-rewire boundary."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_identifies_unreleased_supporting_surface() -> None:
    readme = (ROOT / "README.md").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert "Lifecycle: Experimental supporting surface" in readme
    assert "feat/hosted-rewire" in readme
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
