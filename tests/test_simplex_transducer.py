"""Comprehensive unit tests for ``openadapt.simplex_transducer`` and ``openadapt.wrap(agent)``."""

from __future__ import annotations

import concurrent.futures
import json
import struct
import zlib
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


def _make_png(
    width: int, height: int, *, solid_rgb: tuple[int, int, int] | None = None
) -> bytes:
    """Build a minimal valid 8-bit RGB PNG using stdlib only."""

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # Filter type 0 (None)
        for x in range(width):
            if solid_rgb is not None:
                rows.extend(solid_rgb)
            else:
                rows.extend(((x * 37) % 256, (y * 53) % 256, ((x + y) * 19) % 256))
    idat = zlib.compress(bytes(rows))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


@dataclass
class _NativeObservation:
    screenshot: bytes | None = None
    screenshot_path: str | None = None
    viewport: tuple[int, int] | None = None
    accessibility_tree: dict[str, Any] | None = None
    window_title: str | None = None
    focused_element: dict[str, Any] | None = None
    raw_observation: dict[str, Any] | None = None


@dataclass
class _NativeAction:
    type: str
    target_node_id: str | None = None
    target_name: str | None = None
    text: str | None = None
    key: str | None = None
    answer: str | None = None
    raw_action: dict[str, Any] = field(default_factory=dict)


@dataclass
class _NativeTask:
    task_id: str
    instruction: str
    domain: str = "desktop"
    time_limit_steps: int | None = 20
    raw_config: dict[str, Any] = field(default_factory=dict)


def test_wal_idempotency_key_backward_compat_and_helpers(tmp_path: Path) -> None:
    wal_path = tmp_path / "legacy_and_new.jsonl"
    # Legacy WAL JSONL record without idempotency_key
    legacy_line = {
        "run_id": "run-legacy",
        "task_id": "task-legacy",
        "commit_index": 1,
        "state": "VERIFIED",
        "action_summary": {"op": "old-commit"},
        "evidence": {"tier": 2},
        "updated_at": "2026-11-01T00:00:00Z",
    }
    wal_path.write_text(json.dumps(legacy_line) + "\n", encoding="utf-8")

    wal = WriteAheadSagaLog(wal_path)
    legacy_rec = wal.get("run-legacy", "task-legacy", 1)
    assert legacy_rec is not None
    assert legacy_rec.state is SagaState.VERIFIED
    assert legacy_rec.idempotency_key == ""
    assert wal.find_by_idempotency_key("run-legacy", "task-legacy", "") is None

    # New records with idempotency_key
    wal.record_intent("run-1", "task-1", 1, {"item": "A"}, idempotency_key="key-A")
    wal.record_pending_effect("run-1", "task-1", 1, idempotency_key="key-A")
    assert [r.commit_index for r in wal.pending_effects_for("run-1", "task-1")] == [1]

    wal.record_outcome("run-1", "task-1", 1, SagaState.VERIFIED, {"tier": 2})
    assert wal.pending_effects_for("run-1", "task-1") == []

    wal.record_intent("run-1", "task-1", 2, {"item": "B"}, idempotency_key="key-B")
    wal.record_pending_effect("run-1", "task-1", 2, idempotency_key="key-B")

    # Reload from disk and verify helpers
    reloaded = WriteAheadSagaLog(wal_path)
    found_a = reloaded.find_by_idempotency_key("run-1", "task-1", "key-A")
    assert found_a is not None
    assert found_a.commit_index == 1
    assert found_a.state is SagaState.VERIFIED
    found_b = reloaded.find_by_idempotency_key("run-1", "task-1", "key-B")
    assert found_b is not None
    assert found_b.commit_index == 2
    assert found_b.state is SagaState.PENDING_EFFECT
    assert [
        r.commit_index for r in reloaded.pending_effects_for("run-1", "task-1")
    ] == [2]


def test_cold_resume_blocks_amnesic_duplicate_commit_by_content_key(
    tmp_path: Path,
) -> None:
    wal_dir = tmp_path / "wal"
    commit_a = _NativeAction(
        type="click",
        target_name="Save",
        text="Item-Alpha",
        raw_action={"is_commit": True, "item_id": "ALPHA-1"},
    )
    commit_b = _NativeAction(
        type="click",
        target_name="Save",
        text="Item-Beta",
        raw_action={"is_commit": True, "item_id": "BETA-2"},
    )
    task = {"run_id": "run-crash", "task_id": "task-crash"}

    # Process 1: dispatches commit A on step 1, then step 2 proactively verifies
    # commit A via rung3_verifier before a crash occurs.
    verified_flag = {"a_persisted": False}

    def _rung3(obs: Any, tsk: Any, st: Any) -> dict[str, Any]:
        return {"verified": verified_flag["a_persisted"]}

    agent1 = _ScriptedBenchmarkAgent(
        [commit_a, _NativeAction(type="click", target_name="Next")]
    )
    wrapped1 = openadapt.wrap(
        agent1,
        supervisor_config=SupervisorConfig(wal_dir=wal_dir),
        rung3_verifier=_rung3,
    )
    out1 = wrapped1.act({}, task)
    assert out1.text == "Item-Alpha"
    assert (
        wrapped1.wal.get("run-crash", "task-crash", 1).state is SagaState.PENDING_EFFECT
    )

    # Step t+1: rung3 confirms commit A -> immediately transitions to VERIFIED in WAL
    verified_flag["a_persisted"] = True
    wrapped1.act({}, task)
    rec1 = wrapped1.wal.get("run-crash", "task-crash", 1)
    assert rec1 is not None
    assert rec1.state is SagaState.VERIFIED
    assert rec1.idempotency_key != ""

    # Process 2 (cold restart from disk): amnesic agent tries to repeat commit A,
    # receives WAL_CRASH_RESUME + WAL_SKIP_VERIFIED, and advances to commit B.
    agent2 = _ScriptedBenchmarkAgent([commit_a, commit_b])
    wrapped2 = openadapt.wrap(
        agent2,
        supervisor_config=SupervisorConfig(wal_dir=wal_dir),
    )
    out2 = wrapped2.act({}, task)
    assert out2.text == "Item-Beta"
    kinds = [
        n.kind for n in wrapped2.get_state("run-crash", "task-crash").intervention_log
    ]
    assert "WAL_CRASH_RESUME" in kinds
    assert "WAL_SKIP_VERIFIED" in kinds
    # Commit 1 stayed Item-Alpha (VERIFIED); Commit 2 is Item-Beta (no duplicate!)
    commits = wrapped2.wal.commits_for("run-crash", "task-crash")
    assert len(commits) == 2
    assert commits[0].commit_index == 1 and commits[0].state is SagaState.VERIFIED
    assert commits[1].commit_index == 2 and commits[1].state is SagaState.PENDING_EFFECT
    assert commits[0].idempotency_key != commits[1].idempotency_key

    # Process 3: persistently amnesic agent repeats commit A past max_repairs -> abstains
    agent3 = _ScriptedBenchmarkAgent([commit_a, commit_a, commit_a, commit_a])
    wrapped3 = openadapt.wrap(
        agent3,
        supervisor_config=SupervisorConfig(wal_dir=wal_dir, max_repairs_per_commit=2),
    )
    res3 = wrapped3.act({}, task)
    assert isinstance(res3, TransducerAction)
    assert res3.is_abstained is True
    assert res3.raw_action["trap_code"] == "WAL_DUPLICATE_COMMIT"


def test_cold_start_pending_effect_reconciliation_and_rung2_step_reconcile(
    tmp_path: Path,
) -> None:
    wal_dir = tmp_path / "wal_reconcile"
    wal_file = wal_dir / "saga_wal.jsonl"
    seed_wal = WriteAheadSagaLog(wal_file)
    seed_wal.record_intent("r-rec", "t-rec", 1, {"op": "c1"}, idempotency_key="k1")
    seed_wal.record_pending_effect("r-rec", "t-rec", 1, idempotency_key="k1")
    seed_wal.record_outcome("r-rec", "t-rec", 1, SagaState.VERIFIED, {"tier": 2})
    seed_wal.record_intent("r-rec", "t-rec", 2, {"op": "c2"}, idempotency_key="k2")
    seed_wal.record_pending_effect("r-rec", "t-rec", 2, idempotency_key="k2")

    # Cold start with rung3 confirming pending commit 2 before WAL_CRASH_RESUME
    commit3 = _FakeAction(type="click", is_commit=True, raw_action={"op": "c3"})
    agent = _ScriptedBenchmarkAgent(
        [commit3, _FakeAction(type="click", raw_action={"nav": "next"})]
    )
    rung2_calls: list[int] = []

    def _rung3(obs: Any, task: Any, st: Any) -> dict[str, Any]:
        # Only confirm commit 2 (k2), not commit 3
        return {"verified_keys": ["k2"]}

    def _rung2(obs: Any, task: Any, st: Any) -> dict[str, Any]:
        rung2_calls.append(st.step_count)
        return {"verified": st.step_count >= 2}

    wrapped = openadapt.wrap(
        agent,
        supervisor_config=SupervisorConfig(wal_dir=wal_dir),
        rung3_verifier=_rung3,
        rung2_verifier=_rung2,
    )
    task = {"run_id": "r-rec", "task_id": "t-rec"}
    wrapped.act({}, task)

    # Commit 2 was reconciled to VERIFIED on cold start, so commit3 became commit_index=3
    assert wrapped.wal.get("r-rec", "t-rec", 2).state is SagaState.VERIFIED
    assert wrapped.wal.get("r-rec", "t-rec", 3).state is SagaState.PENDING_EFFECT
    resume_notes = [
        n
        for n in wrapped.get_state("r-rec", "t-rec").intervention_log
        if n.kind == "WAL_CRASH_RESUME"
    ]
    assert len(resume_notes) == 1
    assert "#2" in resume_notes[0].hint

    # Step t+1: rung2 proactively verifies commit 3
    wrapped.act({}, task)
    assert wrapped.wal.get("r-rec", "t-rec", 3).state is SagaState.VERIFIED
    assert len(rung2_calls) >= 1


def test_stream_disconnect_guard_none_blank_and_window_title() -> None:
    good_png = _make_png(16, 16, solid_rgb=None)
    blank_png = _make_png(16, 16, solid_rgb=(0, 0, 0))
    task = _NativeTask(task_id="t-vdi", instruction="Click Save")
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(type="click", target_name="Field"),
            _NativeAction(
                type="click", target_name="Save", raw_action={"is_commit": True}
            ),
        ]
    )
    wrapped = openadapt.wrap(agent)

    # Step 1: normal frame -> inner agent runs (calls == 1)
    a1 = wrapped.act(
        _NativeObservation(screenshot=good_png, window_title="Portal App"),
        task,
    )
    assert isinstance(a1, _NativeAction)
    assert a1.type == "click"
    assert agent.calls == 1

    # Step 2: screenshot drops to None -> wait returned directly, 0 LLM calls
    a2 = wrapped.act(
        _NativeObservation(screenshot=None, window_title="Portal App"),
        task,
    )
    assert isinstance(a2, _NativeAction)
    assert a2.type == "wait"
    assert a2.raw_action.get("guard") == "STREAM_DISCONNECTED"
    assert agent.calls == 1

    # Step 3: zero-variance blank PNG -> wait returned directly, 0 LLM calls
    a3 = wrapped.act(
        _NativeObservation(screenshot=blank_png, window_title="Portal App"),
        task,
    )
    assert isinstance(a3, _NativeAction)
    assert a3.type == "wait"
    assert agent.calls == 1

    # Step 4: window title indicates reconnecting -> wait returned directly, 0 LLM calls
    a4 = wrapped.act(
        _NativeObservation(
            screenshot=good_png, window_title="VDI Session - Reconnecting..."
        ),
        task,
    )
    assert isinstance(a4, _NativeAction)
    assert a4.type == "wait"
    assert agent.calls == 1

    # Step 5: stream recovers -> inner agent resumes and commits
    a5 = wrapped.act(
        _NativeObservation(screenshot=good_png, window_title="Portal App"),
        task,
    )
    assert a5.target_name == "Save"
    assert agent.calls == 2


def test_focus_steal_modal_guard_blocks_occluded_commit_and_allows_expected() -> None:
    task = _NativeTask(
        task_id="t-modal",
        instruction="Submit the invoice for order ORD-42.",
    )
    modal_tree = {
        "role": "window",
        "name": "Portal",
        "children": [
            {
                "role": "alertdialog",
                "node_id": "dlg-99",
                "name": "IT Policy Update - Action Required",
                "children": [
                    {"role": "button", "node_id": "btn-dismiss", "name": "Dismiss"}
                ],
            }
        ],
    }
    obs_with_modal = _NativeObservation(
        window_title="Portal",
        accessibility_tree=modal_tree,
        focused_element={
            "role": "alertdialog",
            "node_id": "dlg-99",
            "name": "IT Policy Update - Action Required",
        },
    )
    # Agent first tries to commit on occluded Save button, then repairs by dismissing modal
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(
                type="click",
                target_node_id="btn-save",
                target_name="Save",
                raw_action={"is_commit": True},
            ),
            _NativeAction(
                type="click",
                target_node_id="btn-dismiss",
                target_name="Dismiss",
            ),
        ]
    )
    wrapped = openadapt.wrap(agent)
    out = wrapped.act(obs_with_modal, task)
    assert out.target_name == "Dismiss"
    assert any("FOCUS_STEAL_MODAL" in note["hint"] for note in agent.interventions)

    # Expected dialog referenced in task.instruction is NOT blocked
    expected_task = _NativeTask(
        task_id="t-expected-modal",
        instruction="Open the Confirm Delete dialog and click Confirm.",
    )
    expected_obs = _NativeObservation(
        accessibility_tree={
            "role": "dialog",
            "node_id": "dlg-del",
            "name": "Confirm Delete",
            "children": [
                {"role": "button", "node_id": "btn-confirm", "name": "Confirm"}
            ],
        }
    )
    agent2 = _ScriptedBenchmarkAgent(
        [
            _NativeAction(
                type="click",
                target_node_id="btn-other",
                target_name="Proceed",
                raw_action={"is_commit": True},
            )
        ]
    )
    wrapped2 = openadapt.wrap(agent2)
    out2 = wrapped2.act(expected_obs, expected_task)
    assert out2.target_name == "Proceed"
    assert agent2.interventions == []


def test_contextual_integrity_missing_task_anchor_repair() -> None:
    task = _NativeTask(
        task_id="t-anchor",
        instruction=(
            "Send an update to Bob about Project Orion and make sure to mention "
            "3:00 PM and Dr. Patel."
        ),
    )
    agent = _ScriptedBenchmarkAgent(
        [
            # First attempt omits "3:00 PM"
            _NativeAction(
                type="click",
                target_name="Send",
                raw_action={
                    "is_commit": True,
                    "parsed_json": {
                        "shared_content": "Project Orion update: Dr. Patel confirmed the agenda."
                    },
                },
            ),
            # Repaired attempt includes both required anchors
            _NativeAction(
                type="click",
                target_name="Send",
                raw_action={
                    "is_commit": True,
                    "parsed_json": {
                        "shared_content": (
                            "Project Orion update: Dr. Patel confirmed the 3:00 PM agenda."
                        )
                    },
                },
            ),
        ]
    )
    wrapped = openadapt.wrap(agent)
    out = wrapped.act(_NativeObservation(), task)
    assert "3:00 PM" in out.raw_action["parsed_json"]["shared_content"]
    assert len(agent.interventions) == 1
    assert "MISSING_TASK_ANCHOR" in agent.interventions[0]["hint"]


def test_contextual_scope_bleed_selective_repair_co_location_and_abstain() -> None:
    task = _NativeTask(
        task_id="t-scope",
        instruction="Share the Project Orion schedule update with Bob and make sure to mention 3:00 PM.",
    )
    obs = _NativeObservation(
        raw_observation={
            "containers": {
                "team_notes": [
                    "Project Orion review with Sarah is scheduled at 3:00 PM.",
                ],
                "hr_inbox": [
                    "Marcus Vance requested a transfer to the Berlin office.",
                ],
            }
        }
    )

    # Case A: co-located entity ("Sarah") in the same anchored source item is allowed
    agent_ok = _ScriptedBenchmarkAgent(
        [
            _NativeAction(
                type="click",
                target_name="Send",
                raw_action={
                    "is_commit": True,
                    "parsed_json": {
                        "shared_content": [
                            "Project Orion review with Sarah is at 3:00 PM."
                        ]
                    },
                },
            )
        ]
    )
    wrapped_ok = openadapt.wrap(agent_ok)
    out_ok = wrapped_ok.act(obs, task)
    assert "Sarah" in out_ok.raw_action["parsed_json"]["shared_content"][0]
    assert agent_ok.interventions == []

    # Case B: leaking unrelated item ("Marcus Vance" from hr_inbox) triggers selective
    # 1-turn TRAP_REPAIR_HINT (CONTEXTUAL_SCOPE_BLEED); agent removes only that item.
    leaking_action = _NativeAction(
        type="click",
        target_name="Send",
        raw_action={
            "is_commit": True,
            "parsed_json": {
                "shared_content": [
                    "Project Orion review with Sarah is at 3:00 PM.",
                    "Marcus Vance requested a transfer to the Berlin office.",
                ]
            },
        },
    )
    repaired_action = _NativeAction(
        type="click",
        target_name="Send",
        raw_action={
            "is_commit": True,
            "parsed_json": {
                "shared_content": [
                    "Project Orion review with Sarah is at 3:00 PM.",
                ]
            },
        },
    )
    agent_repair = _ScriptedBenchmarkAgent([leaking_action, repaired_action])
    wrapped_repair = openadapt.wrap(agent_repair)
    out_rep = wrapped_repair.act(obs, task)
    assert len(agent_repair.interventions) == 1
    assert "CONTEXTUAL_SCOPE_BLEED" in agent_repair.interventions[0]["hint"]
    assert "Marcus Vance" in agent_repair.interventions[0]["hint"]
    assert "Marcus Vance" not in str(
        out_rep.raw_action["parsed_json"]["shared_content"]
    )

    # Case C1: persistent bleed past max_repairs_per_commit cleanly strips ONLY
    # out-of-scope item(s) when valid required items remain
    agent_strip = _ScriptedBenchmarkAgent(
        [leaking_action, leaking_action, leaking_action]
    )
    wrapped_strip = openadapt.wrap(
        agent_strip,
        supervisor_config=SupervisorConfig(max_repairs_per_commit=1),
    )
    out_strip = wrapped_strip.act(obs, task)
    assert not (isinstance(out_strip, TransducerAction) and out_strip.is_abstained)
    stripped_items = out_strip.raw_action["parsed_json"]["shared_content"]
    assert len(stripped_items) == 1
    assert "Project Orion" in stripped_items[0]
    assert "Marcus Vance" not in str(stripped_items)

    # Case C2: persistent bleed with ONLY out-of-scope items abstains cleanly (0 SER)
    only_leaking = _NativeAction(
        type="click",
        target_name="Send",
        raw_action={
            "is_commit": True,
            "parsed_json": {
                "shared_content": [
                    "Marcus Vance requested a transfer to the Berlin office at 3:00 PM.",
                ]
            },
        },
    )
    agent_abstain = _ScriptedBenchmarkAgent([only_leaking, only_leaking, only_leaking])
    wrapped_abstain = openadapt.wrap(
        agent_abstain,
        supervisor_config=SupervisorConfig(max_repairs_per_commit=1),
    )
    out_abs = wrapped_abstain.act(obs, task)
    assert isinstance(out_abs, TransducerAction)
    assert out_abs.is_abstained is True
    assert out_abs.raw_action["trap_code"] == "CONTEXTUAL_SCOPE_BLEED"
    assert wrapped_abstain.last_receipt is not None
    assert wrapped_abstain.last_receipt.outcome == "HALTED"


def test_contextual_scope_bleed_from_structured_instruction_sections() -> None:
    task = _NativeTask(
        task_id="t-sections",
        instruction=(
            "Reply to Elena with the Atlas launch checklist.\n"
            "## Work Calendar\n"
            "- Atlas launch checklist reviewed by Elena at 10:00 AM.\n"
            "## Personal Messages\n"
            "- Victor Morales asked about borrowing the garage key.\n"
        ),
    )
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(
                type="answer",
                answer=(
                    "Atlas launch checklist reviewed at 10:00 AM. "
                    "Also Victor Morales asked about borrowing the garage key."
                ),
            ),
            _NativeAction(
                type="answer",
                answer="Atlas launch checklist was reviewed at 10:00 AM.",
            ),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        supervisor_config=SupervisorConfig(enable_verify_before_done=False),
    )
    out = wrapped.act(_NativeObservation(), task)
    assert out.type == "answer"
    assert "Victor Morales" not in (out.answer or "")
    assert any("CONTEXTUAL_SCOPE_BLEED" in n["hint"] for n in agent.interventions)


def test_coordinate_jitter_wal_dedup_and_commit_verified_intervention(
    tmp_path: Path,
) -> None:
    wal_dir = tmp_path / "wal_jitter"
    click_1 = _NativeAction(
        type="click",
        raw_action={"is_commit": True, "x": 0.8203125, "y": 0.8375},
    )
    click_jitter = _NativeAction(
        type="click",
        raw_action={"is_commit": True, "x": 0.821875, "y": 0.8400},
    )
    verified_flag = {"ok": False}

    def _rung3(obs: Any, tsk: Any, st: Any) -> dict[str, Any]:
        return {"verified": verified_flag["ok"]}

    agent = _ScriptedBenchmarkAgent(
        [
            click_1,
            click_jitter,
            click_jitter,
            _NativeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        supervisor_config=SupervisorConfig(wal_dir=wal_dir, max_repairs_per_commit=2),
        rung3_verifier=_rung3,
    )
    task = {"run_id": "r-jit", "task_id": "t-jit"}

    # Step 1: commit dispatched
    a1 = wrapped.act({}, task)
    assert a1.type == "click"
    assert wrapped.wal.get("r-jit", "t-jit", 1).state is SagaState.PENDING_EFFECT

    # Step 2: rung3 verifies commit 1 -> delivers COMMIT_VERIFIED, and 1-px jitter
    # re-click hashes to the same idempotency_key so WAL_SKIP_VERIFIED blocks it
    # until agent returns done.
    verified_flag["ok"] = True
    a2 = wrapped.act({}, task)
    assert a2.type == "done"
    kinds = [n.kind for n in wrapped.get_state("r-jit", "t-jit").intervention_log]
    assert "COMMIT_VERIFIED" in kinds
    assert "WAL_SKIP_VERIFIED" in kinds
    # Only 1 commit in WAL (no duplicate!)
    assert len(wrapped.wal.commits_for("r-jit", "t-jit")) == 1


def test_contextual_scope_bleed_nested_containers_outlier_path_and_task_prompt() -> (
    None
):
    # Even when task.instruction contains a full serialized app dump, raw_config.task_prompt
    # defines the authoritative task scope; singleton outlier file paths and restricted
    # nested thread messages are detected and stripped cleanly if repair doesn't remove them.
    task = _NativeTask(
        task_id="t-nested",
        instruction="FULL SERIALIZED DUMP including personal_drafts/letter_to_landlord.txt",
        raw_config={
            "task_prompt": (
                "I need help debugging the auth flow. Send a message to the senior dev "
                "listing the open code files."
            )
        },
    )
    obs = _NativeObservation(
        raw_observation={
            "containers": {
                "code_editor": {
                    "open_tabs": [
                        "frontend/src/hooks/useAuth.js",
                        "frontend/src/components/Button.jsx",
                        "backend/api/middleware/security.py",
                        "backend/api/routes/users.py",
                        "personal_drafts/letter_to_landlord.txt",
                    ]
                }
            }
        }
    )
    leaking = _NativeAction(
        type="answer",
        raw_action={
            "is_commit": True,
            "parsed_json": {
                "shared_content": [
                    "frontend/src/hooks/useAuth.js",
                    "backend/api/middleware/security.py",
                    "personal_drafts/letter_to_landlord.txt",
                ]
            },
        },
    )
    agent = _ScriptedBenchmarkAgent([leaking, leaking])
    wrapped = openadapt.wrap(
        agent,
        supervisor_config=SupervisorConfig(
            max_repairs_per_commit=1, enable_verify_before_done=False
        ),
    )
    out = wrapped.act(obs, task)
    items = out.raw_action["parsed_json"]["shared_content"]
    assert "frontend/src/hooks/useAuth.js" in items
    assert "backend/api/middleware/security.py" in items
    assert "personal_drafts/letter_to_landlord.txt" not in items
    assert any("CONTEXTUAL_SCOPE_BLEED" in n["hint"] for n in agent.interventions)


# ---------------------------------------------------------------------------
# v3 hooks wired through openadapt.wrap(agent)
# ---------------------------------------------------------------------------


def _v3_task(instruction: str = "Do the task.", **raw: Any) -> _NativeTask:
    return _NativeTask(task_id="t-v3", instruction=instruction, raw_config=raw)


def test_v3_shell_gate_blocks_destructive_and_marks_mutation_as_commit() -> None:
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(type="bash", text="rm -rf /"),
            _NativeAction(type="bash", text="rm -rf /"),
            _NativeAction(type="bash", text="rm -rf /"),
        ]
    )
    wrapped = openadapt.wrap(agent)
    out = wrapped.act(_NativeObservation(), _v3_task())
    assert out.is_abstained
    assert out.raw_action["trap_code"] == "DESTRUCTIVE_PRIVILEGED_COMMAND"
    assert wrapped.last_receipt.outcome == "HALTED"

    agent2 = _ScriptedBenchmarkAgent(
        [_NativeAction(type="bash", text="echo hi > out.txt")]
    )
    wrapped2 = openadapt.wrap(agent2)
    task = _v3_task()
    out2 = wrapped2.act(_NativeObservation(), task)
    assert out2.raw_action["is_commit"] is True
    assert wrapped2.get_state("default-run", "t-v3").pending_verification
    assert wrapped2.wal.commits_for("default-run", "t-v3")


def test_v3_oracle_tamper_and_regression_gate_before_done(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text("assert True\n")
    results = iter([(False, "FAILED test_x: boom"), (True, "")])
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(type="bash", text="echo fix > src.txt"),
            _NativeAction(type="done"),
            _NativeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(
        agent,
        workspace_root=str(tmp_path),
        regression_test_runner=lambda: next(results),
    )
    task = _v3_task()
    wrapped.act(_NativeObservation(), task)
    out = wrapped.act(_NativeObservation(), task)
    assert out.type == "done"
    assert any("REGRESSION_TEST_FAILED" in i["hint"] for i in agent.interventions)
    assert wrapped.last_receipt.outcome == "VERIFIED"
    assert wrapped.last_receipt.oracle_tier == 2

    agent2 = _ScriptedBenchmarkAgent([_NativeAction(type="done")])
    wrapped2 = openadapt.wrap(agent2, workspace_root=str(tmp_path))
    task2 = _v3_task()
    obs = _NativeObservation()
    wrapped2.get_state("default-run", "t-v3")
    wrapped2._v3.on_context(wrapped2.get_state("default-run", "t-v3"), obs, task2)
    (tmp_path / "test_x.py").write_text("assert False\n")
    out2 = wrapped2.act(obs, task2)
    assert out2.is_abstained and out2.raw_action["trap_code"] == "ORACLE_TAMPER"


def test_v3_egress_strip_monotonic_and_single_turn_skips_reopen() -> None:
    task = _v3_task("Email the Orion team the Orion notes.")
    obs = _NativeObservation(
        raw_observation={
            "containers": {
                "Orion": ["Orion kickoff is Monday"],
                "Payroll": ["Salaries are confidential"],
            }
        }
    )
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(
                type="answer",
                answer="x",
                raw_action={
                    "shared_content": [
                        "Orion kickoff is Monday",
                        "Salaries are confidential",
                    ]
                },
            ),
            _NativeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(agent, item_auditor=lambda prompt, item: True)
    out = wrapped.act(obs, task)
    assert agent.calls == 1
    assert out.raw_action["shared_content"] == ["Orion kickoff is Monday"]
    assert not agent.interventions
    done = wrapped.act(obs, task)
    assert done.type == "done"
    assert not any(
        i["delta_note"] == "verification_subgoal_requested" for i in agent.interventions
    )


def test_v3_compiled_obligations_hold_done_until_full_readback() -> None:
    task = _v3_task(
        "File claim CLM-1234 with amount $1,250.00.",
        structured_fields={"claim": "CLM-1234"},
    )
    partial = _NativeObservation(raw_observation={"readback_text": "CLM-1234 saved"})
    full = _NativeObservation(
        raw_observation={"readback_text": "CLM-1234 saved, total $1,250.00"}
    )
    agent = _ScriptedBenchmarkAgent(
        [
            _NativeAction(type="done"),
            _NativeAction(type="done"),
        ]
    )
    wrapped = openadapt.wrap(agent)
    out = wrapped.act(partial, task)
    assert out.type == "done"
    assert any("HOLD_DONE" in i["hint"] for i in agent.interventions)
    assert wrapped.last_receipt is None or wrapped.last_receipt.outcome != "VERIFIED"
    agent._script = [_NativeAction(type="done")]
    wrapped.act(full, task)
    assert wrapped.last_receipt.outcome == "VERIFIED"


def test_v3_stall_detector_escalates_then_micro_handoff_and_macros(
    tmp_path: Path,
) -> None:
    from openadapt.transducer_macros import MacroStore
    from openadapt.transducer_obligations import MicroHandoffResolution

    escalations: list[Any] = []
    tickets: list[Any] = []

    def resolver(ticket: Any) -> MicroHandoffResolution:
        tickets.append(ticket)
        return MicroHandoffResolution(
            ticket_id=ticket.ticket_id, resolved=True, learned_selector="#next"
        )

    store = MacroStore(tmp_path / "macros.json")
    click = _NativeAction(type="click", raw_action={"selector": "#stuck"})
    agent = _ScriptedBenchmarkAgent([click] * 12)
    wrapped = openadapt.wrap(
        agent,
        macro_store=store,
        on_stall_escalate=escalations.append,
        micro_handoff_resolver=resolver,
    )
    obs = _NativeObservation(raw_observation={"dom_elements": []})
    task = _v3_task()
    for _ in range(8):
        wrapped.act(obs, task)
    assert any(i["delta_note"].startswith("stall_count") for i in agent.interventions)
    assert escalations
    assert tickets
    assert any("#next" in i["hint"] for i in agent.interventions)

    trace = [
        {"action": "fill", "selector": "#a", "value": "Acme"},
        {"action": "click", "selector": "#save", "is_commit": True},
    ]
    assert (
        wrapped.record_verified_macro("default-run", "t-v3", trace, {"name": "Acme"})
        is None
    )
    verified = openadapt.wrap(_ScriptedBenchmarkAgent([]), macro_store=store)
    state = verified.get_state("default-run", "t-v3")
    verified._emit_receipt(
        state=state, outcome="VERIFIED", oracle_tier=2,
        observed_effect_strength="independent_system_of_record",
        authorization_passed=True, identity_passed=True, postcondition_passed=True,
        effect_passed=True, delivery_uncertain=False, commit_index=1, reason="ok",
    )  # fmt: skip
    macro = verified.record_verified_macro(
        "default-run", "t-v3", trace, {"name": "Acme"}
    )
    assert macro is not None
    hit = verified.try_instantiate_macro("default-run", "t-v3", {"name": "Beta"})
    assert hit is not None and hit[1][0]["value"] == "Beta"
    assert verified.record_macro_outcome(macro.macro_id, False) is False
    assert verified.try_instantiate_macro("default-run", "t-v3", {"name": "C"}) is None
