"""Exact first-delivery recovery with a fake writer and independent SQL reads."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from types import SimpleNamespace

import pytest

from openadapt_flow.ir import ApiBinding, Workflow
from openadapt_flow.run_gate import is_consequential, must_be_identity_armed
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.durable import (
    ApprovalRequired,
    CheckpointStore,
    StateDiverged,
    resume,
)
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.continuation import ContinuationBusy
from openadapt_flow.runtime.durable.controller import DurableRun
from openadapt_flow.runtime.durable.first_local_delivery import FirstLocalDeliveryHeld
from openadapt_flow.runtime.effects import Effect, EffectKind, SqlRecordVerifier
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from openadapt_flow.runtime.replayer import Replayer
from tests.test_durable_runtime import _approval, _key_step, _vision_ok
from tests.test_governed_authorization import _authorization
from tests.test_replayer import (
    FakeBackend,
    OcrLine,
    context_click_step,
    make_png,
    resolving_vision,
)


class LegInterrupted(BaseException):
    """Stop at an explicit boundary without simulating a process kill."""


class LocalRun:
    def __init__(self, tmp_path, *, following_step=False, api=False):
        self.bundle = tmp_path / "bundle"
        self.run_dir = tmp_path / "run"
        self.database = tmp_path / "records.sqlite3"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, step TEXT)")
        effect = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"step": "write"},
            expected_count=1,
            count_new_only=True,
            timeout_s=0.0,
        )
        step = _key_step("write", "A", effect=effect, risk="irreversible")
        if api:
            step.api_binding = ApiBinding(
                method="POST",
                url_template="/records",
                body_template={"step": "write"},
                effects=[effect],
                on_unavailable="halt",
            )
        steps = [step, _key_step("after", "B")] if following_step else [step]
        self.workflow = Workflow(name="synthetic-local-delivery", steps=steps)
        self.workflow.save(self.bundle)
        self.store = CheckpointStore(self.run_dir)

    def write(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO records (step) VALUES (?)", ("write",))

    def rows(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute("SELECT step FROM records ORDER BY id").fetchall()

    def replayer(self, backend, *, actuator=None, vision=None, authorization=None):
        verifier = SqlRecordVerifier(
            lambda: sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True),
            "SELECT id, step FROM records",
        )
        return Replayer(
            backend,
            vision=vision if vision is not None else _vision_ok(),
            effect_verifier=verifier,
            governed_authorization=authorization,
            api_actuator=actuator,
            durable=True,
            poll_interval_s=0.01,
        )

    def run(self, backend, *, actuator=None):
        return self.replayer(backend, actuator=actuator).run(
            self.workflow, bundle_dir=self.bundle, run_dir=self.run_dir
        )

    def armed(self, *, native):
        pending = self.store.read_pending()
        manifest = self.store.read_manifest()
        assert manifest is not None and pending is not None
        assert pending.run_id == manifest.run_id
        assert pending.step_id == "write" and pending.resume_from_index == 0
        assert pending.step_index == 0 and pending.category == "delivery_uncertain"
        uncertainty = pending.delivery_uncertainty
        assert uncertainty is not None
        assert uncertainty.operation == "first_local_delivery"
        assert uncertainty.native is native
        return pending


class WritingBackend(FakeBackend):
    def __init__(self, local, *, before_write=None, after_write=None):
        super().__init__()
        self.local = local
        self.before_write = before_write
        self.after_write = after_write

    def press(self, key):
        super().press(key)
        assert key == "A"
        if self.before_write is not None:
            self.before_write()
        self.local.write()
        if self.after_write is not None:
            self.after_write()


def test_first_local_gui_refuses_when_write_ahead_cannot_persist(tmp_path, monkeypatch):
    local = LocalRun(tmp_path)
    backend = WritingBackend(local)
    original = CheckpointStore.cas_pending
    attempted = []

    def fail_write_ahead(store, digest, pending):
        uncertainty = pending.delivery_uncertainty if pending is not None else None
        if uncertainty is not None and uncertainty.operation == "first_local_delivery":
            attempted.append(pending.step_id)
            raise OSError("synthetic write-ahead persistence failure")
        return original(store, digest, pending)

    monkeypatch.setattr(CheckpointStore, "cas_pending", fail_write_ahead)
    report = local.run(backend)

    assert attempted == ["write"]
    assert report.success is False and len(report.results) == 1
    result = report.results[0]
    assert result.safety_halt and result.delivery_attempted is not True
    assert "first_local_delivery_held" in result.error
    assert backend.actions == [] and local.rows() == []
    assert local.store.checkpoints() == [] and local.store.read_pending() is None


def test_first_local_gui_interruption_after_effect_holds_without_retry(tmp_path):
    local = LocalRun(tmp_path)

    def interrupt_after_write():
        assert local.rows() == [("write",)]
        raise LegInterrupted

    backend = WritingBackend(local, after_write=interrupt_after_write)
    with pytest.raises(LegInterrupted):
        local.run(backend)
    assert backend.actions == [("press", "A")]
    pending = local.armed(native=True)
    assert local.store.checkpoints() == []

    with pytest.raises(ApprovalRequired, match="first_local_delivery_held"):
        _approval(local.bundle, local.run_dir, authorize_uncertain_retry=True)
    contender = FakeBackend()
    with pytest.raises(StateDiverged, match="first_local_delivery_held"):
        resume(
            local.run_dir,
            local.replayer(contender),
            approval=_approval(local.bundle, local.run_dir),
        )
    assert contender.actions == [] and local.rows() == [("write",)]
    assert local.store.checkpoints() == []
    pending_digest = approval_pause_digest(pending)
    assert approval_pause_digest(local.armed(native=True)) == pending_digest


def test_matching_first_local_checkpoint_advances_without_repeating_effect(
    tmp_path, monkeypatch
):
    local = LocalRun(tmp_path, following_step=True)
    original = DurableRun.record

    def interrupt_after_commit(run, index, step, result, params, **kwargs):
        original(run, index, step, result, params, **kwargs)
        if step.id == "write":
            assert result.effect_verified is True
            raise LegInterrupted

    backend = WritingBackend(local)
    with monkeypatch.context() as patch:
        patch.setattr(DurableRun, "record", interrupt_after_commit)
        with pytest.raises(LegInterrupted):
            local.run(backend)
    assert backend.actions == [("press", "A")]
    pending = local.armed(native=True)
    [checkpoint] = local.store.checkpoints()
    assert checkpoint.first_local_pause_sha256 == approval_pause_digest(pending)
    assert checkpoint.effect_verified is True and checkpoint.next_step_index == 1
    assert local.rows() == [("write",)]

    continuation = FakeBackend()
    report = resume(
        local.run_dir,
        local.replayer(continuation),
        approval=_approval(local.bundle, local.run_dir),
    )
    assert report.success is True
    assert [row.step_id for row in report.results] == ["write", "after"]
    assert continuation.actions == [("press", "B")]
    assert local.rows() == [("write",)]
    assert local.store.checkpoints()[0] == checkpoint
    assert local.store.read_pending() is None


def test_live_first_local_owner_refuses_competing_resume(tmp_path):
    local = LocalRun(tmp_path)
    entered = Event()
    release = Event()

    def hold_before_write():
        entered.set()
        assert release.wait(timeout=15)

    backend = WritingBackend(local, before_write=hold_before_write)
    contender = FakeBackend()
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(local.run, backend)
        try:
            assert entered.wait(timeout=15)
            pending = local.armed(native=True)
            assert local.rows() == [] and local.store.checkpoints() == []
            with pytest.raises(ContinuationBusy) as caught:
                resume(local.run_dir, local.replayer(contender))
            assert isinstance(caught.value.__cause__, FirstLocalDeliveryHeld)
            assert contender.actions == []
            pending_digest = approval_pause_digest(pending)
            assert approval_pause_digest(local.armed(native=True)) == pending_digest
        finally:
            release.set()
        assert owner.result(timeout=15).success is True
    assert backend.actions == [("press", "A")]
    assert local.rows() == [("write",)] and local.store.read_pending() is None


def test_standalone_first_local_api_is_armed_before_the_fake_request(tmp_path):
    local = LocalRun(tmp_path, api=True)
    requests = []
    seen_pauses = []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs["json"]))
        pending = local.armed(native=False)
        assert local.rows() == [] and local.store.checkpoints() == []
        seen_pauses.append(approval_pause_digest(pending))
        local.write()
        return SimpleNamespace(status_code=201)

    base_url = "https://fixture.invalid"
    api = ApiActuator(base_url, session=SimpleNamespace(request=request))
    backend = FakeBackend()
    report = local.run(backend, actuator=api)

    assert report.success is True
    assert requests == [("POST", f"{base_url}/records", {"step": "write"})]
    assert backend.actions == [] and local.rows() == [("write",)]
    [checkpoint] = local.store.checkpoints()
    assert checkpoint.actuation == "api" and checkpoint.effect_verified is True
    assert seen_pauses == [checkpoint.first_local_pause_sha256]
    assert local.store.read_pending() is None


def test_identity_navigation_precedes_a_separately_fenced_write(tmp_path):
    local = LocalRun(tmp_path)
    context = "Jane Sample Knee pain referral High"
    navigation = context_click_step(context, step_id="open", ocr_text="Open")
    local.bundle = tmp_path / "navigation-bundle"
    templates = local.bundle / "templates"
    templates.mkdir(parents=True)
    (templates / "btn.png").write_bytes(make_png((50, 20)))
    workflow = Workflow(
        name="navigate-then-write", steps=[navigation, *local.workflow.steps]
    )
    workflow.save(local.bundle)
    workflow = Workflow.load(local.bundle)
    navigation, write = workflow.steps
    assert must_be_identity_armed(navigation, workflow)
    assert not is_consequential(navigation, workflow)
    assert is_consequential(write, workflow)
    authorization = _authorization(workflow, required=(navigation.id,))

    class NavigatingBackend(WritingBackend):
        def click(self, x, y, *, double=False):
            assert local.store.read_pending() is None
            assert local.store.checkpoints() == []
            assert local.rows() == []
            super().click(x, y, double=double)

    pause_digests = []

    def before_write():
        [checkpoint] = local.store.checkpoints()
        assert checkpoint.step_id == "open"
        assert checkpoint.first_local_pause_sha256 is None
        pending = local.store.read_pending()
        assert pending is not None and pending.step_id == "write"
        assert pending.resume_from_index == 1 and pending.resume_from_step_id == "open"
        assert pending.category == "delivery_uncertain"
        assert pending.delivery_uncertainty is not None
        assert pending.delivery_uncertainty.operation == "first_local_delivery"
        assert pending.delivery_uncertainty.native is True
        assert local.rows() == []
        pause_digests.append(approval_pause_digest(pending))

    vision = resolving_vision()
    vision.ocr_lines = [OcrLine(context)]
    vision.text_results = _vision_ok().text_results
    backend = NavigatingBackend(local, before_write=before_write)
    report = local.replayer(backend, vision=vision, authorization=authorization).run(
        workflow, bundle_dir=local.bundle, run_dir=local.run_dir
    )

    assert report.success is True
    opened, written = report.results
    assert opened.identity is not None and opened.identity.status == "verified"
    assert opened.effect_verified is None and written.effect_verified is True
    assert backend.actions == [("click", 110, 105, False), ("press", "A")]
    first, second = local.store.checkpoints()
    assert first.step_id == "open" and first.first_local_pause_sha256 is None
    assert second.step_id == "write" and second.effect_verified is True
    assert pause_digests == [second.first_local_pause_sha256]
    assert local.rows() == [("write",)] and local.store.read_pending() is None


@pytest.mark.parametrize("actuation", ["gui", "api"])
@pytest.mark.parametrize("contract", ["empty", "placeholder"])
def test_invalid_selected_effect_contract_refuses_before_input(
    tmp_path, actuation, contract
):
    local = LocalRun(tmp_path)
    [step] = local.workflow.steps
    [effect] = step.effects
    invalid = (
        [effect.model_copy(update={"needs_operator_confirmation": True})]
        if contract == "placeholder"
        else []
    )
    # Each fixture has valid effects on the alternate path. They cannot cover
    # missing or placeholder evidence on the path the runtime would select.
    step.effects = invalid if actuation == "gui" else [effect]
    step.api_binding = ApiBinding(
        method="POST",
        url_template="/records",
        body_template={"step": "write"},
        effects=invalid if actuation == "api" else [effect],
        on_unavailable="gui",
    )
    local.bundle = tmp_path / "invalid-contract-bundle"
    local.workflow.save(local.bundle)
    requests = []

    def request(method, url, **kwargs):
        requests.append((method, url))
        local.write()
        return SimpleNamespace(status_code=201)

    actuator = (
        ApiActuator("https://fixture.invalid", session=SimpleNamespace(request=request))
        if actuation == "api"
        else None
    )
    backend = WritingBackend(local)
    report = local.run(backend, actuator=actuator)

    assert report.success is False
    [result] = report.results
    assert result.safety_halt and result.delivery_attempted is False
    assert backend.actions == [] and requests == [] and local.rows() == []
    assert local.store.checkpoints() == []
    pending = local.store.read_pending()
    assert pending is not None and pending.step_id == "write"
    assert pending.delivery_uncertainty is None and pending.reason == result.error
    if contract == "placeholder":
        assert pending.category == "placeholder_effect"
        assert result.effect_verified is False
        assert "unconfirmed PLACEHOLDER" in pending.reason
        assert any("binding" in option for option in pending.proposed_options)
    elif actuation == "api":
        assert "no api_binding.effects contract" in result.effect_results[0]
    else:
        assert "requires an effect contract for the selected actuation path" in (
            pending.reason
        )


@pytest.mark.parametrize("actuation", ["gui", "api"])
def test_marked_deferred_readback_resumes_without_redelivery(
    tmp_path, monkeypatch, actuation
):
    api = actuation == "api"
    local = LocalRun(tmp_path, api=api)
    key = "synthetic-first-local-checkpoint-key"
    local.store = CheckpointStore(local.run_dir, key=key)
    initial_markers = []
    requests = []

    def before_write():
        pending = local.armed(native=not api)
        initial_markers.append(pending.delivery_uncertainty)
        assert local.rows() == []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs["json"]))
        before_write()
        local.write()
        return SimpleNamespace(status_code=201)

    url = "https://fixture.invalid"
    actuator = (
        ApiActuator(url, session=SimpleNamespace(request=request)) if api else None
    )
    backend = WritingBackend(local, before_write=before_write)
    pipeline = ReadbackPipeline()
    handoffs = []

    def leave_unsettled(entry, settle):
        handoffs.append(entry)

    # Keep real pre-state capture, delivery and readback. Interrupt only the
    # handoff that would publish the deferred result and consume the pause.
    monkeypatch.setattr(pipeline, "hand_off", leave_unsettled)
    first = local.replayer(backend, actuator=actuator)
    first.checkpoint_key = key
    first.readback_pipeline = pipeline
    try:
        held = first.run(local.workflow, bundle_dir=local.bundle, run_dir=local.run_dir)
    finally:
        pipeline.close()

    assert not held.success and not held.execution_completed
    assert len(handoffs) == 1 and handoffs[0].step_id == "write"
    assert backend.actions == ([] if api else [("press", "A")])
    expected_requests = [("POST", f"{url}/records", {"step": "write"})] if api else []
    assert requests == expected_requests and local.rows() == [("write",)]
    assert local.store.checkpoints() == []
    pending = local.store.read_pending()
    assert pending is not None and pending.category == "readback_pending"
    assert pending.delivery_uncertainty == initial_markers[0]
    assert pending.deferred_readback is not None
    assert pending.deferred_readback.actuation == actuation
    assert pending.deferred_readback.result.effect_verified is None
    digest = approval_pause_digest(pending)

    resume_reads = []

    def connect_read_only():
        resume_reads.append(True)
        return sqlite3.connect(local.database.as_uri() + "?mode=ro", uri=True)

    contender = WritingBackend(local)
    second = local.replayer(contender, actuator=actuator)
    second.effect_verifier = SqlRecordVerifier(
        connect_read_only, "SELECT id, step FROM records"
    )
    resumed = resume(
        local.run_dir,
        second,
        approval=_approval(local.bundle, local.run_dir, checkpoint_key=key),
        key=key,
    )

    assert resumed.success is True and resumed.execution_completed is True
    assert resume_reads
    assert contender.actions == [] and requests == expected_requests
    assert local.rows() == [("write",)]
    [checkpoint] = local.store.checkpoints()
    assert checkpoint.step_id == "write" and checkpoint.effect_verified is True
    assert checkpoint.first_local_pause_sha256 == digest
    assert checkpoint.next_step_index == 1 and local.store.read_pending() is None
