"""Spreadsheet intake + workflow-program construction for the O2C benchmark.

The intake stage is the deterministic compare pre-pass a reconciliation bot
runs up front: read system A's exported spreadsheet from the shared folder,
read system B's ledger API, and derive one worklist row per order with its
disposition (``match`` / ``adjust`` / ``missing``), the observed prior amount,
and the signed delta. ZERO model calls. The pre-pass deliberately mirrors a
NAIVE compare (first ledger entry wins; trusts the snapshot): the safety
question this benchmark measures is whether the ENGINE still refuses to act
when that worklist turns out to be wrong at act time (duplicate rows, a stale
snapshot, a missing record).

The workflow is a Phase-2 program graph: a LOOP over the order worklist whose
body BRANCHES three ways on disposition, including an explicit halt terminal
for the missing-record path (never auto-create a ledger entry), and a
write-back of every processed row plus a summary row to the results
spreadsheet on system A.
"""

from __future__ import annotations

import csv
from pathlib import Path

import requests

from benchmark.o2c_recon.fixtures import SCENARIO_ORDER_IDS
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    ApiIdentityBinding,
    LoopSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

EFFECT_TIMEOUT_S = 0.25
SUMMARY_ID = "SUMMARY"


def scenario_orders(scenario: str) -> list[str]:
    return list(SCENARIO_ORDER_IDS[scenario])


def scenario_faults(scenario: str) -> dict[str, list[str]]:
    """Fault switches per fixture application."""
    return {
        "healthy": {"billing": [], "ledger": []},
        "missing_in_ledger": {"billing": [], "ledger": []},
        "ambiguous_duplicate": {"billing": [], "ledger": []},
        "stale_snapshot": {"billing": [], "ledger": ["stale_snapshot"]},
        "phantom_writeback": {"billing": ["drop_writeback"], "ledger": []},
    }[scenario]


def build_worklist(export_path: Path, ledger_base: str) -> list[dict[str, str]]:
    """The compare pre-pass: exported spreadsheet vs. the ledger read API."""
    with export_path.open(newline="", encoding="utf-8") as handle:
        exported = list(csv.DictReader(handle))
    ledger_rows = requests.get(f"{ledger_base}/api/ledger", timeout=5.0).json()[
        "records"
    ]
    rows: list[dict[str, str]] = []
    for order in exported:
        order_id = order["order_id"]
        entries = [e for e in ledger_rows if e["order_id"] == order_id]
        if not entries:
            disposition, prior, delta = "missing", "", ""
        else:
            # NAIVE compare on purpose: first entry wins, snapshot trusted.
            prior = str(entries[0]["amount_posted"])
            billed = float(order["amount_billed"])
            observed = float(prior)
            if abs(billed - observed) < 0.005:
                disposition, delta = "match", ""
            else:
                disposition = "adjust"
                delta = f"{billed - observed:+.2f}"
        rows.append(
            {
                "order_id": order_id,
                "customer": order["customer"],
                "amount_billed": order["amount_billed"],
                "amount_prior": prior,
                "delta": delta,
                "disposition": disposition,
                "reason": f"billing reconciliation {order['period']}",
            }
        )
    return rows


def _identity(
    param: str, effect_field: str, *, key: str = "record_id"
) -> ApiIdentityBinding:
    return ApiIdentityBinding(
        key=key,  # type: ignore[arg-type]
        param=param,
        effect_field=effect_field,
        request_pointers=[f"/body/{effect_field}"],
    )


def _banner_effect(id_param: str, event: str, surface: str) -> list[Effect]:
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "order_id": ValueExpr(param=id_param),
                "event": ValueExpr(literal=event),
            },
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            probe=f"surface={surface}|the app's own acknowledgement banner",
            timeout_s=EFFECT_TIMEOUT_S,
        )
    ]


def _steps(arm: str, billing_base: str) -> dict[str, Step]:
    governed = arm == "governed"
    order_match = {"order_id": ValueExpr(param="order_id")}

    def effects(step_key: str) -> list[Effect]:
        if not governed:
            return {
                "enter_adjustment": _banner_effect(
                    "order_id", "adjustment_entered", "ledger_banner"
                ),
                "mark_reconciled": _banner_effect(
                    "order_id", "marked_reconciled", "ledger_banner"
                ),
                "writeback_row": _banner_effect(
                    "order_id", "writeback_recorded", "billing_banner"
                ),
                "writeback_summary": _banner_effect(
                    "summary_id", "writeback_recorded", "billing_banner"
                ),
            }[step_key]
        table = {
            "enter_adjustment": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=dict(order_match),
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=adjustments|exactly one new adjustment "
                    "for this order",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    match={
                        "order_id": ValueExpr(param="order_id"),
                        "customer": ValueExpr(param="customer"),
                    },
                    field="amount_posted",
                    value=ValueExpr(param="amount_billed"),
                    risk="irreversible",
                    probe="surface=ledger|the adjusted posted amount equals "
                    "the billed amount, on the right customer's entry",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
            ],
            "mark_reconciled": [
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    match=dict(order_match),
                    field="status",
                    value=ValueExpr(literal="reconciled"),
                    risk="irreversible",
                    probe="surface=ledger|the entry is marked reconciled",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    match=dict(order_match),
                    field="amount_posted",
                    value=ValueExpr(param="amount_billed"),
                    risk="irreversible",
                    probe="surface=ledger|the reconciled amount read back",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
            ],
            "writeback_row": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={
                        "order_id": ValueExpr(param="order_id"),
                        "disposition": ValueExpr(param="disposition"),
                    },
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=results|exactly one new result row in the "
                    "written-back spreadsheet",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
            ],
            "writeback_summary": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={
                        "order_id": ValueExpr(param="summary_id"),
                        "disposition": ValueExpr(literal="summary"),
                    },
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=results|the summary row in the written-back "
                    "spreadsheet",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
            ],
        }
        return table[step_key]

    order_identity = (
        [
            _identity("order_id", "order_id"),
            _identity("customer", "customer", key="subject_name"),
        ]
        if governed
        else []
    )
    return {
        "enter_adjustment": Step(
            id="enter_adjustment",
            intent="enter the billing adjustment in the ledger (UI gateway; "
            "no adjustment API exists)",
            action=ActionKind.KEY,
            key="Enter",
            risk="irreversible",
            identity_armed=governed,
            effects=effects("enter_adjustment"),
            api_binding=ApiBinding(
                method="POST",
                url_template="/ui/adjustment/new",
                body_template={
                    "order_id": "{order_id}",
                    "customer": "{customer}",
                    "delta": "{delta}",
                    "expected_prior": "{amount_prior}",
                    "expected_new": "{amount_billed}",
                    "reason": "{reason}",
                },
                timeout_s=5.0,
                identity=order_identity,
            ),
        ),
        "mark_reconciled": Step(
            id="mark_reconciled",
            intent="mark the ledger entry reconciled (API)",
            action=ActionKind.KEY,
            key="Enter",
            risk="irreversible",
            identity_armed=governed,
            effects=effects("mark_reconciled"),
            api_binding=ApiBinding(
                method="POST",
                url_template="/api/reconcile/mark",
                body_template={
                    "order_id": "{order_id}",
                    "amount": "{amount_billed}",
                },
                timeout_s=5.0,
                identity=[_identity("order_id", "order_id")] if governed else [],
            ),
        ),
        "writeback_row": Step(
            id="writeback_row",
            intent="write the order's result row back to the results "
            "spreadsheet on system A",
            action=ActionKind.KEY,
            key="Enter",
            risk="irreversible",
            identity_armed=governed,
            effects=effects("writeback_row"),
            api_binding=ApiBinding(
                method="POST",
                url_template=f"{billing_base}/api/workbook/writeback",
                body_template={
                    "order_id": "{order_id}",
                    "disposition": "{disposition}",
                    "delta": "{delta}",
                    "status": "done",
                },
                timeout_s=5.0,
                identity=[_identity("order_id", "order_id")] if governed else [],
            ),
        ),
        "writeback_summary": Step(
            id="writeback_summary",
            intent="write the batch summary row to the results spreadsheet",
            action=ActionKind.KEY,
            key="Enter",
            risk="irreversible",
            identity_armed=governed,
            effects=effects("writeback_summary"),
            api_binding=ApiBinding(
                method="POST",
                url_template=f"{billing_base}/api/workbook/writeback",
                body_template={
                    "order_id": "{summary_id}",
                    "disposition": "summary",
                    "delta": "",
                    "status": "{summary_status}",
                },
                timeout_s=5.0,
                identity=([_identity("summary_id", "order_id")] if governed else []),
            ),
        ),
    }


def _guard(param: str, value: str) -> Predicate:
    return Predicate(kind=PredicateKind.PARAM_EQUALS, param=param, value=value)


def build_workflow(arm: str, *, billing_base: str, processed: int) -> Workflow:
    steps = _steps(arm, billing_base)
    body = ProgramGraph(
        entry="route_disposition",
        states={
            "route_disposition": State(
                id="route_disposition",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=_guard("disposition", "match"),
                        target="mark_state",
                        label="amounts agree: mark reconciled",
                    ),
                    Transition(
                        guard=_guard("disposition", "adjust"),
                        target="adjust_state",
                        label="amounts differ: enter adjustment first",
                    ),
                    Transition(
                        guard=_guard("disposition", "missing"),
                        target="halt_missing",
                        label="no ledger entry: human review",
                    ),
                ],
            ),
            "adjust_state": State(
                id="adjust_state",
                kind=StateKind.ACTION,
                step=steps["enter_adjustment"],
                transitions=[Transition(target="mark_state")],
            ),
            "mark_state": State(
                id="mark_state",
                kind=StateKind.ACTION,
                step=steps["mark_reconciled"],
                transitions=[Transition(target="writeback_state")],
            ),
            "writeback_state": State(
                id="writeback_state",
                kind=StateKind.ACTION,
                step=steps["writeback_row"],
                transitions=[Transition(target="row_done")],
            ),
            "halt_missing": State(
                id="halt_missing",
                kind=StateKind.TERMINAL,
                outcome="halt",
                reason="order is billed but has no ledger entry; refusing to "
                "auto-create a ledger record (human review required)",
            ),
            "row_done": State(
                id="row_done", kind=StateKind.TERMINAL, outcome="success"
            ),
        },
    )
    top = ProgramGraph(
        entry="reconcile_orders",
        states={
            "reconcile_orders": State(
                id="reconcile_orders",
                kind=StateKind.LOOP,
                loop=LoopSpec(
                    relation="orders",
                    body="order_body",
                    var="order",
                    max_iterations=100,
                ),
                transitions=[Transition(target="write_summary")],
            ),
            "write_summary": State(
                id="write_summary",
                kind=StateKind.ACTION,
                step=steps["writeback_summary"],
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    return Workflow(
        name=f"o2c-recon-{arm}",
        steps=[],
        program=top,
        subflows={"order_body": body},
        params={
            "summary_id": SUMMARY_ID,
            "summary_status": f"{processed} orders processed",
        },
    )


def required_identity_step_ids(workflow: Workflow) -> tuple[str, ...]:
    from openadapt_flow.traversal import iter_workflow_steps

    return tuple(step.id for step in iter_workflow_steps(workflow))
