"""The PyPI Python classifiers match ``requires-python``.

The README's PyPI "python versions" badge reads these classifiers. With none,
it renders "missing" in red, so keep them present and in step with the
supported range.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def test_python_classifiers_match_requires_python() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    match = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", project["requires-python"])
    assert match, project["requires-python"]
    supported = {f"3.{minor}" for minor in range(int(match[1]), int(match[2]))}

    listed = {
        classifier.rsplit(" :: ", 1)[1]
        for classifier in project.get("classifiers", [])
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", classifier)
    }
    assert listed == supported
    assert "License :: OSI Approved :: MIT License" in project["classifiers"]
