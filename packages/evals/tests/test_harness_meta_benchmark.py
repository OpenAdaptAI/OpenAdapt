"""Targeted, fully-mocked tests for the lightweight meta-benchmark harness.

No flow extra, no VM, no browser, no network, no $ -- every heavy collaborator
is injected as a fake. Covers:

- the mock WAA adapter (via the shim) and a hand-rolled mock env both satisfy
  the ``runtime_checkable`` :class:`Environment` protocol;
- ``run_meta`` on a mock env produces a correct metrics row (ground truth from
  the env verifier, not the policy self-report);
- ``verify()`` delegates to the verifier registry AND to a native effect
  verifier;
- the Inspect eval-log export round-trips;
- the OSWorld / BrowserGym stubs raise ``NotImplementedError`` cleanly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openadapt_types import BenchmarkAction, BenchmarkObservation

from openadapt_evals.adapters.waa.mock import WAAMockAdapter
from openadapt_evals.evaluation.verifier_registry import (
    TaskVerifierRegistry,
    VerificationResult,
)
from openadapt_evals.harness import (
    BenchmarkAdapterEnvironment,
    BrowserGymAdapter,
    Environment,
    MetaMetricsRow,
    MetaTask,
    MockMedAdapter,
    OSWorldAdapter,
    PolicyOutcome,
    from_inspect_eval_log,
    make_agent_policy,
    run_meta,
    to_inspect_eval_log,
    write_inspect_eval_log,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEnv:
    """A minimal hand-rolled env that structurally satisfies ``Environment``."""

    name = "mockmed"

    def __init__(self, *, success=True, verdict="confirmed"):
        self._success = success
        self._verdict = verdict
        self.reset_called = False

    def reset(self, task):
        self.reset_called = True
        return BenchmarkObservation(raw_observation={"reset": True})

    def observe(self):
        return BenchmarkObservation(raw_observation={})

    def act(self, action):
        return BenchmarkObservation(raw_observation={}), action.type == "done", {}

    def verify(self, task):
        return VerificationResult(
            success=self._success,
            score=1.0 if self._success else 0.0,
            details={"source": "effect_verifier", "effect_verdict": self._verdict},
        )

    def close(self):
        pass


class _ScriptedAgent:
    """A BenchmarkAgent-like that types then declares done."""

    mode = "scripted"

    def __init__(self):
        self._i = 0
        self._last_step_logs = {"cost_usd": 0.001}

    def reset(self):
        self._i = 0

    def act(self, obs, task, history=None):
        self._i += 1
        if self._i == 1:
            return BenchmarkAction(type="type", text="hello")
        return BenchmarkAction(type="done")


def _meta_task(**kw):
    kw.setdefault("task_id", "t1")
    kw.setdefault("instruction", "do the thing")
    kw.setdefault("domain", "web")
    return MetaTask(**kw)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_mock_waa_adapter_shim_conforms_to_environment():
    env = BenchmarkAdapterEnvironment(WAAMockAdapter(num_tasks=3))
    assert isinstance(env, Environment)


def test_hand_rolled_mock_env_conforms_to_environment():
    assert isinstance(_FakeEnv(), Environment)


def test_incomplete_object_does_not_conform():
    incomplete = SimpleNamespace(reset=lambda t: None, verify=lambda t: None)
    assert not isinstance(incomplete, Environment)


# ---------------------------------------------------------------------------
# run_meta produces a correct row
# ---------------------------------------------------------------------------


def test_run_meta_row_uses_env_verifier_not_self_report(tmp_path):
    # Policy CLAIMS success; env verifier says FAIL -> row must follow the verifier.
    env = _FakeEnv(success=False, verdict="refuted")
    task = _meta_task(env="mockmed")

    def lying_policy(env, task, obs):
        return PolicyOutcome(
            reported_success=True,
            model_calls=0,
            structural_rung_counts={"primary": 4},
            num_steps=4,
            cost_usd=0.0,
        )

    jsonl = tmp_path / "rows.jsonl"
    row = run_meta(env, task, lying_policy, mode="replay", jsonl_path=jsonl)

    assert isinstance(row, MetaMetricsRow)
    assert env.reset_called
    assert row.env == "mockmed"
    assert row.task_id == "t1"
    assert row.mode == "replay"
    assert row.replay_success is False           # ground truth from verify()
    assert row.reported_success is True          # policy self-report preserved
    assert row.effect_verdict == "refuted"
    assert row.structural_rung_rate == 1.0       # 4 fires / 4 steps
    assert row.model_calls == 0
    assert row.wall_ms >= 0.0
    assert row.verifier_source == "effect_verifier"

    # Emitted exactly one JSONL row with the required schema keys.
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    for key in (
        "env", "task_id", "mode", "replay_success", "structural_rung_rate",
        "model_calls", "effect_verdict", "wall_ms", "cost_usd",
    ):
        assert key in d


def test_run_meta_with_agent_policy_over_adapter_env():
    env = BenchmarkAdapterEnvironment(WAAMockAdapter(num_tasks=1))
    tasks = env.adapter.list_tasks()
    task = MetaTask.from_benchmark_task(tasks[0], env="waa")
    policy = make_agent_policy(_ScriptedAgent(), max_steps=5)

    row = run_meta(env, task, policy)
    assert row.env == "waa"
    assert row.mode == "scripted"
    assert row.num_steps == 2                     # type, then done
    assert row.reported_success is True
    assert row.cost_usd == pytest.approx(0.002, abs=1e-6)  # 2 steps * 0.001
    assert isinstance(row.replay_success, bool)


def test_run_meta_survives_policy_exception():
    env = _FakeEnv(success=True)
    task = _meta_task(env="mockmed")

    def boom(env, task, obs):
        raise RuntimeError("policy blew up")

    row = run_meta(env, task, boom, mode="replay")
    assert "policy blew up" in (row.error or "")
    # verify() still ran -> ground truth still recorded.
    assert row.replay_success is True


# ---------------------------------------------------------------------------
# verify() delegation
# ---------------------------------------------------------------------------


def test_verify_delegates_to_registry_when_task_names_a_verifier():
    reg = TaskVerifierRegistry()

    calls = {}

    @reg.register("my_check")
    def _v(adapter):
        calls["adapter"] = adapter
        return VerificationResult(success=True, score=1.0, details={"k": "v"})

    adapter = WAAMockAdapter(num_tasks=1)
    env = BenchmarkAdapterEnvironment(adapter, verifier_registry=reg)
    task = MetaTask.from_benchmark_task(
        adapter.list_tasks()[0], env="waa", verifier="my_check"
    )

    result = env.verify(task)
    assert result.success is True
    assert result.details["source"] == "verifier_registry"
    assert result.details["verifier_key"] == "my_check"
    assert calls["adapter"] is adapter            # registry got the raw adapter


def test_verify_falls_back_to_native_evaluate_without_registry_key():
    adapter = WAAMockAdapter(num_tasks=1)
    env = BenchmarkAdapterEnvironment(adapter, verifier_registry=TaskVerifierRegistry())
    task = MetaTask.from_benchmark_task(adapter.list_tasks()[0], env="waa")

    # No actions taken -> mock evaluate() reports failure via adapter.evaluate().
    result = env.verify(task)
    assert result.details["source"] == "adapter.evaluate"
    assert isinstance(result.success, bool)


def test_verify_delegates_to_native_effect_verifier():
    # A fake flow EffectVerifier: verify() ignores args, returns a CONFIRMED-like.
    fake_verdict = SimpleNamespace(
        verdict=SimpleNamespace(value="confirmed"),
        confirmed=True,
        substrate="rest",
        reason="record present exactly once",
        observed_count=1,
        expected_count=1,
    )
    fake_verifier = SimpleNamespace(
        capture_pre_state=lambda: SimpleNamespace(reachable=True, records=[]),
        verify=lambda expected, before: fake_verdict,
    )
    env = MockMedAdapter(
        verifier=fake_verifier,
        backend=SimpleNamespace(screenshot=lambda: b"", viewport=(800, 600)),
        effect_builder=lambda task: {"kind": "record_written"},
    )
    task = _meta_task(env="mockmed", evaluation_spec={"kind": "record_written"})
    env.reset(task)
    result = env.verify(task)
    assert result.success is True
    assert result.details["effect_verdict"] == "confirmed"
    assert result.details["substrate"] == "rest"


# ---------------------------------------------------------------------------
# Inspect eval-log round-trip
# ---------------------------------------------------------------------------


def test_inspect_eval_log_round_trips(tmp_path):
    rows = [
        MetaMetricsRow(
            env="mockmed", task_id="t1", mode="replay", replay_success=True,
            structural_rung_rate=1.0, model_calls=0, effect_verdict="confirmed",
            wall_ms=12.3, cost_usd=0.0,
        ),
        MetaMetricsRow(
            env="openemr", task_id="t2", mode="hybrid", replay_success=False,
            structural_rung_rate=0.5, model_calls=7, effect_verdict="refuted",
            wall_ms=45.6, cost_usd=0.03,
        ),
    ]
    doc = to_inspect_eval_log(rows, task_name="meta", model="flow/compiled")
    assert doc["version"] == 2
    assert doc["results"]["total_samples"] == 2
    assert doc["results"]["scores"][0]["metrics"]["accuracy"]["value"] == 0.5

    # Round-trip through JSON text.
    reloaded = json.loads(json.dumps(doc))
    back = from_inspect_eval_log(reloaded)
    assert len(back) == 2
    assert back[0]["task_id"] == "t1"
    assert back[0]["replay_success"] is True
    assert back[0]["effect_verdict"] == "confirmed"
    assert back[1]["task_id"] == "t2"
    assert back[1]["replay_success"] is False
    assert back[1]["model_calls"] == 7

    # write_inspect_eval_log -> file -> read back.
    path = write_inspect_eval_log(rows, tmp_path / "run.eval.json", task_name="meta")
    on_disk = json.loads(path.read_text())
    assert from_inspect_eval_log(on_disk)[0]["env"] == "mockmed"


# ---------------------------------------------------------------------------
# Phase-2 stubs
# ---------------------------------------------------------------------------


def test_external_adapters_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        OSWorldAdapter()
    with pytest.raises(NotImplementedError):
        BrowserGymAdapter()


def test_external_adapters_expose_environment_shaped_methods():
    # Even unconstructable, the classes advertise the Environment method names so
    # phase 2 has a concrete surface to fill in.
    for cls in (OSWorldAdapter, BrowserGymAdapter):
        for meth in ("reset", "observe", "act", "verify", "close"):
            assert callable(getattr(cls, meth))
