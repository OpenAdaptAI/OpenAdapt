"""Record one workflow that crosses windows: ``record --surface``.

A person's task often moves between a web app, a native app and a remote
(RDP or Citrix) window. ``record --surface NAME=KIND:WINDOW`` (repeatable)
records it in one session while the person switches windows themselves:

- the browser surface (``--url``) is recorded by the interactive web
  recorder, which follows the app's own popups and tabs;
- every other declared surface is recorded by one full-screen
  openadapt-capture session. Capture keeps the active-window timeline, and
  each action belongs to the declared surface whose window was active when
  it happened. ``WINDOW`` is a case-sensitive part of that window's title
  (on macOS the title starts with the app name). Repeat a name to give one
  surface more windows, for example the app's own dialog;
- the two streams are merged in time order, and each change of surface is
  written as an explicit ``switch_surface`` event, so the compiled bundle has
  a SWITCH_SURFACE step with its own identity check at every boundary.

Halt, never guess. The recording is refused, with the step and time named,
when an action went to a window nobody declared, when a window matches two
surfaces, when the person switched surfaces too fast to order the actions
safely, or when a window-pixel action falls outside its window. Nothing is
dropped silently: a desktop action is set aside only as the browser's own
copy of an action the web recorder also saw.

Pixel spaces follow the replay backend of each kind: a Windows (UI
Automation agent) surface keeps full-desktop pixels; macOS, Linux, RDP and
Citrix surfaces replay in their window's own pixels, so their frames are
cropped to the window and their coordinates moved into it.

See docs/MULTI_SURFACE.md ("Recording across windows").
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

#: Surface kinds a declared window can be. The browser is ``--url``.
DESKTOP_KINDS = ("windows", "macos", "linux", "rdp", "citrix")

#: Kinds whose replay backend works in desktop pixels; the others replay in
#: the target window's own pixels.
DESKTOP_PIXEL_KINDS = frozenset({"windows"})

#: Two actions on different surfaces closer than this cannot be ordered
#: safely across the two recorders.
MIN_SWITCH_GAP_S = 0.25

#: How close an OS action must be to a browser event to count as the
#: browser's own copy of it.
WEB_DUPLICATE_WINDOW_S = 1.5

#: The poll lag of capture's window timeline: a click that brings a window
#: forward shows in the timeline a moment after it lands.
WINDOW_POLL_LAG_S = 0.3

PRIMARY_WEB_ID = "main"

_POINTER_KINDS = frozenset({"click", "double_click", "right_click", "drag"})


class MultiSurfaceRecordError(ValueError):
    """The recording cannot be split into declared surfaces without guessing."""


@dataclass
class RecordSurface:
    """One declared surface: an id, a kind and the windows that are it."""

    id: str
    kind: str
    matchers: list[str] = field(default_factory=list)
    window_ids: set[str] = field(default_factory=set)

    def matches(self, window: "WindowSample") -> bool:
        if window.window_id and window.window_id in self.window_ids:
            return True
        return any(m in window.title for m in self.matchers)


@dataclass(frozen=True)
class WindowSample:
    """The active window from capture's timeline, from ``t`` on."""

    t: float
    title: str
    left: int
    top: int
    width: int
    height: int
    window_id: str

    def contains(self, x: float, y: float) -> bool:
        return (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )


def parse_record_surfaces(
    flags: Sequence[str], *, web_primary: bool
) -> list[RecordSurface]:
    """Parse repeated ``--surface NAME=KIND:WINDOW`` flags for ``record``.

    The kind is required (there is no bundle to take it from yet) and must
    be a desktop or remote kind; the browser is ``--url``. Repeating a name
    with the same kind adds a window to that surface.
    """

    from openadapt_flow.ir import SURFACE_ID_PATTERN
    from openadapt_flow.surface_bindings import parse_surface_flag

    surfaces: dict[str, RecordSurface] = {}
    for text in flags:
        name, kind, target = parse_surface_flag(text)
        if kind is None:
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: name the kind, NAME=KIND:WINDOW "
                f"(KIND is one of {', '.join(DESKTOP_KINDS)})"
            )
        if kind == "web":
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: the browser is the --url surface; its "
                "popups and tabs are followed without a --surface"
            )
        if not re.fullmatch(SURFACE_ID_PATTERN, name):
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: a surface name is lower case letters, "
                "digits and _ (starting with a letter, at most 32)"
            )
        if web_primary and name == PRIMARY_WEB_ID:
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: {PRIMARY_WEB_ID!r} is the browser surface"
            )
        if not target:
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: give part of the window's title, "
                f"{name}={kind}:WINDOW, so only that window is recorded as it"
            )
        surface = surfaces.get(name)
        if surface is None:
            surface = surfaces[name] = RecordSurface(name, kind)
        elif surface.kind != kind:
            raise MultiSurfaceRecordError(
                f"--surface {text!r}: {name!r} was already declared as {surface.kind}"
            )
        if target not in surface.matchers:
            surface.matchers.append(target)
    if not surfaces:
        raise MultiSurfaceRecordError("record --surface: no surface declared")
    return list(surfaces.values())


def window_timeline(rows: Sequence[Any]) -> list[WindowSample]:
    """Capture's window rows as samples, oldest first."""

    samples = [
        WindowSample(
            t=float(row.timestamp),
            title=str(row.title or ""),
            left=int(row.left or 0),
            top=int(row.top or 0),
            width=int(row.width or 0),
            height=int(row.height or 0),
            window_id=str(row.window_id or ""),
        )
        for row in rows
    ]
    samples.sort(key=lambda s: s.t)
    return samples


def _window_at(timeline: Sequence[WindowSample], t: float) -> Optional[WindowSample]:
    current = None
    for sample in timeline:
        if sample.t > t:
            break
        current = sample
    return current


def _surface_of(
    window: WindowSample, surfaces: Sequence[RecordSurface]
) -> Optional[RecordSurface]:
    hits = [s for s in surfaces if s.matches(window)]
    if len(hits) > 1:
        raise MultiSurfaceRecordError(
            "one window matches more than one declared surface ("
            + ", ".join(s.id for s in hits)
            + "); make each --surface WINDOW name only its own window"
        )
    return hits[0] if hits else None


@dataclass
class _Placed:
    """An action placed on a surface, with its time and source."""

    t: float
    surface: str
    source: str  # "web" or "desktop"
    index: int  # event index in its source recording
    event: dict[str, Any]
    window: Optional[WindowSample] = None


def _from_flow_browser(
    event: dict[str, Any],
    t: float,
    web_events: Sequence[tuple[float, str]],
    flow_pids: frozenset[int],
) -> bool:
    """True only when this OS action is the web recorder's own copy.

    Needs positive evidence: the action's process is one of the browser
    processes Flow launched for ``--url`` (``flow_pids``), and the web
    recorder saw an action of the same family that close in time. An
    action with no readable process is never treated as the browser's.
    """

    observed = event.get("observed_process")
    pid = observed.get("pid") if isinstance(observed, dict) else None
    if not isinstance(pid, int) or pid not in flow_pids:
        return False
    family = _family(str(event.get("kind")))
    return any(
        abs(wt - t) <= WEB_DUPLICATE_WINDOW_S and _family(wk) == family
        for wt, wk in web_events
    )


def _family(kind: str) -> str:
    return "pointer" if kind in _POINTER_KINDS or kind == "scroll" else "keys"


def attribute_desktop_events(
    events: Sequence[dict[str, Any]],
    *,
    started_at: float,
    timeline: Sequence[WindowSample],
    surfaces: Sequence[RecordSurface],
    origin: tuple[int, int],
    web_events: Sequence[tuple[float, str]] = (),
    flow_pids: frozenset[int] = frozenset(),
) -> list["_Placed"]:
    """Place each converted desktop event on the declared surface it went to.

    ``events`` are converted capture events (``t`` relative to
    ``started_at``, coordinates in desktop-frame pixels whose (0, 0) is the
    desktop ``origin``). ``web_events`` are ``(time, kind)`` of the browser
    recorder's events: an OS action in an undeclared window is the browser's
    own copy only when a browser event of the same family is that close.
    """

    placed: list[_Placed] = []
    for event in events:
        t = started_at + float(event.get("t", 0.0))
        kind = str(event.get("kind"))
        point = None
        if "x" in event and "y" in event:
            point = (float(event["x"]) + origin[0], float(event["y"]) + origin[1])
        candidates = [_window_at(timeline, t)]
        if point is not None:
            # A click on a window behind brings it forward; the timeline
            # shows that a poll later.
            later = _window_at(timeline, t + WINDOW_POLL_LAG_S)
            if later is not None and later is not candidates[0]:
                candidates.append(later)
            candidates = [w for w in candidates if w is not None and w.contains(*point)]
        present = [w for w in candidates if w is not None]
        matched: list[tuple[RecordSurface, WindowSample]] = []
        for window in present:
            surface = _surface_of(window, surfaces)
            if surface is not None:
                matched.append((surface, window))
        where = f"step {event.get('i')} ({kind} at +{float(event.get('t', 0.0)):.1f}s)"
        if len({s.id for s, _ in matched}) > 1:
            raise MultiSurfaceRecordError(
                f"{where}: the click lands where two declared windows were "
                "active a moment apart; recording refused rather than guess"
            )
        if matched:
            surface, window = matched[0]
            if window.window_id:
                surface.window_ids.add(window.window_id)
            placed.append(
                _Placed(t, surface.id, "desktop", int(event["i"]), dict(event), window)
            )
            continue
        if _from_flow_browser(event, t, web_events, flow_pids):
            continue  # the browser recorder has this action
        raise MultiSurfaceRecordError(
            f"{where}: it went to a window no --surface declared. Declare "
            "that window (--surface NAME=KIND:WINDOW) and record again"
        )
    return placed


#: Remote-desktop clients, by process name (lower case, without ``.exe``).
#: Their window is a remote session: replayed in its own pixels.
REMOTE_CLIENTS = {
    "rdp": frozenset(
        {
            "mstsc",
            "msrdc",
            "microsoft remote desktop",
            "windows app",
            "xfreerdp",
            "xfreerdp3",
            "wlfreerdp",
            "sdl-freerdp",
            "remmina",
            "rdesktop",
        }
    ),
    "citrix": frozenset({"wfica32", "wfica", "cdviewer", "citrix viewer"}),
}

#: Chromium-family browsers, whose page is read through the OS accessibility
#: tree (benchmark/multi_surface_record/results/chrome-accessibility-probe-*).
CHROMIUM_BROWSERS = frozenset(
    {
        "chrome",
        "google chrome",
        "google chrome for testing",
        "chromium",
        "chromium-browser",
        "msedge",
        "microsoft edge",
        "brave",
        "brave browser",
    }
)

#: The one-line fix for a Linux Chrome whose page is not readable.
LINUX_BROWSER_FIX = "openadapt-flow browser-accessibility --enable, then restart Chrome"


def process_key(name: str) -> str:
    key = name.strip().lower()
    return key[:-4] if key.endswith(".exe") else key


def surface_kind(process_name: str, os_kind: str) -> str:
    """A surface's kind from the process that owns its window."""

    key = process_key(process_name)
    for kind, clients in REMOTE_CLIENTS.items():
        if key in clients:
            return kind
    return os_kind


def is_browser(process_name: str) -> bool:
    return process_key(process_name) in CHROMIUM_BROWSERS


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", process_key(name)).strip("_")[:28] or "app"
    if not base[0].isalpha():
        base = "app_" + base
    slug, n = base, 2
    while slug in taken or slug == PRIMARY_WEB_ID:
        slug, n = f"{base}_{n}", n + 1
    taken.add(slug)
    return slug


def own_process_names() -> frozenset[str]:
    """This recorder and the terminal it runs in: never part of the task."""

    try:
        import psutil

        proc = psutil.Process()
        names = {proc.name()}
        for parent in proc.parents():
            names.add(parent.name())
    except Exception:
        return frozenset()
    return frozenset(process_key(n) for n in names if n)


def discover_surfaces(
    events: Sequence[dict[str, Any]],
    *,
    started_at: float,
    timeline: Sequence[WindowSample],
    origin: tuple[int, int],
    os_kind: str,
    web_events: Sequence[tuple[float, str]] = (),
    own_processes: frozenset[str] = frozenset(),
    flow_pids: frozenset[int] = frozenset(),
) -> tuple[list[RecordSurface], list["_Placed"]]:
    """Place each desktop action on a surface found from the process it went to.

    Every application the person acted in becomes one surface, named after
    it; a remote-desktop client's window becomes an ``rdp`` or ``citrix``
    surface. Nothing is guessed: an action whose process cannot be read, a
    readable action in the terminal running the recorder before the end, or
    a click in a Chromium page whose accessibility is off refuses the
    recording with a reason.
    """

    surfaces: dict[str, RecordSurface] = {}
    taken: set[str] = set()
    placed: list[_Placed] = []
    last: Optional[tuple[str, Optional[WindowSample]]] = None
    own_tail: list[int] = []
    for event in events:
        t = started_at + float(event.get("t", 0.0))
        kind = str(event.get("kind"))
        where = f"step {event.get('i')} ({kind} at +{float(event.get('t', 0.0)):.1f}s)"
        observed = event.get("observed_process")
        active = _window_at(timeline, t)
        if isinstance(observed, dict) and observed.get("name"):
            name = str(observed["name"])
        elif (
            last is not None
            and active is not None
            and last[1] is not None
            and active.window_id
            and active.window_id == last[1].window_id
        ):
            # A keystroke with no accessibility reading, in the same window
            # as the action before it: only when both windows carry an id
            # and it is the same id. Two windows with no id are never the
            # same window.
            name = last[0]
        else:
            raise MultiSurfaceRecordError(
                f"{where}: the application it went to could not be read, so "
                "it cannot be placed on a surface"
            )
        key = process_key(name)
        if key in own_processes:
            own_tail.append(len(placed))
            continue
        if own_tail:
            raise MultiSurfaceRecordError(
                f"{where}: an earlier action went to the terminal running "
                "record. Keep the terminal for stopping only"
            )
        if _from_flow_browser(event, t, web_events, flow_pids):
            continue  # the browser recorder has this action
        if (
            is_browser(name)
            and os_kind == "linux"
            and kind in _POINTER_KINDS
            and isinstance(observed, dict)
            and observed.get("element_role") == "frame"
        ):
            raise MultiSurfaceRecordError(
                f"{where}: the click went to a Chrome page whose accessibility "
                f"is off, so its target cannot be read. Run {LINUX_BROWSER_FIX}"
            )
        surface = surfaces.get(key)
        if surface is None:
            surface = surfaces[key] = RecordSurface(
                _slug(name, taken), surface_kind(name, os_kind)
            )
            surface.matchers.append(name)
        window = active
        bounds = observed.get("window_bounds") if isinstance(observed, dict) else None
        if isinstance(bounds, list) and len(bounds) == 4:
            left, top, width, height = (int(v) for v in bounds)
            window = WindowSample(t, "", left, top, width, height, "")
        placed.append(
            _Placed(t, surface.id, "desktop", int(event["i"]), dict(event), window)
        )
        last = (name, active)
    return list(surfaces.values()), placed


def _load_events(recording: Path) -> list[dict[str, Any]]:
    from openadapt_flow import sealed_set

    text = sealed_set.read_text(recording / "events.jsonl")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _load_meta(recording: Path) -> dict[str, Any]:
    from openadapt_flow import sealed_set

    return dict(json.loads(sealed_set.read_text(recording / "meta.json")))


def _read_frame(recording: Path, index: int, which: str) -> Optional[bytes]:
    from openadapt_flow import sealed_set

    path = recording / "frames" / f"{index:04d}_{which}.png"
    if not path.exists():
        return None
    return sealed_set.read_bytes(path)


def _crop(png: bytes, box: tuple[int, int, int, int]) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        left, top, width, height = box
        if (
            left < 0
            or top < 0
            or left + width > image.width
            or top + height > image.height
        ):
            raise MultiSurfaceRecordError(
                "a declared window reaches past the captured desktop; keep "
                "it fully on screen while recording"
            )
        out = io.BytesIO()
        image.crop((left, top, left + width, top + height)).save(out, format="PNG")
        return out.getvalue()


def _png_size(png: bytes) -> list[int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        return [int(image.width), int(image.height)]


def merge_surface_recordings(
    out_dir: Path,
    *,
    surfaces: Sequence[RecordSurface],
    desktop_dir: Optional[Path],
    desktop_started_at: float = 0.0,
    timeline: Sequence[WindowSample] = (),
    origin: tuple[int, int] = (0, 0),
    web_dir: Optional[Path] = None,
    web_clock: Sequence[float] = (),
    os_kind: Optional[str] = None,
    own_processes: frozenset[str] = frozenset(),
    flow_pids: frozenset[int] = frozenset(),
    confirmed: bool = True,
) -> Path:
    """Write one multi-surface recording from a browser and a desktop stream.

    With ``web_dir`` the browser is the primary surface (``main``) and must
    take the first action. Without it the first declared surface is the
    primary and must take the first action. With no declared ``surfaces``
    they are found from the processes the actions went to
    (:func:`discover_surfaces`, needs ``os_kind``), and the surface of the
    first action is the primary.
    """

    out_dir = Path(out_dir)
    web_events = _load_events(web_dir) if web_dir is not None else []
    if web_dir is not None and len(web_clock) != len(web_events):
        raise MultiSurfaceRecordError(
            "the browser recorder's clock does not cover every event; "
            "recording refused rather than order actions by guess"
        )
    desktop_events = _load_events(desktop_dir) if desktop_dir is not None else []
    web_kinds = [(float(t), str(e.get("kind"))) for t, e in zip(web_clock, web_events)]
    automatic = not surfaces
    if automatic:
        if os_kind is None:
            raise ValueError("automatic surfaces need os_kind")
        surfaces, placed = discover_surfaces(
            desktop_events,
            started_at=desktop_started_at,
            timeline=timeline,
            origin=origin,
            os_kind=os_kind,
            web_events=web_kinds,
            own_processes=own_processes,
            flow_pids=flow_pids,
        )
    else:
        placed = attribute_desktop_events(
            desktop_events,
            started_at=desktop_started_at,
            timeline=timeline,
            surfaces=surfaces,
            origin=origin,
            web_events=web_kinds,
            flow_pids=flow_pids,
        )
    kinds = {s.id: s.kind for s in surfaces}
    if web_dir is not None:
        active_page = PRIMARY_WEB_ID
        for i, (t, event) in enumerate(zip(web_clock, web_events)):
            if event.get("kind") == "switch_surface":
                active_page = str(event["surface"])
            else:
                active_page = str(event.get("surface") or active_page)
            placed.append(_Placed(float(t), active_page, "web", i, dict(event)))
    if not placed:
        raise MultiSurfaceRecordError("no action was recorded on any surface")
    # Stable: equal times keep source order (browser page switches stay
    # next to the action that made them).
    placed.sort(key=lambda p: p.t)

    if automatic and web_dir is None:
        # The surface the person started in leads.
        first_surface = placed[0].surface
        surfaces = sorted(surfaces, key=lambda s: s.id != first_surface)
    primary = PRIMARY_WEB_ID if web_dir is not None else surfaces[0].id
    first = placed[0]
    if web_dir is not None and first.source != "web":
        raise MultiSurfaceRecordError(
            "the first action went to a desktop window, but the browser "
            "(--url) is where this recording starts; start in the browser"
        )
    if web_dir is None and first.surface != primary:
        raise MultiSurfaceRecordError(
            f"the first action went to {first.surface!r}, but {primary!r} is "
            "listed first; list the surface you start in first"
        )
    for prev, nxt in zip(placed, placed[1:]):
        if (
            prev.source != nxt.source
            and prev.surface != nxt.surface
            and nxt.t - prev.t < MIN_SWITCH_GAP_S
        ):
            raise MultiSurfaceRecordError(
                f"two actions on {prev.surface!r} and {nxt.surface!r} came "
                f"{nxt.t - prev.t:.2f}s apart, too close to order safely. "
                "Pause briefly after you change windows and record again"
            )

    seen = {p.surface for p in placed if p.source == "desktop"}
    unused = [s.id for s in surfaces if s.id not in seen]
    if unused:
        raise MultiSurfaceRecordError(
            "no action was recorded on declared surface(s) "
            + ", ".join(unused)
            + "; drop them or record the actions on them"
        )
    if out_dir.exists() and any(out_dir.iterdir()):
        raise MultiSurfaceRecordError(f"{out_dir} is not empty")
    frames = out_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    t0 = placed[0].t
    prev_after: Optional[bytes] = None
    prev_surface = primary

    def write(event: dict[str, Any], before: bytes, after: bytes, t: float) -> None:
        i = len(lines)
        (frames / f"{i:04d}_before.png").write_bytes(before)
        (frames / f"{i:04d}_after.png").write_bytes(after)
        line = {"i": i, **event}
        line["viewport_before"] = _png_size(before)
        line["viewport_after"] = _png_size(after)
        line["t"] = round(t - t0, 3)
        lines.append(json.dumps(line))

    for item in placed:
        source_dir = web_dir if item.source == "web" else desktop_dir
        assert source_dir is not None
        before = _read_frame(source_dir, item.index, "before")
        after = _read_frame(source_dir, item.index, "after")
        if before is None or after is None:
            raise MultiSurfaceRecordError(
                f"step {item.index} of the {item.source} stream has no frame "
                "pair; nothing would anchor it"
            )
        event = {
            k: v
            for k, v in item.event.items()
            if k not in ("i", "t", "observed_process")
        }
        if item.source == "desktop":
            kind = kinds[item.surface]
            event["surface"] = item.surface
            if kind in ("rdp", "citrix"):
                # The local accessibility tree of a remote-desktop client
                # describes only its canvas, never a control in the session.
                event.pop("structural", None)
            if kind not in DESKTOP_PIXEL_KINDS:
                assert item.window is not None
                box = (
                    item.window.left - origin[0],
                    item.window.top - origin[1],
                    item.window.width,
                    item.window.height,
                )
                before = _crop(before, box)
                after = _crop(after, box)
                for xk, yk in (("x", "y"), ("end_x", "end_y")):
                    if xk in event:
                        x = int(event[xk]) - box[0]
                        y = int(event[yk]) - box[1]
                        if not (0 <= x < box[2] and 0 <= y < box[3]):
                            raise MultiSurfaceRecordError(
                                f"step {item.index}: the action lands outside "
                                f"the {item.surface!r} window"
                            )
                        event[xk], event[yk] = x, y
        if item.source == "web" and event.get("kind") == "switch_surface":
            # A page switch inside the browser, recorded by the web recorder.
            write(event, before, after, item.t)
            prev_surface = str(event["surface"])
            prev_after = after
            continue
        if item.surface != prev_surface:
            if prev_after is None:
                raise MultiSurfaceRecordError(
                    f"the recording changes to {item.surface!r} before any "
                    "action on the surface it starts in"
                )
            write(
                {"kind": "switch_surface", "surface": item.surface},
                prev_after,
                before,
                item.t,
            )
            prev_surface = item.surface
        if item.source == "web" and item.surface == PRIMARY_WEB_ID:
            event.pop("surface", None)
            event["surface"] = PRIMARY_WEB_ID
        write(event, before, after, item.t)
        prev_after = after

    (out_dir / "events.jsonl").write_text("\n".join(lines) + "\n")

    if web_dir is not None:
        meta = _load_meta(web_dir)
        listed = list(meta.get("surfaces") or [{"id": PRIMARY_WEB_ID, "kind": "web"}])
        meta["surface"] = "web"
    else:
        meta = _load_meta(desktop_dir) if desktop_dir is not None else {}
        for key in ("desktop_capture", "coordinate_space", "window_capture"):
            meta.pop(key, None)
        first_line = json.loads(lines[0])
        meta["viewport"] = first_line["viewport_before"]
        meta["surface"] = kinds[primary]
        listed = [{"id": primary, "kind": kinds[primary]}]
    if web_dir is not None and desktop_dir is not None:
        desktop_params = _load_meta(desktop_dir).get("params") or {}
        params = dict(meta.get("params") or {})
        for name, value in desktop_params.items():
            if name in params and params[name] != value:
                raise MultiSurfaceRecordError(
                    f"parameter {name!r} was recorded with two different values"
                )
            params[name] = value
        meta["params"] = params
    for surface in surfaces:
        if surface.id != primary:
            listed.append({"id": surface.id, "kind": surface.kind})
    meta["surfaces"] = listed
    steps = {s.id: 0 for s in surfaces}
    for item in placed:
        if item.surface in steps:
            steps[item.surface] += 1
    listed_surfaces = []
    for s in surfaces:
        entry: dict[str, Any] = {
            "id": s.id,
            "kind": s.kind,
            "pixel_space": "desktop" if s.kind in DESKTOP_PIXEL_KINDS else "window",
            "steps": steps.get(s.id, 0),
        }
        if automatic and s.matchers:
            # The application's name (never a window title): what replay
            # binds a macOS or Linux surface to.
            entry["app"] = s.matchers[0]
            entry["browser"] = is_browser(s.matchers[0])
        listed_surfaces.append(entry)
    meta["multi_surface_record"] = {
        "automatic": automatic,
        "surfaces": listed_surfaces,
        # Compile refuses an unconfirmed recording (see confirm_review).
        "confirmed": bool(confirmed),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return out_dir


def record_multi_surface(
    out_dir: Path | str,
    *,
    surfaces: Sequence[RecordSurface],
    url: Optional[str] = None,
    params: Optional[dict[str, str]] = None,
    web_params: Sequence[str] = (),
    web_secrets: Sequence[str] = (),
    headless: bool = False,
    task_description: str = "openadapt-flow multi-surface recording",
    stop: Optional[Callable[[], bool]] = None,
    recorder_factory: Optional[Callable[..., Any]] = None,
    web_recorder: Optional[Callable[..., Path]] = None,
    load_capture: Optional[Callable[[Path], Any]] = None,
    announce: bool = True,
) -> Path:
    """Record a browser and declared desktop windows together, then merge.

    One full-screen capture session runs for the whole recording. With
    ``url`` the web recorder runs inside it, against a browser Flow launched.
    """

    import functools

    from openadapt_flow.desktop_record import (
        _default_recorder_factory,
        _require_capture_extra,
        _wait_for_stop,
    )

    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"record: {out_dir} is not empty. Nothing was recorded.")
    if recorder_factory is None:
        _require_capture_extra()
        recorder_factory = functools.partial(
            _default_recorder_factory, window=None, window_timeline=True
        )
    work = Path(tempfile.mkdtemp(prefix=".record-", dir=str(out_dir.parent)))
    try:
        cap_dir = work / "capture"
        cap_dir.mkdir()
        web_dir = work / "web" if url else None
        clock: list[float] = []
        automatic = not surfaces
        if announce and automatic:
            print(
                "Recording. Work as you normally do, in any windows: each\n"
                "  application you use becomes a surface, and each change of\n"
                "  window becomes a checked step. When you stop, you see the\n"
                "  list before anything is kept.\n"
                "  Press Ctrl-C here to finish."
            )
        elif announce:
            names = ", ".join(f"{s.id} ({s.kind})" for s in surfaces)
            start = "the browser that opens" if url else surfaces[0].id
            print(
                f"Recording across windows: {('main (web), ' if url else '')}{names}.\n"
                f"  Start in {start}, then work as you normally do.\n"
                "  Only the windows you declared are recorded; an action in\n"
                "  any other window stops the recording with a reason.\n"
                "  Press Ctrl-C here to finish."
            )
        from openadapt_flow.browser_accessibility import (
            readable_browsers_while_recording,
        )

        flow_pids: set[int] = set()
        with (
            readable_browsers_while_recording(),
            _watch_children(flow_pids),
            recorder_factory(task_description, str(cap_dir)) as recorder,
        ):
            wait_ready = getattr(recorder, "wait_for_ready", None)
            if callable(wait_ready):
                wait_ready(timeout=60.0)
            if url:
                if web_recorder is None:
                    from openadapt_flow.interactive_recorder import record_interactive

                    web_recorder = record_interactive
                assert web_dir is not None
                web_recorder(
                    url,
                    web_dir,
                    secret_fields=tuple(web_secrets),
                    param_fields=tuple(web_params),
                    headless=headless,
                    stop_when=stop,
                    surface="web",
                    event_clock=clock,
                )
            else:
                _wait_for_stop(stop)
        if load_capture is None:
            from openadapt_capture import CaptureSession

            load_capture = CaptureSession.load
        session = load_capture(cap_dir)
        try:
            started_at = float(session.started_at)
            rows = getattr(session, "window_events", None)
            if not callable(rows):
                raise MultiSurfaceRecordError(
                    "this openadapt-capture keeps no active-window timeline, "
                    "so actions cannot be placed on their windows; install "
                    "openadapt-capture 1.3.0 or later"
                )
            timeline = window_timeline(list(rows()))
            if not timeline:
                raise MultiSurfaceRecordError(
                    "capture recorded no active window, so actions cannot be "
                    "placed on their windows"
                )
            desktop = getattr(session, "desktop_capture", None) or {}
            origin_raw = desktop.get("origin") or [0, 0]
            origin = (int(origin_raw[0]), int(origin_raw[1]))
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        desktop_dir: Optional[Path] = None
        if _has_actions(cap_dir, load_capture):
            from openadapt_flow.adapters.capture import convert_capture

            desktop_dir = work / "desktop"
            convert_capture(
                cap_dir,
                desktop_dir,
                params=params or {},
                include_structural=automatic
                or any(s.kind == "windows" for s in surfaces),
                include_process=True,
            )
        return merge_surface_recordings(
            out_dir,
            surfaces=surfaces,
            desktop_dir=desktop_dir,
            desktop_started_at=started_at,
            timeline=timeline,
            origin=origin,
            web_dir=web_dir,
            web_clock=clock,
            os_kind=host_surface_kind() if automatic else None,
            own_processes=own_process_names() if automatic else frozenset(),
            flow_pids=frozenset(flow_pids),
            confirmed=False,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


@contextlib.contextmanager
def _watch_children(pids: set[int], interval_s: float = 0.5) -> Iterator[None]:
    """Collect every process this recorder starts (the browser for ``--url``).

    An OS action counts as the web recorder's own copy only when it went to
    one of these processes.
    """

    stop = threading.Event()

    def loop() -> None:
        try:
            import psutil

            me = psutil.Process()
        except Exception:
            return
        while not stop.is_set():
            try:
                pids.update(child.pid for child in me.children(recursive=True))
            except Exception:
                pass
            stop.wait(interval_s)

    thread = threading.Thread(target=loop, name="record-children", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


REVIEW_FILE = "surfaces.review.txt"


def confirm_review(recording: Path) -> list[str]:
    """Mark a cross-window recording's surfaces as reviewed by a person."""

    recording = Path(recording)
    meta = _load_meta(recording)
    section = meta.get("multi_surface_record")
    if not isinstance(section, dict):
        raise MultiSurfaceRecordError(f"{recording} is not a cross-window recording")
    lines = review_lines(recording)
    section["confirmed"] = True
    (recording / "meta.json").write_text(json.dumps(meta, indent=2))
    (recording / REVIEW_FILE).unlink(missing_ok=True)
    return lines


def write_unconfirmed_review(recording: Path) -> Path:
    """Write the review list beside an unconfirmed recording."""

    path = Path(recording) / REVIEW_FILE
    path.write_text(
        "This recording used the surfaces below. Compile refuses it until a\n"
        "person confirms them with:\n"
        f"  openadapt-flow confirm-surfaces {recording}\n\n"
        + "\n".join(review_lines(recording))
        + "\n"
    )
    return path


def host_surface_kind() -> str:
    """The native surface kind of this computer."""

    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def review_lines(recording: Path) -> list[str]:
    """What a recording used, one line per surface, for the person to check."""

    meta = _load_meta(recording)
    lines = []
    for entry in (meta.get("multi_surface_record") or {}).get("surfaces", []):
        label = entry.get("app") or entry["id"]
        what = "browser page" if entry.get("browser") else entry["kind"]
        lines.append(f"  {label}: {entry.get('steps', 0)} step(s), {what}")
    return lines


def _has_actions(cap_dir: Path, load_capture: Callable[[Path], Any]) -> bool:
    session = load_capture(cap_dir)
    try:
        return any(True for _ in session.actions(include_moves=False))
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()
