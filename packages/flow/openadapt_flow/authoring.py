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

Windows native, Citrix, and RDP are ``COACH_ONLY``: agent-drive is refused.
Capture observers already drop OS-injected events; this module does not invent
a ``record_injected`` flag.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, TypedDict

from openadapt_flow.compiler.compile import compile_recording
from openadapt_flow.recorder import Recorder

__all__ = [
    "COACH_ONLY",
    "NEEDS_HUMAN_ADMIT",
    "AuthoringError",
    "AuthoringSession",
    "CoachOnlyError",
    "CompileResult",
    "MissingSecretTypeError",
    "open_session",
]

COACH_ONLY = "COACH_ONLY"
NEEDS_HUMAN_ADMIT: Literal["needs_human_admit"] = "needs_human_admit"

_AGENT_DRIVE_KINDS = frozenset({"web", "macos", "linux"})
_COACH_ONLY_KINDS = frozenset({"windows", "rdp"})


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
        return {
            "status": NEEDS_HUMAN_ADMIT,
            "workflow_id": f"wf_{recording_id}",
            "recording_retained": True,
        }

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
