"""Email intake + workflow-program construction for the AP invoice benchmark.

The intake stage is the deterministic fixture-side document pipeline a real
AP deployment would wire up front: read the vendor request emails from the
INBOX maildir, extract each PDF invoice attachment, parse its fields, and
derive one worklist row per invoice (including the discount-eligibility and
expected 3-way-match route the workflow branches on). ZERO model calls: the
PDF is a deterministic fixture document parsed by a deterministic parser; this
benchmark does not measure OCR or model-based document extraction.

The workflow itself is a Phase-2 workflow-program graph: a LOOP over the
invoice worklist whose body BRANCHES on the match route (post vs. hold) and on
discount eligibility, with every consequential write carried by an
``ApiBinding`` (the api tier), an exact API identity contract, and typed
system-of-record effect contracts routed to per-surface persisted-state reads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import requests

from benchmark.ap_invoice.fixtures import (
    INVOICE_SOURCE_SEEDS,
    SCENARIO_INVOICE_IDS,
    invoice_source_pdf,
)
from benchmark.multiapp_common import (
    build_request_email,
    extract_pdf_lines,
    parse_email,
    parse_kv_lines,
    read_maildir_messages,
    write_maildir_message,
)
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
BATCH_ID = "BATCH-2026-07"


#: invoice_id -> (vendor_id, vendor_name, po_number, amount, terms)
#: ``2/10 NET 30`` terms are discount-eligible at batch time; ``NET 30`` is
#: expired. INV-1003's amount deliberately disagrees with PO-503 (the price
#: mismatch routed to the exception queue). INV-1101 references a PO that does
#: not exist; INV-1201 re-presents an already-posted invoice number.
def seed_inbox(inbox: Path, invoice_ids: list[str]) -> None:
    """Deliver one deterministic vendor request email per invoice."""
    for invoice_id in invoice_ids:
        vendor_id, vendor_name, po_number, amount, _terms = INVOICE_SOURCE_SEEDS[
            invoice_id
        ]
        pdf = invoice_source_pdf(invoice_id)
        message = build_request_email(
            from_addr=f"billing@{vendor_id.lower()}.example.test",
            to_addr="ap@example-corp.test",
            subject=f"Invoice {invoice_id} for {po_number}",
            body=(
                f"Please process the attached invoice {invoice_id} "
                f"({vendor_name}, {amount} USD) against {po_number}."
            ),
            message_id=f"<{invoice_id.lower()}@{vendor_id.lower()}.example.test>",
            pdf_name=f"{invoice_id}.pdf",
            pdf_bytes=pdf,
        )
        write_maildir_message(inbox, f"request-{invoice_id}.eml", message)


def build_worklist(inbox: Path, erp_base: str) -> list[dict[str, str]]:
    """Parse the inbox emails + PDF attachments into workflow worklist rows.

    The route/discount fields drive the workflow-program branches; the
    ``match_status`` field is what the 3-way match effect contract VERIFIES the
    ERP independently concluded (a disagreement halts, never mis-routes). A
    row whose PO is unknown at intake is still routed as ``ok`` on purpose:
    real intake queues carry wrong references, and the measured behavior must
    be a governed halt at entry, not a silent skip.
    """
    po_rows = requests.get(f"{erp_base}/api/purchase_orders", timeout=5.0).json()[
        "records"
    ]
    pos = {row["po_number"]: row for row in po_rows}
    rows: list[dict[str, str]] = []
    for name, raw in sorted(read_maildir_messages(inbox).items()):
        parsed, attachments = parse_email(raw)
        assert attachments, f"fixture email {name} carries no PDF"
        pdf_bytes = attachments[0][1]
        fields = parse_kv_lines(extract_pdf_lines(pdf_bytes))
        invoice_id = fields["invoice"]
        po_number = fields["po"]
        amount = fields["amount"]
        po = pos.get(po_number)
        matched = po is not None and str(po["amount"]) == amount
        route = "ok" if (po is None or matched) else "mismatch"
        eligible = fields["terms"].startswith("2/10") and route == "ok"
        amount_payable = f"{round(float(amount) * 0.98, 2):.2f}" if eligible else amount
        kind = "confirm" if route == "ok" else "hold"
        rows.append(
            {
                "invoice_id": invoice_id,
                "vendor_id": fields["vendor"],
                "po_number": po_number,
                "amount": amount,
                "doc_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "route": route,
                "match_status": "matched" if route == "ok" else "price_mismatch",
                "discount": "eligible" if eligible else "expired",
                "discount_applied": "2/10" if eligible else "none",
                "amount_payable": amount_payable,
                "reply_to": str(parsed["From"]),
                "mail_name": f"{kind}-{invoice_id}.eml",
                "mail_subject": f"{kind.capitalize()}: {invoice_id}",
            }
        )
    return rows


# -- step construction --------------------------------------------------------


def _identity(
    param: str, *, key: str = "record_id", effect_field: Optional[str] = None
) -> ApiIdentityBinding:
    field = effect_field or param
    return ApiIdentityBinding(
        key=key,  # type: ignore[arg-type]
        param=param,
        effect_field=field,
        request_pointers=[f"/body/{field}"],
    )


def _banner_effect(
    id_param: str, event: str, *, surface: str = "banner"
) -> list[Effect]:
    """The NAIVE arm's contract: the app's own painted acknowledgement row."""
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "invoice_id": ValueExpr(param=id_param),
                "event": ValueExpr(literal=event),
            },
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            probe=f"surface={surface}|the app's own acknowledgement banner",
            timeout_s=EFFECT_TIMEOUT_S,
        )
    ]


def _field_effect(
    surface: str,
    match: dict[str, ValueExpr],
    field: str,
    value: ValueExpr,
    probe: str,
) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=match,
        field=field,
        value=value,
        risk="irreversible",
        probe=f"surface={surface}|{probe}",
        timeout_s=EFFECT_TIMEOUT_S,
    )


def _inv_match() -> dict[str, ValueExpr]:
    return {"invoice_id": ValueExpr(param="invoice_id")}


def _steps(arm: str, mailer_base: str, adjacent_invoice: str) -> dict[str, Step]:
    """The ten hardened action leaves, keyed by step id.

    ``arm`` selects the effect contracts: ``governed`` carries persisted-state
    system-of-record contracts (SQL / REST / maildir surfaces, plus the
    adjacent-record collateral guard); ``naive`` carries only the app-painted
    banner acknowledgement (what a screen-echo automation trusts).
    """
    governed = arm == "governed"

    def effects(step_key: str) -> list[Effect]:
        if not governed:
            naive = {
                "draft": _banner_effect("invoice_id", "invoice_created"),
                "attach_doc": _banner_effect("invoice_id", "document_attached"),
                "match": _banner_effect("invoice_id", "match_run"),
                "apply_discount": _banner_effect("invoice_id", "discount_applied"),
                "approve": _banner_effect("invoice_id", "invoice_approved"),
                "schedule_payment": _banner_effect("invoice_id", "payment_scheduled"),
                "send_confirmation": _banner_effect(
                    "invoice_id", "mail_sent", surface="mail_banner"
                ),
                "route_exception": _banner_effect("invoice_id", "exception_routed"),
                "send_hold_notice": _banner_effect(
                    "invoice_id", "mail_sent", surface="mail_banner"
                ),
                "batch_complete": _banner_effect("batch_id", "batch_completed"),
            }
            return naive[step_key]
        inv = _inv_match()
        # Email delivery to the capture point: exactly one NEW conforming
        # message for THIS invoice arrived in the outbox maildir (read from
        # disk; a duplicate send or a dropped send is caught).
        mail_arrival = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "name": ValueExpr(param="mail_name"),
                "arrived": ValueExpr(literal="True"),
            },
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            probe="surface=outbox|one new conforming message in the outbox maildir",
            timeout_s=EFFECT_TIMEOUT_S,
        )
        table = {
            "draft": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={
                        "invoice_id": ValueExpr(param="invoice_id"),
                        "vendor_id": ValueExpr(param="vendor_id"),
                        "po_number": ValueExpr(param="po_number"),
                    },
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=invoices|exactly one new draft for this "
                    "invoice/vendor/PO",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
                _field_effect(
                    "invoices",
                    inv,
                    "amount",
                    ValueExpr(param="amount"),
                    "the persisted invoice amount read back",
                ),
            ],
            "attach_doc": [
                _field_effect(
                    "invoices",
                    inv,
                    "doc_sha256",
                    ValueExpr(param="doc_sha256"),
                    "the stored document digest equals the received PDF's",
                )
            ],
            "match": [
                _field_effect(
                    "invoices",
                    inv,
                    "status",
                    ValueExpr(param="match_status"),
                    "the ERP's own 3-way match verdict equals the routed one",
                )
            ],
            "apply_discount": [
                _field_effect(
                    "invoices",
                    inv,
                    "discount_applied",
                    ValueExpr(param="discount_applied"),
                    "the discount terms persisted",
                ),
                _field_effect(
                    "invoices",
                    inv,
                    "amount_payable",
                    ValueExpr(param="amount_payable"),
                    "the discounted payable amount persisted",
                ),
            ],
            "approve": [
                _field_effect(
                    "invoices",
                    inv,
                    "status",
                    ValueExpr(literal="approved"),
                    "the target invoice is approved",
                ),
                _field_effect(
                    "invoices",
                    inv,
                    "amount_payable",
                    ValueExpr(param="amount_payable"),
                    "the approved payable amount read back",
                ),
                # Collateral guard: the ADJACENT grid row must be untouched.
                _field_effect(
                    "invoices",
                    {"invoice_id": ValueExpr(literal=adjacent_invoice)},
                    "status",
                    ValueExpr(literal="draft"),
                    "the adjacent invoice was NOT collaterally approved",
                ),
            ],
            "schedule_payment": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=inv,
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=payments|exactly one new scheduled payment",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
                _field_effect(
                    "payments",
                    inv,
                    "amount",
                    ValueExpr(param="amount_payable"),
                    "the scheduled payment amount read back",
                ),
            ],
            "send_confirmation": [mail_arrival],
            "route_exception": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=inv,
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=exceptions|exactly one new AP exception entry",
                    timeout_s=EFFECT_TIMEOUT_S,
                ),
                _field_effect(
                    "invoices",
                    inv,
                    "status",
                    ValueExpr(literal="held"),
                    "the mismatched invoice is held, not payable",
                ),
            ],
            "send_hold_notice": [mail_arrival.model_copy(deep=True)],
            "batch_complete": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"batch_id": ValueExpr(param="batch_id")},
                    expected_count=1,
                    count_new_only=True,
                    risk="irreversible",
                    probe="surface=batches|the batch completion record",
                    timeout_s=EFFECT_TIMEOUT_S,
                )
            ],
        }
        return table[step_key]

    def step(
        step_id: str,
        intent: str,
        binding: ApiBinding,
    ) -> Step:
        path_effects = effects(step_id)
        binding.effects = [effect.model_copy(deep=True) for effect in path_effects]
        return Step(
            id=step_id,
            intent=intent,
            action=ActionKind.KEY,
            key="Enter",
            risk="irreversible",
            identity_armed=governed,
            effects=path_effects,
            api_binding=binding,
        )

    # The email step's identity contract binds the per-invoice message name:
    # the request's /body/name and the arrival effect's `name` selector must
    # both carry the same run parameter before the send is allowed.
    mail_identity = [_identity("mail_name", effect_field="name")] if governed else []
    inv_identity = [_identity("invoice_id")] if governed else []
    return {
        "draft": step(
            "draft",
            "enter the invoice into the ERP (UI gateway; no entry API exists)",
            ApiBinding(
                method="POST",
                url_template="/ui/invoice/new",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "vendor_id": "{vendor_id}",
                    "po_number": "{po_number}",
                    "amount": "{amount}",
                },
                timeout_s=5.0,
                identity=(
                    [
                        _identity("invoice_id"),
                        _identity("vendor_id", key="secondary_identifier"),
                    ]
                    if governed
                    else []
                ),
            ),
        ),
        "attach_doc": step(
            "attach_doc",
            "attach the received PDF's digest to the invoice (API)",
            ApiBinding(
                method="POST",
                url_template="/api/invoice/document",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "doc_sha256": "{doc_sha256}",
                },
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "match": step(
            "match",
            "run the 3-way match (invoice vs PO vs receipts) (API)",
            ApiBinding(
                method="POST",
                url_template="/api/invoice/match",
                body_template={"invoice_id": "{invoice_id}"},
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "apply_discount": step(
            "apply_discount",
            "apply the early-payment discount (API; eligible branch only)",
            ApiBinding(
                method="POST",
                url_template="/api/invoice/discount",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "discount_applied": "{discount_applied}",
                    "amount_payable": "{amount_payable}",
                },
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "approve": step(
            "approve",
            "approve the invoice for payment (UI gateway)",
            ApiBinding(
                method="POST",
                url_template="/ui/invoice/approve",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "amount_payable": "{amount_payable}",
                },
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "schedule_payment": step(
            "schedule_payment",
            "schedule the payment run entry (API)",
            ApiBinding(
                method="POST",
                url_template="/api/payment",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "amount": "{amount_payable}",
                },
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "send_confirmation": step(
            "send_confirmation",
            "email the vendor a processing confirmation (mail gateway)",
            ApiBinding(
                method="POST",
                url_template=f"{mailer_base}/api/send",
                body_template={
                    "name": "{mail_name}",
                    "invoice_id": "{invoice_id}",
                    "to": "{reply_to}",
                    "subject": "{mail_subject}",
                    "body": "Invoice {invoice_id} was matched, approved, and "
                    "scheduled for payment of {amount_payable} USD.",
                },
                timeout_s=5.0,
                identity=mail_identity,
            ),
        ),
        "route_exception": step(
            "route_exception",
            "route the mismatched invoice to the AP exception queue (API)",
            ApiBinding(
                method="POST",
                url_template="/api/exception",
                body_template={
                    "invoice_id": "{invoice_id}",
                    "reason": "price mismatch vs {po_number}",
                },
                timeout_s=5.0,
                identity=inv_identity,
            ),
        ),
        "send_hold_notice": step(
            "send_hold_notice",
            "email the vendor that the invoice is held (mail gateway)",
            ApiBinding(
                method="POST",
                url_template=f"{mailer_base}/api/send",
                body_template={
                    "name": "{mail_name}",
                    "invoice_id": "{invoice_id}",
                    "to": "{reply_to}",
                    "subject": "{mail_subject}",
                    "body": "Invoice {invoice_id} does not match {po_number} "
                    "and was routed to the AP exception queue.",
                },
                timeout_s=5.0,
                identity=mail_identity,
            ),
        ),
        "batch_complete": step(
            "batch_complete",
            "record the batch completion (API)",
            ApiBinding(
                method="POST",
                url_template="/api/batch/complete",
                body_template={"batch_id": "{batch_id}", "processed": "{processed}"},
                timeout_s=5.0,
                identity=[_identity("batch_id")] if governed else [],
            ),
        ),
    }


def _guard(param: str, value: str) -> Predicate:
    return Predicate(kind=PredicateKind.PARAM_EQUALS, param=param, value=value)


def build_workflow(
    arm: str,
    *,
    mailer_base: str,
    adjacent_invoice: str,
    processed: int,
) -> Workflow:
    """The complete AP intake workflow program for one arm."""
    steps = _steps(arm, mailer_base, adjacent_invoice)

    body = ProgramGraph(
        entry="enter_invoice",
        states={
            "enter_invoice": State(
                id="enter_invoice",
                kind=StateKind.ACTION,
                step=steps["draft"],
                transitions=[Transition(target="attach_document")],
            ),
            "attach_document": State(
                id="attach_document",
                kind=StateKind.ACTION,
                step=steps["attach_doc"],
                transitions=[Transition(target="run_match")],
            ),
            "run_match": State(
                id="run_match",
                kind=StateKind.ACTION,
                step=steps["match"],
                transitions=[Transition(target="route_on_match")],
            ),
            "route_on_match": State(
                id="route_on_match",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=_guard("route", "ok"),
                        target="discount_gate",
                        label="3-way match clean: post it",
                    ),
                    Transition(
                        guard=_guard("route", "mismatch"),
                        target="hold_invoice",
                        label="price mismatch: exception queue",
                    ),
                ],
            ),
            "discount_gate": State(
                id="discount_gate",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=_guard("discount", "eligible"),
                        target="take_discount",
                        label="early-payment terms still open",
                    ),
                    Transition(
                        guard=_guard("discount", "expired"),
                        target="approve_invoice",
                        label="discount window expired: pay net",
                    ),
                ],
            ),
            "take_discount": State(
                id="take_discount",
                kind=StateKind.ACTION,
                step=steps["apply_discount"],
                transitions=[Transition(target="approve_invoice")],
            ),
            "approve_invoice": State(
                id="approve_invoice",
                kind=StateKind.ACTION,
                step=steps["approve"],
                transitions=[Transition(target="pay_invoice")],
            ),
            "pay_invoice": State(
                id="pay_invoice",
                kind=StateKind.ACTION,
                step=steps["schedule_payment"],
                transitions=[Transition(target="confirm_by_email")],
            ),
            "confirm_by_email": State(
                id="confirm_by_email",
                kind=StateKind.ACTION,
                step=steps["send_confirmation"],
                transitions=[Transition(target="row_done")],
            ),
            "hold_invoice": State(
                id="hold_invoice",
                kind=StateKind.ACTION,
                step=steps["route_exception"],
                transitions=[Transition(target="notify_hold")],
            ),
            "notify_hold": State(
                id="notify_hold",
                kind=StateKind.ACTION,
                step=steps["send_hold_notice"],
                transitions=[Transition(target="row_done")],
            ),
            "row_done": State(
                id="row_done", kind=StateKind.TERMINAL, outcome="success"
            ),
        },
    )

    top = ProgramGraph(
        entry="process_invoices",
        states={
            "process_invoices": State(
                id="process_invoices",
                kind=StateKind.LOOP,
                loop=LoopSpec(
                    relation="invoices",
                    body="invoice_body",
                    var="invoice",
                    max_iterations=50,
                ),
                transitions=[Transition(target="finish_batch")],
            ),
            "finish_batch": State(
                id="finish_batch",
                kind=StateKind.ACTION,
                step=steps["batch_complete"],
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )

    return Workflow(
        name=f"ap-invoice-{arm}",
        steps=[],
        program=top,
        subflows={"invoice_body": body},
        params={"batch_id": BATCH_ID, "processed": str(processed)},
    )


def required_identity_step_ids(workflow: Workflow) -> tuple[str, ...]:
    from openadapt_flow.traversal import iter_workflow_steps

    return tuple(step.id for step in iter_workflow_steps(workflow))


def scenario_invoices(scenario: str) -> list[str]:
    """Which seeded request emails a scenario's inbox carries."""
    return list(SCENARIO_INVOICE_IDS[scenario])


def scenario_faults(scenario: str) -> list[str]:
    return {
        "healthy": [],
        "missing_po": [],
        "duplicate_invoice": [],
        "collateral_approve": ["collateral_adjacent_on_approve"],
        "payment_confirm_outage": ["payments_read_down_after_write"],
    }[scenario]


def scenario_seed_duplicate(scenario: str) -> bool:
    return scenario == "duplicate_invoice"
