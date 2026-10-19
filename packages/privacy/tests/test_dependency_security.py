"""Dependency contract for the default PHI scrubber."""

from __future__ import annotations

import re
from pathlib import Path


def test_presidio_extra_does_not_install_transformers() -> None:
    root = Path(__file__).resolve().parents[1]
    project_source = (root / "pyproject.toml").read_text()
    presidio_extra = re.search(r"(?ms)^presidio\s*=\s*\[(.*?)^\]$", project_source)
    assert presidio_extra is not None
    assert "transformers" not in presidio_extra.group(1).lower()
    provider_source = (root / "openadapt_privacy/providers/presidio.py").read_text()
    assert "spacy_transformers" not in provider_source
