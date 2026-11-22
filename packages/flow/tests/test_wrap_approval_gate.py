"""ItemApprovalGate: the fallback's pre-commit gate without a terminal.

A person answers in Desktop's Run it or on a paired phone; the answer is the
real item-approval store's, signed by a test approver key on a real roster
(tests/_approver_keys.py). The fallback's run 1 is the stand-in from
tests/test_wrap_fallback.py.
"""

# ruff: noqa: F811 - pytest fixtures are imported by name
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow import compile_agent
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from openadapt_flow.approval_mode.human_attestation import ApproverSigner
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalAnswer,
    ItemApprovalStore,
)
from openadapt_flow.approval_mode.model import DECISION_PORTAL_ROUTE
from openadapt_flow.wrap.approval_gate import ItemApprovalGate
from tests._approver_keys import answer_as_person
from tests.test_wrap_fallback import (  # noqa: F401 - fixtures
    PARAMS,
    PROMPT,
    Agent,
    install_reply,
    model,
    policy,
    run1,
)

FRAME = b"\x89PNG\r\n\x1a\nsynthetic gate frame"


def request(tmp_path: Path) -> compile_agent.GateRequest:
    shot = tmp_path / "gate.png"
    shot.write_bytes(FRAME)
    return compile_agent.GateRequest("Save", ("Synthetic record",), "", shot)


def answering(
    gate: ItemApprovalGate,
    write: Any,
) -> None:
    """Stand in for the person: on the gate's first wait, answer the one
    waiting request with ``write(store, request)``."""

    done: list = []

    def sleep(_seconds: float) -> None:
        if done:
            return
        store = ItemApprovalStore(gate._dir, key_dir=gate.key_dir)
        [asked] = store.requests()
        done.append(write(store, asked))

    gate._sleep = sleep


def person(decision: str = "approve", **extra: Any):
    def write(store: ItemApprovalStore, asked: Any) -> None:
        fields: dict[str, Any] = dict(
            decision=decision,
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
        )
        if decision == "decline":
            fields["decline_reason"] = "wrong_values"
        fields.update(extra)
        store.write_answer(answer_as_person(store, asked, **fields))

    return write


def bound(tmp_path: Path, **kw: Any) -> ItemApprovalGate:
    gate = ItemApprovalGate(wait_s=kw.pop("wait_s", 5.0), **kw)
    gate.bind(tmp_path / "fallback-run", "fallback-abc")
    return gate


def test_a_signed_person_approval_lets_the_save_through(tmp_path, approver) -> None:
    gate = bound(tmp_path)
    assert gate.attended
    answering(gate, person())
    assert gate(request(tmp_path)) is True
    [row] = gate.decisions
    assert row["decision"] == "approved" and row["signer"] == "front-desk"
    store = ItemApprovalStore(gate._dir)
    [asked] = store.requests()
    assert store.is_consumed(asked.request_id)
    assert asked.run_id == "fallback-abc" and asked.provenance.actor.startswith(
        "agent:"
    )
    # The person is shown the gate screenshot itself.
    assert store.read_frame(asked) == FRAME


def test_the_phone_portal_route_also_answers(tmp_path, approver) -> None:
    gate = bound(tmp_path)
    answering(gate, person(route=DECISION_PORTAL_ROUTE, signer=portal_signer()))
    assert gate(request(tmp_path)) is True
    assert gate.decisions[0]["route"] == DECISION_PORTAL_ROUTE


def test_a_decline_refuses(tmp_path, approver) -> None:
    gate = bound(tmp_path)
    answering(gate, person("decline"))
    assert gate(request(tmp_path)) is False
    assert gate.decisions[0]["code"] == "declined"


def test_an_unsigned_answer_refuses(tmp_path, approver) -> None:
    def forged(store: ItemApprovalStore, asked: Any) -> None:
        store.write_answer(
            ItemApprovalAnswer(
                request_id=asked.request_id,
                request_sha256=asked.digest(),
                decision="approve",
                decided_by="human",
                route="desktop_run_it",
                approver_kind="unauthenticated",
                auth_method="none",
                answered_at=asked.created_at,
            )
        )

    gate = bound(tmp_path)
    answering(gate, forged)
    assert gate(request(tmp_path)) is False
    assert gate.decisions[0]["code"] == "human_answer_unsigned"


@pytest.mark.parametrize("name", ["front-desk", "stranger"])
def test_a_key_off_the_pinned_roster_refuses(tmp_path, approver, name) -> None:
    """A new key claiming a roster name, or a name the roster doesn't hold."""
    stranger = ApproverSigner(
        principal_id=name, method="desktop_key", signing=Ed25519PrivateKey.generate()
    )

    def write(store: ItemApprovalStore, asked: Any) -> None:
        from openadapt_flow.approval_mode.item_approval import _iso, _utc_now

        answer = ItemApprovalAnswer(
            request_id=asked.request_id,
            request_sha256=asked.digest(),
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_kind="unauthenticated",
            auth_method="none",
            answered_at=_iso(_utc_now()),
        )
        from openadapt_flow.approval_mode.human_attestation import ANSWER_DOMAIN

        store.write_answer(
            answer.model_copy(
                update={
                    "attestation": stranger.attest(
                        ANSWER_DOMAIN, answer.signed_payload()
                    )
                }
            )
        )

    gate = bound(tmp_path)
    answering(gate, write)
    assert gate(request(tmp_path)) is False
    assert (
        gate.decisions[0]["code"]
        == {
            "front-desk": "human_answer_signature_invalid",
            "stranger": "human_approver_unregistered",
        }[name]
    )


def test_no_answer_before_expiry_refuses(tmp_path, approver) -> None:
    gate = bound(tmp_path, wait_s=0.3)
    gate._sleep = lambda s: __import__("time").sleep(s)
    assert gate(request(tmp_path)) is False
    assert gate.decisions[0]["code"] in ("request_expired", "approval_wait_timed_out")


def test_an_answer_after_expiry_refuses(tmp_path, approver) -> None:
    """Signed while the request was open, read after it expired."""
    import time

    def late(store: ItemApprovalStore, asked: Any) -> None:
        answer = answer_as_person(
            store,
            asked,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
        )
        time.sleep(0.4)
        store.write_answer(answer)

    gate = bound(tmp_path, wait_s=0.3)
    answering(gate, late)
    assert gate(request(tmp_path)) is False
    assert gate.decisions[0]["code"] in ("request_expired", "answer_after_expiry")


def test_no_screenshot_refuses_without_asking(tmp_path, approver) -> None:
    gate = bound(tmp_path)
    blind = compile_agent.GateRequest("Save", ("Synthetic record",), "", None)
    assert gate(blind) is False and gate.decisions[0]["code"] == "screenshot_required"
    assert (
        not (gate._dir / "item-approval").exists()
        or not ItemApprovalStore(gate._dir).requests()
    )


def test_an_unbound_gate_refuses(tmp_path, approver) -> None:
    gate = ItemApprovalGate(wait_s=1)
    assert gate(request(tmp_path)) is False


def test_no_pinned_root_means_nobody_can_answer(
    tmp_path, approver, monkeypatch
) -> None:
    monkeypatch.delenv(ROOT_ENV)
    assert ItemApprovalGate(wait_s=1).attended is False


# -- through the policy ---------------------------------------------------------


def test_a_service_fallback_saves_after_a_phone_approval(
    model,
    monkeypatch,
    run1,
    approver,
    tmp_path,
) -> None:
    install_reply(model, monkeypatch)
    gate = ItemApprovalGate(wait_s=5, approvals_dir=tmp_path / "approvals")
    real_bind = gate.bind

    def bind(run_root: Path, request_id: str) -> None:
        real_bind(run_root, request_id)
        answering(gate, person(route=DECISION_PORTAL_ROUTE, signer=portal_signer()))

    gate.bind = bind  # type: ignore[method-assign]
    chosen = policy(model, gate=gate)
    result = chosen.run(PROMPT, PARAMS, Agent())
    assert result.verified and result.status == "VERIFIED"
    assert model.rows == [{"id": "f1", "kind": "synthetic", "value": "one"}]
    # The approval folder sits where Desktop or the portal scans for it.
    assert (
        tmp_path / "approvals" / chosen.last["request_id"] / "item-approval"
    ).is_dir()
    assert chosen.last["gate_answers"][0]["decision"] == "approved"


def test_a_service_fallback_with_no_pin_never_starts(
    model,
    monkeypatch,
    run1,
    approver,
) -> None:
    install_reply(model, monkeypatch)
    monkeypatch.delenv(ROOT_ENV)
    result = policy(
        model,
        gate=ItemApprovalGate(wait_s=1),
        agent_factory=lambda: pytest.fail("agent started"),
    ).run(PROMPT, PARAMS, None)
    assert result.status == "FALLBACK_GATE_UNATTENDED" and model.rows == []


def test_a_service_fallback_refused_at_the_gate_saves_nothing(
    model,
    monkeypatch,
    run1,
    approver,
) -> None:
    install_reply(model, monkeypatch)
    gate = ItemApprovalGate(wait_s=0.3)
    gate._sleep = lambda s: __import__("time").sleep(s)
    result = policy(model, gate=gate).run(PROMPT, PARAMS, Agent())
    assert result.status == "GATE_REFUSED" and model.rows == []


def portal_signer(key_dir: Optional[Path] = None) -> ApproverSigner:
    import os

    from openadapt_flow.approval_mode.human_attestation import (
        PORTAL_KEY_ENV,
        load_signer,
    )
    from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
    from tests._approver_keys import PASSPHRASE, root_of

    directory = key_dir or Path(os.environ[KEY_DIR_ENV])
    return load_signer(  # type: ignore[return-value]
        Path(os.environ[PORTAL_KEY_ENV]),
        directory,
        method="portal_key",
        env={KEY_PASSPHRASE_ENV: PASSPHRASE},
        root=root_of(directory),
    )
