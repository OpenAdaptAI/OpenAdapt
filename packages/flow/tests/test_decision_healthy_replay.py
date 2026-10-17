"""Invariant 1: a healthy replay makes no decision-provider call.

The test replaces every provider entry point with one that raises, then
replays a healthy one-step save against the in-process MockMed system of
record. A second check runs the same replay in a fresh interpreter and
confirms the decision package was never imported.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.decision import feature_model, provider
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer_effects import (
    TARGET,
    WritingBackend,
    _dirs,
    _fault_server,
    _save_workflow,
    _vision_that_confirms_saved,
)

REPO = Path(__file__).resolve().parents[1]


class ProviderCalled(AssertionError):
    pass


def _explode(*_args, **_kwargs):
    raise ProviderCalled("a decision provider was reached from replay")


def _replay(tmp_path: Path):
    url, _db, stop = _fault_server()
    try:
        workflow = _save_workflow(
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                    timeout_s=2.0,
                )
            ]
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            WritingBackend(url),
            vision=_vision_that_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        return replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    finally:
        stop()


def test_healthy_replay_reaches_no_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(feature_model.FeatureModelProvider, "answer", _explode)
    monkeypatch.setattr(feature_model.FeatureModelProvider, "__init__", _explode)
    monkeypatch.setattr(provider.DecisionRequest, "__init__", _explode)
    with pytest.raises(ProviderCalled):
        provider.DecisionRequest(group_key="0" * 64, features={})

    report = _replay(tmp_path)

    assert report.success is True
    assert report.results[0].effect_verified is True
    assert report.model_calls == 0


def test_healthy_replay_never_imports_the_decision_package(tmp_path):
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tests.test_decision_healthy_replay import _replay\n"
        f"report = _replay(Path({str(tmp_path)!r}))\n"
        "assert report.success\n"
        "print(sorted(m for m in sys.modules if m == 'openadapt_flow.decision' or m.startswith('openadapt_flow.decision.')))\n"
    )
    # The test module itself imports the package; run the replay helper
    # through a copy that doesn't.
    script = tmp_path / "replay_only.py"
    source = (REPO / "tests" / "test_decision_healthy_replay.py").read_text()
    source = source.replace(
        "from openadapt_flow.decision import feature_model, provider\n", ""
    )
    script.write_text(source)
    code = code.replace(
        "from tests.test_decision_healthy_replay import _replay",
        "sys.path.insert(0, %r)\nfrom replay_only import _replay" % str(tmp_path),
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"
