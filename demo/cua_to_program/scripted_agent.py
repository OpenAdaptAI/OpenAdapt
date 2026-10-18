"""A free stand-in for the Claude agent, for dry runs of the pipeline.

It does the same claim entry as the scripted openIMIS demonstration, sending
every action through the JIT ``CaptureComputer`` in the ``generic`` dialect.
It calls no model, so ``compile_agent`` records it without a model key. The
launch demo itself is recorded with ``scoreboard.agent.ClaudeComputerUseAgent``;
this agent only checks the plumbing.
"""

from __future__ import annotations

from typing import Any

from scoreboard.tasks.openimis import _control_input

DIAGNOSIS_QUERY = "Scarlet"
DIAGNOSIS_OPTION = "Scarlet fever, uncomplicated"
SERVICE_QUERY = "General"
SERVICE_OPTION = "General Consultation"


def _center(locator: Any) -> tuple[int, int]:
    for _ in range(20):
        locator.wait_for(state="visible", timeout=30_000)
        box = locator.bounding_box()
        if box is not None:
            return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        locator.page.wait_for_timeout(250)
    raise RuntimeError("target never got a bounding box")


class ScriptedClaimAgent:
    dialect = "generic"
    requires_model_key = False
    principal_id = "agent:scripted-dry-run"

    def run(self, request: Any, computer: Any) -> None:
        page = computer._page
        values = request.inputs

        def click(locator: Any) -> None:
            x, y = _center(locator)
            computer.execute({"kind": "click", "x": x, "y": y})

        def type_(text: str) -> None:
            computer.execute({"kind": "type", "text": text})

        type_(values["insurance_no"])
        page.get_by_text("Policy Information (Active)").wait_for(
            state="visible", timeout=30_000
        )
        click(_control_input(page, "Claim No"))
        type_(values["claim_no"])
        click(page.get_by_placeholder("Search Diagnosis").first)
        type_(DIAGNOSIS_QUERY)
        click(page.locator("[role=option]", has_text=DIAGNOSIS_OPTION).first)
        # Click the empty field on its label text, as a person would.
        click(page.locator("label", has_text="Explanation").first)
        type_(values["explanation"])
        computer.execute({"kind": "scroll", "dx": 0, "dy": 400})
        page.wait_for_timeout(500)
        click(page.get_by_placeholder("Search Service").first)
        type_(SERVICE_QUERY)
        click(page.locator("[role=option]", has_text=SERVICE_OPTION).first)
        page.wait_for_timeout(1_000)
        click(page.locator("button.MuiFab-root:enabled").last)
        page.wait_for_timeout(3_000)
