"""Shared test setup: make the repo root importable without pip install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_durable_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test's monotonic run authority independent and disposable."""

    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        str(tmp_path / ".durable-authority.sqlite3"),
    )
