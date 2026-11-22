"""End to end: `openadapt-flow flight -- <an agent loop>` on MockMed, headless.

A scripted agent (``tests/fixtures/flight/mockmed_agent.py``) runs its own
Anthropic-style computer-use loop against a loopback fake provider through
the flight proxy, with its Playwright executor swapped for GatedComputer.
The model's script opens a referral, starts an encounter, types a note and
clicks Save Encounter. The flight run must meter every call, keep the
planted patient name out of everything the provider received, hold the Save
for a person (FileGate, no answer), and leave MockMed with nothing saved.
The run declares New Encounter and Triage safe (--safe-label); without that
the fail-closed gate holds them too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from openadapt_flow.flight.timeline import validate_flight
from tests.fixtures.flight.fake_provider import (
    ANTHROPIC_USAGE,
    FakeProvider,
    anthropic_click,
)

FLOW_ROOT = Path(__file__).resolve().parents[2]
AGENT = FLOW_ROOT / "tests/fixtures/flight/mockmed_agent.py"
PLANTED = "Zyxwv Qtest"


class _Scrubber:
    # Also scrubs the name in the start-up probe (require_scrubber).
    def scrub_text(self, text, is_separated=False):
        return text.replace(PLANTED, "<PERSON>").replace("John Smith", "<PERSON>")

    def scrub_image(self, image, fill_color=None):
        return image  # the screenshots carry no planted value


def _centers(url: str) -> dict[str, tuple[int, int]]:
    """Where the script's targets are on MockMed (same viewport as the agent)."""
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    out: dict[str, tuple[int, int]] = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url)
            page.fill("#username", "nurse.demo")
            page.fill("#password", "mockmed-demo-pass")
            page.click("#signin")
            for selector in (
                ".open-btn",
                "#new-encounter",
                "#type-triage",
                "#note",
                "#save-encounter",
            ):
                locator = page.locator(selector).first
                locator.wait_for(state="visible")
                box = locator.bounding_box()
                assert box is not None
                out[selector] = (
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                )
                if selector != "#save-encounter" and selector != "#note":
                    locator.click()
            browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")
    return out


def _require_tesseract() -> None:
    """flight checks the scrubber end to end before it starts, and that
    needs pytesseract and the tesseract binary even with a stand-in
    scrubber. CI without them skips; elsewhere their absence fails."""
    import shutil

    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing = "pytesseract"
    else:
        missing = "" if shutil.which("tesseract") else "the tesseract binary"
    if missing:
        if os.environ.get("CI"):
            pytest.skip(f"CI without {missing}")
        raise AssertionError(f"{missing} is missing; flight can't start without it")


def test_flight_meters_scrubs_and_holds_the_save(tmp_path, monkeypatch):
    from openadapt_flow import privacy

    _require_tesseract()
    from openadapt_flow.flight.cli import cmd_flight
    from openadapt_flow.mockmed.server import serve

    url, stop = serve(port=0)
    provider = FakeProvider().start()
    try:
        points = _centers(url)
        script = [
            [anthropic_click(*points[".open-btn"], "toolu_1")],
            [anthropic_click(*points["#new-encounter"], "toolu_2")],
            [anthropic_click(*points["#type-triage"], "toolu_3")],
            [anthropic_click(*points["#note"], "toolu_4")],
            [
                {
                    "type": "tool_use",
                    "id": "toolu_5",
                    "name": "computer",
                    "input": {"action": "type", "text": f"Triage for {PLANTED}"},
                }
            ],
            [anthropic_click(*points["#save-encounter"], "toolu_6")],
            [{"type": "text", "text": "The save was held for a person; stopping."}],
        ]
        provider.anthropic_turns.extend(script)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", provider.url)
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join([str(FLOW_ROOT), os.environ.get("PYTHONPATH", "")]),
        )
        privacy.set_text_scrubber(_Scrubber())
        result_path = tmp_path / "agent.json"
        run_dir = tmp_path / "flight"
        args = argparse.Namespace(
            command=["--", sys.executable, str(AGENT), url, str(result_path)],
            task_id="mockmed-triage-1",
            out=str(run_dir),
            label="scripted-claude-cu-loop",
            gate="file",
            approval_timeout=1.0,
            cdp=None,
            guard_config=None,
            guard_task=None,
            max_cost_usd=None,
            # The gate holds every button not known to be safe; this
            # deployment declares its two navigation buttons safe.
            safe_label=["New Encounter", "Triage"],
            no_scrub=False,
            i_accept_unscrubbed=False,
            # macOS runs the agent under the sandbox-exec egress block; other
            # systems have none and must opt out to run at all.
            allow_unenforced_egress=sys.platform != "darwin",
            no_egress_block=False,
            json=False,
        )
        code = cmd_flight(args)
    finally:
        privacy.reset_scrubbers()
        provider.stop()
        stop()

    assert code == 0
    agent = json.loads(result_path.read_text())
    assert agent["saved"] is False
    assert (
        len(agent["held"]) == 1
        and "held for a person: not performed" in agent["held"][0]
    )

    summary = json.loads((run_dir / "flight.json").read_text())
    assert validate_flight(summary) == []
    assert summary["task_id"] == "mockmed-triage-1"
    assert summary["details"]["safe_labels_added"] == ["new encounter", "triage"]
    egress = summary["details"]["egress"]
    assert egress["enforced"] is (sys.platform == "darwin")
    assert egress["refused"] is False
    assert json.loads((run_dir / "egress.json").read_text())["mode"] == "scrubbed"
    calls = len(script)
    assert summary["model"]["calls"] == calls
    assert summary["model"]["input_tokens"] == calls * ANTHROPIC_USAGE["input_tokens"]
    assert summary["model"]["output_tokens"] == calls * ANTHROPIC_USAGE["output_tokens"]
    assert summary["model"]["cost_source"] == "price_table"
    assert summary["model"]["cost_usd"] > 0
    assert [e["kind"] for e in summary["gate_events"]] == ["held"]
    assert summary["actions_count"] == 6
    assert PLANTED.encode() not in provider.received_bytes()
    assert all(r["headers"]["x-api-key"] == "sk-fake-e2e" for r in provider.requests)
    events = [
        json.loads(line)
        for line in (run_dir / "timeline.jsonl").read_text().splitlines()
    ]
    executor = [e for e in events if e["kind"] == "executor_action"]
    assert [e["decision"] for e in executor] == ["performed"] * 5 + ["held"]
    assert "Save Encounter" in executor[-1]["label"]
    requests = list((run_dir / "item-approval").glob("request-*.json"))
    assert len(requests) == 1
    page = (run_dir / "timeline.html").read_text()
    assert "held" in page
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".html"):
            assert PLANTED not in path.read_text("utf-8")
