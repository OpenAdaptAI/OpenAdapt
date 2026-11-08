"""Deferred browser actions retain one positively known document owner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from openadapt_flow.interactive_recorder import BrowserAttachError, InteractiveRecorder
from tests.test_interactive_frame_boundary import model as model


_DEFERRED = (
    ("input", "input", "_pending_type"),
    ("scroll", "scroll", "_pending_scroll"),
    ("click", "double_click", "_pending_click"),
)


def _event(kind, owner, value="first"):
    return {
        "kind": kind,
        "_oaflow_producer_doc": owner,
        "_oaflow_input_session": "repeated-input-session",
        "field": "shared",
        "value": value,
        "dx": 0,
        "dy": 7,
        "x": 10,
        "y": 20,
    }


@given(
    path=st.sampled_from(_DEFERRED),
    previous=st.sampled_from([None, "", "doc:first", "doc:second"]),
    current=st.sampled_from([None, "", "doc:first", "doc:second"]),
    field=st.sampled_from(["shared", "another"]),
    input_session=st.sampled_from(["repeated-input-session", "another-input-session"]),
)
def test_all_deferred_merge_paths_require_positive_document_equality(
    path, previous, current, field, input_session
):
    first, second, attribute = path
    session = InteractiveRecorder(
        "http://127.0.0.1:34567/", Path("unused-model-recording"), headless=True
    )
    recorded = []
    session.recorder = SimpleNamespace(
        record_observed=lambda event, **evidence: recorded.append((event, evidence))
    )
    session.backend = SimpleNamespace()
    session._last_frame = b"before-first"
    session._last_structural = {"title": "First view"}
    session._captured_boundary = SimpleNamespace(
        png=b"after-first", structural={"title": "First view"}
    )
    session._process(_event(first, previous))
    pending = getattr(session, attribute)
    assert pending is not None
    retained = dict(pending)
    session._captured_boundary = SimpleNamespace(
        png=b"after-second", structural={"title": "Second view"}
    )
    incoming = _event(second, current, "second")
    incoming["field"] = field
    incoming["_oaflow_input_session"] = input_session
    if not previous or previous != current:
        with pytest.raises(BrowserAttachError, match="producer ownership"):
            session._process(incoming)
        assert getattr(session, attribute) is pending
        assert pending == retained
        assert recorded == []
        assert session._last_frame == b"before-first"
    else:
        session._process(incoming)
        if first == "input":
            assert session._pending_type["value"] == "second"
            session._flush_type()
        elif first == "scroll":
            assert session._pending_scroll["dy"] == 14
            session._flush_scroll()
        separate_inputs = first == "input" and (
            field != "shared" or input_session != "repeated-input-session"
        )
        assert len(recorded) == (2 if separate_inputs else 1)
        assert recorded[0][1]["before_png"] == b"before-first"
        assert recorded[-1][1]["after_png"] == b"after-second"
        if separate_inputs:
            assert recorded[0][1]["after_png"] == b"after-first"
            assert recorded[1][1]["before_png"] == b"after-first"


@pytest.mark.parametrize("first,second,attribute", _DEFERRED)
@pytest.mark.parametrize("transition", ["new-document", "unknown-pending-owner"])
def test_later_acquired_batch_cannot_overwrite_a_different_document(
    model, monkeypatch, first, second, attribute, transition
):
    session, frame, emit, shots, _hook = model
    session._last_frame = b"before-first"
    session._last_structural = {"title": "First view"}
    recorded = []
    monkeypatch.setattr(
        session.recorder,
        "record_observed",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
        raising=False,
    )
    emit(first, value="alpha", dx=0, dy=7, x=10, y=20)
    assert session._drain_event_queue()
    pending = getattr(session, attribute)
    assert pending is not None
    if transition == "new-document":
        frame.doc = f"{session._session_id}:doc:second"
        frame.ordinal = 0
    else:
        pending.pop("_oaflow_producer_doc")
    retained = dict(pending)
    emit(second, value="beta", dx=0, dy=7, x=10, y=20)
    with pytest.raises(BrowserAttachError, match="producer ownership") as caught:
        session._drain_event_queue()
    assert session._listener_error is caught.value
    assert session.done
    assert getattr(session, attribute) is pending
    assert pending == retained
    assert len(shots) == 2
    assert recorded == []
    aborted = []
    monkeypatch.setattr(session, "abort", lambda: aborted.append(True))
    with pytest.raises(BrowserAttachError, match="producer ownership"):
        session.finish()
    assert aborted == [True]


@pytest.mark.parametrize("first,second,attribute", _DEFERRED)
def test_same_document_acquired_batches_still_coalesce(
    model, monkeypatch, first, second, attribute
):
    session, _frame, emit, shots, _hook = model
    session._last_frame = b"before-first"
    session._last_structural = {"title": "First view"}
    recorded = []
    monkeypatch.setattr(
        session.recorder,
        "record_observed",
        lambda event, **evidence: recorded.append((event, evidence)),
        raising=False,
    )
    emit(first, value="alpha", dx=0, dy=7, x=10, y=20)
    assert session._drain_event_queue()
    emit(second, value="beta", dx=0, dy=7, x=10, y=20)
    assert session._drain_event_queue()
    if first == "input":
        assert session._pending_type["value"] == "beta"
        session._flush_type()
    elif first == "scroll":
        assert session._pending_scroll["dy"] == 14
        session._flush_scroll()
    else:
        assert session._pending_click is None
    assert len(shots) == 2
    assert len(recorded) == 1
    assert recorded[0][1]["after_png"].startswith(b"frame:2:")
