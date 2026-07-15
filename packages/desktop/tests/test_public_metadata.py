"""Keep public package metadata aligned with the current product boundary."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_identifies_experimental_supporting_surface() -> None:
    readme = (ROOT / "README.md").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package = json.loads((ROOT / "package.json").read_text())

    assert "Lifecycle: Experimental supporting surface" in readme
    assert "openadapt-flow" in readme
    assert "AI training data collection" not in readme
    assert "AI training data collection" not in pyproject["project"]["description"]
    assert "AI training data collection" not in package["description"]
    assert pyproject["project"]["readme"] == "README.md"
    assert pyproject["project"]["scripts"] == {
        "openadapt-desktop": "engine.cli:main"
    }


def test_semantic_release_preserves_pre_one_versions() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    release = pyproject["tool"]["semantic_release"]

    assert release["major_on_zero"] is False
    assert release["allow_zero_version"] is True


def test_readme_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)

    for link in links:
        target = link.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (ROOT / target).exists(), f"README link does not exist: {link}"
