"""Copy-on-write step and episode rollback for a workspace directory."""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

_IGNORED_DIRS = frozenset({".git", "__pycache__"})


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n in _IGNORED_DIRS or n.endswith(".pyc")]


def _files(root: Path) -> set[str]:
    out: set[str] = set()
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            out.add((Path(dirpath) / name).relative_to(root).as_posix())
    return out


class TransactionalWorkspaceGuard:
    """Snapshot a workspace, then roll back single files or the whole tree."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self._tmp: Path | None = None
        self._episode_snapshot: Path | None = None
        self._step_snapshot: Path | None = None

    def _copy(self, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        if self.workspace_root.is_dir():
            shutil.copytree(self.workspace_root, dest, ignore=_ignore, symlinks=True)
        else:
            dest.mkdir(parents=True)

    def begin_transaction(self) -> None:
        self.cleanup()
        self._tmp = Path(tempfile.mkdtemp(prefix="oa-txn-"))
        self._episode_snapshot = self._tmp / "episode"
        self._step_snapshot = self._tmp / "step"
        self._copy(self._episode_snapshot)
        self._copy(self._step_snapshot)

    def snapshot_step(self) -> None:
        if self._step_snapshot is None:
            self.begin_transaction()
            return
        self._copy(self._step_snapshot)

    def rollback_paths(self, rel_paths: Sequence[str]) -> list[str]:
        restored: list[str] = []
        root = self.workspace_root.resolve()
        for rel in rel_paths:
            target = (root / rel).resolve()
            if target != root and root not in target.parents:
                continue
            for snap in (self._step_snapshot, self._episode_snapshot):
                if snap is None:
                    continue
                src = snap / rel
                if src.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    restored.append(rel)
                    break
        return restored

    def rollback_all(self) -> bool:
        snap = self._episode_snapshot
        if snap is None or not snap.is_dir():
            return False
        root = self.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        for rel in _files(root) - _files(snap):
            (root / rel).unlink(missing_ok=True)
        for rel in sorted(_files(snap)):
            src, dst = snap / rel, root / rel
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        # remove directories that did not exist in the snapshot
        for dirpath, dirnames, _ in os.walk(root, topdown=False):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
            d = Path(dirpath)
            if d != root and not (snap / d.relative_to(root)).exists():
                shutil.rmtree(d, ignore_errors=True)
        return True

    def has_workspace_changes(self) -> bool:
        snap = self._episode_snapshot
        if snap is None:
            return False
        cur, old = _files(self.workspace_root), _files(snap)
        if cur != old:
            return True
        return any(
            not filecmp.cmp(snap / r, self.workspace_root / r, shallow=False)
            for r in cur
        )

    def cleanup(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = self._episode_snapshot = self._step_snapshot = None
