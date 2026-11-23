"""Silent-failure fault injectors for Odoo 17."""

from __future__ import annotations

from benchmarks.real_apps.fault_proxy import FaultRule


def silent_bill_post_drop_rule() -> FaultRule:
    """Drop Odoo JSON-RPC vendor-bill create/post and return a deceptive HTTP 200 result."""
    return FaultRule(
        mode="drop_write_return_200",
        path_contains="/jsonrpc",
        methods=("POST",),
        synthetic_body={"jsonrpc": "2.0", "id": 1, "result": 99999},
    )
