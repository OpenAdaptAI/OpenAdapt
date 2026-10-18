"""Who decided each save: the Desktop projection of Flow's approval rows.

A person in Run it or on their phone, or a delegate (an agent, rule or
system) named in a grant a person signed. A row that doesn't hold together
reads as unverified, and a row that isn't an answer is left out.
"""

from __future__ import annotations

from openadapt_desktop.approval_mode import approvers, item_approval

_GRANT = "sha256:" + "e" * 64


def _row(**extra: object) -> dict:
    row = {
        "step_id": "s4",
        "request_sha256": "sha256:" + "a" * 64,
        "answer_sha256": "sha256:" + "b" * 64,
        "decision": "approved",
        "decline_reason": None,
        "decided_by": "human",
        "route": "desktop_run_it",
        "approver_kind": "cloud_user",
        "approver_id": "aaaaaaaa-0000-4000-8000-000000000003",
        "auth_method": "cloud_aal2",
    }
    row.update(extra)
    return row


_DELEGATE = {
    "decided_by": "automation",
    "route": "local_cli",
    "approver_kind": "unauthenticated",
    "approver_id": None,
    "auth_method": "none",
    "approver": {
        "schema_version": "openadapt.approver-identity/v1",
        "kind": "agent",
        "auth": "local_cli",
        "id": "agent:approver-bot",
        "auth_method": "none",
        "policy_ref": _GRANT,
        "on_behalf_of": None,
    },
}


def test_the_record_names_who_decided_each_save() -> None:
    rows = approvers(
        [
            _row(),
            _row(route="cloud_phone"),
            _row(**_DELEGATE),
            _row(
                decision="declined",
                decline_reason="wrong_values",
                approver_kind="unauthenticated",
                approver_id=None,
                auth_method="none",
            ),
        ]
    )
    assert [r["route"] for r in rows] == [
        "desktop_run_it",
        "cloud_phone",
        "local_cli",
        "desktop_run_it",
    ]
    assert rows[0] == {
        "decision": "approved",
        "route": "desktop_run_it",
        "kind": "person",
        "id": "aaaaaaaa-0000-4000-8000-000000000003",
        "policy_ref": None,
        "signed_in": True,
        "verified": True,
    }
    assert rows[1]["verified"] is True
    assert rows[2]["kind"] == "agent"
    assert rows[2]["id"] == "agent:approver-bot"
    assert rows[2]["policy_ref"] == _GRANT
    assert rows[2]["verified"] is True
    assert rows[3]["signed_in"] is False


def test_a_delegate_without_a_signed_grant_or_on_a_persons_route_is_not_verified() -> None:
    no_grant = dict(_DELEGATE, approver=dict(_DELEGATE["approver"], policy_ref=None))
    on_person_route = dict(_DELEGATE, route="desktop_run_it")
    phone_without_aal2 = _row(route="cloud_phone", auth_method="cloud_aal1")
    rows = approvers([_row(**no_grant), _row(**on_person_route), phone_without_aal2])
    assert [r["verified"] for r in rows] == [False, False, False]
    # Rows that aren't answers at all are left out, never shown as a person.
    assert approvers([{"decision": "maybe"}, "x", _row(decided_by="software")]) == []
    assert approvers(None) == []


def test_the_item_projection_carries_the_approvers() -> None:
    block = {
        "schema_version": "openadapt.approval-mode-item/v1",
        "mode": "each_item",
        "decision": "approved",
        "needs_person": False,
        "pre_checks": [{"code": "each_item", "passed": True, "detail": "approved_by_person"}],
        "approvals": [_row()],
    }
    projected = item_approval(block)
    assert projected is not None
    assert projected["approvers"][0]["kind"] == "person"
    assert projected["approvers"][0]["route"] == "desktop_run_it"
