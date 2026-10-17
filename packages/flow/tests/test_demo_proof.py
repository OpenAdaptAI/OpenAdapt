"""``openadapt-flow demo``: the proof page and its short terminal summary.

Browser-free. The two tutorial runs are faked with retained artifacts on disk
(``report.json``, the final frames, and the halted run's pending decision), so
these tests prove what the page and the summary say about each kind of ending:

* every plain result comes from ``transaction_outcome``; a run the engine left
  at ``RECONCILIATION_REQUIRED`` never reads "Nothing was written";
* "identical, pixel for pixel" appears only when the frame digests match;
* the person's choices come from ``pending_escalation.json``;
* the page is self-contained (inline CSS and images, no scripts);
* the terminal prints at most eight lines and the CLI keeps its exit codes.
"""

from __future__ import annotations

import html
import re
import struct
import zlib
from pathlib import Path
from typing import Optional

import pytest

from openadapt_flow import demo_proof
from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.ir import HaltObservation, RunReport, StepResult
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
)
from openadapt_flow.tutorial import BreakItResult, TutorialError, TutorialResult

ENGINE_OPTIONS = [
    "Inspect the system of record and correct it (the automatic compensation "
    "could not safely undo the fault)",
    "Approve and RESUME from the last verified checkpoint (re-runs only this "
    "step onward; already-confirmed steps are not repeated)",
    "Abort the run and discard the pending escalation",
]


def _png(width: int, height: int, shade: int = 240) -> bytes:
    """A minimal valid grayscale PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes([shade]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _write_run(
    run_dir: Path,
    *,
    outcome: str,
    transaction: Optional[str],
    frame: bytes,
    halted: bool,
) -> None:
    (run_dir / "steps").mkdir(parents=True)
    (run_dir / "steps" / "step_005_after.png").write_bytes(frame)
    (run_dir / "REPORT.md").write_text("# report\n", encoding="utf-8")
    RunReport(
        workflow_name="local-quickstart",
        started_at="2026-10-09T12:00:00+00:00",
        execution_outcome=outcome,
        transaction_outcome=transaction,
        execution_profile="standard",
        results=[
            StepResult(
                step_id="step_005",
                intent="click 'Save Encounter'",
                ok=not halted,
                postconditions_ok=True,
                effect_verified=False if halted else True,
                after_png="steps/step_005_after.png",
                elapsed_ms=50.0,
            )
        ],
        success=not halted,
        halt=(
            HaltObservation(
                state_id="step_005",
                intent="click 'Save Encounter'",
                reason="record_written refuted <against> the system of record",
            )
            if halted
            else None
        ),
        model_calls=0,
        total_ms=4200.0,
    ).save(run_dir)


def _tutorial_result(
    root: Path,
    *,
    broken_transaction: Optional[str] = "RECONCILIATION_REQUIRED",
    broken_records: int = 0,
    identical_frames: bool = True,
    pending: bool = True,
) -> TutorialResult:
    frame = _png(64, 40)
    _write_run(
        root / "run",
        outcome="VERIFIED",
        transaction="VERIFIED",
        frame=frame,
        halted=False,
    )
    _write_run(
        root / "run-broken",
        outcome="HALTED",
        transaction=broken_transaction,
        frame=frame if identical_frames else _png(64, 40, shade=10),
        halted=True,
    )
    if pending:
        CheckpointStore(root / "run-broken").write_pending(
            PendingEscalation(
                workflow_name="local-quickstart",
                step_index=5,
                step_id="step_005",
                intent="click 'Save Encounter'",
                category="effect_escalated",
                proposed_options=ENGINE_OPTIONS,
            )
        )
    broken = BreakItResult(
        run_dir=root / "run-broken",
        report_path=root / "run-broken" / "REPORT.md",
        fault="optimistic",
        execution_outcome="HALTED",
        transaction_outcome=broken_transaction,
        transaction_billable=False,
        screen_claimed_success=True,
        screen_claim_text="Encountersaved-",
        effects_required=2,
        effects_refuted=1,
        halt_reason="record_written refuted <against> the system of record",
        system_of_record_records=broken_records,
    )
    return TutorialResult(
        recording_dir=root / "recording",
        bundle_dir=root / "bundle",
        run_dir=root / "run",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        execution_profile="standard",
        transaction_billable=True,
        model_calls=0,
        effects_required=2,
        effects_confirmed=2,
        effect_tier=1,
        bundle_digest="d" * 64,
        system_of_record_records=1,
        break_it=broken,
    )


def _visible_text(page: str) -> str:
    """The page text outside the folded technical details."""

    before_details = page.split("<details>", 1)[0]
    return html.unescape(re.sub(r"<[^>]+>", " ", before_details))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class TestProofPage:
    def test_shows_the_difference_from_the_runs_own_evidence(
        self, tmp_path: Path
    ) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        assert evidence.showed_the_difference
        assert evidence.screens_identical
        assert evidence.save_step_name == "Save Encounter"

        page = demo_proof.render_demo_page(evidence)
        visible = _visible_text(page)
        assert "The screen said “saved” both times." in visible
        assert "Done and checked" in visible
        assert "Stopped: the screen said saved, the record says no" in visible
        assert "Check the record" in visible
        assert "identical, pixel for pixel" in visible
        # Both final frames are embedded, and each links to the full frame.
        assert page.count("data:image/png;base64,") == 2
        assert 'href="run/steps/step_005_after.png"' in page
        assert 'href="run-broken/steps/step_005_after.png"' in page

    def test_person_choices_come_from_the_pending_decision(
        self, tmp_path: Path
    ) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        assert [c.engine_text for c in evidence.choices] == ENGINE_OPTIONS
        page = demo_proof.render_demo_page(evidence)
        visible = _visible_text(page)
        assert "Check the record in the app and correct it by hand." in visible
        assert "After checking the record, let the run continue" in visible
        assert "Cancel the run." in visible
        # The engine's exact words stay available one level down.
        details = page.split("<details>", 1)[1]
        assert "Approve and RESUME from the last verified checkpoint" in details

    def test_reconciliation_never_claims_nothing_was_written(
        self, tmp_path: Path
    ) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        assert evidence.broken.plain.key == "check_the_record"
        visible = _visible_text(demo_proof.render_demo_page(evidence)).lower()
        assert "nothing was written" not in visible
        assert "safe to" not in visible
        assert "didn't reach the record" not in visible
        # The headline reports the record check's count, never that the
        # second run saved nothing.
        assert "the record check found only one note." in visible
        assert "only one note was saved" not in visible
        assert "a person checks the record before anything is retried" in visible

    def test_halted_before_effect_may_say_nothing_was_written(
        self, tmp_path: Path
    ) -> None:
        result = _tutorial_result(tmp_path, broken_transaction="HALTED_BEFORE_EFFECT")
        evidence = demo_proof.collect_evidence(result, tmp_path)
        assert evidence.broken.plain.key == "stopped_before_saving"
        visible = _visible_text(demo_proof.render_demo_page(evidence))
        assert "Stopped before saving" in visible
        assert "Nothing was written." in visible

    def test_missing_transaction_outcome_is_presented_conservatively(
        self, tmp_path: Path
    ) -> None:
        result = _tutorial_result(tmp_path, broken_transaction=None)
        evidence = demo_proof.collect_evidence(result, tmp_path)
        assert evidence.broken.plain.key == "check_the_record"
        assert not evidence.broken.plain.nothing_written

    def test_identical_is_claimed_only_when_digests_match(self, tmp_path: Path) -> None:
        result = _tutorial_result(tmp_path, identical_frames=False)
        evidence = demo_proof.collect_evidence(result, tmp_path)
        assert not evidence.screens_identical
        page = demo_proof.render_demo_page(evidence)
        assert "pixel for pixel" not in page
        assert "look the same" in page

    def test_a_pair_that_did_not_show_the_difference_says_so(
        self, tmp_path: Path
    ) -> None:
        result = _tutorial_result(tmp_path, broken_records=1)
        evidence = demo_proof.collect_evidence(result, tmp_path)
        assert not evidence.showed_the_difference
        visible = _visible_text(demo_proof.render_demo_page(evidence))
        assert "This demo didn't go the way it should have." in visible
        assert "found only one note" not in visible

    def test_page_is_self_contained_and_escaped(self, tmp_path: Path) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        page = demo_proof.render_demo_page(evidence)
        assert "<script" not in page
        assert not re.search(r'(?:src|href)="https?://', page)
        assert "<link" not in page
        assert "refuted &lt;against&gt; the system" in page
        assert "refuted <against>" not in page
        # Dark mode is defined for both the media query and an explicit theme.
        assert "prefers-color-scheme: dark" in page
        assert ':root[data-theme="dark"]' in page

    def test_technical_terms_stay_folded(self, tmp_path: Path) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        page = demo_proof.render_demo_page(evidence)
        visible = _visible_text(page)
        for term in ("RECONCILIATION_REQUIRED", "HALTED", "VERIFIED", "oracle"):
            assert term not in visible
        assert "RECONCILIATION_REQUIRED" in page.split("<details>", 1)[1]

    def test_no_pending_decision_hides_the_choices(self, tmp_path: Path) -> None:
        result = _tutorial_result(tmp_path, pending=False)
        evidence = demo_proof.collect_evidence(result, tmp_path)
        assert evidence.choices == []
        page = demo_proof.render_demo_page(evidence)
        assert "What a person sees when a run stops" not in page
        assert "Stopped and asked a person" not in page

    def test_unknown_engine_option_is_shown_verbatim(self) -> None:
        text = "Do something the table doesn't know"
        assert demo_proof.plain_choice(text) == text


# ---------------------------------------------------------------------------
# Terminal summary, output folder, browser
# ---------------------------------------------------------------------------


class TestSummaryAndHelpers:
    def test_summary_is_short_and_derived_from_the_plain_results(
        self, tmp_path: Path
    ) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        lines = demo_proof.summary_lines(evidence, tmp_path / "index.html", False)
        assert len(lines) <= 7
        text = "\n".join(lines)
        assert "Run 1  Done and checked." in text
        assert "Run 2  Stopped: the screen said saved, the record says no." in text
        assert "Check the record before anyone runs this again." in text
        assert "(open it in a browser)" in text
        assert lines[-1].startswith("Next: openadapt-flow record ")

    def test_summary_uses_the_launcher_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evidence = demo_proof.collect_evidence(_tutorial_result(tmp_path), tmp_path)
        monkeypatch.setattr("sys.argv", ["/venv/bin/openadapt", "flow", "demo"])
        lines = demo_proof.summary_lines(evidence, tmp_path / "index.html", True)
        assert lines[-1].startswith("Next: openadapt flow record ")
        assert "(opened in your browser)" in "\n".join(lines)

    def test_default_output_dir_never_reuses_a_folder(self, tmp_path: Path) -> None:
        first = demo_proof.choose_output_dir(None, cwd=tmp_path)
        assert first == tmp_path.resolve() / "openadapt-demo"
        first.mkdir()
        second = demo_proof.choose_output_dir(None, cwd=tmp_path)
        assert second.name == "openadapt-demo-2"

    def test_a_name_an_earlier_run_reserved_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deleted demo folder's run paths stay reserved by the engine."""

        import sqlite3

        from openadapt_flow.runtime.durable.authority import _path_key

        db = tmp_path / "authority.sqlite3"
        monkeypatch.setenv("OPENADAPT_DURABLE_AUTHORITY_DB", str(db))
        work = tmp_path / "work"
        work.mkdir()
        reserved = work.resolve() / "openadapt-demo"
        with sqlite3.connect(db) as connection:
            connection.execute(
                "CREATE TABLE durable_authority (path_key TEXT PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO durable_authority VALUES (?)",
                (_path_key(reserved / "run"),),
            )
        connection.close()

        assert demo_proof.run_paths_reserved(reserved)
        assert not demo_proof.run_paths_reserved(work.resolve() / "other")
        # The folder doesn't exist, but its run path is taken: skip the name.
        chosen = demo_proof.choose_output_dir(None, cwd=work)
        assert chosen.name == "openadapt-demo-2"
        with pytest.raises(demo_proof.DemoError, match="never reuses a run folder"):
            demo_proof.choose_output_dir(reserved)

    def test_no_registry_means_nothing_is_reserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "OPENADAPT_DURABLE_AUTHORITY_DB", str(tmp_path / "missing.sqlite3")
        )
        assert not demo_proof.run_paths_reserved(tmp_path / "openadapt-demo")

    def test_explicit_non_empty_output_dir_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
        with pytest.raises(demo_proof.DemoError, match="isn't empty"):
            demo_proof.choose_output_dir(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        assert demo_proof.choose_output_dir(empty) == empty.resolve()

    @pytest.mark.parametrize(
        ("platform", "environ", "tty", "expected"),
        [
            ("darwin", {}, True, True),
            ("win32", {}, True, True),
            ("linux", {"DISPLAY": ":0"}, True, True),
            ("linux", {}, True, False),
            ("darwin", {"SSH_CONNECTION": "x"}, True, False),
            ("darwin", {}, False, False),
        ],
    )
    def test_display_available(
        self, platform: str, environ: dict[str, str], tty: bool, expected: bool
    ) -> None:
        assert (
            demo_proof.display_available(
                platform=platform, environ=environ, isatty=lambda: tty
            )
            is expected
        )

    def test_png_size(self, tmp_path: Path) -> None:
        path = tmp_path / "f.png"
        path.write_bytes(_png(128, 80))
        assert demo_proof.png_size(path) == (128, 80)
        path.write_bytes(b"not a png")
        assert demo_proof.png_size(path) is None
        assert demo_proof.png_size(None) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestDemoCommand:
    def test_parser_registers_demo(self) -> None:
        helptext = build_parser().format_help()
        assert "demo" in helptext
        assert "openadapt-flow demo" in helptext or "demo' to see" in helptext

    def test_demo_prints_at_most_eight_lines_and_writes_the_page(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[dict] = []

        def fake_run_tutorial(work_dir, **kwargs):
            calls.append(kwargs)
            return _tutorial_result(Path(work_dir))

        monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
        monkeypatch.delenv("OPENADAPT_FLOW_SCRUB", raising=False)
        out = tmp_path / "proof"
        assert main(["demo", "--out", str(out), "--no-open"]) == 0
        printed = capsys.readouterr().out.strip("\n").splitlines()
        assert len(printed) <= 8
        assert (out / "index.html").is_file()
        assert calls == [{"headed": False, "echo": None, "break_it": True}]
        # The operator's privacy setting is restored after the demo.
        import os

        assert "OPENADAPT_FLOW_SCRUB" not in os.environ

    def test_demo_exit_1_when_the_pair_did_not_show_the_difference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tutorial_module,
            "run_tutorial",
            lambda work_dir, **kw: _tutorial_result(Path(work_dir), broken_records=1),
        )
        assert main(["demo", "--out", str(tmp_path / "p"), "--no-open"]) == 1

    def test_demo_exit_2_when_a_stage_lacks_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def refuse(work_dir, **kwargs):
            raise TutorialError("the engine FAILED to catch the injected fault")

        monkeypatch.setattr(tutorial_module, "run_tutorial", refuse)
        assert main(["demo", "--out", str(tmp_path / "p"), "--no-open"]) == 2
        assert "FAILED to catch the injected fault" in capsys.readouterr().out

    def test_demo_exit_2_when_the_engine_refuses_the_run_folder(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from openadapt_flow.runtime.durable.authority import DurableAuthorityBusy

        def busy(work_dir, **kwargs):
            raise DurableAuthorityBusy(
                "this canonical run path already has durable authority; "
                "use a new run directory"
            )

        monkeypatch.setattr(tutorial_module, "run_tutorial", busy)
        assert main(["demo", "--out", str(tmp_path / "p"), "--no-open"]) == 2
        out = capsys.readouterr().out
        assert "Traceback" not in out
        assert "Run it again with --out and a new folder." in out

    def test_demo_refuses_a_non_empty_out_before_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "x").write_text("x", encoding="utf-8")

        def must_not_run(*args, **kwargs):
            raise AssertionError("the demo ran despite a non-empty --out")

        monkeypatch.setattr(tutorial_module, "run_tutorial", must_not_run)
        assert main(["demo", "--out", str(tmp_path), "--no-open"]) == 2
