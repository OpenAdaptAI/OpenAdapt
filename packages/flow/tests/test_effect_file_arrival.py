"""File / SFTP arrival EffectVerifier (kit).

Local-directory arrival is proven against real temp directories; the SFTP
path is contract-proven against an in-memory FAKE transport duck-typed to
paramiko's SFTPClient -- NOT live-proven against a real SFTP server.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    FileArrivalVerifier,
    Verdict,
)

ARRIVED = {"arrived": "True"}


def _effect(**kwargs) -> Effect:
    defaults = dict(
        kind=EffectKind.RECORD_WRITTEN,
        match=ARRIVED,
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=False,
        timeout_s=0.05,
    )
    defaults.update(kwargs)
    return Effect(**defaults)


class TestLocalArrival:
    def test_confirmed_one_new_conforming_file(self, tmp_path: Path):
        v = FileArrivalVerifier(tmp_path, pattern="batch_*.csv")
        before = v.capture_pre_state()
        assert before.reachable
        (tmp_path / "batch_001.csv").write_text("HEADER\nrow\n")
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED

    def test_refuted_nothing_arrived(self, tmp_path: Path):
        v = FileArrivalVerifier(tmp_path, pattern="batch_*.csv")
        before = v.capture_pre_state()
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.should_halt

    def test_zero_byte_arrival_is_not_a_record(self, tmp_path: Path):
        v = FileArrivalVerifier(tmp_path, pattern="*.csv")
        before = v.capture_pre_state()
        (tmp_path / "empty.csv").write_text("")
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.REFUTED

    def test_duplicate_export_caught(self, tmp_path: Path):
        v = FileArrivalVerifier(tmp_path, pattern="*.csv")
        before = v.capture_pre_state()
        (tmp_path / "report.csv").write_text("data")
        (tmp_path / "report (1).csv").write_text("data")
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2

    def test_stale_mtime_outside_window_not_fresh(self, tmp_path: Path):
        path = tmp_path / "old.csv"
        path.write_text("data")
        stale = time.time() - 3600
        os.utime(path, (stale, stale))
        v = FileArrivalVerifier(tmp_path, pattern="*.csv", mtime_window_s=60)
        # Absolute count (no baseline dependency): the stale file must not
        # satisfy an arrival contract.
        before = v.capture_pre_state()
        verdict = v.verify(_effect(count_new_only=False), before)
        assert verdict.verdict is Verdict.REFUTED

    def test_content_probe(self, tmp_path: Path):
        v = FileArrivalVerifier(
            tmp_path, pattern="*.csv", content_probe=r"^BATCH_HEADER"
        )
        before = v.capture_pre_state()
        (tmp_path / "bad.csv").write_text("wrong contents")
        assert v.verify(_effect(), before).verdict is Verdict.REFUTED
        (tmp_path / "good.csv").write_text("BATCH_HEADER\nrow\n")
        # exactly one NEW conforming file (the non-conforming one is not a
        # matching record)
        assert v.verify(_effect(), before).verdict is Verdict.CONFIRMED

    def test_missing_root_indeterminate(self, tmp_path: Path):
        v = FileArrivalVerifier(tmp_path / "gone", pattern="*")
        before = v.capture_pre_state()
        assert not before.reachable
        verdict = v.verify(_effect(count_new_only=False), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.should_halt

    def test_match_by_name_for_fixed_filename_overwrites(self, tmp_path: Path):
        (tmp_path / "export.csv").write_text("old")
        v = FileArrivalVerifier(tmp_path, pattern="*.csv")
        before = v.capture_pre_state()
        (tmp_path / "export.csv").write_text("new contents this run")
        effect = _effect(
            match={"name": "export.csv", "arrived": "True"}, count_new_only=True
        )
        # The rewrite changed size, so it is a NEW arrival by identity.
        assert v.verify(effect, before).verdict is Verdict.CONFIRMED


# -- fake SFTP transport (paramiko SFTPClient duck type) ---------------------


class _FakeSftp:
    """In-memory listdir_attr/open fake mirroring paramiko's surface."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, float]] = {}
        self.fail = False

    def put(self, name: str, data: bytes, mtime: float | None = None) -> None:
        self.files[name] = (data, time.time() if mtime is None else mtime)

    def listdir_attr(self, path: str):
        if self.fail:
            raise OSError("connection lost")
        return [
            SimpleNamespace(filename=name, st_size=len(data), st_mtime=mtime)
            for name, (data, mtime) in sorted(self.files.items())
        ]

    def open(self, path: str, mode: str = "rb"):
        if self.fail:
            raise OSError("connection lost")
        name = path.rsplit("/", 1)[-1]
        data, _ = self.files[name]

        class _Handle:
            def read(self, n: int) -> bytes:
                return data[:n]

            def close(self) -> None:
                pass

        return _Handle()


class TestSftpArrival:
    def test_confirmed_new_remote_arrival(self):
        sftp = _FakeSftp()
        v = FileArrivalVerifier("/outbox", pattern="claim_*.csv", transport=sftp)
        before = v.capture_pre_state()
        assert before.reachable
        sftp.put("claim_0001.csv", b"HEADER\nrow\n")
        assert v.verify(_effect(), before).verdict is Verdict.CONFIRMED

    def test_remote_content_probe_reads_file(self):
        sftp = _FakeSftp()
        v = FileArrivalVerifier(
            "/outbox", pattern="*.csv", content_probe="HEADER", transport=sftp
        )
        before = v.capture_pre_state()
        sftp.put("x.csv", b"no header here"[:0] + b"junk")
        assert v.verify(_effect(), before).verdict is Verdict.REFUTED
        sftp.put("y.csv", b"HEADER\nrow")
        assert v.verify(_effect(), before).verdict is Verdict.CONFIRMED

    def test_transport_failure_indeterminate(self):
        sftp = _FakeSftp()
        v = FileArrivalVerifier("/outbox", pattern="*", transport=sftp)
        before = v.capture_pre_state()
        sftp.fail = True
        verdict = v.verify(_effect(count_new_only=False), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.should_halt

    def test_pattern_filters_remote_names(self):
        sftp = _FakeSftp()
        sftp.put("other.txt", b"data")
        v = FileArrivalVerifier("/outbox", pattern="claim_*.csv", transport=sftp)
        before = v.capture_pre_state()
        assert before.records == []
        sftp.put("claim_1.csv", b"data")
        assert v.verify(_effect(), before).verdict is Verdict.CONFIRMED
