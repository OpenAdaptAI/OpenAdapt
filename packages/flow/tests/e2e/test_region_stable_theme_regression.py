"""Regression for the exact v1.16.1 theme + parameter false halt.

The committed fixture is metadata-only.  This test regenerates the bundled
synthetic MockMed evidence locally, so no screenshots or third-party
application source enter the repository or package artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.benchmark.dom_arm import verify_final_state
from openadapt_flow.compiler import compile_recording
from openadapt_flow.demo_driver import record_triage_demo
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime import Replayer

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "theme-region-stable-overhalt-v1.16.1.json"
)


def _load_regression() -> dict:
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["artifact_policy"].startswith("metadata only")
    assert fixture["observed_count"] == 3
    assert fixture["outcomes"]["over_halt"] == 3
    assert len(fixture["replay_notes"]) == fixture["observed_count"]
    return fixture


def _run(bundle: Path, url: str, note: str, run_dir: Path):
    backend, close = PlaywrightBackend.launch(url)
    try:
        report = Replayer(backend).run(
            Workflow.load(bundle),
            params={"note": note},
            bundle_dir=bundle,
            run_dir=run_dir,
        )
        verdict = verify_final_state(backend.screenshot(), note)
        return report, verdict
    finally:
        close()


@pytest.fixture(scope="module")
def regression_bundle(
    mockmed_url: str, tmp_path_factory: pytest.TempPathFactory
) -> tuple[dict, Path]:
    regression = _load_regression()
    root = tmp_path_factory.mktemp("theme-parameter-regression")
    recording = record_triage_demo(
        mockmed_url,
        root / "recording",
        note_text=regression["recording_note"],
    )
    bundle = root / "bundle"
    compile_recording(recording, bundle, name="theme-parameter-regression")
    return regression, bundle


@pytest.mark.parametrize("note_index", range(3))
def test_theme_parameter_substitution_keeps_structural_outcome_evidence(
    mockmed_url: str,
    tmp_path: Path,
    regression_bundle: tuple[dict, Path],
    note_index: int,
) -> None:
    regression, bundle = regression_bundle
    note = regression["replay_notes"][note_index]

    report, verdict = _run(
        bundle,
        f"{mockmed_url}{regression['condition']}",
        note,
        tmp_path / "theme-run",
    )

    assert verdict.success is True
    assert verdict.wrong_action is False
    assert report.success is True
    assert sum(result.drift_oracle_calls for result in report.results) == 0
    save = next(result for result in report.results if result.step_id == "step_010")
    assert save.postconditions_ok is True


def test_true_region_change_still_refuses_after_structural_theme_match(
    mockmed_url: str, tmp_path: Path, regression_bundle: tuple[dict, Path]
) -> None:
    regression, bundle = regression_bundle

    report, verdict = _run(
        bundle,
        f"{mockmed_url}?drift=theme,modal",
        regression["replay_notes"][0],
        tmp_path / "theme-modal-run",
    )

    assert verdict.success is False
    assert report.success is False
    assert sum(result.drift_oracle_calls for result in report.results) == 0
    failure = next(result for result in report.results if not result.ok)
    assert failure.step_id == regression["failed_step"]
    assert "region_stable" in (failure.error or "")
    assert "text_present" in (failure.error or "")
