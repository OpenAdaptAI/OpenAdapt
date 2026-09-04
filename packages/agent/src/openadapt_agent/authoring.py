"""Local stdio authoring tools: first demo, not governed run.

``openadapt-agent serve --authoring`` registers the same probe names hosted
MCP will use: ``observe``, ``start_record``, ``click``, ``halt``. Local
stdio may also include ``type`` for agent-driven typing through Flow's
Recorder. Hosted remains pause-only. Human type during a pause is
``record_observed`` everywhere; never ``type_text`` on the pause target.

This module is a transport-independent stdio bridge. It does not open a
network listener. Hosted ChatGPT.com uses ``openadapt-agent authoring
connect`` (outbound mailbox poll in :mod:`openadapt_agent.mailbox`).
Window titles, field values, screenshots, and backend pixels never
cross the MCP wire. ``--authoring`` does not imply ``--allow-run``.

Deps F1/C1/T1 are not required to be merged: Flow's ``AuthoringSession``
is constructed when importable; Capture's projector is used when
importable; Types' observe schema is used when importable. Each is
fail-closed with a local fallback.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_agent.bridge import BridgeError, ToolSpec

__all__ = [
    "AUTHORING_LOCAL_TOOLS",
    "AUTHORING_PROBE_TOOLS",
    "AuthoringBridge",
    "AuthoringError",
    "MAX_AUTHORING_WIRE_BYTES",
    "NODE_ID_RE",
    "OBSERVE_SCHEMA_VERSION",
    "discover_desktop_authoring_ipc",
    "open_authoring_session",
    "pin_local_backend",
    "project_observe",
]

OBSERVE_SCHEMA_VERSION = "openadapt.authoring.observe/v1"
try:
    from openadapt_types.authoring import AUTHORING_OBSERVE_SCHEMA as _TYPES_OBSERVE
except ImportError:
    try:
        from openadapt_types.authoring import OBSERVE_SCHEMA_VERSION as _TYPES_OBSERVE
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
_COACH_ONLY_BACKENDS = frozenset({"windows", "rdp", "citrix"})
_AGENT_DRIVE_BACKENDS = frozenset({"web", "macos", "linux"})
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
_ADMIT_FIELDS = frozenset({"confirm"})
_ADMIT_ACCEPT = frozenset({"", "ok", "yes", "y", "enter"})

_DESKTOP_IPC_RELATIVE = Path(".openadapt") / "desktop_ipc.json"

_PROBE_HELP = {
    "observe": (
        "Return a PHI-safe authoring observation of the pinned local window "
        "(openadapt.authoring.observe/v1). No screenshots, OCR, field values, "
        "window titles, URLs, or backend pixels. Use node_id values from this "
        "tree for click."
    ),
    "start_record": (
        "Start a Flow Recorder session over the locally pinned backend. "
        "Refuses Windows native, Citrix, and RDP (coach-only). Does not "
        "compile and does not enable run tools."
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
    """Stdio stand-in when the pinned substrate is v1 coach-only.

    Observe works. Agent-drive (start_record / click / type) refuses.
    Never constructs the in-guest Windows agent HTTP helper.
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


def discover_desktop_authoring_ipc(*, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return Desktop authoring IPC discovery when D2 has advertised it.

    Overlay stays Desktop-owned. Stdio ``--authoring`` does not speak D2.
    Hosted ChatGPT.com uses :mod:`openadapt_agent.mailbox` (outbound poll).
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
) -> tuple[Any, str, Any]:
    """Pin the local target. Titles never go to MCP.

    Prefers Desktop loopback when D2 has advertised authoring IPC, but does
    not speak that protocol from this MIT package. Otherwise:

    - ``url`` launches Playwright Chromium with empty cookies (no debug-port
      attach, not the person's already-logged-in Chrome).
    - Windows native / RDP / Citrix → coach-only; never the in-guest Windows
      agent HTTP helper.
    - macOS / Linux → unique frontmost window via Flow backends when those
      constructors are importable (F1). Non-unique Linux titles are coach-only.
    """

    if backend is not None:
        kind = backend_kind or "web"
        return backend, kind, None
    plat = platform or sys.platform
    if url:
        return _pin_web(url, headed=headed)
    if plat == "win32" or plat.startswith("win"):
        return None, "windows", None
    native = _try_pin_native(plat)
    if native is not None:
        return native
    raise AuthoringError(
        "stdio --authoring needs a locally pinned window: pass --url for "
        "Playwright Chromium with empty cookies, or run Desktop so overlay "
        "stays single-owner. Native pin uses Flow backends after a unique "
        "frontmost window (openadapt_flow.authoring / F1)"
    )


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


def _try_pin_native(plat: str) -> Optional[tuple[Any, str, Any]]:
    """Best-effort unique frontmost window. None means caller should fail closed."""

    if plat == "darwin":
        try:
            from openadapt_flow.backends.macos_backend import MacOSBackend
            from openadapt_flow.backends.remote_display import MacWindowClient
        except ImportError:
            return None
        client = MacWindowClient()
        window_id = client.frontmost_window_id()
        if window_id is None:
            return None
        finder = getattr(client, "find_windows", None)
        if not callable(finder):
            return None
        matches = [item for item in finder("", None) if getattr(item, "window_id", None) == window_id]
        if len(matches) != 1:
            return None
        info = matches[0]
        app = getattr(info, "owner", None) or getattr(info, "app", None)
        title = getattr(info, "title", None)
        if not isinstance(app, str) or not app:
            return None
        same_title = [
            item
            for item in finder(app, title if isinstance(title, str) else None)
            if getattr(item, "window_id", None)
        ]
        if title and len(same_title) != 1:
            return None
        return MacOSBackend(client, app=app, window_title=title), "macos", None
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
    **kwargs: Any,
) -> object:
    """Construct Flow's public authoring session when that module exists.

    ``AuthoringSession`` (F1) requires ``backend``, ``out_dir``, and
    ``backend_kind``. This helper pins locally (or returns a coach-only
    stand-in for Windows native / Citrix / RDP) so ``serve --authoring``
    still works when F1 lands. Desktop IPC is preferred when advertised;
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
    work_dir = Path(out_dir) if out_dir is not None else Path("runs") / "authoring"
    close = None
    kind = backend_kind
    pinned = backend
    if pinned is None and kind is None:
        try:
            pinned, kind, close = pin_local_backend(
                url=url, headed=headed, platform=platform
            )
        except AuthoringError:
            plat = platform or sys.platform
            if plat == "win32" or plat.startswith("win"):
                pinned, kind, close = None, "windows", None
            else:
                raise
    kind = kind or "web"
    normalized = kind.strip().lower().replace("_", "-")
    if normalized in {"remote-display", "citrix", "rdp", "windows", "win", "win-agent"}:
        session = CoachOnlySession(
            "citrix" if normalized == "citrix" else ("rdp" if normalized in {"rdp", "remote-display"} else "windows")
        )
        if close is not None:
            session.close = close  # type: ignore[attr-defined]
        return session
    opener = getattr(flow_authoring, "open_session", None)
    session_cls = getattr(flow_authoring, "AuthoringSession", None)
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
            if isinstance(exc, AuthoringError):
                raise
            raise AuthoringError(str(exc), code=code if isinstance(code, str) else None) from exc
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
    if not isinstance(value.get("enabled"), bool) or not isinstance(value.get("focused"), bool):
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
        backend = payload.get("backend") if payload.get("backend") in _T1_BACKENDS else "windows"
        provider = payload.get("provider") if payload.get("provider") in _T1_PROVIDERS else "none"
        coach_only = backend in _COACH_ONLY_BACKENDS
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


def _require_object(arguments: Optional[dict[str, Any]], allowed: set[str]) -> dict[str, Any]:
    payload = dict(arguments or {})
    unknown = set(payload) - allowed
    if unknown:
        raise AuthoringError("arguments do not match the declared authoring schema")
    return payload


def _confirm_is_acceptance(value: Any) -> bool:
    if value is None or value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in _ADMIT_ACCEPT:
        return True
    return False


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

    def __init__(self, session: object, *, out_dir: Optional[Path | str] = None):
        self.session = session
        self._out_dir = Path(out_dir) if out_dir is not None else None
        self._last_tool: Optional[str] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._coach_only = isinstance(session, CoachOnlySession)
        self._nodes: dict[str, dict[str, Any]] = {}
        self._coach_hint: Optional[str] = None

    def handles(self, name: str) -> bool:
        return name in AUTHORING_TOOLS

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
                    "A named human then calls admit with a one-token ok. "
                    "An agent click never paints VERIFIED. Refuses a session "
                    "that had a secret pause and no TYPE/param event."
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
                    "One-step human admit of the pre-filled draft (schema, "
                    "authority, effect contract, environment, digest). The "
                    "human does not fill those fields. Missing confirm, empty, "
                    "ok, yes, y, enter, or true accepts. Any other confirm "
                    "refuses. Does not mint a Seal or write an unsigned ledger "
                    "row when the Flow session has no admit."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": ["string", "boolean"],
                            "description": (
                                "Optional one-token ok. Omit, empty, ok, yes, "
                                "y, enter, or true accepts. Anything else "
                                "refuses."
                            ),
                        }
                    },
                    "additionalProperties": False,
                },
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
        return specs

    def dispatch(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if name not in AUTHORING_TOOLS:
            raise AuthoringError("unknown tool name")
        if name == "get_command_result":
            _require_object(arguments, _RESULT_FIELDS)
            return self._last_command_result()
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
            "admit": self._admit,
            "set_coach": self._set_coach,
            "get_coach": self._get_coach,
            "bind_status": self._bind_status,
        }
        result = handlers[name](arguments)
        self._last_tool = name
        self._last_result = result
        return result

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
                "refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)",
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
                stored = {key: pixels[key] for key in ("x", "y", "w", "h") if key in pixels}
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
                "backend": getattr(self.session, "backend_kind", None) or getattr(self.session, "backend", "web"),
                "coach_only": self._coach_only,
            }
        )
        self._coach_only = projected.get("coach_only") is True
        return projected

    def _refuse_coach_only(self, tool: str) -> None:
        if self._coach_only:
            raise AuthoringError(
                f"{tool} refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)",
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
                "start_record refused: COACH_ONLY (person actuates; this backend "
                "is not agent-drive in v1)",
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
        if isinstance(raw, Mapping) and raw.get("error") in {"stale_node", "COACH_ONLY"}:
            if raw.get("error") == "stale_node" or raw.get("status") == "stale_node":
                return {"status": "error", "error": "stale_node"}
            raise AuthoringError(
                "click refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)",
                code="COACH_ONLY",
            )
        result = _public_result(raw)
        if result.get("error") == "stale_node" or result.get("status") == "stale_node":
            return {"status": "error", "error": "stale_node"}
        if result.get("error") == "COACH_ONLY":
            raise AuthoringError(
                "click refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)",
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
        bundle_dir = (self._out_dir or Path("runs") / "authoring") / "bundle"
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
        if result.get("status") == "error" or result.get("error") == "missing_secret_type":
            return {"status": "error", "error": result.get("error") or "missing_secret_type"}
        workflow_id = result.get("workflow_id")
        public = {
            "status": "needs_human_admit",
            "recording_retained": True,
        }
        if isinstance(workflow_id, str) and workflow_id:
            public["workflow_id"] = workflow_id
        summary = _safe_label(result.get("summary"))
        if summary:
            public["summary"] = summary
        return public

    def _admit(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _ADMIT_FIELDS)
        confirm = payload.get("confirm") if "confirm" in payload else None
        if not _confirm_is_acceptance(confirm):
            raise AuthoringError(
                "admit refused: confirm is not a one-token ok",
                code="admit_refused",
            )
        admit_fn = getattr(self.session, "admit", None)
        if not callable(admit_fn):
            raise AuthoringError(
                "authoring session does not implement admit; "
                "refusing rather than minting a Seal, writing an unsigned "
                "ledger row, or claiming Production",
                code="admit_unavailable",
            )
        try:
            try:
                raw = admit_fn(confirm=confirm)
            except TypeError:
                try:
                    raw = admit_fn(confirm)
                except TypeError:
                    raw = admit_fn()
        except Exception as exc:
            mapped = self._map_session_error(exc)
            if mapped.get("error"):
                return mapped
            raise
        if isinstance(raw, Mapping) and (raw.get("status") == "error" or raw.get("error")):
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
