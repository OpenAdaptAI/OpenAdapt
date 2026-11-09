from __future__ import annotations

import hashlib
import importlib.util
import json
import signal
from pathlib import Path

from openadapt_flow.backends.remote_display import WindowInfo

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify_macos_textedit.py"
EVIDENCE_DIR = Path(__file__).parents[1] / "benchmark" / "macos_native"
COUNTED_REPORT = EVIDENCE_DIR / "textedit_counted_3plus1_b1b61a5_20260717.json"
ADJUDICATION = EVIDENCE_DIR / (
    "textedit_counted_3plus1_b1b61a5_20260717.adjudication.json"
)
COUNTED_REPORT_SHA256 = (
    "19c62cd4b1a8ba55001925cc129af80d22cec94cf6d5c7d9f56c2b5d71c79075"
)
SPEC = importlib.util.spec_from_file_location("macos_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def test_file_oracle_confirms_exact_bytes_and_refutes_drift(tmp_path: Path) -> None:
    target = tmp_path / "document.txt"
    target.write_bytes(b"expected\n")
    assert qualification.file_oracle(target, b"expected\n")["status"] == "confirmed"
    assert qualification.file_oracle(target, b"different\n")["status"] == "refuted"


def test_oracle_failure_taxonomy_distinguishes_refuted_and_unverifiable() -> None:
    assert qualification._oracle_failure_type({"status": "confirmed"}) is None
    assert (
        qualification._oracle_failure_type({"status": "refuted"})
        == "file_effect_refuted"
    )


def test_trial_metrics_separate_silent_incorrect_from_over_halt() -> None:
    results = [
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "confirmed"},
        },
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "refuted"},
        },
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "unverifiable"},
        },
        {
            "backend_sequence_reported_success": False,
            "oracle": {"status": "refuted"},
        },
    ]
    assert qualification._trial_metrics(results) == {
        "silent_incorrect_successes": 2,
        "over_halts": 1,
    }


def test_run_trial_records_backend_success_before_refuted_oracle(
    monkeypatch, tmp_path: Path
) -> None:
    class SuccessfulBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def screenshot(self):
            return b"frame"

        def press(self, _key):
            return None

        def type_text(self, _text):
            return None

    monkeypatch.setattr(qualification, "MacOSBackend", SuccessfulBackend)
    monkeypatch.setattr(qualification, "_open_isolated", lambda _path: None)
    monkeypatch.setattr(
        qualification,
        "_wait_for_matches",
        lambda *_args, **_kwargs: [
            WindowInfo(1, "TextEdit", "trial.txt", 9001, (0, 0, 100, 100))
        ],
    )
    monkeypatch.setattr(
        qualification,
        "_wait_oracle",
        lambda *_args, **_kwargs: {
            "status": "refuted",
            "expected_bytes": 10,
            "observed_bytes": 1,
        },
    )
    monkeypatch.setattr(qualification, "_close_target", lambda *_args, **_kwargs: None)

    result = qualification._run_trial(object(), tmp_path, "run", 1, [], [])
    assert result["status"] == "failed"
    assert result["backend_sequence_reported_success"] is True
    assert qualification._trial_metrics([result]) == {
        "silent_incorrect_successes": 1,
        "over_halts": 0,
    }
    assert (
        qualification._oracle_failure_type({"status": "unverifiable"})
        == "file_effect_unverifiable"
    )


def test_missing_permissions_block_before_launch(monkeypatch) -> None:
    class BlockedClient:
        def capture_trusted(self):
            return False

        def input_trusted(self):
            return False

    monkeypatch.setattr(qualification, "MacWindowClient", BlockedClient)

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("permission refusal must happen before app launch")

    monkeypatch.setattr(qualification, "_open_isolated", unexpected_launch)
    code, report = qualification.qualify(3)
    assert code == 2
    assert report["status"] == "blocked"
    assert report["trials_completed"] == 0
    assert report["missing_permissions"] == ["screen_recording", "accessibility"]


def test_dirty_candidate_blocks_before_launch(monkeypatch) -> None:
    class TrustedClient:
        def capture_trusted(self):
            return True

        def input_trusted(self):
            return True

    monkeypatch.setattr(qualification, "MacWindowClient", TrustedClient)
    monkeypatch.setattr(
        qualification,
        "_candidate_state",
        lambda trials: {
            "git_sha": "abc123",
            "git_dirty": True,
            "dirty_paths": [" M openadapt_flow/backends/macos_backend.py"],
            "flow_version": "1.9.1",
            "qualification_config": qualification._qualification_config(trials),
        },
    )

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("dirty candidate must block before app launch")

    monkeypatch.setattr(qualification, "_open_isolated", unexpected_launch)
    code, report = qualification.qualify(3)
    assert code == 2
    assert report["status"] == "blocked"
    assert report["evidence_classification"] == "diagnostic_only_not_acceptance"
    assert report["trials_completed"] == 0
    assert report["environment"]["candidate"]["git_sha"] == "abc123"


def test_harness_enforces_three_trial_minimum() -> None:
    assert qualification.MIN_TRIALS == 3


def test_window_wait_ignores_one_poll_title_race() -> None:
    transient = WindowInfo(1, "TextEdit", "oa-trial.txt", 10, (0, 0, 100, 100))
    stable = WindowInfo(2, "TextEdit", "oa-trial.txt", 11, (0, 0, 100, 100))

    class RacingClient:
        calls = 0

        def find_windows(self, _owner, _title):
            self.calls += 1
            return [transient] if self.calls == 1 else [stable]

    client = RacingClient()
    matches = qualification._wait_for_matches(
        client, "oa-trial", count=1, timeout_s=1.0
    )
    assert matches == [stable]
    assert client.calls >= 4


def test_graceful_close_refusal_is_warning_when_exact_pid_is_absent(
    monkeypatch,
) -> None:
    target = WindowInfo(8, "TextEdit", "target.txt", 9001, (0, 0, 100, 100))

    class Client:
        def find_windows(self, _owner, _title):
            return [target]

    class RefusingBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def press(self, _key):
            raise qualification.MacOSBackendError("stale exact window mapping")

    monkeypatch.setattr(qualification, "MacOSBackend", RefusingBackend)
    monkeypatch.setattr(
        qualification,
        "_terminate_isolated",
        lambda pid: {
            "pid": pid,
            "initially_present": False,
            "sigterm_sent": False,
            "sigkill_sent": False,
            "verified_absent": True,
            "exit_stage": "already_absent",
        },
    )
    warnings: list[str] = []
    receipts: list[dict] = []

    qualification._close_target(
        Client(), "target", 9001, warnings=warnings, receipts=receipts
    )

    assert warnings == ["window cleanup 'target': stale exact window mapping"]
    assert receipts[0]["verified_absent"] is True
    assert receipts[0]["graceful_close"] == {
        "status": "refused",
        "error": "stale exact window mapping",
    }


def test_exact_pid_fallback_waits_after_sigkill_before_confirming(
    monkeypatch,
) -> None:
    signals: list[int] = []
    waits = iter([False, True])
    monkeypatch.setattr(qualification, "_process_exists", lambda _pid: True)
    monkeypatch.setattr(
        qualification,
        "_wait_for_process_absence",
        lambda _pid, _timeout: next(waits),
    )
    monkeypatch.setattr(
        qualification.os,
        "kill",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )

    receipt = qualification._terminate_isolated(9001)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert receipt["sigterm_sent"] is True
    assert receipt["sigkill_sent"] is True
    assert receipt["verified_absent"] is True
    assert receipt["exit_stage"] == "after_sigkill"


def test_exact_pid_fallback_refuses_success_when_pid_survives_sigkill(
    monkeypatch,
) -> None:
    monkeypatch.setattr(qualification, "_process_exists", lambda _pid: True)
    monkeypatch.setattr(
        qualification, "_wait_for_process_absence", lambda _pid, _timeout: False
    )
    monkeypatch.setattr(qualification.os, "kill", lambda _pid, _signal: None)

    receipt = qualification._terminate_isolated(9001)

    assert receipt["sigkill_sent"] is True
    assert receipt["verified_absent"] is False
    assert receipt["exit_stage"] == "still_present_after_sigkill"


def test_process_lookup_race_does_not_assume_pid_absence(monkeypatch) -> None:
    monkeypatch.setattr(qualification, "_process_exists", lambda _pid: True)

    def lookup_race(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(qualification.os, "kill", lookup_race)

    receipt = qualification._terminate_isolated(9001)

    assert receipt["verified_absent"] is False
    assert receipt["exit_stage"] == "before_sigterm"
    assert receipt["error"] == "PID was present after SIGTERM lookup race"


def test_cleanup_acceptance_depends_only_on_residue_and_unrelated_pids() -> None:
    receipts = [{"pid": 9001, "verified_absent": True}]
    assert (
        qualification._cleanup_failures(
            receipts,
            root_exists=False,
            textedit_pids_before={1206},
            textedit_pids_after={1206},
        )
        == []
    )

    failures = qualification._cleanup_failures(
        [{"pid": 9001, "verified_absent": False}],
        root_exists=True,
        textedit_pids_before={1206},
        textedit_pids_after={1206, 9999},
    )
    assert failures == [
        "harness PID remained after cleanup: 9001",
        "temporary qualification root remained after cleanup",
        "unrelated TextEdit PID set changed: before=[1206] after=[1206, 9999]",
    ]


def test_qualify_passes_with_graceful_close_warnings_after_verified_pid_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    class TrustedClient:
        def capture_trusted(self):
            return True

        def input_trusted(self):
            return True

        def frontmost_pid(self):
            return None

    root = tmp_path / "qualification-root"
    root.mkdir()
    pid_snapshots = iter([{1206}, {1206}])
    monkeypatch.setattr(qualification, "MacWindowClient", TrustedClient)
    monkeypatch.setattr(
        qualification,
        "_candidate_state",
        lambda trials: {
            "git_sha": "candidate",
            "git_dirty": False,
            "dirty_paths": [],
            "flow_version": "1.9.1",
            "qualification_config": qualification._qualification_config(trials),
        },
    )
    monkeypatch.setattr(qualification.tempfile, "mkdtemp", lambda **_kwargs: str(root))
    monkeypatch.setattr(qualification, "_textedit_pids", lambda: next(pid_snapshots))

    def passed_trial(_client, _root, _run_id, trial, warnings, receipts):
        warnings.append(f"graceful close warning {trial}")
        receipts.append({"pid": 9000 + trial, "verified_absent": True})
        return {
            "trial": trial,
            "status": "passed",
            "backend_sequence_reported_success": True,
            "oracle": {"status": "confirmed"},
        }

    def passed_ambiguity(_client, _root, _run_id, warnings, receipts):
        warnings.append("graceful close warning ambiguity")
        receipts.extend(
            [
                {"pid": 9010, "verified_absent": True},
                {"pid": 9011, "verified_absent": True},
            ]
        )
        return {"status": "passed"}

    monkeypatch.setattr(qualification, "_run_trial", passed_trial)
    monkeypatch.setattr(qualification, "_run_ambiguity_trial", passed_ambiguity)

    code, report = qualification.qualify(3)

    assert code == 0
    assert report["status"] == "passed"
    assert report["cleanup_errors"] == []
    assert len(report["cleanup_warnings"]) == 4
    assert len(report["cleanup_receipts"]) == 5
    assert report["cleanup_audit"]["temporary_root_exists"] is False
    assert report["cleanup_audit"]["unrelated_textedit_pids_after"] == [1206]


def test_counted_report_is_byte_preserved_and_adjudication_is_hash_bound() -> None:
    report_bytes = COUNTED_REPORT.read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == COUNTED_REPORT_SHA256
    report = json.loads(report_bytes)
    adjudication = json.loads(ADJUDICATION.read_bytes())

    assert report["status"] == "failed"
    assert adjudication["original_evidence"] == {
        "path": (
            "benchmark/macos_native/textedit_counted_3plus1_b1b61a5_20260717.json"
        ),
        "sha256": COUNTED_REPORT_SHA256,
        "bytes": len(report_bytes),
        "status": "failed",
        "preserved_byte_for_byte": True,
        "status_is_not_rewritten_or_superseded": True,
    }
    assert (
        adjudication["candidate"]["git_sha"]
        == report["environment"]["candidate"]["git_sha"]
    )
    assert adjudication["candidate"]["git_sha"] == (
        "b1b61a5152a3662f3fe0b4b44446f2a139a1ad83"
    )
    assert [
        item["original_text"]
        for item in adjudication["original_graceful_close_warnings"]
    ] == report["cleanup_errors"]
    assert adjudication["counted_run"]["silent_incorrect_successes"] == 0
    assert adjudication["counted_run"]["over_halts"] == 0
    assert adjudication["counted_run"]["automatic_retry"] is False
    assert adjudication["post_run_cleanup_audit"]["all_harness_pids_absent"] is True
    assert (
        adjudication["post_run_cleanup_audit"]["temporary_root_audit"][
            "verified_absent"
        ]
        is True
    )
