"""Tests for report rendering (report.py) and the bench harness (bench.py)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.bench import _percentile, run_bench
from openadapt_flow.ir import (
    Anchor,
    HealEvent,
    Resolution,
    RunReport,
    Step,
    StepResult,
    UnarmedStep,
    Workflow,
)
from openadapt_flow.report import render_bench_report, render_run_report


def _png(path: Path, color: tuple[int, int, int] = (200, 40, 40)) -> str:
    """Write a tiny PNG and return the path as given (for relative refs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color).save(path)
    return str(path)


def _anchor(template: str) -> Anchor:
    return Anchor(
        template=template,
        region=(10, 20, 160, 64),
        click_point=(90, 52),
        ocr_text="Save Encounter",
    )


def _make_run_dir(tmp_path: Path, *, success: bool) -> Path:
    """Build a synthetic run dir: report.json + steps/ + heals/ images."""
    run_dir = tmp_path / ("run_ok" if success else "run_fail")
    run_dir.mkdir()

    results: list[StepResult] = []
    # Step 1: clean template hit.
    _png(run_dir / "steps" / "step_login_before.png")
    _png(run_dir / "steps" / "step_login_after.png", (40, 200, 40))
    results.append(
        StepResult(
            step_id="step_login",
            intent="click 'Sign In'",
            ok=True,
            resolution=Resolution(
                rung="template", point=(90, 52), confidence=0.97, elapsed_ms=12.0
            ),
            postconditions_ok=True,
            before_png="steps/step_login_before.png",
            after_png="steps/step_login_after.png",
            elapsed_ms=350.0,
        )
    )
    # Step 2: healed via ocr rung.
    _png(run_dir / "steps" / "step_save_before.png")
    _png(run_dir / "steps" / "step_save_after.png", (40, 40, 200))
    _png(run_dir / "heals" / "step_save" / "heal_frame.png", (10, 10, 10))
    results.append(
        StepResult(
            step_id="step_save",
            intent="click 'Save Encounter'",
            ok=True,
            resolution=Resolution(
                rung="ocr", point=(500, 700), confidence=0.88, elapsed_ms=140.0
            ),
            postconditions_ok=True,
            heal=HealEvent(
                step_id="step_save",
                rung_used="ocr",
                old_anchor=_anchor("templates/step_save.png"),
                new_anchor=_anchor("templates/step_save_healed.png"),
                screenshot="heals/step_save/heal_frame.png",
                applied=True,
            ),
            before_png="steps/step_save_before.png",
            after_png="steps/step_save_after.png",
            elapsed_ms=820.0,
        )
    )
    # Step 3: final step — succeeds or fails depending on scenario.
    _png(run_dir / "steps" / "step_verify_before.png")
    _png(run_dir / "steps" / "step_verify_after.png", (250, 250, 40))
    results.append(
        StepResult(
            step_id="step_verify",
            intent="verify banner | with pipe",
            ok=success,
            resolution=Resolution(
                rung="template", point=(20, 30), confidence=0.91, elapsed_ms=9.0
            ),
            postconditions_ok=success,
            error=None if success else "postcondition TEXT_PRESENT timed out",
            before_png="steps/step_verify_before.png",
            after_png="steps/step_verify_after.png",
            elapsed_ms=410.0,
        )
    )

    report = RunReport(
        workflow_name="triage note",
        started_at="2026-07-06T12:00:00+00:00",
        params={"note": "Follow-up in 2 weeks"},
        results=results,
        success=success,
        rung_counts={"template": 2, "ocr": 1},
        heal_count=1,
        model_calls=2,
        est_model_cost_usd=0.0135,
        total_ms=1580.0,
        identity_applicable_steps=3,
        identity_armed_steps=2,
        identity_unarmed=[
            UnarmedStep(
                step_id="step_save",
                intent="click 'Save Encounter'",
                reason="no readable text in the target's row band",
            )
        ],
    )
    report.save(run_dir)
    return run_dir


# -- render_run_report --------------------------------------------------------


def test_run_report_json_is_utf8(tmp_path: Path) -> None:
    report = RunReport(
        workflow_name="Crème brûlée — ✓",
        started_at="2026-07-17T12:00:00+00:00",
    )

    path = report.save(tmp_path / "unicode-run")

    payload = path.read_bytes().decode("utf-8")
    assert "Crème brûlée — ✓" in payload


def test_run_report_success(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, success=True)
    out = render_run_report(run_dir)

    assert out == run_dir / "REPORT.md"
    md = out.read_text(encoding="utf-8")

    # Outcome headline.
    assert md.splitlines()[0].startswith("# ✅")
    assert "triage note" in md
    # Params.
    assert "## Parameters" in md
    assert "`note`" in md and "Follow-up in 2 weeks" in md
    # Identity-protection coverage: armed count + unarmed steps by id
    # with the reason — an unarmed click proceeds with NO identity check.
    assert "## Identity protection coverage" in md
    assert "**2 of 3 click steps identity-armed.**" in md
    assert "no identity verification" in md
    assert "| `step_save` " in md
    assert "no readable text in the target's row band" in md
    # Per-step table columns and rows.
    assert (
        "| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |"
    ) in md
    assert "`step_login`" in md and "template" in md and "0.97" in md
    assert "`step_save`" in md and "ocr" in md
    # Pipe in intent must be escaped, not break the table.
    assert "verify banner \\| with pipe" in md
    # Per-step evidence: a before/after frame for EVERY step, not just the
    # final / healed / failed ones.
    assert "## Per-step evidence" in md
    assert "![step_verify before](steps/step_verify_before.png)" in md
    assert "![step_verify after](steps/step_verify_after.png)" in md
    assert "![step_save before](steps/step_save_before.png)" in md
    assert "![step_save heal](heals/step_save/heal_frame.png)" in md
    # The clean, non-final, non-healed step is now shown too (every step is).
    assert "![step_login before](steps/step_login_before.png)" in md
    assert "![step_login after](steps/step_login_after.png)" in md
    # Each step block carries its governance line (rung + gates + heal + outcome).
    assert "**Rung** `template` (conf 0.97, resolved (90, 52))" in md
    assert "**Heal** healed via `ocr`" in md
    assert "**Outcome** ✅ ok" in md
    # Rung histogram.
    assert "## Rung histogram" in md
    assert "| `template` | 2 | ██ |" in md
    assert "| `ocr` | 1 | █ |" in md
    assert "| `grounder` | 0 |" in md
    # Totals incl. model calls / cost.
    assert "## Totals" in md
    assert "| model_calls | 2 |" in md
    assert "| est_model_cost_usd | $0.0135 |" in md
    assert "1580 ms" in md


def test_run_report_failure(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, success=False)
    md = render_run_report(run_dir).read_text(encoding="utf-8")

    assert md.splitlines()[0].startswith("# ❌")
    assert "FAILED" in md
    # Failed step is named with its error and before/after screenshots.
    assert "postcondition TEXT_PRESENT timed out" in md
    assert "![step_verify before](steps/step_verify_before.png)" in md
    assert "![step_verify after](steps/step_verify_after.png)" in md


def test_run_report_marks_unretained_frames(tmp_path: Path) -> None:
    """A step whose frame the run did not keep on disk is marked absent, never
    linked as a broken image (some bundles retain only a final frame)."""
    run_dir = _make_run_dir(tmp_path, success=True)
    # Drop step_login's before frame; its report.json path stays, but the file
    # is gone — the report must not emit a broken image link for it.
    (run_dir / "steps" / "step_login_before.png").unlink()

    md = render_run_report(run_dir).read_text(encoding="utf-8")

    assert "![step_login before](steps/step_login_before.png)" not in md
    assert "_frame not retained_" in md
    # The retained after-frame for the same step is still shown.
    assert "![step_login after](steps/step_login_after.png)" in md


def test_run_report_rejects_image_paths_outside_run(tmp_path: Path) -> None:
    """A report path cannot disclose a file outside the retained run bundle."""
    run_dir = _make_run_dir(tmp_path, success=True)
    outside = tmp_path / "outside.png"
    _png(outside)
    report = RunReport.model_validate_json((run_dir / "report.json").read_text())
    report.results[0].before_png = "../outside.png"
    report.results[0].after_png = str(outside.resolve())
    report.save(run_dir)

    md = render_run_report(run_dir).read_text(encoding="utf-8")

    assert "../outside.png" not in md
    assert str(outside.resolve()) not in md
    assert md.count("_frame not retained_") >= 2


def test_run_report_rejects_symlinked_image(tmp_path: Path) -> None:
    """A retained-frame link cannot escape through a symlink component."""
    run_dir = _make_run_dir(tmp_path, success=True)
    outside = tmp_path / "outside.png"
    _png(outside)
    linked = run_dir / "steps" / "linked.png"
    try:
        linked.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - platform policy, not behavior
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = RunReport.model_validate_json((run_dir / "report.json").read_text())
    report.results[0].before_png = "steps/linked.png"
    report.save(run_dir)

    md = render_run_report(run_dir).read_text(encoding="utf-8")

    assert "steps/linked.png" not in md
    assert "_frame not retained_" in md


def test_run_report_discloses_approved_unverified_effect(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, success=True)
    report = RunReport.model_validate_json((run_dir / "report.json").read_text())
    report.governed_authorization_id = "approval-123"
    report.governed_approval_source = "local-cli-explicit-flag"
    report.approved_unverified_effect_step_ids = ["step_save"]
    report.results[1].effect_approved_unverified = True
    report.results[1].effect_results = ["[approved-unverified] explicit approval"]
    report.save(run_dir)

    md = render_run_report(run_dir).read_text(encoding="utf-8")
    assert "**Governed authorization:** `approval-123`" in md
    assert "**Approved without independent effect verification:** `step_save`" in md
    assert "effect ⚠ approved" in md


def test_run_report_missing_json(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        render_run_report(tmp_path)


# -- bench --------------------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_runtime(reports: list[RunReport]) -> types.ModuleType:
    """Build a fake openadapt_flow.runtime module with a scripted Replayer."""
    module = types.ModuleType("openadapt_flow.runtime")
    calls: list[dict] = []

    class Replayer:
        def __init__(self, backend, **kwargs) -> None:
            self.backend = backend

        def run(
            self, workflow, *, params=None, bundle_dir, run_dir, save_healed_to=None
        ):
            calls.append(
                {
                    "backend": self.backend,
                    "workflow": workflow,
                    "params": params,
                    "bundle_dir": Path(bundle_dir),
                    "run_dir": Path(run_dir),
                }
            )
            return reports[len(calls) - 1]

    module.Replayer = Replayer
    module.calls = calls  # type: ignore[attr-defined]
    return module


def _bench_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    workflow = Workflow(
        name="triage note",
        params={"note": "Follow-up in 2 weeks"},
        steps=[Step(id="step_0", intent="click 'Sign In'", action="click")],
    )
    workflow.save(bundle)
    return bundle


def _report(
    success: bool, total_ms: float, rungs: dict[str, int], heals: int = 0
) -> RunReport:
    return RunReport(
        workflow_name="triage note",
        started_at="2026-07-06T12:00:00+00:00",
        success=success,
        rung_counts=rungs,
        heal_count=heals,
        model_calls=1,
        est_model_cost_usd=0.01,
        total_ms=total_ms,
    )


def test_run_bench_aggregates(tmp_path: Path, monkeypatch) -> None:
    bundle = _bench_bundle(tmp_path)
    reports = [
        _report(True, 100.0, {"template": 3}),
        _report(True, 200.0, {"template": 2, "ocr": 1}, heals=1),
        _report(True, 300.0, {"template": 3}),
        _report(False, 1000.0, {"template": 1, "geometry": 1}, heals=1),
    ]
    fake = _fake_runtime(reports)
    monkeypatch.setitem(sys.modules, "openadapt_flow.runtime", fake)

    backends: list[_FakeBackend] = []

    def backend_factory() -> _FakeBackend:
        backend = _FakeBackend()
        backends.append(backend)
        return backend

    run_root = tmp_path / "bench_runs"
    result = run_bench(
        bundle,
        backend_factory,
        4,
        params={"note": "hello"},
        run_root=run_root,
    )

    # Fresh backend per iteration, each closed after use.
    assert len(backends) == 4
    assert all(b.closed for b in backends)
    used = [c["backend"] for c in fake.calls]
    assert len(set(map(id, used))) == 4
    # Params and per-iteration run dirs forwarded to the Replayer.
    assert fake.calls[0]["params"] == {"note": "hello"}
    assert [c["run_dir"].name for c in fake.calls] == [
        "iter_000",
        "iter_001",
        "iter_002",
        "iter_003",
    ]
    assert fake.calls[0]["bundle_dir"] == bundle
    # Each iteration must replay a FRESH Workflow loaded from disk:
    # Replayer.run applies heals to the in-memory object, so sharing one
    # instance would leak iteration i's healed anchors into iteration i+1.
    workflows = [c["workflow"] for c in fake.calls]
    assert all(w.name == "triage note" for w in workflows)
    assert len({id(w) for w in workflows}) == 4

    # Aggregates.
    assert result["n"] == 4
    assert result["success_count"] == 3
    assert result["success_rate"] == pytest.approx(0.75)
    assert result["total_ms_p50"] == pytest.approx(250.0)
    assert result["total_ms_p95"] == pytest.approx(895.0)
    assert result["rung_counts"] == {"template": 9, "ocr": 1, "geometry": 1}
    assert result["heal_count"] == 2
    assert result["model_calls"] == 4
    assert result["est_model_cost_usd"] == pytest.approx(0.04)
    assert len(result["iterations"]) == 4
    assert result["iterations"][3]["success"] is False

    # bench.json written and round-trips.
    on_disk = json.loads((run_root / "bench.json").read_text())
    assert on_disk["success_count"] == 3
    assert on_disk["workflow_name"] == "triage note"


def test_run_bench_context_manager_factory(tmp_path: Path, monkeypatch) -> None:
    from contextlib import contextmanager

    bundle = _bench_bundle(tmp_path)
    fake = _fake_runtime([_report(True, 50.0, {"template": 1})])
    monkeypatch.setitem(sys.modules, "openadapt_flow.runtime", fake)

    events: list[str] = []

    @contextmanager
    def backend_factory():
        events.append("enter")
        yield _FakeBackend()
        events.append("exit")

    result = run_bench(bundle, backend_factory, 1, run_root=tmp_path / "cm_runs")
    assert result["success_count"] == 1
    assert events == ["enter", "exit"]


def test_percentile_edges() -> None:
    assert _percentile([], 50.0) == 0.0
    assert _percentile([42.0], 95.0) == 42.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50.0) == pytest.approx(2.5)


# -- render_bench_report ------------------------------------------------------


def test_render_bench_report(tmp_path: Path) -> None:
    bench = {
        "bundle": "/bundles/triage",
        "workflow_name": "triage note",
        "n": 3,
        "success_count": 2,
        "success_rate": 2 / 3,
        "total_ms_p50": 210.0,
        "total_ms_p95": 480.5,
        "rung_counts": {"template": 5, "ocr": 1},
        "heal_count": 1,
        "model_calls": 3,
        "est_model_cost_usd": 0.021,
        "iterations": [
            {
                "i": 0,
                "success": True,
                "total_ms": 200.0,
                "heal_count": 0,
                "run_dir": "runs/iter_000",
            },
            {
                "i": 1,
                "success": True,
                "total_ms": 210.0,
                "heal_count": 1,
                "run_dir": "runs/iter_001",
            },
            {
                "i": 2,
                "success": False,
                "total_ms": 500.0,
                "heal_count": 0,
                "run_dir": "runs/iter_002",
            },
        ],
    }
    bench_json = tmp_path / "bench.json"
    bench_json.write_text(json.dumps(bench))

    out = render_bench_report(bench_json, tmp_path / "sub" / "BENCH.md")
    md = out.read_text(encoding="utf-8")

    assert out.exists()
    assert md.splitlines()[0].startswith("# ❌")  # 2/3 succeeded
    assert "triage note" in md
    assert "67% (2/3)" in md
    assert "210" in md and "480" in md  # p50 / p95
    assert "| `template` | 5 |" in md
    assert "| `ocr` | 1 |" in md
    assert "model_calls" in md and "3" in md
    assert "$0.0210" in md
    assert "`runs/iter_002`" in md
