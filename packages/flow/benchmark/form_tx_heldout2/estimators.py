"""Figures computed by the portals' calculator tools.

Every function is deterministic in its inputs and returns whole dollars.
"""

from __future__ import annotations

GLASSWING_ROOM_RATE = {"Lightly": 3900, "Typically": 6400, "Fully": 9300}


def glasswing_belongings(rooms: int, furnished: str, valuables: int) -> int:
    """Belongings calculator: per-room rate by furnishing level, plus valuables, to the $100."""
    raw = int(rooms) * GLASSWING_ROOM_RATE[furnished] + int(valuables)
    return int(round(raw / 100.0)) * 100
