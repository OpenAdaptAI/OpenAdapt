"""Signed approver grants and self-approval. Made-up ids.

Invariants under test:

* only a person-signed grant that verifies now lets an agent or system
  answer a save; an unsigned, tampered, expired or wrongly signed one means
  person-only;
* the agent or system that started a run can't answer that run's saves;
* every answer names who decided: a person (and where), or the delegate and
  the digest of the grant it acted under.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode.approver import (
    ApproverAuth,
    ApproverIdentityV1,
    ApproverKind,
    ApproverPolicyV1,
    StepClass,
)
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalRefused,
    ItemApprovalStore,
    decided_by_summary,
    make_answer,
)
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.approval_mode.signed_policy import (
    ApproverGrant,
    load_approver_policy,
    sign_grant,
)
from openadapt_flow.jit.authority import public_hex, register_principal
from tests._approver_grant_support import (
    SIGNER,
    grant_store,
    key_dir,
    make_grant,
    signed_grant,
    signer_key,
)

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
FORM = ("dom", "sha256:" + "1" * 64)
AGENT = ApproverIdentityV1(
    kind=ApproverKind.AGENT, auth=ApproverAuth.LOCAL_CLI, id="agent:approver-bot"
)


def _policy() -> ApproverPolicyV1:
    allowed = {c.value: ["person"] for c in StepClass}
    allowed[StepClass.CONSEQUENTIAL.value] = ["person", "agent", "system"]
    return ApproverPolicyV1.model_validate({"allowed": allowed})


def _open(store: ItemApprovalStore, workflow: str = "referral"):
    return store.open_request(
        run_id="run-0001",
        workflow_name=workflow,
        step_id="save",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=FORM,
    )


def _agent_answer(store, request, approver=AGENT):
    return make_answer(
        request,
        decision="approve",
        decided_by="automation",
        route="local_cli",
        approver_id=None,
        auth_method="none",
        approver=approver,
        policy=store.context(),
    )


def _refused(store, request, approver=AGENT) -> str:
    with pytest.raises(ItemApprovalRefused) as err:
        _agent_answer(store, request, approver)
    return err.value.code


# -- grants -----------------------------------------------------------------------


def test_a_signed_grant_lets_its_named_agent_answer(tmp_path):
    store = grant_store(tmp_path, _policy())
    request = _open(store)
    answer = _agent_answer(store, request)
    store.write_answer(answer)
    grant = store.context().grant
    assert grant is not None and grant.signer == SIGNER
    assert answer.approver.policy_ref == grant.policy_ref
    summary = decided_by_summary(store, request)
    assert summary["kind"] == "agent" and summary["id"] == AGENT.id
    assert summary["policy_ref"] == grant.policy_ref


def test_an_unsigned_policy_that_admits_an_agent_is_person_only(tmp_path):
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir(tmp_path))
    store.write_policy(_policy())
    store.write_initiator(None)
    request = _open(store)
    assert _refused(store, request) == "approver_policy_unsigned"


def test_a_planted_policy_file_gives_no_authority(tmp_path):
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir(tmp_path))
    store._checked_dir(create=True)
    (store.dir / "approver-policy.json").write_text(_policy().model_dump_json())
    store.write_initiator(None)
    request = _open(store)
    assert store.policy().allowed[StepClass.CONSEQUENTIAL] == (ApproverKind.PERSON,)
    assert _refused(store, request) == "approver_policy_unsigned"


def test_a_tampered_grant_is_refused(tmp_path):
    store = grant_store(tmp_path, _policy())
    request = _open(store)
    path = store.dir / "approver-grant.json"
    data = json.loads(path.read_text())
    data["grant"]["delegates"] = ["agent:approver-bot", "agent:intruder"]
    path.chmod(0o600)
    path.unlink()
    path.write_text(json.dumps(data))
    assert _refused(store, request) == "approver_grant_signature_invalid"


def test_a_grant_swapped_for_another_policy_is_refused(tmp_path):
    store = grant_store(tmp_path, _policy())
    request = _open(store)
    other = signed_grant(
        tmp_path,
        ApproverPolicyV1.model_validate(
            {
                "allowed": {
                    **{c.value: ["person"] for c in StepClass},
                    "irreversible": ["person", "agent"],
                }
            }
        ),
    )
    path = store.dir / "approver-grant.json"
    path.unlink()
    path.write_text(other.model_dump_json(by_alias=True))
    assert _refused(store, request) == "approver_grant_policy_mismatch"


def test_an_expired_grant_is_refused(tmp_path):
    start = datetime.now(timezone.utc) - timedelta(days=40)
    store = grant_store(tmp_path, _policy(), signed_at=start, days=30)
    request = _open(store)
    assert _refused(store, request) == "approver_grant_expired"


def test_a_grant_signed_by_an_unregistered_key_is_refused(tmp_path):
    signer_key(tmp_path)  # registers the real signer
    impostor = Ed25519PrivateKey.generate()
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir(tmp_path))
    store.write_grant(sign_grant(make_grant(_policy()), impostor))
    store.write_initiator(None)
    request = _open(store)
    assert _refused(store, request) == "approver_grant_signer_key_mismatch"


def test_an_agent_key_cannot_sign_a_grant(tmp_path):
    agent_key = Ed25519PrivateKey.generate()
    register_principal(
        key_dir(tmp_path),
        role="agent",
        principal_id="agent-signer",
        public_key_hex=public_hex(agent_key),
    )
    grant = make_grant(_policy()).model_copy(update={"signer": "agent-signer"})
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir(tmp_path))
    store.write_grant(sign_grant(grant, agent_key))
    store.write_initiator(None)
    request = _open(store)
    assert _refused(store, request) == "approver_grant_signer_unregistered"


def test_a_grant_must_name_the_delegate_and_the_workflow(tmp_path):
    store = grant_store(tmp_path, _policy(), workflows=("intake",))
    other = ApproverIdentityV1(
        kind=ApproverKind.AGENT, auth=ApproverAuth.LOCAL_CLI, id="agent:other"
    )
    assert _refused(store, _open(store, "intake"), other) == "delegate_not_in_grant"
    assert _refused(store, _open(store, "referral")) == "workflow_not_in_grant"
    assert _agent_answer(store, _open(store, "intake")).approver.id == AGENT.id


def test_an_answer_pinned_to_another_grant_is_refused(tmp_path):
    store = grant_store(tmp_path, _policy())
    pinned = AGENT.model_copy(update={"policy_ref": "sha256:" + "e" * 64})
    assert _refused(store, _open(store), pinned) == "policy_ref_mismatch"


def test_grant_shape_rules():
    with pytest.raises(ValueError):
        make_grant(_policy(), delegates=(SIGNER,))  # signer can't be a delegate
    with pytest.raises(ValueError):
        make_grant(ApproverPolicyV1())  # a person-only grant grants nothing
    with pytest.raises(ValueError):
        make_grant(_policy(), days=400)


def test_load_approver_policy(tmp_path):
    keys = key_dir(tmp_path)
    good = tmp_path / "grant.json"
    signed_grant(tmp_path, _policy()).save(good)
    loaded = load_approver_policy(good, keys)
    assert loaded.verified is not None and loaded.refusal is None
    unsigned = tmp_path / "policy.json"
    unsigned.write_text(_policy().model_dump_json())
    loaded = load_approver_policy(unsigned, keys)
    assert loaded.refusal == "approver_policy_unsigned"
    assert loaded.policy == ApproverPolicyV1()
    person_only = tmp_path / "person.json"
    person_only.write_text(ApproverPolicyV1().model_dump_json())
    assert load_approver_policy(person_only, keys).refusal is None
    (tmp_path / "bad.json").write_text("{")
    assert load_approver_policy(tmp_path / "bad.json", keys).refusal == (
        "approver_grant_unreadable"
    )


# -- self-approval ----------------------------------------------------------------


def test_an_agent_cannot_approve_a_run_it_started(tmp_path):
    store = grant_store(tmp_path, _policy(), initiator=AGENT.id)
    assert _refused(store, _open(store)) == "self_approval"


def test_an_agent_cannot_approve_on_behalf_of_the_initiator(tmp_path):
    store = grant_store(tmp_path, _policy(), initiator="agent:runner")
    proxy = AGENT.model_copy(update={"on_behalf_of": "agent:runner"})
    assert _refused(store, _open(store), proxy) == "self_approval"


def test_no_delegate_answers_a_run_an_unnamed_agent_started(tmp_path):
    store = grant_store(tmp_path, _policy(), initiator="agent:unidentified")
    assert _refused(store, _open(store)) == "self_approval_unprovable"


def test_no_delegate_answers_when_the_initiator_was_never_recorded(tmp_path):
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir(tmp_path))
    store.write_grant(signed_grant(tmp_path, _policy()))
    assert _refused(store, _open(store)) == "run_initiator_unknown"


def test_a_planted_initiator_refuses_the_run(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    store._checked_dir(create=True)
    (store.dir / "run-initiator.json").write_text('{"started_by": null}x')
    with pytest.raises(ItemApprovalRefused) as err:
        store.write_initiator(None)
    assert err.value.code == "initiator_changed"
    store2 = ItemApprovalStore(tmp_path / "run2")
    store2.write_initiator("agent:a")
    store2.write_initiator("agent:a")  # same value: fine
    with pytest.raises(ItemApprovalRefused):
        store2.write_initiator(None)


def test_unknown_grant_fields_are_refused():
    data = json.loads(make_grant(_policy()).model_dump_json(by_alias=True))
    data["self_enable"] = True
    with pytest.raises(ValueError):
        ApproverGrant.model_validate(data)
