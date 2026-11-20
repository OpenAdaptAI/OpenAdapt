"""Synthetic program delivery recovery through actual fresh/resume entrypoints."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from openadapt_flow.ir import (
    ActionKind,
    LoopSpec,
    ParamKind,
    ParamSpec,
    ProgramGraph,
    Relation,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.durable import ApprovalRequired, StateDiverged, resume
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.authority import DurableAuthorityBusy
from openadapt_flow.runtime.durable.continuation import ContinuationBusy
from openadapt_flow.runtime.durable.controller import DurableRun
from openadapt_flow.runtime.durable.program_checkpoint import ProgramDeliveryCursor
from openadapt_flow.runtime.effects import SqlRecordVerifier
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.runtime.replayer import Replayer
from tests.test_durable_first_local_delivery import (
    LegInterrupted,
    LocalRun,
    WritingBackend,
)
from tests.test_durable_runtime import _approval
from tests.test_replayer import FakeBackend


class LocalProgramRun(LocalRun):
    """Own fake writer, independent SQLite reader and a compiled program graph."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        step = self.workflow.steps[0]
        self.workflow = Workflow(
            name="synthetic-local-program-delivery",
            program=ProgramGraph(
                entry="write-state",
                states={
                    "write-state": State(
                        id="write-state",
                        kind=StateKind.ACTION,
                        step=step,
                        transitions=[Transition(target="done")],
                    ),
                    "done": State(
                        id="done", kind=StateKind.TERMINAL, outcome="success"
                    ),
                },
            ),
        )
        self.workflow.save(self.bundle)


def test_first_program_input_has_exact_program_write_ahead(tmp_path):
    local = LocalProgramRun(tmp_path)
    at_input = []
    backend = WritingBackend(
        local, before_write=lambda: at_input.append(local.store.read_pending())
    )

    report = local.run(backend)

    assert report.success is True
    assert backend.actions == [("press", "A")]
    assert local.rows() == [("write",)]
    assert len(at_input) == 1
    pause = at_input[0]
    assert pause is not None, "program input occurred before its write-ahead pause"
    assert pause.program is True
    assert pause.state_id == "write-state"
    assert pause.program_frames[-1].state_id == "write-state"
    assert pause.program_checkpoint_seq == 0
    assert pause.delivery_uncertainty.operation == "first_local_delivery"


# The same write state occurs in a nested subflow on every loop row. Selection
# changes the fake system target; the independent SQL verifier reads its result.


class NestedProgramRun(LocalRun):
    def __init__(self, tmp_path, *, rows=2, on_exception=False):
        super().__init__(tmp_path)
        save = self.workflow.steps[0].model_copy(deep=True)
        save.effects[0].match = {"step": ValueExpr(param="record")}
        select = Step(
            id="select",
            intent="select synthetic record",
            action=ActionKind.TYPE,
            param="record",
        )
        leaf = ProgramGraph(
            entry="select",
            states={
                "select": State(
                    id="select",
                    kind=StateKind.ACTION,
                    step=select,
                    transitions=[Transition(target="save")],
                ),
                "save": State(
                    id="save",
                    kind=StateKind.ACTION,
                    step=save,
                    transitions=[Transition(target="leaf-done")],
                    on_exception="handler" if on_exception else None,
                ),
                "handler": State(
                    id="handler",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="handler",
                        intent="exception continuation",
                        action=ActionKind.KEY,
                        key="B",
                    ),
                    transitions=[Transition(target="leaf-done")],
                ),
                "leaf-done": State(
                    id="leaf-done", kind=StateKind.TERMINAL, outcome="success"
                ),
            },
        )
        body = ProgramGraph(
            entry="call",
            states={
                "call": State(
                    id="call",
                    kind=StateKind.SUBFLOW_CALL,
                    subflow="leaf",
                    transitions=[Transition(target="body-done")],
                ),
                "body-done": State(
                    id="body-done", kind=StateKind.TERMINAL, outcome="success"
                ),
            },
        )
        self.workflow = Workflow(
            name="synthetic-nested-local-delivery",
            program=ProgramGraph(
                entry="queue",
                states={
                    "queue": State(
                        id="queue",
                        kind=StateKind.LOOP,
                        loop=LoopSpec(relation="records", body="body", var="record"),
                        transitions=[Transition(target="done")],
                    ),
                    "done": State(
                        id="done", kind=StateKind.TERMINAL, outcome="success"
                    ),
                },
            ),
            subflows={"body": body, "leaf": leaf},
            data_sources={
                "records": Relation(
                    name="records", rows=[{"record": f"R{i}"} for i in range(rows)]
                )
            },
            param_specs={
                "counter": ParamSpec(name="counter", type=ParamKind.NUMBER, example=1)
            },
        )
        self.workflow.save(self.bundle)
        self.readback_available = True
        self.replayer_type = None
        self.active_replayer = None

    def replayer(self, backend, **kwargs):
        original = super().replayer(backend, **kwargs)

        def connect():
            if not self.readback_available:
                raise OSError("synthetic readback unavailable")
            return sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)

        original.effect_verifier = SqlRecordVerifier(
            connect, "SELECT id, step FROM records"
        )
        if self.replayer_type is not None:
            original.__class__ = self.replayer_type
        self.active_replayer = original
        return original

    def write_record(self, record):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO records (step) VALUES (?)", (record,))

    def program_pause(self):
        pause = self.store.read_pending()
        assert pause is not None and pause.program
        assert pause.delivery_uncertainty.operation == "first_local_delivery"
        assert pause.state_id == "save" and pause.step_id == "write"
        assert len(pause.program_frames) == 3
        return pause


class ProgramWriter(FakeBackend):
    def __init__(self, local, *, before_write=None, after_write=None):
        super().__init__()
        self.local = local
        self.selected = None
        self.before_write = before_write
        self.after_write = after_write

    def type_text(self, text):
        super().type_text(text)
        self.selected = text

    def press(self, key):
        super().press(key)
        if key == "A":
            assert self.selected is not None
            if self.before_write is not None:
                self.before_write()
            self.local.write_record(self.selected)
            if self.after_write is not None:
                self.after_write()


def _interrupt(local, monkeypatch, cut):
    if cut == "armed":
        original = DurableRun.arm_first_local_program_delivery

        def after_arm(run, *args, **kwargs):
            original(run, *args, **kwargs)
            raise LegInterrupted

        monkeypatch.setattr(DurableRun, "arm_first_local_program_delivery", after_arm)
        return ProgramWriter(local)
    if cut == "effect":

        def after_write():
            raise LegInterrupted

        return ProgramWriter(local, after_write=after_write)
    if cut == "checkpoint_file":
        original = DurableRun._sync_authority

        def before_authority(run):
            checkpoints = run.store.program_checkpoints()
            if checkpoints and checkpoints[-1].first_local_pause_sha256:
                assert run.store.read_pending() is not None
                raise LegInterrupted
            return original(run)

        monkeypatch.setattr(DurableRun, "_sync_authority", before_authority)
        return ProgramWriter(local)
    original = DurableRun.record_program_checkpoint

    def after_authority(run, checkpoint, **kwargs):
        original(run, checkpoint, **kwargs)
        if kwargs.get("first_local_pause_digest"):
            raise LegInterrupted

    monkeypatch.setattr(DurableRun, "record_program_checkpoint", after_authority)
    return ProgramWriter(local)


@pytest.mark.parametrize("cut", ["armed", "effect", "checkpoint_file", "authority"])
def test_nested_program_cut_never_repeats_unknown_delivery(tmp_path, monkeypatch, cut):
    local = NestedProgramRun(tmp_path)
    with monkeypatch.context() as patch:
        writer = _interrupt(local, patch, cut)
        with pytest.raises(LegInterrupted):
            local.run(writer)
    pause = local.program_pause()
    original_binding = approval_pause_digest(pause)
    assert local.rows() == ([] if cut == "armed" else [("R0",)])
    assert pause.program_frames[1].loop.row_index == 0
    with pytest.raises(ApprovalRequired, match="first_local_delivery_held"):
        _approval(local.bundle, local.run_dir, authorize_uncertain_retry=True)
    continuation = ProgramWriter(local)
    if cut == "authority":
        report = resume(
            local.run_dir,
            local.replayer(continuation),
            approval=_approval(local.bundle, local.run_dir),
        )
        assert report.success is True
        assert local.rows() == [("R0",), ("R1",)]
        assert continuation.actions.count(("press", "A")) == 1
        assert local.store.read_pending() is None
    else:
        # checkpoint_file is an actual persisted matching artifact whose
        # authority advance never ran. Its mere existence cannot allow resume.
        if cut == "checkpoint_file":
            assert (
                local.store.program_checkpoints()[-1].first_local_pause_sha256
                == original_binding
            )
        with pytest.raises((StateDiverged, DurableAuthorityBusy)):
            approval = _approval(local.bundle, local.run_dir)
            resume(local.run_dir, local.replayer(continuation), approval=approval)
        assert continuation.actions == []
        assert local.rows() == ([] if cut == "armed" else [("R0",)])
        assert approval_pause_digest(local.program_pause()) == original_binding


@pytest.mark.parametrize("unavailable", [False, True])
def test_armed_program_runtime_failure_cannot_route_on_exception(tmp_path, unavailable):
    local = NestedProgramRun(tmp_path, on_exception=True)

    def after_write():
        local.readback_available = not unavailable
        raise RuntimeError("synthetic post-effect failure")

    writer = ProgramWriter(local, after_write=after_write)
    report = local.run(writer)
    assert report.success is False
    assert local.rows() == [("R0",)]
    assert ("press", "B") not in writer.actions
    assert report.results[-1].safety_halt
    assert "first_local_delivery_held" in report.results[-1].error
    pause = local.program_pause()
    assert pause.program_frames[1].loop.row_index == 0
    assert [cp.step_id for cp in local.store.program_checkpoints()] == ["select"]


def test_matching_program_tail_still_requires_independent_readback(
    tmp_path, monkeypatch
):
    local = NestedProgramRun(tmp_path)
    with monkeypatch.context() as patch:
        writer = _interrupt(local, patch, "authority")
        with pytest.raises(LegInterrupted):
            local.run(writer)
    with closing(sqlite3.connect(local.database)) as conn, conn:
        conn.execute("DELETE FROM records")
    continuation = ProgramWriter(local)
    with pytest.raises(StateDiverged):
        resume(
            local.run_dir,
            local.replayer(continuation),
            approval=_approval(local.bundle, local.run_dir),
        )
    assert continuation.actions == [] and local.rows() == []
    local.program_pause()


@pytest.mark.parametrize(
    "mutation",
    ["frame", "row", "row_bool", "params", "typed_params", "sequence", "history"],
)
def test_pre_callback_cursor_refuses_live_alias_mutation(tmp_path, mutation):
    local = NestedProgramRun(tmp_path)

    class MutatingReplayer(Replayer):
        mutated = False

        def _emit_control_overlay_phase(self, phase, **kwargs):
            if (
                phase == "executing"
                and self._current_state_id == "save"
                and not self.mutated
            ):
                self.mutated = True
                frames = self._frame_stack
                if mutation == "frame":
                    frames[0]["state_id"] = "done"
                elif mutation == "row":
                    frames[1]["loop"].rows[0]["record"] = "wrong"
                elif mutation == "row_bool":
                    frames[1]["loop"].row_index = False  # Python False == 0.
                elif mutation == "params":
                    frames[-1]["params"]["record"] = "wrong"
                elif mutation == "typed_params":
                    for frame in frames:
                        frame["params"]["counter"] = True  # Python True == 1.
                    self._current_params["counter"] = True
                elif mutation == "sequence":
                    self._program_seq = False
                else:
                    self._program_durable_history.append("wrong-history")
            return super()._emit_control_overlay_phase(phase, **kwargs)

    local.replayer_type = MutatingReplayer
    writer = ProgramWriter(local)
    report = local.run(writer)
    assert report.success is False
    assert local.rows() == [] and ("press", "A") not in writer.actions
    assert not any(
        cp.first_local_pause_sha256 for cp in local.store.program_checkpoints()
    )


def test_nested_program_live_owner_blocks_competing_continuation(tmp_path):
    local = NestedProgramRun(tmp_path, rows=1)
    entered, release = Event(), Event()

    def hold_before_effect():
        entered.set()
        assert release.wait(timeout=15)

    writer = ProgramWriter(local, before_write=hold_before_effect)
    contender = ProgramWriter(local)
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(local.run, writer)
        try:
            assert entered.wait(timeout=15)
            local.program_pause()
            assert local.rows() == []
            with pytest.raises(ContinuationBusy):
                resume(local.run_dir, local.replayer(contender))
            assert contender.actions == []
        finally:
            release.set()
        assert owner.result(timeout=15).success is True
    assert local.rows() == [("R0",)] and local.store.read_pending() is None


def test_program_cursor_snapshot_and_decoded_copies_preserve_exact_types(
    tmp_path, monkeypatch
):
    import copy

    from openadapt_flow.runtime.durable.program_checkpoint import (
        bundle_version,
        history_hash,
    )

    local = NestedProgramRun(tmp_path)
    with monkeypatch.context() as patch:
        writer = _interrupt(local, patch, "armed")
        with pytest.raises(LegInterrupted):
            local.run(writer)
    original = ProgramDeliveryCursor.from_pending(
        local.program_pause(), bundle_version=bundle_version(local.bundle)
    )
    expected = original.fields()
    borrowed = original.fields()
    borrowed["program_frames"][1]["loop"]["rows"][0]["record"] = "wrong"
    borrowed["params"]["counter"] = True
    assert original.fields() == expected
    for sibling in ("row", "row_bool", "typed_params", "state", "sequence", "history"):
        fields = copy.deepcopy(expected)
        if sibling == "row":
            fields["program_frames"][1]["loop"]["row_index"] = 1
        elif sibling == "row_bool":
            fields["program_frames"][1]["loop"]["row_index"] = False
            with pytest.raises(ValueError, match="exact integer"):
                ProgramDeliveryCursor.capture(**fields)
            continue
        elif sibling == "typed_params":
            fields["params"]["counter"] = True
            fields["program_frames"][-1]["params"]["counter"] = True
        elif sibling == "state":
            fields["program_frames"][-1]["graph_id"] = "other-graph"
        elif sibling == "sequence":
            fields["program_checkpoint_seq"] += 1
        else:
            fields["program_history"].append("another-state")
            fields["program_history_delta"].append("another-state")
            fields["program_history_hash"] = history_hash(fields["program_history"])
        assert ProgramDeliveryCursor.capture(**fields) != original
    assert local.rows() == []


@pytest.mark.parametrize(
    "mutation", ["row", "row_bool", "typed_params", "sequence", "history"]
)
def test_recorded_program_result_must_match_the_armed_cursor(
    tmp_path, monkeypatch, mutation
):
    from openadapt_flow.runtime.durable.program_checkpoint import history_hash

    local = NestedProgramRun(tmp_path)
    original = DurableRun.record_program_checkpoint

    def conflicting_result(run, checkpoint, **kwargs):
        if kwargs.get("first_local_pause_digest"):
            checkpoint = checkpoint.model_copy(deep=True)
            if mutation == "row":
                checkpoint.frames[1].loop.row_index = 1
            elif mutation == "row_bool":
                checkpoint.frames[1].loop.row_index = False
            elif mutation == "typed_params":
                checkpoint.bound_params["counter"] = True
                checkpoint.frames[-1].params["counter"] = True
            elif mutation == "sequence":
                checkpoint.seq += 1
            else:
                checkpoint.transition_delta.append("another-state")
                checkpoint.transition_history.append("another-state")
                checkpoint.transition_history_hash = history_hash(
                    checkpoint.transition_history
                )
        return original(run, checkpoint, **kwargs)

    monkeypatch.setattr(DurableRun, "record_program_checkpoint", conflicting_result)
    writer = ProgramWriter(local)
    report = local.run(writer)
    assert report.success is False
    assert local.rows() == [("R0",)]
    assert writer.actions.count(("press", "A")) == 1
    assert all(
        not cp.first_local_pause_sha256 for cp in local.store.program_checkpoints()
    )
    local.program_pause()
    continuation = ProgramWriter(local)
    with pytest.raises(StateDiverged, match="first_local_delivery_held"):
        resume(
            local.run_dir,
            local.replayer(continuation),
            approval=_approval(local.bundle, local.run_dir),
        )
    assert continuation.actions == [] and local.rows() == [("R0",)]


def test_actual_owned_process_exit_retains_unknown_program_effect(
    tmp_path, record_testsuite_property
):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from tests.conftest import _isolation_env

    local = NestedProgramRun(tmp_path, rows=1)
    # The child opens this already compiled own fixture. It exits itself after
    # SQLite commit, bypassing Python finally blocks; no foreign PID is signalled.
    child = """
import os,sys
from pathlib import Path
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.durable import CheckpointStore
from tests.test_durable_first_local_program_delivery import NestedProgramRun,ProgramWriter
root=Path(sys.argv[1])
local=object.__new__(NestedProgramRun)
local.bundle=root/'bundle';local.run_dir=root/'run';local.database=root/'records.sqlite3'
local.workflow=Workflow.load(local.bundle);local.store=CheckpointStore(local.run_dir)
local.replayer_type=None;local.active_replayer=None;local.readback_available=True
local.run(ProgramWriter(local,after_write=lambda:os._exit(73)))
"""
    package = Path(__file__).resolve().parents[1]
    env = {
        name: value
        for name, value in _isolation_env(tmp_path).items()
        if value is not None
    }
    env.update(
        PATH=os.defpath,
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=os.pathsep.join((str(package), str(package.parent / "types/src"))),
        OPENADAPT_DURABLE_AUTHORITY_DB=str(tmp_path / ".durable-authority.sqlite3"),
    )
    outcome = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        cwd=package,
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    record_testsuite_property("owned_child_exit_code", outcome.returncode)
    record_testsuite_property(
        "interruption_limit",
        "os._exit after own SQLite effect; not SIGKILL or power-loss qualification",
    )
    assert outcome.returncode == 73, outcome.stderr.decode(errors="replace")
    assert outcome.stdout == b"" and len(outcome.stderr) < 8192
    assert local.rows() == [("R0",)]
    local.program_pause()
    # Successful approval proves the dead child's owner lock was released by
    # the OS. Its unresolved attempt still cannot deliver or claim completion.
    approval = _approval(local.bundle, local.run_dir)
    contender = ProgramWriter(local)
    with pytest.raises(StateDiverged, match="first_local_delivery_held"):
        resume(local.run_dir, local.replayer(contender), approval=approval)
    assert contender.actions == [] and local.rows() == [("R0",)]


@pytest.mark.parametrize("cut", ["complete", "effect", "authority"])
def test_first_local_program_api_is_bound_without_gui_or_redelivery(
    tmp_path, monkeypatch, cut
):
    from types import SimpleNamespace

    from openadapt_flow.ir import ApiBinding
    from openadapt_flow.runtime.actuators import ApiActuator

    local = LocalProgramRun(tmp_path)
    step = local.workflow.program.states["write-state"].step
    step.api_binding = ApiBinding(
        method="POST",
        url_template="/records",
        body_template={"step": "write"},
        effects=step.effects,
        on_unavailable="halt",
    )
    local.workflow.save(local.bundle)
    requests, bindings = [], []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs["json"]))
        pause = local.store.read_pending()
        assert pause is not None and pause.program
        assert pause.delivery_uncertainty.native is False
        assert pause.state_id == "write-state" and pause.program_checkpoint_seq == 0
        bindings.append(approval_pause_digest(pause))
        assert local.rows() == []
        local.write()
        if cut == "effect":
            raise LegInterrupted
        return SimpleNamespace(status_code=201)

    api = ApiActuator(
        "https://fixture.invalid", session=SimpleNamespace(request=request)
    )
    backend = FakeBackend()
    with monkeypatch.context() as patch:
        if cut == "authority":
            original = DurableRun.record_program_checkpoint

            def interrupt(run, checkpoint, **kwargs):
                original(run, checkpoint, **kwargs)
                if kwargs.get("first_local_pause_digest"):
                    raise LegInterrupted

            patch.setattr(DurableRun, "record_program_checkpoint", interrupt)
        if cut == "complete":
            assert local.run(backend, actuator=api).success is True
        else:
            with pytest.raises(LegInterrupted):
                local.run(backend, actuator=api)
    assert requests == [("POST", "https://fixture.invalid/records", {"step": "write"})]
    assert backend.actions == [] and local.rows() == [("write",)]
    if cut == "effect":
        with pytest.raises(StateDiverged, match="first_local_delivery_held"):
            resume(
                local.run_dir,
                local.replayer(FakeBackend(), actuator=api),
                approval=_approval(local.bundle, local.run_dir),
            )
    elif cut == "authority":
        assert (
            resume(
                local.run_dir,
                local.replayer(FakeBackend(), actuator=api),
                approval=_approval(local.bundle, local.run_dir),
            ).success
            is True
        )
    if cut != "effect":
        assert local.store.read_pending() is None
        assert (
            local.store.program_checkpoints()[0].first_local_pause_sha256 == bindings[0]
        )
    assert len(requests) == 1


def test_verified_same_values_on_another_loop_row_keep_existing_effect_dedupe(
    tmp_path, monkeypatch
):
    local = NestedProgramRun(tmp_path)
    local.workflow.data_sources["records"].rows = [
        {"record": "same"},
        {"record": "same"},
    ]
    local.workflow.save(local.bundle)
    with monkeypatch.context() as patch:
        writer = _interrupt(local, patch, "authority")
        with pytest.raises(LegInterrupted):
            local.run(writer)
    assert local.program_pause().program_frames[1].loop.row_index == 0
    continuation = ProgramWriter(local)
    report = resume(
        local.run_dir,
        local.replayer(continuation),
        approval=_approval(local.bundle, local.run_dir),
    )
    assert report.success is True
    assert local.rows() == [("same",)]
    assert ("press", "A") not in continuation.actions
    assert ("type", "same") in continuation.actions
    assert local.store.read_pending() is None
