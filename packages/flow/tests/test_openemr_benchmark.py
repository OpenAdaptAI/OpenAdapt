"""Unit tests for the OpenEMR benchmark pieces (no network anywhere).

The Anthropic client and backend are faked, the orchestrator's run helpers
are monkeypatched, and ``verify_note_saved`` runs real OCR on synthetic
cv2-rendered screenshots — the same testing style as ``test_benchmark.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from openadapt_flow.benchmark import agent_baseline, openemr_benchmark
from openadapt_flow.benchmark.agent_baseline import (
    openemr_task_prompt,
    run_agent,
)
from openadapt_flow.benchmark.openemr_benchmark import (
    aggregate_openemr_results,
    note_for,
    render_openemr_markdown,
    run_openemr_benchmark,
    write_openemr_outputs,
)
from openadapt_flow.benchmark.verify import verify_note_saved

NOTE = "Insurance card copied and coverage verified by phone."


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


BLANK_PNG = to_png(np.full((800, 1280, 3), 245, dtype=np.uint8))


def make_screen(*lines: str, thickness: int = 2) -> bytes:
    img = np.full((800, 1280, 3), 245, dtype=np.uint8)
    for i, line in enumerate(lines):
        cv2.putText(
            img,
            line,
            (40, 200 + i * 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            thickness,
            cv2.LINE_AA,
        )
    return to_png(img)


# -- verify_note_saved (shared, arm-independent) -------------------------------


class TestVerifyNoteSaved:
    def test_note_embedded_in_message_row_passes(self) -> None:
        # OpenEMR shows the note inside a longer list line (timestamp +
        # "(admin to admin)" prefix). Rendered at thickness=1 (thin strokes,
        # like real anti-aliased browser text) rather than the helper's
        # bold thickness=2 default: rapidocr's angle classifier mis-detects
        # the BOLD cv2-Hershey render of this long, digit-prefixed line as
        # 180°-rotated and flips it, garbling the OCR. That is a synthetic-
        # render artifact of the blocky bold font — it does NOT occur on the
        # real browser-rendered OpenEMR row (which verifies at 100%). The row
        # content, the note, and the criterion below are all unchanged; only
        # the incidental stroke weight is thinned so OCR reads the frame the
        # verifier actually faces in production.
        screen = make_screen(
            "Patient Messages",
            f"2026-07-08 (admin to admin) {NOTE}",
            thickness=1,
        )
        verdict = verify_note_saved(screen, NOTE)
        assert verdict.success
        assert verdict.longest_run >= 16

    def test_wrapped_fragment_passes(self) -> None:
        # A wrapped note where OCR only captured one distinctive fragment
        # of at least 16 contiguous characters still counts.
        screen = make_screen("coverage verified by")
        verdict = verify_note_saved(screen, NOTE)
        assert verdict.success
        assert verdict.longest_run >= 16

    def test_blank_screen_fails(self) -> None:
        verdict = verify_note_saved(BLANK_PNG, NOTE)
        assert not verdict.success
        assert verdict.longest_run < 16

    def test_wrong_note_fails(self) -> None:
        # A different run's note visible on screen must not satisfy this
        # run's check (contiguous-run criterion, dissimilar note texts).
        screen = make_screen(
            "2026-07-08 (admin to admin) "
            "Dermatology biopsy site healing cleanly, no drainage.",
        )
        verdict = verify_note_saved(screen, NOTE)
        assert not verdict.success

    def test_empty_note_fails(self) -> None:
        assert not verify_note_saved(BLANK_PNG, "  ").success


# -- task prompt ---------------------------------------------------------------


class TestOpenemrTaskPrompt:
    def test_states_intent_not_coordinates(self) -> None:
        prompt = openemr_task_prompt(NOTE)
        assert '"admin"' in prompt
        assert '"pass"' in prompt
        assert "Belford, Phil" in prompt
        assert NOTE in prompt
        assert "coordinate" not in prompt.lower()
        assert "px" not in prompt.lower()


# -- agent scroll action -------------------------------------------------------


class FakeBackend:
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

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))


def tool_use(action: dict[str, Any], block_id: str = "tu_1") -> Any:
    return SimpleNamespace(type="tool_use", id=block_id, name="computer", input=action)


def response(
    blocks: list[Any],
    stop_reason: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> Any:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        ),
    )


class FakeClient:
    """Scripted beta.messages.create; records a deep copy of each request."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        import copy

        self.calls.append(copy.deepcopy(kwargs))
        return self.script.pop(0)


class TestAgentScroll:
    def test_scroll_dispatches_wheel_pixels(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [
                        tool_use(
                            {
                                "action": "scroll",
                                "coordinate": [640, 400],
                                "scroll_direction": "down",
                                "scroll_amount": 4,
                            }
                        )
                    ],
                    "tool_use",
                ),
                response(
                    [tool_use({"action": "scroll", "scroll_direction": "up"})],
                    "tool_use",
                ),
                response([], "end_turn"),
            ]
        )
        result = run_agent(backend, "task", client=client)
        px = agent_baseline.SCROLL_PX_PER_UNIT
        assert ("scroll", 0, 4 * px) in backend.calls
        assert ("scroll", 0, -3 * px) in backend.calls
        assert result.actions == 2

    def test_scroll_listed_as_supported(self) -> None:
        assert "scroll" in agent_baseline._SUPPORTED_ACTIONS


# -- notes ---------------------------------------------------------------------


class TestNotes:
    def test_distinct_across_both_arms(self) -> None:
        notes = [note_for("compiled", i) for i in range(20)] + [
            note_for("agent", i) for i in range(10)
        ]
        assert len(set(notes)) == 30

    def test_pairwise_dissimilarity_below_run_threshold(self) -> None:
        # Several runs' notes are visible on the same final screen, and
        # verify_note_saved accepts a contiguous 16-char match — so no two
        # notes may share a 16-char squashed substring, or one run's note
        # would satisfy another run's check.
        import difflib

        def squash(text: str) -> str:
            return "".join(text.lower().split())

        notes = [note_for("compiled", i) for i in range(20)] + [
            note_for("agent", i) for i in range(10)
        ]
        for i, a in enumerate(notes):
            for b in notes[i + 1 :]:
                longest = max(
                    block.size
                    for block in difflib.SequenceMatcher(
                        None, squash(a), squash(b), autojunk=False
                    ).get_matching_blocks()
                )
                assert longest < 16, (a, b, longest)


# -- orchestrator --------------------------------------------------------------


def compiled_row(i: int, *, success: bool = True, wall: float = 38.0) -> dict:
    return {
        "arm": "compiled",
        "i": i,
        "note": note_for("compiled", i),
        "wall_s": wall,
        "success": success,
        "matched_ratio": 1.0 if success else 0.1,
        "longest_run": 40 if success else 3,
        "replayer_success": success,
        "heal_count": 1,
        "actions": 18,
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
    wall: float = 150.0,
    cost: float = 0.5,
    error: str | None = None,
) -> dict:
    return {
        "arm": "agent",
        "i": i,
        "note": note_for("agent", i),
        "wall_s": wall,
        "success": success,
        "matched_ratio": 1.0 if success else 0.2,
        "longest_run": 40 if success else 4,
        "actions": 30,
        "api_calls": 31,
        "input_tokens": 90_000,
        "output_tokens": 9_000,
        "cache_creation_input_tokens": 120_000,
        "cache_read_input_tokens": 700_000,
        "cost_usd": cost,
        "stopped": "model_done" if success else "budget_exhausted",
        "model_stop_reason": "end_turn",
        "error": error,
    }


class TestOpenemrOrchestrator:
    def make_results(self) -> dict:
        compiled = [compiled_row(i, success=i != 3) for i in range(20)]
        agents = [agent_row(i, success=i not in (1, 5)) for i in range(10)]
        results = aggregate_openemr_results(compiled, agents)
        results["pace_s"] = 30.0
        return results

    def test_aggregates(self) -> None:
        results = self.make_results()
        c = results["arms"]["compiled"]
        a = results["arms"]["agent"]
        assert c["n"] == 20
        assert c["success_count"] == 19
        assert c["cost_usd_total"] == 0.0
        assert a["n"] == 10
        assert a["success_count"] == 8
        assert a["cost_usd_total"] == 5.0
        assert results["model"] == agent_baseline.MODEL
        assert results["target"].startswith("https://demo.openemr.io")

    def test_markdown_names_anchor_and_caveats(self) -> None:
        md = render_openemr_markdown(self.make_results())
        assert "MockMed" in md  # methodology anchor called out
        assert "CI-reproducible" in md
        assert "Caveats" in md
        assert "resets daily" in md
        assert "95%" in md  # compiled success rate
        assert "80%" in md  # agent success rate
        assert "agent run 2" in md and "agent run 6" in md
        assert "compiled run 4" in md
        for banned in (
            "delve",
            "leverage",
            "seamless",
            "robust",
            "comprehensive",
            "transformative",
        ):
            assert banned not in md.lower(), banned

    def test_markdown_discloses_compiled_self_flags(self) -> None:
        # A run whose replayer self-flagged postcondition drift but whose
        # note the arm-independent OCR check verified saved counts as a
        # success AND is disclosed.
        compiled = [compiled_row(i) for i in range(20)]
        compiled[19]["replayer_success"] = False
        compiled[19]["first_failure"] = {"step": "step_017", "error": "drift"}
        results = aggregate_openemr_results(compiled, [agent_row(i) for i in range(10)])
        results["pace_s"] = 30.0
        md = render_openemr_markdown(results)
        assert "100% (20/20)" in md  # headline unchanged
        assert "self-flagged" in md
        assert "compiled run 20" in md
        assert "step_017" in md
        assert "arm-independent OCR check" in md
        # No self-flag block when nothing self-flagged (a genuinely failed
        # run belongs in the failed-runs list, not here).
        assert "self-flagged" not in render_openemr_markdown(self.make_results())

    def test_write_outputs(self, tmp_path: Path) -> None:
        import json

        write_openemr_outputs(self.make_results(), tmp_path)
        loaded = json.loads((tmp_path / "results.json").read_text())
        assert loaded["arms"]["agent"]["success_count"] == 8
        assert (tmp_path / "latency_cost.png").stat().st_size > 1000
        assert "latency_cost.png" in (tmp_path / "BENCHMARK.md").read_text()

    def test_write_outputs_survives_font_lookup_failure(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A corrupt font cache must skip the chart, not fail the benchmark.

        Simulates ``ValueError: Failed to find font DejaVu Sans`` from
        ``findfont`` (fresh venvs / concurrent runs). The cosmetic PNG may be
        skipped, but the numeric ``results.json`` must be written intact and
        the call must not raise.
        """
        import json

        from matplotlib import font_manager

        def boom(*args: Any, **kwargs: Any) -> str:
            raise ValueError("Failed to find font DejaVu Sans")

        monkeypatch.setattr(font_manager.fontManager, "findfont", boom)
        monkeypatch.setattr(font_manager, "findfont", boom)

        write_openemr_outputs(self.make_results(), tmp_path)  # must not raise
        loaded = json.loads((tmp_path / "results.json").read_text())
        assert loaded["arms"]["agent"]["success_count"] == 8
        assert "latency_cost.png" in (tmp_path / "BENCHMARK.md").read_text()

    def test_run_paces_and_records_failures(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        sleeps: list[float] = []

        def fake_compiled(bundle, url, run_dir, note, **kwargs):
            if note == note_for("compiled", 1):
                raise RuntimeError("demo instance hiccup")
            return compiled_row(0)

        def fake_agent(url, note, **kwargs):
            assert kwargs["max_actions"] == 40
            assert kwargs["max_cost_usd"] == 1.50
            assert note in kwargs["task"]
            return agent_row(0)

        monkeypatch.setattr(openemr_benchmark, "_compiled_run", fake_compiled)
        monkeypatch.setattr(openemr_benchmark, "_agent_run", fake_agent)
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=3,
            n_agent=2,
            pace_s=30.0,
            preflight=lambda: (True, None),
            sleep=sleeps.append,
            log=lambda _msg: None,
        )
        # 3 compiled runs pace twice (no sleep before the first), 2 agent
        # runs pace twice (gap after the compiled arm and between runs).
        assert sleeps == [30.0, 30.0, 30.0, 30.0]
        rows = results["runs"]["compiled"]
        assert [r["success"] for r in rows] == [True, False, True]
        assert "demo instance hiccup" in rows[1]["error"]
        assert results["agent_arm_note"] is None
        assert (tmp_path / "results.json").is_file()
        assert (tmp_path / "BENCHMARK.md").is_file()


# -- cost guardrails -------------------------------------------------------


class TestComputeCostCacheBuckets:
    def test_each_bucket_priced_at_list(self) -> None:
        import math

        mtok = 1_000_000
        assert agent_baseline.compute_cost(mtok, 0) == 3.00
        assert agent_baseline.compute_cost(0, mtok) == 15.00
        # 5-minute cache writes bill at 1.25x input list price.
        assert agent_baseline.compute_cost(0, 0, mtok, 0) == 3.75
        # Cache reads bill at 0.1x input list price.
        assert math.isclose(agent_baseline.compute_cost(0, 0, 0, mtok), 0.30)

    def test_buckets_sum(self) -> None:
        cost = agent_baseline.compute_cost(500_000, 100_000, 200_000, 2_000_000)
        expected = 0.5 * 3.00 + 0.1 * 15.00 + 0.2 * 3.75 + 2.0 * 0.30
        assert abs(cost - expected) < 1e-9


class TestPerRunCostCap:
    def test_cap_trips_and_stops_loop(self) -> None:
        # 200K uncached input tokens/call = $0.60/call at list. With a
        # $1.00 cap the second call exceeds it; the loop must stop with
        # stopped="cost_cap" without executing that turn's actions.
        backend = FakeBackend()
        click = tool_use({"action": "left_click", "coordinate": [10, 10]})
        client = FakeClient(
            [
                response([click], "tool_use", input_tokens=200_000),
                response([click], "tool_use", input_tokens=200_000),
                response([click], "tool_use", input_tokens=200_000),
            ]
        )
        result = run_agent(backend, "task", client=client, max_cost_usd=1.0)
        assert result.stopped == "cost_cap"
        assert result.api_calls == 2
        assert result.actions == 1  # second turn's action never executed
        assert result.cost_usd > 1.0
        assert client.script  # third scripted response never requested

    def test_capped_run_returns_normally_with_counters(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [],
                    "end_turn",
                    input_tokens=1_000_000,
                    cache_creation_input_tokens=100_000,
                    cache_read_input_tokens=400_000,
                )
            ]
        )
        result = run_agent(backend, "task", client=client, max_cost_usd=0.5)
        assert result.stopped == "cost_cap"
        assert result.input_tokens == 1_000_000
        assert result.cache_creation_input_tokens == 100_000
        assert result.cache_read_input_tokens == 400_000
        assert result.cost_usd == agent_baseline.compute_cost(
            1_000_000, 50, 100_000, 400_000
        )


class TestCacheControlPlacement:
    def test_tools_and_newest_message_marked_stale_stripped(self) -> None:
        backend = FakeBackend()
        click = tool_use({"action": "left_click", "coordinate": [10, 10]})
        client = FakeClient(
            [
                response([click], "tool_use"),
                response([click], "tool_use"),
                response([], "end_turn"),
            ]
        )
        run_agent(backend, "task", client=client)
        assert len(client.calls) == 3
        for call in client.calls:
            # The tool definition carries a stable breakpoint on every call.
            assert call["tools"][0]["cache_control"] == {"type": "ephemeral"}
            # Exactly one per-turn marker, on the last block of the last
            # (newest user) message; stale markers are stripped.
            marked = [
                (mi, bi)
                for mi, msg in enumerate(call["messages"])
                if isinstance(msg.get("content"), list)
                for bi, block in enumerate(msg["content"])
                if isinstance(block, dict) and "cache_control" in block
            ]
            last_mi = len(call["messages"]) - 1
            last_bi = len(call["messages"][last_mi]["content"]) - 1
            assert marked == [(last_mi, last_bi)], call["messages"]

    def test_first_call_task_string_becomes_marked_text_block(self) -> None:
        backend = FakeBackend()
        client = FakeClient([response([], "end_turn")])
        run_agent(backend, "do the thing", client=client)
        (msg,) = client.calls[0]["messages"]
        assert msg["content"] == [
            {
                "type": "text",
                "text": "do the thing",
                "cache_control": {"type": "ephemeral"},
            }
        ]


def _patch_arms(monkeypatch: Any, agent_fn: Any) -> None:
    monkeypatch.setattr(
        openemr_benchmark,
        "_compiled_run",
        lambda bundle, url, run_dir, note, **kw: compiled_row(0),
    )
    monkeypatch.setattr(openemr_benchmark, "_agent_run", agent_fn)


class TestTotalCostCap:
    def test_truncates_arm_and_discloses(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_arms(monkeypatch, lambda url, note, **kw: agent_row(0, cost=3.0))
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=1,
            n_agent=5,
            pace_s=0.0,
            max_cost_per_run_usd=3.0,
            max_total_cost_usd=8.0,
            preflight=lambda: (True, None),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        # Runs 1 and 2 fit ($0+$3, $3+$3 <= $8); run 3 could reach $9.
        assert len(results["runs"]["agent"]) == 2
        note = results["agent_arm_note"]
        assert "truncated by $8.00 cost ceiling after 2 of 5 runs" in note
        assert note in (tmp_path / "BENCHMARK.md").read_text()
        assert note in (tmp_path / "results.json").read_text()


class TestRowsJsonl:
    def test_appended_after_every_run_in_both_arms(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import json as _json

        _patch_arms(monkeypatch, lambda url, note, **kw: agent_row(0))
        run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=3,
            n_agent=2,
            pace_s=0.0,
            preflight=lambda: (True, None),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        lines = (tmp_path / "rows.jsonl").read_text().splitlines()
        rows = [_json.loads(line) for line in lines]
        assert [r["arm"] for r in rows] == ["compiled"] * 3 + ["agent"] * 2

    def test_rows_survive_a_mid_arm_crash(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # An unexpected crash on compiled run 3 must not lose runs 1-2.
        import json as _json

        calls = {"n": 0}

        def crashing_compiled(bundle, url, run_dir, note, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt  # not caught by the per-run except
            return compiled_row(0)

        monkeypatch.setattr(openemr_benchmark, "_compiled_run", crashing_compiled)
        try:
            run_openemr_benchmark(
                tmp_path,
                tmp_path / "bundle",
                n_compiled=5,
                n_agent=0,
                pace_s=0.0,
                preflight=lambda: (True, None),
                sleep=lambda _s: None,
                log=lambda _msg: None,
            )
        except KeyboardInterrupt:
            pass
        lines = (tmp_path / "rows.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert all(_json.loads(line)["arm"] == "compiled" for line in lines)


class TestBillingErrorAbort:
    def test_two_consecutive_billing_errors_abort_arm(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_arms(
            monkeypatch,
            lambda url, note, **kw: agent_row(
                0,
                success=False,
                cost=0.0,
                error=(
                    "AuthenticationError: Error code: 401 - your credit "
                    "balance is too low"
                ),
            ),
        )
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=1,
            n_agent=6,
            pace_s=0.0,
            preflight=lambda: (True, None),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        assert len(results["runs"]["agent"]) == 2
        note = results["agent_arm_note"]
        assert "aborted after 2 consecutive auth/billing errors" in note
        assert note in (tmp_path / "BENCHMARK.md").read_text()

    def test_transient_errors_do_not_abort(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # Non-billing failures (demo weather) keep the arm going.
        _patch_arms(
            monkeypatch,
            lambda url, note, **kw: agent_row(
                0,
                success=False,
                cost=0.01,
                error="TimeoutError: navigation timed out",
            ),
        )
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=1,
            n_agent=4,
            pace_s=0.0,
            preflight=lambda: (True, None),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        assert len(results["runs"]["agent"]) == 4
        assert results["agent_arm_note"] is None

    def test_billing_error_detector(self) -> None:
        looks = openemr_benchmark._looks_like_billing_error
        assert looks("Error code: 401 - invalid x-api-key")
        assert looks("Error code: 402 - payment required")
        assert looks("your credit balance is too low")
        assert looks("BillingError: card declined")
        assert not looks("TimeoutError: page load took 45000 ms")
        assert not looks("replayed 14000 steps")  # 400 as a substring only


class CrashingClient(FakeClient):
    """Serves its script, then raises on every further API call.

    Models a mid-run API failure (429/500/529) after N paid calls: the
    paid usage must still reach the recorded row and the cost ceiling.
    """

    def __init__(self, script: list[Any], error: Exception) -> None:
        super().__init__(script)
        self.error = error

    def _create(self, **kwargs: Any) -> Any:
        if not self.script:
            raise self.error
        return super()._create(**kwargs)


def _fake_launch(monkeypatch: Any, backend: FakeBackend) -> None:
    """Patch PlaywrightBackend.launch to return ``backend`` (no browser)."""
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    monkeypatch.setattr(
        PlaywrightBackend,
        "launch",
        classmethod(lambda cls, url, headless=True: (backend, lambda: None)),
    )


class TestCrashedRunSpendAccounting:
    """F1: a mid-run crash must not zero out the run's real spend."""

    CLICK = {"action": "left_click", "coordinate": [10, 10]}

    def crashing_client(self) -> CrashingClient:
        # Two paid calls at 200K uncached input tokens each ($0.60/call at
        # list), then the third call raises mid-run.
        return CrashingClient(
            [
                response([tool_use(self.CLICK)], "tool_use", input_tokens=200_000),
                response([tool_use(self.CLICK)], "tool_use", input_tokens=200_000),
            ],
            RuntimeError("Error code: 529 - overloaded_error"),
        )

    def test_mid_run_crash_row_carries_partial_cost(self, monkeypatch: Any) -> None:
        from openadapt_flow.benchmark.run_benchmark import _agent_run

        _fake_launch(monkeypatch, FakeBackend())
        row = _agent_run("http://x", NOTE, client=self.crashing_client(), task="task")
        assert row["error"] == "RuntimeError: Error code: 529 - overloaded_error"
        assert row["stopped"] == "error"
        assert not row["success"]
        # The two paid calls' usage reached the row despite the crash.
        assert row["api_calls"] == 2
        assert row["input_tokens"] == 400_000
        assert row["output_tokens"] == 100
        expected = agent_baseline.compute_cost(400_000, 100)
        assert row["cost_usd"] == expected
        assert row["cost_usd"] > 1.0

    def test_crashed_run_spend_counts_against_ceiling(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # End to end through the orchestrator: run 1 crashes after ~$1.20
        # of paid calls; the $2.00 ceiling must see that spend and truncate
        # the arm before run 2 (1.20 + 1.50 > 2.00).
        _fake_launch(monkeypatch, FakeBackend())
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=0,
            n_agent=3,
            pace_s=0.0,
            max_cost_per_run_usd=1.50,
            max_total_cost_usd=2.00,
            agent_client=self.crashing_client(),
            preflight=lambda: (True, None),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        rows = results["runs"]["agent"]
        assert len(rows) == 1
        assert rows[0]["error"] is not None
        assert rows[0]["cost_usd"] > 1.0  # crashed run's spend recorded
        note = results["agent_arm_note"]
        assert "truncated by $2.00 cost ceiling after 1 of 3 runs" in note
        assert "$1.20 spent" in note
        # The crashed spend is in the aggregate totals too.
        assert results["arms"]["agent"]["cost_usd_total"] == rows[0]["cost_usd"]


class TestNoteForBounds:
    def test_index_past_note_list_asserts(self) -> None:
        import pytest

        with pytest.raises(AssertionError, match="pairwise distinctness"):
            note_for("agent", len(openemr_benchmark._AGENT_NOTES))
        with pytest.raises(AssertionError, match="pairwise distinctness"):
            note_for("compiled", len(openemr_benchmark._COMPILED_NOTES))

    def test_orchestrator_rejects_n_beyond_notes(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(ValueError, match="n_agent"):
            run_openemr_benchmark(
                tmp_path,
                tmp_path / "bundle",
                n_compiled=0,
                n_agent=len(openemr_benchmark._AGENT_NOTES) + 1,
                preflight=lambda: (True, None),
                sleep=lambda _s: None,
                log=lambda _msg: None,
            )
        with pytest.raises(ValueError, match="n_compiled"):
            run_openemr_benchmark(
                tmp_path,
                tmp_path / "bundle",
                n_compiled=len(openemr_benchmark._COMPILED_NOTES) + 1,
                n_agent=0,
                preflight=lambda: (True, None),
                sleep=lambda _s: None,
                log=lambda _msg: None,
            )


class TestPreflight:
    def test_failed_preflight_skips_agent_arm_keeps_compiled(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        def no_agent(url, note, **kw):  # pragma: no cover - must not run
            raise AssertionError("agent arm must not run after preflight")

        _patch_arms(monkeypatch, no_agent)
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=2,
            n_agent=5,
            pace_s=0.0,
            preflight=lambda: (
                False,
                "AuthenticationError: credit balance too low",
            ),
            sleep=lambda _s: None,
            log=lambda _msg: None,
        )
        assert len(results["runs"]["compiled"]) == 2
        assert results["runs"]["agent"] == []
        note = results["agent_arm_note"]
        assert note.startswith("skipped: API preflight failed")
        assert "credit balance too low" in note
        # Compiled-only outputs are still written cleanly.
        assert note in (tmp_path / "BENCHMARK.md").read_text()
        assert (tmp_path / "latency_cost.png").is_file()

    def test_preflight_check_reports_exception(self) -> None:
        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("Error code: 402 - no credit")

        ok, err = agent_baseline.preflight_check(client=Boom())
        assert not ok
        assert "402" in err

    def test_preflight_check_passes_minimal_call(self) -> None:
        seen = {}

        class Ok:
            class messages:
                @staticmethod
                def create(**kwargs):
                    seen.update(kwargs)
                    return SimpleNamespace()

        ok, err = agent_baseline.preflight_check(client=Ok())
        assert ok and err is None
        assert seen["max_tokens"] == 1
        assert seen["model"] == agent_baseline.MODEL

    def test_transient_error_retried_once_then_passes(self) -> None:
        # A 529/overload blip must not declare the key dead: one retry.
        calls = {"n": 0}
        sleeps: list[float] = []

        class Flaky:
            class messages:
                @staticmethod
                def create(**kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("Error code: 529 - overloaded")
                    return SimpleNamespace()

        ok, err = agent_baseline.preflight_check(client=Flaky(), sleep=sleeps.append)
        assert ok and err is None
        assert calls["n"] == 2
        assert sleeps == [2.0]

    def test_transient_error_twice_fails_with_last_error(self) -> None:
        calls = {"n": 0}

        class Down:
            class messages:
                @staticmethod
                def create(**kwargs):
                    calls["n"] += 1
                    raise RuntimeError("Error code: 500 - server error")

        ok, err = agent_baseline.preflight_check(client=Down(), sleep=lambda _s: None)
        assert not ok
        assert "500" in err
        assert calls["n"] == 2  # exactly one retry, then declared dead

    def test_billing_error_not_retried(self) -> None:
        # An auth/billing failure cannot succeed on retry; fail fast.
        calls = {"n": 0}

        class Dead:
            class messages:
                @staticmethod
                def create(**kwargs):
                    calls["n"] += 1
                    raise RuntimeError("Error code: 401 - invalid x-api-key")

        ok, err = agent_baseline.preflight_check(client=Dead(), sleep=lambda _s: None)
        assert not ok
        assert "401" in err
        assert calls["n"] == 1
