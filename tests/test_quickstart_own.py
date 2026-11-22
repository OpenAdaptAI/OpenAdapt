"""`openadapt quickstart --own`: record, compile, two new runs, share, next steps.

The engine, the questions and sharing are fakes, so no browser, sample app
or network is used, except in the one test that records through the real
interactive recorder in a headless browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.quickstart import own
from openadapt.quickstart.own import OWN_NOTES, OwnTaskError, run_own


@dataclass
class Step:
    label: str


@dataclass
class Run:
    run_dir: Path
    note: str
    checked: bool = True
    plain_label: str = "Done and checked"
    effects_confirmed: int = 2
    effects_required: int = 2
    model_calls: int = 0


class FakeSample:
    def __init__(self, root: Path, *, visible: bool) -> None:
        self.root = root
        self.visible = visible
        self.calls: list[str] = []
        FakeSample.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("closed")

    bundle_dir = Path("/tmp/oa/bundle")
    app_url = "http://127.0.0.1:5000"

    def record_yourself(self):
        self.calls.append("record_yourself")

    def record_for_you(self, on_step):
        self.calls.append("record_for_you")
        on_step(Step("Click Save"))

    def compile(self):
        self.calls.append("compile")
        return 6

    def run(self, index, note):
        self.calls.append(f"run {index}")
        return Run(self.root / f"own-run-{index}", note)

    def seal(self, runs):
        self.calls.append("seal")


def _go(tmp_path, *, answers=(), can_record_here=False, share=None, url=None):
    lines: list[str] = []
    asked: list[str] = []
    queue = list(answers)

    def ask(prompt):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    result = run_own(
        tmp_path,
        url=url,
        can_record_here=can_record_here,
        ask=ask,
        say=lines.append,
        hint=lambda rest: f"openadapt {rest}",
        quote=str,
        sample_factory=FakeSample,
        share=share,
    )
    return result, "\n".join(lines), asked


def test_without_a_screen_openadapt_records_the_sample_and_asks_nothing(tmp_path):
    result, out, asked = _go(tmp_path)
    assert asked == []
    assert FakeSample.last.calls == [
        "record_for_you",
        "compile",
        "run 1",
        "run 2",
        "seal",
        "closed",
    ]
    assert FakeSample.last.visible is False
    assert "OpenAdapt does it once in a hidden browser" in out
    assert [r.note for r in result.runs] == list(OWN_NOTES)
    assert "6-step program" in out


def test_a_window_opens_only_after_a_yes_at_a_screen(tmp_path):
    _go(tmp_path, can_record_here=True, answers=["n"])
    assert "record_for_you" in FakeSample.last.calls
    _go(tmp_path, can_record_here=True, answers=[""])
    assert "record_for_you" in FakeSample.last.calls
    _, _, asked = _go(tmp_path, can_record_here=True, answers=["y"])
    assert FakeSample.last.calls[0] == "record_yourself"
    assert "browser window" in asked[0]


def test_the_ending_names_a_working_mcp_command_and_the_publish_guide(tmp_path):
    _, out, _ = _go(tmp_path)
    assert (
        "claude mcp add openadapt -- openadapt agent serve --bundles "
        "/tmp/oa/bundle --mode production --allow-run"
    ) in out
    assert own.WRAP_PUBLISH_GUIDE_URL in out
    assert "openadapt login" in out  # nothing was shared, so how to keep them


def test_sharing_is_offered_only_when_both_runs_checked(tmp_path, monkeypatch):
    shared: list = []

    def share(run_dirs):
        shared.append(run_dirs)
        return "https://cloud.test/seal#a,b", True

    result, out, _ = _go(tmp_path, share=share)
    assert shared == [[tmp_path / "own-run-1", tmp_path / "own-run-2"]]
    assert result.link == "https://cloud.test/seal#a,b" and result.claimed
    assert "Your runs: https://cloud.test/seal#a,b" in out

    shared.clear()
    monkeypatch.setattr(
        FakeSample,
        "run",
        lambda self, index, note: Run(
            self.root / f"own-run-{index}", note, checked=False
        ),
    )
    _go(tmp_path, share=share)
    assert shared == []


def test_own_app_needs_a_screen_and_says_so_in_one_line(tmp_path):
    with pytest.raises(OwnTaskError, match="no screen"):
        _go(tmp_path, url="https://app.example")


def test_own_app_ending_targets_that_app(tmp_path, monkeypatch):
    def fake_own_app(root, *, url, visible, ask, say):
        say(f"recorded {url}")
        return own.OwnResult(bundle_dir=root / "bundle")

    lines: list[str] = []
    run_own(
        tmp_path,
        url="https://app.example",
        can_record_here=True,
        ask=lambda _p: "y",
        say=lines.append,
        hint=lambda rest: f"openadapt {rest}",
        quote=str,
        own_app=fake_own_app,
    )
    out = "\n".join(lines)
    assert (
        f"--bundles {tmp_path / 'bundle'} --mode production --allow-run --url https://app.example"
        in out
    )
    assert "sample app" not in out


def test_own_app_recording_without_a_yes_opens_nothing(tmp_path, monkeypatch):
    opened: list = []
    monkeypatch.setattr(
        "openadapt_flow.interactive_recorder.record_interactive",
        lambda *a, **k: opened.append(a),
    )
    with pytest.raises(OwnTaskError, match="no window"):
        own.record_own_app(
            tmp_path,
            url="https://app.example",
            visible=False,
            ask=lambda _p: "",
            say=print,
        )
    assert opened == []


def test_cli_routes_own_and_refuses_url_without_it(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(
        cli, "_quickstart_own", lambda root, url, watch: seen.append((root, url, watch))
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["quickstart", "--own", "--out", str(tmp_path / "q")]
    )
    assert result.exit_code == 0, result.output
    assert seen == [(tmp_path / "q", None, False)]
    result = runner.invoke(cli.main, ["quickstart", "--url", "https://app.example"])
    assert result.exit_code == 2
    assert "--url works with --own" in result.output


def test_share_runs_never_raises_and_asks_before_any_upload(monkeypatch, tmp_path):
    from openadapt import seal_publish

    monkeypatch.setattr(seal_publish, "hosted_seals_live", lambda origin: True)
    sent: list = []
    monkeypatch.setattr(
        seal_publish,
        "publish_quickstart",
        lambda run_dirs, *, ask, say, command: (
            sent.append(ask("Publish? [y/N] ")) or None
        ),
    )
    lines: list[str] = []
    link, claimed = own.share_runs(
        [tmp_path], ask=lambda _p: "n", say=lines.append, command=lambda r: r
    )
    assert (link, claimed) == (None, False)
    assert sent == ["n"]

    def refuse(*_a, **_k):
        raise seal_publish.PublishError("Not published: the service is busy.")

    monkeypatch.setattr(seal_publish, "publish_quickstart", refuse)
    assert own.share_runs(
        [tmp_path], ask=lambda _p: "y", say=lines.append, command=str
    ) == (None, False)
    assert "Not published: the service is busy." in lines[-1]


@pytest.mark.timeout(600)
def test_own_app_records_through_the_real_recorder_and_compiles(tmp_path):
    """The --url path end to end in a headless browser: the real capture
    listeners watch a scripted user in the bundled sample app, then compile."""
    pytest.importorskip("playwright")
    from openadapt_flow.mockmed.server import serve

    def drive(page, pump):
        def click(selector):
            page.wait_for_selector(selector, state="visible", timeout=20000)
            page.click(selector)
            pump()
            pump()

        click("#username")
        page.keyboard.type("nurse.demo")
        pump()
        click("#password")
        page.keyboard.type("synthetic-password")
        pump()
        click("#signin")
        click(".open-btn")
        click("#new-encounter")
        click("#type-triage")
        click("#note")
        page.keyboard.type("Synthetic note for the own-task test")
        pump()
        pump()
        click("#save-encounter")
        for _ in range(4):
            pump()

    url, stop = serve(port=0)
    lines: list[str] = []
    try:
        result = own.record_own_app(
            tmp_path,
            url=url,
            visible=False,
            ask=lambda _p: pytest.fail("a scripted recording asks nothing"),
            say=lines.append,
            params=("note",),
            script=drive,
        )
    finally:
        stop()
    assert (result.bundle_dir / "workflow.json").is_file()
    assert any("step program" in line for line in lines)
    assert (
        "synthetic-password"
        not in (tmp_path / "recording" / "events.jsonl").read_text()
    )
