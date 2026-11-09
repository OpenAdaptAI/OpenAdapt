"""Synthetic PowerShell policy controls; no native account/task operation."""

from __future__ import annotations

import hashlib
import json

import pytest

from openadapt_flow.qualify import qualification_pixel_fault_task as owner
from tests import test_qualification_guest_task_owner as existing

OTHER_SID = "S-1-5-21-111-222-333-1002"
SENTINEL = "synthetic sensitive lookup sentinel"


def observation(sid=existing.USER_SID, **changes):
    return {
        "resolved": True,
        "count": 1,
        "sid_type": 1,
        "canonical_sid": sid,
        **changes,
    }


def calls(lab):
    return [call for record in lab.records for call in record["trace"]]


def test_same_user_name_to_sid_survives_create_and_disposal():
    lab = existing.SyntheticLab()
    lab.state["registered_user_id"] = existing.USER_SID
    receipt = existing.launch(lab)
    assert (
        receipt.principal_sid_sha256
        == hashlib.sha256(existing.USER_SID.encode()).hexdigest()
    )
    assert receipt.nonce == existing.NONCE
    assert receipt.request_sha256 == existing.REQUEST
    assert receipt.helper_sha256 == existing.HELPER
    assert receipt.session_binding_sha256 == existing.SESSION
    assert receipt.instance_guid == existing.GUID
    assert existing.USER_SID not in lab.records[-1]["output"]
    assert lab.user not in lab.records[-1]["output"]
    owner.close_owned_interactive_task(lab, receipt)
    assert existing.destructive_calls(lab) == [f"stop:{existing.GUID}", "unregister"]
    assert lab.state["definition"] is None


def test_same_user_sid_to_name_survives_create_and_disposal():
    lab = existing.SyntheticLab()
    lab.user = existing.USER_SID
    lab.state["registered_user_id"] = "synthetic-user"
    receipt = existing.launch(lab)
    assert receipt.instance_guid == existing.GUID
    owner.close_owned_interactive_task(lab, receipt)
    assert existing.destructive_calls(lab) == [f"stop:{existing.GUID}", "unregister"]


@pytest.mark.parametrize("mode", ["register_fail_after_create", "start_fail_empty"])
def test_same_user_alias_permits_only_exact_empty_partial_cleanup(mode):
    lab = existing.SyntheticLab(mode)
    lab.state["registered_user_id"] = existing.USER_SID
    with pytest.raises(owner.TaskLaunchError):
        existing.launch(lab)
    assert existing.destructive_calls(lab) == ["unregister"]
    assert lab.state["definition"] is None
    assert lab.state["instances"] == []


def test_current_name_to_same_sid_change_retains_owned_disposal():
    lab = existing.SyntheticLab()
    receipt = existing.launch(lab)
    lab.state["definition"]["Principal"]["UserId"] = existing.USER_SID
    owner.close_owned_interactive_task(lab, receipt)
    assert existing.destructive_calls(lab) == [f"stop:{existing.GUID}", "unregister"]


_INVALID_OBSERVATIONS = [
    observation(resolved=False),
    observation(resolved=1),
    observation(resolved="true"),
    observation(count=0),
    observation(count=2),
    observation(count=True),
    observation(count="1"),
    observation(count=1.0),
    observation(sid_type=2),
    observation(sid_type=True),
    observation(sid_type="1"),
    observation(sid_type=1.0),
    observation(canonical_sid=None),
    observation(canonical_sid=1),
    observation(canonical_sid="not-a-sid"),
    observation(canonical_sid="S-1-5-4294967296"),
    observation(extra=True),
    *[
        {k: v for k, v in observation().items() if k != missing}
        for missing in observation()
    ],
    None,
    [observation(), observation()],
]


@pytest.mark.parametrize(
    "untrusted", _INVALID_OBSERVATIONS, ids=range(len(_INVALID_OBSERVATIONS))
)
def test_unproven_expected_user_never_registers_or_cleans_up(untrusted):
    lab = existing.SyntheticLab()
    lab.state["sid_observations"][lab.user] = untrusted
    with pytest.raises(owner.TaskLaunchError):
        existing.launch(lab)
    assert "register" not in calls(lab)
    assert "start" not in calls(lab)
    assert existing.destructive_calls(lab) == []
    assert lab.state["definition"] is None
    assert all(record["lookup_body_stubbed"] is True for record in lab.records)


@pytest.mark.parametrize(
    "untrusted",
    [
        observation(OTHER_SID),
        observation(sid_type=2),
        observation(resolved=False),
        observation(count=2),
    ],
)
def test_unproven_returned_principal_is_not_started_or_adopted(untrusted):
    lab = existing.SyntheticLab()
    lab.state["registered_user_id"] = OTHER_SID
    lab.state["sid_observations"][OTHER_SID] = untrusted
    with pytest.raises(owner.TaskLaunchError):
        existing.launch(lab)
    assert "start" not in calls(lab)
    assert existing.destructive_calls(lab) == []
    assert lab.state["definition"] is not None


@pytest.mark.parametrize("group", [OTHER_SID, " ", False, 1])
def test_nonempty_or_malformed_group_id_is_not_user_ownership(group):
    lab = existing.SyntheticLab()
    lab.state["registered_group_id"] = group
    with pytest.raises(owner.TaskLaunchError):
        existing.launch(lab)
    assert "start" not in calls(lab)
    assert existing.destructive_calls(lab) == []


@pytest.mark.parametrize(
    "untrusted",
    [
        observation(OTHER_SID),
        observation(sid_type=2),
        observation(resolved=False),
        observation(count=2),
    ],
)
def test_changed_or_unproven_disposal_user_never_stops_or_unregisters(untrusted):
    lab = existing.SyntheticLab()
    receipt = existing.launch(lab)
    lab.records.clear()
    lab.state["definition"]["Principal"]["UserId"] = OTHER_SID
    lab.state["sid_observations"][OTHER_SID] = untrusted
    with pytest.raises(RuntimeError):
        owner.close_owned_interactive_task(lab, receipt)
    assert existing.destructive_calls(lab) == []
    assert lab.state["instances"] == [existing.GUID]


def test_failed_expected_lookup_cannot_adopt_matching_looking_partial_task():
    lab = existing.SyntheticLab("late_matching_partial")
    lab.state["sid_observations"][lab.user] = observation(resolved=False)
    lab.state["late_definition"] = {
        "TaskName": f"OAFlowQualificationPixelFault-{existing.NONCE}",
        "Description": f"openadapt.qualification-owned-task/v1:{existing.NONCE}:{existing.REQUEST}:{existing.HELPER}:{existing.SESSION}",
        "Actions": [
            {"Execute": r"C:\Synthetic\helper.exe", "Arguments": "--synthetic"}
        ],
        "Principal": {
            "UserId": lab.user,
            "LogonType": "Interactive",
            "RunLevel": "Limited",
        },
    }
    with pytest.raises(owner.TaskLaunchError) as failure:
        existing.launch(lab)
    assert "register" not in calls(lab)
    assert existing.destructive_calls(lab) == []
    assert failure.value.diagnostic.registration not in {
        "verified_owned",
        "started_owned",
        "owned_handle",
    }
    assert lab.state["definition"] is not None


@pytest.mark.parametrize("during_close", [False, True])
def test_actual_lookup_catch_emits_only_fixed_refusal(during_close):
    lab = existing.SyntheticLab()
    receipt = existing.launch(lab) if during_close else None
    lab.records.clear()
    lab.state["lookup_throws"] = True
    with pytest.raises(RuntimeError) as failure:
        if during_close:
            owner.close_owned_interactive_task(lab, receipt)
        else:
            existing.launch(lab)
    assert SENTINEL not in str(failure.value)
    assert SENTINEL not in json.dumps(lab.records)
    assert all(record["lookup_body_stubbed"] is True for record in lab.records)
    assert existing.destructive_calls(lab) == []
    assert "register" not in calls(lab)


def test_principal_change_after_owned_stop_prevents_unregister():
    lab = existing.SyntheticLab()
    receipt = existing.launch(lab)
    lab.records.clear()
    lab.state["after_stop_user_id"] = OTHER_SID
    lab.state["sid_observations"][OTHER_SID] = observation(OTHER_SID)
    with pytest.raises(RuntimeError):
        owner.close_owned_interactive_task(lab, receipt)
    assert existing.destructive_calls(lab) == [f"stop:{existing.GUID}"]
    assert lab.state["definition"] is not None
