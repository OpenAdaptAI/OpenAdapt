"""The Claude computer-use readiness candidate, against a stub upstream only.

``FakeProvider`` (loopback) answers the Anthropic Messages API; a fake
screen stands in for the guarded tab. No model is called and no key is real.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.flight.fake_provider import FakeProvider, anthropic_click

pytest.importorskip("anthropic")
AGENT = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "readiness"
    / "claude_cu_agent.py"
)


def _agent() -> Any:
    spec = importlib.util.spec_from_file_location("readiness_claude_cu_agent", AGENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeScreen:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n fake"

    def click(self, x: float, y: float, **_kw: Any) -> None:
        self.clicks.append((x, y))

    def move(self, x: float, y: float) -> None: ...
    def drag(self, path: Any) -> None: ...
    def type(self, text: str) -> None: ...
    def key(self, keys: Any) -> None: ...
    def scroll(self, *a: Any) -> None: ...
    def wait(self, seconds: float = 1.0) -> None: ...


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch):
    fake = FakeProvider(model="claude-sonnet-5-5").start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    yield fake
    fake.stop()


def test_the_loop_acts_and_reports_usage(upstream: FakeProvider) -> None:
    upstream.anthropic_turns = [
        [anthropic_click(640, 400)],
        [{"type": "text", "text": "RESULT: DONE"}],
    ]
    screen = FakeScreen()
    usage = _agent().run_task("Add the note and save.", screen)
    assert screen.clicks == [(640, 400)]
    assert usage["calls"] == 2 and usage["cost_usd"] > 0
    assert usage["schema"] == "openadapt.agent-usage/v1"
    assert usage["final_line"] == "RESULT: DONE"
    first = upstream.requests[0]["body"].decode()
    assert "Medplum" not in first and "Add the note and save." in first


def test_usage_json_is_written_after_every_call(
    upstream: FakeProvider, tmp_path: Path
) -> None:
    import json

    upstream.anthropic_turns = [[anthropic_click(10, 10, f"t{i}")] for i in range(5)]
    agent = _agent()
    path = tmp_path / "usage.json"
    seen: list[int] = []

    class Watching(FakeScreen):
        def click(self, x: float, y: float, **kw: Any) -> None:
            seen.append(json.loads(path.read_text())["calls"])

    agent.run_task(
        "x",
        Watching(),
        limits=agent.caps({"OPENADAPT_CU_MAX_STEPS": "3"}),
        usage_path=path,
    )
    assert seen == [1, 2, 3]  # on disk before each action, so a stop loses nothing
    assert json.loads(path.read_text())["calls"] == 3


def test_the_step_cap_holds(upstream: FakeProvider) -> None:
    upstream.anthropic_turns = [[anthropic_click(10, 10, f"t{i}")] for i in range(40)]
    agent = _agent()
    limits = agent.caps({"OPENADAPT_CU_MAX_STEPS": "3"})
    usage = agent.run_task("x", FakeScreen(), limits=limits)
    assert usage["calls"] == 3 and usage["stopped"].startswith("step cap")
    assert len(upstream.requests) == 3


def test_the_spend_cap_holds(upstream: FakeProvider) -> None:
    upstream.anthropic_turns = [[anthropic_click(10, 10, f"t{i}")] for i in range(40)]
    agent = _agent()
    usage = agent.run_task(
        "x", FakeScreen(), limits=agent.caps({"OPENADAPT_CU_MAX_USD": "0.0001"})
    )
    assert usage["calls"] == 1 and usage["stopped"].startswith("spend cap")


def test_caps_never_pass_the_maximums() -> None:
    agent = _agent()
    assert agent.caps({}) == {"steps": 25, "usd": 1.0, "seconds": 600}
    assert agent.caps(
        {
            "OPENADAPT_CU_MAX_STEPS": "500",
            "OPENADAPT_CU_MAX_USD": "90",
            "OPENADAPT_CU_MAX_S": "1e9",
        }
    ) == {"steps": 50, "usd": 5.0, "seconds": 1800}


def test_an_unpriced_model_is_refused() -> None:
    with pytest.raises(SystemExit, match="no price"):
        _agent().run_task("x", FakeScreen(), model="claude-unknown")


def test_the_shared_leaderboard_ledger_is_not_touched(
    upstream: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.fhir_medplum.leaderboard import common

    monkeypatch.setattr(common, "LEDGER", tmp_path / "spend.json")
    upstream.anthropic_turns = [[{"type": "text", "text": "RESULT: NOT DONE stub"}]]
    _agent().run_task("x", FakeScreen())
    assert not (tmp_path / "spend.json").exists()


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_the_loop_works_with_run_tokens_through_the_flight_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flight proxy (real scrubber, pseudonyms on) between the loop and a
    stub upstream: the upstream sees ``[PERSON_1]`` for the name in the task,
    answers with a ``type`` of ``[PERSON_1]``, and the loop types the real
    name, because the proxy de-tokenizes tool inputs on the way back."""

    from openadapt_flow.flight.egress import ScrubbedEgress
    from openadapt_flow.flight.scrub import ScrubberUnavailable, require_scrubber

    try:
        scrubber = require_scrubber()
    except ScrubberUnavailable as exc:
        pytest.skip(f"the scrubber can't run here: {exc}")
    fake = FakeProvider(model="claude-sonnet-5-5").start()
    typed: list[str] = []

    class TypingScreen(FakeScreen):
        def screenshot(self) -> bytes:
            return _png()

        def type(self, text: str) -> None:
            typed.append(text)

    fake.anthropic_turns = [
        [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "computer",
                "input": {"action": "type", "text": "[PERSON_1]"},
            }
        ],
        [{"type": "text", "text": "RESULT: DONE"}],
    ]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    run_dir = tmp_path / "task-0001"
    try:
        with ScrubbedEgress(
            run_dir,
            spend_cap_usd=1.0,
            allow_unenforced=True,
            use_block=False,
            upstreams={"anthropic": fake.url},
            scrubber=scrubber,
        ) as egress:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", egress.env["ANTHROPIC_BASE_URL"])
            usage = _agent().run_task(
                "Open the chart of Jane Sample and type the patient's name in the note.",
                TypingScreen(),
                usage_path=run_dir / "usage.json",
            )
    finally:
        fake.stop()
    sent = fake.received_bytes()
    assert b"[PERSON_1]" in sent and b"Jane Sample" not in sent and b"Jane" not in sent
    assert typed == ["Jane Sample"], typed
    assert usage["calls"] == 2 and usage["final_line"] == "RESULT: DONE"
    assert (run_dir / "egress" / "pseudonyms.json").is_file()  # local only
