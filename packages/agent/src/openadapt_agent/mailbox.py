"""Outbound authoring mailbox client for hosted ChatGPT.com / Claude.ai.

Prefers Desktop ``engine.authoring_runner.AuthoringMailboxTransport`` when
importable. Otherwise uses this copy (stdlib urllib, outbound POST only).
This module does not open a listener, tunnel loopback, or start the
in-guest Windows agent. Overlay chrome stays Desktop-only.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional, TextIO

from openadapt_agent.authoring import (
    AuthoringBridge,
    AuthoringError,
    CoachOnlySession,
    open_authoring_session,
    pin_local_backend,
    project_observe,
)
from openadapt_agent.runner_bind import (
    AUTHORING_ORIGIN,
    RunnerBindError,
    parse_connect_target,
    valid_bind_token,
    valid_lease_secret,
    valid_pack_id,
)

API_TIMEOUT_S = 10.0
DEFAULT_LEASE_S = 900
POLL_WAIT_S = 0
LOCAL_POLL_SLEEP_S = 1.0
COMMAND_ENVELOPE_SCHEMA = "openadapt.authoring.command/v1"
CLIENT_DISPLAYS = frozenset({"ChatGPT", "Claude"})
PAUSE_PROMPT = "Sign in in the app, then press Enter"
BOUND_SENTENCE = (
    "OpenAdapt is installed on this computer, so an agent can drive only through OpenAdapt."
)
ENQUEUE_REQUIRING_ALLOW = frozenset(
    {
        "observe",
        "click",
        "start_record",
        "pause_for_input",
        "stop_record",
        "compile",
        "set_coach",
        "get_coach",
        "halt",
    }
)
MAILBOX_ACTIONS = frozenset({"claim", "poll", "callback", "allow"})
COACH_ONLY_BACKENDS = frozenset({"windows", "rdp", "citrix"})
UNIQUE_WINDOW_BACKENDS = frozenset({"macos", "linux"})
_COMMAND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "value",
        "text",
        "title",
        "screenshot",
        "png",
        "ocr",
        "backend_pixels",
        "pixels",
        "events",
        "window_title",
        "image",
        "raw",
        "leaseSecret",
        "lease_secret",
        "bind",
    }
)

__all__ = [
    "AUTHORING_ORIGIN",
    "BOUND_SENTENCE",
    "DEFAULT_LEASE_S",
    "PAUSE_PROMPT",
    "POLL_WAIT_S",
    "MailboxClient",
    "MailboxError",
    "MailboxTransport",
    "connect_mailbox",
    "open_mailbox_transport",
    "parse_connect_target",
    "require_empty_cookies",
]


class MailboxError(RuntimeError):
    """A safe, user-facing mailbox failure with no secret-bearing text."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise MailboxError("The authoring request was redirected.")

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _sanitize_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_result(child)
            for key, child in value.items()
            if key not in FORBIDDEN_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    return value


def _client_display(value: object) -> str:
    if value in CLIENT_DISPLAYS:
        return str(value)
    return "ChatGPT"


def require_empty_cookies(browser: Any) -> None:
    """Refuse a Playwright session that already has cookies."""

    cookies_fn = getattr(browser, "cookies", None)
    if cookies_fn is None:
        context = getattr(browser, "context", None)
        cookies_fn = getattr(context, "cookies", None)
    if not callable(cookies_fn):
        return
    cookies = cookies_fn()
    if cookies:
        raise MailboxError("Playwright Chromium did not start with empty cookies.")


def _default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


class MailboxTransport:
    """Outbound HTTPS bind/poll/callback. Wait is always 0."""

    def __init__(
        self,
        *,
        origin: str = AUTHORING_ORIGIN,
        post: Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], Any]]
        | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if origin != AUTHORING_ORIGIN:
            raise MailboxError("The authoring origin is not pinned.")
        self.origin = origin
        self._post_impl = post
        self._opener = opener or _default_opener()

    def _path(self, pack_id: str, action: str) -> str:
        if not valid_pack_id(pack_id) or action not in MAILBOX_ACTIONS:
            raise MailboxError("The authoring mailbox path is invalid.")
        return f"/j/{urllib.parse.quote(pack_id, safe='._-')}/runner/{action}"

    def _urllib_post(
        self, path: str, body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, str], Any]:
        url = self.origin + path
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=API_TIMEOUT_S) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() if exc.fp is not None else b""
            resp_headers = (
                {k.lower(): v for k, v in exc.headers.items()} if exc.headers is not None else {}
            )
            if status == 204:
                return 204, resp_headers, None
            if status == 401:
                raise MailboxError("The authoring mailbox credential was rejected.") from exc
            payload: Any = None
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = None
            return status, resp_headers, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MailboxError("The authoring request did not complete.") from exc
        if status == 204:
            return 204, resp_headers, None
        if not raw:
            return status, resp_headers, None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MailboxError("The authoring response was not valid JSON.") from exc
        return status, resp_headers, payload

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
        expected: tuple[int, ...],
        allow_empty: bool = False,
    ) -> tuple[int, dict[str, Any] | None]:
        operation = path.rsplit("/", 1)[-1]
        if self._post_impl is not None:
            status, resp_headers, payload = self._post_impl(path, body, headers)
        else:
            status, resp_headers, payload = self._urllib_post(path, body, headers)
        if allow_empty and status == 204:
            return 204, None
        if status == 401:
            raise MailboxError("The authoring mailbox credential was rejected.")
        if status not in expected:
            raise MailboxError(f"The authoring {operation} request returned HTTP {status}.")
        cache = (resp_headers.get("cache-control") or "").strip().lower()
        if cache != "no-store":
            raise MailboxError(f"The authoring {operation} response was not marked no-store.")
        if not isinstance(payload, dict):
            raise MailboxError(f"The authoring {operation} response was not an object.")
        return status, payload

    def claim(self, pack_id: str, bind: str) -> dict[str, Any]:
        if not valid_bind_token(bind):
            raise MailboxError("Bind token is malformed")
        path = self._path(pack_id, "claim")
        _status, body = self._post(
            path,
            {"bind": bind},
            headers={"Content-Type": "application/json"},
            expected=(201,),
        )
        assert body is not None
        secret = body.get("leaseSecret")
        lease_s = body.get("lease_s", DEFAULT_LEASE_S)
        if not valid_lease_secret(secret) or not isinstance(lease_s, int) or lease_s <= 0:
            raise MailboxError("The authoring claim response was not a mailbox lease.")
        return {"leaseSecret": secret, "lease_s": lease_s}

    def poll(self, pack_id: str, lease_secret: str) -> dict[str, Any] | None:
        if not valid_lease_secret(lease_secret):
            raise MailboxError("The authoring mailbox credential is malformed.")
        path = self._path(pack_id, "poll")
        _status, body = self._post(
            path,
            {"wait_seconds": POLL_WAIT_S, "lease_seconds": DEFAULT_LEASE_S},
            headers={
                "Authorization": f"Bearer {lease_secret}",
                "Content-Type": "application/json",
            },
            expected=(200,),
            allow_empty=True,
        )
        return body

    def callback(
        self,
        pack_id: str,
        lease_secret: str,
        payload: dict[str, Any],
    ) -> None:
        if not valid_lease_secret(lease_secret):
            raise MailboxError("The authoring mailbox credential is malformed.")
        path = self._path(pack_id, "callback")
        self._post(
            path,
            _sanitize_result(payload),
            headers={
                "Authorization": f"Bearer {lease_secret}",
                "Content-Type": "application/json",
            },
            expected=(200, 202),
        )

    def allow(self, pack_id: str, lease_secret: str, command_id: str) -> None:
        if not valid_lease_secret(lease_secret):
            raise MailboxError("The authoring mailbox credential is malformed.")
        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            raise MailboxError("The authoring Allow request is malformed.")
        path = self._path(pack_id, "allow")
        self._post(
            path,
            {"command_id": command_id},
            headers={
                "Authorization": f"Bearer {lease_secret}",
                "Content-Type": "application/json",
            },
            expected=(200, 202),
        )


class _NullAudit:
    def log(self, event: str, **data: Any) -> None:
        return None


def _try_desktop_transport(*, origin: str, post: Any = None) -> MailboxTransport | Any | None:
    if post is not None:
        return None
    try:
        from engine.authoring_runner import AuthoringMailboxTransport
    except ImportError:
        return None
    return AuthoringMailboxTransport(origin=origin, audit=_NullAudit())


def open_mailbox_transport(
    *,
    origin: str = AUTHORING_ORIGIN,
    post: Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], Any]]
    | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> Any:
    """Prefer Desktop's mailbox transport; copy claim/poll/allow otherwise."""

    desktop = _try_desktop_transport(origin=origin, post=post)
    if desktop is not None:
        return desktop
    return MailboxTransport(origin=origin, post=post, opener=opener)


class MailboxClient:
    """Claim, Allow-per-sub, wait=0 poll, and Continue via record_observed."""

    def __init__(
        self,
        transport: Any,
        *,
        session: Any = None,
        prompt: Callable[[str], bool] | None = None,
        pause_wait: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        stdout: TextIO | None = None,
        url: Optional[str] = None,
        platform: Optional[str] = None,
        unique_window: Callable[[], dict[str, Any] | None] | None = None,
        recorder: Any = None,
        text_value_at: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        self.transport = transport
        self.session = session
        self._bridge = AuthoringBridge(session) if session is not None else None
        self._prompt = prompt or _default_allow_prompt
        self._pause_wait = pause_wait
        self._sleep = sleep
        self._stdout = stdout or sys.stdout
        self._url = url
        self._platform = platform or sys.platform
        self._unique_window = unique_window
        self._recorder = recorder
        self._text_value_at = text_value_at
        self._pack: str | None = None
        self._lease_secret: str | None = None
        self._allowed_sub: str | None = None
        self._pending_allow: dict[str, Any] | None = None
        self._pause_target: dict[str, Any] | None = None
        self._paused = False
        self._seen: set[str] = set()
        self._uncertain = False
        self._actuation_started = False
        self._backend, self._coach_only = self._pin()

    def _pin(self) -> tuple[str, bool]:
        if self._url:
            return "web", False
        plat = self._platform
        if plat == "win32" or plat.startswith("win"):
            return "windows", True
        if self._unique_window is not None:
            unique = self._unique_window()
            if unique is None:
                kind = "macos" if plat == "darwin" else "linux"
                return kind, True
            kind = str(unique.get("backend") or ("macos" if plat == "darwin" else "linux"))
            if kind in COACH_ONLY_BACKENDS:
                return kind, True
            if kind in UNIQUE_WINDOW_BACKENDS and unique.get("window_title_unique") is not True:
                return kind, True
            return kind, False
        if self.session is not None:
            kind = getattr(self.session, "backend_kind", None) or getattr(
                self.session, "backend", None
            )
            if kind in COACH_ONLY_BACKENDS:
                return str(kind), True
            if isinstance(kind, str) and kind:
                coach = getattr(self.session, "coach_only", False) is True
                return kind, coach
            return "web", False
        try:
            _backend, kind, _close = pin_local_backend(platform=plat)
        except AuthoringError:
            kind = "macos" if plat == "darwin" else "linux"
            return kind, True
        if kind in COACH_ONLY_BACKENDS:
            return kind, True
        if kind in UNIQUE_WINDOW_BACKENDS:
            return kind, False
        return kind, True

    def claim(self, pack_id: str, bind: str) -> dict[str, Any]:
        claimed = self.transport.claim(pack_id, bind)
        self._pack = pack_id
        self._lease_secret = claimed["leaseSecret"]
        return {"bound": True, "pack": pack_id}

    def poll_once(self) -> dict[str, Any] | None:
        if self._pack is None or self._lease_secret is None:
            raise MailboxError("not_bound")
        body = self.transport.poll(self._pack, self._lease_secret)
        if body is None:
            return None
        return self.handle_envelope(body)

    def handle_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        tool = envelope.get("tool")
        command_id = envelope.get("command_id")
        if not isinstance(tool, str) or not isinstance(command_id, str):
            return {"status": "error", "error": "invalid_envelope"}
        schema = envelope.get("schema_version")
        if schema not in (None, COMMAND_ENVELOPE_SCHEMA):
            return self._callback_error(command_id, "invalid_envelope")
        if command_id in self._seen:
            return {"status": "error", "error": "RECONCILIATION_REQUIRED"}
        args = envelope.get("args") if isinstance(envelope.get("args"), Mapping) else {}
        sub = envelope.get("oauth_sub_sha256")
        if tool == "bind_pack":
            pending = self._pending_allow
            if pending and pending.get("command_id") == command_id:
                return {"status": "pending_allow"}
            return self._queue_allow(envelope)
        if tool in {"type", "type_text"}:
            return self._callback_error(command_id, "type_refused")
        if tool in ENQUEUE_REQUIRING_ALLOW and (not self._allowed_sub or sub != self._allowed_sub):
            return self._callback_error(command_id, "not_allowed")
        if self._uncertain:
            self._seen.add(command_id)
            return self._callback_error(command_id, "RECONCILIATION_REQUIRED")
        self._seen.add(command_id)
        try:
            result = self._dispatch(tool, dict(args), command_id)
        except MailboxError as exc:
            code = (
                str(exc)
                if str(exc) in {"stale_node", "COACH_ONLY", "RECONCILIATION_REQUIRED"}
                else "error"
            )
            if str(exc) == "RECONCILIATION_REQUIRED" or self._uncertain:
                self._uncertain = True
            return self._callback_error(command_id, code)
        except AuthoringError as exc:
            code = exc.code or "error"
            if code == "COACH_ONLY":
                return self._callback_error(command_id, "COACH_ONLY")
            if code == "stale_node":
                return self._callback_error(command_id, "stale_node")
            return self._callback_error(command_id, "error")
        if result.get("status") == "paused":
            return result
        self._callback_done(command_id, result)
        return result

    def _queue_allow(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        sub = envelope.get("oauth_sub_sha256")
        command_id = envelope.get("command_id")
        if not isinstance(sub, str) or not isinstance(command_id, str):
            return self._callback_error(envelope.get("command_id") or "cmd", "invalid_allow")
        display = _client_display(envelope.get("client_display"))
        replace = bool(self._allowed_sub and self._allowed_sub != sub)
        self._pending_allow = {
            "command_id": command_id,
            "oauth_sub_sha256": sub,
            "client_display": display,
            "replace": replace,
        }
        if replace:
            message = "A different ChatGPT account is asking. Allow it to replace the current one?"
        else:
            message = f"Allow {display} to drive this job?"
        self._write(message)
        if not self._prompt(message):
            self._pending_allow = None
            return self._callback_error(command_id, "denied")
        return self.allow(replace=replace)

    def allow(self, *, replace: bool = False) -> dict[str, Any]:
        pending = self._pending_allow
        if pending is None:
            raise MailboxError("There is no pending Allow request.")
        if self._allowed_sub and self._allowed_sub != pending["oauth_sub_sha256"] and not replace:
            return {"allowed": False, "status": "replace_allow"}
        command_id = pending["command_id"]
        if self._pack and self._lease_secret:
            self.transport.allow(self._pack, self._lease_secret, command_id)
        self._allowed_sub = pending["oauth_sub_sha256"]
        self._pending_allow = None
        self._seen.add(command_id)
        result = {"allowed": True, "client_display": pending.get("client_display")}
        self._callback_done(command_id, result)
        return result

    def continue_pause(self) -> dict[str, Any]:
        if not self._paused or self._pause_target is None:
            return {"paused": False}
        target = self._pause_target
        recorder = self._recorder
        if recorder is None and self.session is not None:
            recorder = self.session
        if recorder is None:
            raise MailboxError("authoring session does not implement record_observed")
        original = None
        if hasattr(recorder, "type_text"):
            original = recorder.type_text

            def _forbidden(*_args: Any, **_kwargs: Any) -> None:
                raise MailboxError("Continue must not type")

            recorder.type_text = _forbidden
        try:
            if hasattr(recorder, "continue_input"):
                raw = recorder.continue_input()
            elif hasattr(recorder, "record_observed"):
                kwargs: dict[str, Any] = {
                    "event": {"kind": "type"},
                    "param": target.get("param"),
                }
                if target.get("secret"):
                    kwargs["secret"] = True
                else:
                    text = None
                    if self._text_value_at is not None and isinstance(
                        target.get("backend_pixels"), dict
                    ):
                        text = self._text_value_at(target["backend_pixels"])
                    kwargs["text"] = text
                recorder.record_observed(**kwargs)
                raw = {"recorded": True, "param": target.get("param")}
            else:
                raise MailboxError("authoring session does not implement record_observed")
        finally:
            if original is not None:
                recorder.type_text = original
        self._paused = False
        self._pause_target = None
        result = {"recorded": True}
        if isinstance(raw, Mapping):
            if raw.get("param"):
                result["param"] = raw["param"]
        elif target.get("param"):
            result["param"] = target["param"]
        result = _sanitize_result(result)
        command_id = target.get("command_id")
        if isinstance(command_id, str):
            self._callback_done(command_id, result)
        return result

    def run(self, *, max_polls: int | None = None) -> int:
        polls = 0
        while max_polls is None or polls < max_polls:
            self.poll_once()
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            self._sleep(LOCAL_POLL_SLEEP_S)
        return 0

    def _dispatch(self, tool: str, args: dict[str, Any], command_id: str) -> dict[str, Any]:
        if tool == "observe":
            return self._observe()
        if tool == "start_record":
            return self._start_record()
        if tool == "click":
            return self._click(args)
        if tool == "halt":
            return self._halt()
        if tool == "pause_for_input":
            return self._pause_for_input(args, command_id)
        if tool == "stop_record":
            if self._bridge is not None:
                return self._bridge.dispatch("stop_record", {})
            return {"status": "stopped", "compiled": False}
        if tool == "compile":
            if self._bridge is not None:
                return self._bridge.dispatch("compile", {})
            return {"status": "needs_human_admit", "recording_retained": True}
        if tool in {"set_coach", "get_coach"}:
            if self._bridge is not None:
                return self._bridge.dispatch(tool, args)
            return {"ok": True} if tool == "set_coach" else {"hint": None}
        raise MailboxError("unknown_tool")

    def _observe(self) -> dict[str, Any]:
        if self._coach_only:
            return project_observe(
                {
                    "backend": self._backend
                    if self._backend in {"windows", "rdp", "citrix", "macos", "linux", "web"}
                    else "windows",
                    "provider": "none",
                    "coach_only": True,
                    "agent_drive": False,
                    "tree": [],
                }
            )
        if self._bridge is None:
            raise MailboxError("authoring session is not available")
        return self._bridge.dispatch("observe", {})

    def _start_record(self) -> dict[str, Any]:
        if self._coach_only:
            raise MailboxError("COACH_ONLY")
        if self._bridge is None:
            raise MailboxError("authoring session is not available")
        return self._bridge.dispatch("start_record", {})

    def _click(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._coach_only:
            raise MailboxError("COACH_ONLY")
        if self._uncertain:
            raise MailboxError("RECONCILIATION_REQUIRED")
        node_id = args.get("node_id")
        if not isinstance(node_id, str):
            raise MailboxError("stale_node")
        if self._bridge is None:
            raise MailboxError("authoring session is not available")
        self._actuation_started = True
        try:
            result = self._bridge.dispatch("click", {"node_id": node_id})
        except AuthoringError as exc:
            if exc.code in {"stale_node", "COACH_ONLY"}:
                raise MailboxError(exc.code) from exc
            self._uncertain = True
            raise MailboxError("RECONCILIATION_REQUIRED") from None
        except Exception:
            self._uncertain = True
            raise MailboxError("RECONCILIATION_REQUIRED") from None
        finally:
            self._actuation_started = False
        if isinstance(result, Mapping) and result.get("error") == "stale_node":
            raise MailboxError("stale_node")
        return dict(result)

    def _halt(self) -> dict[str, Any]:
        if self._actuation_started:
            self._uncertain = True
            raise MailboxError("RECONCILIATION_REQUIRED")
        if self._bridge is not None:
            return self._bridge.dispatch("halt", {})
        return {"status": "halted", "compiled": False}

    def _pause_for_input(self, args: dict[str, Any], command_id: str) -> dict[str, Any]:
        self._pause_target = {
            "node_id": args.get("node_id"),
            "param": args.get("param"),
            "secret": args.get("secret") is True,
            "backend_pixels": args.get("backend_pixels"),
            "command_id": command_id,
        }
        self._paused = True
        self._write(PAUSE_PROMPT)
        waiter = self._pause_wait
        if waiter is None:
            input(PAUSE_PROMPT)
        else:
            waiter()
        return {"status": "paused", **self.continue_pause()}

    def _callback_error(self, command_id: object, error: str) -> dict[str, Any]:
        payload = {
            "command_id": command_id,
            "status": "error",
            "result": {"error": error},
        }
        if self._pack and self._lease_secret and isinstance(command_id, str):
            self.transport.callback(self._pack, self._lease_secret, payload)
        return {"status": "error", "error": error}

    def _callback_done(self, command_id: str, result: Mapping[str, Any]) -> None:
        if not self._pack or not self._lease_secret:
            return
        self.transport.callback(
            self._pack,
            self._lease_secret,
            {
                "command_id": command_id,
                "status": "done",
                "result": _sanitize_result(dict(result)),
            },
        )

    def _write(self, message: str) -> None:
        self._stdout.write(message + "\n")
        self._stdout.flush()


def _default_allow_prompt(message: str) -> bool:
    reply = input(f"{message} [y/N] ").strip().lower()
    return reply in {"y", "yes"}


def _open_session(*, url: Optional[str], headed: bool, platform: Optional[str]) -> Any:
    plat = platform or sys.platform
    if (plat == "win32" or plat.startswith("win")) and not url:
        return CoachOnlySession("windows")
    try:
        return open_authoring_session(url=url, headed=headed, platform=platform)
    except AuthoringError:
        if (plat == "win32" or plat.startswith("win")) and not url:
            return CoachOnlySession("windows")
        return None


def connect_mailbox(
    target: str,
    *,
    url: Optional[str] = None,
    headed: bool = False,
    prompt: Callable[[str], bool] | None = None,
    pause_wait: Callable[[], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    post: Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, str], Any]]
    | None = None,
    session: Any = None,
    max_polls: int | None = None,
    stdout: TextIO | None = None,
    platform: Optional[str] = None,
    unique_window: Callable[[], dict[str, Any] | None] | None = None,
    recorder: Any = None,
    text_value_at: Callable[[dict[str, Any]], str | None] | None = None,
) -> int:
    """Claim a runner link or pack URL and poll the hosted mailbox."""

    try:
        parsed = parse_connect_target(target)
    except RunnerBindError as exc:
        raise MailboxError(str(exc)) from exc
    if "bind" not in parsed:
        raise MailboxError(
            "this pack URL is not a runner link; paste the Connect this computer "
            "command (openadapt://runner?pack=…&bind=oab_…)"
        )
    transport = open_mailbox_transport(origin=parsed["origin"], post=post)
    opened = session
    if opened is None:
        opened = _open_session(url=url, headed=headed, platform=platform)
    if url and opened is not None:
        require_empty_cookies(opened)
        inner = getattr(opened, "page", None) or getattr(opened, "backend", None)
        if inner is not None and inner is not opened:
            require_empty_cookies(inner)
    client = MailboxClient(
        transport,
        session=opened,
        prompt=prompt,
        pause_wait=pause_wait,
        sleep=sleep or time.sleep,
        stdout=stdout,
        url=url,
        platform=platform,
        unique_window=unique_window,
        recorder=recorder,
        text_value_at=text_value_at,
    )
    bound = client.claim(parsed["pack"], parsed["bind"])
    out = stdout or sys.stdout
    out.write(BOUND_SENTENCE + "\n")
    out.write("Waiting for ChatGPT or Claude to ask for Allow.\n")
    out.flush()
    if bound.get("leaseSecret") or bound.get("lease_secret"):
        raise MailboxError("claim must not return the mailbox lease to the operator")
    try:
        return client.run(max_polls=max_polls)
    except KeyboardInterrupt:
        out.write("stopped\n")
        return 0
