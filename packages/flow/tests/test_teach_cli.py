"""Self-serve HALT -> LEARN through the CLI ``teach`` verb (governed, one command).

Drives the SAME modal-once scenario the halt-learn library test proves
(``test_halt_learn_loop``) but end to end THROUGH ``openadapt-flow teach``: a
halted run directory + a fix demonstration in, an updated bundle out, and a
re-run that no longer halts. Plus the governed-refusal path: an underdetermined
fix is refused (nonzero exit, bundle unchanged, re-run still halts).

Deterministic and ``$0`` -- the SAME FakeBackend/FakeVision as Phase-2, no live
browser and no model calls. The fix is supplied two ways: a scripted correction
spec (CI) and a compiled recording directory (the operator's preferred path).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

# Reuse the modal-once scenario fixtures/helpers verbatim (no re-derivation).
from tests.test_halt_learn_loop import (
    DISMISS_POINT,
    INTENT_DISMISS,
    INTENT_SAVE,
    INTENT_VERIFY,
    MODAL_FACT,
    SKILL_ID,
    _base_workflow,
    _replay,
)
from tests.test_replayer import make_png

from openadapt_flow.__main__ import main
from openadapt_flow.ir import HaltObservation, RunReport, Workflow

# -- shared scaffolding ------------------------------------------------------


def _base_bundle(root: Path) -> Path:
    """A saved base bundle that HALTS on modal-once: the naive program plus the
    template crops the learned dismiss branch (``templates/x.png``) and the
    confirm step (``templates/verify.png``) resolve against on re-run."""
    bundle = root / "base_bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "verify.png").write_bytes(make_png((50, 20)))
    (bundle / "templates" / "x.png").write_bytes(make_png((20, 20)))
    _base_workflow().save(bundle)
    return bundle


def _halt_run(base_bundle: Path, run_dir: Path) -> RunReport:
    """Replay the base bundle on modal-once so it HALTS and persists report.json."""
    report, _ = _replay(
        _base_workflow(), modal_text=MODAL_FACT, bundle=base_bundle, run_dir=run_dir
    )
    assert report.terminal_outcome == "halt"
    assert (run_dir / "report.json").is_file()
    return report


def _dismiss_recording(recording: Path) -> Path:
    """A minimal 1-click recording of the operator dismissing the modal.

    One CLICK on a 'Dismiss' button, compiled through the ordinary
    ``compile_recording`` path -- a NEW intent the base program does not have, so
    the reference inducer splices it as the guarded dismiss branch."""
    import cv2

    (recording / "frames").mkdir(parents=True)
    before = np.full((800, 1280, 3), 255, np.uint8)
    cv2.rectangle(before, (60, 84), (220, 132), (40, 40, 40), -1)
    cv2.putText(
        before, "Dismiss", (74, 116), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2
    )
    after = np.full((800, 1280, 3), 255, np.uint8)  # modal gone
    for i, (tag, img) in enumerate([("before", before), ("after", after)] * 1):
        ok, buf = cv2.imencode(".png", img)
        assert ok
        (recording / "frames" / f"0000_{tag}.png").write_bytes(buf.tobytes())
    events = [{"i": 0, "kind": "click", "x": 140, "y": 108, "t": 1.0}]
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-dismiss-001",
                "created_at": "2026-07-14T00:00:00+00:00",
                "viewport": [1280, 800],
                "app_url": "http://localhost:0/",
                "params": {},
            }
        )
    )
    return recording


def _assert_resolved(out_bundle: Path, run_dir: Path) -> None:
    """The updated bundle now dismisses the modal and completes without halting."""
    learned = Workflow.load(out_bundle)
    report, backend = _replay(
        learned, modal_text=MODAL_FACT, bundle=out_bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.halt is None
    assert backend.actions == [
        ("press", "S"),
        ("click", *DISMISS_POINT, False),
        ("click", 410, 305, False),
    ]


# -- --help renders ----------------------------------------------------------


def test_teach_help_renders() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["teach", "--help"])
    assert exc.value.code == 0


# -- success: a valid fix resolves the halt, one command ---------------------


def test_teach_resolves_modal_once_via_correction_spec(tmp_path: Path) -> None:
    base_bundle = _base_bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halt_run(base_bundle, run_dir)

    # The scripted fix: dismiss the survey modal (the one corrective action).
    fix = tmp_path / "fix.json"
    fix.write_text(
        json.dumps(
            {"resolution_steps": [{"intent": INTENT_DISMISS, "action": "click"}]}
        )
    )
    out = tmp_path / "out_bundle"

    rc = main(
        [
            "teach",
            str(run_dir),
            "--fix",
            str(fix),
            "--bundle",
            str(base_bundle),
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    assert (out / "workflow.json").is_file()
    _assert_resolved(out, tmp_path / "rerun")


def test_teach_resolves_modal_once_via_recording(tmp_path: Path) -> None:
    base_bundle = _base_bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halt_run(base_bundle, run_dir)

    fix = _dismiss_recording(tmp_path / "fix_recording")
    out = tmp_path / "out_bundle"

    rc = main(
        [
            "teach",
            str(run_dir),
            "--fix",
            str(fix),
            "--bundle",
            str(base_bundle),
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    assert (out / "workflow.json").is_file()
    _assert_resolved(out, tmp_path / "rerun")


# -- governed refusal: an underdetermined fix is refused, bundle unchanged ----


def test_teach_refuses_underdetermined_fix(tmp_path: Path) -> None:
    base_bundle = _base_bundle(tmp_path)

    # A halt that observed NO discriminating dialog text -> no fact to key a
    # branch guard on. Persist it as this run's report.json.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    blind = RunReport(workflow_name=SKILL_ID, started_at="t")
    blind.halt = HaltObservation(
        state_id="s_verify",
        intent=INTENT_VERIFY,
        reason="confirm banner could not be resolved (unknown overlay)",
        outcome="halt",
        observed_texts=[],  # nothing discriminating observed
        completed_intents=[INTENT_SAVE],
    )
    blind.save(run_dir)

    fix = tmp_path / "fix.json"
    fix.write_text(
        json.dumps(
            {"resolution_steps": [{"intent": INTENT_DISMISS, "action": "click"}]}
        )
    )
    out = tmp_path / "out_bundle"

    rc = main(
        [
            "teach",
            str(run_dir),
            "--fix",
            str(fix),
            "--bundle",
            str(base_bundle),
            "--out",
            str(out),
        ]
    )

    # Governed refusal: nonzero exit, and NOTHING written (bundle unchanged).
    assert rc == 1
    assert not out.exists()

    # The base bundle still HALTS on modal-once (no ungoverned learning).
    report, _ = _replay(
        _base_workflow(),
        modal_text=MODAL_FACT,
        bundle=base_bundle,
        run_dir=tmp_path / "rerun",
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"


# -- input guards ------------------------------------------------------------


def test_teach_errors_on_non_halted_run(tmp_path: Path) -> None:
    base_bundle = _base_bundle(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    RunReport(workflow_name=SKILL_ID, started_at="t", success=True).save(run_dir)

    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps({"resolution_steps": [{"intent": INTENT_DISMISS}]}))
    out = tmp_path / "out_bundle"

    rc = main(
        [
            "teach",
            str(run_dir),
            "--fix",
            str(fix),
            "--bundle",
            str(base_bundle),
            "--out",
            str(out),
        ]
    )
    assert rc == 2  # TeachError: nothing to teach a run that did not halt
    assert not out.exists()


def test_teach_errors_on_missing_report(tmp_path: Path) -> None:
    base_bundle = _base_bundle(tmp_path)
    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps({"resolution_steps": [{"intent": INTENT_DISMISS}]}))
    rc = main(
        [
            "teach",
            str(tmp_path / "does_not_exist"),
            "--fix",
            str(fix),
            "--bundle",
            str(base_bundle),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
