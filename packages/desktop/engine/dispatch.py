"""dispatch -- the single command dispatcher shared by both local wires.

Two surfaces talk to the engine and BOTH route through this one dispatcher, so
command semantics can never drift between them:

    * the Tauri sidecar over stdin/stdout JSON-lines (:mod:`engine.ipc`), whose
      frontend catalog of command names lives in the app's ``src/lib/engine.ts``
      (``CMD`` values); and
    * the tray over the loopback TCP socket (:mod:`engine.socket_server`), whose
      command names live in the tray's ``IPCMessageType`` enum.

The dispatcher keys handlers on the EXACT ``engine.ts`` ``CMD`` strings
(``compile_recording`` not ``compile`` -- review 2.1 P0-2/P0-3). The tray's
command vocabulary is a strict subset (``start_recording`` / ``stop_recording``
/ ``get_status`` / ``pause_sync`` / ``resume_sync`` / ``open_workflow_library``
/ ``open_teach``); the socket server maps those names straight through.

Each handler returns a JSON-serializable ``dict`` whose shape matches the
frontend TypeScript types (``AuthStatus`` / ``EngineStatus`` / ``Workflow`` /
``RunReport`` / ``SyncState`` / ``NeedsAttention`` / ``PermissionStatus``).
Handlers emit events (``recording_started`` / ``compile_progress`` /
``sync_state`` / ``break_count`` / ``log_line`` / ...) through the injected
``emit`` callback so both wires stream the same events.

Services (db / storage / controller / flow bridge) are built lazily on first
use, so constructing a dispatcher is cheap and side-effect-free.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from engine.config import EngineConfig

EmitFn = Callable[[str, dict], None]

_RUN_PERSISTENCE_MARKER = ".desktop-run-persistence.json"
_KNOWN_RUN_OUTCOMES = frozenset(
    {
        "VERIFIED",
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
        "success",
        "halt",
        "unknown",
    }
)
_TEACHABLE_RUN_OUTCOMES = frozenset({"HALTED", "halt"})


def _noop_emit(event: str, data: dict) -> None:
    """Default event sink -- drops events when no emitter is wired."""


@dataclass
class _ActiveFlowRecording:
    capture_id: str
    capture_dir: Path
    started_at: str
    started_monotonic: float
    session: Any
    redactions: tuple[str, ...]
    task: str = ""


class EngineServices:
    """Lazily-constructed engine subsystems shared across commands.

    Args:
        config: Engine configuration.
        db: Injected :class:`~engine.db.IndexDB` (built on demand otherwise).
        storage: Injected storage manager (built on demand otherwise).
        audit: Injected audit logger (built on demand otherwise).
        controller: Injected recording controller (built on demand otherwise).
        flow_bridge: Injected flow bridge (built on demand otherwise).
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        db: Any = None,
        storage: Any = None,
        audit: Any = None,
        controller: Any = None,
        flow_bridge: Any = None,
        runner: Any = None,
        portal: Any = None,
    ) -> None:
        self.config = config
        self._db = db
        self._storage = storage
        self._audit = audit
        self._controller = controller
        self._flow_bridge = flow_bridge
        # The runner-loop service is shared across wires like everything else,
        # but it needs the dispatcher's emit callback, so the dispatcher builds
        # it lazily (tests inject a fake here).
        self.runner = runner
        # The mobile decision portal is likewise built on first use so the
        # engine never binds a socket or spawns a console it was not asked for.
        self.portal = portal

    @property
    def db(self) -> Any:
        if self._db is None:
            from engine.db import IndexDB

            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self._db = IndexDB(self.config.data_dir / "index.db")
            self._db.initialize()
        return self._db

    @property
    def storage(self) -> Any:
        if self._storage is None:
            from engine.storage_manager import StorageManager

            self._storage = StorageManager(self.config)
            self._storage.initialize()
            self._storage._db = self.db
        return self._storage

    @property
    def audit(self) -> Any:
        if self._audit is None:
            from engine.audit import AuditLogger

            self._audit = AuditLogger(
                self.config.audit_log_path, enabled=self.config.network_audit_log
            )
        return self._audit

    @property
    def flow_bridge(self) -> Any:
        if self._flow_bridge is None:
            from engine.flow_bridge import FlowBridge

            self._flow_bridge = FlowBridge()
        return self._flow_bridge

    @property
    def controller(self) -> Any:
        if self._controller is None:
            from engine.controller import RecordingController

            self._controller = RecordingController(
                captures_dir=self.config.data_dir / "captures",
                quality=self.config.recording_quality,
                storage_manager=self.storage,
                flow_bridge=self.flow_bridge,
                db=self.db,
                bundles_dir=self.config.data_dir / "bundles",
            )
        return self._controller

    def close(self) -> None:
        """Release owned resources (only the DB holds an open handle)."""
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass


class EngineDispatcher:
    """Maps command names to engine actions for both local wires.

    Args:
        config: Engine configuration.
        services: Injected :class:`EngineServices` (built from ``config``
            otherwise). Injected in tests to supply fakes.
        emit: Callback ``emit(event, data)`` used to stream events to the
            connected surface(s). Defaults to a no-op.
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        services: EngineServices | None = None,
        emit: EmitFn | None = None,
    ) -> None:
        self.config = config
        self.services = services or EngineServices(config)
        self.emit = emit or _noop_emit
        # Sync is orthogonal to recording -- a single paused flag mirrors the
        # tray's pause/resume-sync commands and the frontend sync banner.
        self._sync_paused = False
        self._flow_recording: _ActiveFlowRecording | None = None
        self._handlers: dict[str, Callable[..., dict | None]] = {}
        self._register()

    # ------------------------------------------------------------------ setup

    def _register(self) -> None:
        """Register every command keyed on the frontend ``engine.ts`` name."""
        self._handlers = {
            # recording lifecycle
            "start_recording": self.start_recording,
            "stop_recording": self.stop_recording,
            "pause_recording": self.pause_recording,
            "resume_recording": self.resume_recording,
            "get_status": self.get_status,
            # library / captures / workflows
            "get_workflows": self.get_workflows,
            "get_captures": self.get_captures,
            "get_storage_usage": self.get_storage_usage,
            "get_presentation_export_status": self.get_presentation_export_status,
            "export_presentation_video": self.export_presentation_video,
            # the loop: compile -> replay/run -> teach
            "compile_recording": self.compile_recording,
            "replay_workflow": self.replay_workflow,
            "run_workflow": self.run_workflow,
            "get_run_report": self.get_run_report,
            "retry_run_persistence": self.retry_run_persistence,
            "teach_fix": self.teach_fix,
            # qualification cockpit (canonical Flow graph/policy/manifests)
            "get_qualification": self.get_qualification,
            "initialize_qualification": self.initialize_qualification,
            "set_qualification_risk": self.set_qualification_risk,
            "arm_qualification_identity": self.arm_qualification_identity,
            "set_qualification_identity": self.set_qualification_identity,
            "bind_qualification_effect": self.bind_qualification_effect,
            "set_qualification_effect_verification": (self.set_qualification_effect_verification),
            "set_qualification_minimum_effect_tier": (self.set_qualification_minimum_effect_tier),
            "add_qualification_case": self.add_qualification_case,
            "run_qualification_case": self.run_qualification_case,
            "import_qualification_results": self.import_qualification_results,
            "certify_qualification": self.certify_qualification,
            "version_qualification_workflow": self.version_qualification_workflow,
            "seal_qualification_workflow": self.seal_qualification_workflow,
            "export_qualification_workflow": self.export_qualification_workflow,
            "deploy_qualification_workflow": self.deploy_qualification_workflow,
            # cloud sync / push
            "push_workflow": self.push_workflow,
            "get_sync_state": self.get_sync_state,
            "pause_sync": self.pause_sync,
            "resume_sync": self.resume_sync,
            "get_needs_attention": self.get_needs_attention,
            # auth (both providers live in engine.auth -- spec 3a)
            "login_browser": self.login_browser,
            "login_paste": self.login_paste,
            "connect_uri": self.connect_uri,
            "logout": self.logout,
            "get_auth_status": self.get_auth_status,
            # config / settings
            "get_config": self.get_config,
            "set_config": self.set_config,
            # effective policy (fail-closed; Tier-1 user / Tier-2 org / Tier-3 safety)
            "get_effective_policy": self.get_effective_policy,
            "refresh_policy": self.refresh_policy,
            # OS permissions
            "check_permissions": self.check_permissions,
            "request_input_monitoring": self.request_input_monitoring,
            # capability-aware surface availability (engine.capabilities)
            "get_capabilities": self.get_capabilities,
            # review / egress gate
            "scrub_capture": self.scrub_capture,
            "approve_review": self.approve_review,
            "dismiss_review": self.dismiss_review,
            "get_pending_reviews": self.get_pending_reviews,
            # tray-only UI navigation (relayed to the desktop frontend)
            "open_workflow_library": self.open_workflow_library,
            "open_teach": self.open_teach,
            # runner lane (EXPERIMENTAL -- outbound /api/runners/* long-poll)
            "runner_status": self.runner_status,
            "runner_enable": self.runner_enable,
            "runner_disable": self.runner_disable,
            # mobile attended-decision portal (Desktop owns lifecycle, ingress,
            # QR pairing, and generic notifications -- never a decision)
            "portal_status": self.portal_status,
            "portal_start": self.portal_start,
            "portal_stop": self.portal_stop,
            "portal_create_pairing": self.portal_create_pairing,
            "portal_pairing_status": self.portal_pairing_status,
            "portal_approve_pairing": self.portal_approve_pairing,
            "portal_cancel_pairing": self.portal_cancel_pairing,
            "portal_devices": self.portal_devices,
            "portal_revoke_device": self.portal_revoke_device,
            "portal_notification": self.portal_notification,
        }

    @property
    def commands(self) -> list[str]:
        """The registered command names (for discovery / tests)."""
        return sorted(self._handlers)

    def dispatch(self, cmd: str, params: dict | None = None) -> dict | None:
        """Dispatch a command by name, returning its JSON-serializable result.

        Args:
            cmd: The command name (an ``engine.ts`` ``CMD`` value / tray type).
            params: Command parameters.

        Returns:
            The handler's result dict.

        Raises:
            KeyError: If the command is not registered.
        """
        handler = self._handlers.get(cmd)
        if handler is None:
            raise KeyError(f"Unknown command: {cmd}")
        return handler(**(params or {}))

    # ------------------------------------------------------- recording

    def start_recording(self, **params: Any) -> dict:
        """Start a recording session and emit ``recording_started``."""
        controller = self.services.controller
        if self._flow_recording is not None:
            return self._status_dict(controller)
        if controller.is_recording:
            return self._status_dict(controller)

        target = None
        if params.get("target") is not None:
            try:
                target, deployment_config = self._execution_target(params)
                if target is None:
                    raise ValueError("recording target is required")
                if deployment_config is not None:
                    raise ValueError(
                        "deployment_config applies to replay and governed run, not recording"
                    )
                target.validate_record_required()
            except ValueError as exc:
                message = str(exc)
                self.emit("recording_error", {"error": message})
                raise ValueError(message) from None
            from engine.capabilities import CapabilityError, ensure_backend_capability

            try:
                ensure_backend_capability(target.backend, action="record")
            except CapabilityError as exc:
                message = str(exc)
                self.emit("recording_error", {"error": message})
                raise ValueError(message) from None

        needs_native_input = target is None or target.backend != "web"
        if (
            sys.platform == "darwin"
            and needs_native_input
            and not _mac_preflight_input_monitoring()
        ):
            # Starting a capture is the explicit user action where macOS may
            # present its Input Monitoring consent prompt. Passive permission
            # checks must remain prompt-free.
            if not _mac_request_input_monitoring():
                message = (
                    "Input Monitoring permission is required to record keyboard "
                    "and mouse input. Grant it in System Settings, then try again."
                )
                self.emit("recording_error", {"error": message})
                raise PermissionError(message)
        task = str(params.get("purpose") or params.get("task") or params.get("name") or "")
        if target is not None:
            if target.backend == "web":
                ensure_browser = getattr(
                    self.services.flow_bridge,
                    "ensure_browser_runtime",
                    None,
                )
                if ensure_browser is not None:
                    try:
                        ensure_browser(
                            lambda state, detail: self.emit(
                                "browser_runtime",
                                {
                                    "workflow_id": "recording",
                                    "state": state,
                                    "detail": detail,
                                },
                            )
                        )
                    except Exception:
                        message = "Browser setup failed before recording began"
                        self.emit("recording_error", {"error": message})
                        raise RuntimeError(message) from None
            capture_id = uuid.uuid4().hex[:8]
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
            capture_dir = self.config.data_dir / "captures" / f"{timestamp}_{capture_id}"
            control_dir = self.config.data_dir / "recording-control"
            stop_path = control_dir / f".stop-{capture_id}"
            ready_path = control_dir / f".ready-{capture_id}"
            started_at = datetime.now(timezone.utc).isoformat()
            from engine.private_flow_config import (
                prepare_flow_record_request,
                stage_private_yaml,
            )

            if target.backend == "web" and not target.url:
                active = _ActiveFlowRecording(
                    capture_id=capture_id,
                    capture_dir=capture_dir,
                    started_at=started_at,
                    started_monotonic=time.monotonic(),
                    session=None,
                    redactions=(),
                    task=task,
                )
                self.services.audit.log("recording_started", capture_id=capture_id)
                self.emit("recording_started", {"capture_id": capture_id})
                try:
                    result = self.services.flow_bridge.demo_record(capture_dir)
                except Exception:
                    message = "OpenAdapt Flow could not create the bundled demonstration"
                    self.emit("recording_error", {"error": message})
                    self.emit("status_update", self._status_dict(controller))
                    raise RuntimeError(message) from None
                return self._finalize_flow_capture(active, result)

            prepared = prepare_flow_record_request(
                target=target,
                out_dir=capture_dir,
                task=task,
                stop_path=stop_path,
                ready_path=ready_path,
            )
            try:
                with stage_private_yaml(
                    control_dir,
                    prepared=prepared,
                    prefix=".record-request-",
                ) as request:
                    session = self.services.flow_bridge.start_record(
                        capture_dir,
                        request=request,
                        stop_path=stop_path,
                        ready_path=ready_path,
                    )
            except Exception:
                stop_path.unlink(missing_ok=True)
                ready_path.unlink(missing_ok=True)
                message = "OpenAdapt Flow could not start the selected recording target"
                self.emit("recording_error", {"error": message})
                raise RuntimeError(message) from None
            self._flow_recording = _ActiveFlowRecording(
                capture_id=capture_id,
                capture_dir=capture_dir,
                started_at=started_at,
                started_monotonic=time.monotonic(),
                session=session,
                redactions=prepared.redactions,
                task=task,
            )
            self.services.audit.log("recording_started", capture_id=capture_id)
            self.emit("recording_started", {"capture_id": capture_id})
            self.emit("status_update", self._status_dict(controller))
            return {"capture_id": capture_id, "recording": True}

        capture_id = controller.start(task_description=str(task))
        self.services.audit.log("recording_started", capture_id=capture_id)
        self.emit("recording_started", {"capture_id": capture_id})
        self.emit("status_update", self._status_dict(controller))
        return {"capture_id": capture_id, "recording": True}

    def stop_recording(self, **params: Any) -> dict:
        """Stop the active recording, retain it, and compile it automatically."""
        controller = self.services.controller
        active = self._flow_recording
        if active is not None:
            try:
                result = active.session.stop()
            finally:
                self._flow_recording = None
            return self._finalize_flow_capture(active, result)
        if not controller.is_recording:
            self.emit("recording_error", {"error": "No recording is active"})
            return {"capture_id": None, "recording": False}
        metadata = controller.stop()
        self.emit("recording_stopped", metadata)
        self.emit("status_update", self._status_dict(controller))
        stopped = {"capture_id": metadata.get("id"), **metadata}
        stopped["compile"] = self._compile_registered_capture(
            str(stopped["capture_id"]),
            automatic=True,
        )
        return stopped

    def _finalize_flow_capture(self, active: _ActiveFlowRecording, result: Any) -> dict:
        """Register one compile-ready Flow capture and emit its local evidence."""

        from engine.private_flow_config import redact_flow_log

        for line in (result.stdout or "").splitlines():
            self.emit(
                "log_line",
                {"line": redact_flow_log(line, active.redactions)},
            )
        if not result.ok or not (active.capture_dir / "meta.json").is_file():
            message = (
                "OpenAdapt Flow could not finalize a compile-ready recording; "
                "the incomplete local capture was retained for inspection"
            )
            self.emit("recording_error", {"error": message})
            self.emit("status_update", self._status_dict(self.services.controller))
            raise RuntimeError(message)

        meta_path = active.capture_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("capture_id", active.capture_id)
        if active.task:
            meta["task_description"] = active.task
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if self.services.db.get_capture(active.capture_id) is None:
            self.services.db.insert_capture(
                active.capture_id,
                str(active.capture_dir),
                str(meta.get("started_at") or active.started_at),
                task_description=str(meta.get("task_description") or ""),
            )
        duration = max(0.0, time.monotonic() - active.started_monotonic)
        metadata = {
            "capture_id": active.capture_id,
            "id": active.capture_id,
            "duration": duration,
            "event_count": 0,
            "size_bytes": sum(
                path.stat().st_size for path in active.capture_dir.rglob("*") if path.is_file()
            ),
            "path": str(active.capture_dir),
            "recording": False,
        }
        self.emit("recording_stopped", metadata)
        self.emit("status_update", self._status_dict(self.services.controller))
        metadata["compile"] = self._compile_registered_capture(
            active.capture_id,
            automatic=True,
        )
        return metadata

    def pause_recording(self, **params: Any) -> dict:
        """Pause is not supported (stop/start instead); report current status."""
        return self.get_status()

    def resume_recording(self, **params: Any) -> dict:
        """Resume is not supported (stop/start instead); report current status."""
        return self.get_status()

    def get_status(self, **params: Any) -> dict:
        """Return the current :class:`EngineStatus`-shaped recording status."""
        return self._status_dict(self.services.controller)

    def _status_dict(self, controller: Any) -> dict:
        from engine.controller import RecordingState

        if self._flow_recording is not None:
            return {
                "recording": True,
                "paused": False,
                "duration_secs": max(
                    0.0,
                    time.monotonic() - self._flow_recording.started_monotonic,
                ),
                "capture_id": self._flow_recording.capture_id,
                "controls": {"pause": False, "resume": False, "stop": True},
            }
        recording = controller.is_recording
        paused = controller.state == RecordingState.PAUSED
        duration = None
        started = getattr(controller, "_started_at", None)
        if started:
            try:
                from datetime import datetime, timezone

                duration = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(started)
                ).total_seconds()
            except Exception:
                duration = None
        return {
            "recording": recording,
            "paused": paused,
            "duration_secs": duration,
            "capture_id": controller.current_capture_id,
            # The canonical Capture/Flow record sessions currently support a
            # clean finalizing stop, not a lossless pause/resume. Keep this
            # capability explicit so every UI can avoid a placebo control.
            "controls": {
                "pause": False,
                "resume": False,
                "stop": recording,
            },
        }

    # ------------------------------------------------------- library

    def get_workflows(self, **params: Any) -> list:
        """Return the local workflow library as a list of ``Workflow`` dicts.

        The frontend (``src/lib/engine.ts`` / ``App.tsx`` / ``WorkflowLibrary``)
        consumes a bare ``Workflow[]``; return the list directly so the two
        parallel wires share one shape.
        """
        bundles = self.services.db.list_bundles(limit=int(params.get("limit", 100)))
        return [self._bundle_to_workflow(b) for b in bundles]

    def _bundle_to_workflow(self, b: dict) -> dict:
        bid = b.get("bundle_id")
        open_halts = sum(
            1 for h in self.services.db.list_open_halts() if h.get("workflow_id") == bid
        )
        last_run_state = None
        try:
            rep = self.get_run_report(workflow_id=bid)
        except Exception:
            rep = None
        if rep:
            report_outcome = rep.get("outcome")
            states = {s.get("state") for s in (rep.get("steps") or [])}
            if report_outcome == "VERIFIED":
                last_run_state = "verified"
            elif report_outcome == "HALTED" or rep.get("halt") or "halted" in states:
                last_run_state = "halted"
            elif report_outcome == "FAILED" or "failed" in states:
                last_run_state = "failed"
            elif report_outcome in {"COMPLETED_UNVERIFIED", "ROLLED_BACK", "unknown"}:
                last_run_state = "attention"
            elif states:
                last_run_state = "verified"
        return {
            "id": bid,
            "name": b.get("workflow_name") or b.get("capture_id") or bid,
            "steps": b.get("steps") or 0,
            "updated_at": b.get("compiled_at") or b.get("created_at"),
            "last_run_state": last_run_state,
            "open_halts": open_halts,
            "synced": bool(b.get("workflow_id")),
            "workflow_id": b.get("workflow_id"),
        }

    def get_captures(self, **params: Any) -> dict:
        """Return recent captures from local storage."""
        captures = self.services.storage.get_captures(
            limit=int(params.get("limit", 50)),
            review_status=params.get("status"),
        )
        return {"captures": captures}

    def _presentation_capture_dir(self, params: dict[str, Any]) -> Path:
        capture_id = str(params.get("capture_id") or "")
        capture = self.services.db.get_capture(capture_id) if capture_id else None
        if not capture:
            raise ValueError(f"Unknown capture {capture_id or '<missing>'}")
        capture_dir = Path(str(capture["capture_path"])).resolve(strict=True)
        captures_root = (self.config.data_dir / "captures").resolve(strict=False)
        try:
            capture_dir.relative_to(captures_root)
        except ValueError as error:
            raise ValueError("Capture path is outside the local capture boundary") from error
        if capture_dir == captures_root or not capture_dir.is_dir():
            raise ValueError("Capture path is not a capture directory")
        return capture_dir

    def get_presentation_export_status(self, **params: Any) -> dict:
        from engine.presentation_export import presentation_export_status

        return presentation_export_status(self._presentation_capture_dir(params))

    def export_presentation_video(self, **params: Any) -> dict:
        from engine.presentation_export import export_presentation_video

        return export_presentation_video(self._presentation_capture_dir(params))

    def get_storage_usage(self, **params: Any) -> dict:
        """Return local storage usage."""
        return self.services.storage.get_storage_usage()

    # ------------------------------------------------------- the loop

    def compile_recording(self, **params: Any) -> dict:
        """Compile a captured recording into a flow bundle.

        Frontend passes ``capture_id``; returns ``{workflow_id}`` where the id
        is the LOCAL bundle id (the hosted id only exists after a push).
        """
        capture_id = params.get("capture_id")
        if not capture_id:
            return {"ok": False, "error": "capture_id is required", "workflow_id": ""}
        return self._compile_registered_capture(str(capture_id), automatic=False)

    def _compile_registered_capture(self, capture_id: str, *, automatic: bool) -> dict:
        """Compile one retained capture and report a retryable local state.

        Flow writes the bundle to a separate directory. The source recording
        remains in the capture directory on success and on failure.
        """
        progress = {"capture_id": capture_id, "automatic": automatic}
        capture = self.services.db.get_capture(capture_id)
        capture_dir = capture and (capture.get("capture_path") or capture.get("capture_dir"))
        if not capture_dir:
            error = f"OpenAdapt could not find the retained recording {capture_id}."
            self.emit(
                "compile_progress",
                {
                    **progress,
                    "state": "failed",
                    "error": error,
                    "recording_retained": False,
                },
            )
            return {
                "ok": False,
                "error": error,
                "workflow_id": "",
                "recording_retained": False,
            }
        self.emit("compile_progress", {**progress, "state": "compiling"})
        try:
            compiled = self.services.controller.compile_capture(capture_id, Path(capture_dir))
        except Exception:
            logger.exception("Compile failed for retained capture {cid}", cid=capture_id)
            compiled = None
        recording_retained = Path(capture_dir).is_dir()
        if not compiled:
            error = (
                "OpenAdapt could not compile this recording. "
                "The raw recording was retained and is ready for another attempt."
                if recording_retained
                else "OpenAdapt could not compile because the recording is no longer available."
            )
            failed = {
                **progress,
                "state": "failed",
                "error": error,
                "recording_retained": recording_retained,
            }
            self.emit("compile_progress", failed)
            return {
                "ok": False,
                "error": error,
                "workflow_id": "",
                "recording_retained": recording_retained,
            }
        self.emit(
            "compile_progress",
            {
                **progress,
                "state": "compiled",
                "bundle_id": compiled["bundle_id"],
                "recording_retained": recording_retained,
            },
        )
        return {
            "ok": True,
            "workflow_id": compiled["bundle_id"],
            "bundle_path": compiled["bundle_path"],
            "recording_retained": recording_retained,
        }

    def replay_workflow(self, **params: Any) -> dict:
        """Replay a bundle locally and return a ``RunReport``-shaped dict."""
        return self._replay_or_run(params, run=False)

    def run_workflow(self, **params: Any) -> dict:
        """Run a bundle under the deployment config; return a ``RunReport`` dict."""
        return self._replay_or_run(params, run=True)

    def _replay_or_run(self, params: dict, *, run: bool) -> dict:
        workflow_id = params.get("workflow_id")
        bundle = self._bundle_dir(workflow_id)
        if bundle is None:
            return self._pre_action_refusal(f"Unknown workflow {workflow_id}")
        try:
            target, deployment_config = self._execution_target(params)
        except ValueError as exc:
            return self._pre_action_refusal(str(exc))
        if target is not None:
            from engine.capabilities import CapabilityError, ensure_backend_capability

            try:
                ensure_backend_capability(target.backend, action="run" if run else "replay")
            except CapabilityError as exc:
                return self._pre_action_refusal(str(exc))
        run_id = uuid.uuid4().hex[:8]
        run_dir = self.config.data_dir / "runs" / f"{'run' if run else 'replay'}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        params_file = None
        qualification_case_id = params.get("_qualification_case_id")
        qualification_case_execution = params.get("_qualification_case_execution")
        if qualification_case_id is not None:
            from engine.qualification_lifecycle import case_parameters_path

            params_file = case_parameters_path(
                self.config.data_dir,
                workflow_id=str(workflow_id),
                case_id=str(qualification_case_id),
            )
        from engine.bundle_keys import bundle_key_environment

        bundle_env = bundle_key_environment(str(workflow_id))
        backend = target.backend if target is not None else "configured"
        # A selected config always exists (validated above). The historical
        # governed-run default remains fail-loud when absent; a direct target
        # can still form a complete target-only deployment config.
        default_config = self.config.data_dir / "deployment.json"
        base_config = deployment_config
        if run and base_config is None and default_config.is_file():
            base_config = default_config

        result = None
        invocation_started = False
        log_redactions: tuple[str, ...] = ()
        try:
            from engine.private_flow_config import (
                prepare_flow_config,
                stage_private_yaml,
            )

            prepared = prepare_flow_config(base_config, target)
            log_redactions = prepared.redactions if prepared is not None else ()
            if prepared is None:
                staged_context = nullcontext(None)
            else:
                staged_context = stage_private_yaml(run_dir, prepared=prepared)

            with staged_context as staged_config:
                # Native/remote Flow backends do not import or launch
                # Playwright. Config-only execution delegates backend setup to
                # Flow, preserving a native/remote backend without injecting
                # Desktop's historical web default.
                ensure_browser = getattr(self.services.flow_bridge, "ensure_browser_runtime", None)
                should_ensure_browser = (
                    not run
                    and ensure_browser is not None
                    and (
                        (target is not None and target.backend == "web")
                        or (target is None and deployment_config is None)
                    )
                )
                if should_ensure_browser:
                    try:
                        ensure_browser(
                            lambda state, detail: self.emit(
                                "browser_runtime",
                                {
                                    "workflow_id": workflow_id,
                                    "state": state,
                                    "detail": detail,
                                },
                            )
                        )
                    except Exception:
                        return self._pre_action_refusal(
                            "Browser setup failed before Flow was invoked"
                        )

                self.emit(
                    "replay_progress",
                    {
                        "workflow_id": workflow_id,
                        "state": "running",
                        "backend": backend,
                        "mode": "governed" if run else "replay",
                        "total_steps": self._workflow_step_count(workflow_id),
                    },
                )
                try:
                    invocation_started = True
                    if run:
                        # A missing historical default stays fail-loud in Flow.
                        config_path = staged_config or default_config
                        run_kwargs: dict[str, Any] = {"out_dir": run_dir}
                        if params_file is not None:
                            run_kwargs["params_file"] = params_file
                        if bundle_env:
                            run_kwargs["env_overrides"] = bundle_env
                        if qualification_case_execution is not None:
                            result = self.services.flow_bridge.qualify_run_case(
                                bundle,
                                config_path,
                                case_id=str(qualification_case_execution["case_id"]),
                                inputs_file=Path(qualification_case_execution["inputs_file"]),
                                campaign_id=str(qualification_case_execution["campaign_id"]),
                                run_id=run_id,
                                out_dir=run_dir,
                                env_overrides=bundle_env,
                            )
                        else:
                            result = self.services.flow_bridge.run(
                                bundle,
                                config_path,
                                **run_kwargs,
                            )
                    else:
                        replay_kwargs: dict[str, Any] = {
                            "out_dir": run_dir,
                            "config": staged_config,
                        }
                        if params_file is not None:
                            replay_kwargs["params_file"] = params_file
                        if bundle_env:
                            replay_kwargs["env_overrides"] = bundle_env
                        result = self.services.flow_bridge.replay(bundle, **replay_kwargs)
                except Exception:
                    # Once invocation begins, delivery/effect state is unknown.
                    # Never echo exception text: it may contain config values.
                    result = None
        except Exception as exc:
            from engine.private_flow_config import PrivateFlowConfigError

            if invocation_started:
                # Cleanup or post-spawn failures cannot prove that no action
                # reached the target.
                result = None
            elif isinstance(exc, PrivateFlowConfigError):
                return self._pre_action_refusal(str(exc))
            else:
                return self._pre_action_refusal(
                    "Execution configuration could not be staged safely"
                )

        from engine.flow_bridge import FlowBridge

        report_data = FlowBridge.read_report(run_dir)
        if result is None:
            outcome = "unknown"
            error = (
                "Flow invocation ended without a classifiable result. The "
                "workflow may have delivered an action."
            )
        else:
            from engine.private_flow_config import redact_flow_log

            for line in (result.stdout or "").splitlines():
                self.emit(
                    "log_line",
                    {"line": redact_flow_log(line, log_redactions)},
                )
            outcome = FlowBridge.classify_outcome(result.returncode, report_data)
            error = None
            if outcome == "unknown":
                error = (
                    "Flow produced no explicit success or halt outcome "
                    f"(exit {result.returncode}). The workflow may have delivered "
                    "an action."
                )

        persistence = self._persist_local_run(
            run_id=run_id,
            run_dir=run_dir,
            workflow_id=str(workflow_id),
            outcome=outcome,
        )
        report = self._run_report(
            run_dir,
            workflow_id,
            run_id,
            outcome=outcome,
            error=error,
        )
        report["persistence"] = persistence
        progress_state = {
            "VERIFIED": "done",
            "COMPLETED_UNVERIFIED": "completed_unverified",
            "HALTED": "halted",
            "FAILED": "failed",
            "ROLLED_BACK": "rolled_back",
            "success": "done",
            "halt": "halted",
            "unknown": "unknown",
        }[outcome]
        self.emit(
            "replay_progress",
            {
                "workflow_id": workflow_id,
                "state": progress_state,
                # Preserve the precise contract outcome. A legacy ``done``
                # progress state alone is not sufficient to claim VERIFIED.
                "outcome": outcome,
                "backend": backend,
                "mode": "governed" if run else "replay",
                "profile": (
                    report.get("outcome_details", {}).get("profile")
                    if isinstance(report.get("outcome_details"), dict)
                    else None
                ),
                "current_step": len(report.get("steps") or []) or None,
                "total_steps": report.get("total_steps"),
                "duration_s": (report.get("metrics") or {}).get("duration_s"),
                "evidence_classes": (
                    report.get("outcome_details", {}).get("evidence_classes")
                    if isinstance(report.get("outcome_details"), dict)
                    else []
                ),
                "model_calls": (
                    report.get("outcome_details", {}).get("model_calls")
                    if isinstance(report.get("outcome_details"), dict)
                    else None
                ),
                "external_network_calls": (
                    report.get("outcome_details", {}).get("external_network_calls")
                    if isinstance(report.get("outcome_details"), dict)
                    else None
                ),
            },
        )
        return report

    @staticmethod
    def _persistence_marker_path(run_dir: Path) -> Path:
        return run_dir / _RUN_PERSISTENCE_MARKER

    def _persist_local_run(
        self,
        *,
        run_id: str,
        run_dir: Path,
        workflow_id: str,
        outcome: str,
    ) -> dict:
        """Persist one local run or leave a bounded recovery marker."""

        marker = self._persistence_marker_path(run_dir)
        payload = {
            "schema": "openadapt.desktop-run-persistence/v1",
            "run_id": run_id,
            "workflow_id": workflow_id,
            "outcome": outcome,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        staging = marker.with_name(f"{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            staging.write_text(json.dumps(payload, sort_keys=True))
            staging.replace(marker)
        except Exception:
            staging.unlink(missing_ok=True)
            return {
                "state": "failed",
                "retryable": False,
                "message": (
                    "The run report is available for this session, but Desktop "
                    "could not create a local history recovery record. Preserve "
                    "the run evidence before closing Desktop."
                ),
            }

        try:
            self.services.db.insert_run(
                run_id,
                str(run_dir),
                bundle_id=workflow_id,
                status=outcome,
            )
        except Exception:
            return self._degraded_run_persistence()

        try:
            marker.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove reconciled run persistence marker")
        return {
            "state": "persisted",
            "retryable": False,
            "message": "The report is saved in local history.",
        }

    @staticmethod
    def _degraded_run_persistence() -> dict:
        return {
            "state": "degraded",
            "retryable": True,
            "message": (
                "The report remains available, but Desktop could not add this run "
                "to local history. History and Teach will not use it until the "
                "local history save succeeds."
            ),
        }

    def _pending_run(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
    ) -> tuple[Path, dict] | None:
        """Find the newest valid local-history recovery marker."""

        runs_root = self.config.data_dir / "runs"
        if not runs_root.is_dir():
            return None
        matches: list[tuple[float, Path, dict]] = []
        for marker in runs_root.glob(f"*/{_RUN_PERSISTENCE_MARKER}"):
            if marker.parent.is_symlink():
                continue
            try:
                payload = json.loads(marker.read_text())
                valid = (
                    payload.get("schema") == "openadapt.desktop-run-persistence/v1"
                    and payload.get("workflow_id") == workflow_id
                    and payload.get("outcome") in _KNOWN_RUN_OUTCOMES
                    and isinstance(payload.get("run_id"), str)
                    and isinstance(payload.get("created_at"), str)
                    and (run_id is None or payload.get("run_id") == run_id)
                )
                if valid:
                    matches.append((marker.stat().st_mtime, marker.parent, payload))
            except (OSError, ValueError, TypeError):
                continue
        if not matches:
            return None
        _mtime, run_dir, payload = max(matches, key=lambda item: item[0])
        return run_dir, payload

    @staticmethod
    def _pending_run_is_newer(pending: tuple[Path, dict] | None, run: dict | None) -> bool:
        if pending is None:
            return False
        if run is None:
            return True
        return str(pending[1].get("created_at") or "") >= str(run.get("created_at") or "")

    def retry_run_persistence(self, **params: Any) -> dict:
        """Retry a failed local-history save from its bounded recovery marker."""

        workflow_id = str(params.get("workflow_id") or "")
        run_id = str(params.get("run_id") or "")
        if not workflow_id or not run_id:
            return {"ok": False, "error": "workflow_id and run_id are required"}

        existing = self.services.db.get_run(run_id)
        if existing is not None:
            if existing.get("bundle_id") != workflow_id:
                return {"ok": False, "error": "The saved run belongs to another workflow"}
            if existing.get("status") in _KNOWN_RUN_OUTCOMES:
                report = self._run_report(
                    Path(str(existing["run_path"])),
                    workflow_id,
                    run_id,
                    outcome=str(existing["status"]),
                )
                report["persistence"] = {
                    "state": "persisted",
                    "retryable": False,
                    "message": "The report is saved in local history.",
                }
                return {"ok": True, "report": report}

        pending = self._pending_run(workflow_id, run_id=run_id)
        if pending is None:
            return {
                "ok": False,
                "error": "No retryable local history record was found for this run",
            }
        run_dir, payload = pending
        try:
            if existing is None:
                self.services.db.insert_run(
                    run_id,
                    str(run_dir),
                    bundle_id=workflow_id,
                    status=str(payload["outcome"]),
                )
            else:
                if Path(str(existing.get("run_path") or "")) != run_dir:
                    return {
                        "ok": False,
                        "error": "The recovery record does not match the saved run path",
                    }
                self.services.db.update_run(run_id, status=str(payload["outcome"]))
        except Exception:
            return {
                "ok": False,
                "error": "Local history is still unavailable. Fix local storage and retry.",
            }
        try:
            self._persistence_marker_path(run_dir).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove reconciled run persistence marker")
        report = self._run_report(
            run_dir,
            workflow_id,
            run_id,
            outcome=str(payload["outcome"]),
        )
        report["persistence"] = {
            "state": "persisted",
            "retryable": False,
            "message": "The report is saved in local history.",
        }
        return {"ok": True, "report": report}

    @staticmethod
    def _pre_action_refusal(error: str) -> dict:
        """Return the only response that proves Flow was never invoked."""

        return {
            "ok": False,
            "outcome": "refused",
            "pre_action_refusal": True,
            "error": error,
        }

    def _execution_target(self, params: dict) -> tuple[Any | None, Path | None]:
        """Validate one typed PHI-capable target and optional config path."""

        from pydantic import ValidationError

        from engine.targets import ExecutionTarget

        raw_target = params.get("target")
        target = None
        if raw_target is not None:
            if not isinstance(raw_target, dict):
                raise ValueError("target must be an object")
            try:
                target = ExecutionTarget.model_validate(raw_target)
            except ValidationError as exc:
                # Pydantic errors can contain selector values. Return field
                # names only; every target value is treated as PHI-capable.
                fields = sorted(
                    {
                        ".".join(str(part) for part in error.get("loc", ()))
                        for error in exc.errors()
                        if error.get("loc")
                    }
                )
                detail = ", ".join(fields) or "target"
                raise ValueError(f"Invalid execution target ({detail})") from None

        raw_config = params.get("deployment_config")
        deployment_config: Path | None = None
        if raw_config is not None:
            if not isinstance(raw_config, str) or not raw_config.strip():
                raise ValueError("deployment_config must be a non-empty local file path")
            deployment_config = Path(raw_config.strip()).expanduser()
            if not deployment_config.is_file():
                raise ValueError("Selected deployment config file was not found")
        if target is not None:
            try:
                target.validate_required(deployment_config=deployment_config is not None)
            except ValueError as exc:
                raise ValueError(str(exc)) from None

        return target, deployment_config

    def get_run_report(self, **params: Any) -> dict | None:
        """Return the latest ``RunReport`` for a workflow, or None if none."""
        workflow_id = str(params.get("workflow_id") or "")
        runs = [
            r for r in self.services.db.list_runs(limit=100) if r.get("bundle_id") == workflow_id
        ]
        pending = self._pending_run(workflow_id)
        pending_is_newest = self._pending_run_is_newer(
            pending,
            runs[0] if runs else None,
        )
        if pending_is_newest:
            assert pending is not None
            run_dir, payload = pending
            report = self._run_report(
                run_dir,
                workflow_id,
                str(payload["run_id"]),
                outcome=str(payload["outcome"]),
            )
            report["persistence"] = self._degraded_run_persistence()
            return report
        if not runs:
            return None
        run = runs[0]
        run_dir = run.get("run_path")
        if not run_dir:
            return None
        stored_outcome = run.get("status")
        if stored_outcome not in _KNOWN_RUN_OUTCOMES:
            pending = self._pending_run(workflow_id, run_id=str(run.get("run_id") or ""))
            if pending is not None:
                pending_dir, payload = pending
                report = self._run_report(
                    pending_dir,
                    workflow_id,
                    str(payload["run_id"]),
                    outcome=str(payload["outcome"]),
                )
                report["persistence"] = self._degraded_run_persistence()
                return report
        outcome = stored_outcome if stored_outcome in _KNOWN_RUN_OUTCOMES else None
        report = self._run_report(
            Path(run_dir),
            workflow_id,
            run.get("run_id", ""),
            outcome=outcome,
        )
        report["persistence"] = (
            {
                "state": "persisted",
                "retryable": False,
                "message": "The report is saved in local history.",
            }
            if outcome is not None
            else {
                "state": "failed",
                "retryable": False,
                "message": (
                    "Desktop found the run evidence, but its local history outcome "
                    "is incomplete and no recovery record is available."
                ),
            }
        )
        return report

    def _run_report(
        self,
        run_dir: Path,
        workflow_id: str | None,
        run_id: str,
        *,
        outcome: str | None = None,
        error: str | None = None,
    ) -> dict:
        from engine.flow_bridge import PRECISE_FLOW_OUTCOMES, FlowBridge

        report = FlowBridge.read_report(run_dir)
        inferred_returncode = 0 if report.get("success") is True else 1
        classified_outcome = FlowBridge.classify_outcome(inferred_returncode, report)
        if outcome is None:
            # Report-only fallback for older DB rows. A true report can be
            # explicit success evidence; a structured halt remains a halt.
            outcome = classified_outcome
        elif outcome != "unknown" and classified_outcome != outcome:
            outcome = "unknown"
            error = error or (
                "The retained report no longer proves the stored execution outcome. "
                "Inspect the local run evidence before retrying."
            )
        halt = FlowBridge.read_halt(run_dir)
        halt_state = (
            (halt or {}).get("state_id")
            if outcome in {"HALTED", "halt"} and isinstance(halt, dict)
            else None
        )

        # openadapt-flow writes per-step outcomes under ``results`` (each with
        # step_id / intent / ok / resolution / effect_verified / elapsed_ms). Map
        # them onto the frontend ``RunStep`` shape; fall back to a pre-shaped
        # ``steps`` list for older reports.
        results = report.get("results")
        if isinstance(results, list) and results:
            steps = [self._map_step(r, halt_state) for r in results]
        else:
            raw_steps = report.get("steps")
            steps = raw_steps if isinstance(raw_steps, list) else []

        halt_block = None
        if outcome in {"HALTED", "halt"} and isinstance(halt, dict) and halt:
            rung = None
            for r in results or []:
                if r.get("step_id") == halt_state:
                    rung = (r.get("resolution") or {}).get("rung")
            halt_block = {
                "step_index": self._step_index(halt.get("state_id") or halt.get("step_index")),
                "step_intent": (
                    halt.get("intent") or halt.get("step_intent") or "Execution halted"
                ),
                "reason": halt.get("reason")
                or "The runtime halted before completing the workflow.",
                "resolver_rung": halt.get("resolver_rung") or rung,
            }

        total_steps = (
            self._workflow_step_count(workflow_id) or report.get("total_steps") or len(steps)
        )
        total_ms = report.get("total_ms")
        metrics = report.get("metrics") or {}
        duration_s = metrics.get("duration_s")
        if duration_s is None and isinstance(total_ms, (int, float)):
            duration_s = round(total_ms / 1000.0, 1)
        cost = metrics.get("cost_usd")
        if cost is None:
            cost = report.get("est_model_cost_usd")

        outcome_details = None
        envelope = report.get("outcome_envelope")
        if outcome in PRECISE_FLOW_OUTCOMES:
            # FlowBridge validated this exact envelope before classifying the
            # outcome. Project its bounded evidence contract into the cockpit.
            if isinstance(envelope, dict):
                outcome_details = {
                    "profile": envelope.get("profile"),
                    "production_eligible": envelope.get("production_eligible"),
                    "execution_completed": envelope.get("execution_completed"),
                    "required_contracts": envelope.get("required_contracts"),
                    "passed_contracts": envelope.get("passed_contracts"),
                    "evidence_classes": envelope.get("evidence_classes"),
                    "model_calls": envelope.get("model_calls"),
                    "external_network_calls": envelope.get("external_network_calls"),
                    "compensation_actions": envelope.get("compensation_actions"),
                }

        mapped = {
            "ok": outcome in {"VERIFIED", "success"},
            "outcome": outcome,
            "pre_action_refusal": False,
            "run_id": report.get("run_id") or run_id,
            "workflow_id": workflow_id or report.get("workflow_id") or "",
            "workflow_name": report.get("workflow_name", ""),
            "total_steps": total_steps,
            "steps": steps,
            "halt": halt_block,
            "metrics": {"duration_s": duration_s, "cost_usd": cost},
            "outcome_details": outcome_details,
        }
        if error:
            mapped["error"] = error
        return mapped

    @staticmethod
    def _step_index(step_id: Any) -> int:
        """Parse a ``step_009`` id (or int) into a 0-based index."""
        if isinstance(step_id, int):
            return step_id
        try:
            return int(str(step_id).rsplit("_", 1)[-1])
        except (ValueError, TypeError):
            return 0

    def _map_step(self, r: dict, halt_state: str | None) -> dict:
        """Map one flow ``results`` entry onto a frontend ``RunStep`` dict."""
        intent = str(r.get("intent") or "")
        action, _, rest = intent.partition(" ")
        target = rest.strip().strip("'\"") or "-"
        sid = r.get("step_id")
        if sid is not None and sid == halt_state:
            state = "halted"
        elif r.get("ok"):
            state = "verified"
        elif r.get("skipped"):
            state = "pending"
        else:
            state = "failed"
        ev = r.get("effect_verified")
        effect = "verified" if ev is True else ("not_verified" if ev is False else None)
        elapsed = r.get("elapsed_ms")
        return {
            "index": self._step_index(sid),
            "action": action or intent or "step",
            "target": target,
            "state": state,
            "latency_ms": round(elapsed) if isinstance(elapsed, (int, float)) else None,
            "effect": effect,
        }

    def _workflow_step_count(self, workflow_id: str | None) -> int | None:
        """Best-effort total step count from the bundle's ``workflow.json``."""
        bundle = self._bundle_dir(workflow_id)
        if bundle is None:
            return None
        wf = bundle / "workflow.json"
        if not wf.exists():
            return None
        try:
            data = json.loads(wf.read_text())
        except (OSError, ValueError):
            return None
        steps = data.get("steps") or data.get("program") or []
        return len(steps) if hasattr(steps, "__len__") else None

    def teach_fix(self, **params: Any) -> dict:
        """Teach a fix for a halted workflow via ``openadapt-flow teach``."""
        workflow_id = params.get("workflow_id")
        bundle = self._bundle_dir(workflow_id)
        if bundle is None:
            return {"promoted": False, "message": f"Unknown workflow {workflow_id}"}
        # Teach is a response to the latest execution state, not a search for
        # any historical halt. Selecting an older halt after a newer VERIFIED,
        # FAILED, or otherwise terminal run would promote evidence against a
        # stale application state.
        run = next(
            (r for r in self.services.db.list_runs(limit=100) if r.get("bundle_id") == workflow_id),
            None,
        )
        pending = self._pending_run(str(workflow_id))
        pending_matches_saved_run = bool(
            pending is not None
            and run is not None
            and pending[1].get("run_id") == run.get("run_id")
        )
        if pending_matches_saved_run or self._pending_run_is_newer(pending, run):
            return {
                "promoted": False,
                "message": (
                    "The latest run is not saved in local history. Retry the "
                    "local history save before teaching a fix."
                ),
            }
        if run is None:
            return {"promoted": False, "message": "No halted run to teach against"}
        if run.get("status") not in _TEACHABLE_RUN_OUTCOMES:
            status = str(run.get("status") or "unknown")
            return {
                "promoted": False,
                "message": f"The latest run ended as {status}, so it is not teachable.",
            }
        if not run.get("run_path"):
            return {
                "promoted": False,
                "message": "The latest halted run has no evidence path",
            }
        out_dir = self.config.data_dir / "bundles" / f"{workflow_id}_taught_{uuid.uuid4().hex[:6]}"
        try:
            result = self.services.flow_bridge.teach(Path(run["run_path"]), bundle, out_dir)
        except Exception as exc:
            return {"promoted": False, "message": str(exc)}
        message = "Fix promoted." if result.ok else (result.stderr or "Teach did not promote.")
        return {"promoted": result.ok, "message": message}

    def _bundle_dir(self, bundle_id: str | None) -> Path | None:
        if not bundle_id:
            return None
        bundle = self.services.db.get_bundle(bundle_id)
        if not bundle or not bundle.get("bundle_path"):
            return None
        return Path(bundle["bundle_path"])

    def _qualification_bundle_dir(self, bundle_id: str | None) -> Path:
        """Resolve one writable local bundle without following a staged symlink."""

        bundle = self._bundle_dir(bundle_id)
        if bundle is None:
            raise ValueError(f"Unknown workflow {bundle_id}")
        root = (self.config.data_dir / "bundles").resolve()
        resolved = bundle.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Workflow bundle is outside the local Desktop bundle store")
        # A symlinked bundle (or parent below the bundle-store root) can alias a
        # different workflow while still resolving inside ``root``.  Reject the
        # unresolved path itself before returning the canonical directory.
        cursor = bundle.absolute()
        while cursor.resolve() != root:
            if cursor == cursor.parent or not cursor.resolve().is_relative_to(root):
                raise ValueError("Workflow bundle is outside the local Desktop bundle store")
            if cursor.is_symlink():
                raise ValueError(
                    "Workflow bundle contains a symbolic link and cannot be edited safely"
                )
            cursor = cursor.parent
        if not resolved.is_dir():
            raise ValueError("Workflow bundle is unavailable")
        if any(path.is_symlink() for path in resolved.rglob("*")):
            raise ValueError("Workflow bundle contains a symbolic link and cannot be edited safely")
        return resolved

    @staticmethod
    def _qualification_bundle_key(bundle_id: str) -> str | None:
        from engine.bundle_keys import load_bundle_key

        return load_bundle_key(bundle_id)

    def get_qualification(self, **params: Any) -> dict:
        """Inspect Flow's canonical graph, coverage, and certification contract."""

        from engine.qualification import DEFAULT_QUALIFICATION_POLICY, inspect_bundle

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            return inspect_bundle(
                bundle,
                workflow_id=workflow_id,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def initialize_qualification(self, **params: Any) -> dict:
        """Create Flow's versioned project for one explicit environment boundary."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            initialize_qualification,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        raw_capabilities = params.get("required_capabilities") or []
        try:
            if not isinstance(raw_capabilities, list):
                raise ValueError("required_capabilities must be a list")
            bundle = self._qualification_bundle_dir(workflow_id)
            result = initialize_qualification(
                bundle,
                workflow_id=workflow_id,
                target_kind=str(params.get("target_kind") or ""),
                application=str(params.get("application") or ""),
                application_version=str(params.get("application_version") or ""),
                environment_label=(
                    str(params["environment_label"])
                    if params.get("environment_label") is not None
                    else None
                ),
                environment_digest=(
                    str(params["environment_digest"])
                    if params.get("environment_digest") is not None
                    else None
                ),
                required_capabilities=[str(item) for item in raw_capabilities],
                minimum_effect_tier=int(params.get("minimum_effect_tier", 3)),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(workflow_id, status="qualification_pending")
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def set_qualification_risk(self, **params: Any) -> dict:
        """Correct one action risk, reseal it, and invalidate prior certification."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            set_action_risk,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            result = set_action_risk(
                bundle,
                workflow_id=workflow_id,
                step_id=str(params.get("step_id") or ""),
                risk=str(params.get("risk") or ""),
                explanation=(
                    str(params["explanation"]) if params.get("explanation") is not None else None
                ),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def arm_qualification_identity(self, **params: Any) -> dict:
        """Arm retained Flow identity evidence for one exact action."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            arm_action_identity,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            result = arm_action_identity(
                bundle,
                workflow_id=workflow_id,
                step_id=str(params.get("step_id") or ""),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def set_qualification_identity(self, **params: Any) -> dict:
        """Persist exact or signal-quorum semantics for retained identity evidence."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            set_action_identity_policy,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        raw_signals = params.get("signals") or []
        try:
            if not isinstance(raw_signals, list) or not all(
                isinstance(signal, dict) for signal in raw_signals
            ):
                raise ValueError("signals must be a list of identity signal objects")
            bundle = self._qualification_bundle_dir(workflow_id)
            result = set_action_identity_policy(
                bundle,
                workflow_id=workflow_id,
                step_id=str(params.get("step_id") or ""),
                enforcement=str(params.get("enforcement") or ""),
                signals=raw_signals,
                quorum=int(params.get("quorum", 0)),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def bind_qualification_effect(self, **params: Any) -> dict:
        """Bind one parameterized Flow effect contract without raw JSON edits."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            bind_action_effect,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            raw_index = params.get("effect_index")
            result = bind_action_effect(
                bundle,
                workflow_id=workflow_id,
                step_id=str(params.get("step_id") or ""),
                kind=str(params.get("kind") or ""),
                match_field=str(params.get("match_field") or ""),
                match_param=str(params.get("match_param") or ""),
                field=(str(params["field"]) if params.get("field") is not None else None),
                value_param=(
                    str(params["value_param"]) if params.get("value_param") is not None else None
                ),
                idempotency_param=(
                    str(params["idempotency_param"])
                    if params.get("idempotency_param") is not None
                    else None
                ),
                key_field=str(params.get("key_field") or "key"),
                expected_count=int(params.get("expected_count", 1)),
                count_new_only=bool(params.get("count_new_only", False)),
                effect_index=int(raw_index) if raw_index is not None else None,
                verification_tier=int(params.get("verification_tier", 3)),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def set_qualification_effect_verification(self, **params: Any) -> dict:
        """Set the required evidence tier for one selected declared effect."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            set_action_effect_verification,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            result = set_action_effect_verification(
                bundle,
                workflow_id=workflow_id,
                step_id=str(params.get("step_id") or ""),
                effect_index=int(params.get("effect_index", -1)),
                verification_tier=int(params.get("verification_tier", 3)),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def set_qualification_minimum_effect_tier(self, **params: Any) -> dict:
        """Version the project's minimum accepted effect strength."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            set_project_minimum_effect_tier,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            result = set_project_minimum_effect_tier(
                bundle,
                workflow_id=workflow_id,
                minimum_effect_tier=int(params.get("minimum_effect_tier", 3)),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_pending"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def add_qualification_case(self, **params: Any) -> dict:
        """Add a typed case and keep its optional parameter fixture local."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            add_qualification_case,
        )
        from engine.qualification_lifecycle import store_case_parameters

        workflow_id = str(params.get("workflow_id") or "")
        case_id = str(params.get("case_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            from engine.qualification import inspect_bundle

            current = inspect_bundle(
                bundle,
                workflow_id=workflow_id,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            secret_params = {
                str(item["name"])
                for item in current.get("controls", {}).get("parameters", [])
                if item.get("secret")
            }
            input_ref = None
            parameters_json = params.get("parameters_json")
            if parameters_json is not None:
                _path, input_ref = store_case_parameters(
                    self.config.data_dir,
                    workflow_id=workflow_id,
                    case_id=case_id,
                    parameters_json=str(parameters_json),
                    forbidden_keys=secret_params,
                )
            result = add_qualification_case(
                bundle,
                workflow_id=workflow_id,
                case_id=case_id,
                kind=str(params.get("kind") or "representative"),
                description=str(params.get("description") or ""),
                input_ref=input_ref,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(workflow_id, status="qualification_pending")
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def run_qualification_case(self, **params: Any) -> dict:
        """Execute, hash, sign, and retain one case against the exact revision."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            prepare_local_qualification_runner,
            record_local_qualification_result,
            set_local_qualification_case_scope,
        )
        from engine.qualification_lifecycle import (
            retain_capability_observation,
            retain_run_evidence,
            stage_case_runtime_inputs,
            store_case_parameters,
        )

        workflow_id = str(params.get("workflow_id") or "")
        case_id = str(params.get("case_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            from engine.qualification import inspect_bundle

            current = inspect_bundle(
                bundle,
                workflow_id=workflow_id,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            if not any(
                str(item.get("id")) == case_id
                for item in (current.get("project") or {}).get("cases", [])
            ):
                raise ValueError(f"Unknown qualification case {case_id!r}")
            secret_params = {
                str(item["name"])
                for item in current.get("controls", {}).get("parameters", [])
                if item.get("secret")
            }
            parameters_json = params.get("parameters_json")
            if parameters_json is not None:
                store_case_parameters(
                    self.config.data_dir,
                    workflow_id=workflow_id,
                    case_id=case_id,
                    parameters_json=str(parameters_json),
                    forbidden_keys=secret_params,
                )
            from engine.qualification import _load
            from engine.qualification_lifecycle import case_parameters_path

            parameters_path = case_parameters_path(
                self.config.data_dir,
                workflow_id=workflow_id,
                case_id=case_id,
            )
            if parameters_path is None:
                raise ValueError("Qualification case parameters are required before execution")
            workflow_for_inputs = _load(
                bundle,
                key=self._qualification_bundle_key(workflow_id),
            )
            inputs_path, runtime_input_bytes = stage_case_runtime_inputs(
                self.config.data_dir,
                workflow_id=workflow_id,
                case_id=case_id,
                workflow=workflow_for_inputs,
                parameters_path=parameters_path,
            )
            set_local_qualification_case_scope(
                bundle,
                workflow_id=workflow_id,
                case_id=case_id,
                runtime_input_bytes=runtime_input_bytes,
                fault_target=params.get("fault_target"),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            prepare_local_qualification_runner(
                bundle,
                workflow_id=workflow_id,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            execution_params = {
                "workflow_id": workflow_id,
                "_qualification_case_id": case_id,
                "_qualification_case_execution": {
                    "case_id": case_id,
                    "inputs_file": str(inputs_path),
                    "campaign_id": uuid.uuid4().hex,
                },
            }
            if params.get("target") is not None:
                execution_params["target"] = params["target"]
            if params.get("deployment_config") is not None:
                execution_params["deployment_config"] = params["deployment_config"]
            execution = self._replay_or_run(execution_params, run=True)
            if execution.get("pre_action_refusal"):
                return {
                    "ok": False,
                    "workflow_id": workflow_id,
                    "error": execution.get("error") or "Case execution was refused",
                    "case_run": execution,
                }
            run_id = str(execution.get("run_id") or "")
            run = self.services.db.get_run(run_id)
            if not run or not run.get("run_path"):
                raise ValueError("Qualification run did not retain a local evidence directory")
            raw_report_path = Path(str(run["run_path"])) / "report.json"
            if not raw_report_path.is_file() or raw_report_path.is_symlink():
                raise ValueError("Qualification run did not retain a valid report")
            raw_report_bytes = raw_report_path.read_bytes()
            try:
                raw_report = json.loads(raw_report_bytes)
            except json.JSONDecodeError as exc:
                raise ValueError("Qualification run report is not valid JSON") from exc
            if not isinstance(raw_report, dict):
                raise ValueError("Qualification run report must be a JSON object")
            evidence = retain_run_evidence(
                bundle,
                case_id=case_id,
                run_id=run_id,
                run_dir=Path(str(run["run_path"])),
                report_bytes=raw_report_bytes,
                runtime_input_bytes=runtime_input_bytes,
            )
            from openadapt_flow.traversal import iter_workflow_steps

            from engine.flow_bridge import FlowBridge
            from engine.qualification import _flow_api, _load, _runtime_version
            from engine.qualification_capabilities import (
                collect_qualification_capabilities,
                sign_qualification_capability_observation,
            )
            from engine.qualification_keys import KEY_ID, qualification_signer

            precise_outcome = str(execution.get("outcome") or "unknown")
            if (
                precise_outcome
                not in {
                    "VERIFIED",
                    "COMPLETED_UNVERIFIED",
                    "HALTED",
                    "FAILED",
                    "ROLLED_BACK",
                }
                or FlowBridge.classify_outcome(0, raw_report) != precise_outcome
            ):
                raise ValueError(
                    "Qualification requires a complete, internally consistent "
                    "Flow execution-outcome report"
                )
            workflow = _load(
                bundle,
                key=self._qualification_bundle_key(workflow_id),
            )
            project = workflow.qualification
            if project is None:  # pragma: no cover - guarded before execution
                raise ValueError("Qualification project disappeared during case execution")
            actual_runtime_version = _runtime_version()
            capability_observation = collect_qualification_capabilities(
                raw_report,
                expected_target_kind=project.environment.target_kind,
                runtime_version=actual_runtime_version,
                report_sha256=hashlib.sha256(raw_report_bytes).hexdigest(),
                action_kinds={step.id: step.action.value for step in iter_workflow_steps(workflow)},
            )
            private_key, _public_key = qualification_signer()
            signed_capability_observation = sign_qualification_capability_observation(
                capability_observation,
                project_id=project.project_id,
                project_revision=project.revision,
                project_contract_sha256=project.contract_sha256(),
                workflow_contract_sha256=_flow_api()["workflow_contract_sha256"](workflow),
                environment_contract_sha256=project.environment.contract_sha256(),
                environment_digest=project.environment.environment_digest,
                case_id=case_id,
                run_id=run_id,
                attestation_key_id=KEY_ID,
                private_key=private_key,
            )
            evidence.append(
                retain_capability_observation(
                    bundle,
                    case_id=case_id,
                    run_id=run_id,
                    observation=signed_capability_observation.model_dump(mode="json"),
                )
            )
            outcome_map = {
                "VERIFIED": "verified",
                "COMPLETED_UNVERIFIED": "completed_unverified",
                "HALTED": "halted",
                "FAILED": "failed",
                "ROLLED_BACK": "rolled_back",
                "halt": "halted",
                "success": "completed_unverified",
                "unknown": "failed",
            }
            raw_outcome = precise_outcome
            observed_outcome = outcome_map.get(raw_outcome, "failed")
            missing_capabilities = sorted(
                set(project.environment.required_capabilities)
                - set(signed_capability_observation.observed_capabilities)
            )
            if missing_capabilities:
                result = inspect_bundle(
                    bundle,
                    workflow_id=workflow_id,
                    policy_source=policy,
                    bundle_key=self._qualification_bundle_key(workflow_id),
                )
                result.update(
                    {
                        "ok": False,
                        "error": (
                            "This exact run did not observe required runner capabilities: "
                            + ", ".join(missing_capabilities)
                        ),
                        "missing_capabilities": missing_capabilities,
                        "case_run": execution,
                    }
                )
                self.services.db.update_bundle(
                    workflow_id,
                    status="qualification_pending",
                )
                return result
            result = record_local_qualification_result(
                bundle,
                workflow_id=workflow_id,
                case_id=case_id,
                observed_outcome=observed_outcome,
                evidence=evidence,
                capability_observation=signed_capability_observation,
                detail_code=(
                    None if raw_outcome in {"VERIFIED", "HALTED"} else raw_outcome.lower()
                ),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            result["case_run"] = execution
            self.services.db.update_bundle(workflow_id, status="qualification_pending")
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def import_qualification_results(self, **params: Any) -> dict:
        """Import canonical signed results and re-verify every local evidence hash."""

        from engine.qualification import (
            DEFAULT_QUALIFICATION_POLICY,
            import_qualification_results,
        )

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            result = import_qualification_results(
                self._qualification_bundle_dir(workflow_id),
                workflow_id=workflow_id,
                signed_results_json=str(params.get("signed_results_json") or ""),
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(workflow_id, status="qualification_pending")
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def certify_qualification(self, **params: Any) -> dict:
        """Persist a pass/fail policy attempt into the resealed local bundle."""

        from engine.qualification import DEFAULT_QUALIFICATION_POLICY, certify_bundle

        workflow_id = str(params.get("workflow_id") or "")
        policy = str(params.get("policy") or DEFAULT_QUALIFICATION_POLICY)
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            result = certify_bundle(
                bundle,
                workflow_id=workflow_id,
                policy_source=policy,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            self.services.db.update_bundle(
                workflow_id,
                status=(
                    "certified" if result.get("certification_current") else "qualification_failed"
                ),
            )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def _new_bundle_version(self, workflow_id: str) -> tuple[str, int, Path, dict]:
        source = self.services.db.get_bundle(workflow_id)
        if source is None:
            raise ValueError(f"Unknown workflow {workflow_id}")
        version = int(source.get("version") or 1) + 1
        new_id = f"{workflow_id}-v{version}-{uuid.uuid4().hex[:6]}"
        destination = self.config.data_dir / "bundles" / new_id
        return new_id, version, destination, source

    def _register_bundle_version(
        self,
        *,
        bundle_id: str,
        version: int,
        destination: Path,
        source: dict,
        status: str,
    ) -> None:
        self.services.db.insert_bundle(
            bundle_id,
            str(destination),
            capture_id=source.get("capture_id"),
        )
        self.services.db.update_bundle(
            bundle_id,
            workflow_name=source.get("workflow_name") or "",
            version=version,
            steps=int(source.get("steps") or 0),
            schema_version=int(source.get("schema_version") or 2),
            status=status,
        )

    def version_qualification_workflow(self, **params: Any) -> dict:
        """Create an exact local working version without altering its predecessor."""

        import shutil

        from engine.bundle_keys import copy_bundle_key, delete_bundle_key
        from engine.qualification_lifecycle import copy_bundle_version

        workflow_id = str(params.get("workflow_id") or "")
        try:
            source_path = self._qualification_bundle_dir(workflow_id)
            new_id, version, destination, source = self._new_bundle_version(workflow_id)
            copy_bundle_version(source_path, destination)
            encrypted = (destination / "workflow.json.enc").is_file()
            if encrypted:
                copy_bundle_key(workflow_id, new_id)
            self._register_bundle_version(
                bundle_id=new_id,
                version=version,
                destination=destination,
                source=source,
                status=str(source.get("status") or "qualification_pending"),
            )
            return {"ok": True, "workflow_id": new_id, "version": version}
        except Exception as exc:
            if "destination" in locals():
                shutil.rmtree(destination, ignore_errors=True)
            if "new_id" in locals():
                delete_bundle_key(new_id)
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def seal_qualification_workflow(self, **params: Any) -> dict:
        """Create an encrypted version through Flow's atomic sealing command."""

        import shutil

        from engine.bundle_keys import delete_bundle_key, generate_bundle_key
        from engine.qualification import inspect_bundle, seal_qualification_bundle

        workflow_id = str(params.get("workflow_id") or "")
        try:
            source_path = self._qualification_bundle_dir(workflow_id)
            current = inspect_bundle(
                source_path,
                workflow_id=workflow_id,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            if current["graph"]["bundle"]["encrypted"]:
                raise ValueError("This workflow version is already sealed and encrypted")
            new_id, version, destination, source = self._new_bundle_version(workflow_id)
            key = generate_bundle_key(new_id)
            verified = seal_qualification_bundle(
                source_path,
                destination,
                workflow_id=new_id,
                destination_key=key,
            )
            self._register_bundle_version(
                bundle_id=new_id,
                version=version,
                destination=destination,
                source=source,
                status=(
                    "certified"
                    if verified.get("certification_current")
                    else "qualification_pending"
                ),
            )
            return {
                "ok": True,
                "workflow_id": new_id,
                "version": version,
                "certification_current": verified.get("certification_current", False),
            }
        except Exception as exc:
            if "destination" in locals():
                shutil.rmtree(destination, ignore_errors=True)
            if "new_id" in locals():
                delete_bundle_key(new_id)
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def export_qualification_workflow(self, **params: Any) -> dict:
        """Export the exact certified encrypted artifact to a deterministic archive."""

        import hashlib

        from engine.qualification import inspect_bundle
        from engine.qualification_lifecycle import export_certified_bundle

        workflow_id = str(params.get("workflow_id") or "")
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            qualified = inspect_bundle(
                bundle,
                workflow_id=workflow_id,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            if not qualified.get("capability_coverage", {}).get("satisfied"):
                raise ValueError(
                    "Every required runner capability must be observed in signed "
                    "current-revision case evidence before export"
                )
            if not qualified.get("certification_current"):
                raise ValueError("Certify this exact workflow version before export")
            if not qualified["graph"]["bundle"]["encrypted"]:
                raise ValueError("Seal and encrypt this workflow version before export")
            row = self.services.db.get_bundle(workflow_id) or {}
            exports = self.config.data_dir / "exports"
            staging = exports / f".{workflow_id}-{uuid.uuid4().hex}.zip"
            digest = export_certified_bundle(bundle, staging)
            destination = exports / (
                f"{workflow_id}-v{int(row.get('version') or 1)}-{digest[:12]}.zip"
            )
            if destination.exists():
                existing = hashlib.sha256(destination.read_bytes()).hexdigest()
                if existing != digest:
                    raise ValueError("An export with this artifact identity is inconsistent")
                staging.unlink()
            else:
                staging.replace(destination)
            return {
                "ok": True,
                "workflow_id": workflow_id,
                "path": str(destination),
                "sha256": digest,
            }
        except Exception as exc:
            if "staging" in locals():
                staging.unlink(missing_ok=True)
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    def deploy_qualification_workflow(self, **params: Any) -> dict:
        """Send an exact certified sealed artifact through Flow's governed push path."""

        from engine.auth.store import auth_header
        from engine.bundle_keys import bundle_key_environment
        from engine.qualification import inspect_bundle
        from engine.qualification_lifecycle import parse_flow_push

        workflow_id = str(params.get("workflow_id") or "")
        try:
            bundle = self._qualification_bundle_dir(workflow_id)
            qualified = inspect_bundle(
                bundle,
                workflow_id=workflow_id,
                bundle_key=self._qualification_bundle_key(workflow_id),
            )
            if not qualified.get("capability_coverage", {}).get("satisfied"):
                raise ValueError(
                    "Every required runner capability must be observed in signed "
                    "current-revision case evidence before deployment"
                )
            if not qualified.get("certification_current"):
                raise ValueError("Certify this exact workflow version before deployment")
            if not qualified["graph"]["bundle"]["encrypted"]:
                raise ValueError("Seal and encrypt this workflow version before deployment")
            if not self.services.flow_bridge.supports_command("push"):
                raise ValueError("The bundled Flow runtime does not support governed deployment")
            env = bundle_key_environment(workflow_id)
            authorization = auth_header().get("Authorization", "")
            if authorization.startswith("Bearer "):
                env["OPENADAPT_INGEST_TOKEN"] = authorization.removeprefix("Bearer ")
            pushed = self.services.flow_bridge.push(
                bundle,
                kind="bundle",
                host=self.config.hosted_host,
                env_overrides=env,
            )
            result = parse_flow_push(pushed.stdout, pushed.stderr, ok=pushed.ok)
            result["workflow_id"] = result.get("workflow_id") or workflow_id
            if result.get("deployed"):
                self.services.db.update_bundle(
                    workflow_id,
                    workflow_id=result["workflow_id"],
                    status="deployed",
                )
            return result
        except Exception as exc:
            return {"ok": False, "workflow_id": workflow_id, "error": str(exc)}

    # ------------------------------------------------------- sync / push

    def push_workflow(self, **params: Any) -> dict:
        """Push a compiled bundle to ``/api/ingest`` and mirror sync state."""
        from engine import hosted

        workflow_id = params.get("workflow_id")
        bundle = self._bundle_dir(workflow_id)
        if bundle is None:
            return {"ok": False, "error": f"Unknown workflow {workflow_id}", "workflow_id": ""}
        self._emit_sync("pushing")
        try:
            result = hosted.push(
                bundle,
                kind="bundle",
                host=self.config.hosted_host,
                db=self.services.db,
                bundle_id=workflow_id,
            )
        except Exception as exc:
            self._emit_sync("offline")
            return {"ok": False, "error": str(exc), "workflow_id": ""}
        self._emit_sync("synced" if result.get("success") else "offline")
        return {
            "ok": bool(result.get("success")),
            "workflow_id": result.get("workflow_id", ""),
            "dashboard_url": result.get("dashboard_url", ""),
            "error": result.get("error", ""),
        }

    def get_sync_state(self, **params: Any) -> dict:
        """Return the current :class:`SyncState`-shaped sync status."""
        state = "paused" if self._sync_paused else "synced"
        return {"state": state, "queued": 0}

    def pause_sync(self, **params: Any) -> dict:
        """Pause the upload/sync queue and emit ``sync_state``."""
        self._sync_paused = True
        return self._emit_sync("paused")

    def resume_sync(self, **params: Any) -> dict:
        """Resume the upload/sync queue and emit ``sync_state``."""
        self._sync_paused = False
        return self._emit_sync("synced")

    def _emit_sync(self, state: str) -> dict:
        payload = {"state": state, "queued": 0}
        self.emit("sync_state", payload)
        return payload

    def get_needs_attention(self, **params: Any) -> dict:
        """Return the local break count as a ``NeedsAttention`` dict + emit badge."""
        open_halts = self.services.db.count_open_halts()
        payload = {"count": open_halts, "open_halts": open_halts, "failed_runs": 0}
        self.emit("break_count", {"count": open_halts})
        return payload

    # ------------------------------------------------------- auth

    def login_browser(self, **params: Any) -> dict:
        """Log in via the browser-PKCE provider; return an ``AuthStatus``."""
        from engine import auth

        host = params.get("host") or self.config.hosted_host
        try:
            cred = auth.login(host=host, prefer="browser_pkce")
        except Exception as exc:
            return {"authenticated": False, "error": str(exc)}
        return self._auth_status(cred)

    def login_paste(self, **params: Any) -> dict:
        """Log in with a pasted ingest token; return an ``AuthStatus``."""
        from engine.auth.paste import PasteTokenProvider

        host = params.get("host") or self.config.hosted_host
        token = params.get("token")
        try:
            cred = PasteTokenProvider(host=host).login(token=token)
        except Exception as exc:
            return {"authenticated": False, "error": str(exc)}
        return self._auth_status(cred)

    def connect_uri(self, **params: Any) -> dict:
        """Handle only a validated ``openadapt://connect`` pairing URI."""
        from engine.auth.pairing import connect_uri

        uri = params.get("uri")
        if not isinstance(uri, str):
            raise ValueError("uri is required")
        result = connect_uri(uri)
        self.config.hosted_host = result["host"]
        self._persist_config_key("hosted_host", result["host"])
        self.emit(
            "pairing_state",
            {"status": "connected", "host": result["host"]},
        )
        return result

    def logout(self, **params: Any) -> dict:
        """Clear the active credential."""
        from engine.auth.store import active_host, clear_credential

        host = params.get("host") or active_host()
        if host:
            clear_credential(host)
        return {"authenticated": False}

    def get_auth_status(self, **params: Any) -> dict:
        """Return the current :class:`AuthStatus` from the active credential."""
        from engine.auth.store import active_credential

        cred = active_credential()
        if not cred:
            return {"authenticated": False}
        return self._auth_status(cred)

    def _auth_status(self, cred: Any) -> dict:
        return {
            "authenticated": True,
            "kind": cred.get("kind"),
            "host": cred.get("host"),
            "org_id": cred.get("org_id"),
        }

    # ------------------------------------------------------- config

    def get_config(self, **params: Any) -> dict:
        """Return the user-facing (non-secret) config the settings screen reads."""
        return {
            # ``host`` is the key the Settings screen reads; keep ``hosted_host``
            # too for any consumer keyed on the engine field name.
            "host": self.config.hosted_host,
            "hosted_host": self.config.hosted_host,
            "deployment_lane": self.config.deployment_lane,
            "phi_mode": self.config.phi_mode,
            "poll_interval_s": self.config.poll_interval_s,
        }

    def set_config(self, **params: Any) -> dict:
        """Update a non-secret hosted config key (persisted to ``config.toml``).

        Only whitelisted hosted keys are accepted; secrets never touch this file.
        """
        key = params.get("key")
        value = params.get("value")
        allowed = {"hosted_host", "deployment_lane", "phi_mode", "poll_interval_s"}
        if key not in allowed:
            return {"ok": False, "error": f"Unknown or non-settable key: {key}"}
        # Update the live config object so subsequent commands see the change.
        try:
            setattr(self.config, key, value)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._persist_config_key(key, value)
        return {"ok": True, **self.get_config()}

    def _persist_config_key(self, key: str, value: Any) -> None:
        """Write a single ``[hosted]`` key into ``~/.openadapt/config.toml``."""
        import tomllib

        from engine.config import _config_toml_path

        # Map EngineConfig field -> config.toml [hosted] key.
        toml_key = {"hosted_host": "host"}.get(key, key)
        path = _config_toml_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = tomllib.loads(path.read_text())
            except Exception:
                data = {}
        hosted = data.get("hosted")
        if not isinstance(hosted, dict):
            hosted = {}
        hosted[toml_key] = value
        data["hosted"] = hosted
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dumps_toml(data))
        except Exception as exc:
            logger.warning("Could not persist config key {k}: {e}", k=key, e=exc)

    # ------------------------------------------------------- effective policy

    def get_effective_policy(self, **params: Any) -> dict:
        """Return the org's effective policy, always fail-closed on safety.

        Resolves via :func:`engine.policy.resolve_effective_policy` (network ->
        cache -> fully-safe default). NEVER raises to the caller: any unexpected
        error still yields the fail-closed default so the settings screen and any
        run gate can rely on a fully-populated, safest-value ``safety`` block.

        The result carries ``is_admin``/``role`` (the cloud is the only source of
        admin status -- the engine has ``org_id`` but no role concept) so the
        frontend can decide which Tier-2/Tier-3 cards are read-only.
        """
        from engine import policy as policy_mod

        try:
            return policy_mod.resolve_effective_policy(self.config.hosted_host)
        except Exception as exc:  # defensive: resolver shouldn't raise, but never crash
            logger.warning("get_effective_policy fell back to fail-closed: {e}", e=exc)
            return policy_mod.harden_safety(
                {
                    "user": {},
                    "org": {},
                    "is_admin": False,
                    "role": "member",
                    "policy_version": None,
                    "source": "fail-closed-default",
                }
            )

    def refresh_policy(self, **params: Any) -> dict:
        """Force a network fetch of the effective policy, refreshing the cache.

        On network failure this still returns a usable, hardened policy (cache or
        the fail-closed default) via :func:`get_effective_policy` -- ``refresh``
        is a hint to skip any staleness, not a promise the network is up.
        """
        from engine import policy as policy_mod

        try:
            hardened = policy_mod.harden_safety(
                policy_mod.fetch_effective_policy(self.config.hosted_host)
            )
            hardened["source"] = "network"
            return hardened
        except policy_mod.PolicyFetchError as exc:
            logger.warning("refresh_policy fetch failed ({e}); resolving fail-closed", e=exc)
            return self.get_effective_policy()

    # ------------------------------------------------------- permissions

    def check_permissions(self, **params: Any) -> dict:
        """Return the prompt-free :class:`PermissionStatus`.

        macOS capture needs Screen Recording, Accessibility, and Input
        Monitoring. This check never requests access. Input Monitoring fails
        closed if its preflight API is unavailable; non-mac platforms do not
        use these macOS permissions and report all three as granted.
        """
        if sys.platform != "darwin":
            return {
                "screen_recording": True,
                "accessibility": True,
                "input_monitoring": True,
            }
        screen = _mac_preflight_screen()
        access = _mac_preflight_accessibility()
        input_monitoring = _mac_preflight_input_monitoring()
        return {
            "screen_recording": screen,
            "accessibility": access,
            "input_monitoring": input_monitoring,
        }

    def request_input_monitoring(self, **params: Any) -> dict:
        """Request Input Monitoring and return the refreshed permission state.

        This command is reserved for an explicit user action in the onboarding
        UI. The passive :meth:`check_permissions` command remains prompt-free.
        The post-request preflight is authoritative: a successful request call
        does not count as permission until macOS reports access as granted.
        """
        if sys.platform != "darwin":
            return self.check_permissions()
        if not _mac_preflight_input_monitoring():
            _mac_request_input_monitoring()
        return self.check_permissions()

    def get_capabilities(self, **params: Any) -> dict:
        """Return the machine-readable capability report for every surface.

        The report is produced by :mod:`engine.capabilities`, the same module
        that gates record/replay/run, so the UI's pills and the engine's
        refusal messages can never disagree. Detection is prompt-free and
        never raises.
        """
        from engine.capabilities import capability_report

        return capability_report()

    # ------------------------------------------------------- review / egress

    def scrub_capture(self, **params: Any) -> dict:
        """Scrub PII from a capture and advance its review state."""
        from engine.review import ReviewStatus, transition_status
        from engine.scrubber import Scrubber, ScrubbingUnavailableError, ScrubLevel

        capture_id = params.get("capture_id")
        capture = capture_id and self.services.db.get_capture(capture_id)
        if not capture:
            return {"ok": False, "error": f"Unknown capture {capture_id}"}
        capture_id = str(capture_id)
        level = params.get("level", "basic")
        scrubber = Scrubber(level=ScrubLevel(level))
        try:
            scrubbed = scrubber.scrub_capture(Path(capture["capture_path"]))
        except ScrubbingUnavailableError as exc:
            # The scrub could not run. The capture stays CAPTURED, which the
            # review state machine already blocks from every egress path.
            logger.warning("scrub_capture refused for {c}: {e}", c=capture_id, e=exc)
            self.services.audit.log(
                "scrub_refused", capture_id=capture_id, level=level, reason=str(exc)
            )
            return {"ok": False, "error": str(exc)}
        transition_status(
            capture_id,
            ReviewStatus.CAPTURED,
            ReviewStatus.SCRUBBED,
            db=self.services.db,
            audit=self.services.audit,
        )
        self.services.db.update_capture(capture_id, scrubbed_path=str(scrubbed))
        return {"ok": True, "scrubbed_path": str(scrubbed)}

    def approve_review(self, **params: Any) -> dict:
        """Approve a scrubbed capture for egress."""
        return self._review_transition(params, "SCRUBBED", "REVIEWED")

    def dismiss_review(self, **params: Any) -> dict:
        """Dismiss scrubbing (accept PII risk) for a capture."""
        return self._review_transition(params, "CAPTURED", "DISMISSED")

    def _review_transition(self, params: dict, frm: str, to: str) -> dict:
        from engine.review import ReviewStatus, transition_status

        capture_id = params.get("capture_id")
        if not capture_id:
            return {"ok": False, "error": "capture_id is required"}
        try:
            transition_status(
                capture_id,
                getattr(ReviewStatus, frm),
                getattr(ReviewStatus, to),
                db=self.services.db,
                audit=self.services.audit,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_pending_reviews(self, **params: Any) -> dict:
        """Return captures pending review."""
        from engine.review import get_pending_reviews

        return {"pending": get_pending_reviews(self.services.db)}

    # ------------------------------------------------------- runner lane

    def _runner_service(self) -> Any:
        """Lazily build the shared runner-loop service (EXPERIMENTAL lane)."""
        if self.services.runner is None:
            from engine.runner_loop import RunnerService

            self.services.runner = RunnerService(self.config, self.services, emit=self.emit)
        return self.services.runner

    def runner_status(self, **params: Any) -> dict:
        """Return the ``RunnerStatus``-shaped dict for the Runner screen."""
        return self._runner_service().status()

    def runner_enable(self, **params: Any) -> dict:
        """Enable the runner lane, start its loop, and persist the flag."""
        status = self._runner_service().enable()
        self._persist_config_key("runner_enabled", True)
        return status

    def runner_disable(self, **params: Any) -> dict:
        """Disable the runner lane, stop its loop, and persist the flag."""
        status = self._runner_service().disable()
        self._persist_config_key("runner_enabled", False)
        return status

    # ------------------------------------------- mobile decision portal

    def _portal_service(self) -> Any:
        """Lazily build the runner-local decision portal service."""
        if self.services.portal is None:
            from engine.portal.service import PortalService

            self.services.portal = PortalService(self.config)
        return self.services.portal

    def portal_status(self, **params: Any) -> dict:
        """Portal lifecycle state, ingress posture, and paired devices."""
        return self._portal_service().status()

    def portal_start(self, **params: Any) -> dict:
        """Start the portal, or fail closed with the exact misconfiguration."""
        from engine.portal.service import PortalError

        try:
            status = self._portal_service().start()
        except PortalError as exc:
            # Fail loud and stay stopped. The portal never falls back to a
            # broader bind address to make itself reachable.
            raise ValueError(str(exc)) from None
        self.emit("portal_state", status)
        return status

    def portal_stop(self, **params: Any) -> dict:
        """Stop the portal and the attended console it supervises."""
        status = self._portal_service().stop()
        self.emit("portal_state", status)
        return status

    def portal_create_pairing(self, **params: Any) -> dict:
        """Mint one single-use, five-minute QR pairing for a phone."""
        return self._portal_service().create_pairing()

    def portal_pairing_status(self, **params: Any) -> dict:
        """Poll one pairing so Desktop can show the scanned device's code."""
        return self._portal_service().pairing_status(params.get("pairing_id", ""))

    def portal_approve_pairing(self, **params: Any) -> dict:
        """Approve a scanned phone using the code that phone is displaying."""
        return self._portal_service().approve_pairing(
            params.get("pairing_id", ""), params.get("confirm_code", "")
        )

    def portal_cancel_pairing(self, **params: Any) -> dict:
        """Cancel a pairing and revoke any session it minted."""
        return self._portal_service().cancel_pairing(params.get("pairing_id", ""))

    def portal_devices(self, **params: Any) -> dict:
        """List live paired phones (never a token or its digest)."""
        return {"devices": self._portal_service().devices()}

    def portal_revoke_device(self, **params: Any) -> dict:
        """Revoke one paired phone immediately."""
        return self._portal_service().revoke_device(params.get("session_id", ""))

    def portal_notification(self, **params: Any) -> dict:
        """Emit the generic operating-system notification payload.

        The payload is asserted generic before it is emitted, so a regression
        that widened it would fail here rather than on a lock screen.
        """
        from engine.portal.notifications import assert_generic_notification

        # Asserted here as well as in the portal service: this is the last
        # point before the payload crosses into the shell's notification
        # plugin, so a regression fails loudly instead of on a lock screen.
        payload = assert_generic_notification(self._portal_service().notification())
        self.emit("attention_notification", payload)
        return payload

    # ------------------------------------------------------- tray UI nav

    def open_workflow_library(self, **params: Any) -> dict:
        """Relay a tray request to open the desktop workflow-library window."""
        self.emit("open_window", {"view": "workflow_library"})
        return {"ok": True}

    def open_teach(self, **params: Any) -> dict:
        """Relay a tray request to open the desktop local-teach view."""
        self.emit("open_window", {"view": "teach", "workflow_id": params.get("workflow_id")})
        return {"ok": True}


def _dumps_toml(data: dict) -> str:
    """Serialize a shallow ``{table: {k: v}}`` dict to TOML (stdlib has no writer)."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for table, body in data.items():
        if not isinstance(body, dict):
            continue
        lines.append(f"[{table}]")
        for key, value in body.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mac_preflight_screen() -> bool:  # pragma: no cover - platform-specific
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def _mac_preflight_accessibility() -> bool:  # pragma: no cover - platform-specific
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return True


def _mac_preflight_input_monitoring() -> bool:  # pragma: no cover - platform-specific
    """Check Input Monitoring without presenting the system consent prompt."""
    try:
        from Quartz import CGPreflightListenEventAccess

        return bool(CGPreflightListenEventAccess())
    except Exception as exc:
        logger.warning("Input Monitoring preflight unavailable: {e}", e=exc)
        return False


def _mac_request_input_monitoring() -> bool:  # pragma: no cover - platform-specific
    """Request Input Monitoring after an explicit user action."""
    try:
        from Quartz import CGRequestListenEventAccess

        return bool(CGRequestListenEventAccess())
    except Exception as exc:
        logger.warning("Input Monitoring request unavailable: {e}", e=exc)
        return False
