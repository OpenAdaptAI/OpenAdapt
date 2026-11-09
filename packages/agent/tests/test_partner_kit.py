"""Day-1 partner kit: invoke the installed Flow engine.

Unit tests in this package stub ``openadapt-flow run``. These tests call the
installed engine. The MockMed optimistic-fault oracle check lives in the
launcher's ``tests/test_partner_kit.py``. The weekly Flow tutorial e2e still
owns the Playwright ``VERIFIED`` / ``HALTED`` path.
"""

from __future__ import annotations

import subprocess
from importlib.metadata import version

import pytest

from openadapt_agent.runner import default_flow_cli


def _flow_major_minor(raw: str) -> tuple[int, int]:
    nums: list[int] = []
    for part in raw.split(".")[:2]:
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 2:
        nums.append(0)
    return nums[0], nums[1]


def _flow_has_tutorial_break_it(raw: str) -> bool:
    # openadapt-flow 1.29.0 added `tutorial --break-it`.
    return _flow_major_minor(raw) >= (1, 29)


def test_tutorial_break_it_flag_is_gated_to_flow_1_29() -> None:
    assert _flow_has_tutorial_break_it("1.26.0") is False
    assert _flow_has_tutorial_break_it("1.28.9") is False
    assert _flow_has_tutorial_break_it("1.29.0") is True
    assert _flow_has_tutorial_break_it("1.34.0") is True


def test_default_flow_cli_invokes_the_installed_tutorial_help() -> None:
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    result = subprocess.run(
        [*default_flow_cli(), "tutorial", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    # Floor CI pins openadapt-flow==1.26.0. That release has the tutorial
    # verb `serve --allow-run` needs; it does not have --break-it. The halt
    # demo on this package is `openadapt quickstart --break-it`.
    assert "--headed" in text
    if _flow_has_tutorial_break_it(version("openadapt-flow")):
        assert "--break-it" in text
