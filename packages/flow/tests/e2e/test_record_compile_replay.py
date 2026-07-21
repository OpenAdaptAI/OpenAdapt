"""E2E: record the MockMed demo once, compile it, replay it under drift.

Matrix (DESIGN.md "Test policy"):

- baseline x3: all-template resolution, zero heals.
- params: replay with a note value DIFFERENT from the recorded one succeeds
  and the new value is verified via banner OCR (the identity case cannot
  distinguish real substitution from replaying the baked-in literal).
- theme / move / rename: succeed WITH heals, then the healed bundle replays
  all-template.
- modal: fails gracefully, naming the failing step + postcondition.
- risk gate: an irreversible step that only resolves below the ocr rung
  refuses to act.
- CLI smoke: demo-record -> compile -> replay (different param value) ->
  emit-skill round-trip.

The recording/compile are session-shared (see conftest); every replay uses a
fresh browser page so MockMed state never leaks between scenarios.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.ir import PostconditionKind, RunReport, Workflow
from openadapt_flow.vision.ocr import normalize_text

from .conftest import NOTE_TEXT, PARAMS, drift_url

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.timeout(600)


def squash(text: str) -> str:
    """Normalize text for OCR-tolerant comparison (drop case + whitespace)."""
    return "".join(normalize_text(text).split())


def contains_fuzzy(haystack: str, needle: str, min_ratio: float = 0.8) -> bool:
    """OCR-tolerant containment: >= min_ratio of needle's squashed chars
    appear (via difflib matching blocks) inside the squashed haystack."""
    hay, target = squash(haystack), squash(needle)
    if not target or not hay:
        return False
    matcher = difflib.SequenceMatcher(None, target, hay)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(target) >= min_ratio


def anchored_results(report: RunReport):
    """Step results that carry a resolution (i.e. anchored click steps)."""
    return [r for r in report.results if r.resolution is not None]


def rung_of(report: RunReport, step_id: str) -> str:
    """Resolution rung used by ``step_id``."""
    for r in report.results:
        if r.step_id == step_id and r.resolution is not None:
            return r.resolution.rung
    raise AssertionError(f"no resolution recorded for {step_id}")


def failure_summary(report: RunReport) -> str:
    """Compact failure description for assertion messages."""
    lines = [f"success={report.success} rungs={report.rung_counts}"]
    for r in report.results:
        rung = r.resolution.rung if r.resolution else "-"
        lines.append(
            f"  {r.step_id} ok={r.ok} rung={rung} "
            f"pc={r.postconditions_ok} err={r.error}"
        )
    return "\n".join(lines)


# The canonical demo: 8 anchored clicks + 3 unanchored TYPE steps.
N_STEPS = 11
N_ANCHORED = 8
STEP_OPEN = "step_005"  # "Open" button (renamed to "View" under rename)
STEP_NEW_ENC = "step_006"  # "New Encounter" (relocated under move)
STEP_SAVE = "step_010"  # "Save Encounter" (renamed/relocated/modal target)
MOVED_STEPS = (STEP_NEW_ENC, STEP_SAVE)
RENAMED_STEPS = (STEP_OPEN, STEP_SAVE)


class TestCompiledBundle:
    """Sanity on the shared compiled artifact (fast, no replay)."""

    def test_bundle_shape(self, bundle) -> None:
        wf = bundle.workflow
        assert len(wf.steps) == N_STEPS
        assert wf.params == PARAMS
        anchored = [s for s in wf.steps if s.anchor is not None]
        assert len(anchored) == N_ANCHORED
        for step in anchored:
            assert (bundle.dir / step.anchor.template).is_file()
        assert (bundle.dir / "workflow.json").is_file()
        assert (bundle.dir / "workflow.py").is_file()

    def test_save_step_asserts_stable_new_text(self, bundle) -> None:
        """The compiled save step must still verify SOMETHING about the
        post-save screen via TEXT_PRESENT (this is what makes modal drift
        fail), just never the parameterized note itself."""
        save = bundle.workflow.steps[-1]
        assert save.id == STEP_SAVE
        texts = [
            pc.text
            for pc in save.expect
            if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text
        ]
        assert texts, "save step lost its TEXT_PRESENT postcondition"

    def test_param_value_not_baked_into_any_postcondition(self, bundle) -> None:
        """The demo-time note value varies per run: if any step's
        postconditions embed it (e.g. via the 'Encounter saved — <note>'
        banner), the bundle only replays with the exact recorded value."""
        for step in bundle.workflow.steps:
            for pc in step.expect:
                if pc.kind is not PostconditionKind.TEXT_PRESENT or not pc.text:
                    continue
                assert not contains_fuzzy(pc.text, NOTE_TEXT), (
                    f"recorded note baked into {step.id}: {pc.text!r}"
                )


class TestBaseline:
    def test_replay_three_times_all_template_no_heals(
        self, bundle, mockmed_url, replay
    ) -> None:
        for i in range(3):
            report, run_dir = replay(bundle.dir, mockmed_url)
            assert report.success, f"iteration {i}:\n{failure_summary(report)}"
            assert report.heal_count == 0
            # NOTE: the e2e Replayer runs with grounder=None, so model_calls
            # is structurally 0 here — the REAL zero-model-call proof is
            # rung_counts == {"template": N} below (the grounder rung is the
            # only model-calling rung and it never fired).
            assert report.model_calls == 0
            assert report.rung_counts == {"template": N_ANCHORED}
            for result in anchored_results(report):
                assert result.resolution.rung == "template"

            # Run artifacts: report.json + per-step before/after PNGs.
            assert (run_dir / "report.json").is_file()
            loaded = RunReport.model_validate(
                json.loads((run_dir / "report.json").read_text())
            )
            assert loaded.success is True
            for result in report.results:
                assert (run_dir / result.before_png).is_file()
                assert (run_dir / result.after_png).is_file()

            # The save step's postconditions were verified live (params with
            # a DIFFERENT note value are covered by TestParamSubstitution).
            last = report.results[-1]
            assert last.step_id == STEP_SAVE
            assert last.postconditions_ok is True


class TestParamSubstitution:
    def test_replay_with_different_note_value(
        self, bundle, mockmed_url, replay
    ) -> None:
        """Replay with a note value the demo never typed: the run must
        succeed and the new value must appear in the saved banner (verified
        via OCR of the final screen). Guards both the compiler (demo-time
        value baked into postconditions) and the replayer (step.text typed
        instead of params[step.param])."""
        from openadapt_flow.vision.ocr import ocr

        new_note = "Annual physical follow up call"
        assert squash(new_note) != squash(NOTE_TEXT)

        report, run_dir = replay(bundle.dir, mockmed_url, params={"note": new_note})
        assert report.success, failure_summary(report)

        # Banner OCR on the final after-frame: "Encounter saved — <note>".
        after_png = (run_dir / report.results[-1].after_png).read_bytes()
        expected = f"Encounter saved — {new_note[:40]}"
        lines = ocr(after_png)
        best = max(
            (
                difflib.SequenceMatcher(
                    None, squash(line.text), squash(expected)
                ).ratio()
                for line in lines
            ),
            default=0.0,
        )
        assert best >= 0.8, (
            f"saved banner with the NEW note not found; best ratio {best:.2f}; "
            f"lines: {[line.text for line in lines]}"
        )
        # The recorded demo-time note must NOT be on the final screen.
        for line in lines:
            assert squash(NOTE_TEXT) not in squash(line.text), line.text

    def test_replay_with_no_params_uses_recorded_defaults(
        self, bundle, mockmed_url, replay
    ) -> None:
        """workflow.params carries the recorded example values; a replay
        with no explicit params must succeed by falling back to them."""
        report, _ = replay(bundle.dir, mockmed_url, params={})
        assert report.success, failure_summary(report)
        assert report.params == PARAMS


class TestThemeDrift:
    def test_replay_heals_then_healed_bundle_is_template_clean(
        self, bundle, mockmed_url, replay, tmp_path: Path
    ) -> None:
        healed = tmp_path / "healed-bundle"
        url = drift_url(mockmed_url, "theme")

        report, run_dir = replay(bundle.dir, url, save_healed_to=healed)
        assert report.success, failure_summary(report)
        # The dark palette breaks template matching for EVERY anchor (dark
        # crops score far below TEMPLATE_THRESHOLD): all anchored steps must
        # resolve off-template and be healed. Anything less means the drift
        # fixture (or the threshold) stopped exercising the lower rungs.
        assert report.rung_counts.get("template", 0) == 0, failure_summary(report)
        assert report.heal_count == N_ANCHORED, failure_summary(report)
        for result in anchored_results(report):
            assert result.resolution.rung != "template"
        # Heal artifacts persisted per healed step.
        for result in report.results:
            if result.heal is not None:
                heal_dir = run_dir / "heals" / result.step_id
                assert (heal_dir / "heal.json").is_file()
                assert (heal_dir / "template.png").is_file()

        # The healed bundle replays on the SAME drift entirely via templates.
        report2, _ = replay(healed, url)
        assert report2.success, failure_summary(report2)
        assert report2.heal_count == 0
        assert report2.rung_counts == {"template": N_ANCHORED}


class TestMoveDrift:
    def test_replay_heals_then_healed_bundle_is_template_clean(
        self, bundle, mockmed_url, replay, tmp_path: Path
    ) -> None:
        healed = tmp_path / "healed-bundle"
        url = drift_url(mockmed_url, "move")

        report, _ = replay(bundle.dir, url, save_healed_to=healed)
        assert report.success, failure_summary(report)
        # The two relocated buttons resolve via a global rung, not locally.
        for step_id in MOVED_STEPS:
            assert rung_of(report, step_id) in ("template_global", "ocr"), (
                failure_summary(report)
            )
        assert report.heal_count >= 1
        healed_ids = {r.step_id for r in report.results if r.heal is not None}
        assert set(MOVED_STEPS) <= healed_ids

        report2, _ = replay(healed, url)
        assert report2.success, failure_summary(report2)
        assert report2.heal_count == 0
        assert report2.rung_counts == {"template": N_ANCHORED}


class TestRenameDrift:
    def test_replay_heals_via_lower_rungs_and_updates_ocr_text(
        self, bundle_writes_reversible, mockmed_url, replay, tmp_path: Path
    ) -> None:
        # Uses the writes-reversible bundle: rename drift drives the Save button
        # down to the geometry rung, which the auto-classified irreversible
        # risk gate would otherwise (correctly) refuse — see the fixture and
        # TestIrreversibleRiskGate. This test isolates the HEALING mechanism.
        bundle = bundle_writes_reversible
        healed = tmp_path / "healed-bundle"
        url = drift_url(mockmed_url, "rename")

        report, _ = replay(bundle.dir, url, save_healed_to=healed)
        assert report.success, failure_summary(report)
        # Renamed labels break template AND ocr evidence; geometry resolves
        # (ocr acceptable if fuzzy matching still clears the bar).
        for step_id in RENAMED_STEPS:
            assert rung_of(report, step_id) in ("geometry", "ocr"), failure_summary(
                report
            )
        assert report.heal_count >= 1

        # Heals refreshed the anchor text to the NEW label.
        heals = {r.step_id: r.heal for r in report.results if r.heal}
        save_heal = heals[STEP_SAVE]
        assert save_heal.new_anchor.ocr_text is not None
        assert "submit" in normalize_text(save_heal.new_anchor.ocr_text)
        open_heal = heals[STEP_OPEN]
        assert open_heal.new_anchor.ocr_text is not None
        assert "view" in normalize_text(open_heal.new_anchor.ocr_text)

        report2, _ = replay(healed, url)
        assert report2.success, failure_summary(report2)
        assert report2.heal_count == 0
        assert report2.rung_counts == {"template": N_ANCHORED}


class TestModalDrift:
    def test_replay_fails_gracefully_naming_the_postcondition(
        self, bundle, mockmed_url, replay
    ) -> None:
        url = drift_url(mockmed_url, "modal")

        # Must not raise: Replayer.run reports failure instead of crashing.
        report, run_dir = replay(bundle.dir, url)
        assert report.success is False

        # Every step before the save ran fine; the save step failed its
        # postconditions (the post-save patient screen never appeared — a
        # blocking Survey modal did), and the run aborted there.
        assert len(report.results) == N_STEPS
        assert all(r.ok for r in report.results[:-1]), failure_summary(report)
        failing = report.results[-1]
        assert failing.step_id == STEP_SAVE
        assert failing.ok is False
        assert failing.postconditions_ok is False
        assert failing.error is not None
        assert "Postconditions failed" in failing.error
        assert STEP_SAVE in failing.error
        # The error must name the failed TEXT_PRESENT specifically: the
        # post-save screen text never appeared (a Survey modal did). If the
        # compiler ever stopped emitting TEXT_PRESENT for the save step,
        # accepting region_stable alone would green-light that regression.
        assert "text_present" in failing.error, failing.error
        # Before/after evidence saved for the failing step.
        assert (run_dir / failing.before_png).is_file()
        assert (run_dir / failing.after_png).is_file()
        # Saved report reflects the failure.
        saved = json.loads((run_dir / "report.json").read_text())
        assert saved["success"] is False


class TestIrreversibleRiskGate:
    def test_irreversible_step_refuses_below_ocr_resolution(
        self, recording_dir, mockmed_url, replay, tmp_path: Path
    ) -> None:
        """v0 policy end-to-end, through the supported plumbing: an
        irreversible step whose anchor only resolves below the ocr rung must
        NOT act. The save step is marked irreversible here via an explicit
        ``risk_overrides`` (auto risk-classification would now mark it
        irreversible too — see openadapt_flow.risk — but the override keeps
        this test independent of the heuristic); under rename drift its
        template no longer matches, and with ocr_text cleared the geometry
        rung (landmarks are unchanged by rename) is the only evidence left —
        the gate must refuse and fail the run."""
        from openadapt_flow.compiler import compile_recording

        gated = tmp_path / "gated-bundle"
        wf = compile_recording(
            recording_dir,
            gated,
            name="triage-demo",
            risk_overrides={STEP_SAVE: "irreversible"},
        )
        save = wf.steps[-1]
        assert save.id == STEP_SAVE and save.anchor is not None
        assert save.risk == "irreversible"
        # Force a deterministic below-ocr resolution: without ocr evidence,
        # rename drift leaves only the geometry rung for this anchor.
        save.anchor.ocr_text = None
        wf.save(gated)

        url = drift_url(mockmed_url, "rename")
        report, _ = replay(gated, url)

        assert report.success is False
        failing = report.results[-1]
        assert failing.step_id == STEP_SAVE
        assert failing.ok is False
        assert failing.resolution is not None
        assert failing.resolution.rung in ("geometry", "grounder")
        assert failing.error is not None
        assert "irreversible" in failing.error
        assert "needs human confirmation" in failing.error
        assert "refusing to act" in failing.error
        # Nothing was clicked: no postconditions were even evaluated.
        assert failing.postconditions_ok is None


class TestCliSmoke:
    def test_record_compile_replay_emit_roundtrip(
        self, mockmed_url, tmp_path: Path
    ) -> None:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        rec = tmp_path / "rec"
        bundle = tmp_path / "bundle"
        run_dir = tmp_path / "run"
        skills = tmp_path / "skills"

        def cli(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "openadapt_flow", *args],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp_path,
                timeout=420,
            )

        proc = cli("demo-record", "--out", str(rec), "--note-text", NOTE_TEXT)
        assert proc.returncode == 0, proc.stderr
        assert (rec / "meta.json").is_file()
        assert (rec / "events.jsonl").is_file()

        proc = cli("compile", str(rec), "--out", str(bundle), "--name", "cli-smoke")
        assert proc.returncode == 0, proc.stderr
        assert (bundle / "workflow.json").is_file()

        # Replay with a DIFFERENT note value than the one recorded: the CLI
        # path must exercise real param substitution, not the identity case.
        proc = cli(
            "replay",
            str(bundle),
            "--url",
            mockmed_url,
            "--run-dir",
            str(run_dir),
            "--param",
            "note=Different note for the CLI smoke run",
        )
        assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        assert (run_dir / "report.json").is_file()
        report_md = run_dir / "REPORT.md"
        assert report_md.is_file()
        md = report_md.read_text()
        assert "cli-smoke" in md
        # Protection coverage is first-class in every run report: N of M
        # armed, and any unarmed click listed with its compile-time reason.
        assert "## Identity protection coverage" in md
        assert "click steps identity-armed" in md
        # The bundle itself carries the audit fields for click steps.
        wf = json.loads((bundle / "workflow.json").read_text())
        clicks = [s for s in wf["steps"] if s["action"] in ("click", "double_click")]
        assert clicks and all(s["identity_armed"] is not None for s in clicks)
        assert all(s["identity_armed"] or s["identity_unarmed_reason"] for s in clicks)

        proc = cli("emit-skill", str(bundle), "--out", str(skills))
        assert proc.returncode == 0, proc.stderr
        skill_files = list(skills.rglob("SKILL.md"))
        assert len(skill_files) == 1
        content = skill_files[0].read_text()
        assert "openadapt-flow replay" in content

        # README quickstart contract: `replay` with no --url self-serves
        # MockMed, and --drift demonstrates healing in one command.
        selfserve_run = tmp_path / "run-selfserve"
        healed = tmp_path / "healed"
        proc = cli(
            "replay",
            str(bundle),
            "--drift",
            "theme",
            "--run-dir",
            str(selfserve_run),
            "--save-healed-to",
            str(healed),
        )
        assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        assert "bundled MockMed" in proc.stdout
        assert (selfserve_run / "report.json").is_file()
        assert (healed / "workflow.json").is_file()
        report = json.loads((selfserve_run / "report.json").read_text())
        assert report["success"] and report["heal_count"] >= 1

        # --url and --drift together must be rejected loudly.
        proc = cli("replay", str(bundle), "--url", mockmed_url, "--drift", "theme")
        assert proc.returncode != 0
        assert "--drift" in proc.stderr
