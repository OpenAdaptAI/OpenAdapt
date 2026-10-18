"""Local stdio authoring tools: first demo, not governed run.

``openadapt-agent serve --authoring`` registers the same probe names hosted
MCP will use: ``observe``, ``start_record``, ``click``, ``halt``. Local
stdio may also include ``type`` for agent-driven typing through Flow's
Recorder. Hosted remains pause-only. Human type during a pause is
``record_observed`` everywhere; never ``type_text`` on the pause target.

The local agent can drive the recording on every surface Flow has a backend
for. Native Windows apps go through an operator-started Flow Windows agent server
(``--windows-agent-url``), the same synthetic-input path replay uses. RDP and
Citrix go through the local client window (``--remote-window``, ``--citrix``)
and are screen-only: the agent clicks pixels and presses keys. The operator
can turn on the ``screen`` tool (``--authoring-screens``) so the agent sees
the frame it clicks in; it is off unless the operator turns it on.

This module is a transport-independent stdio bridge. It does not open a
network listener. Hosted ChatGPT.com uses ``openadapt-agent authoring
connect`` (outbound mailbox poll in :mod:`openadapt_flow.agent.mailbox`).
Window titles, field values, screenshots, and backend pixels never
cross the MCP wire. ``--authoring`` does not imply ``--allow-run``.

Deps F1/C1/T1 are not required to be merged: Flow's ``AuthoringSession``
is constructed when importable; Capture's projector is used when
importable; Types' observe schema is used when importable. Each is
fail-closed with a local fallback.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.agent.bridge import BridgeError, ToolSpec

_LOG = logging.getLogger(__name__)

__all__ = [
    "AUTHORING_LOCAL_TOOLS",
    "AUTHORING_PROBE_TOOLS",
    "AUTHORING_SCREEN_MODES",
    "AuthoringBridge",
    "AuthoringError",
    "MAX_AUTHORING_WIRE_BYTES",
    "NODE_ID_RE",
    "OBSERVE_SCHEMA_VERSION",
    "ThreadOwnedSession",
    "discover_desktop_authoring_ipc",
    "open_authoring_session",
    "pin_local_backend",
    "project_observe",
]

OBSERVE_SCHEMA_VERSION = "openadapt.authoring.observe/v1"
try:
    from openadapt_types.authoring import (
        AUTHORING_OBSERVE_SCHEMA as _TYPES_OBSERVE,
    )
except ImportError:
    try:
        from openadapt_types.authoring import (
            OBSERVE_SCHEMA_VERSION as _TYPES_OBSERVE,
        )
    except ImportError:
        _TYPES_OBSERVE = None
if isinstance(_TYPES_OBSERVE, str) and _TYPES_OBSERVE:
    OBSERVE_SCHEMA_VERSION = _TYPES_OBSERVE

AUTHORING_PROBE_TOOLS = ("observe", "start_record", "click", "halt")
AUTHORING_LOCAL_TOOLS = (
    "type",
    "pause_for_input",
    "continue_input",
    "stop_record",
    "compile",
    "admit",
    "get_command_result",
    "set_coach",
    "get_coach",
    "bind_status",
)
AUTHORING_TOOLS = AUTHORING_PROBE_TOOLS + AUTHORING_LOCAL_TOOLS
#: Listed only when the operator starts the server with --authoring-screens.
AUTHORING_SCREEN_TOOL = "screen"
#: Who decides whether the agent sees screens: the operator, at server start.
AUTHORING_SCREEN_MODES = ("off", "scrubbed", "unscrubbed")

MAX_AUTHORING_NODES = 200
MAX_AUTHORING_WIRE_BYTES = 32 * 1024
NODE_ID_RE = re.compile(r"^n_[0-9a-f]{8}$")
_PROCESS_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_PROJECTED_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_SIX_DIGITS = re.compile(r"\d{6,}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"\b(?:\+?\d[\d\-\s().]{7,}\d)\b")
_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_EMPTY_OBJECT = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_MUTATING = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
_LOCAL_HALT = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

_FORBIDDEN_WIRE_KEYS = frozenset(
    {
        "value",
        "text",
        "title",
        "window_title",
        "screenshot",
        "ocr",
        "url",
        "urls",
        "backend_pixels",
        "raw",
        "path",
        "file_path",
        "pixels",
    }
)
_BOUNDS_KEYS = frozenset({"x", "y", "w", "h"})
_RESULT_DROP = _FORBIDDEN_WIRE_KEYS | frozenset(
    {"execution_outcome", "success", "events", "frames", "before_png", "after_png"}
)
_T1_BACKENDS = frozenset({"web", "macos", "linux", "windows", "rdp", "citrix"})
_T1_PROVIDERS = frozenset(
    {"playwright_ax", "macos_ax", "windows_uia", "linux_atspi", "none"}
)
#: Surfaces where nothing is pinned to drive (no Windows agent server, no client
#: window) fall back to coach-only. A pinned backend on any surface drives.
_COACH_ONLY_BACKENDS: frozenset[str] = frozenset()
_UNPINNED_COACH_KINDS = frozenset({"windows", "rdp", "citrix"})
_AGENT_DRIVE_BACKENDS = frozenset({"web", "macos", "linux", "windows", "rdp", "citrix"})
#: Remote sessions: pixels and keys, so the observe tree is always empty.
_SCREEN_ONLY_BACKENDS = frozenset({"rdp", "citrix"})
_PIN_HINT = (
    "Start the server with --windows-agent-url for a native Windows app "
    "(the Flow Windows agent server running on the Windows computer), or --remote-window / "
    "--citrix for an RDP or Citrix client window."
)
_ELEMENT_ROLES = frozenset(
    {
        "button",
        "text_input",
        "text_static",
        "label",
        "link",
        "checkbox",
        "radio",
        "combobox",
        "list_item",
        "menu",
        "menu_item",
        "tab",
        "tree_item",
        "image",
        "icon",
        "toolbar",
        "scrollbar",
        "slider",
        "window",
        "dialog",
        "group",
        "table",
        "table_cell",
        "table_row",
        "heading",
        "paragraph",
        "unknown",
    }
)
_OBSERVE_KEYS = (
    "schema_version",
    "backend",
    "provider",
    "mode",
    "agent_drive",
    "coach_only",
    "recording",
    "window",
    "tree",
    "truncated",
    "node_count",
    "reason",
)
_NODE_KEYS = (
    "node_id",
    "role",
    "control_type",
    "class_name",
    "automation_id",
    "name",
    "enabled",
    "focused",
    "bounds",
)

_CLICK_FIELDS = frozenset({"node_id", "x", "y"})
_TYPE_FIELDS = frozenset({"text", "param", "node_id"})
_PAUSE_FIELDS = frozenset({"node_id", "param", "secret"})
_RESULT_FIELDS = frozenset({"command_id"})
_COACH_FIELDS = frozenset({"hint"})
#: admit takes no arguments. A model-supplied token is not a person's
#: consent; the MCP layer asks the person through form elicitation instead.
_ADMIT_FIELDS: frozenset[str] = frozenset()
#: How the person's acceptance was obtained, recorded by Flow when it can.
ADMIT_CONFIRMATION_CHANNEL = "mcp_form_elicitation"

_DESKTOP_IPC_RELATIVE = Path(".openadapt") / "desktop_ipc.json"

_PROBE_HELP = {
    "observe": (
        "Return a PHI-safe authoring observation of the pinned local window "
        "(openadapt.authoring.observe/v1). No screenshots, OCR, field values, "
        "window titles, URLs, or backend pixels. Use node_id values from this "
        "tree for click."
    ),
    "start_record": (
        "Start a Flow Recorder session over the locally pinned backend: a "
        "browser, a macOS or Linux window, a native Windows app, or an RDP "
        "or Citrix client window. RDP and Citrix are screen-only: click x,y "
        "pixels. Refuses only when no window is pinned. Does not compile and "
        "does not enable run tools."
    ),
    "click": (
        "Click one observed node through Flow Recorder. Prefer node_id from "
        "the last observe. Local stdio also accepts integer x,y pixels. "
        "Unknown or stale node_id returns error stale_node."
    ),
    "halt": (
        "Stop the authoring session without compiling. Recording evidence "
        "stays local. This is not a governed run halt."
    ),
}


class AuthoringError(BridgeError):
    """Authoring tool refusal or missing Flow session."""

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


class CoachOnlySession:
    """Stdio stand-in when nothing is pinned for the agent to drive.

    On Windows with no ``--windows-agent-url`` and no client window, the
    person actuates. Observe works; start_record / click / type refuse.
    Never starts the Windows agent server.
    """

    def __init__(self, backend_kind: str = "windows"):
        kind = (backend_kind or "windows").strip().lower()
        if kind in {"remote-display", "remote_display", "citrix"}:
            kind = "rdp" if kind != "citrix" else "citrix"
        if kind not in _COACH_ONLY_BACKENDS:
            kind = "windows"
        self.backend_kind = kind

    def observe(self) -> dict[str, Any]:
        return {
            "backend": self.backend_kind,
            "provider": "none",
            "agent_drive": False,
            "coach_only": True,
            "tree": [],
        }

    def start_record(self) -> dict[str, Any]:
        return {"error": "COACH_ONLY", "coach_only": True, "backend": self.backend_kind}

    def click(self, **kwargs: Any) -> dict[str, Any]:
        return {"error": "COACH_ONLY"}

    def type_text(self, **kwargs: Any) -> dict[str, Any]:
        return {"error": "COACH_ONLY"}

    def halt(self) -> dict[str, Any]:
        return {"status": "halted"}


_OWNER_STOP = object()
_OWNER_STOP_TIMEOUT_S = 30.0


class _BackendOwnerThread:
    """One daemon thread that launches, drives, and closes a browser backend.

    Playwright's sync API binds its objects to the thread that started it and
    leaves its own event loop marked running on that thread. Keeping the
    browser on this thread lets ``anyio.run`` start on the caller's thread,
    and lets MCP worker threads use the session through :meth:`call`.
    """

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="openadapt-authoring-backend", daemon=True
        )
        self._thread.start()

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is _OWNER_STOP:
                return
            future, fn, args, kwargs = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def call(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        future: Future = Future()
        with self._lock:
            if self._stopped:
                raise AuthoringError("the authoring session is closed")
            self._jobs.put((future, fn, args, kwargs))
        return future.result()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._jobs.put(_OWNER_STOP)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=_OWNER_STOP_TIMEOUT_S)


class _OwnedCall:
    """A session method that runs on the backend owner thread."""

    __slots__ = ("_owner", "target")

    def __init__(self, owner: _BackendOwnerThread, target: Callable[..., Any]):
        self._owner = owner
        self.target = target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner.call(self.target, *args, **kwargs)


class ThreadOwnedSession:
    """Authoring session whose every call runs on its backend owner thread.

    Attribute reads, method calls, and attribute writes (such as the mailbox
    guard that swaps out ``type_text`` during Continue) all reach the wrapped
    session on the thread that owns its browser.
    """

    __slots__ = ("_owner", "_session")

    def __init__(self, owner: _BackendOwnerThread, session: object):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> Any:
        if name in ThreadOwnedSession.__slots__:
            raise AttributeError(name)
        value = self._owner.call(getattr, self._session, name)
        if callable(value):
            return _OwnedCall(self._owner, value)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if isinstance(value, _OwnedCall):
            value = value.target
        self._owner.call(setattr, self._session, name, value)

    def __delattr__(self, name: str) -> None:
        self._owner.call(delattr, self._session, name)

    def close(self) -> None:
        """Close the browser on its owner thread, then stop that thread."""

        if self._owner.stopped:
            return
        try:
            closer = self._owner.call(getattr, self._session, "close", None)
            if callable(closer):
                self._owner.call(closer)
        finally:
            self._owner.stop()


def _is_coach_only_session(session: object) -> bool:
    """True for the coach-only stand-in, even behind its owner thread."""

    if isinstance(session, ThreadOwnedSession):
        session = object.__getattribute__(session, "_session")
    return isinstance(session, CoachOnlySession)


def discover_desktop_authoring_ipc(
    *, home: Optional[Path] = None
) -> Optional[dict[str, Any]]:
    """Return Desktop authoring IPC discovery when D2 has advertised it.

    Overlay stays Desktop-owned. Stdio ``--authoring`` does not speak D2.
    Hosted ChatGPT.com uses :mod:`openadapt_flow.agent.mailbox` (outbound poll).
    Until D2 publishes an authoring endpoint in
    ``~/.openadapt/desktop_ipc.json``, return None and pin a local Flow session.
    """

    root = Path.home() if home is None else Path(home)
    path = root / _DESKTOP_IPC_RELATIVE
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    endpoint = document.get("authoring") or document.get("authoring_ipc")
    if not endpoint:
        return None
    return {"path": str(path), "endpoint": endpoint}


def pin_local_backend(
    *,
    url: Optional[str] = None,
    headed: bool = False,
    platform: Optional[str] = None,
    backend: Any = None,
    backend_kind: Optional[str] = None,
    windows_agent_url: Optional[str] = None,
    windows_agent_token: Optional[str] = None,
    remote_window: Optional[str] = None,
    remote_window_title: Optional[str] = None,
    citrix: bool = False,
) -> tuple[Any, str, Any]:
    """Pin the local target. Titles never go to MCP.

    Prefers Desktop loopback when D2 has advertised authoring IPC, but does
    not speak that protocol from this MIT package. Otherwise:

    - ``url`` launches Playwright Chromium with empty cookies (no debug-port
      attach, not the person's already-logged-in Chrome). ``headed`` must be
      true so a person can sign in in that window via ``pause_for_input``.
    - ``windows_agent_url`` → ``WindowsBackend`` over a Windows agent server the
      operator already started (never started here).
    - ``remote_window`` / ``citrix`` → the RDP or Citrix client window,
      screen-only.
    - Windows with none of those → coach-only.
    - macOS / Linux → unique frontmost window via Flow backends when those
      constructors are importable (F1). Non-unique Linux titles are coach-only.
      Omit ``url`` after the person signs in in Chrome if DOM identity is not
      required.
    """

    if backend is not None:
        kind = backend_kind or "web"
        return backend, kind, None
    pinned = _pin_desktop_driver(
        windows_agent_url=windows_agent_url,
        windows_agent_token=windows_agent_token,
        remote_window=remote_window,
        remote_window_title=remote_window_title,
        citrix=citrix,
        url=url,
    )
    if pinned is not None:
        return pinned
    plat = platform or sys.platform
    if url:
        if not headed:
            raise AuthoringError(
                "--url launches Playwright Chromium with empty cookies, not "
                "the Chrome window you already signed into. Pass --headed and "
                "pause_for_input so a person can sign in in that window, then "
                "continue_input. Or pass no --url and pin the unique "
                "frontmost Chrome window after they sign in (macOS; no DOM "
                "identity)."
            )
        return _pin_web(url, headed=headed)
    if plat == "win32" or plat.startswith("win"):
        return None, "windows", None
    native = _try_pin_native(plat)
    if native is not None:
        return native
    raise AuthoringError(
        "stdio --authoring needs a locally pinned window: pass --url --headed "
        "for Playwright Chromium with empty cookies (pause_for_input to sign "
        "in there), omit --url after a unique frontmost Chrome window "
        "(macOS; no DOM identity), or run Desktop so overlay stays "
        "single-owner. Debug-port attach is out of v1."
    )


def _pin_desktop_driver(
    *,
    windows_agent_url: Optional[str],
    windows_agent_token: Optional[str],
    remote_window: Optional[str],
    remote_window_title: Optional[str],
    citrix: bool,
    url: Optional[str],
) -> Optional[tuple[Any, str, Any]]:
    """Build Flow's Windows or remote-window backend through its factory.

    Uses the same ``build_backend`` that ``openadapt-flow replay`` uses, so
    recording and replay share one driver. Never starts the Windows agent server.
    """

    remote = bool(remote_window) or citrix
    if not windows_agent_url and not remote:
        return None
    if windows_agent_url and remote:
        raise AuthoringError(
            "pin one target: --windows-agent-url for a native Windows app, or "
            "--remote-window / --citrix for an RDP or Citrix client window"
        )
    if url:
        raise AuthoringError(
            "--url pins a browser; it can't be combined with "
            "--windows-agent-url, --remote-window, or --citrix"
        )
    try:
        from openadapt_flow.backends.factory import build_backend
        from openadapt_flow.deployment import BackendConfig
    except ImportError as exc:
        raise AuthoringError(
            "driving a Windows app or a remote session needs openadapt-flow "
            "with the windows extra (pip install 'openadapt-flow[windows]')"
        ) from exc
    if windows_agent_url:
        config = BackendConfig(
            kind="windows", agent_url=windows_agent_url, agent_token=windows_agent_token
        )
        kind = "windows"
    else:
        kind = "citrix" if citrix else "rdp"
        config = BackendConfig(
            kind=kind,
            rdp_window=remote_window or None,
            rdp_window_title=remote_window_title or None,
        )
    try:
        backend = build_backend(config)
    except (ValueError, RuntimeError, OSError) as exc:
        raise AuthoringError(f"could not pin the {kind} target: {exc}") from exc
    return backend, kind, getattr(backend, "close", None)


def _pin_web(url: str, *, headed: bool) -> tuple[Any, str, Any]:
    try:
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    except ImportError as exc:
        raise AuthoringError(
            "Playwright web pin requires openadapt-flow with the browser extra "
            "(openadapt-agent[tutorial]); debug-port attach is out of v1"
        ) from exc
    launch = getattr(PlaywrightBackend, "launch", None)
    if not callable(launch):
        raise AuthoringError("PlaywrightBackend.launch is not available")
    backend, close = launch(url, headless=not headed)
    return backend, "web", close


_MAC_DEPS_MISSING = (
    "pinning the frontmost macOS window needs pyobjc (Quartz), which isn't "
    "installed here. Install it with pip install 'openadapt-flow[macos]', "
    "or reinstall openadapt-agent with the tutorial extra, which includes "
    "it. Or pass --url URL --headed to record in a new Chromium window."
)


def _mac_window_owner_title(window_id: int) -> Optional[tuple[str, str]]:
    """Owner and title of one window, read from the window server.

    Flow's ``find_windows`` matches an exact owner, so it can't look a window
    up by id; Quartz can. An empty title usually means macOS hides other
    apps' window titles until Screen Recording is allowed.
    """
    import Quartz

    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, window_id
    )
    for row in rows or []:
        if int(row.get("kCGWindowNumber", 0) or 0) != int(window_id):
            continue
        owner = str(row.get("kCGWindowOwnerName", "") or "")
        title = str(row.get("kCGWindowName", "") or "")
        return owner, title
    return None


def _try_pin_native(plat: str) -> Optional[tuple[Any, str, Any]]:
    """Pin the unique frontmost window.

    Returns None when this Flow has no native backend for the platform.
    Raises :class:`AuthoringError` naming the cause when a backend exists but
    the frontmost window can't be pinned. Window titles never appear in the
    error.
    """

    if plat == "darwin":
        try:
            from openadapt_flow.backends.macos_backend import MacOSBackend
            from openadapt_flow.backends.remote_display import MacWindowClient
        except ImportError:
            return None
        import importlib.util

        try:
            quartz = importlib.util.find_spec("Quartz")
        except (ImportError, ValueError):
            quartz = None
        if quartz is None:
            raise AuthoringError(_MAC_DEPS_MISSING)
        client = MacWindowClient()
        window_id = client.frontmost_window_id()
        if window_id is None:
            raise AuthoringError(
                "No app window is in front. Bring the signed-in app's window to "
                "the front, then start the server again, or pass --url URL --headed."
            )
        front = _mac_window_owner_title(window_id)
        if front is None or not front[0]:
            raise AuthoringError(
                "macOS didn't say which app owns the frontmost window. Bring the "
                "app's window to the front and try again, or pass --url URL --headed."
            )
        app, title = front
        finder = getattr(client, "find_windows", None)
        if not callable(finder):
            return None
        candidates = [
            item
            for item in finder(app, title or None)
            if getattr(item, "window_id", None)
        ]
        if len(candidates) != 1 or candidates[0].window_id != window_id:
            if not title:
                raise AuthoringError(
                    "macOS hid the window titles, so OpenAdapt can't tell this "
                    "app's windows apart. Allow Screen Recording for the program "
                    "that starts this server (System Settings > Privacy & "
                    "Security), or close the app's other windows, or pass --url "
                    "URL --headed."
                )
            if len(candidates) > 1:
                raise AuthoringError(
                    "More than one window of the frontmost app has the same title, "
                    "so OpenAdapt won't guess which one to use. Close the others, "
                    "or pass --url URL --headed."
                )
            raise AuthoringError(
                "macOS didn't list the frontmost window among its app's windows, "
                "so OpenAdapt won't guess which one to use. Bring the app's window "
                "to the front and try again, or pass --url URL --headed."
            )
        return MacOSBackend(client, app=app, window_title=title or None), "macos", None
    if plat.startswith("linux"):
        # Factory requires linux_app AND an exact unique title. Non-unique is
        # coach-only. Without a Desktop pin we cannot guess the app name.
        return None
    return None


def open_authoring_session(
    *,
    out_dir: Optional[Path | str] = None,
    url: Optional[str] = None,
    headed: bool = False,
    backend: Any = None,
    backend_kind: Optional[str] = None,
    platform: Optional[str] = None,
    windows_agent_url: Optional[str] = None,
    windows_agent_token: Optional[str] = None,
    remote_window: Optional[str] = None,
    remote_window_title: Optional[str] = None,
    citrix: bool = False,
    **kwargs: Any,
) -> object:
    """Construct Flow's public authoring session when that module exists.

    ``AuthoringSession`` (F1) requires ``backend``, ``out_dir``, and
    ``backend_kind``. This helper pins locally: a browser, a macOS or Linux
    window, a native Windows app through the Windows agent server, or an RDP or Citrix
    client window. On Windows with nothing pinned it returns a coach-only
    stand-in. Desktop IPC is preferred when advertised;
    this package does not implement the D2 protocol.
    """
    try:
        from openadapt_flow import authoring as flow_authoring
    except ImportError as exc:
        raise AuthoringError(
            "openadapt_flow.authoring is not available in this environment; "
            "stdio --authoring depends on the Flow authoring session that wraps "
            "Recorder (compile returns needs_human_admit; Continue uses "
            "record_observed, never type_text on the pause target)"
        ) from exc
    discover_desktop_authoring_ipc()
    work_dir = Path(out_dir) if out_dir is not None else _default_authoring_dir()
    options = dict(
        work_dir=work_dir,
        url=url,
        headed=headed,
        backend=backend,
        backend_kind=backend_kind,
        platform=platform,
        pin=dict(
            windows_agent_url=windows_agent_url,
            windows_agent_token=windows_agent_token,
            remote_window=remote_window,
            remote_window_title=remote_window_title,
            citrix=citrix,
        ),
        **kwargs,
    )
    if backend is None and backend_kind is None and url:
        # --url pins sync Playwright. Launch, use, and close it on one owner
        # thread: it leaves an event loop marked running on the thread that
        # started it (anyio.run then refuses to start), and its objects fail
        # when MCP worker threads call them.
        owner = _BackendOwnerThread()
        try:
            session = owner.call(_open_pinned_session, flow_authoring, **options)
        except BaseException:
            owner.stop()
            raise
        return ThreadOwnedSession(owner, session)
    return _open_pinned_session(flow_authoring, **options)


def _open_pinned_session(
    flow_authoring: Any,
    *,
    work_dir: Path,
    url: Optional[str],
    headed: bool,
    backend: Any,
    backend_kind: Optional[str],
    platform: Optional[str],
    pin: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> object:
    close = None
    kind = backend_kind
    pinned = backend
    pin = dict(pin or {})
    if pinned is None and kind is None:
        try:
            pinned, kind, close = pin_local_backend(
                url=url, headed=headed, platform=platform, **pin
            )
        except AuthoringError:
            plat = platform or sys.platform
            asked_for_driver = any(pin.get(key) for key in pin)
            if (plat == "win32" or plat.startswith("win")) and not asked_for_driver:
                pinned, kind, close = None, "windows", None
            else:
                raise
    kind = kind or "web"
    normalized = kind.strip().lower().replace("_", "-")
    canonical = (
        "citrix"
        if normalized == "citrix"
        else "rdp"
        if normalized in {"rdp", "remote-display"}
        else "windows"
        if normalized in {"windows", "win", "win-agent"}
        else kind
    )
    if pinned is None and canonical in _UNPINNED_COACH_KINDS:
        # Nothing to drive: the person actuates until a target is pinned.
        session = CoachOnlySession(canonical)
        if close is not None:
            session.close = close  # type: ignore[attr-defined]
        return session
    if canonical in _SCREEN_ONLY_BACKENDS and pin.get("remote_window"):
        hints = {"rdp_window": pin["remote_window"]}
        if pin.get("remote_window_title"):
            hints["rdp_window_title"] = pin["remote_window_title"]
        kwargs.setdefault("backend_hints", hints)
    kind = canonical
    opener = getattr(flow_authoring, "open_session", None)
    session_cls = getattr(flow_authoring, "AuthoringSession", None)
    target = opener if callable(opener) else session_cls
    if "backend_hints" in kwargs and not (
        callable(target) and _accepts_keyword(target, "backend_hints")
    ):
        # An older Flow records the session without replay window hints.
        kwargs.pop("backend_hints")
    try:
        if callable(opener):
            session = opener(
                backend=pinned,
                out_dir=work_dir,
                backend_kind=kind,
                app_url=url,
                **kwargs,
            )
        elif callable(session_cls):
            session = session_cls(
                pinned,
                work_dir,
                backend_kind=kind,
                app_url=url,
                **kwargs,
            )
        else:
            raise AuthoringError(
                "openadapt_flow.authoring is importable but exposes neither "
                "open_session nor AuthoringSession"
            )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "COACH_ONLY" or type(exc).__name__ == "CoachOnlyError":
            session = CoachOnlySession(kind)
        else:
            if close is not None:
                # Do not leave the pinned browser running behind a refusal.
                with contextlib.suppress(Exception):
                    close()
            if isinstance(exc, AuthoringError):
                raise
            raise AuthoringError(
                str(exc), code=code if isinstance(code, str) else None
            ) from exc
    if close is not None:
        session.close = close
    return session


def _safe_label(value: Any, *, process_name: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 80:
        return None
    if process_name:
        if not _PROCESS_NAME.fullmatch(collapsed):
            return None
        return collapsed
    if not _PROJECTED_LABEL.fullmatch(collapsed):
        return None
    if "://" in collapsed or "@" in collapsed or _SIX_DIGITS.search(collapsed):
        return None
    if _EMAIL.search(collapsed) or _SSN.search(collapsed) or _PHONE.search(collapsed):
        return None
    return collapsed


def _bounds(value: Any) -> Optional[dict[str, float]]:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        if number < 0 or number > 1:
            return None
        out[key] = number
    if out["x"] + out["w"] > 1 + 1e-9 or out["y"] + out["h"] > 1 + 1e-9:
        return None
    return out


def _project_window(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    process_name = _safe_label(value.get("process_name"), process_name=True)
    bounds = _bounds(value.get("bounds"))
    if not process_name or bounds is None:
        return None
    return {"process_name": process_name, "role": "window", "bounds": bounds}


def _project_node(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    node_id = value.get("node_id")
    if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
        return None
    role = value.get("role")
    if role not in _ELEMENT_ROLES:
        role = "unknown"
    if not isinstance(value.get("enabled"), bool) or not isinstance(
        value.get("focused"), bool
    ):
        return None
    bounds = _bounds(value.get("bounds"))
    if bounds is None:
        return None
    node: dict[str, Any] = {
        "node_id": node_id,
        "role": role,
        "enabled": value["enabled"],
        "focused": value["focused"],
        "bounds": bounds,
    }
    for key in ("control_type", "class_name", "automation_id", "name"):
        label = _safe_label(value.get(key))
        if label:
            node[key] = label[:64] if key == "class_name" else label
    return {key: node[key] for key in _NODE_KEYS if key in node}


def _empty_observe(*, backend: str, provider: str, coach_only: bool) -> dict[str, Any]:
    return {
        "schema_version": OBSERVE_SCHEMA_VERSION,
        "backend": backend,
        "provider": provider,
        "mode": "authoring",
        "agent_drive": False,
        "coach_only": coach_only,
        "recording": False,
        "tree": [],
        "truncated": False,
        "node_count": 0,
        "reason": "empty_projection",
    }


def _trim_observe(payload: dict[str, Any]) -> dict[str, Any]:
    blob = json.dumps(payload, separators=(",", ":"))
    tree = list(payload.get("tree") or [])
    truncated = payload.get("truncated") is True
    while tree and len(blob.encode("utf-8")) > MAX_AUTHORING_WIRE_BYTES:
        tree.pop()
        truncated = True
        payload["tree"] = tree
        payload["node_count"] = len(tree)
        payload["truncated"] = True
        if not tree:
            payload["reason"] = "empty_projection"
        blob = json.dumps(payload, separators=(",", ":"))
    payload["truncated"] = truncated
    payload["node_count"] = len(payload.get("tree") or [])
    return payload


def _validate_observe_types(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from openadapt_types.authoring import AuthoringObserveV1
    except ImportError:
        return payload
    try:
        model = AuthoringObserveV1.model_validate(payload)
        return json.loads(model.model_dump_json(exclude_none=True))
    except Exception:
        if (
            payload.get("backend") in _UNPINNED_COACH_KINDS
            and payload.get("coach_only") is False
        ):
            # openadapt-types before 0.22 rejects any Windows, RDP, or Citrix
            # observation that isn't coach_only. Check everything else
            # against the contract; if only that rule failed, keep the
            # already-projected payload so a pinned driver can be used.
            try:
                AuthoringObserveV1.model_validate(
                    {**payload, "coach_only": True, "agent_drive": False}
                )
            except Exception:
                pass
            else:
                return payload
        backend = (
            payload.get("backend")
            if payload.get("backend") in _T1_BACKENDS
            else "windows"
        )
        provider = (
            payload.get("provider")
            if payload.get("provider") in _T1_PROVIDERS
            else "none"
        )
        coach_only = payload.get("coach_only") is True
        return _empty_observe(backend=backend, provider=provider, coach_only=coach_only)


def project_observe(payload: Any) -> dict[str, Any]:
    """Fail-closed PHI projection for ``openadapt.authoring.observe/v1``.

    extra keys, values, titles, screenshots, and backend pixels never appear
    on the wire. Prefer Capture's projector when importable; always re-shape
    to the T1 allowlist so a raw fallback cannot leak.
    """
    source = payload if isinstance(payload, Mapping) else {}
    backend = source.get("backend")
    if backend not in _T1_BACKENDS:
        # T1 has no "unknown" backend. An unpinned substrate is not agent-drive.
        backend = "windows"
        source = {**source, "coach_only": True, "agent_drive": False, "tree": []}
    coach_only = backend in _COACH_ONLY_BACKENDS or source.get("coach_only") is True
    provider = source.get("provider")
    if provider not in _T1_PROVIDERS:
        provider = "none"
    window = _project_window(source.get("window"))
    agent_drive = (
        (not coach_only)
        and backend in _AGENT_DRIVE_BACKENDS
        and source.get("agent_drive") is not False
        and window is not None
    )
    if coach_only:
        agent_drive = False
    tree_in = source.get("tree")
    if backend in _SCREEN_ONLY_BACKENDS:
        # Remote sessions are pixels and keys; never project a tree for them.
        tree_in = []
    nodes: list[dict[str, Any]] = []
    if isinstance(tree_in, list) and not coach_only:
        for item in tree_in:
            node = _project_node(item)
            if node is not None:
                nodes.append(node)
            if len(nodes) >= MAX_AUTHORING_NODES:
                break
    projected: dict[str, Any] = {
        "schema_version": OBSERVE_SCHEMA_VERSION,
        "backend": backend,
        "provider": provider,
        "mode": "authoring",
        "agent_drive": agent_drive,
        "coach_only": coach_only,
        "recording": source.get("recording") is True,
        "tree": nodes,
        "truncated": source.get("truncated") is True
        or (isinstance(tree_in, list) and len(tree_in) > MAX_AUTHORING_NODES),
        "node_count": len(nodes),
    }
    if window is not None:
        projected["window"] = window
    if not nodes:
        reason = source.get("reason")
        projected["reason"] = (
            reason if reason == "empty_projection" else "empty_projection"
        )
    projected = {key: projected[key] for key in _OBSERVE_KEYS if key in projected}
    return _trim_observe(_validate_observe_types(_trim_observe(projected)))


def _public_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"status": "ok"}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _RESULT_DROP or key in _FORBIDDEN_WIRE_KEYS:
            continue
        if key in {"execution_outcome", "success"}:
            continue
        if isinstance(value, Mapping):
            nested = _public_result(value)
            if nested:
                out[key] = nested
            continue
        if isinstance(value, list):
            continue
        if key in {"path", "file_path"}:
            continue
        out[key] = value
    return out


def _require_object(
    arguments: Optional[dict[str, Any]], allowed: set[str]
) -> dict[str, Any]:
    payload = dict(arguments or {})
    unknown = set(payload) - allowed
    if unknown:
        raise AuthoringError("arguments do not match the declared authoring schema")
    return payload


def _default_authoring_dir() -> Path:
    """Where a recording goes when the caller names no folder (never cwd)."""
    from openadapt_flow.agent.runner import default_runs_dir

    return default_runs_dir() / "authoring"


def _takes_positional(func: Callable[..., Any]) -> bool:
    """Whether ``func`` takes at least one positional argument."""
    import inspect

    if isinstance(func, _OwnedCall):
        func = func.target
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for parameter in parameters
    )


def _accepts_keyword(func: Callable[..., Any], name: str) -> bool:
    """Whether ``func`` takes keyword ``name`` (or any keyword)."""
    import inspect

    if isinstance(func, _OwnedCall):
        func = func.target
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


def _invoke(session: object, method: str, **kwargs: Any) -> Any:
    func = getattr(session, method, None)
    aliases = {
        "type_agent": ("type_text", "type"),
        "pause_for_input": ("pause",),
        "continue_input": ("continue_pause", "record_observed"),
        "stop_record": ("finish", "stop"),
        "start_record": ("start",),
    }
    if not callable(func):
        for alias in aliases.get(method, ()):
            candidate = getattr(session, alias, None)
            if callable(candidate):
                func = candidate
                break
    if not callable(func):
        raise AuthoringError(f"authoring session does not implement {method}")
    try:
        return func(**kwargs) if kwargs else func()
    except TypeError:
        if "node_id" in kwargs:
            retry = dict(kwargs)
            retry.pop("node_id", None)
            try:
                return func(**retry) if retry else func()
            except TypeError:
                pass
        if kwargs:
            raise
        return func()


def _session_code(exc: BaseException) -> Optional[str]:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


class AuthoringBridge:
    """Stdio authoring tool specs and dispatch over a session object."""

    def __init__(
        self,
        session: object,
        *,
        out_dir: Optional[Path | str] = None,
        screens: str = "off",
        scrubber_factory: Optional[Callable[[], Any]] = None,
    ):
        if screens not in AUTHORING_SCREEN_MODES:
            raise AuthoringError(
                f"screens must be one of {', '.join(AUTHORING_SCREEN_MODES)}"
            )
        self.session = session
        self._screens = screens
        self._scrubber_factory = scrubber_factory
        self._out_dir = Path(out_dir) if out_dir is not None else None
        self._last_tool: Optional[str] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._coach_only = _is_coach_only_session(session)
        self._nodes: dict[str, dict[str, Any]] = {}
        self._coach_hint: Optional[str] = None
        self._compiled_workflow_id: Optional[str] = None

    def handles(self, name: str) -> bool:
        if name == AUTHORING_SCREEN_TOOL:
            return self._screens != "off"
        return name in AUTHORING_TOOLS

    def admit_confirmation_message(self) -> str:
        """What the person is asked before admit records their acceptance."""
        draft = (
            f"the compiled draft {self._compiled_workflow_id}"
            if self._compiled_workflow_id
            else "the compiled draft"
        )
        return (
            f"A person must review {draft} on this computer and confirm that "
            "they accept it. This records your local acceptance only. It isn't "
            "a signed receipt and doesn't make the workflow ready to run on "
            "real records."
        )

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="observe",
                description=_PROBE_HELP["observe"],
                input_schema=_EMPTY_OBJECT,
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="start_record",
                description=_PROBE_HELP["start_record"],
                input_schema=_EMPTY_OBJECT,
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="click",
                description=_PROBE_HELP["click"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Opaque node id from the last observe tree.",
                        },
                        "x": {
                            "type": "integer",
                            "description": "Local pixel X. Stdio only; hosted click is node_id.",
                        },
                        "y": {
                            "type": "integer",
                            "description": "Local pixel Y. Stdio only; hosted click is node_id.",
                        },
                    },
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="halt",
                description=_PROBE_HELP["halt"],
                input_schema=_EMPTY_OBJECT,
                annotations=_LOCAL_HALT,
            ),
            ToolSpec(
                name="type",
                description=(
                    "Agent-driven typing through Flow Recorder.type_text. "
                    "Local stdio only; hosted MCP has no type tool. Do not use "
                    "this for secrets or for text a person already typed. Human "
                    "input uses pause_for_input, which persists with "
                    "record_observed and never type_text on the pause target."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Literal text the agent types through the backend.",
                        },
                        "param": {
                            "type": "string",
                            "description": "Optional workflow parameter name for this type.",
                        },
                        "node_id": {
                            "type": "string",
                            "description": "Optional observed node to focus before typing.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="pause_for_input",
                description=(
                    "Pause so a person can type in the application. Capture the "
                    "pause-target at pause start. On Continue, persist with "
                    "Recorder.record_observed. Never call type_text for that "
                    "human input. Secret pauses store no text. The MCP result "
                    "has no value. Local stdio Continue is continue_input "
                    "(overlay Continue when Desktop owns the session)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Pause-target node id captured at pause start.",
                        },
                        "param": {
                            "type": "string",
                            "description": "Parameter name to bind on the observed type.",
                        },
                        "secret": {
                            "type": "boolean",
                            "description": "If true, persist secret=True and no text.",
                        },
                    },
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="continue_input",
                description=(
                    "Stdio stand-in for overlay Continue after pause_for_input. "
                    "Persists with Recorder.record_observed on the pause-target "
                    "node, never type_text. Hosted MCP has no continue_input "
                    "tool; Desktop overlay Continue owns that path."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="stop_record",
                description=(
                    "Finish the Flow Recorder session without compiling. "
                    "Evidence stays local."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations=_LOCAL_HALT,
            ),
            ToolSpec(
                name="compile",
                description=(
                    "Wrap Flow compile_recording and return needs_human_admit. "
                    "Then call admit: the client asks the person to accept "
                    "the draft. An agent click never paints VERIFIED. Refuses "
                    "a session that had a secret pause and no TYPE/param event."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolSpec(
                name="admit",
                description=(
                    "Ask the person to accept the pre-filled draft (schema, "
                    "authority, effect contract, environment, digest). The "
                    "MCP client shows the person a confirmation form, and "
                    "only their answer there accepts the draft: there is no "
                    "argument an agent can send to accept it. Refuses when "
                    "the client can't show a form or the person declines. "
                    "Does not mint a Seal or write an unsigned ledger row "
                    "when the Flow session has no admit."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolSpec(
                name="get_command_result",
                description=(
                    "Return the last in-process authoring result. Stdio executes "
                    "tools synchronously; call this after a probe if the client "
                    "expects the hosted pending/command_id shape."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command_id": {
                            "type": "string",
                            "description": "Optional id; stdio has one in-process result.",
                        }
                    },
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="set_coach",
                description=(
                    "Local coach hint (80 characters; no URL, @, or six-digit "
                    "runs). Desktop overlay owns hosted set_coach. Stdio stores "
                    "the hint in-process when Desktop IPC is not advertised."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "hint": {
                            "type": "string",
                            "description": "PHI-safe coach hint for the local overlay.",
                        }
                    },
                    "required": ["hint"],
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="get_coach",
                description="Return the last local coach hint. No tree.",
                input_schema=_EMPTY_OBJECT,
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="bind_status",
                description=(
                    "Local stdio bind status. This process is already on the "
                    "machine; there is no pack mailbox. No tree."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations=_READ_ONLY,
            ),
        ]
        if self._screens != "off":
            specs.append(
                ToolSpec(
                    name=AUTHORING_SCREEN_TOOL,
                    description=(
                        "Return the pinned window's current frame as a base64 "
                        "PNG, in the pixel space click x,y uses. Use it to drive "
                        "a native Windows app or an RDP or Citrix session, where "
                        "observe has no tree. The operator turned this on at "
                        "server start"
                        + (
                            "; frames are scrubbed of identifiers first, and none "
                            "is returned if scrubbing fails."
                            if self._screens == "scrubbed"
                            else "; frames are not scrubbed."
                        )
                    ),
                    input_schema=_EMPTY_OBJECT,
                    annotations=_READ_ONLY,
                )
            )
        return specs

    def dispatch(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        human_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Run one authoring tool.

        ``human_confirmed`` is set only by the transport after the person
        accepted a host-mediated confirmation (MCP form elicitation). Only
        ``admit`` reads it; nothing in ``arguments`` can stand in for it.
        """
        if name == AUTHORING_SCREEN_TOOL and self._screens != "off":
            _require_object(arguments, set())
            return self._screen()
        if name not in AUTHORING_TOOLS:
            raise AuthoringError("unknown tool name")
        if name == "get_command_result":
            _require_object(arguments, _RESULT_FIELDS)
            return self._last_command_result()
        if name == "admit":
            result = self._admit(arguments, human_confirmed=human_confirmed is True)
            self._last_tool = name
            self._last_result = result
            return result
        handlers = {
            "observe": self._observe,
            "start_record": self._start_record,
            "click": self._click,
            "halt": self._halt,
            "type": self._type_agent,
            "pause_for_input": self._pause_for_input,
            "continue_input": self._continue_input,
            "stop_record": self._stop_record,
            "compile": self._compile,
            "set_coach": self._set_coach,
            "get_coach": self._get_coach,
            "bind_status": self._bind_status,
        }
        result = handlers[name](arguments)
        self._last_tool = name
        self._last_result = result
        return result

    def _screen(self) -> dict[str, Any]:
        """The pinned frame, scrubbed unless the operator chose unscrubbed."""

        import base64
        import io

        self._refuse_coach_only("screen")
        grab = getattr(self.session, "screenshot", None)
        if not callable(grab):
            raise AuthoringError("this authoring session can't share its screen")
        png = grab()
        if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(
            b"\x89PNG"
        ):
            raise AuthoringError("the pinned window returned no frame")
        png = bytes(png)
        scrubbed = False
        if self._screens == "scrubbed":
            from openadapt_flow.agent.repair import default_scrubber, scrub_png

            try:
                png, _evidence = scrub_png(
                    png, self._scrubber_factory or default_scrubber
                )
            except Exception:
                _LOG.exception("authoring screen scrubbing failed; nothing shared")
                raise AuthoringError(
                    "the screen could not be scrubbed, so no screen was shared"
                ) from None
            scrubbed = True
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
        return {
            "scrubbed": scrubbed,
            "width": int(width),
            "height": int(height),
            "screen_png_base64": base64.b64encode(png).decode("ascii"),
        }

    def _last_command_result(self) -> dict[str, Any]:
        if self._last_result is None:
            return {
                "command_id": None,
                "status": "idle",
                "retry_after_ms": 0,
                "result": None,
            }
        return {
            "command_id": self._last_tool,
            "status": "done",
            "retry_after_ms": 0,
            "result": dict(self._last_result),
        }

    def _map_session_error(self, exc: BaseException) -> dict[str, Any]:
        code = _session_code(exc)
        if code == "stale_node":
            return {"status": "error", "error": "stale_node"}
        if code == "missing_secret_type":
            return {"status": "error", "error": "missing_secret_type"}
        if code == "COACH_ONLY" or type(exc).__name__ == "CoachOnlyError":
            self._coach_only = True
            raise AuthoringError(
                f"refused: COACH_ONLY (nothing is pinned for the agent to "
                f"drive, so a person actuates). {_PIN_HINT}",
                code="COACH_ONLY",
            ) from exc
        if isinstance(exc, AuthoringError):
            raise exc
        raise AuthoringError(str(exc), code=code) from exc

    def _call(self, method: str, **kwargs: Any) -> Any:
        try:
            return _invoke(self.session, method, **kwargs)
        except AuthoringError:
            raise
        except Exception as exc:
            mapped = self._map_session_error(exc)
            if mapped.get("error"):
                return mapped
            raise

    def _remember_nodes(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        tree = raw.get("tree")
        if not isinstance(tree, list):
            return
        remember = getattr(self.session, "remember_node", None)
        for item in tree:
            if not isinstance(item, Mapping):
                continue
            node_id = item.get("node_id")
            pixels = item.get("backend_pixels")
            if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
                continue
            if isinstance(pixels, Mapping):
                stored = {
                    key: pixels[key] for key in ("x", "y", "w", "h") if key in pixels
                }
                self._nodes[node_id] = stored
                if callable(remember):
                    remember(node_id, stored)

    def _observe(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw: Any = None
        observe = getattr(self.session, "observe", None)
        if callable(observe):
            try:
                raw = observe()
            except Exception as exc:
                mapped = self._map_session_error(exc)
                if mapped.get("error"):
                    return mapped
                raise
        self._remember_nodes(raw)
        projected = project_observe(
            raw
            if isinstance(raw, Mapping)
            else {
                "backend": getattr(self.session, "backend_kind", None)
                or getattr(self.session, "backend", "web"),
                "coach_only": self._coach_only,
            }
        )
        self._coach_only = projected.get("coach_only") is True
        return projected

    def _refuse_coach_only(self, tool: str) -> None:
        if self._coach_only:
            raise AuthoringError(
                f"{tool} refused: COACH_ONLY (nothing is pinned for the agent "
                f"to drive, so a person actuates). {_PIN_HINT}",
                code="COACH_ONLY",
            )

    def _start_record(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        self._refuse_coach_only("start_record")
        raw = self._call("start_record")
        if isinstance(raw, Mapping) and (
            raw.get("error") == "COACH_ONLY" or raw.get("coach_only") is True
        ):
            self._coach_only = True
            raise AuthoringError(
                f"start_record refused: COACH_ONLY (nothing is pinned for the "
                f"agent to drive, so a person actuates). {_PIN_HINT}",
                code="COACH_ONLY",
            )
        result = _public_result(raw)
        if result.get("status") == "ok" and raw is None:
            return {"status": "recording"}
        if raw is None:
            return {"status": "recording"}
        result.setdefault("status", "recording")
        if result.get("status") == "ok":
            result["status"] = "recording"
        return result

    def _click(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _CLICK_FIELDS)
        self._refuse_coach_only("click")
        node_id = payload.get("node_id")
        x = payload.get("x")
        y = payload.get("y")
        has_node = isinstance(node_id, str) and bool(node_id)
        has_point = x is not None or y is not None
        if has_point and (
            not isinstance(x, int)
            or isinstance(x, bool)
            or not isinstance(y, int)
            or isinstance(y, bool)
        ):
            raise AuthoringError("click x and y must both be integers")
        if not has_node and not has_point:
            raise AuthoringError("click requires node_id or local x and y")
        kwargs: dict[str, Any] = {}
        if has_node:
            kwargs["node_id"] = node_id
            if node_id not in self._nodes and not NODE_ID_RE.fullmatch(str(node_id)):
                return {"status": "error", "error": "stale_node"}
        if has_point:
            kwargs["x"] = x
            kwargs["y"] = y
        raw = self._call("click", **kwargs)
        if isinstance(raw, Mapping) and raw.get("error") in {
            "stale_node",
            "COACH_ONLY",
        }:
            if raw.get("error") == "stale_node" or raw.get("status") == "stale_node":
                return {"status": "error", "error": "stale_node"}
            raise AuthoringError(
                f"click refused: COACH_ONLY (nothing is pinned for the agent "
                f"to drive, so a person actuates). {_PIN_HINT}",
                code="COACH_ONLY",
            )
        result = _public_result(raw)
        if result.get("error") == "stale_node" or result.get("status") == "stale_node":
            return {"status": "error", "error": "stale_node"}
        if result.get("error") == "COACH_ONLY":
            raise AuthoringError(
                f"click refused: COACH_ONLY (nothing is pinned for the agent "
                f"to drive, so a person actuates). {_PIN_HINT}",
                code="COACH_ONLY",
            )
        result.setdefault("status", "ok")
        if has_node:
            result.setdefault("node_id", node_id)
        result.pop("backend_pixels", None)
        return result

    def _halt(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = self._call("halt")
        result = _public_result(raw)
        result.setdefault("status", "halted")
        if result.get("status") == "ok":
            result["status"] = "halted"
        result["compiled"] = False
        return result

    def _type_agent(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _TYPE_FIELDS)
        self._refuse_coach_only("type")
        text = payload.get("text")
        if not isinstance(text, str) or text == "":
            raise AuthoringError("type requires a non-empty text string")
        kwargs: dict[str, Any] = {"text": text}
        param = payload.get("param")
        if param is not None:
            if not isinstance(param, str) or not _PARAM_NAME.fullmatch(param):
                raise AuthoringError("param must be a string")
            kwargs["param"] = param
        node_id = payload.get("node_id")
        if node_id is not None:
            if not isinstance(node_id, str) or not node_id:
                raise AuthoringError("node_id must be a string")
            kwargs["node_id"] = node_id
        raw = self._call("type_agent", **kwargs)
        result = _public_result(raw)
        result.pop("text", None)
        result.setdefault("status", "ok")
        result["recorded"] = True
        if param:
            result["param"] = param
        return result

    def _pause_for_input(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _PAUSE_FIELDS)
        kwargs: dict[str, Any] = {}
        node_id = payload.get("node_id")
        if node_id is not None:
            if not isinstance(node_id, str) or not node_id:
                raise AuthoringError("node_id must be a string")
            kwargs["node_id"] = node_id
        param = payload.get("param")
        if param is not None:
            if not isinstance(param, str) or not _PARAM_NAME.fullmatch(param):
                raise AuthoringError("param must be a string")
            kwargs["param"] = param
        secret = payload.get("secret")
        if secret is not None:
            if not isinstance(secret, bool):
                raise AuthoringError("secret must be a boolean")
            kwargs["secret"] = secret
        raw = self._call("pause_for_input", **kwargs)
        if callable(getattr(self.session, "continue_input", None)) and not (
            isinstance(raw, Mapping) and "recorded" in raw
        ):
            result = {"status": "paused"}
            if param:
                result["param"] = param
            if secret is True:
                result["secret"] = True
            return result
        result = _public_result(raw)
        result.pop("text", None)
        result.pop("value", None)
        result.setdefault("recorded", True)
        if param:
            result["param"] = param
        if secret is True:
            result["secret"] = True
        return result

    def _continue_input(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = self._call("continue_input")
        result = _public_result(raw)
        result.pop("text", None)
        result.pop("value", None)
        if "recorded" not in result:
            result["recorded"] = True
        return result

    def _stop_record(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = self._call("stop_record")
        result = _public_result(raw)
        result.setdefault("status", "stopped")
        if result.get("status") == "ok":
            result["status"] = "stopped"
        result["compiled"] = False
        return result

    def _compile(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        bundle_dir = (self._out_dir or _default_authoring_dir()) / "bundle"
        try:
            compile_fn = getattr(self.session, "compile", None)
            if not callable(compile_fn):
                raise AuthoringError("authoring session does not implement compile")
            try:
                raw = compile_fn(bundle_dir, name="authoring")
            except TypeError:
                raw = compile_fn()
        except Exception as exc:
            mapped = self._map_session_error(exc)
            if mapped.get("error"):
                return mapped
            raise
        if isinstance(raw, Mapping) and raw.get("error") == "missing_secret_type":
            return {"status": "error", "error": "missing_secret_type"}
        result = _public_result(raw)
        if (
            result.get("status") == "error"
            or result.get("error") == "missing_secret_type"
        ):
            return {
                "status": "error",
                "error": result.get("error") or "missing_secret_type",
            }
        workflow_id = result.get("workflow_id")
        public = {
            "status": "needs_human_admit",
            "recording_retained": True,
        }
        if isinstance(workflow_id, str) and workflow_id:
            public["workflow_id"] = workflow_id
            self._compiled_workflow_id = workflow_id
        summary = _safe_label(result.get("summary"))
        if summary:
            public["summary"] = summary
        return public

    def _admit(
        self, arguments: Optional[dict[str, Any]], *, human_confirmed: bool
    ) -> dict[str, Any]:
        if not human_confirmed:
            raise AuthoringError(
                "admit refused: a person must accept the draft through the "
                "MCP client's confirmation form; no draft was accepted",
                code="admit_requires_host_confirmation",
            )
        _require_object(arguments, set(_ADMIT_FIELDS))
        admit_fn = getattr(self.session, "admit", None)
        if not callable(admit_fn):
            raise AuthoringError(
                "authoring session does not implement admit; "
                "refusing rather than minting a Seal, writing an unsigned "
                "ledger row, or claiming Production",
                code="admit_unavailable",
            )
        # The person accepted through the host, so Flow gets its own ok token
        # and, when it records one, how the acceptance was obtained.
        kwargs: dict[str, Any] = {"confirm": "ok"}
        if _accepts_keyword(admit_fn, "confirmation_channel"):
            kwargs["confirmation_channel"] = ADMIT_CONFIRMATION_CHANNEL
        # Pick the call shape from the signature. Retrying on TypeError could
        # run Flow's admit twice when the TypeError came from inside it.
        if _accepts_keyword(admit_fn, "confirm"):
            call = lambda: admit_fn(**kwargs)  # noqa: E731
        elif _takes_positional(admit_fn):
            call = lambda: admit_fn("ok")  # noqa: E731
        else:
            call = admit_fn
        try:
            raw = call()
        except Exception as exc:
            mapped = self._map_session_error(exc)
            if mapped.get("error"):
                return mapped
            raise
        if isinstance(raw, Mapping) and (
            raw.get("status") == "error" or raw.get("error")
        ):
            error = raw.get("error")
            return {
                "status": "error",
                "error": error if isinstance(error, str) and error else "admit_refused",
            }
        result = _public_result(raw)
        public = {"status": "admitted"}
        workflow_id = result.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id:
            public["workflow_id"] = workflow_id
        return public

    def _set_coach(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _COACH_FIELDS)
        hint = _safe_label(payload.get("hint"))
        if hint is None:
            raise AuthoringError("coach hint failed the PHI filter")
        self._coach_hint = hint
        setter = getattr(self.session, "set_coach", None)
        if callable(setter):
            try:
                setter(hint=hint)
            except TypeError:
                setter(hint)
        return {"status": "ok", "hint": hint}

    def _get_coach(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        getter = getattr(self.session, "get_coach", None)
        if callable(getter):
            raw = getter()
            if isinstance(raw, Mapping):
                hint = _safe_label(raw.get("hint"))
                return {"hint": hint}
            if isinstance(raw, str):
                return {"hint": _safe_label(raw)}
        return {"hint": self._coach_hint}

    def _bind_status(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        return {
            "status": "stdio",
            "bound": True,
            "transport": "stdio",
        }
