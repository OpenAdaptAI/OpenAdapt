"""Completion lifecycle components; no browser, model or native app proof.

The signed authority, TeachGuard, GuardedCapture completion method and Recording
exit are real. The guard summary, compiler and draft writer are instruments.
"""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow import teach_authorization as ta
from openadapt_flow.agent_guard.capture import GuardedCapture, GuardedCaptureError
from openadapt_flow.jit.compile_agent import JitResult, Recording, RecordingDispatcher
from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
from openadapt_flow.teach_scope import TeachGuard
from tests._teach_scope import encounter_authorization_spec


def _recording(tmp_path):
    person = LocalAuthorizer(tmp_path / "keys", "completion-reviewer")
    statement = person.issue(
        tmp_path / "auth",
        encounter_authorization_spec(
            "http://127.0.0.1:8765", patient_id="P1", note="Synthetic note"
        ),
    )
    verification = ta.require(
        statement, purpose="teach", key_dir=person.key_dir, root=person.root
    )
    scope = ta.bind(verification, {"note": "Synthetic note"})
    guard = TeachGuard(scope)
    after = [
        {"id": "one", "patient_id": "P1", "type": "Triage", "note": "Synthetic note"}
    ]
    assert scope.authorized and guard.check_delta([], after).verified
    calls = []

    def compile_recording(*args, **kwargs):
        calls.append("compile")
        return SimpleNamespace()

    def write_draft(*args, **kwargs):
        calls.append("draft")
        return JitResult("draft", "synthetic-task", "miss", False)

    def refuse(status, *args, **kwargs):
        calls.append("refused")
        return JitResult(status, "synthetic-task", "miss", False)

    owner = SimpleNamespace(
        app=SimpleNamespace(record_reader=lambda: after),
        bundle_dir=tmp_path / "bundle",
        task=SimpleNamespace(task_id=scope.task_id),
        on_warning=None,
        _write_draft=write_draft,
        _capture_refused=refuse,
    )
    recording = Recording(owner, {}, {}, "generic")
    recording._session = SimpleNamespace(
        halt=lambda: calls.append("halt"),
        compile=compile_recording,
        compiled_recording_dir=None,
        recording_dir=None,
    )
    recording.computer = SimpleNamespace(actions=1)
    recording.guard = guard
    recording._baseline = []
    recording._close = lambda: calls.append("backend-close")
    recording.dispatcher = RecordingDispatcher(recording)
    recording.dispatcher.on_close(lambda: calls.append("controller-close"))
    return recording, calls


def _guard_completion(verdict):
    capture = object.__new__(GuardedCapture)
    capture.timeout_s = 1.0
    capture.commit_guard = SimpleNamespace(
        finished=True, summary=SimpleNamespace(verdict=verdict)
    )
    return capture._finish


@pytest.mark.parametrize("verdict", ["VERIFIED", "REFUTED", "INDETERMINATE"])
@pytest.mark.parametrize("caught_first", [False, True])
def test_guard_completion_cannot_be_caught_then_bypassed_by_context_exit(
    tmp_path, verdict, caught_first
):
    recording, calls = _recording(tmp_path)
    callback_calls = []
    callback = _guard_completion(verdict)

    def complete():
        callback_calls.append("check")
        callback()

    recording.dispatcher.before_finish(complete)
    with ExitStack() as stack:
        stack.push(recording)
        if caught_first:
            if verdict == "VERIFIED":
                recording.dispatcher.finish()
            else:
                with pytest.raises(GuardedCaptureError):
                    recording.dispatcher.finish()
    assert callback_calls == ["check"]
    if verdict == "VERIFIED":
        assert recording.result.status == "draft"
        assert calls == ["compile", "controller-close", "backend-close", "draft"]
    else:
        assert recording.result.status == "capture_failed"
        assert calls == ["halt", "refused", "controller-close", "backend-close"]
        with pytest.raises(GuardedCaptureError):
            recording.dispatcher.finish()
        assert "compile" not in calls and "draft" not in calls


@pytest.mark.parametrize(
    "exception", [RuntimeError("failed"), ValueError("bad check"), KeyboardInterrupt()]
)
def test_any_completion_failure_is_sticky_and_no_callback_is_retried(exception):
    dispatcher = RecordingDispatcher(SimpleNamespace(computer=SimpleNamespace()))
    calls = []

    def fail():
        calls.append("failed")
        raise exception

    dispatcher.before_finish(lambda: calls.append("first"))
    dispatcher.before_finish(fail)
    dispatcher.before_finish(lambda: calls.append("must-not-run"))
    for _ in range(3):
        with pytest.raises(type(exception)) as caught:
            dispatcher.finish()
        assert caught.value is exception
    assert calls == ["first", "failed"]


@pytest.mark.parametrize("caught_inner", [False, True])
def test_reentrant_completion_cannot_turn_into_success(tmp_path, caught_inner):
    recording, calls = _recording(tmp_path)
    callback_calls = []

    def reenter():
        callback_calls.append("check")
        if caught_inner:
            with pytest.raises(RuntimeError):
                recording.dispatcher.finish()
        else:
            recording.dispatcher.finish()

    recording.dispatcher.before_finish(reenter)
    with ExitStack() as stack:
        stack.push(recording)
        with pytest.raises(RuntimeError):
            recording.dispatcher.finish()
    assert recording.result.status == "capture_failed"
    assert callback_calls == ["check"]
    assert "compile" not in calls and "draft" not in calls


def test_closing_during_completion_cannot_authorize_a_draft(tmp_path):
    recording, calls = _recording(tmp_path)
    recording.dispatcher.before_finish(recording.dispatcher.close)
    with ExitStack() as stack:
        stack.push(recording)
    assert recording.result.status == "capture_failed"
    assert calls.count("controller-close") == 1
    assert "compile" not in calls and "draft" not in calls


def test_all_successful_callbacks_run_once_in_registration_order():
    dispatcher = RecordingDispatcher(SimpleNamespace(computer=SimpleNamespace()))
    called = []
    for index in range(6):
        dispatcher.before_finish(lambda index=index: called.append(index))
    dispatcher.finish()
    dispatcher.finish()
    assert called == list(range(6))


def test_closed_unfinished_dispatcher_does_not_run_checks_or_write_a_draft(tmp_path):
    recording, calls = _recording(tmp_path)
    recording.dispatcher.before_finish(lambda: calls.append("must-not-run"))
    recording.dispatcher.close()
    with ExitStack() as stack:
        stack.push(recording)
    assert recording.result.status == "capture_failed"
    assert "must-not-run" not in calls
    assert "compile" not in calls and "draft" not in calls


@given(
    fail_at=st.integers(min_value=0, max_value=5),
    repeats=st.integers(min_value=1, max_value=15),
)
def test_completion_prefix_failure_never_retries_callbacks(fail_at, repeats):
    dispatcher = RecordingDispatcher(SimpleNamespace(computer=SimpleNamespace()))
    called = []

    def callback(index):
        called.append(index)
        if index == fail_at:
            raise RuntimeError("synthetic completion refusal")

    for index in range(6):
        dispatcher.before_finish(lambda index=index: callback(index))
    for _ in range(repeats):
        with pytest.raises(RuntimeError):
            dispatcher.finish()
    assert called == list(range(fail_at + 1))
    for entry in (
        lambda: dispatcher.execute({}),
        lambda: dispatcher.after_action({}),
        lambda: dispatcher.add_after_action(lambda native: None),
        lambda: dispatcher.before_finish(lambda: None),
    ):
        with pytest.raises(RuntimeError):
            entry()
