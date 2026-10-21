"""A person's approval counts only when a registered approver key signed it.

Before this, ``--decided-by human`` with ``--unauthenticated``, or a named
``--auth-method``, was enough: any process that could run the CLI or write the
run directory could approve its own gated save, and the durable ``approve``
named whoever the OS user was. These tests play that agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.approval_mode.human_attestation import load_signer
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalAnswer,
    ItemApprovalRefused,
    ItemApprovalStore,
    answer_refusal,
    make_answer,
)
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.jit.authority import (
    KEY_PASSPHRASE_ENV,
    generate_encrypted_key,
    register_principal,
)
from tests._approver_keys import PASSPHRASE

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"


def _request(store: ItemApprovalStore):
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
        form_state=("dom", "sha256:" + "1" * 64),
    )


def _cli(*argv: str) -> tuple[int, dict]:
    import contextlib
    import io

    from openadapt_flow.__main__ import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def _answer_args(store, request, *extra: str) -> list[str]:
    return [
        "item-approval",
        "approve",
        str(store.run_dir),
        "--request-id",
        request.request_id,
        "--request-sha256",
        request.digest(),
        "--decided-by",
        "human",
        "--route",
        "desktop_run_it",
        *extra,
    ]


def test_an_agent_claiming_to_be_a_person_is_refused(tmp_path, approver):
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)

    code, out = _cli(*_answer_args(store, request, "--unauthenticated"))

    assert code == 2 and out["code"] == "human_answer_unsigned"
    assert store.read_answer(request.request_id) is None


def test_an_unsigned_answer_written_straight_to_the_run_is_refused(tmp_path, approver):
    """The run checks every answer itself; the CLI's checks aren't the control."""

    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    with pytest.raises(ItemApprovalRefused) as raised:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
            policy=store.context(),
        )
    assert raised.value.code == "human_answer_unsigned"
    forged = ItemApprovalAnswer(
        request_id=request.request_id,
        request_sha256=request.digest(),
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_kind="unauthenticated",
        auth_method="none",
        answered_at=request.created_at,
    )
    assert answer_refusal(request, forged, policy=store.context()) == (
        "human_answer_unsigned"
    )


def test_a_claimed_cloud_sign_in_is_refused(tmp_path, approver):
    key_path, _signer = approver
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)

    code, out = _cli(
        *_answer_args(
            store,
            request,
            "--approver-id",
            "cloud-user-0001",
            "--auth-method",
            "cloud_aal2",
        )
    )
    assert code == 2 and out["code"] == "human_answer_unsigned"

    code, out = _cli(
        *_answer_args(
            store,
            request,
            "--approver-id",
            "front-desk",
            "--auth-method",
            "cloud_aal2",
            "--approver-key",
            str(key_path),
        )
    )
    assert code == 2 and out["code"] == "auth_method_unverified"


def test_a_signed_answer_from_the_desktop_key_passes(tmp_path, approver):
    key_path, _signer = approver
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)

    code, out = _cli(
        *_answer_args(
            store, request, "--unauthenticated", "--approver-key", str(key_path)
        )
    )

    assert code == 0 and out["ok"] is True
    answer = store.read_answer(request.request_id)
    assert answer is not None and answer.attestation is not None
    assert answer.attestation.signer == "front-desk"
    assert answer.attestation.method == "desktop_key"
    assert answer_refusal(request, answer, policy=store.context()) is None


def test_a_changed_signed_answer_is_refused(tmp_path, approver):
    _key_path, signer = approver
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    answer = make_answer(
        request,
        decision="decline",
        decline_reason="wrong_values",
        decided_by="human",
        route="desktop_run_it",
        approver_id=None,
        auth_method="none",
        policy=store.context(),
        signer=signer,
    )
    flipped = answer.model_copy(update={"decision": "approve", "decline_reason": None})
    assert answer_refusal(request, flipped, policy=store.context()) == (
        "human_answer_signature_invalid"
    )


def test_the_portal_key_cant_answer_for_desktop(tmp_path, approver):
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    portal = load_signer(
        store.key_directory() / "decision-portal.approver.pem",
        store.key_directory(),
        method="portal_key",
    )
    with pytest.raises(ItemApprovalRefused) as raised:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
            policy=store.context(),
            signer=portal,
        )
    assert raised.value.code == "attestation_method_mismatch"


def test_an_agent_key_is_not_an_approver(tmp_path, approver, monkeypatch):
    key_dir = Path(approver[0]).parent
    agent_key = tmp_path / "agent.pem"
    env = {KEY_PASSPHRASE_ENV: PASSPHRASE}
    public = generate_encrypted_key(agent_key, env=env)
    register_principal(
        key_dir, role="agent", principal_id="agent:ops", public_key_hex=public
    )
    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)

    code, out = _cli(
        *_answer_args(
            store, request, "--unauthenticated", "--approver-key", str(agent_key)
        )
    )
    assert code == 2 and out["code"] == "human_approver_unregistered"


def test_an_approver_never_answers_a_run_it_started(tmp_path, approver):
    _key_path, signer = approver
    store = ItemApprovalStore(tmp_path / "run")
    store.write_initiator("front-desk")
    request = _request(store)
    with pytest.raises(ItemApprovalRefused) as raised:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
            policy=store.context(),
            signer=signer,
        )
    assert raised.value.code == "self_approval"


def test_no_registry_means_no_person_answer(tmp_path, approver):
    _key_path, signer = approver
    store = ItemApprovalStore(tmp_path / "run", key_dir=tmp_path / "empty-keys")
    request = _request(store)
    with pytest.raises(ItemApprovalRefused) as raised:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id=None,
            auth_method="none",
            policy=store.context(),
            signer=signer,
        )
    assert raised.value.code == "human_approver_unregistered"


# -- durable resume approval -------------------------------------------------


def test_durable_approve_needs_the_approver_key_not_the_os_user(
    tmp_path, approver, capsys
):
    from openadapt_flow.__main__ import main
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from tests.test_cli_new_commands import _paused_run

    run = _paused_run(tmp_path)
    assert main(["approve", str(run)]) == 1
    assert "--approver-key" in capsys.readouterr().out
    assert CheckpointStore(run).read_pending().status == "pending"

    key_path, _signer = approver
    assert (
        main(["approve", str(run), "--approver-key", str(key_path), "--approver", "x"])
        == 1
    )
    assert main(["approve", str(run), "--approver-key", str(key_path)]) == 0
    approval = CheckpointStore(run).read_approval()
    assert approval is not None and approval.approver == "front-desk"
    assert approval.attestation is not None
    assert approval.attestation.signer == "front-desk"


def test_resume_refuses_an_unsigned_stored_approval(tmp_path, approver, capsys):
    """An agent that writes approval.json itself gets nothing."""

    from openadapt_flow.__main__ import main
    from openadapt_flow.runtime.durable import bundle_version, issue_resume_approval
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from tests.test_cli_new_commands import _paused_run

    run = _paused_run(tmp_path)
    store = CheckpointStore(run)
    pending = store.read_pending()
    manifest = store.read_manifest()
    forged = issue_resume_approval(
        pending,
        approver="front-desk",
        resolution="approved",
        bundle_version=bundle_version(manifest.bundle_dir),
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run,
    )
    store.commit_approval_transition(
        expected_pending=pending, approval=forged, target_status="approved"
    )

    assert main(["resume", str(run), "--require-approval"]) == 3
    assert "human_answer_unsigned" in capsys.readouterr().out
