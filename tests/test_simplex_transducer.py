"""Comprehensive unit tests for ``openadapt.simplex_transducer`` and ``openadapt.wrap(agent)``."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openadapt
from openadapt.simplex_transducer import (
    AppCartridge,
    AtomicCartridgeStore,
    DecisionRequest,
    DecisionResponse,
    Obligation,
    ObligationKind,
    OpaqueCredentialVault,
    SagaState,
    SimplexTransducer,
    SupervisorConfig,
    TransducerAction,
    TrustLevel,
    WriteAheadSagaLog,
)


@dataclass
class _FakeAction:
    type: str
    raw_action: dict[str, Any] = field(default_factory=dict)
    is_commit: bool = False
    entities: dict[str, Any] | None = None
    source_trust: str | None = None
    target_origin: str | None = None


class _ScriptedBenchmarkAgent:
    """Minimal BenchmarkAgent exposing .act and optional KV-safe intervention hook."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.interventions: list[dict[str, Any]] = []
        self.calls: int = 0
        self.reset_calls: int = 0

    def on_supervisor_intervention(
        self,
        *,
        hint: str,
        delta_note: str,
        fast_forward_obs: Any = None,
    ) -> None:
        self.interventions.append(
            {
                "hint": hint,
                "delta_note": delta_note,
                "fast_forward_obs": fast_forward_obs,
            }
        )

    def act(
        self,
        observation: Any,
        task: Any,
        history: list[Any] | None = None,
    ) -> Any:
        self.calls += 1
        if not self._script:
            return _FakeAction(type="done")
        return self._script.pop(0)

    def reset(self) -> None:
        self.reset_calls += 1


def test_supervisor_config_sha256_deterministic_and_sensitive() -> None:
    cfg_a = SupervisorConfig()
    cfg_b = SupervisorConfig()
    cfg_c = SupervisorConfig(max_repairs_per_commit=3)
    assert len(cfg_a.config_sha256) == 64
    assert cfg_a.config_sha256 == cfg_b.config_sha256
    assert cfg_a.config_sha256 != cfg_c.config_sha256


def test_wal_multi_commit_crash_recovery_skips_verified(tmp_path: Path) -> None:
    wal_path = tmp_path / "saga.jsonl"
    wal = WriteAheadSagaLog(wal_path)

    # Commit 1: verified
    wal.record_intent("run-1", "task-1", 1, {"op": "commit-1"})
    wal.record_pending_effect("run-1", "task-1", 1)
    wal.record_outcome("run-1", "task-1", 1, SagaState.VERIFIED, {"tier": 2})

    # Commit 2: verified
    wal.record_intent("run-1", "task-1", 2, {"op": "commit-2"})
    wal.record_pending_effect("run-1", "task-1", 2)
    wal.record_outcome("run-1", "task-1", 2, SagaState.VERIFIED, {"tier": 2})

    # Commit 3: crashed mid-flight in PENDING_EFFECT
    wal.record_intent("run-1", "task-1", 3, {"op": "commit-3"})
    wal.record_pending_effect("run-1", "task-1", 3)

    # Simulate partial crash line at EOF
    with wal_path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "run-1", "truncated')

    # Recover in a fresh WAL instance
    recovered = WriteAheadSagaLog(wal_path)
    assert recovered.is_commit_verified("run-1", "task-1", 1) is True
    assert recovered.is_commit_verified("run-1", "task-1", 2) is True
    assert recovered.is_commit_verified("run-1", "task-1", 3) is False
    assert recovered.resume_point("run-1", "task-1") == 3
    pending = recovered.pending_reconcile("run-1", "task-1")
    assert len(pending) == 1
    assert pending[0].commit_index == 3

    # Transition commit 3 to COMPENSATED_UNDO
    recovered.record_outcome("run-1", "task-1", 3, SagaState.COMPENSATED_UNDO)
    assert recovered.get("run-1", "task-1", 3).state is SagaState.COMPENSATED_UNDO


def test_isolation_mode_iid_ephemeral_vs_continual_persistent(tmp_path: Path) -> None:
    cart_file = tmp_path / "cartridges.json"
    cart = AppCartridge(
        cartridge_id="surface-a",
        reopen_paths={"task-1": [{"type": "click", "raw_action": {"id": "nav-1"}}]},
        micro_skills={"reopen:task-1": {"steps": [{"type": "click"}]}},
    )

    # iid_ephemeral wipes learned cartridges on reset()
    agent_iid = _ScriptedBenchmarkAgent([])
    wrapped_iid = openadapt.wrap(
        agent_iid,
        supervisor_config=SupervisorConfig(
            isolation_mode="iid_ephemeral",
            cartridge_path=cart_file,
        ),
        cartridge=cart,
    )
    assert wrapped_iid.cartridge_store.get("surface-a") is not None
    wrapped_iid.reset()
    assert wrapped_iid.cartridge_store.get("surface-a") is None
    assert agent_iid.reset_calls == 1

    # continual_persistent preserves cartridges across reset()
    agent_cont = _ScriptedBenchmarkAgent([])
    wrapped_cont = openadapt.wrap(
        agent_cont,
        supervisor_config=SupervisorConfig(
            isolation_mode="continual_persistent",
            cartridge_path=cart_file,
        ),
        cartridge=cart,
    )
    assert wrapped_cont.cartridge_store.get("surface-a") is not None
    wrapped_cont.reset()
    preserved = wrapped_cont.cartridge_store.get("surface-a")
    assert preserved is not None
    assert "reopen:task-1" in preserved.micro_skills


def test_flock_concurrent_cartridge_merge(tmp_path: Path) -> None:
    cart_file = tmp_path / "shared_cartridges.json"
    num_workers = 12

    def _worker(worker_id: int) -> None:
        store = AtomicCartridgeStore(cart_file)
        store.merge_cartridge(
            AppCartridge(
                cartridge_id="shared-surface",
                commit_signatures=[{"sig_id": f"sig-{worker_id}"}],
                reopen_paths={f"path-{worker_id}": [{"step": worker_id}]},
                micro_skills={f"skill-{worker_id}": {"worker": worker_id}},
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [pool.submit(_worker, i) for i in range(num_workers)]
        for fut in futures:
            fut.result()

    final = AtomicCartridgeStore(cart_file).get("shared-surface")
    assert final is not None
    assert len(final.micro_skills) == num_workers
    assert len(final.reopen_paths) == num_workers
    assert len(final.commit_signatures) == num_workers


def test_thread_safe_episode_state_isolation_across_workers() -> None:
    class _EchoAgent:
        def act(self, obs: dict[str, Any], task: dict[str, Any], history=None):
            return _FakeAction(
                type="click",
                is_commit=True,
                entities={"record_id": task["entities"]["record_id"]},
            )

    wrapped = openadapt.wrap(_EchoAgent())

    def _run_worker(idx: int) -> tuple[str, str]:
        task = {
            "run_id": f"run-{idx}",
            "task_id": f"task-{idx}",
            "entities": {"record_id": f"REC-{idx:04d}"},
        }
        obs = {"surface": f"surf-{idx}"}
        action = wrapped.act(obs, task)
        assert action.is_commit is True
        st = wrapped.get_state(f"run-{idx}", f"task-{idx}")
        return st.observed_entities["record_id"].value, f"REC-{idx:04d}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(_run_worker, range(12)))

    for actual, expected in results:
        assert actual == expected


def test_trap_repair_hint_self_repair_exact_code_and_kv_safe_history() -> None:
    # Agent first proposes wrong <4-char code ("OS" instead of "OD"), then repairs to "OD".
    agent = _ScriptedBenchmarkAgent(
        [
            _FakeAction(
                type="click",
                is_commit=True,
                entities={"laterality": "OS", "record_id": "REC-104"},
            ),
            _FakeAction(
                type="click",
                is_commit=True,
                entities={"laterality": "OD", "record_id": "REC-104"},
            ),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        pinned={"laterality": "OD", "record_id": "REC-104"},
    )
    caller_history = [{"role": "user", "content": "initial prompt"}]
    history_snapshot = list(caller_history)

    action = wrapped.act(
        {"surface": "editor"},
        {"run_id": "r1", "task_id": "t1"},
        caller_history,
    )
    assert action.entities == {"laterality": "OD", "record_id": "REC-104"}
    assert len(agent.interventions) == 1
    assert "TRAP_REPAIR_HINT" in agent.interventions[0]["hint"]
    assert "IDENTITY_MISMATCH" in agent.interventions[0]["hint"]
    # Caller's history prefix was never mutated
    assert caller_history == history_snapshot


def test_hold_done_draft_to_submitted_repair_and_cartridge_compile(
    tmp_path: Path,
) -> None:
    cart_file = tmp_path / "cart.json"
    agent = _ScriptedBenchmarkAgent(
        [
            # Turn 1: commit action
            _FakeAction(type="click", is_commit=True, raw_action={"op": "save"}),
            # Turn 2: agent tries DONE -> held for VERIFICATION_SUBGOAL -> agent reopens
            _FakeAction(type="done"),
            _FakeAction(type="click", raw_action={"nav": "reopen_record"}),
            # Turn 3: reopened frame shows Draft -> agent tries DONE -> HOLD_DONE_REPAIR -> agent submits
            _FakeAction(type="done"),
            _FakeAction(type="click", is_commit=True, raw_action={"op": "finalize"}),
            # Turn 4: reopened frame shows Submitted (non-editable, non-toast) -> DONE verified!
            _FakeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        supervisor_config=SupervisorConfig(
            isolation_mode="continual_persistent",
            cartridge_path=cart_file,
        ),
    )
    task = {
        "run_id": "r-draft",
        "task_id": "t-draft",
        "obligations": [
            Obligation(
                obligation_id="ob-1",
                kind=ObligationKind.RECORD,
                target="rec-1",
            )
        ],
    }

    # Turn 1: commit
    a1 = wrapped.act({"remaining_steps": 10, "surface": "s1"}, task)
    assert a1.is_commit is True

    # Turn 2: DONE held -> VERIFICATION_SUBGOAL -> reopen action
    a2 = wrapped.act({"remaining_steps": 9, "surface": "s1"}, task)
    assert a2.type == "click"
    assert any(
        "VERIFICATION_SUBGOAL" in note["delta_note"] or "HOLD_DONE" in note["hint"]
        for note in agent.interventions
    )

    # Turn 3: reopened frame shows Draft -> HOLD_DONE_REPAIR -> finalize action
    a3 = wrapped.act(
        {
            "remaining_steps": 8,
            "surface": "s1",
            "verification_frame": {
                "is_editable": False,
                "is_toast": False,
                "has_delta": True,
                "record_status": "Draft",
            },
        },
        task,
    )
    assert a3.raw_action.get("op") == "finalize"

    # Turn 4: reopened frame shows Submitted (separate_session) -> DONE released as VERIFIED
    a4 = wrapped.act(
        {
            "remaining_steps": 7,
            "surface": "s1",
            "verification_frame": {
                "is_editable": False,
                "is_toast": False,
                "has_delta": True,
                "record_status": "Submitted",
                "separate_session": True,
            },
        },
        task,
    )
    assert a4.type == "done"
    assert wrapped.last_receipt is not None
    assert wrapped.last_receipt.outcome == "VERIFIED"

    # Successful reopen path was compiled into AppCartridge
    compiled = wrapped.cartridge_store.get("s1")
    assert compiled is not None
    assert "t-draft" in compiled.reopen_paths

    # Receipt converts cleanly to openadapt_types.ExecuteEvidenceReceiptV1
    v1_receipt = wrapped.last_receipt.to_execute_evidence_receipt_v1()
    assert v1_receipt.oracle_tier == 1
    assert v1_receipt.contracts.effect_passed is True


def test_done_low_budget_releases_completed_unverified() -> None:
    agent = _ScriptedBenchmarkAgent(
        [
            _FakeAction(type="click", is_commit=True),
            _FakeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(agent)
    task = {"run_id": "r-low", "task_id": "t-low"}
    wrapped.act({"remaining_steps": 3}, task)
    done_action = wrapped.act({"remaining_steps": 2}, task)
    assert done_action.type == "done"
    assert wrapped.last_receipt is not None
    assert wrapped.last_receipt.outcome == "COMPLETED_UNVERIFIED"
    v1 = wrapped.last_receipt.to_execute_evidence_receipt_v1()
    assert v1.outcome.value == "reconciliation_required"
    assert v1.delivery_uncertain is True


def test_decision_resolver_none_abstains_zero_ser_vs_avc_h() -> None:
    # Agent repeats wrong ID past max_repairs_per_commit (2)
    bad_action = _FakeAction(
        type="click",
        is_commit=True,
        entities={"record_id": "WRONG-999"},
    )
    agent_no_resolver = _ScriptedBenchmarkAgent([bad_action, bad_action, bad_action])
    wrapped_abstain = openadapt.wrap(
        agent_no_resolver,
        pinned={"record_id": "RIGHT-101"},
        decision_resolver=None,
    )
    res = wrapped_abstain.act({}, {"run_id": "r-abs", "task_id": "t-abs"})
    assert isinstance(res, TransducerAction)
    assert res.type == "error"
    assert res.raw_action["outcome"] == "abstained"
    assert res.raw_action["reported_success"] is False
    assert res.reported_success is False
    assert wrapped_abstain.last_receipt is not None
    assert wrapped_abstain.last_receipt.outcome == "HALTED"
    # 0 SER: WAL has zero commits recorded
    assert wrapped_abstain.wal.commits_for("r-abs", "t-abs") == []

    # With AVC_H decision_resolver: human_touches += 1 and replacement action succeeds
    resolver_calls: list[DecisionRequest] = []

    def _resolver(req: DecisionRequest) -> DecisionResponse:
        resolver_calls.append(req)
        return DecisionResponse(
            approved=True,
            replacement_action=_FakeAction(
                type="click",
                is_commit=True,
                entities={"record_id": "RIGHT-101"},
            ),
            reason="operator corrected record_id",
        )

    agent_with_resolver = _ScriptedBenchmarkAgent([bad_action, bad_action, bad_action])
    wrapped_avc = openadapt.wrap(
        agent_with_resolver,
        pinned={"record_id": "RIGHT-101"},
        decision_resolver=_resolver,
    )
    approved_action = wrapped_avc.act({}, {"run_id": "r-avc", "task_id": "t-avc"})
    assert approved_action.entities == {"record_id": "RIGHT-101"}
    assert len(resolver_calls) == 1
    assert wrapped_avc.get_state("r-avc", "t-avc").human_touches == 1


def test_indirect_prompt_injection_blocked() -> None:
    injected = _FakeAction(
        type="click",
        is_commit=True,
        source_trust=TrustLevel.UNTRUSTED_DATA.value,
        target_origin="https://evil.example",
    )
    agent = _ScriptedBenchmarkAgent([injected])
    wrapped = openadapt.wrap(agent)
    task = {
        "run_id": "r-inj",
        "task_id": "t-inj",
        "trusted_origins": ["https://portal.internal"],
        "entities": {"record_id": "REC-555"},
    }
    # Untrusted observation attempting to poison trusted anchor must be ignored
    obs = {
        "origin": "https://evil.example",
        "trust_level": TrustLevel.UNTRUSTED_DATA.value,
        "entities": {"record_id": "POISON-000"},
    }
    res = wrapped.act(obs, task)
    assert isinstance(res, TransducerAction)
    assert res.type == "error"
    assert res.raw_action["outcome"] == "abstained"
    assert res.raw_action["trap_code"] == "UNTRUSTED_INSTRUCTION_ORIGIN"
    state = wrapped.get_state("r-inj", "t-inj")
    assert state.observed_entities["record_id"].value == "REC-555"


def test_vault_non_leakage_in_state_wal_and_receipts(tmp_path: Path) -> None:
    secret_val = "SuperSecretPass!987654"
    vault = OpaqueCredentialVault()
    handle = vault.put("portal_pw", secret_val)
    assert handle == "vault://portal_pw"

    # Secret resolves strictly inside low-level callback
    received: list[str] = []
    login_ok = vault.resolve_for_callback(
        handle, lambda raw: (received.append(raw), True)[1]
    )
    assert login_ok is True
    assert received == [secret_val]

    # Even if an agent accidentally puts the raw secret in an action or observation,
    # it must never appear in EpisodeState, WAL, interventions, or receipts.
    wal_file = tmp_path / "vault_wal.jsonl"
    agent = _ScriptedBenchmarkAgent(
        [
            _FakeAction(
                type="click",
                is_commit=True,
                raw_action={"password_echo": secret_val},
            ),
            _FakeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        vault=vault,
        wal=WriteAheadSagaLog(wal_file),
    )
    task = {"run_id": "r-v", "task_id": "t-v"}
    wrapped.act({"dirty_fields": {"pw": secret_val}, "remaining_steps": 2}, task)
    wrapped.act({"remaining_steps": 2}, task)

    wal_text = wal_file.read_text(encoding="utf-8")
    assert secret_val not in wal_text
    assert handle in wal_text

    state = wrapped.get_state("r-v", "t-v")
    assert secret_val not in repr(state)
    assert wrapped.last_receipt is not None
    assert secret_val not in repr(wrapped.last_receipt.to_dict())


def test_openadapt_wrap_routes_act_and_preserves_callable_and_run() -> None:
    # 1. BenchmarkAgent with .act -> SimplexTransducer
    agent = _ScriptedBenchmarkAgent([_FakeAction(type="done")])
    wrapped_agent = openadapt.wrap(
        agent,
        rung3_verifier=lambda obs, task, st: {"verified": True},
    )
    assert isinstance(wrapped_agent, SimplexTransducer)
    out = wrapped_agent.act({}, {"run_id": "r-wrap", "task_id": "t-wrap"})
    assert out.type == "done"
    assert wrapped_agent.last_receipt is not None
    assert wrapped_agent.last_receipt.outcome == "VERIFIED"
    v1 = wrapped_agent.last_receipt.to_execute_evidence_receipt_v1()
    assert v1.outcome.value == "verified"
    assert v1.oracle_tier == 2

    # 2. Callable still uses wrap_sdk callable wrapper
    def _save(record_id: str) -> dict[str, Any]:
        return {"saved": True, "record_id": record_id}

    safe_save = openadapt.wrap(_save, pinned={"record_id": "R-1"})
    assert safe_save(record_id="R-1") == {"saved": True, "record_id": "R-1"}

    # 3. Object with .run still uses WrappedAgentProxy
    class _Runner:
        def run(self, x: int) -> int:
            return x + 1

    wrapped_runner = openadapt.wrap(_Runner())
    assert wrapped_runner.run(4) == 5


def test_language_agnostic_boundary_detection_wire_and_cartridge() -> None:
    cart = AppCartridge(
        cartridge_id="opaque-ui",
        commit_signatures=[{"role": "button", "bounds_hash": "abc123"}],
    )
    agent = _ScriptedBenchmarkAgent(
        [
            # Wire mutation boundary
            _FakeAction(type="click", raw_action={"http_method": "POST"}),
            # Cartridge structural signature boundary (no English button regexes)
            _FakeAction(
                type="click",
                raw_action={"role": "button", "bounds_hash": "abc123"},
            ),
        ]
    )
    wrapped = openadapt.wrap(agent, cartridge=cart)
    task = {"run_id": "r-b", "task_id": "t-b"}
    wrapped.act({}, task)
    wrapped.act({}, task)
    commits = wrapped.wal.commits_for("r-b", "t-b")
    assert len(commits) == 2
    assert all(c.state is SagaState.PENDING_EFFECT for c in commits)
