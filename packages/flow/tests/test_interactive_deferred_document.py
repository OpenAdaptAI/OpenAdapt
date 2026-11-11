"""Deferred browser actions retain one positively known document owner."""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.interactive_recorder import BrowserAttachError, InteractiveRecorder
from openadapt_flow.recorder import Recorder

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
    session, scene, recorded = _current_model()
    session._process(_event(first, previous))
    pending = getattr(session, attribute)
    assert pending is not None
    retained = dict(pending)
    scene.png, scene.structural = b"after-second", {"title": "Second view"}
    incoming = _event(second, current, "second")
    incoming["field"] = field
    incoming["_oaflow_input_session"] = input_session
    if not previous or previous != current:
        with pytest.raises(BrowserAttachError, match="producer ownership"):
            if previous and current and previous != current:
                # Distinct documents in one acquired batch cannot be flushed:
                # no intermediate frame exists between these queued actions.
                session._validate_owned_event_batch([_event(first, previous), incoming])
            else:
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
        # Another old-document action and the new-document action arrive in
        # one undrained batch. The already retained pending action must survive.
        emit(first, value="queued-first", dx=0, dy=7, x=10, y=20)
        frame.doc = f"{session._session_id}:doc:second"
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
    assert len(shots) == 1  # Refusal precedes any second capture.
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
    monkeypatch.setattr(session.recorder, "_commit", _commit_observer(recorded))
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
    assert len(shots) == (3 if first == "click" else 2)
    assert len(recorded) == 1
    assert recorded[0][1]["after_png"].startswith(b"frame:2:")


def _commit_observer(recorded):
    def capture(event, before_png, after_png, structural_before, **evidence):
        recorded.append((event, {
            "before_png": before_png, "after_png": after_png,
            "structural_before": structural_before, **evidence,
        }))
    return capture


def _current_model():
    """Current processor and genuine Recorder consumer, with modeled pixels.

    The consumer's actual fallback capture runs when no pre-captured after
    frame was supplied. Only its final filesystem commit is observed here.
    Real CDP controls separately exercise the actual browser and filesystem.
    """
    session = InteractiveRecorder(
        "http://127.0.0.1:34567/", Path("unused-current-recording"), headless=True
    )
    scene = SimpleNamespace(png=b"after-first", structural={"title": "First view"})
    recorded = []
    recorder = SimpleNamespace(
        _wait_settled=lambda: scene.png,
        _cancel_native_select=lambda: None,
        _claim_new_pages=lambda index: 0,
        _closed_its_page=lambda event: False,
        _commit=_commit_observer(recorded),
        _i=0,
    )
    recorder.record_observed = MethodType(Recorder.record_observed, recorder)
    session.recorder = recorder
    session.backend = SimpleNamespace(screenshot=lambda: scene.png)
    session._structural_state = lambda: dict(scene.structural)
    session._wait_for_writes = lambda: False
    session._last_frame = b"before-first"
    session._last_structural = {"title": "First view"}
    return session, scene, recorded


def _queued_model():
    session, frame, recorded = _current_model()
    frame.doc = f"{session._session_id}:doc:first"
    shots = []

    def screenshot():
        shots.append(frame.doc)
        return f"frame:{len(shots)}:{frame.doc}".encode()

    session.backend.screenshot = screenshot
    session.recorder._wait_settled = screenshot

    def emit(kind, **values):
        detail = {
            "kind": kind, "field": "shared", **values,
            "__oaflow_session": session._session_id,
            "__oaflow_doc": frame.doc,
            "__oaflow_origin": "http://127.0.0.1:34567",
            "__oaflow_input_session": f"{session._session_id}:input:1",
            "__oaflow_top_level": True,
        }
        session._enqueue_browser_event(detail)

    return session, frame, emit, shots, recorded


@pytest.fixture
def model():
    return _queued_model()


@pytest.mark.parametrize("path", _DEFERRED)
@given(suffix=st.text(alphabet="abc012", min_size=1, max_size=12))
def test_known_document_navigation_preserves_both_deferred_actions(path, suffix):
    first, second, attribute = path
    session, scene, recorded = _current_model()
    session._process(_event(first, "doc:first:" + suffix, "alpha"))
    pending = getattr(session, attribute)
    assert pending is not None
    assert pending["_oaflow_producer_doc"] == "doc:first:" + suffix
    scene.png, scene.structural = b"after-second", {"title": "Second view"}
    session._process(_event(second, "doc:second:" + suffix, "beta"))
    session._flush_type()
    session._flush_scroll()
    session._flush_click()
    assert len(recorded) == 2
    assert recorded[0][1]["before_png"] == b"before-first"
    assert recorded[0][1]["after_png"] == b"after-first"
    assert recorded[1][1]["before_png"] == b"after-first"
    if first == "input":
        assert [row[0]["text"] for row in recorded] == ["alpha", "beta"]
    elif first == "scroll":
        assert [row[0]["dy"] for row in recorded] == [7, 7]
    else:
        assert [row[0]["kind"] for row in recorded] == ["click", "double_click"]
    assert session._listener_error is None
    assert not session.done


@pytest.mark.parametrize("first,second,attribute", _DEFERRED)
def test_mixed_document_batch_refusal_is_sticky_before_pending_mutation(
    first, second, attribute
):
    session, _scene, recorded = _current_model()
    session._process(_event(first, "doc:first", "alpha"))
    pending = getattr(session, attribute)
    assert pending is not None
    retained = dict(pending)
    session._pyq.extend(
        [_event(first, "doc:first", "alpha"), _event(second, "doc:second", "beta")]
    )
    with pytest.raises(BrowserAttachError, match="producer ownership") as caught:
        session._drain_event_queue()
    assert session._listener_error is caught.value
    assert session.done
    assert getattr(session, attribute) is pending
    assert pending == retained
    assert recorded == []
    with pytest.raises(BrowserAttachError):
        session._drain_event_queue()
    assert recorded == []


@pytest.mark.parametrize("first,second,attribute", _DEFERRED)
@pytest.mark.parametrize("order", ["known-first", "known-second", "all-unknown"])
@pytest.mark.parametrize("already_pending", [False, True])
def test_unknown_batch_ownership_refuses_before_any_process_or_capture(
    first, second, attribute, order, already_pending
):
    # Each JSON type scenario starts with a fresh processor and consumer. These
    # inner scenarios are not additional pytest case credit.
    for unknown in (None, "", "   ", False, 0, [], {}):
        session, frame, emit, shots, recorded = _queued_model()
        known = frame.doc
        if already_pending:
            emit(first, value="retained-alpha", dx=0, dy=7, x=10, y=20)
            assert session._drain_event_queue()
        pending = getattr(session, attribute)
        retained = None if pending is None else dict(pending)
        before_shots = list(shots)
        first_owner = known if order == "known-first" else unknown
        second_owner = known if order == "known-second" else unknown
        frame.doc = first_owner
        emit(first, value="queued-alpha", dx=0, dy=7, x=10, y=20)
        frame.doc = second_owner
        emit(second, value="queued-beta", dx=0, dy=7, x=10, y=20)
        assert len(session._pyq) == 2
        processed = []
        process_event = session._process

        def observe(event):
            processed.append(event)
            return process_event(event)

        session._process = observe
        with pytest.raises(BrowserAttachError, match="producer ownership") as caught:
            session._drain_event_queue()
        assert session._listener_error is caught.value
        assert session.done
        assert processed == []
        assert session.events_kept == (1 if already_pending else 0)
        assert shots == before_shots
        assert recorded == []
        assert getattr(session, attribute) is pending
        assert pending == retained
        aborted = []
        session.abort = lambda: aborted.append(True)
        with pytest.raises(BrowserAttachError, match="producer ownership"):
            session.finish()
        assert aborted == [True]
        assert processed == [] and shots == before_shots and recorded == []


@pytest.mark.parametrize(
    "batch",
    [
        [{"kind": "click"}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "click"},
        ],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "key", "key": "Enter"},
        ],
        [{"kind": "scroll", "dy": 10}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "input", "field": "note", "_oaflow_input_session": "b"},
        ],
    ],
)
def test_known_document_does_not_waive_prior_logical_batch_refusals(batch):
    for event in batch:
        event["_oaflow_producer_doc"] = "known-document"
    with pytest.raises(BrowserAttachError, match="more than one logical"):
        InteractiveRecorder._validate_event_batch(batch)
