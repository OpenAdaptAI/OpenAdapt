"""Unit tests for the Parallels control layer (no live VM).

``prlctl`` is mocked by monkeypatching ``subprocess.run`` in the module, so
these run anywhere (CI included) and pin the parsing/So the sequencing logic.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from openadapt_flow.backends import parallels_vm as pv
from openadapt_flow.backends.parallels_vm import (
    ParallelsError,
    ParallelsVM,
    SnapshotInfo,
)

UUID = "{d4f9c29a-52e1-4793-9334-7e971c3d0ab3}"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _mock_run(monkeypatch, responses):
    """Patch subprocess.run to return canned CompletedProcess per call.

    ``responses`` is a list consumed in order; each item is a
    (predicate_or_None, CompletedProcess). Records calls for assertions.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        # args[0] is the prlctl binary; the rest is the subcommand.
        sub = args[1] if len(args) > 1 else ""
        for key, cp in responses.items():
            if key in sub or key in " ".join(str(a) for a in args):
                return cp
        return _completed("")

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    return calls


def test_status_parses_running(monkeypatch):
    # `prlctl list --all -o status,uuid` puts STATUS in the first column.
    out = f"STATUS   UUID\nrunning  {UUID}\n"
    _mock_run(monkeypatch, {"list": _completed(out)})
    assert ParallelsVM(UUID).status() == "running"


def test_snapshot_parses_id(monkeypatch):
    out = (
        "Creating the snapshot...\n"
        "The snapshot with id {516f223f-7e3a-48f4-90d0-f69f9aaa7644} "
        "has been successfully created.\n"
    )
    _mock_run(monkeypatch, {"snapshot": _completed(out)})
    sid = ParallelsVM(UUID).snapshot("ready")
    assert sid == "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"


def test_snapshot_raises_when_unparseable(monkeypatch):
    _mock_run(monkeypatch, {"snapshot": _completed("no id here")})
    with pytest.raises(ParallelsError):
        ParallelsVM(UUID).snapshot("ready")


def test_list_snapshots_marks_current(monkeypatch):
    out = (
        "PARENT_SNAPSHOT_ID  SNAPSHOT_ID\n"
        "                    {6e1057e6-8db0-41b1-b900-c308eb8ec17c}\n"
        "{6e1057e6-8db0-41b1-b900-c308eb8ec17c} *{516f223f-7e3a-48f4-90d0-f69f9aaa7644}\n"
    )
    _mock_run(monkeypatch, {"snapshot-list": _completed(out)})
    snaps = ParallelsVM(UUID).list_snapshots()
    assert len(snaps) == 2
    assert snaps[0].current is False
    assert snaps[1].current is True
    assert snaps[1].snapshot_id == "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"


def test_host_free_space_preflight_refuses_before_vm_command(monkeypatch):
    calls = _mock_run(monkeypatch, {})
    monkeypatch.setattr(
        pv.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(free=7 * 1024**3),
    )
    with pytest.raises(ParallelsError, match="7.0 GiB available, 16.0 GiB required"):
        ParallelsVM(UUID).require_host_free_space(storage_path="/vm-volume")
    assert calls == []


def test_host_free_space_preflight_returns_observed_bytes(monkeypatch):
    free = 49 * 1024**3
    monkeypatch.setattr(
        pv.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(free=free),
    )
    assert ParallelsVM(UUID).require_host_free_space(storage_path="/vm-volume") == free


def test_delete_owned_snapshot_is_exact_and_has_no_children_flag(monkeypatch):
    calls = _mock_run(monkeypatch, {"snapshot-delete": _completed("deleted")})
    snapshot_id = "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"
    ParallelsVM(UUID).delete_owned_snapshot(snapshot_id)
    assert calls == [
        [
            pv.DEFAULT_PRLCTL,
            "snapshot-delete",
            UUID,
            "-i",
            snapshot_id,
        ]
    ]


def test_restore_base_then_deletes_only_owned_snapshot(monkeypatch):
    vm = ParallelsVM(UUID)
    base = "{35dba943-a22d-473c-b1b0-44fa6326e626}"
    owned = "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"
    snapshots = iter(
        [
            [SnapshotInfo(base, False), SnapshotInfo(owned, True)],
            [SnapshotInfo(base, True), SnapshotInfo(owned, False)],
            [SnapshotInfo(base, True)],
        ]
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(vm, "list_snapshots", lambda: next(snapshots))
    monkeypatch.setattr(vm, "revert", lambda item: calls.append(("revert", item)))
    monkeypatch.setattr(
        vm,
        "delete_owned_snapshot",
        lambda item: calls.append(("delete", item)),
    )
    vm.restore_base_and_delete_owned_snapshot(
        base_snapshot_id=base,
        owned_snapshot_id=owned,
    )
    assert calls == [("revert", base), ("delete", owned)]


def test_restore_refusal_never_deletes_when_base_is_not_current(monkeypatch):
    vm = ParallelsVM(UUID)
    base = "{35dba943-a22d-473c-b1b0-44fa6326e626}"
    owned = "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"
    snapshots = iter(
        [
            [SnapshotInfo(base, False), SnapshotInfo(owned, True)],
            [SnapshotInfo(base, False), SnapshotInfo(owned, True)],
        ]
    )
    deleted: list[str] = []
    monkeypatch.setattr(vm, "list_snapshots", lambda: next(snapshots))
    monkeypatch.setattr(vm, "revert", lambda _item: None)
    monkeypatch.setattr(vm, "delete_owned_snapshot", deleted.append)
    with pytest.raises(ParallelsError, match="base did not become current"):
        vm.restore_base_and_delete_owned_snapshot(
            base_snapshot_id=base,
            owned_snapshot_id=owned,
        )
    assert deleted == []


def test_guest_ip_skips_apipa(monkeypatch):
    ipconfig = (
        "   Autoconfiguration IPv4 Address. . : 169.254.83.107\n"
        "   IPv4 Address. . . . . . . . . . . : 10.211.55.3\n"
    )
    _mock_run(monkeypatch, {"exec": _completed(ipconfig)})
    assert ParallelsVM(UUID).guest_ip() == "10.211.55.3"


def test_host_ip_matches_guest_subnet(monkeypatch):
    _mock_run(
        monkeypatch,
        {"exec": _completed("   IPv4 Address. . . . . . . . . . . : 10.211.55.3\n")},
    )
    vm = ParallelsVM(UUID)
    ifconfig = types.SimpleNamespace(
        stdout="\tinet 10.211.55.2 netmask 0xffffff00 broadcast 10.211.55.255\n"
        "\tinet 192.168.1.20 netmask 0xffffff00\n"
    )
    monkeypatch.setattr(
        pv.subprocess,
        "run",
        lambda *a, **k: (
            ifconfig
            if a[0] == ["ifconfig"]
            else _completed("   IPv4 Address. . . : 10.211.55.3\n")
        ),
    )
    assert vm.host_ip("10.211.55.3") == "10.211.55.2"


def test_run_raises_on_nonzero(monkeypatch):
    _mock_run(
        monkeypatch, {"start": _completed("boom", returncode=1, stderr="no such VM")}
    )
    with pytest.raises(ParallelsError):
        ParallelsVM(UUID).start()


def test_shim_url_uses_guest_ip(monkeypatch):
    _mock_run(monkeypatch, {"exec": _completed("   IPv4 Address. . . : 10.211.55.3\n")})
    assert ParallelsVM(UUID).shim_url() == "http://10.211.55.3:5000"


def _stub_agent_deps(monkeypatch, vm, exec_calls, *, alive=True):
    """Neutralize the network/file side effects of launch_agent for unit tests."""
    monkeypatch.setattr(pv.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(vm, "host_ip", lambda *a, **k: "10.211.55.2")
    monkeypatch.setattr(vm, "guest_ip", lambda *a, **k: "10.211.55.3")
    monkeypatch.setattr(vm, "push_file", lambda *a, **k: None)
    monkeypatch.setattr(vm, "exec_cmd", lambda *a, **k: _completed(""))
    monkeypatch.setattr(vm, "kill_shim", lambda: None)

    def fake_exec(args, **k):
        exec_calls.append(list(args))
        return _completed("")

    monkeypatch.setattr(vm, "exec", fake_exec)
    monkeypatch.setattr(vm, "_agent_alive", lambda url, **k: alive)


def test_launch_agent_autoprovisions_tls_and_returns_pinned_endpoint(monkeypatch):
    vm = ParallelsVM(UUID)
    exec_calls: list[list] = []
    pushed: list[str] = []
    _stub_agent_deps(monkeypatch, vm, exec_calls)
    # Record what gets provisioned into the guest (cert + key land here).
    monkeypatch.setattr(vm, "push_file", lambda local, guest, **k: pushed.append(guest))

    ep = vm.launch_agent(port=5000, token="tok-xyz")

    # Secure by default: HTTPS URL, a real pin fingerprint, fail-closed client.
    assert ep.url == "https://10.211.55.3:5000"
    assert ep.require_tls is True
    assert ep.token == "tok-xyz"
    assert ep.pin_fingerprint and len(ep.pin_fingerprint) == 64  # SHA-256 hex
    # The per-run cert + key were provisioned into the guest.
    assert f"{pv.GUEST_DIR}/agent-cert.pem" in pushed
    assert f"{pv.GUEST_DIR}/agent-key.pem" in pushed

    launch = next(
        a for a in exec_calls if any("win_agent_server.py" in str(x) for x in a)
    )
    assert "--host" in launch and "0.0.0.0" in launch
    assert "--token" in launch and "tok-xyz" in launch
    # The agent is told to serve HTTPS with the provisioned material.
    assert "--certfile" in launch and f"{pv.GUEST_DIR}/agent-cert.pem" in launch
    assert "--keyfile" in launch and f"{pv.GUEST_DIR}/agent-key.pem" in launch


def test_launch_agent_endpoint_builds_pinned_backend(monkeypatch):
    vm = ParallelsVM(UUID)
    exec_calls: list[list] = []
    _stub_agent_deps(monkeypatch, vm, exec_calls)

    ep = vm.launch_agent(port=5000, token="tok-xyz")
    backend = ep.backend()
    # End-to-end: the client is wired https + pinned + tokened with no manual step.
    assert type(backend).__name__ == "WindowsBackend"
    assert backend.server_url == "https://10.211.55.3:5000"
    assert backend._pin_fingerprint == ep.pin_fingerprint
    assert backend._auth_token == "tok-xyz"
    assert backend._require_tls is True and backend._tls is True


def test_launch_agent_tls_false_is_plaintext_dev_escape(monkeypatch):
    vm = ParallelsVM(UUID)
    exec_calls: list[list] = []
    pushed: list[str] = []
    _stub_agent_deps(monkeypatch, vm, exec_calls)
    monkeypatch.setattr(vm, "push_file", lambda local, guest, **k: pushed.append(guest))

    ep = vm.launch_agent(port=5000, token="tok-xyz", tls=False)

    assert ep.url == "http://10.211.55.3:5000"
    assert ep.require_tls is False
    assert ep.pin_fingerprint is None
    # No cert material minted or provisioned on the dev escape.
    assert not any("agent-cert.pem" in p or "agent-key.pem" in p for p in pushed)
    launch = next(
        a for a in exec_calls if any("win_agent_server.py" in str(x) for x in a)
    )
    assert "--certfile" not in launch and "--keyfile" not in launch


def test_launch_agent_omits_token_when_none(monkeypatch):
    vm = ParallelsVM(UUID)
    exec_calls: list[list] = []
    _stub_agent_deps(monkeypatch, vm, exec_calls)

    vm.launch_agent(port=5000)
    launch = next(
        a for a in exec_calls if any("win_agent_server.py" in str(x) for x in a)
    )
    assert "--token" not in launch


def test_launch_agent_raises_when_agent_never_comes_up(monkeypatch):
    vm = ParallelsVM(UUID)
    exec_calls: list[list] = []
    _stub_agent_deps(monkeypatch, vm, exec_calls, alive=False)

    with pytest.raises(ParallelsError, match="did not come up"):
        vm.launch_agent(port=5000, wait_s=0.05)
