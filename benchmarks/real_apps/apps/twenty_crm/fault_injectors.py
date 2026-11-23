"""Optimistic-UI rollback fault injector for Twenty CRM."""

from __future__ import annotations

from benchmarks.real_apps.fault_proxy import FaultRule


def optimistic_ui_rollback_rule(opportunity_id: str, name: str) -> FaultRule:
    """Return a fake GraphQL 200 mutation response while dropping DB persistence."""
    return FaultRule(
        mode="optimistic_ui_rollback",
        path_contains="/graphql",
        methods=("POST",),
        synthetic_body={
            "data": {
                "createOpportunity": {
                    "id": opportunity_id,
                    "name": name,
                    "stage": "PROPOSAL",
                    "optimistic": True,
                }
            }
        },
    )
