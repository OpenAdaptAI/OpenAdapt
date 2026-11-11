"""Read-only teaching runs only where a guard stops writes before they leave.

A browser blocks every write request; a desktop app, or one seen over RDP or
Citrix, writes over its own connection, so read-only teaching is refused there
rather than claimed. Write teaching (checked in the system of record) is the
path those apps take.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.agent import browser_teach as bt
from openadapt_flow.agent.cli import main as agent_main
from openadapt_flow.compile_agent.pipeline import BACKENDS

SECTIONS = {
    "windows": "kind: windows\n  agent_url: http://127.0.0.1:8765\n",
    "macos": "kind: macos\n  macos_app: Chart\n",
    "linux": "kind: linux\n  linux_app: chart\n  linux_window_title: Chart\n",
    "rdp": "kind: rdp\n  rdp_host: ehr.example.org\n",
    "citrix": "kind: citrix\n  rdp_window: Citrix Viewer\n",
}


def test_every_backend_has_a_guard_or_a_refusal() -> None:
    for kind in BACKENDS:
        refusal = bt.read_only_teach_refusal(kind)
        if kind in bt.READ_ONLY_GUARDS:
            assert refusal is None
        else:
            assert refusal is not None and kind in refusal
            # The refusal names the path that does work on that app.
            assert "serve --teach" in refusal and "--config" in refusal
    assert set(bt.READ_ONLY_GUARDS) == {"web"}
    # An unknown kind has no guard either.
    assert bt.read_only_teach_refusal("vnc") is not None


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_serve_refuses_read_only_teaching_on_a_desktop_or_remote_app(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("mcp", reason="serve needs the agent extra")
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text("backend:\n  " + SECTIONS[kind])
    assert agent_main(["serve", "--teach-browser", "--config", str(deploy)]) == 2
    err = capsys.readouterr().err
    assert f"read-only teaching is refused for a {kind} app" in err
    assert "Nothing was served" in err
