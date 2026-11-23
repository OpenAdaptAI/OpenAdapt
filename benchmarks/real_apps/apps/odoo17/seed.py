"""Deterministic synthetic vendor-bill seed for Odoo 17."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VendorBillFixture:
    """Synthetic vendor bill (``account.move`` with ``move_type='in_invoice'``)."""

    ref: str
    partner_name: str
    amount_total: float
    currency: str
    state: str = "posted"

    def to_jsonrpc_create_payload(
        self,
        *,
        db: str = "odoo_synthetic",
        uid: int = 2,
        credential: str = "odoo_synthetic_pw",
        request_id: int = 1,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "id": request_id,
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    db,
                    uid,
                    credential,
                    "account.move",
                    "create",
                    [
                        {
                            "move_type": "in_invoice",
                            "ref": self.ref,
                            "partner_name": self.partner_name,
                            "amount_total": self.amount_total,
                            "currency": self.currency,
                            "state": self.state,
                        }
                    ],
                ],
            },
        }


def default_vendor_bill() -> VendorBillFixture:
    """Return a deterministic synthetic vendor bill fixture."""
    return VendorBillFixture(
        ref="BILL-SYN-2026-001",
        partner_name="Synthetic Supply Co.",
        amount_total=1250.50,
        currency="USD",
        state="posted",
    )
