from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openadapt.transducer_cow_container import (
    ContainerDiffTransaction,
    OsPostconditionProbe,
    run_probes,
    synthesize_probes,
)


def local_exec(cmd: str) -> tuple[int, str, str]:
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def env(tmp_path: Path):
    home = tmp_path / "home"
    ssh = tmp_path / "ssh"
    (home / "project").mkdir(parents=True)
    ssh.mkdir()
    (home / ".bashrc").write_text("export OPENAI_API_KEY=abc\n")
    (home / "project" / "app.py").write_text("print(1)\n")
    (home / "project" / "test_app.py").write_text("def test(): pass\n")
    (ssh / "sshd_config").write_text("PermitRootLogin no\n")
    tar = str(tmp_path / "base.tar.gz")
    tx = ContainerDiffTransaction(
        local_exec, watched_roots=[str(home), str(ssh)], baseline_tar=tar
    )
    return tx, home, ssh, Path(tar)


def test_begin_multi_root_snapshot(env):
    tx, home, ssh, tar = env
    snap = tx.begin()
    assert str(home / ".bashrc") in snap
    assert str(home / "project" / "app.py") in snap
    assert str(ssh / "sshd_config") in snap
    assert tar.exists()
    assert str(tar) not in snap


def test_diff_detects_changes(env):
    tx, home, ssh, _ = env
    tx.begin()
    (home / "project" / "new.txt").write_text("x")
    (home / "project" / "app.py").write_text("print(2)\n")
    (home / "project" / "test_app.py").unlink()
    rep = tx.inspect_step_diff(allowed_write_paths=[str(home / "project")])
    assert rep.added == [str(home / "project" / "new.txt")]
    assert rep.modified == [str(home / "project" / "app.py")]
    assert rep.deleted == [str(home / "project" / "test_app.py")]
    assert rep.protected_violations == [str(home / "project" / "test_app.py")]
    assert rep.unauthorized_paths == []


def test_unauthorized_and_protected(env):
    tx, home, ssh, _ = env
    tx.begin()
    (home / ".bashrc").write_text("")
    (ssh / "sshd_config").write_text("PermitRootLogin yes\n")
    rep = tx.inspect_step_diff(allowed_write_paths=[str(home / "project")])
    assert set(rep.protected_violations) == {
        str(home / ".bashrc"),
        str(ssh / "sshd_config"),
    }
    assert set(rep.unauthorized_paths) == set(rep.protected_violations)


def test_clean_diff_is_empty(env):
    tx, *_ = env
    tx.begin()
    assert tx.inspect_step_diff().changed == []


def test_diff_requires_begin(env):
    tx, *_ = env
    with pytest.raises(RuntimeError):
        tx.inspect_step_diff()


def test_rollback_paths_only_protected(env):
    tx, home, ssh, _ = env
    tx.begin()
    (home / ".bashrc").write_text("tampered")
    (home / "project" / "app.py").write_text("legit change")
    (home / "project" / "test_evil.py").write_text("assert True")
    rep = tx.inspect_step_diff()
    done = tx.rollback_paths(rep.protected_violations)
    assert set(done) == set(rep.protected_violations)
    assert (home / ".bashrc").read_text() == "export OPENAI_API_KEY=abc\n"
    assert not (home / "project" / "test_evil.py").exists()
    assert (home / "project" / "app.py").read_text() == "legit change"


def test_rollback_paths_ignores_outside_roots(env, tmp_path):
    tx, *_ = env
    tx.begin()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    assert tx.rollback_paths([str(outside)]) == []
    assert outside.exists()


def test_rollback_all(env):
    tx, home, ssh, _ = env
    tx.begin()
    (home / ".bashrc").write_text("x")
    (ssh / "sshd_config").unlink()
    (home / "project" / "junk.bin").write_text("junk")
    (home / "project" / "app.py").write_text("changed")
    assert tx.rollback_all() is True
    assert tx.inspect_step_diff().changed == []
    assert not (home / "project" / "junk.bin").exists()
    assert (ssh / "sshd_config").read_text() == "PermitRootLogin no\n"


def test_rollback_all_keeps_allowed_new_files(env):
    tx, home, *_ = env
    tx.begin()
    keep = home / "project" / "out.txt"
    keep.write_text("result")
    assert tx.rollback_all(allowed_write_paths=[str(keep)]) is True
    assert keep.exists()


def test_rollback_all_before_begin_refuses(env):
    tx, *_ = env
    assert tx.rollback_all() is False


def test_cleanup_removes_tar(env):
    tx, _, _, tar = env
    tx.begin()
    tx.cleanup()
    assert not tar.exists()


def test_docker_diff_adds_path(env):
    tx, home, *_ = env
    tx.begin()
    fake = home / "project" / "late.txt"
    fake.write_text("x")
    tx.container_id = "cid"
    tx._docker_diff = lambda cid: (0, f"A {fake}\nC /etc\nA /proc/x\n")
    rep = tx.inspect_step_diff()
    assert rep.added == [str(fake)]


def test_docker_diff_failure_falls_back(env):
    tx, home, *_ = env
    tx.begin()
    tx.container_id = "cid"
    tx._docker_diff = lambda cid: (1, "")
    assert tx.inspect_step_diff().changed == []


def test_scan_failure_raises():
    tx = ContainerDiffTransaction(lambda c: (1, "", "boom"))
    with pytest.raises(RuntimeError):
        tx.begin()


# -- probes -----------------------------------------------------------------


def test_env_preservation_probe(env):
    _, home, *_ = env
    task = "Add a cleanup step but keep OPENAI_API_KEY exported in ~/.bashrc."
    probes = synthesize_probes(task, baseline_files={str(home / ".bashrc"): "h"})
    ids = [p.probe_id for p in probes]
    assert "env_preserved_openai_api_key" in ids
    res = run_probes([p for p in probes if p.probe_id.startswith("env_")], local_exec)
    assert res.all_passed
    (home / ".bashrc").write_text("")
    res = run_probes([p for p in probes if p.probe_id.startswith("env_")], local_exec)
    assert not res.all_passed and res.failed_details


def test_profile_only_probe():
    probes = synthesize_probes("Edit .bashrc to add an alias")
    assert probes[0].probe_id == "profile_intact"


def test_permission_probe(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    probes = synthesize_probes(f"Revoke access for user bob to {d}.")
    perm = [p for p in probes if p.probe_id.startswith("perm_")]
    assert len(perm) == 1
    assert run_probes(perm, local_exec).all_passed
    d.rmdir()
    assert not run_probes(perm, local_exec).all_passed


def test_oracle_probe_always_present_and_detects_edit(env):
    tx, home, ssh, tar = env
    probes = synthesize_probes("Do something unrelated")
    assert [p.probe_id for p in probes] == ["oracle_files_untouched"]
    tx.begin()
    # probe uses default roots; rewrite it to the temp roots and temp tar.
    p = probes[0]
    cmd = p.shell_cmd.replace("/home/user", str(home)).replace("/etc/ssh", str(ssh))
    cmd = cmd.replace("/tmp/.oa_cow_baseline.tar.gz", str(tar))
    probe = OsPostconditionProbe(
        p.probe_id, p.description, cmd, expect_stdout_absent=p.expect_stdout_absent
    )
    assert run_probes([probe], local_exec).all_passed
    import time

    time.sleep(1.1)
    (home / "project" / "test_app.py").write_text("assert True")
    assert not run_probes([probe], local_exec).all_passed


def test_run_probes_empty_refuses():
    res = run_probes([], local_exec)
    assert not res.all_passed


def test_run_probes_expectations():
    ok = OsPostconditionProbe("a", "d", "echo hello", expect_stdout_contains="hell")
    bad = OsPostconditionProbe("b", "d", "echo hello", expect_stdout_contains="zzz")
    neg = OsPostconditionProbe("c", "d", "false", expect_exit_zero=False)
    res = run_probes([ok, bad, neg], local_exec)
    assert res.passed_ids == ["a", "c"]
    assert len(res.failed_details) == 1 and not res.all_passed


def test_run_probes_exec_error():
    def boom(cmd):
        raise OSError("gone")

    p = OsPostconditionProbe("a", "d", "true")
    res = run_probes([p], boom)
    assert not res.all_passed and "exec error" in res.failed_details[0]
