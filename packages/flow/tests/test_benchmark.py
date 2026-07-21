"""Unit tests for the benchmark package (agent loop, verify, orchestrator).

No network anywhere: the Anthropic client is faked with scripted responses,
the backend is faked, and orchestrator aggregation is tested on fabricated
rows. ``verify`` runs real OCR on synthetic cv2-rendered screenshots.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from openadapt_flow.benchmark import agent_baseline
from openadapt_flow.benchmark.agent_baseline import (
    AgentRunResult,
    _truncate_screenshots,
    compute_cost,
    load_api_key,
    run_agent,
    triage_task_prompt,
)
from openadapt_flow.benchmark.run_benchmark import (
    aggregate_results,
    render_markdown,
    write_outputs,
)
from openadapt_flow.benchmark.verify import verify_encounter_saved

NOTE = "Follow-up in 2 weeks; BP recheck."


# -- fakes ---------------------------------------------------------------


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


BLANK_PNG = to_png(np.full((800, 1280, 3), 245, dtype=np.uint8))


class FakeBackend:
    """Backend double that records actions and serves a canned screenshot."""

    def __init__(self, png: bytes = BLANK_PNG) -> None:
        self.png = png
        self.calls: list[tuple[Any, ...]] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        return self.png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.calls.append(("click", x, y, double))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press(self, key: str) -> None:
        self.calls.append(("press", key))


def tool_use(action: dict[str, Any], block_id: str = "tu_1") -> Any:
    return SimpleNamespace(type="tool_use", id=block_id, name="computer", input=action)


def text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def response(
    blocks: list[Any], stop_reason: str, in_tok: int = 100, out_tok: int = 50
) -> Any:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class FakeClient:
    """Anthropic client double with scripted ``beta.messages.create``.

    Serves responses from ``script`` in order; when the script is exhausted
    it keeps serving ``repeat`` (for budget-stop tests). Records every
    ``messages`` payload it was called with.
    """

    def __init__(self, script: list[Any], repeat: Any | None = None) -> None:
        self.script = list(script)
        self.repeat = repeat
        self.calls: list[list[dict[str, Any]]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs["messages"])
        self.kwargs.append(kwargs)
        if self.script:
            return self.script.pop(0)
        if self.repeat is not None:
            return self.repeat
        raise AssertionError("FakeClient script exhausted")


@pytest.fixture(autouse=True)
def fast_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip settle polling in the agent loop (unit tests need no settling)."""
    monkeypatch.setattr(
        agent_baseline, "_capture", lambda backend: backend.screenshot()
    )


# -- agent loop: action execution ------------------------------------------


class TestAgentActions:
    def test_click_type_key_executed_in_order(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [
                        tool_use(
                            {"action": "left_click", "coordinate": [100, 200]},
                            "tu_1",
                        ),
                        tool_use({"action": "type", "text": "hi"}, "tu_2"),
                        tool_use({"action": "key", "text": "Enter"}, "tu_3"),
                    ],
                    "tool_use",
                ),
                response([text_block("done")], "end_turn"),
            ]
        )
        result = run_agent(backend, "task", client=client)
        assert backend.calls == [
            ("click", 100, 200, False),
            ("type", "hi"),
            ("press", "Enter"),
        ]
        assert result.actions == 3
        assert result.api_calls == 2
        assert result.stopped == "model_done"
        assert result.model_stop_reason == "end_turn"
        assert result.final_screenshot == BLANK_PNG

    def test_double_click_and_screenshot(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response([tool_use({"action": "screenshot"}, "tu_1")], "tool_use"),
                response(
                    [
                        tool_use(
                            {
                                "action": "double_click",
                                "coordinate": [5, 6],
                            },
                            "tu_2",
                        )
                    ],
                    "tool_use",
                ),
                response([text_block("done")], "end_turn"),
            ]
        )
        result = run_agent(backend, "task", client=client)
        assert backend.calls == [("click", 5, 6, True)]
        assert result.actions == 2
        # Every executed action's tool_result carried a screenshot image.
        second_call = client.calls[1]
        tool_result = second_call[-1]["content"][0]
        kinds = [b["type"] for b in tool_result["content"]]
        assert "image" in kinds

    def test_unsupported_action_returns_is_error(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [
                        tool_use(
                            {
                                "action": "left_click_drag",
                                "start_coordinate": [0, 0],
                                "coordinate": [9, 9],
                            }
                        )
                    ],
                    "tool_use",
                ),
                response([text_block("ok")], "end_turn"),
            ]
        )
        run_agent(backend, "task", client=client)
        assert backend.calls == []  # nothing was executed on the backend
        sent = client.calls[1][-1]["content"][0]
        assert sent["is_error"] is True
        assert "not supported" in sent["content"]

    def test_backend_exception_reported_not_raised(self) -> None:
        backend = FakeBackend()

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("browser gone")

        backend.click = boom  # type: ignore[method-assign]
        client = FakeClient(
            [
                response(
                    [tool_use({"action": "left_click", "coordinate": [1, 2]})],
                    "tool_use",
                ),
                response([text_block("ok")], "end_turn"),
            ]
        )
        result = run_agent(backend, "task", client=client)
        sent = client.calls[1][-1]["content"][0]
        assert sent["is_error"] is True
        assert "browser gone" in sent["content"]
        assert result.actions == 1


# -- agent loop: budget + truncation ----------------------------------------


class TestAgentBudgetAndHistory:
    def test_budget_stop(self) -> None:
        backend = FakeBackend()
        looping = response([tool_use({"action": "screenshot"})], "tool_use")
        client = FakeClient([], repeat=looping)
        result = run_agent(backend, "task", client=client, max_actions=5)
        assert result.actions == 5
        assert result.stopped == "budget_exhausted"
        # One API call per single-action response; loop stops right after
        # the budget is consumed, without a further call.
        assert result.api_calls == 5

    def test_history_keeps_only_last_three_screenshots(self) -> None:
        backend = FakeBackend()
        looping = response([tool_use({"action": "screenshot"})], "tool_use")
        client = FakeClient([], repeat=looping)
        run_agent(backend, "task", client=client, max_actions=8)
        final_messages = client.calls[-1]
        images = 0
        stubs = 0
        for msg in final_messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                for item in block["content"]:
                    if item["type"] == "image":
                        images += 1
                    elif "screenshot removed" in item.get("text", ""):
                        stubs += 1
        assert images <= 3
        assert stubs >= 1

    def test_truncate_helper_replaces_oldest_first(self) -> None:
        def result_msg(i: int) -> dict[str, Any]:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"tu_{i}",
                        "content": [
                            {"type": "text", "text": f"ack {i}"},
                            {
                                "type": "image",
                                "source": {"data": f"img{i}"},
                            },
                        ],
                    }
                ],
            }

        messages = [
            {"role": "user", "content": "task"},
            *[result_msg(i) for i in range(5)],
        ]
        _truncate_screenshots(messages, keep=2)
        remaining = []
        for msg in messages[1:]:
            for block in msg["content"]:
                for item in block["content"]:
                    if item["type"] == "image":
                        remaining.append(item["source"]["data"])
        assert remaining == ["img3", "img4"]
        # Assistant messages and the task prompt are untouched.
        assert messages[0] == {"role": "user", "content": "task"}

    def test_truncate_keep_zero_removes_all(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu",
                        "content": [{"type": "image", "source": {}}],
                    }
                ],
            }
        ]
        _truncate_screenshots(messages, keep=0)
        assert messages[0]["content"][0]["content"][0]["type"] == "text"


# -- cost + config -----------------------------------------------------------


class TestCostAndConfig:
    def test_cost_math(self) -> None:
        assert compute_cost(1_000_000, 0) == pytest.approx(3.00)
        assert compute_cost(0, 1_000_000) == pytest.approx(15.00)
        assert compute_cost(500_000, 100_000) == pytest.approx(1.5 + 1.5)

    def test_usage_accumulated_across_calls(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [tool_use({"action": "screenshot"})],
                    "tool_use",
                    in_tok=1000,
                    out_tok=200,
                ),
                response([text_block("done")], "end_turn", in_tok=1500, out_tok=50),
            ]
        )
        result = run_agent(backend, "task", client=client)
        assert result.input_tokens == 2500
        assert result.output_tokens == 250
        assert result.cost_usd == pytest.approx(compute_cost(2500, 250))

    def test_tool_definition_and_beta_header(self) -> None:
        backend = FakeBackend()
        client = FakeClient([response([text_block("hi")], "end_turn")])
        run_agent(backend, "task", client=client)
        kwargs = client.kwargs[0]
        assert kwargs["betas"] == ["computer-use-2025-11-24"]
        (tool,) = kwargs["tools"]
        assert tool["type"] == "computer_20251124"
        assert tool["display_width_px"] == 1280
        assert tool["display_height_px"] == 800
        assert kwargs["model"] == agent_baseline.MODEL

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
        assert load_api_key() == "sk-test-env"

    def test_api_key_from_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        (tmp_path / ".anthropic").mkdir()
        (tmp_path / ".anthropic" / "api_key").write_text("sk-test-file\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert load_api_key() == "sk-test-file"

    def test_api_key_missing_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            load_api_key()

    def test_task_prompt_states_intent_not_coordinates(self) -> None:
        prompt = triage_task_prompt(NOTE)
        assert "nurse.demo" in prompt
        assert "mockmed-demo-pass" in prompt
        assert "Triage" in prompt
        assert NOTE in prompt
        assert "coordinate" not in prompt.lower()


# -- verify -------------------------------------------------------------------


def put_line(img: np.ndarray, text: str, y: int) -> None:
    cv2.putText(
        img,
        text,
        (40, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


class TestVerify:
    def make_screen(self, *lines: str) -> bytes:
        img = np.full((800, 1280, 3), 245, dtype=np.uint8)
        for i, line in enumerate(lines):
            put_line(img, line, 200 + i * 60)
        return to_png(img)

    def test_saved_state_passes(self) -> None:
        # cv2 Hershey fonts have no em dash; the fuzzy ratio absorbs the
        # hyphen difference, exactly as it absorbs OCR jitter.
        screen = self.make_screen(
            f"Encounter saved - {NOTE[:40]}",
            f"Triage - {NOTE[:60]}",
        )
        verdict = verify_encounter_saved(screen, NOTE)
        assert verdict.banner_found
        assert verdict.note_found
        assert verdict.success

    def test_split_banner_lines_pass(self) -> None:
        # OCR sometimes segments the banner into two lines (prefix + note);
        # the criterion must tolerate segmentation, not require one line.
        screen = self.make_screen(
            "Encounter saved -",
            NOTE[:40],
            f"Triage - {NOTE[:60]}",
        )
        verdict = verify_encounter_saved(screen, NOTE)
        assert verdict.success

    def test_note_in_form_without_banner_fails(self) -> None:
        # The typed note is visible on the encounter form BEFORE saving;
        # without the banner that must not count as success.
        screen = self.make_screen("Note", NOTE[:40], "Save Encounter")
        verdict = verify_encounter_saved(screen, NOTE)
        assert verdict.note_found
        assert not verdict.banner_found
        assert not verdict.success

    def test_blank_screen_fails(self) -> None:
        verdict = verify_encounter_saved(BLANK_PNG, NOTE)
        assert not verdict.success
        assert not verdict.banner_found
        assert not verdict.note_found

    def test_banner_without_encounter_row_fails(self) -> None:
        screen = self.make_screen(f"Encounter saved - {NOTE[:40]}")
        verdict = verify_encounter_saved(screen, NOTE)
        assert verdict.banner_found
        assert not verdict.note_found
        assert not verdict.success

    def test_wrong_note_fails(self) -> None:
        screen = self.make_screen(
            "Encounter saved - A completely different note text",
            "Triage - A completely different note text",
        )
        verdict = verify_encounter_saved(screen, NOTE)
        assert not verdict.success


# -- orchestrator aggregation --------------------------------------------------


def compiled_row(i: int, *, success: bool = True, wall: float = 5.0) -> dict:
    return {
        "arm": "compiled",
        "i": i,
        "wall_s": wall,
        "success": success,
        "banner_found": success,
        "note_found": success,
        "replayer_success": success,
        "heal_count": 0,
        "actions": 11,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }


def agent_row(
    i: int,
    *,
    success: bool = True,
    wall: float = 90.0,
    cost: float = 0.25,
) -> dict:
    return {
        "arm": "agent",
        "i": i,
        "wall_s": wall,
        "success": success,
        "banner_found": success,
        "note_found": success,
        "actions": 12,
        "api_calls": 13,
        "input_tokens": 60_000,
        "output_tokens": 3_000,
        "cost_usd": cost,
        "stopped": "model_done",
        "model_stop_reason": "end_turn",
        "error": None,
    }


class TestOrchestrator:
    def make_results(self) -> dict:
        compiled = [compiled_row(i, wall=4.0 + i * 0.1) for i in range(10)]
        agents = [agent_row(i, success=i != 0, wall=80.0 + i * 5) for i in range(4)]
        drift = {
            "compiled": compiled_row(0, wall=8.0) | {"heal_count": 8},
            "agent": agent_row(0, wall=95.0),
        }
        return aggregate_results(compiled, agents, drift, note_text=NOTE)

    def test_aggregates(self) -> None:
        results = self.make_results()
        c = results["arms"]["compiled"]
        a = results["arms"]["agent"]
        assert c["n"] == 10
        assert c["success_rate"] == 1.0
        assert c["wall_s_p50"] == pytest.approx(4.45)
        assert c["cost_usd_total"] == 0.0
        assert a["n"] == 4
        assert a["success_count"] == 3
        assert a["success_rate"] == pytest.approx(0.75)
        assert a["cost_usd_per_run"] == pytest.approx(0.25)
        assert a["cost_usd_total"] == pytest.approx(1.0)
        assert a["input_tokens_total"] == 240_000
        assert results["model"] == agent_baseline.MODEL
        assert len(results["runs"]["compiled"]) == 10
        assert len(results["runs"]["agent"]) == 4

    def test_write_outputs(self, tmp_path: Path) -> None:
        import json

        results = self.make_results()
        write_outputs(results, tmp_path)
        assert (tmp_path / "results.json").is_file()
        loaded = json.loads((tmp_path / "results.json").read_text())
        assert loaded["arms"]["agent"]["success_count"] == 3
        assert (tmp_path / "latency_cost.png").stat().st_size > 1000
        md = (tmp_path / "BENCHMARK.md").read_text()
        assert "Methodology" in md
        assert "Caveats" in md
        assert "drift" in md.lower()
        assert "latency_cost.png" in md

    def test_write_outputs_survives_font_lookup_failure(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A corrupt font cache must skip the chart, not fail the benchmark.

        Simulates the ``ValueError: Failed to find font DejaVu Sans`` that
        fresh venvs / concurrent runs raise from ``findfont``. The cosmetic
        PNG may be skipped, but the numeric ``results.json`` -- the product of
        the benchmark -- must be written intact and the call must not raise.
        """
        import json

        from matplotlib import font_manager

        def boom(*args: Any, **kwargs: Any) -> str:
            raise ValueError("Failed to find font DejaVu Sans")

        monkeypatch.setattr(font_manager.fontManager, "findfont", boom)
        monkeypatch.setattr(font_manager, "findfont", boom)

        results = self.make_results()
        write_outputs(results, tmp_path)  # must not raise

        loaded = json.loads((tmp_path / "results.json").read_text())
        assert loaded["arms"]["agent"]["success_count"] == 3
        assert loaded["arms"]["compiled"]["success_rate"] == 1.0
        # BENCHMARK.md (also non-chart) is still written intact.
        assert "latency_cost.png" in (tmp_path / "BENCHMARK.md").read_text()

    def test_markdown_reports_both_arms(self) -> None:
        md = render_markdown(self.make_results())
        assert "100%" in md  # compiled success rate
        assert "75%" in md  # agent success rate
        assert "$0.25" in md or "0.2500" in md
        for banned in (
            "delve",
            "leverage",
            "seamless",
            "robust",
            "comprehensive",
            "transformative",
        ):
            assert banned not in md.lower(), banned
