"""Authenticated, loopback-only FastAPI operator console.

``create_app`` builds the app over three on-disk roots -- bundles, runs, and
(optionally) skill libraries -- and a single ``allow_actions`` switch. Every
API and screenshot request requires an unguessable bearer capability. Browser
mutations additionally require same-origin JSON and a session-bound CSRF
token. With ``allow_actions=False`` (the default), mutating endpoints refuse
with a browser-safe placeholder command the operator can copy. ``attend=True``
makes the secure Needs Attention queue the first view; it does not weaken or
remove the normal console. Attended mutations additionally require an exact
engine-issued pause capability and a deployment-bound action service.

Browser DTOs use opaque ids and explicit projections; protected workflow
labels, parameters, identity evidence, local paths, and raw reports do not
cross the API boundary. Run artifacts are limited to PNGs explicitly
referenced by the protected report and resolved without following symlinks.
"""

from __future__ import annotations

import getpass
import hmac
import os
import secrets
import shlex
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response

from openadapt_flow import __version__
from openadapt_flow.console import actions as actions_mod
from openadapt_flow.console import attention as attention_mod
from openadapt_flow.console import data
from openadapt_flow.runtime.durable.approval import ResumeRefused
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    execute_attended_action,
)
from openadapt_flow.runtime.durable.attended_service import AttendedActionService

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_BODY_BYTES = 16 * 1024
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _local_operator_identity() -> str:
    """Derive mutation attribution from the server's OS account.

    On POSIX, use the effective uid instead of caller-controlled LOGNAME/USER
    environment variables.  Windows has no pwd database, so getpass is the
    standard local-account fallback.
    """
    if os.name != "nt":
        try:
            import pwd

            return pwd.getpwuid(os.geteuid()).pw_name
        except (ImportError, KeyError, OSError):
            pass
    return getpass.getuser()


def _validated_root(value: Path | str, *, label: str) -> Path:
    """Resolve a configured root once, refusing symlink path components."""
    lexical = Path(value).expanduser().absolute()
    for component in (lexical, *lexical.parents):
        if component.is_symlink():
            raise ValueError(f"{label} root must not traverse a symlink")
    return lexical.resolve()


def _host_authority(raw: str) -> Optional[str]:
    """Canonical loopback Host authority, or None for anything else."""
    if not raw or any(c in raw for c in ("/", "\\", "@", "#", "?")):
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return f"{host}:{port}" if port is not None else host


def _valid_origin(raw: str, authority: str) -> bool:
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme.lower() != "http" or parsed.query or parsed.fragment:
        return False
    if parsed.path not in ("", "/") or parsed.username or parsed.password:
        return False
    return _host_authority(parsed.netloc) == authority


def _bearer_token(request: Request) -> str:
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _resolve_bundle(root: Path, bundle_id: str) -> Path:
    path = data.resolve_scanned_dir(root, bundle_id, data._is_bundle_dir)
    if path is None:
        raise HTTPException(status_code=404, detail="no such bundle")
    return path


def _resolve_run(root: Path, run_id: str) -> Path:
    path = data.resolve_scanned_dir(root, run_id, data._is_run_dir)
    if path is None:
        raise HTTPException(status_code=404, detail="no such run")
    return path


def _guess_bundle_for_run(
    bundles_root: Path, runs_root: Path, run_dir: Path
) -> Optional[str]:
    """Best-effort bundle path for a run: the durable manifest's recorded
    ``bundle_dir`` first, else a content-digest match over the bundle root."""
    manifest = data._read_json_opt(
        run_dir / "checkpoints" / "_manifest.json", root=run_dir
    )
    if manifest and manifest.get("bundle_dir"):
        try:
            candidate = Path(str(manifest["bundle_dir"])).resolve(strict=True)
        except OSError:
            candidate = None
        if candidate is not None:
            for scanned in data._scan(bundles_root, data._is_bundle_dir):
                if scanned.resolve(strict=True) == candidate:
                    return str(candidate)
    report, _ = data._load_report(run_dir)
    if report is None or not report.bundle_content_digest:
        return None
    for summary in data.list_bundles(bundles_root):
        if summary.content_digest == report.bundle_content_digest:
            matched = data.resolve_scanned_dir(
                bundles_root, summary.id, data._is_bundle_dir
            )
            return str(matched) if matched else None
    return None


def _find_library(
    bundles_root: Path, skills_root: Optional[Path], library_id: str
) -> Path:
    for lib in data.find_skill_libraries(bundles_root, skills_root):
        if data.skill_library_id(lib) == library_id:
            return lib
    raise HTTPException(status_code=404, detail="no such skill library")


def _refuse_or_none(allow_actions: bool, command: str) -> None:
    """Read-only gate: return only a path-free command template."""
    if not allow_actions:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "console is read-only (start with --allow-actions "
                "to enable governed actions)",
                "command": command,
            },
        )


def _execution_response(result: actions_mod.ExecutionResult) -> JSONResponse:
    """Return outcome metadata only; CLI output can echo protected values."""
    return JSONResponse(
        {
            "action_id": result.action_id,
            "returncode": result.returncode,
            "stdout": "action completed" if result.returncode == 0 else "",
            "stderr": (
                "action failed; run the displayed command locally for details"
                if result.returncode != 0
                else ""
            ),
        }
    )


def _public_action_spec(spec: actions_mod.ActionSpec) -> dict[str, Any]:
    """Browser-safe action metadata; never serialize a real filesystem argv."""
    commands = {
        "teach": shlex.join(
            [
                "openadapt-flow",
                "teach",
                "<selected-run-dir>",
                "--fix",
                "<fix-recording-or-spec.json>",
                "--bundle",
                "<bundle-dir>",
                "--out",
                "<updated-bundle-dir>",
            ]
        ),
        "approve": shlex.join(
            [
                "openadapt-flow",
                "approve",
                "<selected-run-dir>",
                "--approver",
                "<local-os-account>",
            ]
        ),
        "resume": shlex.join(
            [
                "openadapt-flow",
                "resume",
                "<selected-run-dir>",
                "--require-approval",
                "--config",
                "<deployment.yaml>",
                "--params-file",
                "<params.json>",
            ]
        ),
        "certify": shlex.join(
            [
                "openadapt-flow",
                "certify",
                "<selected-bundle-dir>",
                "--policy",
                "<built-in-policy>",
            ]
        ),
        "run": shlex.join(
            [
                "openadapt-flow",
                "run",
                "<selected-bundle-dir>",
                "--config",
                "<deployment.yaml>",
            ]
        ),
    }
    public = spec.model_dump()
    public["command"] = commands.get(
        spec.id, "server-bound governed action; no local path exported"
    )
    return public


def _public_action_command(spec: actions_mod.ActionSpec) -> str:
    return str(_public_action_spec(spec)["command"])


def create_app(
    bundles_root: Path | str,
    runs_root: Path | str,
    skills_root: Path | str | None = None,
    *,
    allow_actions: bool = False,
    attend: bool = False,
    access_token: Optional[str] = None,
    csrf_token: Optional[str] = None,
    attended_service: Optional[AttendedActionService] = None,
) -> FastAPI:
    bundles = _validated_root(bundles_root, label="bundles")
    runs = _validated_root(runs_root, label="runs")
    skills = _validated_root(skills_root, label="skills") if skills_root else None
    token = access_token or secrets.token_urlsafe(32)
    csrf = csrf_token or secrets.token_urlsafe(32)
    operator = _local_operator_identity().strip()
    if len(token) < 32 or len(csrf) < 32:
        raise ValueError(
            "console access and CSRF tokens must each be at least 32 chars"
        )
    if allow_actions and not operator:
        raise ValueError("actions require a server-derived local operator identity")

    app = FastAPI(
        title="OpenAdapt Flow operator console",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Test harnesses and an embedding caller can retrieve the generated
    # capability in-process.  It is never returned by an unauthenticated route.
    app.state.console_access_token = token
    app.state.console_csrf_token = csrf
    app.state.console_operator_identity = operator
    app.state.console_attend = attend

    @app.exception_handler(RequestValidationError)
    async def redacted_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default response includes the rejected input value. Console
        # payloads can contain operator notes or deployment paths, so retain
        # details only in protected local diagnostics and return a fixed DTO.
        return JSONResponse(
            {"detail": "request did not match the console action schema"},
            status_code=422,
        )

    @app.middleware("http")
    async def console_security_boundary(request: Request, call_next):
        authority = _host_authority(request.headers.get("host", ""))
        if authority is None:
            response = JSONResponse(
                {"detail": "invalid console Host header"}, status_code=400
            )
        else:
            origin = request.headers.get("origin")
            if origin is not None and not _valid_origin(origin, authority):
                response = JSONResponse(
                    {"detail": "cross-origin console request refused"},
                    status_code=403,
                )
            elif request.url.path.startswith("/api/") and not hmac.compare_digest(
                _bearer_token(request), token
            ):
                response = JSONResponse(
                    {"detail": "console bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                if origin is None:
                    response = JSONResponse(
                        {"detail": "mutation requires a same-origin Origin header"},
                        status_code=403,
                    )
                elif (
                    not request.headers.get("content-type", "")
                    .lower()
                    .startswith("application/json")
                ):
                    response = JSONResponse(
                        {"detail": "mutations require application/json"},
                        status_code=415,
                    )
                elif (
                    request.headers.get("content-length", "").isdigit()
                    and int(request.headers["content-length"]) > _MAX_BODY_BYTES
                ):
                    response = JSONResponse(
                        {"detail": "request body is too large"}, status_code=413
                    )
                elif not hmac.compare_digest(
                    request.headers.get("x-openadapt-csrf", ""), csrf
                ):
                    response = JSONResponse(
                        {"detail": "valid CSRF token required"}, status_code=403
                    )
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    # -- meta ---------------------------------------------------------------

    @app.get("/api/session")
    def session() -> dict[str, Any]:
        return {
            "version": __version__,
            "read_only": not allow_actions,
            "attend": attend,
            "attended_decisions_ready": bool(attend and allow_actions),
            "attended_actions_ready": bool(
                attend and allow_actions and attended_service is not None
            ),
            "csrf_token": csrf,
        }

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "read_only": not allow_actions,
            "attend": attend,
            "attended_decisions_ready": bool(attend and allow_actions),
            "attended_actions_ready": bool(
                attend and allow_actions and attended_service is not None
            ),
        }

    # -- workflows ----------------------------------------------------------

    @app.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        last = data.latest_runs_by_digest(runs)
        out = []
        for summary in data.list_bundles(bundles):
            d = summary.model_dump()
            d["last_run"] = (
                last.get(summary.content_digest) if summary.content_digest else None
            )
            out.append(d)
        return out

    @app.get("/api/workflows/{bundle_id:path}/diff/{other_id:path}")
    def workflow_diff(bundle_id: str, other_id: str) -> dict[str, Any]:
        a = _resolve_bundle(bundles, bundle_id)
        b = _resolve_bundle(bundles, other_id)
        return data.bundle_diff(bundles, a, b)

    @app.get("/api/workflows/{bundle_id:path}/actions")
    def workflow_actions(bundle_id: str) -> list[dict[str, Any]]:
        path = _resolve_bundle(bundles, bundle_id)
        summary = data.bundle_summary(bundles, path)
        return [
            _public_action_spec(a)
            for a in actions_mod.actions_for_bundle(path, summary.policy_name)
        ]

    @app.get("/api/workflows/{bundle_id:path}")
    def workflow_detail(
        bundle_id: str, policy: Optional[str] = Query(default=None)
    ) -> dict[str, Any]:
        if policy is not None and policy not in data.builtin_policy_names():
            raise HTTPException(
                status_code=400,
                detail="the console evaluates built-in policies only; use the CLI "
                "for a reviewed custom policy path",
            )
        path = _resolve_bundle(bundles, bundle_id)
        return data.bundle_detail(bundles, path, policy_override=policy)

    # -- runs ---------------------------------------------------------------

    @app.get("/api/runs")
    def runs_list() -> list[dict[str, Any]]:
        return [r.model_dump() for r in data.list_runs(runs)]

    @app.get("/api/runs/{run_id:path}/artifact")
    def run_artifact(
        run_id: str, artifact_id: str = Query(..., alias="id")
    ) -> Response:
        run_dir = _resolve_run(runs, run_id)
        artifact = data.safe_artifact(run_dir, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="no such artifact")
        try:
            content = artifact.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="no such artifact") from exc
        # Returning bytes avoids exporting the protected filename in response
        # metadata and narrows the path-validation/reopen interval.
        return Response(content, media_type="image/png")

    @app.get("/api/runs/{run_id:path}/actions")
    def run_actions(run_id: str) -> list[dict[str, Any]]:
        run_dir = _resolve_run(runs, run_id)
        summary = data.run_summary(runs, run_dir)
        bundle = _guess_bundle_for_run(bundles, runs, run_dir)
        return [
            _public_action_spec(a)
            for a in actions_mod.actions_for_run(
                run_dir,
                halted=summary.halted,
                paused=summary.paused,
                bundle_dir=bundle,
                operator_identity=operator,
            )
        ]

    @app.get("/api/runs/{run_id:path}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run_dir = _resolve_run(runs, run_id)
        return data.run_detail(runs, run_dir)

    # -- needs attention ----------------------------------------------------

    @app.get("/api/attention")
    def attention_list() -> list[dict[str, Any]]:
        return [item.model_dump() for item in attention_mod.list_attention(runs)]

    @app.get("/api/attention/notification")
    def attention_notification() -> dict[str, Any]:
        """PHI-free payload for a future desktop/tray OS notification."""
        return attention_mod.notification(
            attention_mod.list_attention(runs)
        ).model_dump()

    @app.get("/api/attention/{run_id:path}")
    def attention_detail(run_id: str) -> dict[str, Any]:
        run_dir = _resolve_run(runs, run_id)
        item = attention_mod.attention_item(runs, run_dir)
        if item is None:
            raise HTTPException(status_code=404, detail="no open attention item")
        return item.model_dump()

    @app.post("/api/attention/{run_id:path}/actions/{action_id}")
    def execute_attention_action(
        run_id: str,
        action_id: str,
        payload: AttendedActionRequest,
    ) -> dict[str, Any]:
        if not attend:
            raise HTTPException(status_code=404, detail="no such action")
        _refuse_or_none(
            allow_actions,
            "start `openadapt-flow console --attend --allow-actions` with "
            "a qualified deployment service",
        )
        if action_id != payload.action:
            raise HTTPException(
                status_code=400,
                detail="action path and capability request do not match",
            )
        run_dir = _resolve_run(runs, run_id)
        try:
            decision = (
                attended_service.execute(
                    run_dir,
                    payload,
                    operator=operator,
                )
                if attended_service is not None
                else execute_attended_action(
                    run_dir,
                    payload,
                    operator=operator,
                )
            )
        except (AttendedActionRefused, ResumeRefused) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The durable audit keeps operator, request, capability, and transition
        # bindings. The browser needs only the decision outcome; never reflect
        # those protected identifiers back across the presentation boundary.
        return {
            "action": decision.action,
            "status": decision.status,
            "message": decision.message,
            "report_success": decision.report_success,
        }

    # -- skills -------------------------------------------------------------

    @app.get("/api/skills")
    def skills_list() -> list[dict[str, Any]]:
        return [
            data.skill_library_view(lib)
            for lib in data.find_skill_libraries(bundles, skills)
        ]

    # -- governed actions ---------------------------------------------------

    @app.post("/api/runs/{run_id:path}/actions/{action_id}")
    def execute_run_action(
        run_id: str, action_id: str, payload: dict[str, Any] | None = None
    ) -> JSONResponse:
        run_dir = _resolve_run(runs, run_id)
        summary = data.run_summary(runs, run_dir)
        bundle = _guess_bundle_for_run(bundles, runs, run_dir)
        specs = {
            a.id: a
            for a in actions_mod.actions_for_run(
                run_dir,
                halted=summary.halted,
                paused=summary.paused,
                bundle_dir=bundle,
                operator_identity=operator,
            )
        }
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail="action not available for this run",
            )
        if not spec.executable:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "action needs deployment-bound operator input; "
                    "copy the command instead",
                    "command": _public_action_command(spec),
                },
            )
        _refuse_or_none(allow_actions, _public_action_command(spec))
        kwargs = actions_mod.collect_execution_kwargs(payload or {})
        try:
            result = actions_mod.execute_run_action(
                action_id,
                run_dir,
                operator_identity=operator,
                resolution=kwargs.get("resolution"),
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail="action input was refused"
            ) from e
        return _execution_response(result)

    @app.post("/api/workflows/{bundle_id:path}/actions/{action_id}")
    def execute_bundle_action(
        bundle_id: str, action_id: str, payload: dict[str, Any] | None = None
    ) -> JSONResponse:
        path = _resolve_bundle(bundles, bundle_id)
        summary = data.bundle_summary(bundles, path)
        specs = {
            a.id: a for a in actions_mod.actions_for_bundle(path, summary.policy_name)
        }
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail="action not available for this bundle",
            )
        if not spec.executable:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "action must run in the deployment "
                    "environment; copy the command instead",
                    "command": _public_action_command(spec),
                },
            )
        if spec.mutating:
            _refuse_or_none(allow_actions, _public_action_command(spec))
        kwargs = actions_mod.collect_execution_kwargs(payload or {})
        requested_policy = kwargs.get("policy") or summary.policy_name
        if (
            requested_policy is not None
            and requested_policy not in data.builtin_policy_names()
        ):
            raise HTTPException(
                status_code=400,
                detail="the console executes built-in policies only; copy the "
                "reviewed custom-policy command to a terminal",
            )
        try:
            result = actions_mod.execute_bundle_action(
                action_id, path, policy=requested_policy
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail="action input was refused"
            ) from e
        return _execution_response(result)

    @app.post("/api/skills/{skill_id}/actions/{action_id}")
    def execute_skill_action(
        skill_id: str, action_id: str, payload: dict[str, Any]
    ) -> JSONResponse:
        library_id = payload.get("library")
        version = payload.get("version")
        if not isinstance(library_id, str) or not isinstance(version, int):
            raise HTTPException(
                status_code=400,
                detail="payload must carry 'library' (opaque id) and 'version' (int)",
            )
        lib = _find_library(bundles, skills, library_id)
        internal_skill_id = data.resolve_skill_id(lib, skill_id)
        if internal_skill_id is None:
            raise HTTPException(status_code=404, detail="no such skill")
        specs = {
            a.id: a
            for a in actions_mod.actions_for_skill(lib, internal_skill_id, version)
        }
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="no such action")
        _refuse_or_none(
            allow_actions,
            "server-bound governed skill action; no local path exported",
        )
        kwargs = actions_mod.collect_execution_kwargs(payload)
        try:
            result = actions_mod.execute_skill_action(
                action_id,
                lib,
                internal_skill_id,
                version,
                reason=kwargs.get("reason"),
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail="action input was refused"
            ) from e
        return _execution_response(result)

    # -- UI -----------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/static/console.css")
    def console_css() -> FileResponse:
        return FileResponse(_STATIC_DIR / "console.css", media_type="text/css")

    @app.get("/static/console.js")
    def console_js() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "console.js", media_type="application/javascript"
        )

    return app
