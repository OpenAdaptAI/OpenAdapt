"""Delegated approvers, the approver policy, read-back field checks and
exceptions-only eligibility. Made-up values only.

The default policy is person-only, so nothing here changes a run that
doesn't opt in. An agent or system answers only where the run's policy
allows its kind for the step class, a clinical write needs an explicit
opt-in, and a delegated decision is recorded with its identity and never
becomes a training label.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from types import SimpleNamespace

import pytest

from openadapt_flow.approval_mode.approver import (
    DEFAULT_APPROVER_POLICY,
    ApproverAuth,
    ApproverIdentityV1,
    ApproverKind,
    ApproverPolicyV1,
    DelegatedDecision,
    ReadBackTier,
    StepClass,
    WebhookApprover,
    WebhookRefused,
    is_training_label,
    read_back_tier_for,
    step_class_for,
)
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalRefused,
    ItemApprovalStore,
    answer_refusal,
    make_answer,
    request_facts,
    wait_for_answer,
)
from openadapt_flow.approval_mode.model import ApprovalMode, ItemApprovalRef
from tests._approver_grant_support import grant_store, signed_grant
from tests._approver_keys import answer_as_person

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
FORM = ("dom", "sha256:" + "1" * 64)
OPT_IN = "sha256:" + "c" * 64
SECRET = b"s" * 32

AGENT = ApproverIdentityV1(
    kind=ApproverKind.AGENT, auth=ApproverAuth.LOCAL_CLI, id="agent:approver-bot"
)
WEBHOOK_SYSTEM = ApproverIdentityV1(
    kind=ApproverKind.SYSTEM, auth=ApproverAuth.SIGNED_WEBHOOK, id="system:acme"
)


def _open(store: ItemApprovalStore, step_class=StepClass.CONSEQUENTIAL):
    return store.open_request(
        run_id="run-0001",
        workflow_name="referral",
        step_id="save",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=FORM,
        step_class=step_class,
    )


def _agent_answer(request, store, approver=AGENT, route="local_cli"):
    return make_answer(
        request,
        decision="approve",
        decided_by="automation",
        route=route,
        approver_id=None,
        auth_method="none",
        approver=approver,
        policy=store.context(),
    )


def _allow_agents(*classes: StepClass, opt_in=None) -> ApproverPolicyV1:
    allowed = {c.value: ["person"] for c in StepClass}
    allowed.update({c.value: ["person", "agent", "system"] for c in classes})
    data: dict = {"allowed": allowed}
    if opt_in:
        data["clinical_write_opt_in_sha256"] = opt_in
    return ApproverPolicyV1.model_validate(data)


# -- policy ---------------------------------------------------------------------


def test_the_default_policy_refuses_an_agent(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    assert store.policy() == DEFAULT_APPROVER_POLICY
    with pytest.raises(ItemApprovalRefused) as err:
        _agent_answer(request, store)
    assert err.value.code == "approver_kind_not_allowed"


def test_a_policy_that_allows_agents_records_the_agent(tmp_path):
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    request = _open(store)
    answer = _agent_answer(request, store)
    store.write_answer(answer)
    assert store.status(request) == "approved"
    ref = answer.reference(request)
    assert isinstance(ref, ItemApprovalRef)
    assert ref.decided_by == "automation"
    assert ref.route == "local_cli"
    assert ref.approver == AGENT.model_copy(
        update={"policy_ref": store.context().grant.policy_ref}
    )
    assert not is_training_label(answer.approver_identity())


def test_clinical_write_needs_the_opt_in(tmp_path):
    with pytest.raises(ValueError):
        _allow_agents(StepClass.CLINICAL_WRITE)  # no opt-in: refused outright
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    request = _open(store, StepClass.CLINICAL_WRITE)
    with pytest.raises(ItemApprovalRefused) as err:
        _agent_answer(request, store)
    assert err.value.code == "clinical_write_needs_opt_in"

    opted = grant_store(
        tmp_path, _allow_agents(StepClass.CLINICAL_WRITE, opt_in=OPT_IN), run="run2"
    )
    request = _open(opted, StepClass.CLINICAL_WRITE)
    assert _agent_answer(request, opted).approver.id == AGENT.id


def test_a_run_keeps_its_first_policy(tmp_path):
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    store.write_policy(_allow_agents(StepClass.CONSEQUENTIAL))  # same: fine
    with pytest.raises(ItemApprovalRefused) as err:
        store.write_policy(DEFAULT_APPROVER_POLICY)
    assert err.value.code == "policy_changed"
    with pytest.raises(ItemApprovalRefused) as err:
        store.write_grant(signed_grant(tmp_path, _allow_agents(StepClass.IRREVERSIBLE)))
    assert err.value.code == "policy_changed"


def test_an_unreadable_policy_means_person_only(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    _open(store)
    (store.dir / "approver-policy.json").write_text("{not json")
    assert store.policy() == DEFAULT_APPROVER_POLICY


def test_an_agent_cannot_pose_as_a_person(tmp_path):
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    request = _open(store)
    with pytest.raises(ItemApprovalRefused) as err:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
            approver=AGENT,
            policy=store.policy(),
        )
    assert err.value.code == "answer_fields_inconsistent"
    with pytest.raises(ItemApprovalRefused) as err:
        _agent_answer(request, store, route="desktop_run_it")
    assert err.value.code == "route_not_allowed"


def test_step_class_comes_from_the_run_policy():
    save = SimpleNamespace(effects=[SimpleNamespace(risk="reversible")])
    final = SimpleNamespace(effects=[SimpleNamespace(risk="irreversible")])
    assert step_class_for(save) is StepClass.CONSEQUENTIAL
    assert step_class_for(final) is StepClass.IRREVERSIBLE
    assert (
        step_class_for(save, policy_name="clinical-write") is StepClass.CLINICAL_WRITE
    )
    assert step_class_for(save, policy_name="regulated") is StepClass.CLINICAL_WRITE


def test_a_person_answer_keeps_its_old_bytes(tmp_path, approver):
    """No approver and the default step class: no delegate field is serialized.

    A person's answer now also needs its approver key's signature. The
    signature is added as one ``attestation`` field over the answer's earlier
    bytes, which are otherwise unchanged; without it the answer is refused.
    """

    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    answer = answer_as_person(
        store,
        request,
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_id=None,
        auth_method="none",
    )
    dumped = answer.model_dump(mode="json")
    assert "approver" not in dumped
    assert "step_class" not in request.model_dump(mode="json")
    ref = answer.reference(request).model_dump(mode="json")
    assert "approver" not in ref
    # The signature covers exactly the earlier bytes and adds only itself.
    assert set(dumped) - set(answer.signed_payload()) == {"attestation"}
    unsigned = answer.model_copy(update={"attestation": None})
    assert unsigned.model_dump(mode="json") == answer.signed_payload()
    assert answer_refusal(request, answer, policy=store.context()) is None
    assert answer_refusal(request, unsigned, policy=store.context()) == (
        "human_answer_unsigned"
    )


def test_a_claimed_cloud_sign_in_no_longer_makes_a_person_answer(tmp_path, approver):
    """Nothing here can verify a Cloud sign-in, so claiming one is refused."""

    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    with pytest.raises(ItemApprovalRefused) as err:
        answer_as_person(
            store,
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id="cloud-user-0001",
            auth_method="cloud_aal2",
        )
    assert err.value.code == "auth_method_unverified"
    assert store.read_answer(request.request_id) is None


# -- delegated waiting ------------------------------------------------------------


class _Delegate:
    route = "webhook"

    def __init__(self, replies, identity=WEBHOOK_SYSTEM):
        self.identity = identity
        self.replies = list(replies)
        self.seen: list[dict] = []

    def decide(self, facts):
        self.seen.append(dict(facts))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _clock():
    state = {"t": 0.0}

    def monotonic():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    return monotonic, sleep


def test_a_delegate_answers_a_waiting_save(tmp_path):
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    request = _open(store)
    delegate = _Delegate([None, DelegatedDecision(decision="approve")])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store,
        request,
        timeout_s=60,
        poll_s=1,
        monotonic=monotonic,
        sleep=sleep,
        approver=delegate,
    )
    assert code is None and answer is not None
    assert answer.decision == "approve" and answer.approver.id == WEBHOOK_SYSTEM.id
    assert answer.approver.policy_ref == store.context().grant.policy_ref
    # The delegate saw no intent, no screenshot bytes and no values.
    facts = delegate.seen[0]
    assert "step_intent" not in facts and FRAME.decode(
        errors="ignore"
    ) not in json.dumps(facts)
    assert facts == request_facts(request)


def test_a_delegate_the_policy_forbids_is_never_asked(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    delegate = _Delegate([DelegatedDecision(decision="approve")])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store,
        request,
        timeout_s=3,
        poll_s=1,
        monotonic=monotonic,
        sleep=sleep,
        approver=delegate,
    )
    assert answer is None and delegate.seen == []


def test_a_failing_delegate_leaves_the_save_to_a_person(tmp_path):
    store = grant_store(tmp_path, _allow_agents(StepClass.CONSEQUENTIAL))
    request = _open(store)
    delegate = _Delegate([WebhookRefused("reply_signature_invalid")])
    monotonic, sleep = _clock()
    answer, code = wait_for_answer(
        store,
        request,
        timeout_s=30,
        poll_s=1,
        monotonic=monotonic,
        sleep=sleep,
        approver=delegate,
    )
    assert answer is None
    assert len(delegate.seen) == 1  # asked once, then left to a person


# -- webhook ------------------------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: bytes, signature: str):
        super().__init__(body)
        self.headers = {"X-OpenAdapt-Signature": signature}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, reply: dict, *, sign_with: bytes = SECRET):
        self.reply = reply
        self.sign_with = sign_with
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        body = json.dumps(self.reply).encode()
        sig = "sha256=" + hmac.new(self.sign_with, body, hashlib.sha256).hexdigest()
        return _Response(body, sig)


FACTS = {"request_id": "r1", "request_sha256": "sha256:" + "d" * 64}


def _hook(opener):
    return WebhookApprover(
        "https://approvals.example.test/hook", SECRET, WEBHOOK_SYSTEM, opener=opener
    )


def test_webhook_signs_and_accepts_a_signed_reply():
    opener = _Opener({"request_sha256": FACTS["request_sha256"], "decision": "approve"})
    decided = _hook(opener).decide(FACTS)
    assert decided == DelegatedDecision(decision="approve")
    sent = opener.requests[0]
    body = sent.data
    want = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert sent.headers["X-openadapt-signature"] == want


def test_webhook_pending_means_no_answer_yet():
    opener = _Opener({"request_sha256": FACTS["request_sha256"], "decision": "pending"})
    assert _hook(opener).decide(FACTS) is None


@pytest.mark.parametrize(
    ("opener", "code"),
    [
        (
            _Opener(
                {"request_sha256": FACTS["request_sha256"], "decision": "approve"},
                sign_with=b"x" * 32,
            ),
            "reply_signature_invalid",
        ),
        (
            _Opener({"request_sha256": "sha256:" + "e" * 64, "decision": "approve"}),
            "reply_for_another_request",
        ),
        (
            _Opener({"request_sha256": FACTS["request_sha256"], "decision": "maybe"}),
            "reply_fields_invalid",
        ),
    ],
)
def test_webhook_refuses_bad_replies(opener, code):
    with pytest.raises(WebhookRefused) as err:
        _hook(opener).decide(FACTS)
    assert str(err.value) == code


@pytest.mark.parametrize(
    ("url", "secret", "identity"),
    [
        ("http://approvals.example.test/hook", SECRET, WEBHOOK_SYSTEM),
        ("https://approvals.example.test/hook", b"short", WEBHOOK_SYSTEM),
        ("https://approvals.example.test/hook", SECRET, AGENT),  # not webhook auth
    ],
)
def test_webhook_setup_is_checked(url, secret, identity):
    with pytest.raises(ValueError):
        WebhookApprover(url, secret, identity)


# -- types conformance -------------------------------------------------------------


def test_mirror_matches_the_core_contract():
    # Since the fold the contract ships in the core, so this always runs.
    import openadapt_types.approval as approval

    for name in ("ApproverKind", "ApproverAuth", "StepClass"):
        theirs = {m.value for m in getattr(approval, name)}
        ours = {m.value for m in globals()[name]}
        assert ours == theirs, name
    assert {m.name for m in approval.ReadBackTier} == {m.name for m in ReadBackTier}
    assert set(approval.ApproverIdentityV1.model_fields) == set(
        ApproverIdentityV1.model_fields
    )
    assert set(approval.ApproverPolicyV1.model_fields) == set(
        ApproverPolicyV1.model_fields
    )
    dumped = AGENT.model_dump(mode="json")
    assert (
        approval.ApproverIdentityV1.model_validate(dumped).model_dump(mode="json")
        == dumped
    )


def test_flow_tiers_follow_rfc_0002():
    assert read_back_tier_for(1) is ReadBackTier.INDEPENDENT_READBACK
    assert read_back_tier_for(2) is ReadBackTier.SCREEN_READBACK  # UI, other session
    assert read_back_tier_for(3) is ReadBackTier.SCREEN_READBACK
    assert read_back_tier_for(4) is ReadBackTier.SCREEN_READBACK
    assert read_back_tier_for(None) is ReadBackTier.UNCHECKED
    assert ReadBackTier.SCREEN_READBACK.label == "screen_readback"
