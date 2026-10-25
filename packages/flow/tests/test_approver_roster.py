"""The signed approver roster and the run's pinned root.

These tests play a process that can write the key directory or the run
directory and tries to make itself an approver.
"""

from __future__ import annotations

import argparse
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode import approver_roster as roster
from openadapt_flow.approval_mode.approver_roster import RosterError
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalRefused,
    ItemApprovalStore,
)
from openadapt_flow.jit.authority import (
    KEY_PASSPHRASE_ENV,
    AuthorityError,
    generate_encrypted_key,
    public_hex,
    register_principal,
)


def _key():
    key = Ed25519PrivateKey.generate()
    return key, public_hex(key)


@pytest.fixture
def started(tmp_path):
    key_dir = tmp_path / "keys"
    root_key, root_public = _key()
    roster.init_root(
        key_dir,
        principal="front-desk",
        public_key=root_public,
        sign=roster.file_key_signer(root_key),
    )
    return key_dir, root_key, root_public


def test_the_pinned_root_vouches_for_itself(started):
    key_dir, _key_, root = started

    assert roster.verified_approvers(key_dir, root) == {"front-desk": root}


def test_without_a_pin_nobody_is_an_approver(started, monkeypatch):
    key_dir, _key_, _root = started
    monkeypatch.delenv(roster.ROOT_ENV, raising=False)

    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, roster.pinned_root(None))
    assert raised.value.code == "approver_root_unpinned"


def test_a_roster_with_another_root_approves_nothing(started):
    key_dir, _key_, _root = started
    _other, other_public = _key()

    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, other_public)
    assert raised.value.code == "approver_root_mismatch"


def test_a_key_directory_without_a_roster_approves_nothing(tmp_path):
    _k, public = _key()
    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(tmp_path / "empty", public)
    assert raised.value.code == "approver_roster_missing"


def test_an_approver_added_by_an_approver_counts(started):
    key_dir, root_key, root = started
    _new, new_public = _key()

    roster.add_approver(
        key_dir,
        root,
        principal="night-nurse",
        public_key=new_public,
        signed_by="front-desk",
        sign=roster.file_key_signer(root_key),
    )

    assert roster.verified_approvers(key_dir, root)["night-nurse"] == new_public


def test_an_unsigned_edit_to_the_roster_adds_nobody(started):
    """A process that can write the file appends itself."""

    key_dir, _key_, root = started
    agent_key, agent_public = _key()
    path = key_dir / roster.ROSTER_FILE
    data = json.loads(path.read_text())
    # Signed by the agent's own key, claiming the root signed it.
    first = roster.load(key_dir).entries[0]
    forged = roster.RosterEntry(
        seq=2,
        prev=roster.entry_hash(first),
        action="add",
        principal="helper",
        public_key=agent_public,
        at="2026-10-25T00:00:00+00:00",
        signed_by="front-desk",
        signature="x",
    )
    forged = forged.model_copy(
        update={
            "signature": roster.file_key_signer(agent_key)(
                roster.roster_message(forged.signed_payload())
            )
        }
    )
    data["entries"].append(forged.model_dump(mode="json"))
    path.write_text(json.dumps(data))

    assert roster.verified_approvers(key_dir, root) == {"front-desk": root}


def test_an_edit_to_principals_json_adds_no_approver(started):
    key_dir, _key_, root = started
    _agent, agent_public = _key()
    (key_dir / "principals.json").write_text(
        json.dumps({"approvers": {"helper": agent_public}})
    )

    assert "helper" not in roster.verified_approvers(key_dir, root)


def test_an_entry_changed_after_signing_is_refused(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    roster.add_approver(
        key_dir,
        root,
        principal="a-nurse",
        public_key=a_public,
        signed_by="front-desk",
        sign=roster.file_key_signer(root_key),
    )
    path = key_dir / roster.ROSTER_FILE
    data = json.loads(path.read_text())
    data["entries"][1]["principal"] = "b-nurse"  # changed after signing
    path.write_text(json.dumps(data))

    # The changed entry stops the replay, and the roster no longer reaches
    # the head recorded beside it, so nothing is trusted.
    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root)
    assert raised.value.code == "approver_roster_rolled_back"


def test_only_an_approver_can_add_one(started):
    key_dir, _root_key, root = started
    outsider, _outsider_public = _key()
    _new, new_public = _key()

    with pytest.raises(RosterError) as raised:
        roster.add_approver(
            key_dir,
            root,
            principal="helper",
            public_key=new_public,
            signed_by="outsider",
            sign=roster.file_key_signer(outsider),
        )
    assert raised.value.code == "approver_signer_unregistered"
    # Naming a real approver but signing with another key fails too.
    with pytest.raises(RosterError) as raised:
        roster.add_approver(
            key_dir,
            root,
            principal="helper",
            public_key=new_public,
            signed_by="front-desk",
            sign=roster.file_key_signer(outsider),
        )
    assert raised.value.code == "approver_signature_invalid"
    assert set(roster.verified_approvers(key_dir, root)) == {"front-desk"}


def test_an_agent_name_is_never_an_approver(started):
    key_dir, root_key, root = started
    _a, a_public = _key()

    with pytest.raises(RosterError):
        roster.add_approver(
            key_dir,
            root,
            principal="agent:ops",
            public_key=a_public,
            signed_by="front-desk",
            sign=roster.file_key_signer(root_key),
        )
    assert set(roster.verified_approvers(key_dir, root)) == {"front-desk"}


def test_a_removed_approver_stops_counting_and_the_root_stays(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    sign = roster.file_key_signer(root_key)
    roster.add_approver(
        key_dir,
        root,
        principal="a-nurse",
        public_key=a_public,
        signed_by="front-desk",
        sign=sign,
    )
    roster.remove_approver(
        key_dir,
        root,
        principal="a-nurse",
        public_key=a_public,
        signed_by="front-desk",
        sign=sign,
    )
    assert set(roster.verified_approvers(key_dir, root)) == {"front-desk"}
    with pytest.raises(RosterError):
        roster.remove_approver(
            key_dir,
            root,
            principal="front-desk",
            public_key=root,
            signed_by="front-desk",
            sign=sign,
        )


def test_a_key_with_another_role_is_never_an_approver(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    register_principal(
        key_dir, role="agent", principal_id="ops-bot", public_key_hex=a_public
    )

    with pytest.raises(RosterError):
        roster.add_approver(
            key_dir,
            root,
            principal="a-nurse",
            public_key=a_public,
            signed_by="front-desk",
            sign=roster.file_key_signer(root_key),
        )


def test_jit_register_no_longer_registers_an_approver(tmp_path):
    _k, public = _key()
    with pytest.raises(AuthorityError, match="jit approver add"):
        register_principal(
            tmp_path, role="approver", principal_id="front-desk", public_key_hex=public
        )


def test_a_root_is_set_once(started):
    key_dir, _key_, _root = started
    other, other_public = _key()
    with pytest.raises(RosterError) as raised:
        roster.init_root(
            key_dir,
            principal="intruder",
            public_key=other_public,
            sign=roster.file_key_signer(other),
        )
    assert raised.value.code == "approver_roster_exists"


def test_two_pins_that_disagree_are_refused():
    _a, a = _key()
    _b, b = _key()
    with pytest.raises(RosterError) as raised:
        roster.pinned_root(a, config_root=b, env={})
    assert raised.value.code == "approver_root_conflict"


# -- the run pins its root ---------------------------------------------------------


def test_a_reader_with_another_root_or_key_dir_is_refused(
    started, tmp_path, monkeypatch
):
    key_dir, _key_, root = started
    monkeypatch.delenv(roster.ROOT_ENV, raising=False)
    run = ItemApprovalStore(tmp_path / "run", key_dir=key_dir, approver_root=root)
    run.write_approver_root()

    _other, other_public = _key()
    assert (
        ItemApprovalStore(tmp_path / "run", key_dir=key_dir, approver_root=other_public)
        .context()
        .pin_refusal
        == "approver_root_mismatch"
    )
    assert (
        ItemApprovalStore(
            tmp_path / "run", key_dir=tmp_path / "elsewhere", approver_root=root
        )
        .context()
        .pin_refusal
        == "approver_key_dir_mismatch"
    )
    # A reader naming nothing is refused: the run directory is never the
    # trust anchor.
    reader = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    assert reader.context().pin_refusal == "approver_root_unpinned"
    same = ItemApprovalStore(tmp_path / "run", key_dir=key_dir, approver_root=root)
    assert same.context().approver_root == root
    assert same.context().pin_refusal is None


def test_a_planted_pin_refuses_the_run(started, tmp_path):
    key_dir, _key_, root = started
    _other, other_public = _key()
    planted = tmp_path / "run" / "item-approval"
    planted.mkdir(parents=True)
    (planted / "approver-root.json").write_text(
        json.dumps(
            {
                "approver_root": other_public,
                "key_dir": str(key_dir.resolve()),
                "head": None,
            }
        )
    )

    with pytest.raises(ItemApprovalRefused) as raised:
        ItemApprovalStore(
            tmp_path / "run", key_dir=key_dir, approver_root=root
        ).write_approver_root()
    assert raised.value.code == "approver_root_changed"


def test_a_deployment_pinned_key_dir_refuses_another(tmp_path):
    from openadapt_flow.__main__ import _args_key_dir

    _k, public = _key()
    config = tmp_path / "deployment.yaml"
    config.write_text(
        f"approvers:\n  root_public_key: {public}\n  key_dir: {tmp_path / 'keys'}\n"
    )
    args = argparse.Namespace(config=str(config), key_dir=str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit, match="approver_key_dir_mismatch"):
        _args_key_dir(args)
    args.key_dir = None
    assert _args_key_dir(args) == (tmp_path / "keys").resolve()


# -- jit approver, with no terminal ---------------------------------------------------


def test_jit_approver_needs_a_signature_not_a_terminal(tmp_path, monkeypatch, capsys):
    from openadapt_flow.__main__ import main

    monkeypatch.setenv(KEY_PASSPHRASE_ENV, "pass-phrase-for-tests")
    key_dir = tmp_path / "keys"
    root_pem = tmp_path / "root.pem"
    root = generate_encrypted_key(root_pem)
    new_pem = tmp_path / "new.pem"
    new_public = generate_encrypted_key(new_pem)
    outsider_pem = tmp_path / "outsider.pem"
    generate_encrypted_key(outsider_pem)

    assert (
        main(
            [
                "jit",
                "approver",
                "init",
                "--key-dir",
                str(key_dir),
                "--principal",
                "front-desk",
                "--key",
                str(root_pem),
            ]
        )
        == 0
    )
    refused = main(
        [
            "jit",
            "approver",
            "add",
            "--key-dir",
            str(key_dir),
            "--approver-root",
            root,
            "--principal",
            "night-nurse",
            "--public-key",
            new_public,
            "--signer-key",
            str(outsider_pem),
        ]
    )
    assert refused == 2
    assert (
        main(
            [
                "jit",
                "approver",
                "add",
                "--key-dir",
                str(key_dir),
                "--approver-root",
                root,
                "--principal",
                "night-nurse",
                "--public-key",
                new_public,
                "--signer-key",
                str(root_pem),
            ]
        )
        == 0
    )
    assert roster.verified_approvers(key_dir, root) == {
        "front-desk": root,
        "night-nurse": new_public,
    }


# -- the security review's attacks ------------------------------------------------


def _add(key_dir, root, root_key, principal, public):
    return roster.add_approver(
        key_dir,
        root,
        principal=principal,
        public_key=public,
        signed_by="front-desk",
        sign=roster.file_key_signer(root_key),
    )


def _remove(key_dir, root, root_key, principal, public):
    return roster.remove_approver(
        key_dir,
        root,
        principal=principal,
        public_key=public,
        signed_by="front-desk",
        sign=roster.file_key_signer(root_key),
    )


def test_a_replayed_add_after_a_remove_doesnt_bring_the_approver_back(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    added = _add(key_dir, root, root_key, "a-nurse", a_public)
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    path = key_dir / roster.ROSTER_FILE
    data = json.loads(path.read_text())
    data["entries"].append(added.model_dump(mode="json"))  # same signature, replayed
    path.write_text(json.dumps(data))

    assert "a-nurse" not in roster.verified_approvers(key_dir, root)


def test_dropping_a_removal_from_the_middle_is_caught_by_the_recorded_head(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    _b, b_public = _key()
    _add(key_dir, root, root_key, "a-nurse", a_public)
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    _add(key_dir, root, root_key, "b-nurse", b_public)
    recorded = roster.head(key_dir, root)
    path = key_dir / roster.ROSTER_FILE
    data = json.loads(path.read_text())
    del data["entries"][2]  # the removal of a-nurse
    path.write_text(json.dumps(data))

    # The chain breaks after the removed entry, and the roster no longer
    # reaches the head recorded beside it (or the one a run recorded).
    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root)
    assert raised.value.code == "approver_roster_rolled_back"
    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root, at_least=recorded)
    assert raised.value.code == "approver_roster_rolled_back"


def test_an_older_roster_put_back_is_refused(started):
    key_dir, root_key, root = started
    _a, a_public = _key()
    _add(key_dir, root, root_key, "a-nurse", a_public)
    older = (key_dir / roster.ROSTER_FILE).read_text()
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    recorded = roster.head(key_dir, root)
    (key_dir / roster.ROSTER_FILE).write_text(older)  # a-nurse is back in this copy

    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root, at_least=recorded)
    assert raised.value.code == "approver_roster_rolled_back"


def test_an_agent_pinned_fresh_root_counts_for_nothing_on_a_governed_run(
    tmp_path, monkeypatch
):
    """The agent starts its own roster and pins it with the env var and flag."""

    agent_key, agent_public = _key()
    agent_keys = tmp_path / "agent-keys"
    roster.init_root(
        agent_keys,
        principal="front-desk",
        public_key=agent_public,
        sign=roster.file_key_signer(agent_key),
    )
    monkeypatch.setenv(roster.ROOT_ENV, agent_public)
    run = ItemApprovalStore(
        tmp_path / "run",
        key_dir=agent_keys,
        approver_root=agent_public,
        config_root=None,
        config_only=True,
    )
    run.write_approver_root()

    context = run.context()
    assert context.approver_root is None
    assert context.pin_refusal == "approver_root_unpinned"
    # A reader can't supply the pin the run lacked.
    reader = ItemApprovalStore(
        tmp_path / "run", key_dir=agent_keys, approver_root=agent_public
    )
    assert reader.context().pin_refusal == "approver_root_unpinned"


def test_a_governed_run_uses_only_the_config_pin(started, tmp_path, monkeypatch):
    key_dir, _root_key, root = started
    _agent, agent_public = _key()
    monkeypatch.setenv(roster.ROOT_ENV, agent_public)
    run = ItemApprovalStore(
        tmp_path / "run",
        key_dir=key_dir,
        approver_root=agent_public,
        config_root=root,
        config_only=True,
    )
    run.write_approver_root()

    assert run.context().approver_root == root
    assert run.context().pin_refusal is None


def test_a_reader_with_a_different_config_pin_is_refused(started, tmp_path):
    key_dir, _root_key, root = started
    run = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=root, config_only=True
    )
    run.write_approver_root()
    _other, other_public = _key()

    reader = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=other_public, config_only=True
    )
    assert reader.context().pin_refusal == "approver_root_mismatch"


def test_a_run_refuses_a_roster_rolled_back_after_it_started(started, tmp_path):
    key_dir, root_key, root = started
    _a, a_public = _key()
    _add(key_dir, root, root_key, "a-nurse", a_public)
    older = (key_dir / roster.ROSTER_FILE).read_text()
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    run = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=root, config_only=True
    )
    run.write_approver_root()
    (key_dir / roster.ROSTER_FILE).write_text(older)

    from openadapt_flow.approval_mode.human_attestation import (
        AttestationRefused,
        approvers,
    )

    context = run.context()
    with pytest.raises(AttestationRefused) as raised:
        approvers(key_dir, context.approver_root, at_least=context.approver_head)
    assert raised.value.code == "approver_roster_rolled_back"


def test_a_malformed_deployment_config_refuses_instead_of_dropping_its_pin(
    tmp_path, monkeypatch
):
    from openadapt_flow.__main__ import _approver_pin

    _k, public = _key()
    monkeypatch.setenv(roster.ROOT_ENV, public)
    config = tmp_path / "deployment.yaml"
    config.write_text("approvers: [this is: not valid")
    with pytest.raises(SystemExit, match="deployment_config_unreadable"):
        _approver_pin(
            argparse.Namespace(config=str(config), approver_root=None), config_only=True
        )


# -- second review: nothing agent-writable is a trust anchor ------------------------


def test_verification_never_reads_the_environment(started, monkeypatch):
    from openadapt_flow.approval_mode.human_attestation import (
        AttestationRefused,
        approvers,
    )

    key_dir, _key_, root = started
    monkeypatch.setenv(roster.ROOT_ENV, root)
    with pytest.raises(AttestationRefused) as raised:
        approvers(key_dir, None)
    assert raised.value.code == "approver_root_unpinned"


def test_a_governed_reader_refuses_when_the_run_record_is_deleted(started, tmp_path):
    key_dir, _key_, root = started
    run = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=root, config_only=True
    )
    run.write_approver_root()
    (tmp_path / "run" / "item-approval" / "approver-root.json").unlink()

    resume = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=root, config_only=True
    )
    assert resume.context().pin_refusal == "approver_root_record_missing"


def test_a_rewritten_run_record_without_a_config_pin_counts_for_nothing(
    started, tmp_path, monkeypatch
):
    key_dir, _key_, root = started
    agent_key, agent_public = _key()
    agent_keys = tmp_path / "agent-keys"
    roster.init_root(
        agent_keys,
        principal="front-desk",
        public_key=agent_public,
        sign=roster.file_key_signer(agent_key),
    )
    record = tmp_path / "run" / "item-approval"
    record.mkdir(parents=True)
    (record / "approver-root.json").write_text(
        json.dumps(
            {
                "approver_root": agent_public,
                "key_dir": str(agent_keys.resolve()),
                "head": None,
            }
        )
    )
    monkeypatch.setenv(roster.ROOT_ENV, agent_public)

    resume = ItemApprovalStore(tmp_path / "run", key_dir=agent_keys, config_only=True)
    assert resume.context().pin_refusal == "approver_root_unpinned"


def test_an_older_roster_put_back_between_runs_is_refused(started):
    """No run recorded anything: the head beside the roster catches it."""

    key_dir, root_key, root = started
    _a, a_public = _key()
    _add(key_dir, root, root_key, "a-nurse", a_public)
    older = (key_dir / roster.ROSTER_FILE).read_text()
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    (key_dir / roster.ROSTER_FILE).write_text(older)

    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root)
    assert raised.value.code == "approver_roster_rolled_back"


def test_a_head_from_the_config_refuses_an_older_roster_even_without_the_sidecar(
    started,
):
    key_dir, root_key, root = started
    _a, a_public = _key()
    _add(key_dir, root, root_key, "a-nurse", a_public)
    older = (key_dir / roster.ROSTER_FILE).read_text()
    _remove(key_dir, root, root_key, "a-nurse", a_public)
    config_head = roster.head(key_dir, root)  # what Desktop's Keychain holds
    (key_dir / roster.ROSTER_FILE).write_text(older)
    (key_dir / roster.HEAD_FILE).unlink()  # the sidecar is deleted too

    with pytest.raises(RosterError) as raised:
        roster.verified_approvers(key_dir, root, at_least=config_head)
    assert raised.value.code == "approver_roster_rolled_back"


def test_a_pinned_run_wont_start_on_a_roster_it_cant_verify(started, tmp_path):
    key_dir, _key_, root = started
    _other, other_public = _key()
    run = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, config_root=other_public, config_only=True
    )
    with pytest.raises(ItemApprovalRefused) as raised:
        run.write_approver_root()
    assert raised.value.code == "approver_root_mismatch"


def test_the_task_ledger_never_takes_its_root_from_the_environment(
    started, tmp_path, monkeypatch
):
    from openadapt_flow.agent.ledger import TaskLedger

    key_dir, _key_, root = started
    monkeypatch.setenv(roster.ROOT_ENV, root)
    ledger = TaskLedger(tmp_path / "ledger", key_dir=key_dir, config_only=True)
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R01"}])
    asked = ledger.ask_user(job["job_id"], "R01: north or south?", "R01", 0)

    store = ItemApprovalStore(
        ledger._question_dir(asked["question_id"]), key_dir=key_dir, **ledger._pin()
    )
    assert store.context().pin_refusal == "approver_root_unpinned"


def test_the_deployment_pin_is_read_once_per_command(tmp_path):
    from openadapt_flow.__main__ import _approver_pin

    _a, first = _key()
    _b, second = _key()
    config = tmp_path / "deployment.yaml"
    config.write_text(f"approvers:\n  root_public_key: {first}\n")
    args = argparse.Namespace(config=str(config), approver_root=None)

    assert _approver_pin(args, config_only=True)["config_root"] == first
    config.write_text(f"approvers:\n  root_public_key: {second}\n")  # swapped mid-run
    assert _approver_pin(args, config_only=True)["config_root"] == first


# -- third review --------------------------------------------------------------------


def test_the_guard_server_takes_its_root_only_from_the_config(tmp_path, monkeypatch):
    """The agent sets OPENADAPT_APPROVER_ROOT to its own roster; the guard ignores it."""

    from openadapt_flow.agent import cli as agent_cli
    from openadapt_flow.guard import GuardedSave

    agent_key, agent_public = _key()
    monkeypatch.setenv(roster.ROOT_ENV, agent_public)
    _real, real_public = _key()
    config = tmp_path / "deployment.yaml"
    config.write_text(f"approvers:\n  root_public_key: {real_public}\n")
    seen: dict = {}

    def capture(cls, verify, state, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(GuardedSave, "from_file", classmethod(capture))
    args = argparse.Namespace(
        authoring=None,
        bundles=None,
        tutorial=False,
        guard="verify.yaml",
        guard_tasks="tasks",
        guard_state=str(tmp_path / "state"),
        guard_approval="each_item",
        config=str(config),
    )
    with pytest.raises(SystemExit):
        agent_cli._serve_guard(args, serve=None)
    assert seen["config_only"] is True
    assert seen["approver_root"] == real_public

    # And a guard store built the way the guard builds it never reads the env.
    store = ItemApprovalStore(tmp_path / "guard", config_root=None, config_only=True)
    store.write_approver_root()
    assert store.context().pin_refusal == "approver_root_unpinned"


def test_a_governed_resume_with_a_deleted_record_refuses(tmp_path, monkeypatch, capsys):
    """Deleting approver-root.json and resuming with another root and key dir."""

    from openadapt_flow.__main__ import main
    from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
    from tests._approver_keys import PASSPHRASE, provision, root_of
    from tests.test_cli_new_commands import _paused_run

    keys = tmp_path / "keys"
    monkeypatch.setenv(KEY_DIR_ENV, str(keys))
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, PASSPHRASE)
    provision(keys)
    monkeypatch.setenv(roster.ROOT_ENV, root_of(keys))
    run = _paused_run(tmp_path, managed=True)
    record = run / "item-approval" / "approver-root.json"
    assert record.exists()
    record.unlink()

    agent_key, agent_public = _key()
    agent_keys = tmp_path / "agent-keys"
    roster.init_root(
        agent_keys,
        principal="front-desk",
        public_key=agent_public,
        sign=roster.file_key_signer(agent_key),
    )
    config = tmp_path / "deployment.yaml"
    config.write_text(f"approvers:\n  root_public_key: {root_of(keys)}\n")
    capsys.readouterr()
    code = main(
        [
            "resume",
            str(run),
            "--require-approval",
            "--config",
            str(config),
            "--approver-root",
            agent_public,
            "--key-dir",
            str(agent_keys),
        ]
    )
    assert code == 3


def test_the_config_is_read_once_and_checked_against_its_digest(tmp_path):
    import hashlib

    from openadapt_flow.deployment import (
        _PINNED_CONFIG_BYTES,
        load_deployment,
        pin_deployment_bytes,
    )

    _a, first = _key()
    _b, second = _key()
    config = tmp_path / "deployment.yaml"
    config.write_text(f"approvers:\n  root_public_key: {first}\n")
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    try:
        pin_deployment_bytes(config, digest)
        config.write_text(f"approvers:\n  root_public_key: {second}\n")  # swapped
        assert (
            load_deployment(config, allow_unresolved=True).approvers.root_public_key
            == first
        )
        with pytest.raises(ValueError, match="config-sha256"):
            pin_deployment_bytes(config, digest)  # the swapped file doesn't match
    finally:
        _PINNED_CONFIG_BYTES.clear()
