"""Model checks for acquisition ownership; live CDP checks are separate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openadapt_flow.interactive_recorder import BrowserAttachError, InteractiveRecorder


class Frame:
    def __init__(self, session):
        self.doc = f"{session._session_id}:doc:model"
        self.ordinal = 0
        self.width, self.height, self.dpr = 640, 480, 1

    def evaluate(self, _script, _session):
        return dict(
            doc=self.doc,
            ordinal=self.ordinal,
            width=self.width,
            height=self.height,
            dpr=self.dpr,
        )


@pytest.fixture
def model(tmp_path, monkeypatch):
    session = InteractiveRecorder(
        "http://127.0.0.1:34567/", tmp_path / "recording", headless=True
    )
    frame = Frame(session)
    page = SimpleNamespace(frames=[frame], main_frame=frame)
    monkeypatch.setattr(session, "page", page)
    shots = []
    hook = [None]

    def screenshot():
        shots.append(frame.ordinal)
        png = f"frame:{len(shots)}:{frame.doc}:{frame.ordinal}".encode()
        if hook[0] is not None:
            callback, hook[0] = hook[0], None
            callback()
        return png

    session.backend = SimpleNamespace(screenshot=screenshot)
    session.recorder = SimpleNamespace(_wait_settled=screenshot)
    monkeypatch.setattr(session, "_assert_no_new_pages", lambda: None)
    monkeypatch.setattr(
        session, "_structural_state", lambda: {"observed_ordinal": frame.ordinal}
    )
    monkeypatch.setattr(session, "_follow_event_page", lambda _event: None)

    def emit(kind, **extra):
        frame.ordinal += 1
        detail = dict(
            __oaflow_session=session._session_id,
            __oaflow_doc=frame.doc,
            __oaflow_ordinal=frame.ordinal,
            kind=kind,
            **extra,
        )
        if kind == "input":
            detail.update(
                field="text", __oaflow_input_session=f"{session._session_id}:input:1"
            )
        session._enqueue_browser_event(detail, source={"page": page, "frame": frame})

    return session, frame, emit, shots, hook


@pytest.mark.parametrize(
    "first,second",
    [
        ("click", "click"),
        ("input", "click"),
        ("input", "key"),
        ("scroll", "click"),
        ("key", "click"),
        ("right_click", "input"),
        ("drag", "click"),
        ("hotkey", "input"),
    ],
)
def test_distinct_action_straddling_first_capture_still_refuses(model, first, second):
    session, _frame, emit, shots, hook = model
    emit(first)
    hook[0] = lambda: emit(second)
    with pytest.raises(BrowserAttachError, match="more than one logical"):
        session._drain_event_queue()
    assert len(shots) == 1
    assert session._captured_boundary is None
    assert session._listener_error is not None


@pytest.mark.parametrize(
    "kind,count",
    [
        ("input", 1),
        ("input", 2),
        ("input", 8),
        ("input", 32),
        ("scroll", 2),
        ("scroll", 8),
    ],
)
def test_one_allowed_cohort_acquires_once_without_dropping_raw_changes(
    model, monkeypatch, kind, count
):
    session, frame, emit, shots, _hook = model
    for i in range(count):
        emit(kind, value=str(i), dy=i)
    observed = []
    monkeypatch.setattr(session, "_process", lambda event: observed.append(dict(event)))
    assert session._drain_event_queue()
    assert len(shots) == 1
    assert len(observed) == count
    assert observed[-1].get("value") == str(count - 1)
    assert session.events_kept == count
    assert frame.ordinal == count


@pytest.mark.parametrize(
    "kind",
    [
        "click",
        "double_click",
        "right_click",
        "drag",
        "input",
        "key",
        "hotkey",
        "scroll",
    ],
)
def test_later_action_during_persistence_gets_its_own_frame(model, monkeypatch, kind):
    session, _frame, emit, shots, _hook = model
    emit(kind)
    pairs = []

    def persist(_event):
        boundary = session._captured_boundary
        assert boundary is not None
        pairs.append(
            (
                boundary.png,
                boundary.structural["observed_ordinal"],
                boundary.producer[0][2],
            )
        )
        if len(pairs) == 1:
            emit("click")

    monkeypatch.setattr(session, "_process", persist)
    assert session._drain_event_queue()
    assert len(session._pyq) == 1
    assert session._drain_event_queue()
    assert len(shots) == 2
    assert [pair[1:] for pair in pairs] == [(1, 1), (2, 2)]
    assert pairs[0][0] != pairs[1][0]
    assert session.events_kept == 2


@pytest.mark.parametrize("field,value", [("width", 641), ("height", 481), ("dpr", 2)])
def test_geometry_change_discards_the_pair(model, field, value):
    session, frame, emit, _shots, hook = model
    emit("click")
    hook[0] = lambda: setattr(frame, field, value)
    with pytest.raises(BrowserAttachError, match="geometry changed"):
        session._drain_event_queue()
    assert session._captured_boundary is None


def test_navigation_discards_pre_navigation_pair_before_accepting_new_document(
    model, monkeypatch
):
    session, frame, emit, shots, hook = model
    emit("click")
    old_doc = frame.doc

    def navigate():
        frame.doc = f"{session._session_id}:doc:new"
        frame.ordinal = 0

    hook[0] = navigate
    pairs = []
    monkeypatch.setattr(
        session, "_process", lambda _event: pairs.append(session._captured_boundary)
    )
    assert session._drain_event_queue()
    assert len(shots) == 2
    assert old_doc.encode() not in pairs[0].png
    assert pairs[0].producer[0][1] == frame.doc


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("capture"),
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        SystemExit(17),
    ],
)
def test_interrupted_cohort_cannot_be_omitted_by_later_finish(
    model, monkeypatch, failure
):
    session, _frame, emit, _shots, hook = model
    emit("click")

    def interrupt():
        raise failure

    hook[0] = interrupt
    with pytest.raises(type(failure)) as caught:
        session._drain_event_queue()
    assert caught.value is failure
    assert session._captured_boundary is None
    assert session.done
    abandoned = []
    monkeypatch.setattr(session, "abort", lambda: abandoned.append(True))
    with pytest.raises(BrowserAttachError, match="interrupted"):
        session.finish()
    assert abandoned == [True]


def test_missing_producer_ordinal_is_not_an_exact_frame(model):
    session, _frame, emit, shots, _hook = model
    emit("click")
    session._pyq[0].pop("_oaflow_producer_ordinal")
    with pytest.raises(BrowserAttachError, match="no bound browser producer"):
        session._drain_event_queue()
    assert shots == []


def test_document_local_input_sessions_do_not_merge_across_documents():
    batch = [
        dict(
            kind="input",
            field="text",
            _oaflow_input_session="same",
            _oaflow_producer_doc=doc,
        )
        for doc in ["first", "second"]
    ]
    with pytest.raises(BrowserAttachError, match="crossed document"):
        InteractiveRecorder._validate_event_batch(batch)


def test_undelivered_renderer_action_does_not_seal_an_old_python_queue(model):
    session, frame, emit, shots, _hook = model
    emit("click")
    frame.ordinal += 1
    with pytest.raises(BrowserAttachError, match="order or frame geometry"):
        session._drain_event_queue()
    assert shots == []


def test_action_delivered_by_initial_stamp_is_not_a_later_framed_action(
    model, monkeypatch
):
    session, frame, emit, shots, _hook = model
    emit("click")
    original = frame.evaluate
    injected = False

    def deliver(script, session_id):
        nonlocal injected
        if not injected:
            injected = True
            emit("click")
        return original(script, session_id)

    monkeypatch.setattr(frame, "evaluate", deliver)
    with pytest.raises(BrowserAttachError, match="more than one logical"):
        session._drain_event_queue()
    assert shots == []


def test_caught_ambiguous_queued_batch_cannot_be_finished_as_complete(
    model, monkeypatch
):
    session, _frame, emit, shots, _hook = model
    emit("click")
    emit("key")
    with pytest.raises(BrowserAttachError, match="more than one logical") as first:
        session._drain_event_queue()
    assert shots == []
    abandoned = []
    monkeypatch.setattr(session, "abort", lambda: abandoned.append(True))
    with pytest.raises(BrowserAttachError) as final:
        session.finish()
    assert final.value is first.value
    assert abandoned == [True]
