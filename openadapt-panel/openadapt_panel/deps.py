"""Shared helpers for route handlers."""

from __future__ import annotations

from importlib.util import find_spec

from fastapi import HTTPException, status


def ensure_installed(module_name: str, dist_name: str) -> None:
    """Raise 503 if a sibling package is not installed.

    Call this *before* importing the sibling. It distinguishes "not installed"
    (find_spec is None → 503, actionable message) from "installed but broken":
    in the latter case find_spec succeeds, the caller's import runs, and any
    phantom/renamed symbol surfaces as a real 500 with the true traceback
    instead of being masked as "not installed" (the #999 bug class).
    """
    if find_spec(module_name) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{dist_name} is not installed. "
                f"Install it with: pip install {dist_name}"
            ),
        )
