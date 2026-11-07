"""Workflow id -> instance."""

from __future__ import annotations

import importlib
from typing import Any

# id -> "module:Class" under benchmark.onboarding_effort.pool
WORKFLOWS: dict[str, str] = {
    "cal-mockmed-triage": "calibration_mockmed:MockMedTriage",
    "tr-ticket-new": "trac:TracNewTicket",
    "tr-ticket-resolve": "trac:TracResolve",
    "tr-triage-loop": "trac:TracTriageLoop",
    "nb-device-add": "netbox:NetBoxDeviceAdd",
    "nb-ip-paginated": "netbox:NetBoxIpPaginated",
    "nb-vlan-conditional": "netbox:NetBoxVlanConditional",
}

_cache: dict[str, Any] = {}


def get(workflow_id: str) -> Any:
    if workflow_id not in _cache:
        module, cls = WORKFLOWS[workflow_id].split(":")
        mod = importlib.import_module(f"benchmark.onboarding_effort.pool.{module}")
        _cache[workflow_id] = getattr(mod, cls)()
    return _cache[workflow_id]


def pool_ids() -> list[str]:
    return [k for k in WORKFLOWS if not k.startswith("cal-")]
