"""The signed approver roster and the run's pinned root.

These tests play a process that can write the key directory or the run
directory and tries to make itself an approver.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    forged = roster.RosterEntry(
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


def test_nothing_after_a_bad_entry_counts(started):
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

    assert roster.verified_approvers(key_dir, root) == {"front-desk": root}


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
    # A reader naming nothing uses what the run recorded.
    reader = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    assert reader.context().approver_root == root
    assert reader.context().pin_refusal is None


def test_a_planted_pin_refuses_the_run(started, tmp_path):
    key_dir, _key_, root = started
    _other, other_public = _key()
    planted = ItemApprovalStore(
        tmp_path / "run", key_dir=key_dir, approver_root=other_public
    )
    planted.write_approver_root()

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
