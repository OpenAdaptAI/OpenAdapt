"""System / dashboard route — read-only, imports no sibling package."""

from __future__ import annotations

from fastapi import APIRouter

from ..system import system_report

router = APIRouter(tags=["system"])


@router.get("/system")
def get_system() -> dict:
    return system_report()
