from openadapt.transducer_transactions import TransactionalWorkspaceGuard


def _ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "run_judge.py").write_text("judge")
    (root / "test_a.py").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "x.txt").write_text("x")
    return root


def test_rollback_all_restores_everything(tmp_path):
    root = _ws(tmp_path)
    g = TransactionalWorkspaceGuard(root)
    g.begin_transaction()
    assert not g.has_workspace_changes()
    (root / "run_judge.py").write_text("hacked")
    (root / "test_a.py").unlink()
    (root / "new.txt").write_text("n")
    (root / "newdir").mkdir()
    (root / "newdir" / "f").write_text("f")
    assert g.has_workspace_changes()
    assert g.rollback_all()
    assert (root / "run_judge.py").read_text() == "judge"
    assert (root / "test_a.py").read_text() == "a"
    assert not (root / "new.txt").exists()
    assert not (root / "newdir").exists()
    assert not g.has_workspace_changes()
    g.cleanup()


def test_rollback_paths_prefers_step_snapshot(tmp_path):
    root = _ws(tmp_path)
    g = TransactionalWorkspaceGuard(root)
    g.begin_transaction()
    (root / "sub" / "x.txt").write_text("step1")
    g.snapshot_step()
    (root / "sub" / "x.txt").write_text("bad")
    (root / "test_a.py").unlink()
    restored = g.rollback_paths(["sub/x.txt", "test_a.py", "missing.py", "../evil"])
    assert sorted(restored) == ["sub/x.txt", "test_a.py"]
    assert (root / "sub" / "x.txt").read_text() == "step1"
    assert (root / "test_a.py").read_text() == "a"
    g.cleanup()


def test_ignores_pycache_and_missing_root(tmp_path):
    root = _ws(tmp_path)
    g = TransactionalWorkspaceGuard(root)
    g.begin_transaction()
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_text("c")
    assert not g.has_workspace_changes()
    g.cleanup()
    missing = TransactionalWorkspaceGuard(tmp_path / "nope")
    missing.begin_transaction()
    (tmp_path / "nope").mkdir()
    (tmp_path / "nope" / "f").write_text("f")
    assert missing.has_workspace_changes()
    assert missing.rollback_all()
    assert not (tmp_path / "nope" / "f").exists()
    missing.cleanup()
    assert not missing.rollback_all()
