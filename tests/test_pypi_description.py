"""The package description must not freeze a lifecycle state onto PyPI.

PyPI keeps a release's description forever, while the README's generated
Production lifecycle block follows the live signed record. The build hook in
`hatch_build.py` swaps that block for a pointer to the live record, so the
wheel and source archive never carry an admission state.
"""

from __future__ import annotations

import re
import runpy
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
LIVE_RECORD_URL = "https://docs.openadapt.ai/production-lifecycle.json"
STATE_PHRASES = re.compile(
    r"not actively admitted|active (target )?admissions?|is the admitted runtime",
    re.IGNORECASE,
)


def _hook() -> dict:
    return runpy.run_path(str(ROOT / "hatch_build.py"))


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_still_carries_the_generated_lifecycle_block() -> None:
    readme = _readme()
    assert "<!-- BEGIN PRODUCTION LIFECYCLE -->" in readme
    assert STATE_PHRASES.search(readme)


def test_package_description_drops_the_lifecycle_state() -> None:
    description = _hook()["package_description"](_readme())

    assert "PRODUCTION LIFECYCLE" not in description
    assert not STATE_PHRASES.search(description), STATE_PHRASES.search(description)
    assert LIVE_RECORD_URL in description
    readme = _readme()
    before, _, rest = readme.partition("<!-- BEGIN PRODUCTION LIFECYCLE -->")
    _, _, after = rest.partition("<!-- END PRODUCTION LIFECYCLE -->")
    assert description.startswith(before)
    assert description.endswith(after)


def test_package_description_refuses_a_broken_block() -> None:
    describe = _hook()["package_description"]
    with pytest.raises(ValueError, match="one complete"):
        describe("<!-- BEGIN PRODUCTION LIFECYCLE -->\nno end marker\n")
    block = (
        "<!-- BEGIN PRODUCTION LIFECYCLE -->\nx\n<!-- END PRODUCTION LIFECYCLE -->\n"
    )
    with pytest.raises(ValueError, match="one complete"):
        describe(block + block)


def test_build_uses_the_hook_for_the_package_description() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "readme" not in project["project"]
    assert "readme" in project["project"]["dynamic"]
    assert "custom" in project["tool"]["hatch"]["metadata"]["hooks"]
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/hatch_build.py" in sdist
