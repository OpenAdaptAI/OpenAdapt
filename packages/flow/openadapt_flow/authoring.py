"""Authoring session: scripted actuate+record through :class:`Recorder`.

Hosted MCP and local ``--authoring`` stdio drive a first demonstration by
calling this session, not Capture-while-inject and not the replay MCP tool.
Agent clicks/types/keys go through the existing :class:`~openadapt_flow.recorder.Recorder`
so ``events.jsonl`` and frames are the same path as ``record_desktop_demo``.

Human type during ``pause_for_input`` is persisted with
:meth:`Recorder.record_observed` on the pause-target node captured at pause
start. Continue must not call :meth:`Recorder.type_text` (that method actuates
``backend.type_text``) and must not read current OS focus (overlay Continue
can steal it).

Compile wraps :func:`~openadapt_flow.compiler.compile.compile_recording`
without changing that signature. The wrapper returns
``{status: "needs_human_admit", workflow_id}`` and never paints ``VERIFIED``.
A secret-field pause with no TYPE/param event refuses compile.

``admit(confirm=None)`` records that a named local operator accepted the
compiled draft. Empty / ``ok`` / ``yes`` / ``True`` all succeed. It does
not mint a Seal or a Production admission, and it does not ask the
operator to re-supply schema, authority, effect contract, environment, or
digest.

Windows native, Citrix, and RDP are ``COACH_ONLY``: agent-drive is refused.
Capture observers already drop OS-injected events; this module does not invent
a ``record_injected`` flag.

``observe`` returns a PHI-safe tree (no titles, field values, screenshots,
URLs, or backend pixels on the payload). Pixel bounds stay on the session
for later ``click`` / pause.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, TypedDict

from openadapt_flow.compiler.compile import compile_recording
from openadapt_flow.recorder import Recorder

__all__ = [
    "COACH_ONLY",
    "NEEDS_HUMAN_ADMIT",
    "OBSERVE_SCHEMA_VERSION",
    "AdmitResult",
    "AuthoringError",
    "AuthoringSession",
    "CoachOnlyError",
    "CompileResult",
    "MissingSecretTypeError",
    "open_session",
]

COACH_ONLY = "COACH_ONLY"
NEEDS_HUMAN_ADMIT: Literal["needs_human_admit"] = "needs_human_admit"
OBSERVE_SCHEMA_VERSION = "openadapt.authoring.observe/v1"

_AGENT_DRIVE_KINDS = frozenset({"web", "macos", "linux"})
_COACH_ONLY_KINDS = frozenset({"windows", "rdp"})
_PROVIDERS = {
    "web": "playwright_ax",
    "macos": "macos_ax",
    "linux": "linux_atspi",
}
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
_FORBIDDEN_OBSERVE_KEYS = frozenset(
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
_ACCEPT_CONFIRM = frozenset({"", "ok", "yes", "true", "y"})
_MAX_OBSERVE_NODES = 200
_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_PROJECTED_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_NODE_ID_RE = re.compile(r"^n_[0-9a-f]{8}$")
_SIX_DIGITS_RE = re.compile(r"\d{6,}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d\-\s().]{7,}\d)\b")
_PLAYWRIGHT_TREE_JS = """() => {
  const vw = Math.max(window.innerWidth || 0, 1);
  const vh = Math.max(window.innerHeight || 0, 1);
  const selector = [
    'button', 'a[href]', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
    '[role="tab"]', '[role="menuitem"]', '[contenteditable="true"]'
  ].join(',');
  const focused = document.activeElement;
  const nodes = [];
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (rect.right < 0 || rect.bottom < 0 || rect.left > vw || rect.top > vh) {
      continue;
    }
    const roleAttr = (el.getAttribute('role') || '').toLowerCase();
    const tag = (el.tagName || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let role = 'unknown';
    if (roleAttr === 'button' || tag === 'button') role = 'button';
    else if (roleAttr === 'link' || tag === 'a') role = 'link';
    else if (roleAttr === 'checkbox' || type === 'checkbox') role = 'checkbox';
    else if (roleAttr === 'radio' || type === 'radio') role = 'radio';
    else if (roleAttr === 'combobox' || tag === 'select') role = 'combobox';
    else if (roleAttr === 'tab') role = 'tab';
    else if (roleAttr === 'menuitem') role = 'menu_item';
    else if (
      roleAttr === 'textbox' || tag === 'textarea' || tag === 'input'
    ) role = 'text_input';
    const label = (el.getAttribute('aria-label') || '').trim();
    const name = label || (tag === 'button' ? (el.textContent || '').trim() : '');
    nodes.push({
      role,
      control_type: tag || null,
      automation_id: el.id || null,
      class_name: (el.className && typeof el.className === 'string')
        ? el.className.split(/\\s+/)[0] : null,
      name: name.slice(0, 80),
      enabled: !el.disabled,
      focused: el === focused,
      bounds: {
        x: rect.x / vw, y: rect.y / vh,
        w: rect.width / vw, h: rect.height / vh
      },
      backend_pixels: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        w: Math.round(rect.width), h: Math.round(rect.height)
      }
    });
    if (nodes.length >= 200) break;
  }
  return {tree: nodes, truncated: nodes.length >= 200};
}"""


class AuthoringError(RuntimeError):
    """Fail-loud authoring-session error with a closed-vocab ``code``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CoachOnlyError(AuthoringError):
    """Agent-drive is refused; the person actuates and Capture records."""

    def __init__(self, backend_kind: str) -> None:
        super().__init__(
            f"{COACH_ONLY}: agent-drive is refused for backend "
            f"{backend_kind!r}; the person actuates and Capture records",
            code=COACH_ONLY,
        )
        self.backend_kind = backend_kind


class MissingSecretTypeError(AuthoringError):
    """A secret-field pause was never recorded as a TYPE/param event."""

    def __init__(self, param: str) -> None:
        super().__init__(
            f"compile refused: secret-field pause {param!r} has no TYPE/param event",
            code="missing_secret_type",
        )
        self.param = param


class CompileResult(TypedDict):
    """Authoring compile is not success of the job."""

    status: Literal["needs_human_admit"]
    workflow_id: str
    recording_retained: bool


class AdmitResult(TypedDict, total=False):
    """Local operator acceptance of a compiled draft. Not a Seal."""

    status: Literal["accepted"]
    workflow_id: str
    digest: str


@dataclass(frozen=True)
class _NodePixels:
    """Laptop-only click target. ``x/y/w/h`` are backend pixels."""

    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def region(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class _Pause:
    param: str
    secret: bool
    target: _NodePixels
    before_png: bytes
    structural_before: dict[str, Any]


def _normalize_backend_kind(kind: str) -> str:
    k = (kind or "").strip().lower().replace("_", "-")
    if k in {"remote-display", "citrix"}:
        return "rdp"
    if k in {"win", "win-agent"}:
        return "windows"
    return k


def _pixels_from_mapping(raw: Mapping[str, Any]) -> _NodePixels:
    try:
        x = int(raw["x"])
        y = int(raw["y"])
        w = int(raw["w"])
        h = int(raw["h"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthoringError(
            "backend_pixels must be {x, y, w, h} integers",
            code="invalid_backend_pixels",
        ) from exc
    if w < 0 or h < 0:
        raise AuthoringError(
            "backend_pixels w/h must be non-negative",
            code="invalid_backend_pixels",
        )
    return _NodePixels(x=x, y=y, w=w, h=h)


def _load_events(recording_dir: Path) -> list[dict[str, Any]]:
    path = recording_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _safe_process_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or not _PROCESS_NAME_RE.fullmatch(collapsed):
        return None
    if "://" in collapsed or "@" in collapsed or _SIX_DIGITS_RE.search(collapsed):
        return None
    return collapsed


def _safe_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 80:
        return None
    if not _PROJECTED_LABEL_RE.fullmatch(collapsed):
        return None
    if "://" in collapsed or "@" in collapsed or _SIX_DIGITS_RE.search(collapsed):
        return None
    if (
        _EMAIL_RE.search(collapsed)
        or _SSN_RE.search(collapsed)
        or _PHONE_RE.search(collapsed)
    ):
        return None
    return collapsed


def _confirm_is_ok(confirm: object) -> bool:
    """Empty / ok / yes / True succeed. Anything else is not a one-OK admit."""

    if confirm is True or confirm is None:
        return True
    if confirm is False:
        return False
    if isinstance(confirm, str):
        return confirm.strip().casefold() in _ACCEPT_CONFIRM
    return False


def _local_operator() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    if not isinstance(name, str) or not name.strip():
        return "local-operator"
    return name.strip()[:64]


def _finite_unit(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _normalized_bounds(
    raw: Mapping[str, Any], viewport: tuple[int, int]
) -> Optional[dict[str, float]]:
    out: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        number = _finite_unit(raw.get(key))
        if number is None or number < 0:
            return None
        out[key] = number
    vw, vh = viewport
    if (
        out["x"] <= 1
        and out["y"] <= 1
        and out["w"] <= 1
        and out["h"] <= 1
        and out["x"] + out["w"] <= 1 + 1e-9
        and out["y"] + out["h"] <= 1 + 1e-9
    ):
        return out
    if vw <= 0 or vh <= 0:
        return None
    scaled = {
        "x": out["x"] / vw,
        "y": out["y"] / vh,
        "w": out["w"] / vw,
        "h": out["h"] / vh,
    }
    if (
        scaled["x"] < 0
        or scaled["y"] < 0
        or scaled["x"] + scaled["w"] > 1 + 1e-9
        or scaled["y"] + scaled["h"] > 1 + 1e-9
    ):
        return None
    return scaled


def _pixels_optional(raw: Any) -> Optional[_NodePixels]:
    if not isinstance(raw, Mapping):
        return None
    try:
        return _pixels_from_mapping(raw)
    except AuthoringError:
        return None


class AuthoringSession:
    """Wrap a live backend in :class:`Recorder` for one authoring demonstration.

    Args:
        backend: Live actuation/screenshot backend (Playwright, macOS, Linux).
        out_dir: Recording directory passed to :class:`Recorder`.
        backend_kind: Substrate pin (``web`` / ``macos`` / ``linux`` /
            ``windows`` / ``rdp`` / ``citrix``). Windows native, Citrix, and
            RDP raise :class:`CoachOnlyError`.
        app_url: Optional app URL stored in ``meta.json``.
        settle_interval_s: Recorder frame-settle poll interval.
        settle_stable_frames: Consecutive stable frames required to settle.
        settle_timeout_s: Max seconds to wait for the frame to settle.
    """

    def __init__(
        self,
        backend: Any,
        out_dir: Path | str,
        *,
        backend_kind: str,
        app_url: Optional[str] = None,
        settle_interval_s: float = 0.1,
        settle_stable_frames: int = 2,
        settle_timeout_s: float = 3.0,
    ) -> None:
        kind = _normalize_backend_kind(backend_kind)
        if kind in _COACH_ONLY_KINDS:
            raise CoachOnlyError(backend_kind)
        if kind not in _AGENT_DRIVE_KINDS:
            raise AuthoringError(
                f"unknown backend_kind {backend_kind!r} "
                "(expected: web | macos | linux | windows | rdp | citrix)",
                code="unknown_backend",
            )
        self._backend = backend
        self._kind = kind
        self._out_dir = Path(out_dir)
        self._app_url = app_url
        self._settle_interval_s = settle_interval_s
        self._settle_stable_frames = settle_stable_frames
        self._settle_timeout_s = settle_timeout_s
        self._recorder: Optional[Recorder] = None
        self._recording_dir: Optional[Path] = None
        self._nodes: dict[str, _NodePixels] = {}
        self._last_target: Optional[_NodePixels] = None
        self._pause: Optional[_Pause] = None
        self._secret_pause_params: list[str] = []
        self._halted = False
        self._draft: Optional[CompileResult] = None
        self._draft_digest: Optional[str] = None
        self._draft_bundle: Optional[Path] = None

    @property
    def backend_kind(self) -> str:
        return self._kind

    @property
    def recording_dir(self) -> Optional[Path]:
        return self._recording_dir

    def remember_node(
        self,
        node_id: str,
        backend_pixels: Mapping[str, Any],
    ) -> None:
        """Store laptop-only pixel bounds for a later ``click`` / pause."""

        self._nodes[node_id] = _pixels_from_mapping(backend_pixels)

    def observe(self) -> dict[str, Any]:
        """Return a PHI-safe authoring tree of the pinned window.

        Backend pixels stay on this session (via :meth:`remember_node`) and
        never appear on the payload. Titles, field values, screenshots, and
        URLs are dropped even when a backend supplies them.
        """

        self._require_not_halted()
        viewport = self._viewport()
        raw_nodes, raw_window, truncated = self._collect_raw_tree()
        tree: list[dict[str, Any]] = []
        for item in raw_nodes:
            node = self._project_node(item, viewport)
            if node is None:
                continue
            tree.append(node)
            if len(tree) >= _MAX_OBSERVE_NODES:
                truncated = True
                break
        window = self._project_window(raw_window, viewport)
        payload: dict[str, Any] = {
            "schema_version": OBSERVE_SCHEMA_VERSION,
            "backend": self._kind,
            "provider": _PROVIDERS.get(self._kind, "none"),
            "mode": "authoring",
            "agent_drive": window is not None,
            "coach_only": False,
            "recording": self._recorder is not None,
            "tree": tree,
            "truncated": truncated,
            "node_count": len(tree),
        }
        if window is not None:
            payload["window"] = window
        if not tree:
            payload["reason"] = "empty_projection"
        for key in list(payload):
            if key in _FORBIDDEN_OBSERVE_KEYS:
                payload.pop(key, None)
        return payload

    def start_record(self) -> None:
        """Construct :class:`Recorder` over the bound backend."""

        self._require_not_halted()
        if self._recorder is not None:
            raise AuthoringError("recording already started", code="already_recording")
        self._recorder = Recorder(
            self._backend,
            self._out_dir,
            app_url=self._app_url,
            settle_interval_s=self._settle_interval_s,
            settle_stable_frames=self._settle_stable_frames,
            settle_timeout_s=self._settle_timeout_s,
        )

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        *,
        node_id: Optional[str] = None,
    ) -> None:
        """Click through :class:`Recorder` at pixels or a remembered node."""

        recorder = self._require_recording()
        self._require_not_paused()
        if node_id is not None:
            target = self._lookup_node(node_id)
            px, py = target.center
            self._last_target = target
            recorder.click(px, py)
            return
        if x is None or y is None:
            raise AuthoringError("click requires node_id or x/y", code="invalid_click")
        self._last_target = _NodePixels(x=int(x), y=int(y), w=0, h=0)
        recorder.click(int(x), int(y))

    def type_text(self, text: str, param: Optional[str] = None) -> None:
        """Agent-driven type through :class:`Recorder` (actuates the backend).

        Human type after ``pause_for_input`` must use :meth:`continue_input`,
        never this method.
        """

        recorder = self._require_recording()
        self._require_not_paused()
        recorder.type_text(text, param=param)

    def press(self, key: str) -> None:
        """Press a key or chord through :class:`Recorder`."""

        recorder = self._require_recording()
        self._require_not_paused()
        recorder.press(key)

    def pause_for_input(
        self,
        *,
        param: str,
        secret: bool = False,
        node_id: Optional[str] = None,
        backend_pixels: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Capture the pause-target and before-frame; do not type.

        The pause-target is the node captured **at pause start** (remembered
        ``node_id`` / ``backend_pixels``, else the last click target), not
        current OS focus after overlay Continue.
        """

        recorder = self._require_recording()
        self._require_not_paused()
        if not param:
            raise AuthoringError("pause_for_input requires param", code="invalid_pause")
        target = self._resolve_pause_target(
            node_id=node_id, backend_pixels=backend_pixels
        )
        if secret and (target.w <= 0 or target.h <= 0):
            raise AuthoringError(
                "secret pause needs pause-target bounds to redact",
                code="missing_redact_region",
            )
        before_png = recorder._wait_settled()
        structural_before = recorder._structural_state()
        self._pause = _Pause(
            param=param,
            secret=secret,
            target=target,
            before_png=before_png,
            structural_before=structural_before,
        )
        if secret:
            self._secret_pause_params.append(param)
        return {"paused": True, "param": param, "secret": bool(secret)}

    def continue_input(self, *, operator_confirmed: bool = True) -> dict[str, Any]:
        """Record the user's already-typed value. Never actuates ``type_text``.

        Secret: ``secret=True``, no text on disk, ``redact_region`` of the
        pause-target bounds. An empty *readable* field stays paused. A masked
        field that AX reports empty (``text_value_at`` is ``None``) still
        records when the operator confirmed Continue.

        Non-secret: ``text_value_at`` / Playwright ``input_value`` on the
        pause-target pixels; the string goes into ``record_observed`` only.

        Returns:
            ``{recorded: true, param}`` with no value. ``recorded`` is false
            when an empty readable field keeps the session paused.
        """

        recorder = self._require_recording()
        pause = self._pause
        if pause is None:
            raise AuthoringError("no pause is in progress", code="not_paused")
        text = self._read_pause_target_text(pause.target)
        if pause.secret and text is not None and not text:
            # Readable empty secret field: the person has not typed yet.
            return {
                "recorded": False,
                "param": pause.param,
                "reason": "empty_secret_field",
            }
        if pause.secret and text is None and not operator_confirmed:
            return {
                "recorded": False,
                "param": pause.param,
                "reason": "unconfirmed_masked_secret",
            }
        if not pause.secret and (text is None or not text):
            # Non-secret MockMed note: do not persist an empty TYPE/param.
            return {
                "recorded": False,
                "param": pause.param,
                "reason": "empty_field" if text == "" else "unreadable_field",
            }
        event: dict[str, Any] = {"kind": "type"}
        redact_region: Optional[tuple[int, int, int, int]] = None
        if pause.secret:
            redact_region = pause.target.region
        else:
            event["text"] = text
        recorder.record_observed(
            event,
            before_png=pause.before_png,
            structural_before=pause.structural_before,
            param=pause.param,
            secret=pause.secret,
            redact_region=redact_region,
        )
        self._pause = None
        return {"recorded": True, "param": pause.param}

    def stop_record(self) -> Path:
        """Finish the recording (``meta.json`` + events/frames)."""

        return self._finish_recording()

    def halt(self) -> Optional[Path]:
        """Stop without compile. Finishes an in-flight recording if any."""

        self._pause = None
        if self._recorder is None:
            self._halted = True
            return None
        rec_dir = self._finish_recording()
        self._halted = True
        return rec_dir

    def compile(
        self,
        bundle_dir: Path | str | None = None,
        *,
        name: str = "authoring",
    ) -> CompileResult:
        """Compile via :func:`compile_recording`; never paints ``VERIFIED``.

        Refuses if this session was halted, is still paused, or had a
        secret-field pause with no TYPE/param event. The compiler signature
        and return type (``Workflow``) are unchanged; this wrapper is the
        admit gate. ``bundle_dir`` defaults to a sibling of the recording.
        """

        self._require_not_halted()
        if self._pause is not None:
            raise AuthoringError(
                "session is paused for input; call continue_input",
                code="paused",
            )
        if self._recorder is not None:
            self._finish_recording()
        recording_dir = self._recording_dir
        if recording_dir is None:
            raise AuthoringError("no recording to compile", code="not_recording")
        self._refuse_missing_secret_type(recording_dir)
        out_bundle = (
            Path(bundle_dir)
            if bundle_dir is not None
            else recording_dir.parent / f"{recording_dir.name}-bundle"
        )
        workflow = compile_recording(recording_dir, out_bundle, name=name)
        recording_id = workflow.recording_id or uuid.uuid4().hex
        workflow_id = f"wf_{recording_id}"
        result: CompileResult = {
            "status": NEEDS_HUMAN_ADMIT,
            "workflow_id": workflow_id,
            "recording_retained": True,
        }
        digest = None
        manifest = getattr(workflow, "manifest", None)
        raw_digest = getattr(manifest, "content_digest", None)
        if isinstance(raw_digest, str) and raw_digest:
            digest = raw_digest
        self._draft = result
        self._draft_digest = digest
        self._draft_bundle = out_bundle
        return result

    def admit(self, confirm: object = None) -> AdmitResult:
        """Record one-OK local acceptance of the compiled draft.

        Empty / ``ok`` / ``yes`` / ``True`` succeed. The operator does not
        re-supply schema, authority, effect contract, environment, or digest.
        This is not a Seal and not a Production admission.
        """

        self._require_not_halted()
        if not _confirm_is_ok(confirm):
            raise AuthoringError(
                "admit accepts empty, ok, yes, or True",
                code="admit_refused",
            )
        if self._draft is None:
            raise AuthoringError(
                "compile first; admit records local acceptance of the draft",
                code="not_compiled",
            )
        workflow_id = self._draft["workflow_id"]
        operator = _local_operator()
        record: dict[str, Any] = {
            "status": "accepted",
            "kind": "local_operator_accept",
            "workflow_id": workflow_id,
            "operator": operator,
        }
        if self._draft_digest:
            record["digest"] = self._draft_digest
        dest = self._draft_bundle if self._draft_bundle is not None else self._out_dir
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "admit.json").write_text(json.dumps(record, indent=2) + "\n")
        public: AdmitResult = {"status": "accepted", "workflow_id": workflow_id}
        if self._draft_digest:
            public["digest"] = self._draft_digest
        return public

    # -- internals -----------------------------------------------------------

    def _require_not_halted(self) -> None:
        if self._halted:
            raise AuthoringError(
                "session was halted without compile",
                code="halted",
            )

    def _require_recording(self) -> Recorder:
        self._require_not_halted()
        if self._recorder is None:
            raise AuthoringError(
                "start_record has not been called", code="not_recording"
            )
        return self._recorder

    def _require_not_paused(self) -> None:
        if self._pause is not None:
            raise AuthoringError(
                "session is paused for input; call continue_input",
                code="paused",
            )

    def _lookup_node(self, node_id: str) -> _NodePixels:
        target = self._nodes.get(node_id)
        if target is None:
            raise AuthoringError(
                f"unknown or stale node_id {node_id!r}", code="stale_node"
            )
        return target

    def _resolve_pause_target(
        self,
        *,
        node_id: Optional[str],
        backend_pixels: Optional[Mapping[str, Any]],
    ) -> _NodePixels:
        if backend_pixels is not None:
            target = _pixels_from_mapping(backend_pixels)
            if node_id is not None:
                self._nodes[node_id] = target
            self._last_target = target
            return target
        if node_id is not None:
            target = self._lookup_node(node_id)
            self._last_target = target
            return target
        if self._last_target is not None:
            return self._last_target
        raise AuthoringError(
            "pause_for_input needs node_id, backend_pixels, or a prior click",
            code="missing_pause_target",
        )

    def _read_pause_target_text(self, target: _NodePixels) -> Optional[str]:
        """Read the pause-target control. Never uses OS focus."""

        x, y = target.center
        getter = getattr(self._backend, "text_value_at", None)
        if callable(getter):
            try:
                value = getter(int(x), int(y))
            except Exception:
                value = None
            if isinstance(value, str):
                return value
        return self._playwright_input_value_at(int(x), int(y))

    def _playwright_input_value_at(self, x: int, y: int) -> Optional[str]:
        page = getattr(self._backend, "page", None)
        if page is None:
            return None
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return None
        try:
            result = evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    if ('value' in el && typeof el.value === 'string') return el.value;
                    return null;
                }""",
                [x, y],
            )
        except Exception:
            return None
        return result if isinstance(result, str) else None

    def _finish_recording(self) -> Path:
        recorder = self._require_recording()
        self._pause = None
        rec_dir = recorder.finish()
        self._recording_dir = rec_dir
        self._recorder = None
        return rec_dir

    def _refuse_missing_secret_type(self, recording_dir: Path) -> None:
        events = _load_events(recording_dir)
        typed_params = {
            event.get("param")
            for event in events
            if event.get("kind") == "type" and event.get("param")
        }
        for param in self._secret_pause_params:
            if param not in typed_params:
                raise MissingSecretTypeError(param)

    def _viewport(self) -> tuple[int, int]:
        raw = getattr(self._backend, "viewport", None)
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                raw = None
        if isinstance(raw, (tuple, list)) and len(raw) == 2:
            try:
                width, height = int(raw[0]), int(raw[1])
            except (TypeError, ValueError):
                width, height = 0, 0
            if width > 0 and height > 0:
                return (width, height)
        return (1280, 800)

    def _collect_raw_tree(
        self,
    ) -> tuple[list[Mapping[str, Any]], Optional[Mapping[str, Any]], bool]:
        getter = getattr(self._backend, "authoring_tree", None)
        if callable(getter):
            try:
                raw = getter()
            except Exception:
                raw = None
            if isinstance(raw, Mapping):
                tree = raw.get("tree")
                nodes = (
                    [item for item in tree if isinstance(item, Mapping)]
                    if isinstance(tree, list)
                    else []
                )
                window = raw.get("window")
                return (
                    nodes,
                    window if isinstance(window, Mapping) else None,
                    raw.get("truncated") is True or len(nodes) > _MAX_OBSERVE_NODES,
                )
            if isinstance(raw, list):
                nodes = [item for item in raw if isinstance(item, Mapping)]
                return nodes, None, len(nodes) > _MAX_OBSERVE_NODES
        page_tree = self._playwright_tree()
        if page_tree is not None:
            return page_tree
        return [], None, False

    def _playwright_tree(
        self,
    ) -> Optional[tuple[list[Mapping[str, Any]], Optional[Mapping[str, Any]], bool]]:
        page = getattr(self._backend, "page", None)
        if page is None:
            return None
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return None
        try:
            raw = evaluate(_PLAYWRIGHT_TREE_JS)
        except Exception:
            return None
        if not isinstance(raw, Mapping):
            return None
        tree = raw.get("tree")
        nodes = (
            [item for item in tree if isinstance(item, Mapping)]
            if isinstance(tree, list)
            else []
        )
        return nodes, None, raw.get("truncated") is True

    def _project_window(
        self,
        raw: Optional[Mapping[str, Any]],
        viewport: tuple[int, int],
    ) -> Optional[dict[str, Any]]:
        process_name = None
        bounds = None
        if isinstance(raw, Mapping):
            process_name = _safe_process_name(raw.get("process_name"))
            raw_bounds = raw.get("bounds")
            if isinstance(raw_bounds, Mapping):
                bounds = _normalized_bounds(raw_bounds, viewport)
        if process_name is None:
            for attr in ("process_name", "app", "_app", "_owner_substr"):
                process_name = _safe_process_name(getattr(self._backend, attr, None))
                if process_name is not None:
                    break
        if process_name is None:
            process_name = {
                "web": "Chromium",
                "macos": "App",
                "linux": "App",
            }.get(self._kind)
        if process_name is None:
            return None
        if bounds is None:
            bounds = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        return {"process_name": process_name, "role": "window", "bounds": bounds}

    def _project_node(
        self,
        raw: Mapping[str, Any],
        viewport: tuple[int, int],
    ) -> Optional[dict[str, Any]]:
        bounds_raw = raw.get("bounds")
        bounds = (
            _normalized_bounds(bounds_raw, viewport)
            if isinstance(bounds_raw, Mapping)
            else None
        )
        pixels = _pixels_optional(raw.get("backend_pixels"))
        if bounds is None and pixels is not None:
            bounds = _normalized_bounds(
                {"x": pixels.x, "y": pixels.y, "w": pixels.w, "h": pixels.h},
                viewport,
            )
        if bounds is None:
            return None
        if pixels is None and bounds_raw is not None:
            # Normalized bounds only: map back through the viewport for click.
            vw, vh = viewport
            pixels = _NodePixels(
                x=int(bounds["x"] * vw),
                y=int(bounds["y"] * vh),
                w=max(1, int(bounds["w"] * vw)),
                h=max(1, int(bounds["h"] * vh)),
            )
        role = raw.get("role")
        if role not in _ELEMENT_ROLES:
            role = "unknown"
        enabled = raw.get("enabled")
        focused = raw.get("focused")
        if not isinstance(enabled, bool):
            enabled = True
        if not isinstance(focused, bool):
            focused = False
        node_id = raw.get("node_id")
        if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
            seed = (
                f"{role}|{raw.get('automation_id') or ''}|"
                f"{bounds['x']:.4f}|{bounds['y']:.4f}|"
                f"{bounds['w']:.4f}|{bounds['h']:.4f}"
            )
            node_id = "n_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
        node: dict[str, Any] = {
            "node_id": node_id,
            "role": role,
            "enabled": enabled,
            "focused": focused,
            "bounds": bounds,
        }
        for key in ("control_type", "class_name", "automation_id", "name"):
            label = _safe_label(raw.get(key))
            if label:
                node[key] = label[:64] if key == "class_name" else label
        if pixels is not None:
            self._nodes[node_id] = pixels
        return node


def open_session(
    backend: Any,
    out_dir: Path | str,
    *,
    backend_kind: str,
    app_url: Optional[str] = None,
    settle_interval_s: float = 0.1,
    settle_stable_frames: int = 2,
    settle_timeout_s: float = 3.0,
) -> AuthoringSession:
    """Construct an :class:`AuthoringSession` (Desktop / stdio ``--authoring``)."""

    return AuthoringSession(
        backend,
        out_dir,
        backend_kind=backend_kind,
        app_url=app_url,
        settle_interval_s=settle_interval_s,
        settle_stable_frames=settle_stable_frames,
        settle_timeout_s=settle_timeout_s,
    )
