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

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from engine.config import EngineConfig

EmitFn = Callable[[str, dict], None]


def _noop_emit(event: str, data: dict) -> None:
    """Default event sink -- drops events when no emitter is wired."""


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
            # the loop: compile -> replay/run -> teach
            "compile_recording": self.compile_recording,
            "replay_workflow": self.replay_workflow,
            "run_workflow": self.run_workflow,
            "get_run_report": self.get_run_report,
            "teach_fix": self.teach_fix,
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
        if controller.is_recording:
            return self._status_dict(controller)
        if sys.platform == "darwin" and not _mac_preflight_input_monitoring():
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
        task = params.get("purpose") or params.get("task") or params.get("name") or ""
        capture_id = controller.start(task_description=str(task))
        self.services.audit.log("recording_started", capture_id=capture_id)
        self.emit("recording_started", {"capture_id": capture_id})
        self.emit("status_update", self._status_dict(controller))
        return {"capture_id": capture_id, "recording": True}

    def stop_recording(self, **params: Any) -> dict:
        """Stop the active recording and emit ``recording_stopped``."""
        controller = self.services.controller
        if not controller.is_recording:
            self.emit("recording_error", {"error": "No recording is active"})
            return {"capture_id": None, "recording": False}
        metadata = controller.stop()
        self.emit("recording_stopped", metadata)
        self.emit("status_update", self._status_dict(controller))
        return {"capture_id": metadata.get("id"), **metadata}

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
            states = {s.get("state") for s in (rep.get("steps") or [])}
            if rep.get("halt") or "halted" in states:
                last_run_state = "halted"
            elif "failed" in states:
                last_run_state = "failed"
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
        capture = self.services.db.get_capture(capture_id)
        capture_dir = capture and (capture.get("capture_path") or capture.get("capture_dir"))
        if not capture_dir:
            return {"ok": False, "error": f"Unknown capture {capture_id}", "workflow_id": ""}
        self.emit("compile_progress", {"capture_id": capture_id, "state": "compiling"})
        compiled = self.services.controller.compile_capture(capture_id, Path(capture_dir))
        if not compiled:
            self.emit("compile_progress", {"capture_id": capture_id, "state": "failed"})
            return {"ok": False, "error": "Compile failed (see logs)", "workflow_id": ""}
        self.emit(
            "compile_progress",
            {"capture_id": capture_id, "state": "compiled", "bundle_id": compiled["bundle_id"]},
        )
        return {
            "ok": True,
            "workflow_id": compiled["bundle_id"],
            "bundle_path": compiled["bundle_path"],
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
            return {"ok": False, "error": f"Unknown workflow {workflow_id}"}
        run_id = uuid.uuid4().hex[:8]
        run_dir = self.config.data_dir / "runs" / f"{'run' if run else 'replay'}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.emit("replay_progress", {"workflow_id": workflow_id, "state": "running"})
        try:
            if run:
                config_path = self.config.data_dir / "deployment.json"
                result = self.services.flow_bridge.run(bundle, config_path, out_dir=run_dir)
            else:
                ensure_browser = getattr(self.services.flow_bridge, "ensure_browser_runtime", None)
                if ensure_browser is not None:
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
                result = self.services.flow_bridge.replay(bundle, out_dir=run_dir)
        except Exception as exc:
            self.emit("replay_progress", {"workflow_id": workflow_id, "state": "error"})
            return {"ok": False, "error": str(exc)}
        for line in (result.stdout or "").splitlines():
            self.emit("log_line", {"line": line})
        try:
            self.services.db.insert_run(run_id, str(run_dir), bundle_id=workflow_id)
        except Exception:
            pass
        report = self._run_report(run_dir, workflow_id, run_id)
        self.emit(
            "replay_progress",
            {"workflow_id": workflow_id, "state": "halted" if report.get("halt") else "done"},
        )
        return report

    def get_run_report(self, **params: Any) -> dict | None:
        """Return the latest ``RunReport`` for a workflow, or None if none."""
        workflow_id = params.get("workflow_id")
        runs = [r for r in self.services.db.list_runs(limit=100)
                if r.get("bundle_id") == workflow_id]
        if not runs:
            return None
        run = runs[0]
        run_dir = run.get("run_path")
        if not run_dir:
            return None
        return self._run_report(Path(run_dir), workflow_id, run.get("run_id", ""))

    def _run_report(self, run_dir: Path, workflow_id: str | None, run_id: str) -> dict:
        from engine.flow_bridge import FlowBridge

        report = FlowBridge.read_report(run_dir)
        halt = FlowBridge.read_halt(run_dir)
        halt_state = (halt or {}).get("state_id") if isinstance(halt, dict) else None

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
        if isinstance(halt, dict) and halt:
            rung = None
            for r in results or []:
                if r.get("step_id") == halt_state:
                    rung = (r.get("resolution") or {}).get("rung")
            halt_block = {
                "step_index": self._step_index(halt.get("state_id") or halt.get("step_index")),
                "step_intent": halt.get("intent") or halt.get("step_intent") or "",
                "reason": halt.get("reason", ""),
                "resolver_rung": halt.get("resolver_rung") or rung,
            }

        total_steps = (
            self._workflow_step_count(workflow_id)
            or report.get("total_steps")
            or len(steps)
        )
        total_ms = report.get("total_ms")
        metrics = report.get("metrics") or {}
        duration_s = metrics.get("duration_s")
        if duration_s is None and isinstance(total_ms, (int, float)):
            duration_s = round(total_ms / 1000.0, 1)
        cost = metrics.get("cost_usd")
        if cost is None:
            cost = report.get("est_model_cost_usd")

        return {
            "ok": True,
            "run_id": report.get("run_id") or run_id,
            "workflow_id": workflow_id or report.get("workflow_id") or "",
            "workflow_name": report.get("workflow_name", ""),
            "total_steps": total_steps,
            "steps": steps,
            "halt": halt_block,
            "metrics": {"duration_s": duration_s, "cost_usd": cost},
        }

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
        run = next((r for r in self.services.db.list_runs(limit=100)
                    if r.get("bundle_id") == workflow_id), None)
        if run is None or not run.get("run_path"):
            return {"promoted": False, "message": "No halted run to teach against"}
        out_dir = self.config.data_dir / "bundles" / f"{workflow_id}_taught_{uuid.uuid4().hex[:6]}"
        try:
            result = self.services.flow_bridge.teach(
                Path(run["run_path"]), bundle, out_dir
            )
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

    # ------------------------------------------------------- review / egress

    def scrub_capture(self, **params: Any) -> dict:
        """Scrub PII from a capture and advance its review state."""
        from engine.review import ReviewStatus, transition_status
        from engine.scrubber import Scrubber, ScrubLevel

        capture_id = params.get("capture_id")
        capture = capture_id and self.services.db.get_capture(capture_id)
        if not capture:
            return {"ok": False, "error": f"Unknown capture {capture_id}"}
        capture_id = str(capture_id)
        level = params.get("level", "basic")
        scrubber = Scrubber(level=ScrubLevel(level))
        scrubbed = scrubber.scrub_capture(Path(capture["capture_path"]))
        transition_status(
            capture_id, ReviewStatus.CAPTURED, ReviewStatus.SCRUBBED,
            db=self.services.db, audit=self.services.audit,
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
                capture_id, getattr(ReviewStatus, frm), getattr(ReviewStatus, to),
                db=self.services.db, audit=self.services.audit,
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

            self.services.runner = RunnerService(
                self.config, self.services, emit=self.emit
            )
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
