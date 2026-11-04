"""``record --surface``: one recording across a browser and declared windows.

The streams here are synthetic (frames are solid PNGs); the real-app run is
benchmark/multi_surface_record/qualify_linux_cross_window.py.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.multi_surface_record import (
    MultiSurfaceRecordError,
    merge_surface_recordings,
    parse_record_surfaces,
    window_timeline,
)

T0 = 1_000.0


def _png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def _stream(root: Path, events: list[dict], size, meta: dict | None = None) -> Path:
    (root / "frames").mkdir(parents=True)
    lines = []
    for i, event in enumerate(events):
        (root / "frames" / f"{i:04d}_before.png").write_bytes(_png(size, (i, 10, 10)))
        (root / "frames" / f"{i:04d}_after.png").write_bytes(_png(size, (i, 20, 20)))
        lines.append(json.dumps({"i": i, **event}))
    (root / "events.jsonl").write_text("\n".join(lines) + "\n")
    (root / "meta.json").write_text(json.dumps(meta or {"viewport": list(size)}))
    return root


def _window(t, title, left, top, width, height, window_id):
    return SimpleNamespace(
        timestamp=t,
        title=title,
        left=left,
        top=top,
        width=width,
        height=height,
        window_id=window_id,
    )


# Desktop 1280x800: the browser window, then the chart app, then the browser.
TIMELINE = window_timeline(
    [
        _window(T0 + 0.0, "Chromium", 0, 0, 1280, 800, "11"),
        _window(T0 + 3.0, "Chart - Ada Lovelace", 100, 50, 600, 400, "22"),
        _window(T0 + 6.0, "Chromium", 0, 0, 1280, 800, "11"),
    ]
)


def _web(tmp_path: Path) -> tuple[Path, list[float]]:
    events = [
        {"kind": "click", "x": 10, "y": 10},
        {"kind": "click", "x": 20, "y": 20},
    ]
    meta = {"viewport": [1280, 720], "app_url": "http://app.test", "params": {}}
    return _stream(tmp_path / "web", events, (1280, 720), meta), [T0 + 1.0, T0 + 7.0]


def _desktop(tmp_path: Path, events: list[dict]) -> Path:
    return _stream(tmp_path / "desktop", events, (1280, 800), {"params": {}})


def _read(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]


def test_parse_requires_a_kind_and_a_window() -> None:
    surfaces = parse_record_surfaces(
        ["chart=linux:Chart", "chart=linux:Save note?", "rdp_1=rdp:Remote"],
        web_primary=True,
    )
    assert [(s.id, s.kind, s.matchers) for s in surfaces] == [
        ("chart", "linux", ["Chart", "Save note?"]),
        ("rdp_1", "rdp", ["Remote"]),
    ]
    for bad in ("chart=Chart", "chart=linux", "portal=web:http://x", "Chart=linux:x"):
        with pytest.raises(MultiSurfaceRecordError):
            parse_record_surfaces([bad], web_primary=True)
    with pytest.raises(MultiSurfaceRecordError):
        parse_record_surfaces(["main=linux:x"], web_primary=True)
    with pytest.raises(MultiSurfaceRecordError):
        parse_record_surfaces(["a=linux:x", "a=rdp:y"], web_primary=False)


def test_browser_then_window_then_browser_gets_two_switches(tmp_path: Path) -> None:
    web, clock = _web(tmp_path)
    desktop = _desktop(
        tmp_path,
        [
            # The browser's own copy of the first web click: set aside.
            {"kind": "click", "x": 30, "y": 30, "t": 1.0},
            {"kind": "click", "x": 300, "y": 250, "t": 4.0},
            {"kind": "type", "text": "note", "t": 4.5},
        ],
    )
    surfaces = parse_record_surfaces(["chart=linux:Chart"], web_primary=True)
    out = merge_surface_recordings(
        tmp_path / "rec",
        surfaces=surfaces,
        desktop_dir=desktop,
        desktop_started_at=T0,
        timeline=TIMELINE,
        web_dir=web,
        web_clock=clock,
    )
    events = _read(out)
    assert [(e["kind"], e.get("surface")) for e in events] == [
        ("click", "main"),
        ("switch_surface", "chart"),
        ("click", "chart"),
        ("type", "chart"),
        ("switch_surface", "main"),
        ("click", "main"),
    ]
    # The chart click moved into the chart window's own pixels, and its
    # frames are that window.
    assert (events[2]["x"], events[2]["y"]) == (200, 200)
    assert events[2]["viewport_before"] == [600, 400]
    # A switch's before frame is the surface it leaves, its after the next.
    assert events[1]["viewport_before"] == [1280, 720]
    assert events[1]["viewport_after"] == [600, 400]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["surfaces"] == [
        {"id": "main", "kind": "web"},
        {"id": "chart", "kind": "linux"},
    ]
    assert "Chart" not in (out / "meta.json").read_text()


def test_an_action_in_an_undeclared_window_refuses(tmp_path: Path) -> None:
    web, clock = _web(tmp_path)
    timeline = sorted(
        list(TIMELINE)
        + window_timeline([_window(T0 + 4.0, "Mail - Inbox", 0, 0, 1280, 800, "33")]),
        key=lambda s: s.t,
    )
    desktop = _desktop(tmp_path, [{"kind": "click", "x": 300, "y": 250, "t": 4.2}])
    with pytest.raises(MultiSurfaceRecordError, match="no --surface declared"):
        merge_surface_recordings(
            tmp_path / "rec",
            surfaces=parse_record_surfaces(["chart=linux:Chart"], web_primary=True),
            desktop_dir=desktop,
            desktop_started_at=T0,
            timeline=timeline,
            web_dir=web,
            web_clock=clock,
        )
    assert not (tmp_path / "rec").exists()


def test_a_window_that_matches_two_surfaces_refuses(tmp_path: Path) -> None:
    web, clock = _web(tmp_path)
    desktop = _desktop(tmp_path, [{"kind": "click", "x": 300, "y": 250, "t": 4.0}])
    with pytest.raises(MultiSurfaceRecordError, match="more than one"):
        merge_surface_recordings(
            tmp_path / "rec",
            surfaces=parse_record_surfaces(
                ["chart=linux:Chart", "other=linux:Ada"], web_primary=True
            ),
            desktop_dir=desktop,
            desktop_started_at=T0,
            timeline=TIMELINE,
            web_dir=web,
            web_clock=clock,
        )


def test_switching_too_fast_to_order_refuses(tmp_path: Path) -> None:
    web, _ = _web(tmp_path)
    desktop = _desktop(tmp_path, [{"kind": "click", "x": 300, "y": 250, "t": 3.1}])
    with pytest.raises(MultiSurfaceRecordError, match="too close"):
        merge_surface_recordings(
            tmp_path / "rec",
            surfaces=parse_record_surfaces(["chart=linux:Chart"], web_primary=True),
            desktop_dir=desktop,
            desktop_started_at=T0,
            timeline=TIMELINE,
            web_dir=web,
            web_clock=[T0 + 3.0, T0 + 7.0],
        )


def test_the_recording_must_start_on_the_lead_surface(tmp_path: Path) -> None:
    web, _ = _web(tmp_path)
    desktop = _desktop(tmp_path, [{"kind": "click", "x": 300, "y": 250, "t": 4.0}])
    with pytest.raises(MultiSurfaceRecordError, match="start in the browser"):
        merge_surface_recordings(
            tmp_path / "rec",
            surfaces=parse_record_surfaces(["chart=linux:Chart"], web_primary=True),
            desktop_dir=desktop,
            desktop_started_at=T0,
            timeline=TIMELINE,
            web_dir=web,
            web_clock=[T0 + 6.5, T0 + 7.0],
        )


def test_a_declared_surface_with_no_action_refuses(tmp_path: Path) -> None:
    web, clock = _web(tmp_path)
    with pytest.raises(MultiSurfaceRecordError, match="no action was recorded"):
        merge_surface_recordings(
            tmp_path / "rec",
            surfaces=parse_record_surfaces(["chart=linux:Chart"], web_primary=True),
            desktop_dir=None,
            web_dir=web,
            web_clock=clock,
        )


def test_two_desktop_apps_and_an_rdp_window_without_a_browser(tmp_path: Path) -> None:
    timeline = window_timeline(
        [
            _window(T0 + 0.0, "Billing", 0, 0, 800, 600, "1"),
            _window(T0 + 2.0, "Remote Desktop - host", 200, 100, 1024, 640, "2"),
            _window(T0 + 4.0, "Notes", 50, 50, 400, 300, "3"),
        ]
    )
    desktop = _desktop(
        tmp_path,
        [
            {"kind": "click", "x": 10, "y": 10, "t": 1.0},
            {"kind": "click", "x": 300, "y": 300, "t": 3.0},
            {"kind": "key", "key": "Enter", "t": 3.5},
            {"kind": "click", "x": 100, "y": 100, "t": 5.0},
        ],
    )
    out = merge_surface_recordings(
        tmp_path / "rec",
        surfaces=parse_record_surfaces(
            ["billing=windows:Billing", "rdp_1=rdp:Remote Desktop", "notes=windows:Notes"],
            web_primary=False,
        ),
        desktop_dir=desktop,
        desktop_started_at=T0,
        timeline=timeline,
    )
    events = _read(out)
    assert [(e["kind"], e.get("surface")) for e in events] == [
        ("click", "billing"),
        ("switch_surface", "rdp_1"),
        ("click", "rdp_1"),
        ("key", "rdp_1"),
        ("switch_surface", "notes"),
        ("click", "notes"),
    ]
    # Windows (UI Automation) keeps desktop pixels; RDP replays in its
    # client window's pixels.
    assert (events[0]["x"], events[0]["y"]) == (10, 10)
    assert (events[2]["x"], events[2]["y"]) == (100, 200)
    assert events[2]["viewport_before"] == [1024, 640]
    assert events[5]["viewport_before"] == [1280, 800]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["surface"] == "windows"
    assert [s["id"] for s in meta["surfaces"]] == ["billing", "rdp_1", "notes"]


def test_record_runs_the_browser_inside_one_capture_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestration: one capture session around the web recorder."""

    from contextlib import contextmanager

    import openadapt_flow.adapters.capture as capture_adapter
    from openadapt_flow.multi_surface_record import record_multi_surface

    calls: list[str] = []

    @contextmanager
    def factory(task: str, cap_dir: str):
        calls.append("capture-start")
        yield SimpleNamespace()
        calls.append("capture-stop")

    def web_recorder(url, out, **kwargs):
        calls.append("web")
        web, clock = _web(tmp_path / "src")
        kwargs["event_clock"].extend(clock)
        import shutil

        shutil.copytree(web, out)
        return out

    def convert(cap_dir, out, **kwargs):
        import shutil

        shutil.copytree(
            _desktop(
                tmp_path / "src",
                [{"kind": "click", "x": 300, "y": 250, "t": 4.0}],
            ),
            out,
        )
        return out

    rows = [
        _window(T0 + 0.0, "Chromium", 0, 0, 1280, 800, "11"),
        _window(T0 + 3.0, "Chart - Ada Lovelace", 100, 50, 600, 400, "22"),
        _window(T0 + 6.0, "Chromium", 0, 0, 1280, 800, "11"),
    ]
    session = SimpleNamespace(
        started_at=T0,
        window_events=lambda: rows,
        desktop_capture={"origin": [0, 0]},
        actions=lambda include_moves=False: iter([object()]),
    )
    monkeypatch.setattr(capture_adapter, "convert_capture", convert)
    out = record_multi_surface(
        tmp_path / "rec",
        surfaces=parse_record_surfaces(["chart=linux:Chart"], web_primary=True),
        url="http://app.test",
        recorder_factory=factory,
        web_recorder=web_recorder,
        load_capture=lambda _: session,
        announce=False,
    )
    assert calls == ["capture-start", "web", "capture-stop"]
    kinds = [(e["kind"], e.get("surface")) for e in _read(out)]
    assert kinds == [
        ("click", "main"),
        ("switch_surface", "chart"),
        ("click", "chart"),
        ("switch_surface", "main"),
        ("click", "main"),
    ]
    # The raw capture session (every window's pixels) is not kept.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rec", "src"]
