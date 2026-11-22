"""The outcome vocabulary (openadapt_types/vocabulary/outcomes.json) as P7 cases."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=1)
def outcomes() -> dict[str, Any]:
    text = (
        resources.files("openadapt_types")
        .joinpath("vocabulary/outcomes.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def rows(source: str) -> dict[str, dict[str, Any]]:
    return outcomes()["sources"][source]["values"]


def result_of(source: str, value: Any) -> Any:
    row = rows(source).get(value)
    return row["result"] if row else None
