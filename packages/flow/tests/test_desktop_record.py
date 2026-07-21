"""Tests for the live desktop-recording orchestration + CLI wiring.

The capture stack (openadapt-capture) and the format conversion
(``adapters.capture.convert_capture``) are tested elsewhere
(``tests/test_capture_adapter.py``); this module tests the thin orchestration
that ``record --backend windows|rdp`` adds on top — start a capture session,
wait for the operator to stop, then convert — with the recorder + converter
injected so no live display is needed, plus the CLI argument wiring.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.desktop_record import record_desktop_capture

VIEWPORT = (800, 600)
SEARCH = (120, 90)
NOTE = (250, 180)
SAVE = (330, 420)


def _render_form(note_text: str = "") -> bytes:
    """A deterministic fake 'form' window; distinct widgets at each click target
    so template matching resolves each click unambiguously at replay."""
    img = Image.new("RGB", VIEWPORT, (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, VIEWPORT[0], 40], fill=(30, 60, 120))
    d.rectangle([70, 70, 170, 110], fill=(60, 120, 200), outline=(0, 0, 0), width=2)
    d.rectangle([80, 80, 160, 100], outline=(255, 255, 255), width=2)
    d.rectangle([160, 150, 340, 210], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
    if note_text:
        d.text((168, 172), note_text, fill=(0, 0, 0))
    d.rectangle([280, 400, 380, 440], fill=(40, 160, 80), outline=(0, 0, 0), width=2)
    d.rectangle([290, 410, 370, 430], outline=(255, 255, 255), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_desktop_recording(rec: Path) -> None:
    """Hand-write a recording in the EXACT shape ``convert_capture`` emits
    (meta.json + events.jsonl + frames/, no structural locators — the offline
    desktop case), so compile -> replay can be proven deterministically."""
    frames = rec / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    empty = _render_form()
    filled = _render_form("hello")
    # (before, after) per step; after_i == before_{i+1} (frame chaining).
    steps = [
        ({"kind": "click", "x": SEARCH[0], "y": SEARCH[1]}, empty, empty),
        ({"kind": "click", "x": NOTE[0], "y": NOTE[1]}, empty, empty),
        ({"kind": "type", "text": "hello", "param": "note"}, empty, filled),
        ({"kind": "click", "x": SAVE[0], "y": SAVE[1]}, filled, filled),
    ]
    lines = []
    for i, (ev, before, after) in enumerate(steps):
        (frames / f"{i:04d}_before.png").write_bytes(before)
        (frames / f"{i:04d}_after.png").write_bytes(after)
        lines.append(json.dumps({"i": i, **ev, "t": float(i)}))
    (rec / "events.jsonl").write_text("\n".join(lines) + "\n")
    (rec / "meta.json").write_text(
        json.dumps(
            {
                "id": "deadbeef",
                "created_at": "2026-07-14T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": None,
                "params": {"note": "hello"},
                "source": "openadapt-capture",
            }
        )
    )


class _ReplayFrameBackend:
    """Renders the SAME deterministic form the recording was captured against.

    Because the layout is identical, templates cropped at compile time match the
    live-rendered frame exactly (template rung resolves every click); typing
    updates the note field so the replayer's typed-input OCR verification reads
    back the actual value it just typed (honest, not stubbed)."""

    def __init__(self) -> None:
        self.note_text = ""
        self.clicks: list[tuple[int, int]] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return VIEWPORT

    def screenshot(self) -> bytes:
        return _render_form(self.note_text)

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.clicks.append((x, y))

    def type_text(self, text: str) -> None:
        self.note_text = text

    def press(self, key: str) -> None:
        pass

    def scroll(self, dx: int, dy: int) -> None:
        pass


def test_desktop_recording_compiles_and_replays(tmp_path: Path) -> None:
    """A desktop-shaped recording (the convert_capture output shape) compiles
    into a bundle and REPLAYS to completion through the desktop backend path,
    resolving each click to its recorded target (no wrong-location action)."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    rec = tmp_path / "rec"
    _write_desktop_recording(rec)

    bundle = tmp_path / "bundle"
    workflow = compile_recording(rec, bundle, name="desktop_note")
    clicks = [
        s
        for s in workflow.steps
        if getattr(s.action, "value", s.action) in ("click", "double_click")
    ]
    assert len(clicks) == 3

    backend = _ReplayFrameBackend()
    report = Replayer(backend).run(
        Workflow.load(bundle),
        params={"note": "world"},
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )
    assert report.success, f"replay did not complete: {report}"
    assert len(backend.clicks) == 3
    for (cx, cy), (tx, ty) in zip(backend.clicks, [SEARCH, NOTE, SAVE]):
        assert abs(cx - tx) <= 20 and abs(cy - ty) <= 20


class _FakeRecorder:
    """Stands in for ``openadapt_capture.Recorder`` (a context manager)."""

    def __init__(self, task_description: str, capture_dir: str, log: list) -> None:
        self.task_description = task_description
        self.capture_dir = capture_dir
        self._log = log
        self.ready = False
        self.entered = False
        self.exited = False

    def __enter__(self) -> "_FakeRecorder":
        self.entered = True
        self._log.append(("enter", self.capture_dir))
        return self

    def __exit__(self, *exc: Any) -> None:
        self.exited = True
        self._log.append(("exit", self.capture_dir))

    def wait_for_ready(self, timeout: float = 60) -> bool:
        self.ready = True
        return True


def _make(log: list) -> Any:
    def factory(task: str, cap_dir: str) -> _FakeRecorder:
        rec = _FakeRecorder(task, cap_dir, log)
        return rec

    return factory


def test_orchestration_captures_then_converts(tmp_path: Path) -> None:
    log: list = []
    convert_calls: list = []

    def fake_convert(
        cap_dir: Path, out_dir: Path, *, params: Optional[dict] = None
    ) -> Path:
        # Conversion MUST happen after the recorder context has exited (session
        # fully written) — assert the ordering via the shared log.
        convert_calls.append((Path(cap_dir), Path(out_dir), dict(params or {})))
        assert log[-1][0] == "exit"
        (Path(out_dir) / "events.jsonl").write_text("{}\n")
        return Path(out_dir)

    out = record_desktop_capture(
        tmp_path / "rec",
        task_description="triage note",
        params={"note": "hello"},
        recorder_factory=_make(log),
        convert=fake_convert,
        stop=lambda: True,  # stop immediately (no wait loop)
        announce=False,
    )

    assert out == tmp_path / "rec"
    # Recorder was entered, made ready, and exited (stopped) before conversion.
    assert [k for k, _ in log] == ["enter", "exit"]
    assert len(convert_calls) == 1
    cap_dir, out_dir, params = convert_calls[0]
    assert cap_dir == tmp_path / "rec" / ".capture"
    assert out_dir == tmp_path / "rec"
    assert params == {"note": "hello"}
    assert cap_dir.is_dir()  # capture session dir was created


def test_orchestration_uses_explicit_capture_dir(tmp_path: Path) -> None:
    log: list = []
    seen: dict = {}

    def fake_convert(cap_dir, out_dir, *, params=None):
        seen["cap"] = Path(cap_dir)
        return Path(out_dir)

    record_desktop_capture(
        tmp_path / "rec",
        capture_dir=tmp_path / "raw",
        recorder_factory=_make(log),
        convert=fake_convert,
        stop=lambda: True,
        announce=False,
    )
    assert seen["cap"] == tmp_path / "raw"
    assert (tmp_path / "raw").is_dir()


def test_default_recorder_factory_requires_capture_extra() -> None:
    """Without openadapt-capture the factory raises a clear install hint."""
    import builtins
    import importlib

    from openadapt_flow import desktop_record

    real_import = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any):
        if name == "openadapt_capture" or name.startswith("openadapt_capture."):
            raise ImportError("no capture")
        return real_import(name, *a, **k)

    builtins.__import__ = fake_import
    try:
        with pytest.raises(ImportError, match="openadapt-capture is required"):
            desktop_record._default_recorder_factory("t", "/tmp/x")
    finally:
        builtins.__import__ = real_import
    importlib.import_module  # keep import used (no-op)


# -- CLI wiring --------------------------------------------------------------


def _run_cli(argv: list[str], monkeypatch: Any = None) -> int:
    from openadapt_flow.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_record_windows_invokes_capture(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_record(
        out_dir, *, task_description, params, identifier_region=None, window=None
    ):
        captured["out"] = Path(out_dir)
        captured["task"] = task_description
        captured["params"] = params
        captured["identifier_region"] = identifier_region
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Path(out_dir)

    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture", fake_record
    )
    rc = _run_cli(
        [
            "record",
            "--backend",
            "windows",
            "--out",
            str(tmp_path / "rec"),
            "--param",
            "note=hello",
            "--task",
            "my task",
        ]
    )
    assert rc == 0
    assert captured["out"] == tmp_path / "rec"
    assert captured["task"] == "my task"
    assert captured["params"] == {"note": "hello"}


def test_cli_record_rdp_invokes_capture(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_record(
        out_dir, *, task_description, params, identifier_region=None, window=None
    ):
        captured["params"] = params
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Path(out_dir)

    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture", fake_record
    )
    rc = _run_cli(["record", "--backend", "rdp", "--out", str(tmp_path / "rec")])
    assert rc == 0
    assert captured["params"] == {}


def test_cli_record_desktop_identifier_region(tmp_path: Path, monkeypatch) -> None:
    """`record --backend rdp --identifier X,Y,W,H` threads the marked
    record-identifying region through to the capture orchestration."""
    captured: dict = {}

    def fake_record(
        out_dir, *, task_description, params, identifier_region=None, window=None
    ):
        captured["identifier_region"] = identifier_region
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Path(out_dir)

    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture", fake_record
    )
    rc = _run_cli(
        [
            "record",
            "--backend",
            "rdp",
            "--identifier",
            "10,20,300,40",
            "--out",
            str(tmp_path / "rec"),
        ]
    )
    assert rc == 0
    assert captured["identifier_region"] == (10, 20, 300, 40)


def test_cli_record_desktop_identifier_rejects_field_name(tmp_path: Path) -> None:
    """A pixel capture has no field identity: field-name syntax (the web
    form) is refused loudly rather than silently recording unmarked."""
    with pytest.raises(SystemExit, match="X,Y,W,H"):
        _run_cli(
            [
                "record",
                "--backend",
                "windows",
                "--identifier",
                "patient-banner",
                "--out",
                str(tmp_path / "r"),
            ]
        )


def test_orchestration_stamps_identifier_region_into_meta(tmp_path: Path) -> None:
    """The marked region lands additively in the recording's meta.json — the
    key the compiler reads to scope the identifier crop."""
    log: list = []

    def fake_convert(cap_dir, out_dir, *, params=None):
        (Path(out_dir) / "meta.json").write_text(
            json.dumps({"id": "x", "viewport": [800, 600], "params": {}})
        )
        return Path(out_dir)

    out = record_desktop_capture(
        tmp_path / "rec",
        identifier_region=(10, 20, 300, 40),
        recorder_factory=_make(log),
        convert=fake_convert,
        stop=lambda: True,
        announce=False,
    )
    meta = json.loads((Path(out) / "meta.json").read_text())
    assert meta["identifier_region"] == [10, 20, 300, 40]
    assert meta["id"] == "x"  # existing keys preserved (additive stamp)


def test_cli_record_desktop_rejects_secret(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="secret"):
        _run_cli(
            [
                "record",
                "--backend",
                "windows",
                "--secret",
                "pw",
                "--out",
                str(tmp_path / "r"),
            ]
        )


def test_cli_record_desktop_param_must_be_kv(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="k=v"):
        _run_cli(
            [
                "record",
                "--backend",
                "windows",
                "--param",
                "note",  # missing =VALUE
                "--out",
                str(tmp_path / "r"),
            ]
        )


def test_cli_record_web_requires_url(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --url"):
        _run_cli(["record", "--out", str(tmp_path / "r")])


# -- Window-scoped recording (--window) --------------------------------------


def _fake_desktop_record(captured: dict):
    def fake_record(
        out_dir, *, task_description, params, identifier_region=None, window=None
    ):
        captured["window"] = window
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Path(out_dir)

    return fake_record


def test_cli_record_window_threads_owner_and_title(tmp_path: Path, monkeypatch) -> None:
    """`record --window OWNER --window-title T` builds the capture window spec."""
    captured: dict = {}
    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture",
        _fake_desktop_record(captured),
    )
    rc = _run_cli(
        [
            "record",
            "--backend",
            "rdp",
            "--window",
            "Parallels",
            "--window-title",
            "Windows 11",
            "--out",
            str(tmp_path / "rec"),
        ]
    )
    assert rc == 0
    assert captured["window"] == {"owner": "Parallels", "title": "Windows 11"}


def test_cli_record_window_owner_only(tmp_path: Path, monkeypatch) -> None:
    """Owner substring alone is a valid selector (title stays None)."""
    captured: dict = {}
    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture",
        _fake_desktop_record(captured),
    )
    rc = _run_cli(
        [
            "record",
            "--backend",
            "windows",
            "--window",
            "Citrix Workspace",
            "--out",
            str(tmp_path / "rec"),
        ]
    )
    assert rc == 0
    assert captured["window"] == {"owner": "Citrix Workspace", "title": None}


def test_cli_record_no_window_is_full_screen(tmp_path: Path, monkeypatch) -> None:
    """Without --window the desktop capture stays full-screen (window=None)."""
    captured: dict = {}
    monkeypatch.setattr(
        "openadapt_flow.desktop_record.record_desktop_capture",
        _fake_desktop_record(captured),
    )
    rc = _run_cli(["record", "--backend", "windows", "--out", str(tmp_path / "rec")])
    assert rc == 0
    assert captured["window"] is None


def test_cli_record_web_rejects_window(tmp_path: Path) -> None:
    """--window is a desktop/pixel-capture concept; the web recorder refuses it."""
    with pytest.raises(SystemExit, match="apply only to the desktop"):
        _run_cli(
            [
                "record",
                "--url",
                "http://example.test",
                "--window",
                "Parallels",
                "--out",
                str(tmp_path / "r"),
            ]
        )


def test_default_recorder_factory_passes_window(monkeypatch) -> None:
    """The default factory forwards the window spec to openadapt_capture.Recorder."""
    import sys
    import types

    from openadapt_flow import desktop_record

    seen: dict = {}

    class _FakeCaptureRecorder:
        def __init__(self, *, task_description, capture_dir, window=None):
            seen["task"] = task_description
            seen["capture_dir"] = capture_dir
            seen["window"] = window

    fake_module = types.ModuleType("openadapt_capture")
    fake_module.Recorder = _FakeCaptureRecorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openadapt_capture", fake_module)

    spec = {"owner": "Parallels", "title": None}
    desktop_record._default_recorder_factory("t", "/tmp/cap", window=spec)
    assert seen["window"] == spec
    assert seen["capture_dir"] == "/tmp/cap"


def test_record_desktop_window_forwarded_to_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_desktop_capture(window=...)` reaches the default factory closure."""
    from openadapt_flow import desktop_record

    # The platform guard only permits window capture on darwin/win32; force a
    # supported platform so this forwarding test runs on the Linux CI host.
    monkeypatch.setattr(desktop_record.sys, "platform", "darwin")

    seen: dict = {}
    log: list = []

    def spy_default(task, cap_dir, window=None):
        seen["window"] = window
        return _FakeRecorder(task, cap_dir, log)

    # Replace the default factory; record_desktop_capture binds `window` into it
    # via functools.partial when no factory is injected.
    orig = desktop_record._default_recorder_factory
    desktop_record._default_recorder_factory = spy_default  # type: ignore[assignment]
    try:
        record_desktop_capture(
            tmp_path / "rec",
            window={"owner": "Parallels", "title": None},
            convert=lambda cap_dir, out_dir, *, params=None: Path(out_dir),
            stop=lambda: True,
            announce=False,
        )
    finally:
        desktop_record._default_recorder_factory = orig  # type: ignore[assignment]
    assert seen["window"] == {"owner": "Parallels", "title": None}


def test_record_desktop_window_unsupported_platform(
    tmp_path: Path, monkeypatch
) -> None:
    """On a host with no per-window capture primitive, --window fails LOUD.

    A silent full-screen fallback would record coordinates in the wrong pixel
    space; we refuse before any capture starts.
    """
    from openadapt_flow import desktop_record

    monkeypatch.setattr(desktop_record.sys, "platform", "linux")
    with pytest.raises(SystemExit, match="not supported on this host"):
        record_desktop_capture(
            tmp_path / "rec",
            window={"owner": "Parallels", "title": None},
            stop=lambda: True,
            announce=False,
        )


def test_record_desktop_no_window_ok_on_any_platform(
    tmp_path: Path, monkeypatch
) -> None:
    """Full-screen desktop capture (window=None) is unaffected by the guard."""
    from openadapt_flow import desktop_record

    monkeypatch.setattr(desktop_record.sys, "platform", "linux")
    log: list = []
    out = record_desktop_capture(
        tmp_path / "rec",
        recorder_factory=_make(log),
        convert=lambda cap_dir, out_dir, *, params=None: Path(out_dir),
        stop=lambda: True,
        announce=False,
    )
    assert out == tmp_path / "rec"
