"""Signed approver grants, self-approval, and phone decisions. Made-up ids.

Invariants under test:

* only a person-signed grant that verifies now lets an agent or system
  answer a save; an unsigned, tampered, expired or wrongly signed one means
  person-only;
* the agent or system that started a run can't answer that run's saves;
* a phone answer counts only with a Cloud-signed decision for the exact
  request digest, from a person signed in with AAL2, verified against a key
  pinned on this runner, and it is used once;
* every answer names who decided: a person (and where), or the delegate and
  the digest of the grant it acted under.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode.approver import (
    ApproverAuth,
    ApproverIdentityV1,
    ApproverKind,
    ApproverPolicyV1,
    StepClass,
)
from openadapt_flow.approval_mode.cloud_decision import sign_decision
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalRefused,
    ItemApprovalStore,
    apply_cloud_decision,
    decided_by_summary,
    make_answer,
    wait_for_answer,
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


# -- phone decisions --------------------------------------------------------------

CLOUD_KEY = Ed25519PrivateKey.generate()
CLOUD_PUB = base64.b64encode(
    CLOUD_KEY.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
).decode()
PINNED = {"cloud-decision-1": CLOUD_PUB}


def _decision(request, *, key=CLOUD_KEY, **overrides):
    fields = {
        "request_id": request.request_id,
        "request_sha256": request.digest(),
        "decision": "approve",
        "approver_id": "cloud-user-0001",
        "auth_method": "cloud_aal2",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "link_id": "L" * 22,
        "runner_id": "runner-0001",
        "key_id": "cloud-decision-1",
    }
    fields.update(overrides)
    return sign_decision(fields, key).model_dump(mode="json")


def _phone_store(tmp_path, pinned=PINNED):
    store = ItemApprovalStore(tmp_path / "run", cloud_keys=pinned)
    store.write_initiator(None)
    return store


def test_a_signed_phone_decision_becomes_the_answer(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    answer = apply_cloud_decision(store, request, _decision(request))
    assert answer is not None and answer.route == "cloud_phone"
    assert store.status(request) == "approved"
    ref = answer.reference(request)
    assert ref.route == "cloud_phone" and ref.decided_by == "human"
    assert ref.approver_id == "cloud-user-0001" and ref.auth_method == "cloud_aal2"
    summary = decided_by_summary(store, request)
    assert summary["kind"] == "person" and summary["route"] == "cloud_phone"
    # Used once: the second save can't reuse it, and a replay writes nothing.
    assert store.consume(request.request_id) is True
    assert store.consume(request.request_id) is False
    assert apply_cloud_decision(store, request, _decision(request)) is None


def test_a_phone_decline_carries_its_reason(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    raw = _decision(request, decision="decline", decline_reason="wrong_values")
    answer = apply_cloud_decision(store, request, raw)
    assert answer is not None and answer.decline_reason == "wrong_values"
    assert store.status(request) == "declined"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {"request_sha256": "sha256:" + "0" * 64},
        lambda r: {"key_id": "unpinned"},
        lambda r: {"auth_method": "cloud_aal1"},
    ],
)
def test_phone_decisions_that_dont_bind_are_refused(tmp_path, mutate):
    store = _phone_store(tmp_path)
    request = _open(store)
    try:
        raw = _decision(request, **mutate(request))
    except ValueError:
        return  # refused at the contract: an AAL1 decision can't even be built
    assert apply_cloud_decision(store, request, raw) is None
    assert store.status(request) == "waiting"


def test_a_decision_for_one_request_cant_answer_another(tmp_path):
    store = _phone_store(tmp_path)
    first, second = _open(store), _open(store)
    raw = _decision(first)
    raw_for_second = {**raw, "request_id": second.request_id}
    assert apply_cloud_decision(store, second, raw_for_second) is None
    assert apply_cloud_decision(store, second, raw) is None


def test_a_forged_signature_is_refused(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    forged = _decision(request, key=Ed25519PrivateKey.generate())
    assert apply_cloud_decision(store, request, forged) is None


def test_no_pinned_key_means_no_phone_route(tmp_path):
    store = _phone_store(tmp_path, pinned={})
    request = _open(store)
    assert apply_cloud_decision(store, request, _decision(request)) is None


def test_a_hand_written_phone_answer_is_refused(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    with pytest.raises(ItemApprovalRefused) as err:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="cloud_phone",
            approver_id="cloud-user-0001",
            auth_method="cloud_aal2",
            policy=store.context(),
        )
    assert err.value.code == "cloud_decision_missing"


class _Relay:
    runner_id = "runner-0001"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.published: list[bytes] = []
        self.withdrawn: list[str] = []

    def publish(self, request, frame_png):
        self.published.append(frame_png)
        return True

    def poll(self, request):
        return self.decisions.pop(0) if self.decisions else None

    def withdraw(self, request, reason):
        self.withdrawn.append(reason)


def _clock():
    state = {"t": 0.0}
    return (lambda: state["t"]), (lambda s: state.__setitem__("t", state["t"] + s))


def test_the_run_waits_for_a_phone_decision(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    relay = _Relay([None, _decision(request)])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store,
        request,
        timeout_s=60,
        poll_s=1,
        monotonic=monotonic,
        sleep=sleep,
        phone_relay=relay,
    )
    assert code is None and answer.route == "cloud_phone"
    assert relay.published == [FRAME]
    assert relay.withdrawn == []  # the phone answered; nothing to withdraw


def test_a_desktop_answer_wins_and_the_phone_link_is_withdrawn(tmp_path):
    store = _phone_store(tmp_path)
    request = _open(store)
    store.write_answer(
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id="cloud-user-0002",
            auth_method="cloud_aal2",
        )
    )
    relay = _Relay([_decision(request)])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store, request, timeout_s=5, monotonic=monotonic, sleep=sleep, phone_relay=relay
    )
    assert answer.route == "desktop_run_it"
    assert relay.withdrawn == ["answered_elsewhere"]


def test_a_relay_failure_leaves_desktop_approval(tmp_path):
    class Broken(_Relay):
        def publish(self, request, frame_png):
            raise OSError("offline")

    store = _phone_store(tmp_path)
    request = _open(store)
    relay = Broken([])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store,
        request,
        timeout_s=3,
        poll_s=1,
        monotonic=monotonic,
        sleep=sleep,
        phone_relay=relay,
    )
    assert answer is None and code == "approval_wait_timed_out"
    assert relay.withdrawn == ["run_stopped_waiting"]


def test_unknown_grant_fields_are_refused():
    data = json.loads(make_grant(_policy()).model_dump_json(by_alias=True))
    data["self_enable"] = True
    with pytest.raises(ValueError):
        ApproverGrant.model_validate(data)
