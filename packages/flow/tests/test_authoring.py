"""AuthoringSession: Recorder wrap, pause/continue, compile admit gate.

Synthetic CI un-park gate for hosted authoring (openadapt-flow 449).
Implementation continues without a real bank or tax job. MockMed here is
a Playwright fixture: no PHI, CI-only, never the user's start/pack path.
"""

from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Iterator, Optional

import pytest
from PIL import Image

from openadapt_flow.authoring import (
    COACH_ONLY,
    NEEDS_HUMAN_ADMIT,
    AuthoringError,
    AuthoringSession,
    CoachOnlyError,
    MissingSecretTypeError,
    open_session,
)
from openadapt_flow.compiler.compile import compile_recording
from openadapt_flow.mockmed.server import serve

NOTE = "Patient reports mild headache for two days, advise rest and fluids"
SECRET = "ssn-000-00-0000"


def _png(size: tuple[int, int] = (1280, 800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


class ScriptedBackend:
    """In-memory Backend: constant frame, records calls, pause-target values."""

    def __init__(self) -> None:
        self._png = _png()
        self.calls: list[tuple] = []
        self.values_at: dict[tuple[int, int], Optional[str]] = {}
        self.focus_value: Optional[str] = "OS-FOCUS-SHOULD-NOT-BE-READ"

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.calls.append(("click", x, y, double))

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    def press(self, key: str) -> None:
        self.calls.append(("press", key))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))

    def text_value_at(self, x: int, y: int) -> Optional[str]:
        self.calls.append(("text_value_at", x, y))
        if (x, y) in self.values_at:
            return self.values_at[(x, y)]
        return None

    def focused_text_value(self) -> Optional[str]:
        self.calls.append(("focused_text_value",))
        return self.focus_value


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _session(
    tmp_path: Path, backend: Optional[ScriptedBackend] = None
) -> tuple[AuthoringSession, ScriptedBackend]:
    backend = backend or ScriptedBackend()
    session = AuthoringSession(
        backend,
        tmp_path / "rec",
        backend_kind="web",
        app_url="http://fake.local/",
        settle_interval_s=0.01,
        settle_stable_frames=1,
        settle_timeout_s=0.2,
    )
    return session, backend


# -- coach-only substrates ---------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["windows", "Windows", "win", "win_agent", "rdp", "citrix", "remote_display"],
)
def test_citrix_rdp_windows_refuse_agent_drive(tmp_path: Path, kind: str) -> None:
    with pytest.raises(CoachOnlyError, match=COACH_ONLY) as excinfo:
        AuthoringSession(ScriptedBackend(), tmp_path / "rec", backend_kind=kind)
    assert excinfo.value.code == COACH_ONLY
    assert not hasattr(AuthoringSession, "record_injected")


def test_authoring_module_does_not_spawn_win_agent_or_copy_replay_mcp() -> None:
    import openadapt_flow.authoring as authoring_mod

    src = Path(authoring_mod.__file__).read_text()
    assert "from openadapt_flow.backends" not in src
    assert "launch_agent" not in src
    assert "parallels_vm" not in src
    assert "emit.mcp_tool" not in src
    assert "emit/mcp_tool" not in src


def test_open_session_constructs_authoring_session(tmp_path: Path) -> None:
    session = open_session(
        ScriptedBackend(),
        tmp_path / "rec",
        backend_kind="macos",
    )
    assert isinstance(session, AuthoringSession)
    assert session.backend_kind == "macos"


def test_unknown_backend_kind_is_not_silently_web(tmp_path: Path) -> None:
    with pytest.raises(AuthoringError, match="unknown backend_kind") as excinfo:
        AuthoringSession(ScriptedBackend(), tmp_path / "rec", backend_kind="turbo")
    assert excinfo.value.code == "unknown_backend"


# -- scripted actuate through Recorder ---------------------------------------


def test_scripted_actuate_records_events_and_frames(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.start_record()
    session.click(10, 20)
    session.type_text("hello")
    session.press("Enter")
    rec_dir = session.stop_record()

    events = _events(rec_dir)
    assert [e["kind"] for e in events] == ["click", "type", "key"]
    assert events[0]["x"] == 10 and events[0]["y"] == 20
    assert events[1]["text"] == "hello"
    assert events[2]["key"] == "Enter"
    for i in range(3):
        for suffix in ("before", "after"):
            assert (rec_dir / "frames" / f"{i:04d}_{suffix}.png").exists()
    assert backend.calls == [
        ("click", 10, 20, False),
        ("type_text", "hello"),
        ("press", "Enter"),
    ]


def test_click_node_id_uses_backend_pixels_center(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_9f2c", {"x": 100, "y": 200, "w": 40, "h": 20})
    session.start_record()
    session.click(node_id="n_9f2c")
    session.stop_record()
    assert ("click", 120, 210, False) in backend.calls


def test_unknown_node_id_is_stale_and_does_not_click(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.start_record()
    with pytest.raises(AuthoringError) as excinfo:
        session.click(node_id="n_missing")
    assert excinfo.value.code == "stale_node"
    assert backend.calls == []


# -- continue: record_observed, not type_text, not OS focus ------------------


def test_continue_does_not_invoke_backend_type_text(tmp_path: Path) -> None:
    """REQUIRED: Continue records the human type without actuating it again."""

    session, backend = _session(tmp_path)
    session.remember_node("n_note", {"x": 40, "y": 50, "w": 20, "h": 20})
    backend.values_at[(50, 60)] = NOTE
    session.start_record()
    recorder = session._recorder
    assert recorder is not None
    recorder_type_calls: list[tuple[str, Optional[str]]] = []
    original_recorder_type = recorder.type_text

    def _spy_recorder_type(text: str, param: Optional[str] = None) -> None:
        recorder_type_calls.append((text, param))
        original_recorder_type(text, param=param)

    recorder.type_text = _spy_recorder_type  # type: ignore[method-assign]
    session.click(node_id="n_note")
    paused = session.pause_for_input(param="note", node_id="n_note")
    assert paused == {"paused": True, "param": "note", "secret": False}
    assert "recorded" not in paused
    assert "value" not in paused
    type_calls_before = [c for c in backend.calls if c[0] == "type_text"]
    result = session.continue_input()
    rec_dir = session.stop_record()

    assert recorder_type_calls == []
    assert result == {"recorded": True, "param": "note"}
    assert "value" not in result
    assert NOTE not in json.dumps(result)
    assert [c for c in backend.calls if c[0] == "type_text"] == type_calls_before
    assert ("focused_text_value",) not in backend.calls
    assert ("text_value_at", 50, 60) in backend.calls

    events = _events(rec_dir)
    typed = [e for e in events if e["kind"] == "type"]
    assert len(typed) == 1
    assert typed[0]["param"] == "note"
    assert typed[0]["text"] == NOTE
    blob = (rec_dir / "events.jsonl").read_text()
    assert "OS-FOCUS-SHOULD-NOT-BE-READ" not in blob


def test_continue_reads_pause_target_not_a_later_focus_point(
    tmp_path: Path,
) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_note", {"x": 10, "y": 10, "w": 10, "h": 10})
    backend.values_at[(15, 15)] = NOTE
    backend.values_at[(400, 400)] = "wrong-field"
    session.start_record()
    session.pause_for_input(param="note", node_id="n_note")
    # Overlay Continue can move OS focus; the session must ignore it.
    backend.focus_value = "overlay-stole-focus"
    backend.values_at[(15, 15)] = NOTE
    session.continue_input()
    rec_dir = session.stop_record()
    (typed,) = [e for e in _events(rec_dir) if e["kind"] == "type"]
    assert typed["text"] == NOTE
    assert ("focused_text_value",) not in backend.calls


def test_secret_continue_persists_no_text_and_skips_type_text(
    tmp_path: Path,
) -> None:
    session, backend = _session(tmp_path)
    pixels = {"x": 100, "y": 200, "w": 300, "h": 40}
    session.remember_node("n_ssn", pixels)
    backend.values_at[(250, 220)] = SECRET
    session.start_record()
    session.pause_for_input(param="ssn", secret=True, node_id="n_ssn")
    result = session.continue_input()
    rec_dir = session.stop_record()

    assert result == {"recorded": True, "param": "ssn"}
    assert SECRET not in json.dumps(result)
    assert not any(c[0] == "type_text" for c in backend.calls)
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["secret_params"] == ["ssn"]
    assert "ssn" not in meta["params"]
    blob = (rec_dir / "meta.json").read_text() + (rec_dir / "events.jsonl").read_text()
    assert SECRET not in blob
    (event,) = [e for e in _events(rec_dir) if e["kind"] == "type"]
    assert event["param"] == "ssn"
    assert event["secret"] is True
    assert "text" not in event
    # Pause-target bounds are blacked out in both frames (no secret pixels).
    for suffix in ("before", "after"):
        with Image.open(rec_dir / "frames" / f"0000_{suffix}.png") as frame:
            region = frame.convert("RGB").crop((100, 200, 400, 240))
            assert region.getextrema() == ((0, 0), (0, 0), (0, 0))


def test_click_while_paused_is_refused(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_note", {"x": 0, "y": 0, "w": 10, "h": 10})
    session.start_record()
    session.pause_for_input(param="note", node_id="n_note")
    with pytest.raises(AuthoringError) as excinfo:
        session.click(node_id="n_note")
    assert excinfo.value.code == "paused"
    assert backend.calls == []


def test_non_secret_empty_field_stays_paused(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_note", {"x": 0, "y": 0, "w": 10, "h": 10})
    backend.values_at[(5, 5)] = ""
    session.start_record()
    session.pause_for_input(param="note", node_id="n_note")
    result = session.continue_input()
    assert result["recorded"] is False
    assert result["reason"] == "empty_field"
    assert not any(c[0] == "type_text" for c in backend.calls)
    rec_dir = session.stop_record()
    assert [e["kind"] for e in _events(rec_dir)] == []


def test_continue_uses_playwright_input_value_when_text_value_at_missing(
    tmp_path: Path,
) -> None:
    class PageOnlyBackend:
        """Playwright-shaped backend with page.evaluate and no text_value_at."""

        def __init__(self) -> None:
            self._png = _png()
            self.calls: list[tuple] = []
            self.page = self

        @property
        def viewport(self) -> tuple[int, int]:
            return (1280, 800)

        def screenshot(self) -> bytes:
            return self._png

        def click(self, x: int, y: int, *, double: bool = False) -> None:
            self.calls.append(("click", x, y, double))

        def type_text(self, text: str) -> None:
            self.calls.append(("type_text", text))

        def press(self, key: str) -> None:
            self.calls.append(("press", key))

        def scroll(self, dx: int, dy: int) -> None:
            self.calls.append(("scroll", dx, dy))

        def evaluate(self, _script: str, _args: list[int]) -> str:
            self.calls.append(("evaluate", tuple(_args)))
            return NOTE

    backend = PageOnlyBackend()
    assert not hasattr(backend, "text_value_at")
    assert not hasattr(backend, "focused_text_value")
    session = AuthoringSession(
        backend,
        tmp_path / "rec",
        backend_kind="web",
        settle_interval_s=0.01,
        settle_stable_frames=1,
        settle_timeout_s=0.2,
    )
    session.remember_node("n_note", {"x": 40, "y": 50, "w": 20, "h": 20})
    session.start_record()
    session.pause_for_input(param="note", node_id="n_note")
    result = session.continue_input()
    rec_dir = session.stop_record()
    assert result == {"recorded": True, "param": "note"}
    assert not any(c[0] == "type_text" for c in backend.calls)
    assert ("evaluate", (50, 60)) in backend.calls
    (typed,) = [e for e in _events(rec_dir) if e["kind"] == "type"]
    assert typed["text"] == NOTE
    assert typed["param"] == "note"


def test_empty_readable_secret_field_stays_paused(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_ssn", {"x": 0, "y": 0, "w": 10, "h": 10})
    backend.values_at[(5, 5)] = ""
    session.start_record()
    session.pause_for_input(param="ssn", secret=True, node_id="n_ssn")
    result = session.continue_input()
    assert result["recorded"] is False
    assert result["reason"] == "empty_secret_field"
    assert not any(c[0] == "type_text" for c in backend.calls)
    rec_dir = session.stop_record()
    assert [e["kind"] for e in _events(rec_dir)] == []


def test_masked_empty_secret_records_when_operator_confirms(
    tmp_path: Path,
) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_ssn", {"x": 0, "y": 0, "w": 10, "h": 10})
    # AX reports empty/unavailable on a masked field.
    backend.values_at[(5, 5)] = None
    session.start_record()
    session.pause_for_input(param="ssn", secret=True, node_id="n_ssn")
    result = session.continue_input(operator_confirmed=True)
    rec_dir = session.stop_record()
    assert result == {"recorded": True, "param": "ssn"}
    (event,) = [e for e in _events(rec_dir) if e["kind"] == "type"]
    assert event["secret"] is True
    assert "text" not in event
    assert not any(c[0] == "type_text" for c in backend.calls)


# -- compile wrapper ---------------------------------------------------------


def test_compile_recording_signature_is_unchanged() -> None:
    sig = inspect.signature(compile_recording)
    params = list(sig.parameters)
    assert params[0] == "recording_dir"
    assert params[1] == "out_bundle_dir"
    assert sig.parameters["name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.return_annotation is not dict


def test_compile_returns_needs_human_admit_and_calls_compile_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openadapt_flow.authoring as authoring_mod

    session, _backend = _session(tmp_path)
    session.start_record()
    session.click(10, 20)
    session.type_text(NOTE, param="note")
    session.stop_record()

    real = authoring_mod.compile_recording
    calls: list[tuple] = []

    def wrapped(recording_dir, out_bundle_dir, *, name, **kwargs):
        calls.append((Path(recording_dir), Path(out_bundle_dir), name, kwargs))
        return real(recording_dir, out_bundle_dir, name=name, **kwargs)

    monkeypatch.setattr(authoring_mod, "compile_recording", wrapped)
    result = session.compile(tmp_path / "bundle", name="authoring-demo")
    assert calls, "compile wrapper must call compile_recording"
    assert calls[0][0] == tmp_path / "rec"
    assert calls[0][2] == "authoring-demo"
    assert result["status"] == NEEDS_HUMAN_ADMIT
    assert result["workflow_id"].startswith("wf_")
    assert result["recording_retained"] is True
    dumped = json.dumps(result)
    assert "VERIFIED" not in dumped
    assert "execution_outcome" not in result
    assert result.get("success") is not True


def test_compile_refuses_secret_pause_without_type_param(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_ssn", {"x": 0, "y": 0, "w": 10, "h": 10})
    backend.values_at[(5, 5)] = ""
    session.start_record()
    session.click(node_id="n_ssn")
    session.pause_for_input(param="ssn", secret=True, node_id="n_ssn")
    # Empty readable field stays paused; stop without Continue → no TYPE/param.
    assert session.continue_input()["recorded"] is False
    session.stop_record()
    with pytest.raises(MissingSecretTypeError) as excinfo:
        session.compile(tmp_path / "bundle", name="missing-secret")
    assert excinfo.value.code == "missing_secret_type"
    assert excinfo.value.param == "ssn"


def test_halt_refuses_later_compile(tmp_path: Path) -> None:
    session, _backend = _session(tmp_path)
    session.start_record()
    session.click(10, 20)
    session.halt()
    with pytest.raises(AuthoringError) as excinfo:
        session.compile(tmp_path / "bundle", name="halted")
    assert excinfo.value.code == "halted"


def test_compile_refuses_while_paused(tmp_path: Path) -> None:
    session, _backend = _session(tmp_path)
    session.remember_node("n_note", {"x": 0, "y": 0, "w": 10, "h": 10})
    session.start_record()
    session.pause_for_input(param="note", node_id="n_note")
    with pytest.raises(AuthoringError) as excinfo:
        session.compile(tmp_path / "bundle", name="still-paused")
    assert excinfo.value.code == "paused"


def test_compile_without_args_still_wraps_compile_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openadapt_flow.authoring as authoring_mod

    session, _backend = _session(tmp_path)
    session.start_record()
    session.click(10, 20)
    session.stop_record()
    calls: list[tuple] = []
    real = authoring_mod.compile_recording

    def wrapped(recording_dir, out_bundle_dir, *, name, **kwargs):
        calls.append((Path(recording_dir), Path(out_bundle_dir), name))
        return real(recording_dir, out_bundle_dir, name=name, **kwargs)

    monkeypatch.setattr(authoring_mod, "compile_recording", wrapped)
    result = session.compile()
    assert calls == [(tmp_path / "rec", tmp_path / "rec-bundle", "authoring")]
    assert result["status"] == NEEDS_HUMAN_ADMIT
    assert "VERIFIED" not in json.dumps(result)


def test_compile_accepts_secret_pause_after_continue(tmp_path: Path) -> None:
    session, backend = _session(tmp_path)
    session.remember_node("n_ssn", {"x": 0, "y": 0, "w": 10, "h": 10})
    backend.values_at[(5, 5)] = None
    session.start_record()
    session.pause_for_input(param="ssn", secret=True, node_id="n_ssn")
    session.continue_input()
    result = session.compile(tmp_path / "bundle", name="secret-ok")
    assert result["status"] == NEEDS_HUMAN_ADMIT
    assert "VERIFIED" not in json.dumps(result)


# -- Synthetic CI gate (MockMed Playwright fixture; not a user-facing job) --


@pytest.fixture(scope="module")
def mockmed_url() -> Iterator[str]:
    url, stop = serve(port=0)
    yield url
    stop()


def _box(page, selector: str) -> dict:
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    box = locator.bounding_box()
    if box is None:  # pragma: no cover - visible => box exists
        raise RuntimeError(f"no bounding box for {selector!r}")
    return box


def test_mockmed_continue_uses_text_value_at_not_type_text(
    tmp_path: Path, mockmed_url: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    backend, close = PlaywrightBackend.launch(mockmed_url, headless=True)
    try:
        page = backend.page
        page.fill("#username", "nurse.demo")
        page.fill("#password", "mockmed-demo-pass")
        page.click("#signin")
        page.wait_for_selector("#tasks-table")
        page.locator(".open-btn").first.click()
        page.wait_for_selector("#new-encounter")
        page.click("#new-encounter")
        page.wait_for_selector("#note")
        page.click("#type-triage")

        typed: list[str] = []
        original_type_text = backend.type_text

        def _spy_type_text(text: str) -> None:
            typed.append(text)
            original_type_text(text)

        backend.type_text = _spy_type_text  # type: ignore[method-assign]

        note_box = _box(page, "#note")
        session = AuthoringSession(
            backend,
            tmp_path / "rec",
            backend_kind="web",
            app_url=mockmed_url,
            settle_interval_s=0.05,
            settle_stable_frames=1,
            settle_timeout_s=2.0,
        )
        session.remember_node(
            "n_note",
            {
                "x": int(note_box["x"]),
                "y": int(note_box["y"]),
                "w": int(note_box["width"]),
                "h": int(note_box["height"]),
            },
        )
        session.start_record()
        session.click(node_id="n_note")
        session.pause_for_input(param="note", secret=False, node_id="n_note")
        # Person types in the application. Continue must not type again.
        page.fill("#note", NOTE)
        result = session.continue_input()
        rec_dir = session.stop_record()
        compiled = session.compile(tmp_path / "bundle", name="mockmed-note")
    finally:
        close()

    assert typed == []
    assert result == {"recorded": True, "param": "note"}
    assert NOTE not in json.dumps(result)
    events = _events(rec_dir)
    typed_events = [e for e in events if e.get("kind") == "type"]
    assert len(typed_events) == 1
    assert typed_events[0]["param"] == "note"
    assert typed_events[0]["text"] == NOTE
    assert "secret" not in typed_events[0]
    assert compiled["status"] == NEEDS_HUMAN_ADMIT
    assert compiled["workflow_id"].startswith("wf_")
    assert compiled["recording_retained"] is True
    assert "VERIFIED" not in json.dumps(compiled)
