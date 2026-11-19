"""Typed text that a remote display paints late is re-read, never retyped.

On a real AVC444 session the note box took focus at once and showed the
typed note a moment later, after the settle had seen two equal frames: 14
of 20 healthy runs stopped with "the field region changed but the typed
value is not readable". The verdict now re-reads the field on fresh frames a
bounded number of times; nothing is typed again, and only the exact value
read in the field counts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import openadapt_flow.runtime.replayer as replayer_mod
from openadapt_flow.ir import StepResult
from openadapt_flow.runtime.replayer import Replayer


def _verify(monkeypatch, answers, calls=None):
    calls = [] if calls is None else calls

    def landed(self, *args, **kwargs):
        calls.append(args)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(Replayer, "_typed_input_landed", landed)
    monkeypatch.setattr(replayer_mod.time, "sleep", lambda _s: None)
    # An unchanged field falls through to the retry policy; stop it there.
    monkeypatch.setattr(
        Replayer, "_workflow_snapshot_refusal", lambda self, _w: "stop here"
    )
    backend = SimpleNamespace(actions=[])
    replayer = Replayer.__new__(Replayer)
    replayer.backend = backend
    result = StepResult(step_id="t1", intent="type <note>", ok=False)
    step = SimpleNamespace(id="t1", intent="type <note>")
    error = Replayer._verify_typed_input(
        replayer,
        step,
        "note",
        (10, 10),
        b"before",
        result,
        workflow=SimpleNamespace(surface="rdp"),
        params={},
        bundle_dir=None,
        run_dir=None,
        resolution=None,
        allow_retry=False,
    )
    return error, result, calls, backend


def test_a_value_painted_late_is_verified_on_a_reread(monkeypatch):
    error, result, calls, backend = _verify(monkeypatch, [(False, True), (True, True)])
    assert error is None and result.input_verified is True
    assert len(calls) == 2 and backend.actions == []


def test_a_value_that_never_reads_still_halts_after_the_bounded_rereads(monkeypatch):
    error, result, calls, _backend = _verify(monkeypatch, [(False, True)])
    assert error is not None and "not readable" in error
    assert result.input_verified is False
    assert len(calls) == 1 + replayer_mod.TYPED_ECHO_REREADS


@pytest.mark.parametrize("answer", [(True, True), (False, False)])
def test_no_reread_when_the_first_read_decides(monkeypatch, answer):
    calls: list = []
    try:
        _verify(monkeypatch, [answer], calls)
    except Exception:  # noqa: BLE001 - the unchanged field's retry policy
        pass
    assert len(calls) == 1
