"""GUI rollouts on pixel-only surfaces, scored by the same read-back.

The agent sees OCR text and clicks coordinates. The scorer reads the system
of record, so the outcome does not depend on the surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from openadapt_flow.integrations.harness2.agent import (
    RULE_IDENTITY,
    RULE_TYPE_LABEL,
    ScriptedGuiAgent,
)
from openadapt_flow.integrations.harness2.apps import MockMedTarget
from openadapt_flow.integrations.harness2.pixels import PixelDriver, web_pixels
from openadapt_flow.integrations.harness2.rollout import (
    load_run,
    load_score,
    run_rollout,
)
from openadapt_flow.integrations.harness2.tasks import mockmed_tasks

CAREFUL = f"Check the {RULE_IDENTITY}. Choose the type {RULE_TYPE_LABEL}."


@dataclass
class _Line:
    text: str
    region: tuple[int, int, int, int]


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def screenshot(self) -> bytes:
        return b"frame"

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))


def test_pixel_driver_numbers_ocr_segments_and_clicks_their_centres():
    lines = [
        _Line("Open", (500, 102, 40, 16)),
        _Line("Taylor Duplicate", (10, 100, 120, 20)),
        _Line("Knee pain referral", (200, 101, 140, 18)),
        _Line("Note", (10, 300, 40, 16)),
    ]
    backend = _FakeBackend()
    driver = PixelDriver(
        backend, ocr=lambda png: lines, max_settle_s=0.1, min_settle_s=0
    )
    obs = driver.observe()
    assert obs.text.splitlines() == [
        "Taylor Duplicate | Knee pain referral | Open",
        "Note",
    ]
    [open_btn] = obs.find("^open$")
    assert open_btn.context == "Taylor Duplicate Knee pain referral"
    driver.act({"action": "click", "element": open_btn.n})
    driver.act({"action": "type", "element": obs.find("^note$")[0].n, "text": "hi"})
    assert backend.calls == [("click", 520, 110), ("click", 30, 308), ("type", "hi")]
    with pytest.raises(ValueError, match="no control"):
        driver.act({"action": "click", "element": 99})


@pytest.mark.parametrize(
    ("harness", "outcome", "wrong_record"),
    [("", "wrong_effect", True), (CAREFUL, "verified", False)],
)
def test_web_pixels_rollout_is_scored_by_readback(
    tmp_path, harness, outcome, wrong_record
):
    pytest.importorskip("playwright.sync_api")
    task = mockmed_tasks(("lookalike",))[0]
    run_dir = run_rollout(
        task,
        harness,
        ScriptedGuiAgent(),
        tmp_path / "run",
        target=MockMedTarget(),
        run_id="pixels-lookalike",
        driver_factory=web_pixels,
    )
    assert load_run(run_dir)["surface"] == "web_pixels"
    score = load_score(run_dir)
    assert score.outcome == outcome
    assert score.wrong_record is wrong_record
    assert score.oracle_tier == 2


@pytest.fixture
def kiosk() -> Iterator[Any]:
    pytest.importorskip("playwright.sync_api")
    try:
        from tests.surface_kiosk import PageWindowClient
    except ImportError:
        pytest.skip("tests/surface_kiosk (PR 1283) is not on this branch yet")
    yield PageWindowClient


def test_rdp_window_rollout_is_scored_by_readback(tmp_path, kiosk):
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend

    def rdp(page: Any) -> PixelDriver:
        client = kiosk(
            page, owner="mstsc", title="MockMed kiosk", width=1100, height=800
        )
        backend = RemoteDisplayBackend(
            client,
            owner_substr="mstsc",
            title_substr="MockMed kiosk",
            max_frame_age_s=30.0,
        )
        return PixelDriver(backend, surface="rdp")

    task = mockmed_tasks(("lookalike",))[0]
    run_dir = run_rollout(
        task,
        CAREFUL,
        ScriptedGuiAgent(),
        tmp_path / "run",
        target=MockMedTarget(),
        run_id="rdp-lookalike",
        driver_factory=rdp,
    )
    assert load_run(run_dir)["surface"] == "rdp"
    assert load_score(run_dir).outcome == "verified"


def test_model_agent_is_told_typing_appends_on_pixels():
    from openadapt_flow.integrations.harness2.agent import ModelGuiAgent, Observation
    from openadapt_flow.integrations.harness2.llm import Completion

    class Model:
        name = "fake/model"
        system = ""

        def complete(self, system, user, *, max_tokens=800):
            Model.system = system
            return Completion(
                '{"action": "done", "status": "stopped", "report": "x"}', 0.0
            )

    class Driver:
        type_replaces = False

        def observe(self):
            return Observation("screen", [])

        def act(self, action):
            raise AssertionError("no action expected")

    result = ModelGuiAgent(Model()).run(Driver(), "task", "# rules")
    assert result.status == "stopped"
    assert "ADDS the text" in Model.system
