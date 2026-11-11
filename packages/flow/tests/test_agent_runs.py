"""Tests for RunStore fingerprint key creation, concurrency and validation."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from openadapt_flow.agent.runs import RunStore


def test_fingerprint_key_creation_races_under_concurrency_produce_same_key(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "concurrent_runs"

    def make_store() -> RunStore:
        return RunStore(runs_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(make_store) for _ in range(20)]
        stores = [f.result() for f in futures]

    keys = {s._key for s in stores}
    assert len(keys) == 1
    key = keys.pop()
    assert len(key) == 32
    assert (runs_dir / "openadapt-agent" / "fingerprint.key").read_bytes() == key


def test_fingerprint_key_shorter_than_32_bytes_is_rejected(tmp_path: Path) -> None:
    runs_dir = tmp_path / "short_key_runs"
    agent_dir = runs_dir / "openadapt-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    key_path = agent_dir / "fingerprint.key"
    key_path.write_bytes(b"short-key")

    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        RunStore(runs_dir)


def test_fingerprint_key_empty_is_rejected(tmp_path: Path) -> None:
    runs_dir = tmp_path / "empty_key_runs"
    agent_dir = runs_dir / "openadapt-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    key_path = agent_dir / "fingerprint.key"
    key_path.write_bytes(b"")

    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        RunStore(runs_dir)


def test_fingerprint_key_symlink_is_rejected(tmp_path: Path) -> None:
    runs_dir = tmp_path / "symlink_runs"
    agent_dir = runs_dir / "openadapt-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target.key"
    target.write_bytes(b"x" * 32)
    key_path = agent_dir / "fingerprint.key"
    key_path.symlink_to(target)

    with pytest.raises(ValueError, match="must not be a symlink"):
        RunStore(runs_dir)
