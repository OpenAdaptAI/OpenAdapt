"""The administrator's approver pin, and how Desktop chooses its root.

Each attack is a same-user process (an agent) trying to make its own root the
one Desktop pins, so it can sign answers to its own saves.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from openadapt_desktop import approver_anchor
from openadapt_desktop.approver_anchor import Ace, AnchorError, dacl_admin_only

ROOT = "a" * 64
OTHER = "b" * 64
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and mode")

# The real reader, before the engine conftest stubs it out.
_read_anchor = approver_anchor.read_anchor


def _me() -> frozenset[int]:
    """Root plus this test's own account, standing in for the administrator."""
    return frozenset({0, os.getuid()})


@pytest.fixture
def anchor(tmp_path: Path) -> Path:
    folder = tmp_path.resolve() / "OpenAdapt"
    return folder / approver_anchor.ANCHOR_NAME


def _write(path: Path, root: str = ROOT) -> Path:
    return approver_anchor.write_anchor(root, path, trusted_uids=_me())


# -- reading the anchor ----------------------------------------------------------


@posix_only
def test_an_administrators_anchor_pins_the_root(anchor: Path) -> None:
    _write(anchor)
    assert _read_anchor(anchor, trusted_uids=_me()) == ROOT
    assert oct(anchor.stat().st_mode & 0o777) == "0o644"
    assert oct(anchor.parent.stat().st_mode & 0o777) == "0o755"


def test_no_anchor_is_no_pin(anchor: Path) -> None:
    assert _read_anchor(anchor, trusted_uids=_me()) is None


@posix_only
def test_a_file_this_user_owns_is_not_an_administrators_pin(anchor: Path) -> None:
    """Attack: the agent writes its own root into the anchor's place."""
    _write(anchor)
    with pytest.raises(AnchorError) as raised:
        _read_anchor(anchor)  # only root is trusted, as in real use
    assert raised.value.code == "approver_anchor_untrusted"


@posix_only
def test_a_symlinked_anchor_is_refused(anchor: Path, tmp_path: Path) -> None:
    """Attack: point the anchor at a file the agent controls."""
    _write(anchor)
    mine = tmp_path.resolve() / "mine.json"
    mine.write_text(json.dumps({"root_public_key": OTHER}))
    anchor.unlink()
    anchor.symlink_to(mine)
    with pytest.raises(AnchorError, match="without following links"):
        _read_anchor(anchor, trusted_uids=_me())


@posix_only
def test_a_symlinked_folder_is_refused(tmp_path: Path) -> None:
    """Attack: replace the anchor's folder with a link to the agent's own."""
    base = tmp_path.resolve()
    real = base / "real"
    _write(real / approver_anchor.ANCHOR_NAME)
    (base / "OpenAdapt").symlink_to(real)
    with pytest.raises(AnchorError, match="without following links"):
        _read_anchor(base / "OpenAdapt" / approver_anchor.ANCHOR_NAME, trusted_uids=_me())


@posix_only
@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_an_anchor_others_can_write_is_refused(anchor: Path, mode: int) -> None:
    _write(anchor)
    os.chmod(anchor, mode)
    with pytest.raises(AnchorError, match="Others can write"):
        _read_anchor(anchor, trusted_uids=_me())


@posix_only
def test_a_folder_others_can_write_is_refused(anchor: Path) -> None:
    """Attack: a writable folder lets the agent rename its own file into place."""
    _write(anchor)
    os.chmod(anchor.parent, 0o777)
    with pytest.raises(AnchorError, match="Others can write"):
        _read_anchor(anchor, trusted_uids=_me())


@posix_only
def test_a_hard_linked_anchor_is_refused(anchor: Path, tmp_path: Path) -> None:
    _write(anchor)
    os.link(anchor, tmp_path.resolve() / "elsewhere.json")
    with pytest.raises(AnchorError, match="hard links"):
        _read_anchor(anchor, trusted_uids=_me())


@posix_only
@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps(["list"]), json.dumps({"root_public_key": "zz" * 32}), "{}"],
)
def test_an_unreadable_anchor_is_refused(anchor: Path, content: str) -> None:
    _write(anchor)
    os.chmod(anchor, 0o644)
    anchor.write_text(content)
    with pytest.raises(AnchorError) as raised:
        _read_anchor(anchor, trusted_uids=_me())
    assert raised.value.code == "approver_anchor_unreadable"


@posix_only
def test_real_system_folders_pass_the_folder_check() -> None:
    os.close(approver_anchor._open_folder(Path("/usr/bin"), frozenset({0})))


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="runs as root")
def test_pinning_needs_an_administrator(anchor: Path) -> None:
    with pytest.raises(AnchorError) as raised:
        approver_anchor.write_anchor(ROOT, anchor)
    assert raised.value.code == "not_elevated"
    assert not anchor.exists()


def test_the_anchor_lives_where_only_an_administrator_writes() -> None:
    assert str(approver_anchor.anchor_path("darwin")) == (
        "/Library/Application Support/OpenAdapt/approver-root.json"
    )
    assert str(approver_anchor.anchor_path("linux")) == "/etc/openadapt/approver-root.json"


# -- macOS extended ACLs ---------------------------------------------------------

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")


def _chmod_acl(path: Path, entry: str) -> None:
    import subprocess

    subprocess.run(["/bin/chmod", "+a", entry, str(path)], check=True)


@mac_only
def test_an_acl_letting_everyone_write_the_anchor_is_refused(anchor: Path) -> None:
    """Attack: mode bits say 0644, but an extended ACL lets everyone write."""
    _write(anchor)
    _chmod_acl(anchor, "everyone allow write,append")
    with pytest.raises(AnchorError, match="access list"):
        _read_anchor(anchor, trusted_uids=_me())


@mac_only
@pytest.mark.parametrize(
    "entry",
    ["everyone allow add_file", "everyone allow delete_child", "everyone allow writesecurity"],
)
def test_an_acl_letting_everyone_change_the_folder_is_refused(anchor: Path, entry: str) -> None:
    """Attack: an ACL on the folder lets the agent add, replace or re-permission files."""
    _write(anchor)
    _chmod_acl(anchor.parent, entry)
    with pytest.raises(AnchorError, match="access list"):
        _read_anchor(anchor, trusted_uids=_me())


@mac_only
def test_pinning_strips_an_inherited_acl(tmp_path: Path) -> None:
    """A folder above can hand new folders and files an ACL that grants
    writes (only_inherit: it doesn't apply to the folder itself). Pinning
    strips it, so the anchor reads back."""
    import subprocess

    parent = tmp_path.resolve() / "Support"
    parent.mkdir()
    _chmod_acl(
        parent,
        "everyone allow write,add_file,delete_child,file_inherit,directory_inherit,only_inherit",
    )
    target = parent / "OpenAdapt" / approver_anchor.ANCHOR_NAME
    _write(target)
    assert _read_anchor(target, trusted_uids=_me()) == ROOT
    listed = subprocess.run(["/bin/ls", "-led", str(target.parent)], capture_output=True, text=True)
    assert "allow" not in listed.stdout


def test_mac_acl_text_is_read_entry_by_entry() -> None:
    everyone = "group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12"
    text = (
        "!#acl 1\n"
        f"{everyone}:allow:read,list\n"
        f"{everyone}:deny:write,delete\n"
        f"{everyone}:allow,file_inherit,directory_inherit,only_inherit:write,delete_child\n"
    )
    assert approver_anchor.mac_acl_write_grants(text) == []
    inherited = f"{everyone}:allow,inherited,file_inherit,directory_inherit:write,delete_child"
    assert approver_anchor.mac_acl_write_grants(text + inherited + "\n") == [inherited]


# -- Windows access control ------------------------------------------------------

ADMINS, SYSTEM, USERS, ME = "S-1-5-32-544", "S-1-5-18", "S-1-5-32-545", "S-1-5-21-1-2-3-1001"
FULL, READ = 0x001F01FF, 0x001200A9


def test_only_administrators_writing_is_admin_only() -> None:
    aces = [Ace(0, 0, FULL, ADMINS), Ace(0, 0, FULL, SYSTEM), Ace(0, 0, READ, USERS)]
    assert dacl_admin_only(ADMINS, aces) is None


@pytest.mark.parametrize(
    ("owner", "aces", "reason"),
    [
        (ME, [Ace(0, 0, FULL, ADMINS)], "owned by"),
        (ADMINS, None, "no access control list"),
        (ADMINS, [Ace(0, 0, FULL, ADMINS), Ace(0, 0, 0x0002, USERS)], USERS),
        (ADMINS, [Ace(0, 0, 0x00040000, ME)], ME),  # WRITE_DAC
        (ADMINS, [Ace(0, 0, 0x10000000, ME)], ME),  # GENERIC_ALL
    ],
)
def test_anyone_else_able_to_change_it_is_refused(owner, aces, reason) -> None:
    assert reason in (dacl_admin_only(owner, aces) or "")


@pytest.mark.parametrize("ace_type", [5, 9, 11])  # object, callback, callback object
def test_every_kind_of_allow_entry_is_checked(ace_type: int) -> None:
    """Attack: grant a user write through an allow entry of another type."""
    aces = [Ace(0, 0, FULL, ADMINS), Ace(ace_type, 0, 0x0002, ME)]
    assert ME in (dacl_admin_only(ADMINS, aces) or "")


@pytest.mark.parametrize("ace_type", [4, 17, 99])
def test_an_entry_of_an_unknown_type_refuses(ace_type: int) -> None:
    aces = [Ace(0, 0, FULL, ADMINS), Ace(ace_type, 0, READ, ME)]
    assert "doesn't know" in (dacl_admin_only(ADMINS, aces) or "")


def test_a_folder_above_may_let_users_add_but_not_replace() -> None:
    """ProgramData lets users create folders; only replacing entries counts above."""
    replace = approver_anchor._REPLACE_RIGHTS
    adds = [Ace(0, 0, FULL, SYSTEM), Ace(0, 0, 0x0004 | 0x0002, USERS)]
    assert dacl_admin_only(SYSTEM, adds, rights=replace) is None
    assert dacl_admin_only(SYSTEM, adds) is not None  # but never on the anchor's own folder
    deletes = adds + [Ace(0, 0, 0x0040, USERS)]  # FILE_DELETE_CHILD
    assert USERS in (dacl_admin_only(SYSTEM, deletes, rights=replace) or "")


def test_the_anchor_folder_is_created_admin_only() -> None:
    sddl = approver_anchor.ANCHOR_FOLDER_SDDL
    assert sddl.startswith("O:BAD:P")  # owned by Administrators, protected DACL
    assert "(A;OICI;0x1200a9;;;BU)" in sddl  # Users read and execute only


def test_inherit_only_and_deny_entries_grant_nothing_here() -> None:
    aces = [Ace(0, 0x08, FULL, USERS), Ace(1, 0, FULL, USERS), Ace(0, 0, FULL, ADMINS)]
    assert dacl_admin_only(SYSTEM, aces) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_real_windows_acls(tmp_path: Path) -> None:  # pragma: no cover - Windows CI
    windir = approver_anchor._known_windows_dir()
    assert windir is not None
    approver_anchor._check_windows(windir / "System32" / "drivers" / "etc" / "hosts")
    mine = tmp_path / approver_anchor.ANCHOR_NAME
    mine.write_text(json.dumps({"root_public_key": OTHER}))
    with pytest.raises(AnchorError) as raised:
        approver_anchor._check_windows(mine)
    assert raised.value.code == "approver_anchor_untrusted"


# -- which root Desktop pins -----------------------------------------------------


@pytest.fixture
def roster(monkeypatch, fake_keyring):
    from openadapt_desktop import approver_keychain

    state = {"anchor": None}

    def fake_read_anchor(*_a, **_k):
        if isinstance(state["anchor"], Exception):
            raise state["anchor"]
        return state["anchor"]

    monkeypatch.setattr(approver_anchor, "read_anchor", fake_read_anchor)
    return approver_keychain.FlowRoster(), state, fake_keyring


def _keychain_root(fake_keyring, value: str) -> None:
    from openadapt_desktop.approver_keychain import ROOT_ACCOUNT
    from openadapt_desktop.auth.store import SERVICE_NAME

    fake_keyring.set_password(SERVICE_NAME, ROOT_ACCOUNT, value)


def test_the_administrators_anchor_pins_without_a_keychain_root(roster) -> None:
    flow_roster, state, _keyring = roster
    state["anchor"] = ROOT
    assert flow_roster.root() == ROOT


def test_a_keychain_root_that_disagrees_with_the_anchor_is_refused(roster) -> None:
    """Attack: on a Mac the agent rewrites the Keychain pin (for example by
    running code inside the ad-hoc signed engine); the anchor still decides."""
    from openadapt_desktop.approver_keychain import ApproverKeychainError, pinned_root

    flow_roster, state, keyring = roster
    state["anchor"] = ROOT
    _keychain_root(keyring, OTHER)
    with pytest.raises(ApproverKeychainError) as raised:
        flow_roster.root()
    assert raised.value.code == "approver_root_conflict"
    with pytest.raises(ApproverKeychainError):
        pinned_root()
    _keychain_root(keyring, ROOT)
    assert flow_roster.root() == ROOT


def test_an_untrusted_anchor_counts_no_answer(roster) -> None:
    from openadapt_desktop.approver_keychain import ApproverKeychainError

    flow_roster, state, keyring = roster
    _keychain_root(keyring, ROOT)
    state["anchor"] = AnchorError("approver_anchor_untrusted", "planted")
    with pytest.raises(ApproverKeychainError) as raised:
        flow_roster.root()
    assert raised.value.code == "approver_anchor_untrusted"


def test_off_a_mac_the_credential_store_never_pins(roster, monkeypatch) -> None:
    """Attack: on Windows or Linux any process of this user writes the
    credential store, so a root planted there must count for nothing."""
    from openadapt_desktop.approver_keychain import FlowRoster

    flow_roster, state, keyring = roster
    monkeypatch.setattr(FlowRoster, "keychain_pins", False)
    _keychain_root(keyring, OTHER)
    assert flow_roster.root() is None
    state["anchor"] = ROOT
    assert flow_roster.root() == ROOT


def test_with_an_anchor_this_mac_doesnt_start_another_root(roster) -> None:
    from openadapt_desktop.approver_keychain import ApproverKeychainError

    flow_roster, state, _keyring = roster
    state["anchor"] = ROOT
    with pytest.raises(ApproverKeychainError) as raised:
        flow_roster.init_root("desktop-approver", OTHER, lambda _m: "ed25519:00")
    assert raised.value.code == "approver_root_conflict"


def test_a_run_config_carries_the_anchors_root(roster, tmp_path) -> None:
    import yaml

    from openadapt_desktop.approver_keychain import pinned_root
    from openadapt_desktop.private_flow_config import prepare_flow_config

    _flow_roster, state, _keyring = roster
    state["anchor"] = ROOT
    source = tmp_path / "deployment.json"
    source.write_text(json.dumps({"approvers": {"root_public_key": OTHER}}))
    with pytest.raises(Exception, match="another approver root"):
        prepare_flow_config(source, None, approver_root=pinned_root())
    source.write_text(json.dumps({"name": "clinic"}))
    payload = yaml.safe_load(prepare_flow_config(source, None, approver_root=pinned_root()).payload)
    assert payload["approvers"] == {"root_public_key": ROOT}


# -- the command -----------------------------------------------------------------


def test_the_pin_command_shows_and_refuses_without_elevation(monkeypatch, tmp_path, capsys) -> None:
    from openadapt_desktop import cli

    target = tmp_path.resolve() / "OpenAdapt" / approver_anchor.ANCHOR_NAME
    monkeypatch.setattr(approver_anchor, "anchor_path", lambda platform=None: target)
    monkeypatch.setattr(approver_anchor, "read_anchor", _read_anchor)
    with pytest.raises(SystemExit) as shown:
        cli.main(["approver-pin", "--show"])
    assert shown.value.code == 0
    assert "no approver root pinned" in capsys.readouterr().out
    if sys.platform != "win32" and os.geteuid() != 0:
        with pytest.raises(SystemExit) as refused:
            cli.main(["approver-pin", ROOT])
        assert refused.value.code == 1
        assert "not_elevated" in capsys.readouterr().out
        assert not target.exists()
