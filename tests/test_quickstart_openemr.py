"""`openadapt quickstart --app openemr`: preconditions, plain output, the real run.

The story runs against a fake engine with OpenEMR's words, so the output tests
take milliseconds. The real run needs Docker and a clone of the repository; it
is opt-in (``OPENADAPT_REAL_QUICKSTART_OPENEMR=1``) and skipped in CI.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.quickstart import next_steps, run_quickstart
from openadapt.quickstart.engine import BreakFacts, DemoStep, RunFacts, SealFacts
from openadapt.quickstart.openemr import (
    DOCKER_INSTALL_URL,
    checkout_problem,
    demo_step_label,
    docker_problem,
    find_flow_checkout,
)
from openadapt.quickstart.scene import OPENEMR, SEALS_URL
from openadapt.quickstart.story import NextSteps, Outcome, run_story
from openadapt.quickstart.view import PlainView

SNAPSHOT = Path(__file__).parent / "snapshots" / "quickstart_openemr_plain.snapshot"
REPO = Path(__file__).resolve().parents[1]
FLOW = REPO / "packages" / "flow"
DEMONSTRATION = (
    FLOW / "benchmark" / "openemr_local" / "referral_program" / "demonstration.jsonl"
)

NAMES = (
    "Ines Carvalho",
    "Theo Lindqvist",
    "Grace Adeyemi",
    "Arlo Whitcombe",
    "Priya Raman",
)
DEMO = (
    ('Title: "Ms."', 2.1),
    ('First name: "OpenAdapt"', 5.2),
    ("Click Create New Patient", 34.5),
    ("Click Confirm Create New Patient", 36.8),
)


# ---------------------------------------------------------------------------
# Preconditions: Docker and the clone
# ---------------------------------------------------------------------------


class _Done:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _runner(codes: dict):
    def run(argv, **_kwargs):
        code = codes.get(argv[1], 0)
        if code == "hang":
            raise subprocess.TimeoutExpired(argv, 30)
        return _Done(code)

    return run


def test_no_docker_says_how_to_install_it():
    problem = docker_problem(which=lambda name: None, run=_runner({}))
    assert problem is not None
    assert "needs Docker, which isn't installed" in problem
    assert DOCKER_INSTALL_URL in problem
    assert "\n" not in problem


def test_docker_not_running_says_to_start_it():
    problem = docker_problem(
        which=lambda name: "/usr/bin/docker", run=_runner({"info": 1})
    )
    assert problem is not None and "installed but not running" in problem
    assert "\n" not in problem


def test_docker_that_hangs_counts_as_not_running():
    problem = docker_problem(
        which=lambda name: "/usr/bin/docker",
        run=_runner({"info": "hang"}),
    )
    assert problem is not None and "not running" in problem


def test_docker_without_compose_says_so():
    problem = docker_problem(
        which=lambda name: "/usr/bin/docker", run=_runner({"compose": 1})
    )
    assert problem is not None and "docker compose" in problem


def test_a_working_docker_is_no_problem():
    assert docker_problem(which=lambda name: "/usr/bin/docker", run=_runner({})) is None


def test_the_fixture_is_found_only_in_a_clone(tmp_path):
    assert find_flow_checkout([tmp_path]) is None
    assert find_flow_checkout([tmp_path, FLOW]) == FLOW
    assert checkout_problem(None) is not None and "git clone" in checkout_problem(None)
    assert checkout_problem(FLOW) is None


def test_the_driver_loads_from_the_clones_file_without_shadowing():
    import sys

    from openadapt.quickstart.openemr import _driver

    driver = _driver(FLOW)
    assert (
        Path(driver.__file__).resolve()
        == (FLOW / "benchmark" / "openemr_local" / "quickstart.py").resolve()
    )
    assert sys.path.index(str(FLOW)) > 0, "the clone never goes first on sys.path"
    assert _driver(FLOW) is driver


def test_a_driver_from_another_copy_is_refused(tmp_path, monkeypatch):
    import sys
    import types

    from openadapt.quickstart import openemr

    other = tmp_path / "benchmark" / "openemr_local"
    other.mkdir(parents=True)
    fake = types.ModuleType("benchmark.openemr_local")
    fake.__path__ = [str(other)]
    monkeypatch.setitem(sys.modules, "benchmark.openemr_local", fake)
    monkeypatch.delitem(sys.modules, openemr.DRIVER, raising=False)
    with pytest.raises(RuntimeError, match="refusing to run another copy"):
        openemr._driver(FLOW)


def _no_mockmed(monkeypatch):
    """Fail the test if anything reaches MockMed (no silent fallback)."""

    def refuse(*args, **kwargs):
        raise AssertionError("fell back to MockMed")

    monkeypatch.setattr("openadapt.quickstart.engine.FlowEngine", refuse)
    monkeypatch.setattr("openadapt.quickstart.run_quickstart", refuse)
    monkeypatch.setattr(cli, "_engine_tutorial", refuse)


def test_cli_without_docker_prints_one_line_and_exits_2(monkeypatch, tmp_path):
    _no_mockmed(monkeypatch)
    line = "OpenEMR mode needs Docker, which isn't installed. Install it."
    monkeypatch.setattr("openadapt.quickstart.openemr.docker_problem", lambda: line)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["quickstart", "--app", "openemr"])
    assert result.exit_code == 2
    assert result.stderr.strip() == line
    assert result.stdout == ""
    assert not any(tmp_path.iterdir()), "nothing is written when Docker is missing"


def test_cli_outside_a_clone_prints_one_line_and_exits_2(monkeypatch, tmp_path):
    _no_mockmed(monkeypatch)
    monkeypatch.setattr("openadapt.quickstart.openemr.docker_problem", lambda: None)
    monkeypatch.setattr("openadapt.quickstart.openemr.find_flow_checkout", lambda: None)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["quickstart", "--app", "openemr"])
    assert result.exit_code == 2
    assert result.stderr.count("\n") == 1 and "git clone" in result.stderr
    assert not any(tmp_path.iterdir())


def test_cli_refuses_engine_tutorial_flags_with_openemr(monkeypatch, tmp_path):
    _no_mockmed(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli.main, ["quickstart", "--app", "openemr", "--guided"]
    )
    assert result.exit_code == 2
    assert "--guided" in result.stderr


def test_cli_passes_the_clone_to_the_quickstart(monkeypatch, tmp_path):
    seen = {}

    def fake(root, **kwargs):
        seen.update(kwargs, root=root)
        return Outcome(root=root)

    monkeypatch.setattr("openadapt.quickstart.openemr.docker_problem", lambda: None)
    monkeypatch.setattr("openadapt.quickstart.openemr.find_flow_checkout", lambda: FLOW)
    monkeypatch.setattr("openadapt.quickstart.run_quickstart", fake)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["quickstart", "--app", "openemr"])
    assert result.exit_code == 0, result.output
    assert seen["openemr"] == FLOW


def test_the_default_app_is_still_mockmed(monkeypatch, tmp_path):
    seen = {}

    def fake(root, **kwargs):
        seen.update(kwargs)
        return Outcome(root=root)

    monkeypatch.setattr(
        "openadapt.quickstart.openemr.docker_problem",
        lambda: pytest.fail("MockMed must not check for Docker"),
    )
    monkeypatch.setattr(cli, "_flow_runs_quickstart", lambda: True)
    monkeypatch.setattr("openadapt.quickstart.run_quickstart", fake)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["quickstart"])
    assert result.exit_code == 0, result.output
    assert seen["openemr"] is None


# ---------------------------------------------------------------------------
# Act 1's labels, from the committed demonstration
# ---------------------------------------------------------------------------


def test_demonstration_labels_show_each_field_and_the_two_saves():
    events = [json.loads(line) for line in DEMONSTRATION.read_text().splitlines()]
    labels = [label for label in map(demo_step_label, events) if label]
    assert len(events) == 41
    assert labels[0] == 'Title: "Ms."'
    assert labels[-2:] == [
        "Click Create New Patient",
        "Click Confirm Create New Patient",
    ]
    assert "Open the Contact section" in labels
    assert sum(1 for label in labels if label.endswith('"')) == 12


def test_demonstration_holds_no_urls_or_tokens():
    text = DEMONSTRATION.read_text()
    assert "http" not in text and "token" not in text


# ---------------------------------------------------------------------------
# Plain output
# ---------------------------------------------------------------------------


def _run(root: Path, index: int, **overrides) -> RunFacts:
    facts = RunFacts(
        index=index,
        note=NAMES[index - 1],
        run_dir=root / f"run-{index}",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        checked=True,
        records=1,
        model_calls=0,
        cost_usd=0.0,
        effects_required=11,
        effects_confirmed=11,
        seconds=30.0 + index / 10,
        plain_label="Done and checked",
    )
    return replace(facts, **overrides)


def _broken(root: Path, **overrides) -> BreakFacts:
    facts = BreakFacts(
        run_dir=root / "run-broken",
        fault="possible duplicate",
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        screen_showed_success=False,
        screen_text="a matching chart: Delgado, Rosa, born 1958-03-27",
        screenshot=None,
        new_records=0,
        effects_required=11,
        effects_refuted=0,
        halt_reason="Postconditions failed for step 'step_039'",
        model_calls=0,
        seconds=24.5,
    )
    return replace(facts, **overrides)


class FakeOpenEMR:
    """OpenEMR's words and shape, with the facts a test gives it."""

    scene = OPENEMR
    inputs = ("r3", "r4", "r5", "r7", "r8")
    setup_summary = (
        "OpenEMR 8.0.0.3 at http://127.0.0.1:9741: reused the running copy 5.7s"
    )
    program_detail = "(certified)"

    def __init__(self, root: Path, *, runs=None, broken=None, parallel: int = 5):
        self.root = root
        self.visible = False
        self.recording_dir = root / "demonstration.jsonl"
        self._runs = runs or {}
        self._broken = broken or {}
        self._parallel = parallel
        self.progress: list[tuple[str, bool]] = []

    def set_progress(self, progress):
        self._report = progress

    def __enter__(self):
        self._report("restoring the synthetic baseline", True)
        self._report("restoring the synthetic baseline 3s", False)
        return self

    def __exit__(self, *exc):
        pass

    def record(self, on_step):
        steps = [DemoStep(label, at) for label, at in DEMO]
        for step in steps:
            on_step(step)
        return steps

    def compile(self):
        return 29

    @property
    def program_name(self):
        return "openemr-create-synthetic-patient"

    def note_param(self):
        return True

    def start_workers(self):
        pass

    @property
    def parallel(self):
        return self._parallel

    def run_all(self, keys, on_finish):
        for index, _key in enumerate(keys, start=1):
            on_finish(_run(self.root, index, **self._runs.get(index, {})))

    def before_prompt(self):
        pass

    def break_it(self):
        broken = dict(self._broken)
        self._screen = broken.pop(
            "screen_text", "a matching chart: Delgado, Rosa, born 1958-03-27"
        )
        return _broken(self.root, **broken)

    def read_screen(self, broken):
        return self._screen

    def seal(self, runs):
        return SealFacts(
            "seal-ed25519-0123456789abcdef", sum(1 for run in runs if run.checked)
        )

    def write_page(self, first, broken):
        return None


def _clock(step: float = 0.5):
    now = [0.0]

    def tick() -> float:
        now[0] += step
        return now[0]

    return tick


def _steps(outcome: Outcome) -> NextSteps:
    return next_steps(cli._hint, cli._shell_quote, False)(outcome)


def _plain(engine, **kwargs) -> tuple[str, Outcome]:
    stream = io.StringIO()
    outcome = run_story(
        engine,
        PlainView(stream, scene=OPENEMR),
        next_steps=_steps,
        clock=_clock(),
        **kwargs,
    )
    return stream.getvalue(), outcome


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_under_uvx", lambda: False)
    return tmp_path / "openadapt-quickstart"


def test_plain_output_matches_the_snapshot(root):
    output, _ = _plain(FakeOpenEMR(root))
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(output)
    assert output == SNAPSHOT.read_text()


def test_act_three_reads_as_a_stop_before_a_duplicate(root):
    output, outcome = _plain(FakeOpenEMR(root))
    assert "■ Stopped before saving: possible duplicate" in output
    assert "What OpenEMR's record showed:  0 new charts" in output
    assert "A screen-trusting bot" not in output, "nothing on screen claimed success"
    assert outcome.broken is not None and outcome.broken.stopped


def test_runs_are_checked_in_openemrs_record(root):
    output, outcome = _plain(
        FakeOpenEMR(
            root, runs={2: {"checked": False, "plain_label": "Stopped before saving"}}
        )
    )
    assert output.count("✓ checked in OpenEMR's record") == 4
    assert "■ Stopped before saving" in output
    assert "4 of 5 saved and checked in OpenEMR's record" in output
    assert outcome.checked == 4


def test_a_new_chart_in_act_three_is_never_called_a_clean_stop(root):
    output, _ = _plain(FakeOpenEMR(root, broken={"new_records": 1}))
    assert "1 new chart" in output and "1 new charts" not in output
    assert "Replaying those clicks" not in output
    assert "a new chart is in OpenEMR. Check it." in output


def test_the_finale_links_the_published_seals(root):
    output, _ = _plain(FakeOpenEMR(root))
    assert f"See real Seals from OpenEMR runs: {SEALS_URL}" in output
    assert "agent serve --bundles openadapt-quickstart/bundle" in output


def test_setup_progress_prints_milestones_only(root):
    output, _ = _plain(FakeOpenEMR(root))
    assert output.count("restoring the synthetic baseline") == 1
    assert "reused the running copy 5.7s" in output


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------


def _docker_ready() -> Optional[str]:
    return docker_problem()


@pytest.mark.real_quickstart
@pytest.mark.skipif(
    os.environ.get("OPENADAPT_REAL_QUICKSTART_OPENEMR") != "1",
    reason=(
        "set OPENADAPT_REAL_QUICKSTART_OPENEMR=1 on a machine with Docker to run "
        "OpenEMR (first run: image download and install, minutes)"
    ),
)
def test_real_openemr_quickstart(tmp_path, monkeypatch):
    problem = _docker_ready()
    if problem is not None:
        pytest.skip(problem)
    flow = find_flow_checkout()
    assert flow is not None, "run from a clone with packages/flow installed"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    root = tmp_path / "openadapt-quickstart"
    stream = io.StringIO()
    outcome = run_quickstart(
        root,
        visible=False,
        break_it=True,
        verbose=False,
        steps=_steps,
        stream=stream,
        interactive=False,
        openemr=flow,
    )
    output = stream.getvalue()
    assert outcome.checked == len(outcome.runs) == 5, output
    assert outcome.model_calls == 0
    assert outcome.broken is not None and outcome.broken.stopped, output
    assert outcome.broken.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert outcome.broken.new_records == 0
    assert "5 of 5 saved and checked in OpenEMR's record" in output
    for index in range(1, 6):
        report = json.loads((root / f"run-{index}" / "report.json").read_text())
        assert report["execution_outcome"] == "VERIFIED"
        assert report["execution_profile"] == "standard"
    readback = json.loads(
        (root / "run-broken" / "quickstart-readback.json").read_text()
    )
    assert readback["new_charts_in_database"] == 0
