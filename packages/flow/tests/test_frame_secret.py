"""The frame's host secret: created once, whole, whatever the race."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from openadapt_flow.runtime.effects import db_snapshot


def test_racing_first_uses_share_one_whole_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENADAPT_FRAME_SECRET", raising=False)
    path = tmp_path / "frame.key"
    monkeypatch.setenv("OPENADAPT_FRAME_SECRET_FILE", str(path))
    seen: list[bytes] = []
    threads = [
        threading.Thread(target=lambda: seen.append(db_snapshot._host_secret()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 1 and len(seen[0]) == 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["frame.key"]
