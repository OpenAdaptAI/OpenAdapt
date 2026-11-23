"""Deterministic synthetic contact/opportunity seed for Twenty CRM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpportunityFixture:
    """Synthetic Twenty CRM opportunity + contact record."""

    opportunity_id: str
    name: str
    contact_email: str
    stage: str
    amount_micros: int

    def to_graphql_mutation(self) -> dict[str, Any]:
        return {
            "operationName": "CreateOpportunity",
            "query": (
                "mutation CreateOpportunity($input: OpportunityCreateInput!) { "
                "createOpportunity(data: $input) { id name stage amountMicros contactEmail } }"
            ),
            "variables": {
                "input": {
                    "id": self.opportunity_id,
                    "name": self.name,
                    "contactEmail": self.contact_email,
                    "stage": self.stage,
                    "amountMicros": self.amount_micros,
                }
            },
        }


def default_opportunity() -> OpportunityFixture:
    """Return a deterministic synthetic CRM opportunity fixture."""
    return OpportunityFixture(
        opportunity_id="opp-syn-2001",
        name="Synthetic Enterprise Renewal",
        contact_email="buyer@synthetic.example",
        stage="PROPOSAL",
        amount_micros=50_000_000_000,
    )
