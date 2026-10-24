"""Golden runs of the replayer against the real browser and MockMed.

Each scenario replays one committed bundle (the canonical MockMed triage
demo, recorded and compiled once in CI on Linux) under a different MockMed
drift, through the real ``Replayer`` and ``PlaywrightBackend``. The run
report, the hashes of every file the run wrote, the backend action trace and
the live app state afterwards must match the committed baseline exactly.

A behavior-preserving refactor reproduces every baseline. A deliberate
behavior change regenerates them in CI in a change of its own:
``OPENADAPT_GOLDEN_WRITE=1 pytest tests/golden_runs`` (see harness.py).

Scenarios that depend on timing (MockMed's ``slow`` drift) are left out on
purpose: a golden run must give the same result every time.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime import Replayer
from tests.golden_runs.harness import (
    BASELINES,
    WRITE_ENV,
    GoldenResult,
    Normalizer,
    artifact_hashes,
    check_or_write,
    environment,
    tracing_backend,
)

pytestmark = pytest.mark.timeout(900)

NOTE_TEXT = "Golden triage booking three months"
PARAMS = {"note": NOTE_TEXT}
VIEWPORT = {"width": 1280, "height": 800}
BUNDLE_DIR = BASELINES / "bundles" / "mockmed-triage"

# name -> (MockMed drift, use_structural)
SCENARIOS: dict[str, tuple[str, bool]] = {
    # A clean run that saves the encounter.
    "mockmed_pass": ("", False),
    # The same run with the structural (DOM) rung enabled.
    "mockmed_pass_structural": ("", True),
    # The target patient row is missing: the identity check halts before input.
    "mockmed_missing_row": ("missing", False),
    # A look-alike row sits above the target: never act on the wrong patient.
    "mockmed_lookalike_row": ("lookalike", False),
    # More rows than recorded: still the right patient or a halt.
    "mockmed_data_growth": ("grow", False),
    # A modal covers the page: the postcondition fails and names the step.
    "mockmed_modal": ("modal", False),
    # The Save button is renamed: an irreversible step refuses a low rung.
    "mockmed_rename": ("rename", False),
    # A different theme: lower rungs re-locate targets and heal anchors.
    "mockmed_theme": ("theme", False),
    # Rows sorted differently from the recording.
    "mockmed_sort": ("sort", False),
}


@pytest.fixture(scope="module")
def browser(bundle_dir: Path) -> Iterator[object]:
    # Depends on bundle_dir so a recording, which runs its own sync
    # Playwright, finishes before this one starts (one per thread).
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def mockmed_url() -> Iterator[str]:
    from openadapt_flow.mockmed.server import serve

    url, stop = serve(port=0)
    yield url.rstrip("/")
    stop()


@pytest.fixture(scope="module")
def bundle_dir(mockmed_url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The committed bundle; recorded and compiled only when writing baselines."""
    if (BUNDLE_DIR / "workflow.json").is_file():
        return BUNDLE_DIR
    if os.environ.get(WRITE_ENV) != "1":
        pytest.skip(f"no committed bundle at {BUNDLE_DIR}; generate in CI")
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.demo_driver import record_triage_demo

    recording = record_triage_demo(
        mockmed_url, tmp_path_factory.mktemp("recording"), note_text=NOTE_TEXT
    )
    staging = tmp_path_factory.mktemp("bundle")
    compile_recording(recording, staging, name="golden-triage")
    BUNDLE_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staging, BUNDLE_DIR)
    return BUNDLE_DIR


def _replay(
    browser: object,
    bundle: Path,
    url: str,
    run_dir: Path,
    *,
    use_structural: bool,
) -> GoldenResult:
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    trace: list[dict] = []
    try:
        page.goto(url)
        backend = tracing_backend(PlaywrightBackend(page), trace)
        Replayer(backend, use_structural=use_structural).run(
            Workflow.load(bundle),
            params=PARAMS,
            bundle_dir=bundle,
            run_dir=run_dir,
        )
        app_state = {
            "hash": page.evaluate("location.hash"),
            "banner": page.evaluate(
                "(document.getElementById('saved-banner') || {}).textContent || null"
            ),
            "enc_item": page.evaluate(
                "(document.querySelector('.enc-item') || {}).textContent || null"
            ),
        }
    finally:
        page.close()
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    normalizer = Normalizer([str(run_dir), str(bundle), url])
    normalizer.collect(report)
    return GoldenResult(
        report=normalizer(report),
        # The report embeds times and paths; it is compared normalized above.
        artifacts=artifact_hashes(run_dir, skip={"report.json"}),
        trace=normalizer(trace),
        environment=environment(),
        app_state=normalizer(app_state),
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_run(
    name: str,
    browser: object,
    mockmed_url: str,
    bundle_dir: Path,
    tmp_path: Path,
) -> None:
    drift, use_structural = SCENARIOS[name]
    url = f"{mockmed_url}/?drift={drift}" if drift else f"{mockmed_url}/"
    attempts = iter(range(1, 10))

    def produce() -> GoldenResult:
        run_dir = tmp_path / f"run{next(attempts)}"
        return _replay(browser, bundle_dir, url, run_dir, use_structural=use_structural)

    check_or_write(name, produce)
