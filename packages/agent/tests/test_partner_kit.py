"""Day-1 partner kit: invoke Flow and keep the MockMed oracle honest.

Unit tests in this package stub ``openadapt-flow run``. These tests call the
installed engine. The weekly Flow tutorial e2e still owns the Playwright
``VERIFIED`` / ``HALTED`` path.
"""

from __future__ import annotations

import json
import subprocess
from importlib.metadata import version
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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


def test_mockmed_optimistic_fault_leaves_the_store_unchanged() -> None:
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    try:
        before = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert before["records"] == []

        request = Request(
            url + "api/encounter?fault=optimistic",
            data=json.dumps(
                {"patient_id": "p1", "type": "Triage", "note": "synthetic"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(request, timeout=2)
        assert raised.value.code == 409

        after = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert after["records"] == []
        assert db.rejected_writes == 1
    finally:
        stop()
