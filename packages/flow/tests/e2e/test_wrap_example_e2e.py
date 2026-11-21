"""``openadapt wrap --example`` end to end, with a scripted model client.

Everything is real except the model: the bundled sample app, the headless
browser, the built-in Claude computer-use loop (``build_agent``'s
``every_turn`` agent with its token-count reservation, spend cap and ledger),
the read-back, compile, canary, admission, the emitted MCP server and run 2.
The scripted client stands where ``anthropic.Anthropic`` would. It answers
each turn with the click or typing a model would ask for, at the control's
place on the page, and reports token usage like the API does.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("playwright")
pytest.importorskip("mcp")

from openadapt_flow.compile_agent import agents  # noqa: E402
from openadapt_flow.wrap.cli import example_main  # noqa: E402

pytestmark = pytest.mark.timeout(600)


class ScriptedClaude:
    """A stand-in for ``anthropic.Anthropic`` that drives the sample's form."""

    def __init__(self) -> None:
        self.harness: Any = None
        self.calls = 0
        self.counted = 0
        self.typed = False
        self.triage_clicked = False
        self.messages = SimpleNamespace(
            create=self._create, count_tokens=self._count_tokens
        )

    def with_options(self, **_kwargs: Any) -> "ScriptedClaude":
        return self

    def _count_tokens(self, **_kwargs: Any) -> Any:
        self.counted += 1
        return SimpleNamespace(input_tokens=3000)

    def _center(self, selector: str) -> list[int]:
        box = self.harness.page.locator(selector).bounding_box()
        assert box is not None, selector
        return [int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)]

    def _visible(self, selector: str) -> bool:
        return self.harness.page.locator(selector).is_visible()

    def _next(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        page = self.harness.page
        if self._visible("#saved-banner"):
            return "finish", {"summary": "saved the note"}
        if self._visible("#note"):
            if not self.triage_clicked:
                self.triage_clicked = True
                return "left_click", {"coordinate": self._center("#type-triage")}
            if not self.typed:
                focused = page.evaluate(
                    "document.activeElement && document.activeElement.id"
                )
                if focused != "note":
                    return "left_click", {"coordinate": self._center("#note")}
                self.typed = True
                # The model sees the note only as its placeholder and types that.
                task = messages[0]["content"][0]["text"]
                return "type", {"text": task.rsplit(": ", 1)[1].strip()}
            return "left_click", {"coordinate": self._center("#save-encounter")}
        if self._visible("#new-encounter"):
            return "left_click", {"coordinate": self._center("#new-encounter")}
        return "left_click", {"coordinate": self._center("#open-p1")}

    def _create(self, **kwargs: Any) -> Any:
        self.calls += 1
        name, args = self._next(kwargs["messages"])
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": f"toolu_{self.calls}",
                    "name": name,
                    "input": args,
                }
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=2000,
                output_tokens=40,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def test_example_runs_the_agent_once_then_the_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-not-a-real-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Keep the opt-out ledger and default folders out of the real home.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    client = ScriptedClaude()
    original = agents.ClaudeAgent.drive

    def drive(self: Any, request: Any, harness: Any) -> Any:
        client.harness = harness
        return original(self, request, harness)

    monkeypatch.setattr(agents.ClaudeAgent, "drive", drive)
    out = tmp_path / "example"
    code = example_main(["--example", "--out", str(out)], client=client)
    printed = capsys.readouterr()
    assert code == 0, printed.out + printed.err
    result = json.loads((out / "example-result.json").read_text())
    assert result["ok"] is True
    run1, run2 = result["run1"], result["run2"]
    assert run1["path"] == "agent" and run1["status"] == "admitted"
    assert run1["model_calls"] == client.calls > 0
    assert run1["usage_known"] is True and run1["cost_usd"] > 0
    calls_after_run1 = client.calls
    assert run2["path"] == "skill" and run2["status"] == "VERIFIED"
    assert run2["model_calls"] == 0 and run2["cost_usd"] == 0.0
    assert run2["receipt_outcome"] == "VERIFIED"
    # Run 2 never reached the model client.
    assert client.calls == calls_after_run1
    # The read-back is the app's own record of run 2's new note.
    assert [r["note"] for r in result["readback"]] == ["Recheck knee in ten days"]
    assert result["readback"][0]["patient_id"] == "p1"
    assert (
        "Cost: run 1 $" in printed.out
        and "run 2 $0.0000 (0 model calls)" in printed.out
    )
    # The throwaway keys stay in the example's folder, not the user's.
    assert (out / "keys").is_dir()
    assert not (tmp_path / "home" / "keys").exists()
    ledger = (tmp_path / "home" / "unscrubbed-sends.jsonl").read_text()
    assert "synthetic sample app" in ledger


def test_example_without_a_key_says_which_variable_to_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert example_main(["--example", "--out", str(tmp_path / "x")]) == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "MISSING_MODEL_KEY" in err
    assert not (tmp_path / "x").exists()
