"""Replacement-cost formulas used by the portal estimator tools.

Both functions are deterministic in their inputs and return whole dollars.
"""

from __future__ import annotations

_BASE = {"Economy": 145, "Standard": 185, "Custom": 240, "Premium": 310}
_STORIES = {"1": 1.0, "1.5": 0.97, "2": 0.95, "3": 0.93}


def harborline_rce(sqft: int, year_built: int, stories: str, quality: str) -> int:
    age = 1.08 if year_built < 1950 else 1.04 if year_built < 1980 else 1.0
    raw = sqft * _BASE[quality] * _STORIES[str(stories)] * age
    return int(round(raw / 10.0)) * 10


_PC_BASE = {"Builder grade": 172, "Semi-custom": 214, "Custom": 268, "Luxury": 355}
_PC_CONSTR = {"Wood frame": 1.0, "Brick / masonry": 1.07, "Steel / concrete": 1.12, "Log": 1.15}


def pinecrest_rebuild(sqft: int, year_built: int, construction: str, finish: str) -> int:
    age = 1.06 if year_built < 1960 else 1.0
    raw = sqft * _PC_BASE[finish] * _PC_CONSTR[construction] * age
    return int(round(raw / 100.0)) * 100
