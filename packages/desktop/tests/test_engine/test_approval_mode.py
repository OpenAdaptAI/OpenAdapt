"""Only ask a person about problems: the Desktop side.

The safety contract these tests pin:

* Desktop never turns the switch on. It reads a switch that Cloud turned on,
  and anyone at this computer can turn it off, offline, before the next save.
* A switch Desktop can't read or confirm for this exact version and runner
  reads as off, never on.
* While any piece the switch needs is missing, Desktop shows it as not used
  on this computer, never as on.
* An item counts as saved without a person only when Flow's own approval
  block says ``no_person``, both post-run checks passed, and the transaction
  outcome is a consistent ``VERIFIED``. Every other item goes to a person,
  and the outcome words never change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_desktop.approval_mode import (
    NOT_BUILT,
    SwitchApprovalError,
    accept_switch,
    item_approval,
    read_switch,
    saved_without_person,
    turn_off,
)
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices

switch_api = pytest.importorskip("openadapt_flow.approval_mode.switch")
flow_ir = pytest.importorskip("openadapt_flow.ir")

RUNNER = "runner-clinic-1"
HEX = "a" * 64


def _envelope(bundle_digest: str, *, runner_id: str = RUNNER, switch_id: str = "sw-1") -> dict:
    return {
        "schema": "openadapt-desktop.approval-mode-switch/v1",
        "record": {
            "switch_id": switch_id,
            "org_id": "org-clinic",
            "workflow_id": "cloud-wf-1",
            "runner_id": runner_id,
            "bundle_content_digest": bundle_digest,
            "environment_digest": "sha256:" + "b" * 64,
            "verifier_identities": ["sha256:" + "c" * 64],
            "dedicated_user_ref": "hmac-sha256:" + "d" * 64,
        },
        "turned_on": {"name": "Sam Ortiz", "role": "admin", "at": "2026-10-04T15:02:00Z"},
        "confirmed": {"name": "Lee Park", "at": "2026-10-04T15:20:00Z"},
    }


class _Audit:
    def log(self, *_a, **_k) -> None:
        pass


class _Storage:
    def get_captures(self, **_k):
        return []

    def get_storage_usage(self):
        return {}


def _report(*, transaction: str | None, execution: str, approval: dict | None) -> dict:
    envelope = {
        "version": "openadapt.execution-outcome/v1",
        "outcome": execution,
        "profile": "regulated",
        "production_eligible": execution == "VERIFIED",
        "execution_completed": execution in {"VERIFIED", "COMPLETED_UNVERIFIED"},
        "required_contracts": {"authorization": 1, "identity": 0, "postcondition": 0, "effect": 1},
        "passed_contracts": {"authorization": 1, "identity": 0, "postcondition": 0, "effect": 1},
        "evidence_classes": ["authorization"],
        "model_calls": 0,
        "external_network_calls": "none",
        "compensation_actions": 0,
    }
    report = {
        "success": execution == "VERIFIED",
        "execution_outcome": execution,
        "execution_profile": "regulated",
        "production_eligible": execution == "VERIFIED",
        "execution_completed": envelope["execution_completed"],
        "outcome_envelope": envelope,
        "model_calls": 0,
        "results": [],
    }
    if transaction is not None:
        report["transaction_outcome"] = transaction
    if approval is not None:
        report["approval"] = approval
    return report


def _checks(codes: list[str], *, failed: dict[str, str] | None = None) -> list[dict]:
    failed = failed or {}
    return [
        {"code": code, "passed": code not in failed, "detail": failed.get(code, "ok")}
        for code in codes
    ]


PRE = ["switch_on", "signed_in_user", "right_record", "same_screens", "record_check_ready"]


ITEM = [
    "inputs_supplied",
    "channel",
    "inputs_strict",
    "not_a_retry",
    "record_key_not_recent",
    "deployment_config",
    "switch_lease",
    "not_resumed",
]


def no_person_block() -> dict:
    """Every check Flow's gate runs in ``problems_only``, each passed."""

    return {
        "mode": "problems_only",
        "channel": "desktop_run_it_list_with_header",
        "switch_id": "sw-1",
        "switch_record_sha256": "sha256:" + "e" * 64,
        "item_checks": _checks(ITEM),
        "pre_checks": _checks(PRE),
        "post_checks": _checks(["verified", "whole_run_clean"]),
        "saves_without_person": 1,
        "decision": "no_person",
        "needs_person": False,
        "switch_log_recorded": True,
    }


def screen_changed_block() -> dict:
    """R1 passed, R2 failed: saved and checked, but the run needed a repair."""

    return {
        "mode": "problems_only",
        "channel": "desktop_run_it_list_with_header",
        "switch_id": "sw-1",
        "item_checks": _checks(["channel"]),
        "pre_checks": _checks(PRE),
        "post_checks": _checks(
            ["verified", "whole_run_clean"], failed={"whole_run_clean": "repair_applied"}
        ),
        "saves_without_person": 1,
        "decision": "problem",
        "decision_reason": "screen_changed",
        "needs_person": True,
        "counts_toward_problems": True,
        "switch_off_reason": "screen_changed",
        "switch_log_recorded": True,
    }


class _Bridge:
    def __init__(self, reports: list[dict]) -> None:
        self.reports = list(reports)
        self.calls = 0

    def ensure_browser_runtime(self, _progress) -> None:
        pass

    def supports_approval_gate(self) -> bool:
        return True

    def run(self, bundle, config, out_dir=None, params_file=None, env_overrides=None, **_k):
        from openadapt_desktop.flow_bridge import FlowResult

        assert _k.get("approval_mode") == "each_item"
        self.calls += 1
        report = self.reports.pop(0)
        (out_dir / "report.json").write_text(json.dumps(report))
        return FlowResult(
            ok=report.get("success") is True,
            returncode=0 if report.get("success") is True else 1,
            out_dir=out_dir,
            stdout="",
        )


@pytest.fixture
def task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    bundle = config.data_dir / "bundles" / "wf-note"
    flow_ir.Workflow(
        name="Add a referral",
        params={"member_id": "M-0001", "note": "Reviewed"},
        steps=[flow_ir.Step(id="step-1", intent="Click Save", action=flow_ir.ActionKind.CLICK)],
    ).save(bundle)
    db.insert_capture("cap-1", str(tmp_path / "cap"), "2026-10-08T00:00:00+00:00")
    db.insert_bundle("wf-note", str(bundle), capture_id="cap-1")
    services = EngineServices(config, db=db, storage=_Storage(), audit=_Audit())
    disp = EngineDispatcher(config, services=services, emit=lambda *_a: None)
    monkeypatch.setattr(EngineDispatcher, "_runner_id", lambda self: RUNNER)
    digest = json.loads((bundle / "manifest.json").read_text())["content_digest"]
    yield disp, db, config, digest
    db.close()


def _run(disp, member: str) -> dict:
    return disp.dispatch(
        "run_item",
        {
            "workflow_id": "wf-note",
            "inputs": {"member_id": member, "note": "Reviewed"},
            "target": {"backend": "web", "url": "https://emr.example.test"},
            "batch_id": "batch-1",
        },
    )


# ------------------------------------------------------------- the switch


def test_desktop_has_no_command_that_turns_the_switch_on(task) -> None:
    disp, _db, _config, _digest = task
    commands = {name for name in disp._handlers if "approval" in name or "switch" in name}
    # The item-approval pair answers one save a running item waits on
    # (``each_item``), as does a stop card's save approval; none reads or
    # writes a switch record.
    assert commands == {
        "get_approval_mode",
        "turn_off_approval_mode",
        "get_item_approval",
        "answer_item_approval",
        "answer_stop_approval",
    }
    source = Path(__file__).resolve().parents[2] / "openadapt_desktop" / "dispatch.py"
    assert "accept_switch" not in source.read_text()


def test_no_switch_reads_off_and_has_nothing_to_turn_off(task) -> None:
    disp, _db, _config, _digest = task
    mode = disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})
    assert mode["ok"] is True
    assert mode["state"] == "none"
    # The card shows no switch for a task that has none on this computer.
    assert mode["visible"] is False
    assert mode["can_turn_off"] is False
    assert mode["blockers"][-len(NOT_BUILT):] == list(NOT_BUILT)
    refused = disp.dispatch("turn_off_approval_mode", {"workflow_id": "wf-note"})
    assert refused["ok"] is False
    assert "no switch" in refused["error"]


def test_a_switch_on_in_cloud_is_not_used_while_pieces_are_missing(task) -> None:
    disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))

    mode = disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})

    assert mode["recorded_state"] == "on"
    assert mode["visible"] is True
    # Flow's governed run can't take the gate yet, so nothing here is checked
    # before each save: the switch must not read as on.
    assert mode["state"] == "not_used_here"
    assert "field_change_check" in mode["blockers"]
    assert mode["can_turn_off"] is True
    assert mode["turned_on"]["name"] == "Sam Ortiz"
    assert mode["turned_on"]["role"] == "admin"
    assert mode["confirmed"]["name"] == "Lee Park"
    evidence = mode["evidence"]
    assert evidence["bundle_digest"] == digest
    assert evidence["runner_id"] == RUNNER
    assert evidence["log_intact"] is True
    assert evidence["items_without_person"] == 0


def test_turning_off_works_offline_and_reaches_flows_log_before_the_next_save(
    task, monkeypatch: pytest.MonkeyPatch
) -> None:
    disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))

    import httpx

    def no_network(*_a, **_k):
        raise AssertionError("turning off must not need the network")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    result = disp.dispatch("turn_off_approval_mode", {"workflow_id": "wf-note"})

    assert result["ok"] is True
    assert result["logged"] is True
    # Flow's gate reads this log before every save; any switch-off means off.
    state = switch_api.SwitchLog(
        config.data_dir / "approval_mode" / "wf-note" / "switch.log"
    ).state()
    assert state.is_off
    assert state.off_reason.value == "turned_off"
    mode = result["mode"]
    assert mode["state"] == "off"
    assert mode["off_reason"] == "turned_off"
    assert mode["off_by_this_computer"] is True
    assert mode["can_turn_off"] is False

    again = disp.dispatch("turn_off_approval_mode", {"workflow_id": "wf-note"})
    assert again["ok"] is True
    assert again["already_off"] is True


def test_a_closed_switch_stays_closed_until_cloud_sends_a_new_one(task) -> None:
    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    turn_off(config.data_dir, "wf-note")
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"

    accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-2"))
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "on"
    assert view["switch_id"] == "sw-2"
    assert list((config.data_dir / "approval_mode" / "wf-note" / "closed").iterdir())


def test_a_switch_for_another_version_or_runner_never_reads_on(task) -> None:
    disp, db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope("f" * 64, switch_id="sw-a"))
    mode = disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})
    assert mode["recorded_state"] == "off"
    assert mode["off_reason"] == "version_changed"

    accept_switch(
        config.data_dir, "wf-note",
        _envelope(digest, runner_id="runner-other", switch_id="sw-b"),
    )
    mode = disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})
    assert mode["recorded_state"] == "off"
    assert mode["off_reason"] == "environment_changed"

    # A new version of the same recording doesn't inherit the switch.
    accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-c"))
    newer = config.data_dir / "bundles" / "wf-note-v2"
    flow_ir.Workflow(
        name="Add a referral",
        steps=[flow_ir.Step(id="step-1", intent="Click Save", action=flow_ir.ActionKind.CLICK)],
    ).save(newer)
    db.insert_bundle("wf-note-v2", str(newer), capture_id="cap-1")
    mode = disp.dispatch("get_approval_mode", {"workflow_id": "wf-note-v2"})
    assert mode["state"] == "off"
    assert mode["off_reason"] == "version_changed"


def test_a_tampered_or_missing_log_reads_off(task) -> None:
    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    log = config.data_dir / "approval_mode" / "wf-note" / "switch.log"
    log.write_text(log.read_text().replace('"opened"', '"opened" '))
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"
    assert view["off_reason"] == "uncertain_result"

    # Turning off still records the person's choice when the log is broken.
    result = turn_off(config.data_dir, "wf-note")
    assert result["logged"] is False
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"
    assert view["off_reason"] == "turned_off"

    log.unlink()
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"


def test_an_unfinished_item_in_the_log_reads_off(task) -> None:
    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    log = switch_api.SwitchLog(config.data_dir / "approval_mode" / "wf-note" / "switch.log")
    log.append("item_started", run_ref=HEX)
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"
    assert view["off_reason"] == "uncertain_result"


def test_a_switch_from_cloud_is_validated_before_it_is_stored(task) -> None:
    _disp, _db, config, digest = task
    bad = _envelope(digest)
    bad["record"]["verifier_identities"] = []
    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "wf-note", bad)
    bad = _envelope(digest)
    bad["turned_on"]["role"] = "member"
    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "wf-note", bad)
    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "../escape", _envelope(digest))


def test_requirements_come_from_flow_and_are_not_confirmed_here(task) -> None:
    disp, _db, config, digest = task
    # A task with no switch here doesn't pay for reading its requirements.
    hidden = disp.dispatch(
        "get_approval_mode", {"workflow_id": "wf-note", "requirements": True}
    )
    assert "requirements" not in hidden
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    mode = disp.dispatch(
        "get_approval_mode", {"workflow_id": "wf-note", "requirements": True}
    )
    requirements = mode["requirements"]
    if not requirements["available"]:
        pytest.skip(f"Flow couldn't read the test workflow: {requirements['reason']}")
    codes = [item["code"] for item in requirements["items"]]
    assert codes == ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]
    first = next(item for item in requirements["items"] if not item["met"])
    assert first["code"] == "E1"
    assert first["reason"] == "not_certified_and_sealed"
    # Decisions people make are read, never made: none is met here.
    assert all(not item["met"] for item in requirements["items"])


# --------------------------------------------------------- per-item evidence


def test_only_flows_no_person_with_both_post_checks_counts_as_without_a_person() -> None:
    approval = item_approval(no_person_block())
    assert approval is not None
    assert approval["validated"] is True
    assert saved_without_person(approval, "VERIFIED") is True
    # A VERIFIED execution outcome with no transaction outcome never counts.
    assert saved_without_person(approval, None) is False

    forged = no_person_block()
    forged["post_checks"] = _checks(
        ["verified", "whole_run_clean"], failed={"whole_run_clean": "model_call"}
    )
    approval = item_approval(forged)
    assert approval["validated"] is False
    assert approval["decision"] == "unverified"
    assert approval["needs_person"] is True
    assert saved_without_person(approval, "VERIFIED") is False

    assert item_approval(None) is None
    unreadable = item_approval({"decision": "no_person", "needs_person": False})
    assert unreadable["needs_person"] is True
    assert saved_without_person(unreadable, "VERIFIED") is False


def test_items_saved_without_a_person_stay_out_of_needs_you(switch_in_use) -> None:
    disp, _db, config, digest = switch_in_use
    disp.services._flow_bridge = _Bridge(
        [
            _report(
                transaction="VERIFIED",
                execution="VERIFIED",
                approval=_no_person_for_this_switch(config, digest),
            )
        ]
    )

    result = _run(disp, "M-4821")

    item = result["item"]
    assert item["result"] == "done_checked"
    assert item["status"] == "done"
    assert item["saved_without_person"] is True
    assert result["report"]["approval"]["decision"] == "no_person"
    inbox = disp.dispatch("list_attention_items", {})
    assert inbox["items"] == []
    summary = disp.dispatch("get_results_summary", {})
    assert summary["total"]["done_checked"] == 1
    assert summary["total"]["saved_without_person"] == 1
    assert summary["total"]["needed_person"] == 0
    # The block holds closed codes only, never an input value.
    assert "M-4821" not in json.dumps(result["report"]["approval"])


def test_a_confirmed_save_that_needed_a_workaround_goes_to_a_person(task) -> None:
    disp, db, _config, _digest = task
    disp.services._flow_bridge = _Bridge(
        [
            _report(transaction="VERIFIED", execution="VERIFIED", approval=screen_changed_block()),
            _report(transaction="VERIFIED", execution="VERIFIED", approval=None),
        ]
    )

    result = _run(disp, "M-4822")

    item = result["item"]
    # The outcome word never changes...
    assert item["result"] == "done_checked"
    # ...but the item waits for a person, who can't re-enter it.
    assert item["status"] == "open"
    assert item["saved_without_person"] is False
    assert item["actions"] == ["confirmed_saved", "done_by_person"]
    assert item["approval"]["switch_off_reason"] == "screen_changed"
    inbox = disp.dispatch("list_attention_items", {})
    assert [entry["label"] for entry in inbox["items"]] == ["M-4822"]
    blocked = _run(disp, "M-4823")
    assert blocked["pre_action_refusal"] is True

    refused = disp.dispatch(
        "resolve_attention_item",
        {"item_id": item["item_id"], "action": "retry_after_check", "record_checked": True},
    )
    assert refused["ok"] is False
    closed = disp.dispatch(
        "resolve_attention_item",
        {"item_id": item["item_id"], "action": "confirmed_saved", "record_checked": True},
    )
    assert closed["ok"] is True
    summary = disp.dispatch("get_results_summary", {})
    assert summary["total"]["done_checked"] == 1
    assert summary["total"]["saved_without_person"] == 0
    assert summary["total"]["needed_person"] == 1
    assert db.get_work_item(item["item_id"])["status"] == "confirmed_saved"


def test_a_block_flow_rejects_never_closes_an_item(task) -> None:
    disp, _db, _config, _digest = task
    forged = no_person_block()
    forged["pre_checks"] = []
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=forged)]
    )

    item = _run(disp, "M-4824")["item"]

    assert item["result"] == "done_checked"
    assert item["status"] == "open"
    assert item["saved_without_person"] is False
    assert item["approval"]["decision"] == "unverified"


def test_runs_without_a_gate_keep_todays_behavior(task) -> None:
    disp, _db, _config, _digest = task
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=None)]
    )

    result = _run(disp, "M-4825")

    assert result["report"]["approval"] is None
    assert result["item"]["status"] == "done"
    assert result["item"]["approval"] is None
    assert result["item"]["saved_without_person"] is False
    summary = disp.dispatch("get_results_summary", {})
    assert summary["total"]["done_checked"] == 1
    assert summary["total"]["saved_without_person"] == 0


def test_screens_learn_about_an_automatic_switch_off(task) -> None:
    disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    log = switch_api.SwitchLog(config.data_dir / "approval_mode" / "wf-note" / "switch.log")
    log.append("switched_off", reason="screen_changed")

    inbox = disp.dispatch("list_attention_items", {})
    (switch,) = inbox["switches"]
    assert switch["workflow_id"] == "wf-note"
    assert switch["state"] == "off"
    assert switch["off_reason"] == "screen_changed"
    assert switch["off_by_this_computer"] is False
    summary = disp.dispatch("get_results_summary", {})
    assert summary["switches"][0]["off_reason"] == "screen_changed"


def test_an_item_held_for_approval_offers_no_rerun(task) -> None:
    disp, _db, _config, _digest = task
    held = {
        "mode": "problems_only",
        "channel": "desktop_run_it_list_with_header",
        "item_checks": _checks(["channel", "record_key_not_recent"],
                               failed={"record_key_not_recent": "seen_recently"}),
        "pre_checks": [],
        "post_checks": [],
        "decision": "approval_required",
        "decision_reason": "record_key_not_recent",
        "needs_person": True,
        "gated_step_id": "step-1",
    }
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="HALTED_BEFORE_EFFECT", execution="HALTED", approval=held)]
    )

    item = _run(disp, "M-4851")["item"]

    assert item["result"] == "stopped_before_saving"
    assert item["nothing_written"] is True
    assert item["status"] == "open"
    # Running it again would stop at the same check; approving isn't built.
    assert item["actions"] == ["skip", "decline", "done_by_person"]
    assert item["approval"]["failed_check"]["code"] == "record_key_not_recent"


# ------------------------------------------------------------ attack attempts
#
# Each test below tries to get an item saved without a matching read-back,
# or without a person seeing it, past Desktop. Made-up records only.


def _no_person_for_this_switch(config, digest: str) -> dict:
    """A ``no_person`` block for the switch that ``_envelope(digest)`` stores."""

    record = switch_api.SwitchRecord.model_validate(_envelope(digest)["record"])
    block = no_person_block()
    block["switch_id"] = record.switch_id
    block["switch_record_sha256"] = record.digest()
    return block


@pytest.fixture
def switch_in_use(task, monkeypatch: pytest.MonkeyPatch):
    """The switch on for wf-note, with every missing piece pretended built.

    Only for the positive control and for attacks that need the switch on.
    No production path makes ``blockers`` empty today.
    """

    disp, db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    monkeypatch.setattr("openadapt_desktop.approval_mode.NOT_BUILT", ())
    assert disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})["state"] == "on"
    return disp, db, config, digest


def test_control_a_bound_no_person_item_counts_when_the_switch_is_on_here(switch_in_use) -> None:
    disp, _db, config, digest = switch_in_use
    disp.services._flow_bridge = _Bridge(
        [
            _report(
                transaction="VERIFIED",
                execution="VERIFIED",
                approval=_no_person_for_this_switch(config, digest),
            )
        ]
    )

    item = _run(disp, "M-4901")["item"]

    assert item["status"] == "done"
    assert item["saved_without_person"] is True


@pytest.mark.parametrize(
    "transaction", ["RECONCILIATION_REQUIRED", "HALTED_BEFORE_EFFECT", "COMPLETED_UNVERIFIED",
                    "NOT_A_REAL_OUTCOME"]
)
def test_attack_inconsistent_verified_pair_never_closes_an_item(task, transaction: str) -> None:
    """Execution VERIFIED with a transaction outcome that disagrees.

    Desktop's own table says the pair is "Check the record". Before the fix,
    the dispatcher dropped the disagreeing transaction outcome and the item
    closed as "Done and checked", out of Needs you.
    """

    disp, _db, _config, _digest = task
    disp.services._flow_bridge = _Bridge(
        [_report(transaction=transaction, execution="VERIFIED", approval=None)]
    )

    result = _run(disp, "M-4902")

    item = result["item"]
    assert item["result"] == "check_record"
    assert item["status"] == "open"
    assert item["nothing_written"] is False
    assert item["saved_without_person"] is False
    assert [entry["label"] for entry in disp.dispatch("list_attention_items", {})["items"]] == [
        "M-4902"
    ]
    assert result["report"]["ok"] is False
    # The next item of the task waits until a person checks this record.
    assert _run(disp, "M-4903")["pre_action_refusal"] is True


def test_attack_inconsistent_pair_under_the_switch_never_reads_confirmed(switch_in_use) -> None:
    """R1 failed in Flow; the Needs-you copy must not say the check confirmed it."""

    disp, _db, config, digest = switch_in_use
    block = _no_person_for_this_switch(config, digest)
    block.update(
        decision="problem",
        decision_reason="reconciliation_required",
        needs_person=True,
        counts_toward_problems=True,
        switch_off_reason="uncertain_result",
        post_checks=_checks(
            ["verified", "whole_run_clean"], failed={"verified": "not_verified"}
        ),
    )
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="RECONCILIATION_REQUIRED", execution="VERIFIED", approval=block)]
    )

    item = _run(disp, "M-4904")["item"]

    assert item["result"] == "check_record"
    assert item["status"] == "open"
    assert item["actions"] == ["confirmed_saved", "retry_after_check", "done_by_person"]


def test_attack_verified_without_a_transaction_outcome_goes_to_a_person(switch_in_use) -> None:
    """Design R1: a VERIFIED execution outcome with no transaction outcome fails."""

    disp, _db, config, digest = switch_in_use
    disp.services._flow_bridge = _Bridge(
        [
            _report(
                transaction=None,
                execution="VERIFIED",
                approval=_no_person_for_this_switch(config, digest),
            )
        ]
    )

    item = _run(disp, "M-4905")["item"]

    assert item["result"] == "done_checked"
    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["needs_person"] is True
    assert item["approval"]["unconfirmed"] == "not_verified"


@pytest.mark.parametrize(
    "strip",
    ["item_checks", "pre_checks_partial", "switch_id", "switch_record_sha256"],
)
def test_attack_a_no_person_block_missing_checks_goes_to_a_person(
    switch_in_use, strip: str
) -> None:
    """Flow's evidence model accepts ``no_person`` with no item checks and any
    non-empty set of passed pre-checks. Desktop requires every check Flow's
    gate runs, and the switch the block names."""

    disp, _db, config, digest = switch_in_use
    block = _no_person_for_this_switch(config, digest)
    if strip == "item_checks":
        block["item_checks"] = []
    elif strip == "pre_checks_partial":
        block["pre_checks"] = _checks(["switch_on"])
    else:
        block.pop(strip)
    # Flow's own model still accepts each of these blocks.
    from openadapt_flow.approval_mode.model import ApprovalModeItemEvidence

    ApprovalModeItemEvidence.model_validate(block)
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=block)]
    )

    item = _run(disp, "M-4906")["item"]

    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["decision"] == "unverified"


def test_attack_no_person_for_a_task_with_no_switch_here_goes_to_a_person(task) -> None:
    """A report that claims ``no_person`` while this computer has no switch."""

    disp, _db, _config, _digest = task
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=no_person_block())]
    )

    item = _run(disp, "M-4907")["item"]

    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["unconfirmed"] == "switch_not_on_here"
    summary = disp.dispatch("get_results_summary", {})
    assert summary["total"]["saved_without_person"] == 0


def test_attack_no_person_while_the_switch_is_not_used_here_goes_to_a_person(task) -> None:
    """The switch is on in Cloud, but this computer can't check before saves."""

    disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    assert disp.dispatch("get_approval_mode", {"workflow_id": "wf-note"})["state"] == (
        "not_used_here"
    )
    disp.services._flow_bridge = _Bridge(
        [
            _report(
                transaction="VERIFIED",
                execution="VERIFIED",
                approval=_no_person_for_this_switch(config, digest),
            )
        ]
    )

    item = _run(disp, "M-4908")["item"]

    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["unconfirmed"] == "switch_not_on_here"


def test_attack_no_person_for_another_switch_goes_to_a_person(switch_in_use) -> None:
    disp, _db, config, digest = switch_in_use
    block = _no_person_for_this_switch(config, digest)
    block["switch_id"] = "sw-other"
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=block)]
    )

    item = _run(disp, "M-4909")["item"]

    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["unconfirmed"] == "switch_mismatch"


@pytest.mark.parametrize("kind", ["repeat", "caller_config"])
def test_attack_a_repeat_or_caller_config_never_saves_without_a_person(
    switch_in_use, tmp_path: Path, kind: str
) -> None:
    """Desktop knows facts Flow can't (design 7.2). Flow's block claims the
    item checks passed, but Desktop sent no item context, so it decides."""

    disp, _db, config, digest = switch_in_use
    if kind == "repeat":
        extra: dict = {"allow_repeat": True}
    else:
        caller_config = tmp_path / "caller-deployment.yaml"
        caller_config.write_text("runtime:\n  approval_mode: problems_only\n")
        extra = {"deployment_config": str(caller_config)}
    disp.services._flow_bridge = _Bridge(
        [
            _report(
                transaction="VERIFIED",
                execution="VERIFIED",
                approval=_no_person_for_this_switch(config, digest),
            )
        ]
    )

    item = disp.dispatch(
        "run_item",
        {
            "workflow_id": "wf-note",
            "inputs": {"member_id": "M-4910", "note": "Reviewed"},
            "target": {"backend": "web", "url": "https://emr.example.test"},
            "batch_id": "batch-1",
            **extra,
        },
    )["item"]

    assert item["saved_without_person"] is False
    assert item["status"] == "open"
    assert item["approval"]["unconfirmed"] in {"retry_or_repeat", "caller_configuration"}


def test_attack_a_run_that_crashes_under_the_switch_goes_to_a_person(switch_in_use) -> None:
    disp, _db, _config, _digest = switch_in_use

    class _Crash(_Bridge):
        def run(self, *_a, **_k):
            raise RuntimeError("engine lost")

    disp.services._flow_bridge = _Crash([])

    item = _run(disp, "M-4911")["item"]

    assert item["result"] == "check_record"
    assert item["status"] == "open"
    assert item["saved_without_person"] is False


def test_attack_cloud_cannot_reopen_a_switch_turned_off_here(task) -> None:
    """Design 7.6: a local switch-off that never reached Cloud can't be undone.

    Reporting the switch-off to Cloud isn't built, so Cloud would keep sending
    the same record. Before the fix, storing it again archived the closed log
    and opened a fresh one: the switch read on again.
    """

    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    turn_off(config.data_dir, "wf-note")

    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "wf-note", _envelope(digest))

    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"
    assert view["off_reason"] == "turned_off"


def test_attack_an_archived_closed_switch_cannot_return(task) -> None:
    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    turn_off(config.data_dir, "wf-note")
    accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-2"))
    turn_off(config.data_dir, "wf-note")
    accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-3"))

    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-1"))
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["switch_id"] == "sw-3"


def test_attack_resending_the_same_switch_keeps_its_log(task) -> None:
    """Re-storing the switch Cloud already sent must not reset its log.

    A fresh log would forget an interrupted item (a save that may have gone
    through) and the problem count toward ``too_many_problems``.
    """

    _disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    log = switch_api.SwitchLog(config.data_dir / "approval_mode" / "wf-note" / "switch.log")
    log.append("item_started", run_ref=HEX)

    assert accept_switch(config.data_dir, "wf-note", _envelope(digest)) == "sw-1"

    assert log.state().open_items == [HEX]
    view = read_switch(
        config.data_dir, "wf-note",
        bundle_dir=config.data_dir / "bundles" / "wf-note", runner_id=RUNNER,
    )
    assert view["state"] == "off"
    assert view["off_reason"] == "uncertain_result"


def test_attack_a_new_switch_cannot_hide_an_interrupted_item(task) -> None:
    """A new record replaces a log whose item never finished.

    The old log is closed as ``uncertain_result`` before it is archived, and
    the item itself still waits in Needs you as a record to check, blocking
    the task's lists.
    """

    disp, db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))
    log_path = config.data_dir / "approval_mode" / "wf-note" / "switch.log"
    switch_api.SwitchLog(log_path).append("item_started", run_ref=HEX)
    # Desktop queued the item's row before its run, then stopped mid-run.
    db.insert_work_item(
        "item-interrupted",
        batch_id="batch-0",
        workflow_id="wf-note",
        position=0,
        label="M-4912",
        inputs_json=json.dumps({"member_id": "M-4912", "note": "Reviewed"}),
        inputs_digest="0" * 64,
        retry_of=None,
    )

    accept_switch(config.data_dir, "wf-note", _envelope(digest, switch_id="sw-2"))

    (archived,) = list((config.data_dir / "approval_mode" / "wf-note" / "closed").iterdir())
    old = switch_api.SwitchLog(archived / "switch.log").state()
    assert old.off_reason is not None
    assert old.off_reason.value == "uncertain_result"
    assert old.open_items == [HEX]
    inbox = disp.dispatch("list_attention_items", {})["items"]
    assert [(entry["label"], entry["result"]) for entry in inbox] == [("M-4912", "check_record")]
    assert _run(disp, "M-4913")["pre_action_refusal"] is True


def test_attack_a_turn_off_that_cannot_be_appended_still_reaches_flow(
    task, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person turns the switch off, but the append fails (a full disk).

    Before the fix the error escaped, nothing changed on disk, and Flow's gate
    kept reading the switch as on while the person believed it was off.
    """

    disp, _db, config, digest = task
    accept_switch(config.data_dir, "wf-note", _envelope(digest))

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(switch_api, "turn_off", disk_full)

    result = disp.dispatch("turn_off_approval_mode", {"workflow_id": "wf-note"})

    assert result["ok"] is True
    assert result["logged"] is False
    assert result["moved_aside"] is True
    # What Flow's gate reads before every save: a missing log means off.
    with pytest.raises(switch_api.SwitchLogError):
        switch_api.SwitchLog(
            config.data_dir / "approval_mode" / "wf-note" / "switch.log"
        ).state()
    assert result["mode"]["state"] == "off"
    assert result["mode"]["off_reason"] == "turned_off"
    # And the same switch can't come back.
    with pytest.raises(SwitchApprovalError):
        accept_switch(config.data_dir, "wf-note", _envelope(digest))


@pytest.mark.parametrize(
    ("outcome", "blocks"), [("RECONCILIATION_REQUIRED", True), ("HALTED_BEFORE_EFFECT", False)]
)
def test_attack_a_cloud_run_that_may_have_saved_blocks_the_tasks_lists(
    task, outcome: str, blocks: bool
) -> None:
    """Design 7.5: Desktop lists also wait for an open Cloud-started save.

    Before the fix, ``run_item`` read only Desktop's own work items, so a list
    kept running past a hosted run of the same task whose save may have gone
    through.
    """

    disp, db, _config, _digest = task
    db.update_bundle("wf-note", workflow_id="cloud-wf-1")
    db.insert_run("run-hosted-1", "/nonexistent/run-hosted-1", bundle_id=None)
    db.update_run("run-hosted-1", status=outcome)
    db.insert_halt("halt-run-hosted-1", "run-hosted-1", workflow_id="cloud-wf-1")
    disp.services._flow_bridge = _Bridge(
        [_report(transaction="VERIFIED", execution="VERIFIED", approval=None)]
    )

    result = _run(disp, "M-4914")

    assert bool(result.get("pre_action_refusal")) is blocks
    if blocks:
        assert "OpenAdapt Cloud" in result["error"]
        assert disp.services._flow_bridge.calls == 0
