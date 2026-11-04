"""Hooks for the Frappe Lending OpenEnv environment (environments/openenv/frappe_lending).

Restore the SHA-256-bound baseline before each episode and read the target
Loan Application rows back by direct read-only SQL. Run from a repository
checkout with ``PYTHONPATH`` set to ``packages/flow`` after the fixture is up
(README.md in this directory).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from benchmark.frappe_lending.fixture import FrappeFixture

HERE = Path(__file__).resolve().parent
_fixture: Optional[FrappeFixture] = None


def fixture() -> FrappeFixture:
    global _fixture
    if _fixture is None:
        _fixture = FrappeFixture(os.environ.get("FRAPPE_FIXTURE_STATE", HERE / "state"))
    return _fixture


def reset(**_: Any) -> None:
    fixture().reset()


def records() -> list[dict[str, Any]]:
    return fixture().db_records()
