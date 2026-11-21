"""Deterministic estimators shown by the portals (and used to build the key).

Neither depends on today's date: the book-value table uses a fixed rating
year.
"""

from __future__ import annotations

RATE_PER_SQFT = {"Frame": 185, "Masonry veneer": 196, "Masonry": 210}


def replacement_cost(square_feet: int, construction: str) -> dict[str, int]:
    """Corvane rebuild estimate: base, code-and-debris allowance, total."""
    base = int(square_feet) * RATE_PER_SQFT.get(construction, 185)
    allowance = round(base * 0.08)
    total = int(round((base + allowance) / 1000.0)) * 1000
    return {"base": base, "allowance": allowance, "total": total}


RATING_YEAR = 2027
MAKE_FACTOR = {
    "Halvorsen Craft": 1.18,
    "Tidelark Boats": 1.0,
    "Brannock Marine": 0.92,
    "Osprey Point": 1.07,
    "Kestrel Bay Boatworks": 1.25,
    "Marlowe Skiffs": 0.85,
}


def book_value(year: int, make: str, length_ft: int, horsepower: int) -> dict[str, int]:
    """Pellbrook book value: low, average and high retail, whole hundreds."""
    age = max(0, RATING_YEAR - int(year))
    raw = int(length_ft) * 2150 * MAKE_FACTOR.get(make, 1.0) * (0.93 ** age) + int(horsepower) * 18
    avg = int(round(raw / 100.0)) * 100
    return {
        "low": int(round(avg * 0.86 / 100.0)) * 100,
        "average": avg,
        "high": int(round(avg * 1.12 / 100.0)) * 100,
    }
