"""Outbound authoring mailbox client for ChatGPT.com / Claude.ai drive-once.

Copy poll / lease / TTL / kill-as-command / metadata-callback *shape* from
:mod:`engine.hosted_runner`. Do not copy org, Stripe, ``oar_``, a 25s poll wait,
trust-manifest, or journal dispatch. Windows native is COACH_ONLY: this module
must not spawn ``win_agent`` or call ``parallels_vm.launch_agent``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from loguru import logger

from engine.auth.runner_bind import (
    AUTHORING_ORIGIN,
    parse_runner_uri,
    valid_lease_secret,
    valid_pack_id,
)
from engine.auth.store import load_authoring_lease, store_authoring_lease
from engine.config import EngineConfig

API_TIMEOUT_S = 10.0
DEFAULT_LEASE_S = 900
POLL_WAIT_S = 0
LOCAL_POLL_SLEEP_S = 1.0
NODE_TABLE_LIFETIME_S = 15 * 60
COMMAND_ENVELOPE_SCHEMA = "openadapt.authoring.command/v1"
OBSERVE_SCHEMA = "openadapt.authoring.observe/v1"
CLIENT_DISPLAYS = frozenset({"ChatGPT", "Claude"})
PAUSE_PROMPT = "Type in the application. Continue here when done."
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
COACH_ONLY_BACKENDS = frozenset({"windows", "rdp", "citrix"})
UNIQUE_WINDOW_BACKENDS = frozenset({"macos", "linux"})
PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
SIX_DIGITS_RE = re.compile(r"\d{6,}")
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SAFE_PARAM = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_NODE_ID = re.compile(r"^n_[a-f0-9]{8}$")
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
MAILBOX_ACTIONS = frozenset({"claim", "poll", "callback", "allow"})


class AuthoringError(RuntimeError):
    """A safe, user-facing authoring failure with no secret-bearing text."""


class AuthoringCoachOnly(AuthoringError):
    """This substrate cannot agent-drive in v1."""


class AuthoringTransportError(AuthoringError):
    """The mailbox HTTPS transport did not confirm an operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lease_hmac_key(lease_secret: str) -> bytes:
    """Use the lease secret body as HMAC key material. Do not hash it as a password."""

    if not valid_lease_secret(lease_secret):
        raise AuthoringError("The authoring mailbox credential is malformed.")
    return bytes.fromhex(lease_secret[5:])


def _sanitize_result(value: Any) -> Any:
    """Drop titles, values, pixels, and other vendor-forbidden keys."""

    if isinstance(value, dict):
        return {
            key: _sanitize_result(child)
            for key, child in value.items()
            if key not in FORBIDDEN_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    return value


def _require_empty_cookies(browser: Any) -> None:
    cookies_fn = getattr(browser, "cookies", None)
    if cookies_fn is None:
        context = getattr(browser, "context", None)
        cookies_fn = getattr(context, "cookies", None)
    if not callable(cookies_fn):
        raise AuthoringError("Playwright Chromium did not start with empty cookies.")
    cookies = cookies_fn()
    if cookies:
        raise AuthoringError("Playwright Chromium did not start with empty cookies.")


def launch_empty_playwright_chromium(url: str) -> Any:
    """Launch a fresh Chromium with empty cookies. Never attach to logged-in Chrome."""

    if not isinstance(url, str) or not url.startswith("https://"):
        raise AuthoringError("A Playwright job needs a URL typed into Desktop.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthoringError("Playwright Chromium is unavailable.") from exc
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    cookies = context.cookies()
    if cookies:
        browser.close()
        playwright.stop()
        raise AuthoringError("Playwright Chromium did not start with empty cookies.")
    page = context.new_page()
    page.goto(url)
    return page


def _pack_dir(data_dir: Path, pack_id: str) -> Path:
    return Path(data_dir) / "authoring" / _sha256_hex(pack_id)[:16]


def filter_coach_hint(text: object) -> str | None:
    """Apply the 80-character / no-URL / no-``@`` / no-6-digits coach filter."""

    if not isinstance(text, str):
        return None
    collapsed = " ".join(text.split())
    if not collapsed or len(collapsed) > 80:
        return None
    if "://" in collapsed or "@" in collapsed or SIX_DIGITS_RE.search(collapsed):
        return None
    return collapsed


def _client_display(value: object) -> str:
    if value in CLIENT_DISPLAYS:
        return str(value)
    return "ChatGPT"


class NodeTable:
    """Laptop-only node table. Mode 0600, 15-minute lifetime."""

    def __init__(self, path: Path, hmac_key: bytes) -> None:
        self._path = Path(path)
        self._hmac_key = hmac_key
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                return

    def mint_node_id(self, provider_runtime_id: str) -> str:
        digest = hmac.new(
            self._hmac_key,
            provider_runtime_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"n_{digest[:8]}"

    def replace(self, rows: list[dict[str, Any]]) -> None:
        payload = {
            "updated_at": time.time(),
            "rows": rows,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not os.name == "nt":
            os.chmod(self._path.parent, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._path, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded.encode("utf-8"))
        finally:
            os.close(descriptor)

    def get(self, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                raw = self._path.read_text(encoding="utf-8")
                details = self._path.stat()
            except OSError:
                return None
            if os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o600:
                return None
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                return None
            updated = payload.get("updated_at")
            stale = not isinstance(updated, (int, float)) or (
                time.time() - updated > NODE_TABLE_LIFETIME_S
            )
            if stale:
                return None
            for row in payload["rows"]:
                if isinstance(row, dict) and row.get("node_id") == node_id:
                    observed = row.get("observed_at")
                    if (
                        isinstance(observed, (int, float))
                        and time.time() - (observed / 1000.0) > NODE_TABLE_LIFETIME_S
                    ):
                        return None
                    return row
            return None


def project_observe(
    *,
    backend: str,
    provider: str,
    recording: bool,
    agent_drive: bool,
    coach_only: bool,
    process_name: str | None,
    raw_nodes: list[dict[str, Any]],
    node_table: NodeTable,
) -> dict[str, Any]:
    """PHI-safe observe projection. Fail closed; never a raw fallback."""

    window = {
        "process_name": (
            process_name if process_name and PROCESS_NAME_RE.fullmatch(process_name) else None
        ),
        "role": "window",
        "bounds": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    }
    if window["process_name"] is None:
        window.pop("process_name")
    tree: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    if agent_drive and not coach_only:
        for raw in raw_nodes:
            projected, row = _project_node(raw, node_table)
            if projected is None or row is None:
                continue
            tree.append(projected)
            rows.append(row)
            if len(tree) >= 200:
                break
    node_table.replace(rows)
    payload: dict[str, Any] = {
        "schema_version": OBSERVE_SCHEMA,
        "backend": backend,
        "provider": provider,
        "mode": "authoring",
        "agent_drive": agent_drive and not coach_only,
        "coach_only": coach_only or not agent_drive,
        "recording": recording,
        "window": window,
        "tree": tree,
        "truncated": len(raw_nodes) > len(tree),
        "node_count": len(tree),
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 32 * 1024:
        payload["tree"] = []
        payload["truncated"] = True
        payload["node_count"] = 0
        payload["reason"] = "empty_projection"
        node_table.replace([])
    elif not tree:
        payload["reason"] = "empty_projection"
    return payload


def _project_node(
    raw: dict[str, Any],
    node_table: NodeTable,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        return None, None
    runtime_id = raw.get("provider_runtime_id")
    pixels = raw.get("backend_pixels")
    bounds = raw.get("bounds")
    if not isinstance(runtime_id, str) or not runtime_id:
        return None, None
    if not isinstance(pixels, dict) or not isinstance(bounds, dict):
        return None, None
    try:
        pixel_box = {key: int(pixels[key]) for key in ("x", "y", "w", "h")}
        normalized = {key: float(bounds[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None, None
    node_id = node_table.mint_node_id(runtime_id)
    projected: dict[str, Any] = {
        "node_id": node_id,
        "role": str(raw.get("role") or "unknown")[:40],
        "control_type": str(raw.get("control_type") or "")[:40],
        "enabled": bool(raw.get("enabled", True)),
        "focused": bool(raw.get("focused", False)),
        "bounds": normalized,
    }
    automation_id = _project_label(raw.get("automation_id"))
    if automation_id:
        projected["automation_id"] = automation_id
    name = _project_label(raw.get("name"))
    if name:
        projected["name"] = name
    row = {
        "node_id": node_id,
        "backend_pixels": pixel_box,
        "normalized": normalized,
        "provider_runtime_id": runtime_id,
        "observed_at": int(time.time() * 1000),
    }
    return projected, row


def _project_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 80:
        return None
    if "://" in collapsed or "@" in collapsed or SIX_DIGITS_RE.search(collapsed):
        return None
    return collapsed


class AuthoringMailboxTransport:
    """Outbound HTTPS bind/poll/callback. Wait is always 0."""

    def __init__(
        self,
        *,
        origin: str,
        audit: Any,
        client: httpx.Client | None = None,
    ) -> None:
        if origin != AUTHORING_ORIGIN:
            raise AuthoringTransportError("The authoring origin is not pinned.")
        self.origin = origin
        self._audit = audit
        if client is not None:
            base = str(client.base_url).removesuffix("/")
            if base != origin:
                raise AuthoringTransportError("The authoring HTTP client differs from its origin.")
        self._client = client or httpx.Client(
            base_url=origin,
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _path(self, pack_id: str, action: str) -> str:
        if not valid_pack_id(pack_id) or action not in MAILBOX_ACTIONS:
            raise AuthoringTransportError("The authoring mailbox path is invalid.")
        return f"/j/{quote(pack_id, safe='._-')}/runner/{action}"

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
        self._audit.log(
            "authoring_request",
            operation=operation,
            destination=self.origin,
            path=path.rsplit("/", 3)[0] + "/runner/" + operation,
        )
        try:
            response = self._client.post(
                path,
                json=body,
                headers=headers,
                follow_redirects=False,
            )
        except (httpx.HTTPError, OSError) as exc:
            self._audit.log(
                "authoring_transport_failed",
                operation=operation,
                destination=self.origin,
                error_type=type(exc).__name__,
            )
            raise AuthoringTransportError(
                f"The authoring {operation} request did not complete."
            ) from exc
        self._audit.log(
            "authoring_response",
            operation=operation,
            destination=self.origin,
            status_code=response.status_code,
        )
        if response.status_code == 401:
            raise AuthoringTransportError("The authoring mailbox credential was rejected.")
        if allow_empty and response.status_code == 204:
            return 204, None
        if response.status_code not in expected:
            raise AuthoringTransportError(
                f"The authoring {operation} request returned HTTP {response.status_code}."
            )
        cache = (response.headers.get("cache-control") or "").strip().lower()
        if cache != "no-store":
            raise AuthoringTransportError(
                f"The authoring {operation} response was not marked no-store."
            )
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthoringTransportError(
                f"The authoring {operation} response was not valid JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise AuthoringTransportError(f"The authoring {operation} response was not an object.")
        return response.status_code, parsed

    def claim(self, pack_id: str, bind: str) -> dict[str, Any]:
        path = self._path(pack_id, "claim")
        status, body = self._post(
            path,
            {"bind": bind},
            headers={"Content-Type": "application/json"},
            expected=(201,),
        )
        assert status == 201
        assert body is not None
        secret = body.get("leaseSecret")
        lease_s = body.get("lease_s", DEFAULT_LEASE_S)
        if not valid_lease_secret(secret) or not isinstance(lease_s, int) or lease_s <= 0:
            raise AuthoringTransportError("The authoring claim response was not a mailbox lease.")
        return {"leaseSecret": secret, "lease_s": lease_s}

    def poll(self, pack_id: str, lease_secret: str) -> dict[str, Any] | None:
        if not valid_lease_secret(lease_secret):
            raise AuthoringTransportError("The authoring mailbox credential is malformed.")
        path = self._path(pack_id, "poll")
        _, body = self._post(
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
            raise AuthoringTransportError("The authoring mailbox credential is malformed.")
        path = self._path(pack_id, "callback")
        self._post(
            path,
            payload,
            headers={
                "Authorization": f"Bearer {lease_secret}",
                "Content-Type": "application/json",
            },
            expected=(200, 202),
        )

    def allow(self, pack_id: str, lease_secret: str, command_id: str) -> None:
        if not valid_lease_secret(lease_secret):
            raise AuthoringTransportError("The authoring mailbox credential is malformed.")
        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            raise AuthoringTransportError("The authoring Allow request is malformed.")
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


class AuthoringRunner:
    """Claim, Allow-per-sub, wait=0 poll, and Flow record_observed session."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        audit: Any | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        observe_nodes: Callable[[], list[dict[str, Any]]] | None = None,
        recorder_factory: Callable[..., Any] | None = None,
        compile_recording: Callable[..., Any] | None = None,
        playwright_launcher: Callable[[str], Any] | None = None,
        text_value_at: Callable[[dict[str, int]], str | None] | None = None,
        unique_window: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self.emit = emit or (lambda _event, _data: None)
        self.audit = audit
        self._client = client
        self._sleep = sleep
        self._observe_nodes = observe_nodes or (lambda: [])
        self._recorder_factory = recorder_factory
        self._compile_recording = compile_recording
        self._playwright_launcher = playwright_launcher
        self._text_value_at = text_value_at
        self._unique_window = unique_window
        self._transport: AuthoringMailboxTransport | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pack: str | None = None
        self._lease_secret: str | None = None
        self._allowed_sub: str | None = None
        self._allowed_client_id: str | None = None
        self._pending_allow: dict[str, Any] | None = None
        self._pin: dict[str, Any] = {"backend": "macos", "window_title_unique": False}
        self._node_table: NodeTable | None = None
        self._recorder: Any | None = None
        self._recording = False
        self._paused = False
        self._pause_target: dict[str, Any] | None = None
        self._pause_command_id: str | None = None
        self._active_command_id: str | None = None
        self._secret_pause = False
        self._secret_type_recorded = False
        self._actuation_started = False
        self._uncertain = False
        self._coach_hint: str | None = None
        self._out_dir: Path | None = None
        self._playwright: Any | None = None

    def is_bound(self) -> bool:
        return self._pack is not None and self._lease_secret is not None

    def has_pause(self) -> bool:
        return self._paused and self._recorder is not None

    def status_dict(self) -> dict[str, Any] | None:
        if not self.is_bound():
            return None
        if self._paused:
            return {
                "recording": True,
                "paused": True,
                "capture_id": None,
                "pause_prompt": PAUSE_PROMPT,
                "controls": {"pause": False, "resume": True, "stop": True},
            }
        if self._recording:
            return {
                "recording": True,
                "paused": False,
                "capture_id": None,
                "controls": {"pause": False, "resume": False, "stop": True},
            }
        return {
            "recording": False,
            "paused": False,
            "capture_id": None,
            "controls": {"pause": False, "resume": False, "stop": self.is_bound()},
        }

    def status(self) -> dict[str, Any]:
        pending = self._pending_allow
        if pending and self._allowed_sub and pending.get("oauth_sub_sha256") != self._allowed_sub:
            state = "replace_allow"
        elif pending:
            state = "pending_allow"
        elif self.is_bound():
            state = "bound"
        else:
            state = "idle"
        return {
            "status": state,
            "pack_bound": bool(self._pack),
            "allowed": bool(self._allowed_sub),
            "client_display": pending.get("client_display") if pending else None,
            "coach_only": self._pin.get("backend") in COACH_ONLY_BACKENDS,
        }

    def pin_target(self, **fields: Any) -> dict[str, Any]:
        backend = str(fields.get("backend") or self._pin.get("backend") or "macos")
        unique = fields.get("window_title_unique")
        macos_app = fields.get("macos_app")
        macos_title = fields.get("macos_window_title")
        linux_app = fields.get("linux_app")
        linux_title = fields.get("linux_window_title")
        if fields.get("use_frontmost") is True:
            probe = self._unique_window() if self._unique_window is not None else None
            if not isinstance(probe, dict) or probe.get("unique") is not True:
                unique = False
            else:
                unique = True
                backend = str(probe.get("backend") or backend)
                macos_app = probe.get("process_name") or macos_app
                macos_title = probe.get("window_title") or macos_title
                linux_app = probe.get("process_name") or linux_app
                linux_title = probe.get("window_title") or linux_title
        if unique is None:
            unique = False if backend in UNIQUE_WINDOW_BACKENDS else True
        pin = {
            "backend": backend,
            "url": fields.get("url") if backend == "web" else None,
            "macos_app": macos_app,
            "macos_window_title": macos_title,
            "linux_app": linux_app,
            "linux_window_title": linux_title,
            "window_title_unique": bool(unique),
        }
        self._pin = pin
        return {
            "ok": True,
            "backend": backend,
            "coach_only": self._coach_only(),
        }

    def claim_uri(self, uri: str, *, start_loop: bool = True) -> dict[str, Any]:
        parsed = parse_runner_uri(uri)
        pack = parsed["pack"]
        bind = parsed["bind"]
        origin = parsed["origin"]
        transport = AuthoringMailboxTransport(
            origin=origin,
            audit=self.audit or _NullAudit(),
            client=self._client,
        )
        try:
            claimed = transport.claim(pack, bind)
        except AuthoringTransportError:
            close = getattr(transport, "close", None)
            if self._client is None and callable(close):
                close()
            raise
        lease_secret = claimed["leaseSecret"]
        payload = {
            "pack": pack,
            "origin": origin,
            "lease_secret": lease_secret,
            "lease_s": int(claimed["lease_s"]),
            "claimed_at": _utc_now(),
            "allowed_sub": None,
            "allowed_client_id": None,
            "allowed_at": None,
        }
        if not store_authoring_lease(pack, payload):
            close = getattr(transport, "close", None)
            if self._client is None and callable(close):
                close()
            raise AuthoringError(
                "Desktop could not store the authoring lease in the OS keychain."
            )
        with self._lock:
            self._transport = transport
            self._pack = pack
            self._lease_secret = lease_secret
            hmac_key = _lease_hmac_key(lease_secret)
            self._node_table = NodeTable(
                _pack_dir(self.config.data_dir, pack) / "nodes.json",
                hmac_key,
            )
            self._out_dir = _pack_dir(self.config.data_dir, pack) / "recording"
        if self.audit:
            self.audit.log("authoring_bind_claimed", pack_hash=_sha256_hex(pack)[:16])
        if start_loop:
            self.start()
        self.emit("authoring_state", self.status())
        return {"bound": True, "origin": origin, "pack_prefix": pack[:2]}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="authoring-poll", daemon=True)
            self._thread.start()

    def stop_loop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except AuthoringError:
                logger.warning("authoring poll failed")
            self._sleep(LOCAL_POLL_SLEEP_S)

    def poll_once(self) -> None:
        transport = self._transport
        pack = self._pack
        secret = self._lease_secret
        if transport is None or pack is None or secret is None:
            return
        body = transport.poll(pack, secret)
        if body is None:
            return
        if body.get("halted") is True or (
            isinstance(body.get("head"), dict) and body["head"].get("halted") is True
        ):
            self._halt(unsigned=True, command_id=None)
            return
        envelope = body.get("command") if isinstance(body.get("command"), dict) else body
        if isinstance(envelope, dict) and envelope.get("tool"):
            self.handle_envelope(envelope)

    def handle_envelope(self, envelope: dict[str, Any]) -> None:
        tool = envelope.get("tool")
        command_id = envelope.get("command_id")
        pack_id = envelope.get("pack_id")
        if tool not in ENQUEUE_REQUIRING_ALLOW | {"bind_pack"}:
            self._callback_error(command_id, "unknown_tool")
            return
        if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
            return
        if pack_id != self._pack:
            self._callback_error(command_id, "pack_mismatch")
            return
        if command_id in {self._active_command_id, self._pause_command_id}:
            return
        if (
            tool == "bind_pack"
            and self._pending_allow
            and self._pending_allow.get("command_id") == command_id
        ):
            return
        sub = envelope.get("oauth_sub_sha256")
        if tool == "bind_pack":
            self._queue_allow(envelope)
            return
        if not self._allowed_sub or sub != self._allowed_sub:
            self._callback_error(command_id, "not_allowed")
            return
        args = envelope.get("args") if isinstance(envelope.get("args"), dict) else {}
        self._active_command_id = command_id
        try:
            result = self._dispatch_tool(str(tool), args)
        except AuthoringCoachOnly:
            self._active_command_id = None
            self._callback(
                {
                    "command_id": command_id,
                    "status": "done",
                    "result": {"error": "COACH_ONLY", "agent_drive": False, "coach_only": True},
                }
            )
            return
        except AuthoringError as exc:
            self._active_command_id = None
            self._callback_error(command_id, str(exc))
            return
        if tool == "pause_for_input":
            self._pause_command_id = command_id
            return
        self._active_command_id = None
        self._callback({"command_id": command_id, "status": "done", "result": result})

    def _queue_allow(self, envelope: dict[str, Any]) -> None:
        sub = envelope.get("oauth_sub_sha256")
        client = envelope.get("client_id_sha256")
        if not isinstance(sub, str) or _SHA256_HEX.fullmatch(sub) is None:
            self._callback_error(envelope.get("command_id"), "invalid_allow")
            return
        if client is not None and (
            not isinstance(client, str) or _SHA256_HEX.fullmatch(client) is None
        ):
            self._callback_error(envelope.get("command_id"), "invalid_allow")
            return
        display = _client_display(envelope.get("client_display"))
        self._pending_allow = {
            "command_id": envelope.get("command_id"),
            "oauth_sub_sha256": sub,
            "client_id_sha256": client,
            "client_display": display,
        }
        status = (
            "replace_allow"
            if self._allowed_sub and self._allowed_sub != sub
            else "pending_allow"
        )
        copy = (
            f"A different {display} account is asking. Allow it to replace the current one?"
            if status == "replace_allow"
            else f"Allow {display} to drive this job"
        )
        self.emit(
            "authoring_state",
            {"status": status, "client_display": display, "prompt": copy},
        )

    def allow(self, *, replace: bool = False) -> dict[str, Any]:
        pending = self._pending_allow
        if pending is None:
            raise AuthoringError("There is no pending Allow request.")
        if (
            self._allowed_sub
            and self._allowed_sub != pending["oauth_sub_sha256"]
            and not replace
        ):
            return self.status()
        command_id = pending.get("command_id")
        display = pending.get("client_display")
        if (
            isinstance(command_id, str)
            and self._transport is not None
            and self._pack
            and self._lease_secret
        ):
            self._transport.allow(self._pack, self._lease_secret, command_id)
        granted_at = _utc_now()
        self._allowed_sub = pending["oauth_sub_sha256"]
        self._allowed_client_id = pending.get("client_id_sha256")
        stored = load_authoring_lease(self._pack or "") if self._pack else None
        if stored is not None:
            stored["allowed_sub"] = self._allowed_sub
            stored["allowed_client_id"] = self._allowed_client_id
            stored["allowed_at"] = granted_at
            store_authoring_lease(self._pack or "", stored)
        self._pending_allow = None
        if self.audit:
            self.audit.log(
                "authoring_allowed",
                pack_hash=_sha256_hex(self._pack or "")[:16],
                allowed_sub_prefix=(self._allowed_sub or "")[:8],
                client_display=display,
            )
        self.emit("authoring_state", self.status())
        return {"allowed": True, "client_display": display}

    def deny(self) -> dict[str, Any]:
        pending = self._pending_allow
        self._pending_allow = None
        if pending and isinstance(pending.get("command_id"), str):
            self._callback_error(pending["command_id"], "denied")
        self.emit("authoring_state", {"status": "bound"})
        return {"allowed": False}

    def continue_pause(self) -> dict[str, Any]:
        if not self.has_pause() or self._pause_target is None or self._recorder is None:
            return self.status_dict() or {"recording": False, "paused": False}
        recorder = self._recorder
        target = self._pause_target
        if hasattr(recorder, "type_text"):
            original = recorder.type_text

            def _forbidden(*_args: Any, **_kwargs: Any) -> None:
                raise AuthoringError("Continue must not type")

            recorder.type_text = _forbidden
        else:
            original = None
        try:
            text = None
            if self._text_value_at is not None:
                text = self._text_value_at(target["backend_pixels"])
            if target.get("secret"):
                if text is not None and not text:
                    return self.status_dict() or {"recording": True, "paused": True}
                recorder.record_observed(
                    event={"kind": "type"},
                    param=target.get("param"),
                    secret=True,
                    redact_region=target.get("backend_pixels"),
                )
                self._secret_type_recorded = True
            else:
                recorder.record_observed(
                    event={"kind": "type"},
                    param=target.get("param"),
                    text=text,
                )
        finally:
            if original is not None:
                recorder.type_text = original
        command_id = self._pause_command_id
        self._paused = False
        self._pause_target = None
        self._pause_command_id = None
        self._active_command_id = None
        self.emit("status_update", self.status_dict() or {})
        if self.audit:
            self.audit.log(
                "authoring_pause_typed",
                param=target.get("param"),
                secret=bool(target.get("secret")),
            )
        if isinstance(command_id, str):
            self._callback(
                {
                    "command_id": command_id,
                    "status": "done",
                    "result": {"recorded": True, "param": target.get("param")},
                }
            )
        return self.status_dict() or {}

    def operator_stop(self) -> dict[str, Any]:
        self._halt(unsigned=True, command_id=None)
        return {"recording": False, "paused": False, "halted": True}

    def _dispatch_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "observe":
            return self._observe()
        if tool == "start_record":
            return self._start_record()
        if tool == "click":
            return self._click(args)
        if tool == "pause_for_input":
            return self._pause_for_input(args)
        if tool == "stop_record":
            return self._stop_record()
        if tool == "compile":
            return self._compile()
        if tool == "halt":
            self._halt(unsigned=False, command_id=None)
            return {"halted": True}
        if tool == "set_coach":
            hint = filter_coach_hint(args.get("hint") or args.get("text"))
            self._coach_hint = hint
            return {"ok": hint is not None}
        if tool == "get_coach":
            return {"hint": self._coach_hint}
        raise AuthoringError("unknown_tool")

    def _coach_only(self) -> bool:
        backend = str(self._pin.get("backend") or "")
        if backend in COACH_ONLY_BACKENDS:
            return True
        if backend in UNIQUE_WINDOW_BACKENDS and not self._pin.get("window_title_unique"):
            return True
        return False

    def _observe(self) -> dict[str, Any]:
        backend = str(self._pin.get("backend") or "macos")
        coach_only = self._coach_only()
        agent_drive = not coach_only
        if self._node_table is None:
            raise AuthoringError("not_bound")
        raw = [] if coach_only else list(self._observe_nodes())
        provider = {
            "web": "playwright_ax",
            "macos": "ax",
            "linux": "atspi",
        }.get(backend, "none")
        process_name = None
        if backend == "web":
            process_name = "Chromium"
        elif backend == "macos":
            process_name = self._pin.get("macos_app")
        elif backend == "linux":
            process_name = self._pin.get("linux_app")
        return project_observe(
            backend=backend,
            provider=provider,
            recording=self._recording,
            agent_drive=agent_drive,
            coach_only=coach_only,
            process_name=process_name if isinstance(process_name, str) else None,
            raw_nodes=raw,
            node_table=self._node_table,
        )

    def _start_record(self) -> dict[str, Any]:
        if self._coach_only():
            raise AuthoringCoachOnly("COACH_ONLY")
        backend = str(self._pin.get("backend") or "macos")
        if backend == "windows":
            raise AuthoringCoachOnly("COACH_ONLY")
        if backend == "web":
            url = self._pin.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise AuthoringError("A Playwright job needs a URL typed into Desktop.")
            launcher = self._playwright_launcher or launch_empty_playwright_chromium
            self._playwright = launcher(url)
            _require_empty_cookies(self._playwright)
        factory = self._recorder_factory
        if factory is None:
            raise AuthoringError("Authoring recorder is unavailable.")
        out_dir = self._out_dir or _pack_dir(self.config.data_dir, self._pack or "p.invalidpackid")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._recorder = factory(out_dir)
        self._recording = True
        self._secret_pause = False
        self._secret_type_recorded = False
        self.emit("status_update", self.status_dict() or {})
        return {"recording": True}

    def _click(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._coach_only():
            raise AuthoringCoachOnly("COACH_ONLY")
        with self._lock:
            if self._uncertain:
                raise AuthoringError("RECONCILIATION_REQUIRED")
        node_id = args.get("node_id")
        if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
            raise AuthoringError("stale_node")
        if self._node_table is None or self._recorder is None:
            raise AuthoringError("stale_node")
        row = self._node_table.get(node_id)
        if row is None:
            raise AuthoringError("stale_node")
        pixels = row["backend_pixels"]
        x = int(pixels["x"] + pixels["w"] / 2)
        y = int(pixels["y"] + pixels["h"] / 2)
        with self._lock:
            self._actuation_started = True
        try:
            self._recorder.click(x, y)
        except Exception:
            with self._lock:
                self._uncertain = True
            raise AuthoringError("RECONCILIATION_REQUIRED") from None
        finally:
            with self._lock:
                self._actuation_started = False
        return {"clicked": True}

    def _pause_for_input(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._recorder is None:
            raise AuthoringError("not_recording")
        node_id = args.get("node_id")
        param = args.get("param") or "note"
        if not isinstance(param, str) or _SAFE_PARAM.fullmatch(param) is None:
            raise AuthoringError("invalid_param")
        secret = bool(args.get("secret"))
        row = None
        if isinstance(node_id, str) and self._node_table is not None:
            row = self._node_table.get(node_id)
        if row is None:
            raise AuthoringError("stale_node")
        self._pause_target = {
            "node_id": row["node_id"],
            "backend_pixels": row["backend_pixels"],
            "param": param,
            "secret": secret,
        }
        if secret:
            self._secret_pause = True
        self._paused = True
        self.emit("status_update", self.status_dict() or {})
        return {"paused": True, "param": param}

    def _stop_record(self) -> dict[str, Any]:
        recorder = self._recorder
        if recorder is None:
            return {"recording": False}
        finish = getattr(recorder, "finish", None)
        if callable(finish):
            finish()
        self._recording = False
        self._paused = False
        self.emit("status_update", self.status_dict() or {})
        return {"recording": False}

    def _compile(self) -> dict[str, Any]:
        if self._secret_pause and not self._secret_type_recorded:
            if self.audit:
                self.audit.log("authoring_compile_refused_missing_type")
            raise AuthoringError("secret_type_missing")
        compile_recording = self._compile_recording
        workflow_id = "wf_local"
        if callable(compile_recording) and self._out_dir is not None:
            workflow = compile_recording(self._out_dir)
            workflow_id = str(
                getattr(workflow, "id", None) or getattr(workflow, "workflow_id", workflow_id)
            )
        if self._node_table is not None:
            self._node_table.clear()
        return {
            "status": "needs_human_admit",
            "workflow_id": workflow_id,
            "recording_retained": True,
        }

    def _halt(self, *, unsigned: bool, command_id: str | None) -> None:
        with self._lock:
            if self._actuation_started:
                self._uncertain = True
                uncertain = True
            else:
                uncertain = False
                self._recording = False
                self._paused = False
                self._recorder = None
                self._pause_target = None
                self._pause_command_id = None
                self._active_command_id = None
        if uncertain:
            self._callback(
                {
                    "command_id": command_id,
                    "status": "error",
                    "result": {"error": "RECONCILIATION_REQUIRED"},
                }
            )
            return
        if self._node_table is not None:
            self._node_table.clear()
        if unsigned and self._pack and self._lease_secret and self._transport:
            self._transport.callback(
                self._pack,
                self._lease_secret,
                {"halted": True, "status": "halted"},
            )
        self.emit("status_update", {"recording": False, "paused": False, "halted": True})

    def _callback(self, payload: dict[str, Any]) -> None:
        if self._transport is None or self._pack is None or self._lease_secret is None:
            return
        closed = {
            key: payload[key]
            for key in ("command_id", "status", "result", "halted")
            if key in payload
        }
        if "result" in closed:
            closed["result"] = _sanitize_result(closed["result"])
        self._transport.callback(self._pack, self._lease_secret, closed)

    def _callback_error(self, command_id: object, error: str) -> None:
        if not isinstance(command_id, str):
            return
        self._callback(
            {
                "command_id": command_id,
                "status": "error",
                "result": {"error": error},
            }
        )


class _NullAudit:
    def log(self, *_args: Any, **_kwargs: Any) -> None:
        return


def restore_authoring_runner(
    config: EngineConfig, pack_id: str, **kwargs: Any
) -> AuthoringRunner | None:
    """Rebuild an authoring runner from a stored lease without re-claiming."""

    if not valid_pack_id(pack_id):
        return None
    stored = load_authoring_lease(pack_id)
    if stored is None:
        return None
    runner = AuthoringRunner(config, **kwargs)
    runner._pack = stored["pack"]
    runner._lease_secret = stored["lease_secret"]
    runner._allowed_sub = stored.get("allowed_sub")
    runner._allowed_client_id = stored.get("allowed_client_id")
    hmac_key = _lease_hmac_key(stored["lease_secret"])
    runner._node_table = NodeTable(_pack_dir(config.data_dir, pack_id) / "nodes.json", hmac_key)
    runner._transport = AuthoringMailboxTransport(
        origin=stored["origin"],
        audit=kwargs.get("audit") or _NullAudit(),
        client=kwargs.get("client"),
    )
    return runner
