"""E2E: record a date picker today, replay the same recording in other months.

A form's date field opens a picker on the current month, as real apps do. The
recording sees today's month; the compiled program must check the run's month,
not the recorded one. The page's clock (Playwright's clock control) and the
runtime's clock are set together to another month, and to January of the next
year (the rollover); a page showing a different month than the run's clock
must still halt. The last case is the pre-fix bundle (the recorded literal):
it halts in another month, which is the bug this fixes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import PostconditionKind, Workflow
from openadapt_flow.recorder import Recorder
from openadapt_flow.runtime import Replayer

pytestmark = pytest.mark.timeout(300)

PAGE = """<!doctype html><html><head><style>
body { font: 20px sans-serif; margin: 40px; background: #fff; }
#dob { font-size: 20px; padding: 6px; width: 220px; }
#picker { display: none; margin-top: 16px; padding: 16px; border: 1px solid #888;
          width: 320px; }
#month { font-size: 28px; font-weight: bold; }
</style></head><body>
<h1>Patient intake</h1>
<label for=dob>Date of birth</label><br>
<input id=dob placeholder="YYYY-MM-DD">
<div id=picker><div id=month></div></div>
<script>
const NAMES = ["January","February","March","April","May","June","July",
  "August","September","October","November","December"];
document.getElementById("dob").addEventListener("click", () => {
  const now = new Date();
  document.getElementById("month").textContent =
    NAMES[now.getMonth()] + " " + now.getFullYear();
  document.getElementById("picker").style.display = "block";
});
</script></body></html>"""


def _later(start: datetime, months: int) -> datetime:
    year, month = divmod(start.month - 1 + months, 12)
    return start.replace(year=start.year + year, month=month + 1, day=15, hour=12)


@pytest.fixture(scope="module")
def app_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    page = tmp_path_factory.mktemp("app") / "intake.html"
    page.write_text(PAGE, encoding="utf-8")
    return page.as_uri()


@pytest.fixture(scope="module")
def bundle(app_url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Record today (the recorder's clock is the real one) and compile."""
    backend, close = PlaywrightBackend.launch(app_url)
    try:
        page = backend.page
        box = page.locator("#dob").bounding_box()
        assert box is not None
        recorder = Recorder(backend, tmp_path_factory.mktemp("rec"), app_url=app_url)
        recorder.click(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        page.locator("#month").wait_for(state="visible")
        recording = recorder.finish()
    finally:
        close()
    out = tmp_path_factory.mktemp("bundle")
    compile_recording(recording, out, name="intake-date-picker")
    return out


def _clock_check(workflow: Workflow):
    return [
        pc
        for step in workflow.steps
        for pc in step.expect
        if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text_clock
    ]


def _replay(bundle_dir: Path, url: str, run_dir: Path, *, page_at, run_at):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800}, device_scale_factor=1
            )
            page.clock.set_fixed_time(page_at)
            page.goto(url)
            replayer = Replayer(PlaywrightBackend(page), use_structural=False)
            replayer.clock = lambda: run_at
            return replayer.run(
                Workflow.load(bundle_dir),
                params={},
                bundle_dir=bundle_dir,
                run_dir=run_dir,
            )
        finally:
            browser.close()


def test_the_picker_month_compiles_as_a_clock_check(bundle: Path) -> None:
    checks = _clock_check(Workflow.load(bundle))
    assert [pc.text_clock for pc in checks] == ["{Month} {YYYY}"], checks
    today = datetime.now(timezone.utc).astimezone()
    assert checks[0].text and str(today.year) in checks[0].text


@pytest.mark.parametrize("months", [1, 13 - datetime.now().month])
def test_same_recording_passes_in_another_month(
    bundle: Path, app_url: str, tmp_path: Path, months: int
) -> None:
    """Next month, and January of next year (the Dec to Jan rollover)."""
    at = _later(datetime.now(timezone.utc), months)
    report = _replay(bundle, app_url, tmp_path, page_at=at, run_at=at)
    assert report.success, [(r.step_id, r.error) for r in report.results]


def test_a_wrong_month_on_screen_still_halts(
    bundle: Path, app_url: str, tmp_path: Path
) -> None:
    run_at = _later(datetime.now(timezone.utc), 2)
    page_at = run_at - timedelta(days=31)  # the page shows the previous month
    report = _replay(bundle, app_url, tmp_path, page_at=page_at, run_at=run_at)
    assert not report.success
    failed = [r for r in report.results if not r.ok]
    assert failed and "from the run's clock" in (failed[-1].error or "")


def test_the_recorded_literal_halts_in_another_month(
    bundle: Path, app_url: str, tmp_path: Path
) -> None:
    """The bug: without the clock template the check names the recorded month."""
    literal = tmp_path / "literal"
    workflow = Workflow.load(bundle)
    for step in workflow.steps:
        step.expect = [pc.model_copy(update={"text_clock": None}) for pc in step.expect]
    import shutil

    shutil.copytree(bundle, literal)
    workflow.save(literal)
    at = _later(datetime.now(timezone.utc), 1)
    report = _replay(literal, app_url, tmp_path / "run", page_at=at, run_at=at)
    assert not report.success
