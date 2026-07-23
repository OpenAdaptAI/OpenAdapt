"""Live desktop recording for ``record --backend windows|macos|linux|rdp|citrix``.

Capturing the operator's REAL desktop demonstration is NOT reinvented here — it
reuses the two tested pieces that already exist in this repo:

* **openadapt-capture** (``openadapt_capture.Recorder``) — the cross-platform
  GUI capture component: it records the operator's mouse/keyboard input stream
  time-aligned with an action-gated screen video into a *capture session*
  directory. This is the extensively-tested capture stack; we do not touch it.
* **the capture adapter** (:func:`openadapt_flow.adapters.capture.convert_capture`)
  — converts that capture session into the EXACT recording format the compiler
  consumes (``meta.json`` + ``events.jsonl`` + ``frames/``), running
  openadapt-capture's own event-processing pipeline (raw streams -> merged
  clicks / typed text). This adapter is already unit-tested end to end
  (``tests/test_capture_adapter.py``).

This module is the thin, genuinely-missing piece: the LIVE orchestration that
``record --backend windows|macos|linux|rdp|citrix`` needs — start a capture session, let the
operator perform the workflow, stop on Ctrl-C, then convert to a compile-ready
recording:

    record --backend windows … → (openadapt-capture) → convert_capture
        → compile → replay --backend windows

What is REAL vs deferred (see ``docs/desktop/RECORDING.md`` for the full map):

* REAL: the operator's demonstration on the desktop is captured and converted
  into a recording that compiles into a bundle and replays through the desktop
  backends. Recording is substrate-agnostic (pixel frames + coordinates), so a
  recording made here drives the ``windows`` (WAA) or ``rdp`` (pixel-only)
  backend at replay.
* DEFERRED: offline capture carries NO structural (UIA ``AutomationId`` or
  AT-SPI accessible ID) locator
  — replay uses the visual ladder (template/ocr/geometry). The deterministic
  structural top rung is armed only by the LIVE-over-``WindowsBackend`` path
  (:func:`openadapt_flow.adapters.desktop_recorder.record_desktop_demo`), which
  needs a scripted driver, not a human-in-the-wild demonstration. Re-arming a
  converted capture against a live UIA tree is the tracked follow-up.

openadapt-capture is the optional ``capture`` extra (``pip install
'openadapt-flow[capture]'``); it is imported lazily so the flow core never
pulls it onto the replay hot path.
"""

from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, ContextManager, Optional, Protocol

# Platforms where openadapt-capture's window-scoped capture is implemented
# (openadapt_capture.window_capture.resolve_window / capture_window). Kept in
# lock-step with that module: recording ONE window in its own pixel space needs
# a per-window capture primitive (macOS CGWindowListCreateImage / Windows
# Win32 + region grab). Elsewhere we refuse UP FRONT rather than start a
# full-screen capture that silently ignores the requested --window scope.
WINDOW_CAPTURE_PLATFORMS = ("darwin", "win32")


class _CaptureRecorder(Protocol):
    """The slice of ``openadapt_capture.Recorder`` this orchestration uses.

    A context manager that records on ``__enter__`` and stops (joining its
    threads) on ``__exit__``; ``wait_for_ready`` blocks until capture is live.
    Declared here (not imported) so this module type-checks without the
    optional ``capture`` extra and so tests can inject a fake.
    """

    def __enter__(self) -> "_CaptureRecorder": ...

    def __exit__(self, *exc: Any) -> None: ...

    def wait_for_ready(self, timeout: float = ...) -> bool: ...


RecorderFactory = Callable[[str, str], ContextManager[_CaptureRecorder]]
ConvertFn = Callable[..., Path]


def _default_recorder_factory(
    task_description: str,
    capture_dir: str,
    window: Optional[dict[str, Optional[str]]] = None,
) -> ContextManager[_CaptureRecorder]:
    """Build a live ``openadapt_capture.Recorder`` (lazy import of the extra).

    ``window`` is the openadapt-capture window-scoping spec
    (``{"owner": <app-substring>, "title": <title-substring>}`` or ``None``).
    When set, capture records ONE window in that window's own pixel space
    (``openadapt_capture.window_capture``) instead of the full screen; the
    resolved window's identity (owner/title/pid/window id) is persisted on the
    capture session and surfaced by the capture adapter into ``meta.json``.
    """
    try:
        from openadapt_capture import Recorder as CaptureRecorder
    except ImportError as exc:  # pragma: no cover - exercised via install state
        raise ImportError(
            "openadapt-capture is required to record a desktop workflow but is "
            "not installed. Install the optional extra:\n\n"
            "    pip install 'openadapt-flow[capture]'\n"
        ) from exc
    return CaptureRecorder(
        task_description=task_description, capture_dir=capture_dir, window=window
    )


def _wait_for_stop(stop: Optional[Callable[[], bool]]) -> None:
    """Block until the operator interrupts (Ctrl-C) or ``stop()`` returns True.

    ``stop`` is a test/programmatic hook; in the interactive CLI it is None and
    the loop runs until KeyboardInterrupt.
    """
    try:
        while True:
            if stop is not None and stop():
                return
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[record] stopping…")


def record_desktop_capture(
    out_dir: Path | str,
    *,
    task_description: str = "openadapt-flow desktop recording",
    params: Optional[dict[str, str]] = None,
    identifier_region: Optional[tuple[int, int, int, int]] = None,
    window: Optional[dict[str, Optional[str]]] = None,
    backend_kind: Optional[str] = None,
    replay_window: Optional[str] = None,
    replay_window_title: Optional[str] = None,
    readiness_text: Optional[str] = None,
    capture_dir: Optional[Path | str] = None,
    ready_timeout_s: float = 60.0,
    recorder_factory: Optional[RecorderFactory] = None,
    convert: Optional[ConvertFn] = None,
    stop: Optional[Callable[[], bool]] = None,
    announce: bool = True,
) -> Path:
    """Record a live desktop demonstration and convert it to a recording.

    Runs an openadapt-capture session while the operator performs the workflow,
    then converts it (via :func:`convert_capture`) into the compile-ready
    recording format. Returns the recording directory.

    Args:
        out_dir: Output recording directory (compile input).
        task_description: Stored on the capture session / recording metadata.
        params: ``{param_name: demonstrated_value}`` — a typed value equal to a
            demonstrated value is marked as that parameter (overridable at
            replay). Desktop has no field identity, so parameters are keyed by
            their demonstrated VALUE (mirrors ``convert_capture``).
        identifier_region: Operator-marked RECORD-IDENTIFYING region
            ``(x, y, w, h)`` in the recording's pixel space (the patient
            banner / MRN cell — ``record --identifier X,Y,W,H``). Stamped
            additively into the recording's ``meta.json`` so the compiler
            crops those pixels (``anchor.identifier_crop``) and the pixel
            identity tier arms on remote-display replays. A pixel capture has
            no field identity, so the region is marked once for the
            recording (the identifying banner is static app chrome).
        window: Window-scoping spec
            ``{"owner": <app-substring>, "title": <title-substring>}`` (either
            value may be ``None``). When set, capture records ONE window in
            that window's OWN pixel space (``record --window``) instead of the
            full screen — closing the coordinate-space gap with the pixel
            (``rdp``) replay surface. The window's identity (target + resolved
            owner/title/pid/window id) rides on the capture session and is
            surfaced into ``meta.json`` by the capture adapter. Refused up front
            on hosts where capture has no per-window primitive
            (see :data:`WINDOW_CAPTURE_PLATFORMS`).
        backend_kind: Optional replay substrate identity (``rdp`` or
            ``citrix``) to seal into the compiled bundle's local execution
            hints. Other backends do not use the ``rdp_*`` target contract.
        replay_window: Optional exact owner/process selector for replay.
        replay_window_title: Optional exact title selector for replay.
        readiness_text: Optional current-frame marker required by governed
            Citrix execution.
        capture_dir: Where the raw capture session is written (default: a
            ``.capture`` subdir of ``out_dir``). Kept so the raw session is
            inspectable / re-convertible.
        ready_timeout_s: Seconds to wait for capture to become live.
        recorder_factory: Builds the capture recorder (default: the real
            ``openadapt_capture.Recorder``). Injected in tests.
        convert: The capture->recording converter (default:
            ``adapters.capture.convert_capture``). Injected in tests.
        stop: Optional predicate; when it returns True recording stops (default:
            wait for Ctrl-C). A test/programmatic hook.
        announce: Print operator instructions (suppressed in tests).

    Returns:
        The recording directory (compile-ready).
    """
    if window is not None and sys.platform not in WINDOW_CAPTURE_PLATFORMS:
        # Fail LOUD before starting: a full-screen fallback would silently
        # ignore the requested --window scope and record coordinates in the
        # wrong pixel space (the exact corruption window-scoping prevents).
        raise SystemExit(
            "window-scoped recording (--window) is not supported on this host "
            f"(platform {sys.platform!r}); openadapt-capture implements "
            "per-window capture only on "
            f"{' and '.join(WINDOW_CAPTURE_PLATFORMS)}. Record on a supported "
            "host, or omit --window to capture the full screen."
        )
    if backend_kind is not None and backend_kind not in ("rdp", "citrix"):
        raise ValueError(
            "backend_kind execution hints are supported only for rdp or citrix"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_dir = Path(capture_dir) if capture_dir is not None else out_dir / ".capture"
    cap_dir.mkdir(parents=True, exist_ok=True)

    # Injected factories keep the tested 2-arg ``(task, capture_dir)`` shape;
    # the DEFAULT factory carries the window spec through to the capture
    # Recorder (functools.partial keeps the call site's arity unchanged).
    factory = recorder_factory or functools.partial(
        _default_recorder_factory, window=window
    )
    if convert is None:
        from openadapt_flow.adapters.capture import convert_capture

        convert = convert_capture

    if announce:
        scope_line = ""
        if window is not None:
            scope_line = (
                "  Window-scoped capture is active (recording that window's "
                "own pixels; target selectors are not printed).\n"
            )
        print(
            f"Recording desktop workflow (task: {task_description!r}).\n"
            f"{scope_line}"
            "  Perform your workflow on the target desktop now.\n"
            "  Press Ctrl-C here to finish."
        )

    with factory(task_description, str(cap_dir)) as recorder:
        wait_ready = getattr(recorder, "wait_for_ready", None)
        if callable(wait_ready):
            wait_ready(timeout=ready_timeout_s)
        _wait_for_stop(stop)
    # The recorder context has exited: capture threads joined, the session is
    # fully written to disk. Convert it into the compile-ready recording.
    recording = convert(cap_dir, out_dir, params=params or {})
    if backend_kind is None and identifier_region is None:
        return recording
    meta_path = Path(recording) / "meta.json"
    meta = json.loads(meta_path.read_text())
    changed = False
    if backend_kind is not None:
        # Capture accepts a substring selector, while replay requires one exact
        # owner/title. Prefer the exact resolved window identity unless the
        # operator supplied an explicit replay selector.
        window_capture = meta.get("window_capture")
        resolved_owner = None
        resolved_title = None
        if isinstance(window_capture, dict):
            resolved_owner = window_capture.get("resolved_owner")
            resolved_title = window_capture.get("resolved_title")

        if backend_kind == "citrix":
            from openadapt_flow.backends.citrix_workspace import (
                default_citrix_owner,
            )

            default_owner: Optional[str] = default_citrix_owner()
        else:
            default_owner = None

        hints: dict[str, str] = {"backend": backend_kind}
        owner = replay_window or resolved_owner or default_owner
        title = replay_window_title or resolved_title
        if owner and str(owner).strip():
            hints["rdp_window"] = str(owner).strip()
        if title and str(title).strip():
            hints["rdp_window_title"] = str(title).strip()
        if readiness_text and readiness_text.strip():
            hints["rdp_readiness_text"] = readiness_text.strip()
        meta["backend_hints"] = hints
        changed = True
    if identifier_region is not None:
        # Additive meta.json stamp (the compiler reads `identifier_region`).
        # Written post-convert so the converter contract stays unchanged.
        meta["identifier_region"] = [int(v) for v in identifier_region]
        changed = True
    if changed:
        meta_path.write_text(json.dumps(meta, indent=2))
    return recording
