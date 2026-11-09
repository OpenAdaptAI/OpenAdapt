"""Batched timestamps for offline Seals: optional, one request per batch."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_flow.offline_seal import seal_run, seal_sha256
from openadapt_flow.seal_batch import (
    build_batch,
    merkle_root_and_proofs,
    proof_for,
    submit_all,
    submit_batch,
    verify_inclusion,
)

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


class FakeTsa:
    """Stands in for an RFC 3161 service; counts requests."""

    name = "fake-tsa"

    def __init__(self) -> None:
        self.digests: list[bytes] = []

    def timestamp(self, digest: bytes) -> bytes:
        self.digests.append(digest)
        return b"token:" + digest


class LocalOnlyTsa(FakeTsa):
    MAY_EGRESS = False
    name = "local-tsa"


class BrokenTsa(FakeTsa):
    def timestamp(self, digest: bytes) -> bytes:
        raise ConnectionError("unreachable")


def _seal_runs(tmp_path: Path, home: Path, n: int) -> list[dict]:
    seals = []
    for i in range(n):
        run = tmp_path / f"run-{i}"
        run.mkdir()
        (run / "report.json").write_text(
            json.dumps({"run_id": f"run-{i}", "execution_outcome": "VERIFIED"})
        )
        seals.append(seal_run(run, home=home, now=NOW).wire())
    return seals


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13])
def test_every_leaf_proves_inclusion(n):
    leaves = [f"{i:064x}" for i in range(n)]
    root, proofs = merkle_root_and_proofs(leaves)
    for leaf, proof in zip(leaves, proofs):
        assert verify_inclusion(leaf, proof, root)
    # A proof does not fit another leaf, and a flipped side fails.
    if n > 1:
        assert not verify_inclusion(leaves[1], proofs[0], root)
        bad = [
            dict(step, side="left" if step["side"] == "right" else "right")
            for step in proofs[0]
        ]
        assert not verify_inclusion(leaves[0], bad, root)


def test_build_batch_folds_queued_seals_and_signs_the_root(tmp_path):
    home = tmp_path / "home"
    seals = _seal_runs(tmp_path, home, 5)
    path = build_batch(home, now=NOW)
    assert path is not None
    batch = json.loads(path.read_text())
    assert batch["count"] == 5 and batch["status"] == "built"
    assert batch["root_seal"]["subject_sha256"] == batch["root_sha256"]
    assert not list((home / "batch" / "pending").glob("*.json"))
    for seal in seals:
        found = proof_for(seal, home)
        assert found and found["verified_inclusion"]
    assert build_batch(home, now=NOW) is None  # nothing left to batch


def test_one_timestamp_request_covers_the_whole_batch(tmp_path):
    home = tmp_path / "home"
    _seal_runs(tmp_path, home, 7)
    path = build_batch(home, now=NOW)
    tsa = FakeTsa()
    result = submit_batch(path, tsa, env={})
    assert result.status == "timestamped"
    batch = json.loads(path.read_text())
    assert len(tsa.digests) == 1
    assert tsa.digests[0] == bytes.fromhex(batch["root_sha256"])  # 32 bytes only
    assert base64.b64decode(batch["timestamps"][0]["token"]).startswith(b"token:")
    assert submit_batch(path, tsa, env={}).status == "already_timestamped"
    assert len(tsa.digests) == 1


def test_no_outbound_mode_holds_the_batch_and_never_calls_the_tsa(tmp_path):
    home = tmp_path / "home"
    _seal_runs(tmp_path, home, 3)
    build_batch(home, now=NOW)
    tsa = FakeTsa()
    results = submit_all(tsa, home, env={"OPENADAPT_NO_OUTBOUND": "1"})
    assert [r.status for r in results] == ["held_no_outbound"]
    assert tsa.digests == []
    # A TSA that declares itself local may still run.
    local = LocalOnlyTsa()
    results = submit_all(local, home, env={"OPENADAPT_NO_OUTBOUND": "1"})
    assert [r.status for r in results] == ["timestamped"] and len(local.digests) == 1


def test_tsa_failure_leaves_the_batch_unchanged(tmp_path):
    home = tmp_path / "home"
    _seal_runs(tmp_path, home, 2)
    path = build_batch(home, now=NOW)
    before = path.read_text()
    assert submit_batch(path, BrokenTsa(), env={}).status == "timestamp_failed"
    assert path.read_text() == before


def test_a_tampered_queue_entry_stops_the_batch(tmp_path):
    home = tmp_path / "home"
    _seal_runs(tmp_path, home, 2)
    entry = next((home / "batch" / "pending").glob("*.json"))
    seal = json.loads(entry.read_text())
    seal["run_id"] = "other"
    entry.write_text(json.dumps(seal))
    with pytest.raises(ValueError):
        build_batch(home, now=NOW)


def test_seal_digest_is_the_leaf(tmp_path):
    home = tmp_path / "home"
    seal = _seal_runs(tmp_path, home, 1)[0]
    assert (home / "batch" / "pending" / f"{seal_sha256(seal)}.json").exists()
