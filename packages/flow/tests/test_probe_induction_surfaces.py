"""``induce --probe-on`` probes a test copy on a desktop or remote surface.

The deployment file declares a Windows, macOS, Linux, RDP or Citrix test copy.
:class:`~openadapt_flow.compiler.probing.DeploymentProbeTarget` resets it with
``reset_command`` and opens a fresh backend from the file's ``backend:``
section for every probe run, through the factory. It never opens a browser.
The screen side is the scripted MockMed session from test_probe_induction.py,
standing in for the desktop app; writes land in the same HTTP test copy, and
the read-back decides. See docs/BACKEND_PARITY.md.
"""

# ruff: noqa: F811 - pytest fixtures imported from test_probe_induction

from __future__ import annotations

import sys

import pytest

from openadapt_flow.compiler import probing
from openadapt_flow.compiler.probing import (
    DeploymentProbeTarget,
    RunSignal,
    probe_and_induce,
    runtime_signal_of,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.runtime.replayer import Replayer
from tests.test_probe_induction import (  # noqa: F401 - pytest fixtures
    MockMedBackend,
    MockMedSession,
    MockMedVision,
    branch_traces,
    contract,
    mockmed,
)

SECTIONS = {
    "windows": {"kind": "windows", "agent_url": "http://127.0.0.1:5001"},
    "macos": {"kind": "macos", "macos_app": "TextEdit"},
    "linux": {"kind": "linux", "linux_app": "notes", "linux_window_title": "Notes"},
    "rdp": {"kind": "rdp", "rdp_host": "10.0.0.5"},
    "citrix": {
        "kind": "citrix",
        "rdp_window": "wfica32",
        "rdp_readiness_text": "Patient Notes",
    },
}


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_a_desktop_test_copy_is_probed_through_its_own_backend(
    tmp_path,
    mockmed,
    contract,
    monkeypatch,
    kind,
):
    import openadapt_flow.backends.factory as factory

    url, _db = mockmed
    built: list = []
    closed: list = []

    def build_backend(cfg, **kwargs):
        assert kwargs.get("page") is None
        built.append(cfg.kind)
        backend = MockMedBackend(MockMedSession(url))
        backend.close = lambda: closed.append(cfg.kind)  # type: ignore[attr-defined]
        return backend

    def replay(self, backend, workflow, bundle_dir, run_dir, params) -> RunSignal:
        # The scripted session's templates are told apart by its own vision;
        # everything else is the target's real code path.
        report = Replayer(
            backend, vision=MockMedVision(backend.session), poll_interval_s=0.01
        ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params)
        return RunSignal(runtime_signal_of(report), model_calls=report.model_calls)

    monkeypatch.setattr(factory, "build_backend", build_backend)
    monkeypatch.setattr(DeploymentProbeTarget, "_replay", replay)
    sync_api = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(
        sync_api, "sync_playwright", lambda: pytest.fail("no browser for a desktop")
    )

    reset = (
        "import urllib.request; urllib.request.urlopen(urllib.request.Request("
        f"{url + 'api/reset'!r}, data=b'{{}}', method='POST'), timeout=5).close()"
    )
    cfg = DeploymentConfig.model_validate(
        {
            "name": f"{kind}-test-copy",
            "backend": SECTIONS[kind],
            "test_copy": {
                "declared": True,
                "reset_command": [sys.executable, "-c", reset],
                "reward_contract": str(contract),
            },
        }
    )
    assert probing.probe_target_refusals(cfg) == []
    target = DeploymentProbeTarget(cfg)
    result = probe_and_induce(
        branch_traces(tmp_path),
        target=target,
        reward_contract=contract,
        work_dir=tmp_path / "probe",
    )

    assert result.certified is True
    assert result.probe_verdicts[0].certified == "conditional"
    assert result.probe_runs and set(built) == {kind}
    assert len(built) == len(result.probe_runs) == len(closed)
    assert sum(r.model_calls for r in result.probe_runs) == 0
