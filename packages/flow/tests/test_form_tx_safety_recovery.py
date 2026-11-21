"""Refusal regressions for form intent, human decisions, and session evidence."""

from __future__ import annotations

import pytest

from openadapt_flow.form_tx.reconciler import FormReconciler, ReconcilerSession
from openadapt_flow.form_tx.session_lease import LeaseState, WarmSessionLease
from openadapt_flow.form_tx.surface import match_anchor_in_snapshot
from openadapt_flow.form_tx.types import (
    FieldAnchorSpec,
    FormHaltKind,
    FormSnapshot,
    HumanDecisionAction,
    HumanResolutionInput,
    ObservedControl,
)
from tests.test_form_tx_lease_and_compiler import DeterministicFakeSurface
from tests.test_form_tx_reconciler_and_receipts import (
    FakeStatefulFormSurface,
    _build_dossier,
    _build_pack,
)


@pytest.mark.parametrize("answer", ["No", "Yes", "unknown", "false"])
def test_nonempty_required_answer_still_needs_grounding(answer: str) -> None:
    control = ObservedControl(
        control_id="unknown",
        dom_selector="#unknown",
        label_text="Unmapped question",
        required=True,
        current_value=answer,
    )
    snapshot = FormSnapshot(
        url_origin_and_path="https://portal.example/question", controls=(control,)
    )
    session = ReconcilerSession(_build_pack(), _build_dossier())
    halts = FormReconciler()._collect_ungrounded_required_halts(session, snapshot, ())
    assert len(halts) == 1
    assert halts[0].kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD


@pytest.mark.parametrize(
    "flag", ["unknown", "No, needs review", "pending", [], {"yes": True}]
)
def test_ambiguous_modal_permission_never_confirms(flag: object) -> None:
    from openadapt_flow.form_tx.reconciler import _is_truthy_flag

    assert _is_truthy_flag(flag) is False


def test_stale_prefill_value_cannot_be_accepted() -> None:
    surface = FakeStatefulFormSurface(prefill_values={"prior_claims_count": "2"})
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    result = reconciler.run_until_halt_or_done(session)
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    surface.values["prior_claims_count"] = "3"
    with pytest.raises(ValueError, match="value changed"):
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id=halt.halt_id,
                action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                actor_id="operator",
                idempotency_key="old-value",
            ),
        )
    assert session.human_resolutions == []
    assert surface.committed_record is None


def test_replaced_entity_at_same_row_selector_cannot_be_dismissed() -> None:
    surface = FakeStatefulFormSurface(
        entity_rows=[
            {
                "full_name": "Unknown Person",
                "relationship": "Spouse",
                "dob": "1980-01-01",
            }
        ]
    )
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    result = reconciler.run_until_halt_or_done(session)
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_ENTITY_CONFLICT
    surface.household_rows[0]["full_name"] = "Replacement Person"
    with pytest.raises(ValueError, match="entity row changed"):
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id=halt.halt_id,
                action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
                actor_id="operator",
                idempotency_key="old-row",
            ),
        )
    assert len(surface.household_rows) == 1
    assert session.human_resolutions == []


@pytest.mark.parametrize(
    "change",
    [
        {"label_patterns": ("Different question",)},
        {"role": "checkbox"},
        {"section_patterns": ("Other person",)},
        {"name_or_id_patterns": ("different_name",)},
    ],
)
def test_exact_css_does_not_override_semantic_anchor_constraints(change: dict) -> None:
    control = ObservedControl(
        control_id="target",
        dom_selector="#target",
        label_text="Question",
        name_attr="question",
        section_text="Applicant",
        role="textbox",
    )
    anchor = FieldAnchorSpec(css_selector="#target", **change)
    snapshot = FormSnapshot(
        url_origin_and_path="https://portal.example", controls=(control,)
    )
    assert match_anchor_in_snapshot(anchor, snapshot) is None


def test_authentication_needs_positive_evidence_and_heartbeat_rechecks_it() -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/form", authenticated_landmark_present=False
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/form")
    assert lease.preflight_auth() == LeaseState.AUTH_REQUIRED
    surface.authenticated_landmark_present = True
    assert lease.preflight_auth() == LeaseState.READY
    surface.auth_wall_present = True
    surface.authenticated_landmark_present = False
    # HTTP 200 alone cannot keep an expired authenticated lease alive.
    assert lease.heartbeat() is False
    assert lease.state == LeaseState.AUTH_REQUIRED


def test_replacement_document_cannot_inherit_human_prefill_decision() -> None:
    class ContextSurface(FakeStatefulFormSurface):
        context_id = "first-document"

        def snapshot(self, **kwargs):
            return (
                super()
                .snapshot(**kwargs)
                .model_copy(update={"context_id": self.context_id})
            )

    surface = ContextSurface(prefill_values={"prior_claims_count": "2"})
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    result = reconciler.run_until_halt_or_done(session)
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    surface.context_id = "replacement-at-same-origin-path-and-controls"
    with pytest.raises(ValueError, match="context changed"):
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id=halt.halt_id,
                action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                actor_id="operator",
                idempotency_key="old-document",
            ),
        )
    assert session.human_resolutions == []
    assert surface.committed_record is None
