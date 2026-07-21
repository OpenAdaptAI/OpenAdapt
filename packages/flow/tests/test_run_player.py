"""Light test for the interactive run player generator.

Exercises the extraction + HTML build from the committed REAL run artifacts
(no Playwright, no models): asserts the player carries the real step counts,
resolution rungs, heal count, the loud HALT, and genuine embedded frames.
"""

from __future__ import annotations

import json

import pytest

generate = pytest.importorskip("benchmark.run_player.generate")


@pytest.fixture(scope="module")
def runs():
    # Uses only the committed run directories (baseline/theme from
    # docs/showcase, the modal-halt run from benchmark/run_player/runs). If
    # the halt run has not been generated yet, skip rather than invoke a live
    # replay from a unit test.
    if not (generate.HALT_RUN_DIR / "report.json").is_file():
        pytest.skip(
            "halt run not generated; run `python -m benchmark.run_player.generate --regen-halt`"
        )
    return [generate.extract_run(spec) for spec in generate.RUN_SPECS]


def test_three_real_runs_captured(runs):
    ids = {r["id"] for r in runs}
    assert ids == {"baseline", "theme", "halt"}
    # Every run is the same 11-step compiled workflow.
    for r in runs:
        assert r["summary"]["steps"] == 11
        assert len(r["steps"]) == 11


def test_runs_are_model_free(runs):
    for r in runs:
        assert r["summary"]["model_calls"] == 0


def test_baseline_all_template_no_heal(runs):
    base = next(r for r in runs if r["id"] == "baseline")
    assert base["summary"]["success"] is True
    assert base["summary"]["heals"] == 0
    assert base["summary"]["rungs"].get("template", 0) >= 1
    # No lower rung fired on the clean run.
    assert set(base["summary"]["rungs"]) == {"template"}


def test_theme_run_heals_via_lower_rungs(runs):
    theme = next(r for r in runs if r["id"] == "theme")
    assert theme["summary"]["success"] is True
    assert theme["summary"]["heals"] >= 1
    # Heals come off the geometry / ocr rungs, and each carries a diff.
    assert set(theme["summary"]["rungs"]) <= {"geometry", "ocr", "grounder"}
    healed = [s for s in theme["steps"] if s["healed"]]
    assert healed, "theme run should heal at least one step"
    for step in healed:
        diff = step["heal_diff"]
        assert diff is not None
        assert diff["rung_used"] in {"geometry", "ocr", "grounder"}
        assert diff["note"], "a heal must explain what it did"
    # At least one heal moved the click target — proving the diff mechanism.
    assert any(s["heal_diff"]["changed"] for s in healed)


def test_halt_run_stops_loudly(runs):
    halt = next(r for r in runs if r["id"] == "halt")
    assert halt["summary"]["success"] is False
    assert halt["summary"]["halted_at"] == "step_010"
    last = halt["steps"][-1]
    assert last["ok"] is False
    assert last["error"] and "aborted" in last["error"]
    assert last["postconditions_ok"] is False


def test_html_carries_real_step_data():
    runs = [generate.extract_run(spec) for spec in generate.RUN_SPECS]
    html = generate.build_html(runs)
    # Embedded JSON payload carries the real per-run counts + rungs.
    assert '"steps":11' in html
    assert '"halted_at":"step_010"' in html
    assert '"rung":"template"' in html
    assert '"rung":"geometry"' in html
    assert '"rung":"ocr"' in html
    assert '"model_calls":0' in html
    # Real embedded frames (before + after for 33 steps across 3 runs).
    assert html.count("data:image/png;base64,") == 66
    # Self-contained: no external asset references (CSP-safe).
    for bad in ("http://", "https://", 'src="//'):
        assert bad not in html


def test_player_data_json_has_no_image_bytes():
    runs = [generate.extract_run(spec) for spec in generate.RUN_SPECS]
    data = generate._strip_images(runs)
    blob = json.dumps(data)
    assert "base64" not in blob
    assert all("before" not in s and "after" not in s for r in data for s in r["steps"])
