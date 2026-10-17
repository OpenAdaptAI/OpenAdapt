"""Behavior contracts for the optional guided local tutorial."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import interactive_recorder, recorder, tutorial
from openadapt_flow.__main__ import _cmd_tutorial, build_parser
from openadapt_flow.compiler import compile_recording
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.tutorial import TutorialResult


def test_fast_tutorial_remains_the_cli_default() -> None:
    args = build_parser().parse_args(["tutorial"])

    assert args.guided is False
    assert args.interactive_record is False
    assert args.presentation_delay is None


def test_guided_cli_composes_human_recording_and_paced_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(work_dir: Path, **kwargs: Any) -> TutorialResult:
        observed.update(work_dir=work_dir, **kwargs)
        return TutorialResult(
            recording_dir=work_dir / "recording",
            bundle_dir=work_dir / "bundle",
            run_dir=work_dir / "run",
            execution_outcome="VERIFIED",
            transaction_outcome="VERIFIED",
            execution_profile="standard",
            transaction_billable=True,
            model_calls=0,
            effects_required=2,
            effects_confirmed=2,
            effect_tier=1,
            bundle_digest="a" * 64,
            system_of_record_records=1,
        )

    monkeypatch.setattr(tutorial, "run_tutorial", fake_run)
    args = Namespace(
        out=str(tmp_path / "tutorial"),
        headed=False,
        guided=True,
        interactive_record=False,
        presentation_delay=None,
        name=None,
        no_receipt=True,
        break_it=False,
    )

    assert _cmd_tutorial(args) == 0
    assert observed["interactive_record"] is True
    assert observed["headed"] is True
    assert observed["presentation_delay_s"] == tutorial.GUIDED_PRESENTATION_DELAY_S


def test_presentation_delay_is_bounded_and_injected() -> None:
    waits: list[float] = []

    tutorial._presentation_pause(0.75, sleep=waits.append)
    tutorial._presentation_pause(0.0, sleep=waits.append)

    assert waits == [0.75]
    with pytest.raises(tutorial.TutorialError):
        tutorial._presentation_pause(tutorial.MAX_PRESENTATION_DELAY_S + 0.1)


def test_paced_replayer_pauses_once_before_each_logical_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeReplayer:
        def _run_step(self, step: object) -> object:
            events.append(("run", step))
            return step

    monkeypatch.setattr(
        tutorial,
        "_presentation_pause",
        lambda delay: events.append(("pause", delay)),
    )
    paced_type = tutorial._presentation_replayer_type(FakeReplayer, 1.25)
    step = object()

    assert paced_type()._run_step(step) is step
    assert events == [("pause", 1.25), ("run", step)]
    assert tutorial._presentation_replayer_type(FakeReplayer, 0.0) is FakeReplayer


def test_scripted_presentation_pauses_before_each_recorded_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakePage:
        def wait_for_selector(self, *args: Any, **kwargs: Any) -> None:
            return None

        def wait_for_timeout(self, milliseconds: int) -> None:
            events.append(("settle", milliseconds))

    class FakeBackend:
        page = FakePage()

    class FakeRecorder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def click(self, *point: int) -> None:
            events.append(("click", point))

        def type_text(self, text: str, *, param: str) -> None:
            events.append(("type", (text, param)))

        def finish(self) -> Path:
            return tmp_path

    monkeypatch.setattr(tutorial, "_http_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(tutorial, "_center", lambda page, selector: (1, 2))
    monkeypatch.setattr(
        tutorial,
        "_presentation_pause",
        lambda delay: events.append(("pause", delay)),
    )
    monkeypatch.setattr(recorder, "Recorder", FakeRecorder)

    # record_tutorial imports PlaywrightBackend locally, so patch the source
    # module rather than adding a second test-only seam to the product.
    from openadapt_flow.backends import playwright_backend

    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend,
        "launch",
        lambda *args, **kwargs: (FakeBackend(), lambda: None),
    )

    tutorial.record_tutorial(
        "http://127.0.0.1:1",
        tmp_path,
        headed=True,
        presentation_delay_s=0.5,
    )

    actions = [event for event in events if event[0] in {"click", "type"}]
    pauses = [event for event in events if event[0] == "pause"]
    assert len(actions) == 6
    assert pauses == [("pause", 0.5)] * len(actions)


@pytest.mark.timeout(600)
def test_human_recorder_output_can_pass_the_tutorial_effect_contract(
    tmp_path: Path,
) -> None:
    """The human recorder supplies the evidence used by Standard admission."""

    base_url, _db, stop = serve()
    root_url = base_url.rstrip("/")
    tutorial._http_json(f"{root_url}/api/reset", method="POST", body={})

    def drive(page, pump) -> None:
        def click(selector: str) -> None:
            page.wait_for_selector(selector, state="visible", timeout=20000)
            page.click(selector)
            pump()
            pump()

        click(".open-btn")
        click("#new-encounter")
        click("#type-triage")
        click("#note")
        page.keyboard.type("A note demonstrated by the operator")
        pump()
        pump()
        click("#save-encounter")
        page.wait_for_selector("#saved-banner", state="visible", timeout=20000)
        pump()
        pump()

    try:
        entry_url = f"{root_url}/{tutorial.TUTORIAL_ENTRY_QUERY}"
        recording_dir = interactive_recorder.record_interactive(
            entry_url,
            tmp_path / "recording",
            param_fields=("note",),
            headless=True,
            script=drive,
            system_of_record_reader=lambda: tutorial._records(base_url),
            stop_when=lambda: bool(tutorial._records(base_url)),
        )
        events = [
            json.loads(line)
            for line in (recording_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert events[-1]["kind"] == "click"
        assert events[-1]["sor_before"] == []
        assert len(events[-1]["sor_after"]) == 1

        bundle_dir = tmp_path / "bundle"
        workflow = compile_recording(
            recording_dir,
            bundle_dir,
            name="human-guided-tutorial",
            mine_effects=True,
        )

        assert tutorial.consequential_step(workflow).effects
        assert tutorial.certify_tutorial(workflow).passed is True
        report = tutorial.run_tutorial_workflow(
            base_url=base_url,
            workflow=workflow,
            bundle_dir=bundle_dir,
            run_dir=tmp_path / "run",
        )
        assert report.execution_outcome == "VERIFIED"
        assert report.model_calls == 0
        assert report.outcome_envelope.required_contracts.effect == 2
        assert report.outcome_envelope.passed_contracts.effect == 2
    finally:
        stop()
