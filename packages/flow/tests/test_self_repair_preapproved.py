"""Pre-approved repairs, pre-flight and one-approval release, on MockMed.

A person signs a repair policy once (an ITIL standard change): the classes of
UI change the repair agent may apply without them. These tests cover:

* a relabelled, moved link (``?drift=relink``) repaired with nobody asked,
  shown in the digest and undone with one rollback;
* refusals that still go to a person: the save control changed, identity
  evidence changed, or a class the policy does not allow;
* a pre-flight on the test record that catches the drift before item 1;
* one phone approval releasing five items held at the same halt.

The agent is scripted and calls no model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.ir import Workflow
from openadapt_flow.jit.authority import (
    AdmissionPolicy,
    PreapprovedRepairs,
    PrincipalRegistry,
    public_hex,
    register_principal,
    sign_policy,
)
from openadapt_flow.repair.candidate import Actor
from openadapt_flow.repair.cli import automatic_repairs
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.queue import RepairQueue, WorkItem
from openadapt_flow.repair.self_repair import (
    AppSession,
    ReplayTarget,
    ScreenAccess,
    SelfRepair,
    preapproved_change_class,
)
from tests.test_self_repair import (  # noqa: F401 - the shared MockMed fixture
    QUERY,
    _browser,
    _phone,
    _records,
    _reset,
    mockmed,
)
from tests.test_self_repair_bridge import BridgeAgent, click

pytest.importorskip("playwright.sync_api")

NOTE = "Synthetic follow-up in two weeks"


def _policy(tmp_path: Path, base_url: str, **classes) -> tuple:
    """A repair policy signed by a registered person, and its registry."""

    key = Ed25519PrivateKey.generate()
    key_dir = tmp_path / "keys"
    register_principal(
        key_dir,
        role="policy_signer",
        principal_id="ops-lead",
        public_key_hex=public_hex(key),
    )
    now = datetime.now(timezone.utc)
    policy = AdmissionPolicy(
        policy_id="ui-drift-standard-changes",
        signer="ops-lead",
        signed_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(days=30)).isoformat(),
        allowed_hosts=(base_url.split("//", 1)[1].rstrip("/"),),
        allowed_actions=("click", "type"),
        agent_principals=(BridgeAgent.agent_id,),
        preapproved_repairs=PreapprovedRepairs(**classes),
    )
    return sign_policy(policy, key), PrincipalRegistry.load(key_dir)


def _target(base_url: str, drift: Optional[str]) -> ReplayTarget:
    """MockMed with a fresh system of record for every session it opens.

    No session writes before an approved resume, so each item's resumed run
    writes into an empty store and its record check reads exactly that save.
    """

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.effects import RestRecordVerifier

    def url(fault: Optional[str]) -> str:
        drifts = ",".join(d for d in (drift, fault) if d)
        return f"{base_url}/{QUERY}" + (f"&drift={drifts}" if drifts else "") + "#tasks"

    def open_session(fault: Optional[str]) -> AppSession:
        _reset(base_url)
        backend, close = PlaywrightBackend.launch(url(fault), headless=True)
        return AppSession(backend=backend, close=close)

    return ReplayTarget(
        open_session,
        effect_verifier=RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        ),
        policy="clinical-write",
        params={"note": NOTE},
        entry_url=url(None),
        execution_origin=base_url,
    )


def _repair(base_url, tmp_path, agent, drift, **kwargs) -> SelfRepair:
    kwargs.setdefault("fault_probes", ("missing",))
    return SelfRepair(
        agent,
        _target(base_url, drift),
        store=tmp_path / "repair-store",
        screen_access=ScreenAccess(enabled_by="ops-lead", mode="allow_unscrubbed"),
        **kwargs,
    )


def _nobody(_seconds: float) -> None:
    raise AssertionError("a person was asked")


def test_a_relabelled_link_is_repaired_without_a_person(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    signed, registry = _policy(tmp_path, base_url, relabel_or_move=True)
    agent = BridgeAgent([click("#start-encounter")])
    with _browser():
        result = _repair(
            base_url,
            tmp_path,
            agent,
            "relink",
            repair_policy=signed,
            policy_registry=registry,
            approval_wait_s=60,
            sleep=_nobody,
        ).run(bundle, tmp_path / "run")

    assert result.status == "resumed", result.evidence.reason
    assert result.report.execution_outcome == "VERIFIED"
    assert len(_records(base_url)) == 1
    evidence = result.evidence
    assert evidence.applied_automatically
    assert evidence.change_class == "relabel_or_move"
    assert evidence.approval_route == "repair_policy"
    assert evidence.repair_policy_digest == signed.policy.digest()
    assert not (tmp_path / "run" / "item-approval").exists()
    store = RepairStore(tmp_path / "repair-store")
    candidate = store.load_candidate(evidence.candidate_id)
    assert candidate.approval.pre_approved_by == "ops-lead"
    assert candidate.approval.approved_by.startswith("repair-policy:")
    assert candidate.state == "canary"
    # The daily digest shows it, with the one command that undoes it.
    rows = automatic_repairs(store)
    assert [row["candidate_id"] for row in rows] == [candidate.candidate_id]
    assert "relabel_or_move" in rows[0]["change"]
    assert "repair rollback --candidate-id" in rows[0]["undo"]
    undone = store.rollback(Actor(kind="human", id="ops-lead"), candidate.candidate_id)
    assert undone.state == "rolled_back"
    assert store.active_pointer().active_digest == candidate.prior_content_digest


def test_changes_outside_the_policy_still_go_to_a_person(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    relabel, registry = _policy(tmp_path / "a", base_url, relabel_or_move=True)
    dialogs_only, registry_b = _policy(
        tmp_path / "b", base_url, dialog_patterns=("Ordering provider",)
    )
    with _browser():
        submit = _repair(
            base_url,
            tmp_path / "save",
            BridgeAgent([click("#submit-encounter")]),
            "redesign",
            repair_policy=relabel,
            policy_registry=registry,
            approval_wait_s=0,
        ).run(bundle, tmp_path / "save" / "run")
        other_class = _repair(
            base_url,
            tmp_path / "class",
            BridgeAgent([click("#start-encounter")]),
            "relink",
            repair_policy=dialogs_only,
            policy_registry=registry_b,
            approval_wait_s=0,
        ).run(bundle, tmp_path / "class" / "run")

    # The save control changed: never pre-approvable.
    assert submit.status == "awaiting_approval"
    assert not submit.evidence.applied_automatically
    assert any(
        "the save step step_005 changed" in s.detail for s in submit.evidence.steps
    )
    # A relabelled link, but the policy only pre-approves dialogs.
    assert other_class.status == "awaiting_approval"
    assert any(
        "relabel_or_move is not a class the repair policy allows" in s.detail
        for s in other_class.evidence.steps
    )
    assert len(_records(base_url)) == 0


def test_changed_identity_evidence_is_never_pre_approved(
    mockmed,  # noqa: F811 - the shared fixture
) -> None:
    _base_url, bundle = mockmed
    prior = Workflow.load(bundle)
    patched = prior.model_copy(deep=True)
    step = next(s for s in patched.steps if s.id == "step_001")
    template = step.anchor.identity_template
    step.anchor = step.anchor.model_copy(
        update={
            "identity_template": template.model_copy(update={"structured": "0" * 16}),
            "click_point": (1, 1),
        }
    )
    allowed = PreapprovedRepairs(relabel_or_move=True, menu_steps=2)
    change, why = preapproved_change_class(prior, patched, allowed)
    assert change is None
    assert "step_001's identity evidence changed" in why
    retargeted = prior.model_copy(deep=True)
    step = next(s for s in retargeted.steps if s.id == "step_001")
    step.anchor = step.anchor.model_copy(update={"click_point": (1, 1)})
    assert preapproved_change_class(prior, retargeted, allowed) == (
        "relabel_or_move",
        "",
    )


def test_the_pre_flight_catches_drift_before_the_first_item(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    signed, registry = _policy(tmp_path, base_url, relabel_or_move=True)
    agent = BridgeAgent([click("#start-encounter")])
    queue = RepairQueue(
        _repair(
            base_url,
            tmp_path,
            agent,
            "relink",
            repair_policy=signed,
            policy_registry=registry,
            sleep=_nobody,
        ),
        test_item=WorkItem("TEST-001", {"note": NOTE}),
    )
    with _browser():
        result = queue.run(
            bundle,
            [WorkItem("item-1", {"note": NOTE}), WorkItem("item-2", {"note": NOTE})],
            tmp_path / "queue",
        )

    assert result.started, result.log
    # The test record halted on the drift and was repaired automatically; the
    # second test run, on the repaired bundle, was clean.
    assert [r.status for r in result.preflight] == ["resumed", "not_needed"]
    assert result.preflight[0].evidence.applied_automatically
    assert result.bundle == result.preflight[0].patched_bundle
    # Every real item then ran on the repaired bundle without halting.
    assert [r.status for r in result.items.values()] == ["not_needed", "not_needed"]
    assert all(r.report.execution_outcome == "VERIFIED" for r in result.items.values())
    assert len(agent.requests) == 1


def test_one_approval_releases_every_item_held_at_the_same_halt(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path: Path,
) -> None:
    base_url, bundle = mockmed
    root = tmp_path / "queue"
    agent = BridgeAgent([click("#submit-encounter")])
    seen: list[int] = []
    queue = RepairQueue(
        _repair(
            base_url,
            tmp_path,
            agent,
            "redesign",
            approval_wait_s=60,
            sleep=_phone(root, approve=True, seen=seen, base_url=base_url),
        ),
        test_item=None,
    )
    items = [WorkItem(f"item-{n}", {"note": NOTE}) for n in range(1, 6)]
    with _browser():
        result = queue.run(bundle, items, root)

    # One proposal, one request, one tap.
    assert len(agent.requests) == 1
    assert seen == [0]
    requests = list(root.glob("*/item-approval/request-*.json"))
    assert len(requests) == 1
    statuses = {item_id: r.status for item_id, r in result.items.items()}
    assert statuses == {item.item_id: "resumed" for item in items}, result.log
    assert all(r.report.execution_outcome == "VERIFIED" for r in result.items.values())
    released = [result.items[f"item-{n}"] for n in range(2, 6)]
    assert all(
        any("released by the approval" in s.detail for s in r.evidence.steps)
        for r in released
    )
    assert len({r.evidence.candidate_id for r in result.items.values()}) == 1
