"""Shared metric implementations for evaluation.

Single source of truth for metric functions used by both the client-side
evaluator (evaluation/client.py) and the server-side evaluate endpoint
(server/evaluate_endpoint.py).
"""

from __future__ import annotations

from typing import Any


def exact_match(result: Any, expected: Any, **options) -> float:
    """Exact string/value match."""
    if result is None or expected is None:
        raise ValueError("exact_match requires observed and expected values")
    if result == expected:
        return 1.0
    if str(result).strip() == str(expected).strip():
        return 1.0
    return 0.0


def fuzzy_match(
    result: Any, expected: Any, threshold: float = 0.8, **options
) -> float:
    """Fuzzy string matching using rapidfuzz (character-level Levenshtein).

    Falls back to substring containment when rapidfuzz is not installed.
    """
    if result is None or expected is None or str(expected).strip() == "":
        raise ValueError("fuzzy_match requires a non-empty expected value")
    try:
        from rapidfuzz import fuzz

        score = fuzz.ratio(str(result), str(expected)) / 100.0
        return 1.0 if score >= threshold else score
    except ImportError:
        result_str = str(result).lower()
        expected_str = str(expected).lower()
        if expected_str in result_str or result_str in expected_str:
            return 0.8
        return 0.0


def contains(result: Any, expected: Any, **options) -> float:
    """Check if result contains expected (case-insensitive)."""
    if result is None or expected is None or str(expected).strip() == "":
        raise ValueError("contains requires a non-empty expected value")
    result_str = str(result).lower()
    expected_str = str(expected).lower()
    return 1.0 if expected_str in result_str else 0.0


def boolean(result: Any, expected: Any, **options) -> float:
    """Boolean equality check."""
    return 1.0 if _parse_boolean(result) == _parse_boolean(expected) else 0.0


def file_exists(result: Any, expected: Any, **options) -> float:
    """Compare explicit remote file-existence evidence."""
    del options
    return 1.0 if _parse_boolean(result) == _parse_boolean(expected) else 0.0


def _parse_boolean(value: Any) -> bool:
    """Parse an explicit Boolean value without Python truthiness."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"expected an explicit boolean value, got {value!r}")


def get_metric(name: str):
    """Look up a metric function by name. Returns None if not found."""
    return _METRICS.get(name)


_METRICS = {
    "exact_match": exact_match,
    "fuzzy_match": fuzzy_match,
    "contains": contains,
    "boolean": boolean,
    "file_exists": file_exists,
}
