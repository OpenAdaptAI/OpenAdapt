"""Hosted runner shell for the strict OpenAdapt Flow adapter.

Desktop owns the authenticated HTTPS transport, the background loop, and local
operator status. Flow owns admission, parameter resolution, one-use delivery,
execution, and terminal verification. Desktop does not infer any of those
contracts from a report or an exit code.

The local-first product path remains independent. The runner is off by default,
and air-gapped storage mode prevents the loop from making a network request.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import random
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from loguru import logger

from engine.auth.store import (
    auth_header,
    clear_runner_credential,
    load_runner_credential,
    store_runner_registration_secure,
)
from engine.config import EngineConfig
from engine.flow_bridge import HostedRunnerAdapterUnavailableError

REGISTER_PATH = "/api/runners/register"
POLL_PATH = "/api/runners/poll"


def callback_path(run_id: str) -> str:
    """Return the terminal callback path for one strict run identifier."""

    return f"/api/runners/runs/{run_id}/callback"


def canonical_https_origin(host: str) -> str:
    """Require one lowercase HTTPS origin with no implicit-normalization drift."""

    if not isinstance(host, str):
        raise RunnerTransportError("The hosted runner origin is invalid.")
    try:
        parsed = urlsplit(host)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RunnerTransportError("The hosted runner origin is invalid.") from exc
    canonical = f"https://{parsed.netloc}"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname != parsed.hostname.lower()
        or parsed.netloc != parsed.netloc.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port == 443
        or host != canonical
    ):
        raise RunnerTransportError("The hosted runner origin is not canonical.")
    return canonical


def callback_url(host: str, run_id: str) -> str:
    """Return the exact canonical HTTPS callback URL for one run."""

    if not _UUID_V1_8.fullmatch(str(run_id)):
        raise RunnerTransportError("The callback target is invalid.")
    return f"{canonical_https_origin(host)}{callback_path(str(run_id))}"


def callback_origin(target: str, run_id: str) -> str:
    """Validate a retained callback URL and return its exact HTTPS origin."""

    try:
        parsed = urlsplit(target)
    except (TypeError, ValueError) as exc:
        raise RunnerJournalError("runner callback target is invalid") from exc
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        expected = callback_url(origin, run_id)
    except RunnerTransportError as exc:
        raise RunnerJournalError("runner callback target is invalid") from exc
    if target != expected:
        raise RunnerJournalError("runner callback target is invalid")
    return origin


DEFAULT_WAIT_S = 25
DEFAULT_LEASE_S = 900
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
STOP_JOIN_TIMEOUT_S = 2.0

_SAFE_LOCAL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_UUID_V1_8 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_RUNNER_TOKEN = re.compile(r"oar_[a-f0-9]{64}")
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


class RunnerTransportError(RuntimeError):
    """The authenticated Cloud transport did not confirm an operation."""


class ReauthRequired(RunnerTransportError):
    """The current enrollment or runner credential is no longer accepted."""


class RunnerSessionStale(RunnerTransportError):
    """The registered runner session or its admission binding is stale."""


class RunnerJournalError(RuntimeError):
    """The local observation journal is unsafe or corrupt."""


class RunnerTrustManifestError(RuntimeError):
    """The operator-authored runner trust manifest cannot admit hosted work."""


def _model_dump(model: Any) -> dict[str, Any]:
    """Return one strict model as JSON-ready data."""

    dump = getattr(model, "model_dump", None)
    if not callable(dump):
        raise TypeError("Flow returned an object outside the hosted-runner contract")
    value = dump(mode="json")
    if not isinstance(value, dict):
        raise TypeError("Flow returned a non-object hosted-runner model")
    return value


def _model_validate(model_type: Any, value: Any) -> Any:
    validate = getattr(model_type, "model_validate", None)
    if not callable(validate):
        raise TypeError("The bundled Flow hosted-runner model is incomplete")
    return validate(value)


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    resolved = getter() if callable(getter) else value
    if not isinstance(resolved, str) or not resolved:
        raise TypeError("The hosted dispatch has no delivery authority token")
    return resolved


def _wire_value(value: Any) -> str:
    """Return a string-backed wire enum without its Python enum name."""

    resolved = getattr(value, "value", value)
    if not isinstance(resolved, str) or not resolved:
        raise TypeError("Flow returned an empty hosted-runner wire value")
    return resolved


def _utc_expired(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    return datetime.now(timezone.utc) >= parsed.astimezone(timezone.utc)


def _is_windows() -> bool:
    return os.name == "nt"


def _require_private_windows_acl(descriptor: int) -> None:
    """Fail unless Flow proves that one opened Windows file is private."""

    try:
        from openadapt_flow.private_file import windows_descriptor_has_private_acl

        private = windows_descriptor_has_private_acl(descriptor)
    except Exception as exc:
        raise RunnerJournalError("Windows runner journal ACL verification is unavailable") from exc
    if not private:
        raise RunnerJournalError("Windows runner journal ACL is unsafe")


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Return bounded jittered backoff for a transport failure."""

    rng = rng or random
    maximum = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt)))
    return maximum * (0.5 + rng.random() / 2.0)


class RunnerJournal:
    """Durable observation journal for the runner screen.

    Flow's hosted adapter owns the one-use execution ledger. This journal never
    authorizes GUI execution. It protects the credential-bearing recovery
    binding and retains an exact PHI-free terminal callback until Cloud accepts
    it. Neither object is returned through runner status or written to a log.
    """

    _PHASES = frozenset(
        {
            "leased",
            "executing",
            "callback_pending",
            "reconciliation_required",
            "finished",
        }
    )

    def __init__(self, journal_dir: Path) -> None:
        self._dir = Path(journal_dir)
        self._lock = threading.RLock()

    def _secure_dir(self) -> None:
        try:
            self._dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            details = self._dir.lstat()
            if not stat.S_ISDIR(details.st_mode) or self._dir.is_symlink():
                raise RunnerJournalError("runner journal directory is unsafe")
            if not _is_windows() and details.st_uid != os.geteuid():
                raise RunnerJournalError("runner journal directory has a different owner")
            if not _is_windows() and details.st_mode & 0o077:
                self._dir.chmod(0o700)
                details = self._dir.lstat()
                if details.st_mode & 0o077:
                    raise RunnerJournalError("runner journal directory permissions are unsafe")
        except RunnerJournalError:
            raise
        except OSError as exc:
            raise RunnerJournalError("runner journal directory is unavailable") from exc

    def _path(self, dispatch_id: str) -> Path:
        if not _SAFE_LOCAL_ID.fullmatch(dispatch_id):
            raise RunnerJournalError("runner dispatch id is not a safe journal key")
        return self._dir / f"{dispatch_id}.json"

    def _opened_metadata(
        self,
        path: Path,
        descriptor: int,
        before: os.stat_result,
    ) -> os.stat_result:
        """Verify the identity and privacy of an already opened journal file."""

        try:
            opened = os.fstat(descriptor)
            after = path.lstat()
        except OSError as exc:
            raise RunnerJournalError("runner observation journal is unavailable") from exc
        identity_changed = any(
            candidate.st_dev != opened.st_dev or candidate.st_ino != opened.st_ino
            for candidate in (before, after)
        )
        if _is_windows():
            _require_private_windows_acl(descriptor)
            unsafe_permissions = False
        else:
            unsafe_permissions = (
                opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600
            )
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or identity_changed
            or unsafe_permissions
            or opened.st_nlink != 1
            or opened.st_size > MAX_JOURNAL_BYTES
        ):
            raise RunnerJournalError("runner observation journal is unsafe")
        return opened

    def get(self, dispatch_id: str) -> dict[str, Any] | None:
        path = self._path(dispatch_id)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RunnerJournalError("runner observation journal is unavailable") from exc
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = self._opened_metadata(path, descriptor, before)
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise RunnerJournalError(
                        "runner observation journal changed during the safe read"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise RunnerJournalError("runner observation journal changed during the safe read")
            after = os.fstat(descriptor)
            if any(
                (
                    getattr(after, field) != getattr(opened, field)
                    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                )
            ):
                raise RunnerJournalError("runner observation journal changed during the safe read")
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except RunnerJournalError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunnerJournalError("runner observation journal is corrupt") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(value, dict)
            or value.get("dispatch_id") != dispatch_id
            or value.get("phase") not in self._PHASES
        ):
            raise RunnerJournalError("runner observation journal has invalid state")
        return value

    def record(self, dispatch_id: str, phase: str, **fields: Any) -> None:
        if phase not in self._PHASES:
            raise RunnerJournalError("runner observation phase is invalid")
        with self._lock:
            self._secure_dir()
            value = self.get(dispatch_id) or {"dispatch_id": dispatch_id}
            value.update(fields)
            value["phase"] = phase
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_JOURNAL_BYTES:
                raise RunnerJournalError("runner observation journal entry is too large")
            descriptor, temporary = tempfile.mkstemp(
                dir=str(self._dir), prefix=f".{dispatch_id}.", suffix=".tmp"
            )
            temporary_path = Path(temporary)
            try:
                if not _is_windows():
                    os.fchmod(descriptor, 0o600)
                created = temporary_path.lstat()
                self._opened_metadata(temporary_path, descriptor, created)
                handle = os.fdopen(descriptor, "w", encoding="utf-8")
                descriptor = -1
                with handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                    self._opened_metadata(temporary_path, handle.fileno(), created)
                os.replace(temporary, self._path(dispatch_id))
                if self.get(dispatch_id) != value:
                    raise RunnerJournalError(
                        "runner observation journal changed during the safe write"
                    )
                if not _is_windows():
                    directory_descriptor = os.open(self._dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def clear_temporary(self, dispatch_id: str) -> None:
        """Remove private temporary copies after Cloud accepts the callback."""

        self._path(dispatch_id)
        with self._lock:
            self._secure_dir()
            for path in self._dir.glob(f".{dispatch_id}.*.tmp"):
                try:
                    before = path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RunnerJournalError("runner observation journal is unavailable") from exc
                descriptor = -1
                try:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(path, flags)
                    self._opened_metadata(path, descriptor, before)
                    os.unlink(path)
                except RunnerJournalError:
                    raise
                except OSError as exc:
                    raise RunnerJournalError(
                        "runner observation journal temporary file is unavailable"
                    ) from exc
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            if not _is_windows():
                directory_descriptor = os.open(self._dir, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)

    def entries(self) -> list[dict[str, Any]]:
        if not self._dir.exists():
            return []
        self._secure_dir()
        entries: list[dict[str, Any]] = []
        for path in sorted(
            self._dir.glob("*.json"),
            key=lambda item: item.lstat().st_mtime,
            reverse=True,
        ):
            entries.append(self.get(path.stem) or {})
        return entries

    def last_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        keep = (
            "dispatch_id",
            "run_id",
            "workflow_id",
            "phase",
            "outcome",
            "uncertain_delivery",
            "updated_at",
        )
        return [
            {key: entry[key] for key in keep if key in entry} for entry in self.entries()[:limit]
        ]


class HttpHostedRunnerTransport:
    """Authenticated HTTP implementation of Flow's transport protocol."""

    def __init__(
        self,
        *,
        host: str,
        contract: Any,
        enrollment_token: str,
        runner_token: str = "",
        audit: Any,
        client: httpx.Client | None = None,
    ) -> None:
        self.host = canonical_https_origin(host)
        self.contract = contract
        self._enrollment_token = enrollment_token
        self._runner_token = runner_token
        self._audit = audit
        if client is not None:
            try:
                client_origin = canonical_https_origin(str(client.base_url).removesuffix("/"))
            except RunnerTransportError as exc:
                raise RunnerTransportError(
                    "The hosted runner HTTP client has no exact protected origin."
                ) from exc
            if client_origin != self.host:
                raise RunnerTransportError(
                    "The hosted runner HTTP client differs from its protected origin."
                )
            renewal_header = self.contract.RUNNER_RENEWAL_HEADER
            if renewal_header in client.headers:
                raise RunnerTransportError(
                    "The hosted runner HTTP client retains a renewal credential."
                )
        self._client = client or httpx.Client(
            base_url=self.host,
            timeout=DEFAULT_WAIT_S + 35,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def set_runner_token(self, token: str) -> None:
        if not isinstance(token, str) or _RUNNER_TOKEN.fullmatch(token) is None:
            raise ReauthRequired("Cloud returned an invalid runner credential")
        self._runner_token = token

    def callback_target(self, run_id: str) -> str:
        """Return the exact callback URL used by this transport."""

        return callback_url(self.host, run_id)

    def _headers(self, *, enrollment: bool) -> dict[str, str]:
        token = self._enrollment_token if enrollment else self._runner_token
        if not token:
            raise ReauthRequired(
                "Connect this computer to OpenAdapt Cloud before you enable the runner."
            )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _audit_start(self, operation: str, path: str) -> None:
        self._audit.log(
            "hosted_runner_request",
            operation=operation,
            destination=self.host,
            path=path,
        )

    def _audit_finish(self, operation: str, path: str, status_code: int) -> None:
        self._audit.log(
            "hosted_runner_response",
            operation=operation,
            destination=self.host,
            path=path,
            status_code=status_code,
        )

    def _post(
        self,
        path: str,
        request: Any,
        *,
        response_type: Any,
        expected_status: int | tuple[int, ...],
        headers: dict[str, str],
        allow_empty: bool = False,
        response_status_by_http: dict[int, str] | None = None,
    ) -> Any:
        renewal_header = self.contract.RUNNER_RENEWAL_HEADER
        effective_headers = httpx.Headers(self._client.headers)
        effective_headers.update(headers)
        if renewal_header in effective_headers and path != REGISTER_PATH:
            raise RunnerTransportError(
                "The runner renewal credential is restricted to registration."
            )
        operation = path.rsplit("/", 1)[-1]
        self._audit_start(operation, path)
        try:
            response = self._client.post(
                path,
                json=_model_dump(request),
                headers=headers,
                follow_redirects=False,
            )
        except (httpx.HTTPError, OSError) as exc:
            self._audit.log(
                "hosted_runner_transport_failed",
                operation=operation,
                destination=self.host,
                path=path,
                error_type=type(exc).__name__,
            )
            raise RunnerTransportError(
                f"The hosted runner {operation} request did not complete."
            ) from exc
        self._audit_finish(operation, path, response.status_code)
        if response.status_code == 401:
            raise ReauthRequired("The hosted runner credential was rejected.")
        if response.status_code == 409:
            raise RunnerSessionStale("The hosted runner session or its admission binding is stale.")
        if (response.headers.get("cache-control") or "").strip().lower() != "no-store":
            raise RunnerTransportError(
                f"The hosted runner {operation} response was not marked no-store."
            )
        if allow_empty and response.status_code == 204:
            return None
        expected_statuses = (
            (expected_status,) if isinstance(expected_status, int) else expected_status
        )
        if response.status_code not in expected_statuses:
            raise RunnerTransportError(
                f"The hosted runner {operation} request returned HTTP {response.status_code}."
            )
        try:
            parsed = _model_validate(response_type, response.json())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunnerTransportError(
                f"The hosted runner {operation} response did not match the Flow contract."
            ) from exc
        if response_status_by_http is not None:
            expected_response_status = response_status_by_http[response.status_code]
            if _wire_value(getattr(parsed, "status", "")) != expected_response_status:
                raise RunnerTransportError(
                    f"The hosted runner {operation} response status did not match "
                    f"HTTP {response.status_code}."
                )
        return parsed

    def register(self, request: Any) -> Any:
        headers = self._headers(enrollment=True)
        try:
            headers.update(self.contract.registration_renewal_headers(self._runner_token))
        except ValueError:
            # An invalid retained credential cannot identify an existing runner.
            # Register a new identity without sending the malformed value.
            pass
        return self._post(
            REGISTER_PATH,
            request,
            response_type=self.contract.RegisterResponse,
            expected_status=201,
            headers=headers,
        )

    def poll(self, request: Any) -> Any | None:
        return self._post(
            POLL_PATH,
            request,
            response_type=self.contract.HostedDispatch,
            expected_status=200,
            headers=self._headers(enrollment=False),
            allow_empty=True,
        )

    def callback(self, run_id: str, request: Any) -> Any:
        run_id = str(run_id)
        if not _UUID_V1_8.fullmatch(run_id):
            raise RunnerTransportError("The callback has no canonical run UUID")
        target = self.callback_target(run_id)
        if callback_origin(target, run_id) != self.host:
            raise RunnerTransportError("The callback target differs from its transport.")
        return self._post(
            callback_path(run_id),
            request,
            response_type=self.contract.CallbackResponse,
            expected_status=(200, 202),
            headers=self._headers(enrollment=False),
            response_status_by_http={200: "duplicate", 202: "accepted"},
        )


def _platform_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(os.sys.platform, "linux")


def _capabilities() -> dict[str, Any]:
    current = _platform_name()
    backends = {
        "macos": ("web", "macos", "rdp", "rdp_window"),
        "windows": ("web", "windows", "rdp", "rdp_window", "citrix"),
    }.get(current, ("web", "linux"))
    return {
        "backends": backends,
        "attended": True,
        "effects_substrates": backends,
    }


def _flow_version() -> str:
    try:
        return version("openadapt-flow")
    except PackageNotFoundError:
        return "unknown"


class RunnerService:
    """Run the Cloud poll loop and delegate every dispatch to Flow."""

    def __init__(
        self,
        config: EngineConfig,
        services: Any,
        *,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        transport_factory: Callable[..., Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.services = services
        self.emit = emit or (lambda _event, _data: None)
        self._transport_factory = transport_factory or self._default_transport_factory
        self._rng = rng or random.Random()
        self.journal = RunnerJournal(config.data_dir / "runner" / "observations")
        self.runner_config = config.data_dir / "runner.toml"
        self._state = "disabled" if not config.runner_enabled else "offline"
        self._last_error: str | None = None
        self._last_seen_at: str | None = None
        self._attempt = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_generation = 0
        self._tick_lock = threading.Lock()
        self._contract: Any = None
        self._adapter: Any = None
        self._transport: Any = None
        self._registration: dict[str, Any] | None = None

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        try:
            self.emit("runner_state", self.status())
        except Exception:
            logger.exception("runner_state emit failed")

    def _set_error(self, message: str, *, state: str = "error") -> None:
        self._last_error = message
        self._set_state(state)

    def _runtime(self) -> tuple[Any, Any]:
        if self._contract is not None and self._adapter is not None:
            return self._contract, self._adapter
        bridge = self.services.flow_bridge
        contract = bridge.hosted_runner_contract()
        adapter = bridge.hosted_runner_adapter(
            self.config.data_dir / "runner" / "flow-one-use-ledger.sqlite3"
        )
        self._contract = contract
        self._adapter = adapter
        return contract, adapter

    def _default_transport_factory(
        self,
        *,
        contract: Any,
        enrollment_token: str,
        runner_token: str,
        host: str,
    ) -> HttpHostedRunnerTransport:
        return HttpHostedRunnerTransport(
            host=host,
            contract=contract,
            enrollment_token=enrollment_token,
            runner_token=runner_token,
            audit=self.services.audit,
        )

    def _registration_request(self, adapter: Any) -> tuple[Any, str]:
        from engine import __version__

        try:
            protected_origin = self._protected_runner_origin(adapter)
            configured_origin = canonical_https_origin(self.config.hosted_host)
            if protected_origin != configured_origin:
                raise RunnerTrustManifestError(
                    "The Desktop hosted origin differs from the protected runner host."
                )
            request = adapter.registration_request(
                runner_config=self.runner_config,
                name=_platform.node() or "desktop-runner",
                platform=_platform_name(),
                agent_version=__version__,
                engine_version=_flow_version(),
                mode="attended",
                capabilities=_capabilities(),
            )
            if self._protected_runner_origin(adapter) != configured_origin:
                raise RunnerTrustManifestError(
                    "The protected runner host changed during registration."
                )
            return request, configured_origin
        except RunnerTrustManifestError:
            raise
        except RunnerTransportError as exc:
            raise RunnerTrustManifestError(str(exc)) from exc
        except ValueError as exc:
            if str(exc) in {
                "hosted runner requires a protected runner host origin",
                "protected runner host origin is invalid",
                "protected runner host is not one canonical HTTPS origin",
            }:
                raise RunnerTrustManifestError(
                    f'Set [runner].host = "{self.config.hosted_host}" in '
                    f"{self.runner_config} before you enable hosted execution. "
                    "Desktop won't edit this operator trust manifest."
                ) from exc
            raise

    def _protected_runner_origin(self, adapter: Any) -> str:
        accessor = getattr(adapter, "protected_runner_origin", None)
        if not callable(accessor):
            raise HostedRunnerAdapterUnavailableError(
                "This Desktop build needs a newer bundled OpenAdapt Flow runtime."
            )
        try:
            origin = accessor(self.runner_config)
        except ValueError as exc:
            if str(exc) in {
                "hosted runner requires a protected runner host origin",
                "protected runner host origin is invalid",
                "protected runner host is not one canonical HTTPS origin",
            }:
                raise RunnerTrustManifestError(
                    f'Set [runner].host = "{self.config.hosted_host}" in '
                    f"{self.runner_config} before you enable hosted execution. "
                    "Desktop won't edit this operator trust manifest."
                ) from exc
            raise
        if not isinstance(origin, str):
            raise HostedRunnerAdapterUnavailableError(
                "The bundled OpenAdapt Flow hosted-runner contract is incomplete."
            )
        try:
            return canonical_https_origin(origin)
        except RunnerTransportError as exc:
            raise HostedRunnerAdapterUnavailableError(
                "The bundled OpenAdapt Flow hosted-runner contract is incomplete."
            ) from exc

    def _registration_is_current(self, stored: dict[str, Any], request: Any) -> bool:
        request_data = _model_dump(request)
        current_release = request_data.get("local_runtime_release")
        return (
            isinstance(current_release, dict)
            and stored.get("local_runtime_release") == current_release
            and not _utc_expired(stored.get("token_expires_at"))
            and isinstance(stored.get("runner_token"), str)
            and _RUNNER_TOKEN.fullmatch(stored["runner_token"]) is not None
        )

    def _connect(self) -> tuple[Any, Any, dict[str, Any]]:
        if self.config.storage_mode == "air-gapped":
            raise RunnerTransportError(
                "The hosted runner is unavailable while storage mode is air-gapped."
            )
        contract, adapter = self._runtime()
        request, origin = self._registration_request(adapter)
        stored = load_runner_credential(origin) or {}
        enrollment = auth_header(origin).get("Authorization", "")
        enrollment_token = enrollment.removeprefix("Bearer ") if enrollment else ""
        runner_token = str(stored.get("runner_token") or "")
        if self._registration_is_current(stored, request):
            if (
                self._transport is None
                or self._registration != stored
                or getattr(self._transport, "host", None) != origin
            ):
                transport = self._transport_factory(
                    contract=contract,
                    enrollment_token=enrollment_token,
                    runner_token=runner_token,
                    host=origin,
                )
                self._replace_transport(transport)
            self._registration = stored
            return contract, adapter, stored
        if not enrollment_token:
            raise ReauthRequired(
                "Connect this computer to OpenAdapt Cloud before you enable the runner."
            )
        transport = self._transport_factory(
            contract=contract,
            enrollment_token=enrollment_token,
            runner_token=runner_token,
            host=origin,
        )
        try:
            response = transport.register(request)
            response_data = _model_dump(response)
            registration = {
                **response_data,
                "local_runtime_release": _model_dump(request)["local_runtime_release"],
            }
            if not store_runner_registration_secure(origin, registration):
                raise RunnerTransportError(
                    "Desktop could not store the new runner credential in the OS keychain."
                )
            transport.set_runner_token(str(registration["runner_token"]))
        except Exception:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
            raise
        self._registration = registration
        self._replace_transport(transport)
        return contract, adapter, registration

    def _replace_transport(self, transport: Any) -> None:
        previous = self._transport
        self._transport = transport
        if previous is transport:
            return
        close = getattr(previous, "close", None)
        if callable(close):
            close()

    def _poll_request(self, contract: Any, registration: dict[str, Any]) -> Any:
        return contract.PollRequest(
            schema_version="openadapt.hosted-runner-poll/v1",
            runner_session_id=registration["runner_session_id"],
            wait_seconds=DEFAULT_WAIT_S,
            lease_seconds=DEFAULT_LEASE_S,
        )

    def status(self) -> dict[str, Any]:
        registration = self._registration or load_runner_credential(self.config.hosted_host) or {}
        return {
            "enabled": bool(self.config.runner_enabled),
            "state": self._state,
            "runner_id": registration.get("runner_id"),
            "registered": bool(registration.get("runner_token")),
            "host": self.config.hosted_host,
            "last_error": self._last_error,
            "last_seen_at": self._last_seen_at,
            "last_runs": self.journal.last_runs(),
        }

    def enable(self) -> dict[str, Any]:
        if self._thread and self._thread.is_alive() and self._stop.is_set():
            self._set_error(
                "The hosted runner is stopping after the current run reaches a terminal boundary.",
                state="stopping",
            )
            return self.status()
        self.config.runner_enabled = True
        if self.config.storage_mode == "air-gapped":
            self.config.runner_enabled = False
            self._set_error("The hosted runner is unavailable while storage mode is air-gapped.")
            return self.status()
        try:
            self._runtime()
        except HostedRunnerAdapterUnavailableError as exc:
            self._set_error(str(exc), state="incompatible")
            return self.status()
        self.start()
        return self.status()

    def disable(self) -> dict[str, Any]:
        self.config.runner_enabled = False
        if self.stop():
            self._set_state("disabled")
        return self.status()

    def deregister(self) -> None:
        clear_runner_credential(self.config.hosted_host)
        self._registration = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._lifecycle_generation += 1
            self._set_state("offline")
            self._thread = threading.Thread(
                target=self._thread_main,
                daemon=True,
                name="hosted-runner-loop",
            )
            self._thread.start()

    def _close_transport(self) -> None:
        transport = self._transport
        self._transport = None
        close = getattr(transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.exception("hosted runner transport close failed")

    def stop(self) -> bool:
        """Request a boundary-safe stop and report whether the loop has exited."""

        with self._lifecycle_lock:
            self._stop.set()
            self._lifecycle_generation += 1
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
        if thread and thread.is_alive():
            self._set_state("stopping")
            return False
        self._close_transport()
        return True

    def _thread_main(self) -> None:
        try:
            while not self._stop.is_set():
                delay = self.tick()
                if delay is None:
                    return
                if delay > 0:
                    self._stop.wait(delay)
        finally:
            self._close_transport()
            with self._lifecycle_lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            if self._stop.is_set() and not self.config.runner_enabled:
                self._set_state("disabled")

    def tick(self) -> float | None:
        """Poll and handle at most one dispatch. Tests call this directly."""

        with self._tick_lock:
            try:
                contract, adapter = self._runtime()
                self._prepare_interrupted_callbacks(contract, adapter)
                if self._send_pending_callbacks(contract):
                    self._last_seen_at = datetime.now(timezone.utc).isoformat()
                    self._attempt = 0
                    self._last_error = None
                    self._set_state("polling")
                    return 0.0
                contract, adapter, registration = self._connect()
                transport = self._transport
                if transport is None:
                    raise RunnerTransportError("The hosted runner transport is absent.")
                self._set_state("polling")
                with self._lifecycle_lock:
                    poll_generation = self._lifecycle_generation
                dispatch = transport.poll(self._poll_request(contract, registration))
                self._last_seen_at = datetime.now(timezone.utc).isoformat()
                self._attempt = 0
                self._last_error = None
                if dispatch is None:
                    return 0.0
                with self._lifecycle_lock:
                    accept_dispatch = (
                        poll_generation == self._lifecycle_generation
                        and not self._stop.is_set()
                        and self.config.runner_enabled
                    )
                if not accept_dispatch:
                    self.services.audit.log(
                        "hosted_runner_stale_dispatch_refused",
                        dispatch_id=str(dispatch.dispatch_id),
                        run_id=str(dispatch.run_id),
                    )
                    return None
                self._handle_dispatch(contract, adapter, transport, dispatch)
                return 0.0
            except HostedRunnerAdapterUnavailableError as exc:
                self._set_error(str(exc), state="incompatible")
                return None
            except RunnerTrustManifestError as exc:
                self._set_error(str(exc))
                return None
            except (ReauthRequired, RunnerSessionStale) as exc:
                self._set_error(str(exc), state="reauth_required")
                return None
            except RunnerTransportError as exc:
                self._set_error(str(exc), state="offline")
            except Exception as exc:
                logger.error(
                    "hosted runner stopped before a terminal callback ({kind})",
                    kind=type(exc).__name__,
                )
                self._set_error(
                    f"The hosted runner stopped before a terminal callback ({type(exc).__name__})."
                )
                return None
            delay = backoff_delay(self._attempt, self._rng)
            self._attempt += 1
            return delay

    def _handle_dispatch(
        self,
        contract: Any,
        adapter: Any,
        transport: Any,
        dispatch: Any,
    ) -> None:
        dispatch_id = str(dispatch.dispatch_id)
        run_id = str(dispatch.run_id)
        workflow_id = str(dispatch.workflow_id)
        existing = self.journal.get(dispatch_id)
        if existing is not None and existing.get("phase") == "finished":
            raise RunnerSessionStale(
                "Cloud replayed a terminal hosted dispatch. Re-enroll this runner."
            )
        recovery_binding = adapter.recovery_binding(dispatch)
        protected_origin = self._protected_runner_origin(adapter)
        configured_origin = canonical_https_origin(self.config.hosted_host)
        if (
            protected_origin != configured_origin
            or getattr(transport, "host", None) != protected_origin
        ):
            raise RunnerTrustManifestError(
                "The Desktop, Flow, and active transport origins do not match."
            )
        target = self._exact_callback_target(transport, run_id)
        self.journal.record(
            dispatch_id,
            "leased",
            run_id=run_id,
            workflow_id=workflow_id,
            callback_url=target,
            recovery_binding=_model_dump(recovery_binding),
        )
        self.journal.record(dispatch_id, "executing")
        self._set_state("running")
        authority = contract.DeliveryAuthority(
            str(dispatch.managed_delivery_authority_url),
            _secret_value(dispatch.delivery_authority_token),
        )
        run_dir = self.config.data_dir / "runner" / "runs" / run_id
        result = adapter.execute(
            dispatch,
            runner_config=self.runner_config,
            run_dir=run_dir,
            authority=authority,
        )
        outcome = _wire_value(getattr(result, "outcome", ""))
        uncertain = bool(getattr(result, "uncertain_delivery", False))
        callback = adapter.callback_request(dispatch, result)
        self.journal.record(
            dispatch_id,
            "callback_pending",
            outcome=outcome,
            uncertain_delivery=uncertain,
            callback=_model_dump(callback),
        )
        response = self._send_exact_callback(transport, run_id, target, callback)
        self._finish_callback(dispatch_id, response)
        self._set_state("polling")

    @staticmethod
    def _exact_callback_target(transport: Any, run_id: str) -> str:
        target_for = getattr(transport, "callback_target", None)
        if not callable(target_for):
            raise RunnerTransportError(
                "The hosted runner transport cannot bind an exact callback target."
            )
        target = target_for(run_id)
        if not isinstance(target, str):
            raise RunnerTransportError("The hosted runner callback target is invalid.")
        host = canonical_https_origin(getattr(transport, "host", None))
        if target != callback_url(host, run_id):
            raise RunnerTransportError(
                "The hosted runner callback target differs from its transport."
            )
        return target

    def _send_exact_callback(
        self,
        transport: Any,
        run_id: str,
        target: str,
        request: Any,
    ) -> Any:
        if self._exact_callback_target(transport, run_id) != target:
            raise RunnerTransportError("The hosted runner callback target changed after the lease.")
        return transport.callback(run_id, request)

    def _prepare_interrupted_callbacks(
        self,
        contract: Any,
        adapter: Any,
    ) -> int:
        """Convert every interrupted execution to an exact fail-safe callback."""

        prepared = 0
        for entry in self.journal.entries():
            if entry.get("phase") == "callback_pending":
                continue
            if entry.get("phase") not in {
                "leased",
                "executing",
                "reconciliation_required",
            }:
                continue
            binding_data = entry.get("recovery_binding")
            if not isinstance(binding_data, dict):
                raise RunnerJournalError("runner execution journal is missing its recovery binding")
            binding = _model_validate(contract.HostedRecoveryBinding, binding_data)
            interrupted_phase = str(entry["phase"])
            result = adapter.reconciliation_required(
                binding,
                code=f"desktop_interrupted_{interrupted_phase}",
            )
            outcome = _wire_value(getattr(result, "outcome", ""))
            uncertain = bool(getattr(result, "uncertain_delivery", False))
            started = bool(getattr(result, "started", False))
            if outcome != "RECONCILIATION_REQUIRED" or not uncertain or not started:
                raise RunnerJournalError("Flow returned an unsafe interrupted-run classification")
            dispatch_id = str(entry["dispatch_id"])
            run_id = str(entry.get("run_id") or "")
            target = entry.get("callback_url")
            if not isinstance(target, str):
                raise RunnerJournalError(
                    "runner execution journal is missing its exact callback target"
                )
            callback_origin(target, run_id)
            callback = adapter.callback_request(binding, result)
            self.journal.record(
                dispatch_id,
                "callback_pending",
                outcome=outcome,
                uncertain_delivery=True,
                callback=_model_dump(callback),
                recovery_binding=None,
            )
            self.services.audit.log(
                "hosted_runner_recovery_callback_pending",
                dispatch_id=dispatch_id,
                run_id=run_id,
                outcome=outcome,
            )
            prepared += 1
        return prepared

    def _callback_transport(self, contract: Any, origin: str) -> tuple[Any, bool]:
        current = self._transport
        if current is not None and getattr(current, "host", None) == origin:
            return current, False
        stored = load_runner_credential(origin) or {}
        runner_token = str(stored.get("runner_token") or "")
        if _RUNNER_TOKEN.fullmatch(runner_token) is None:
            raise ReauthRequired("The retained hosted callback has no matching runner credential.")
        enrollment = auth_header(origin).get("Authorization", "")
        enrollment_token = enrollment.removeprefix("Bearer ") if enrollment else ""
        transport = self._transport_factory(
            contract=contract,
            enrollment_token=enrollment_token,
            runner_token=runner_token,
            host=origin,
        )
        return transport, True

    def _send_pending_callbacks(self, contract: Any) -> bool:
        """Resend retained callbacks without polling or executing a dispatch."""

        sent = False
        for entry in self.journal.entries():
            if entry.get("phase") != "callback_pending":
                continue
            callback_data = entry.get("callback")
            run_id = entry.get("run_id")
            if not isinstance(callback_data, dict) or not isinstance(run_id, str):
                raise RunnerJournalError("runner callback journal is missing its exact request")
            target = entry.get("callback_url")
            if not isinstance(target, str):
                raise RunnerJournalError("runner callback journal is missing its exact target")
            origin = callback_origin(target, run_id)
            callback = _model_validate(contract.CallbackRequest, callback_data)
            transport, close_after = self._callback_transport(contract, origin)
            try:
                response = self._send_exact_callback(
                    transport,
                    run_id,
                    target,
                    callback,
                )
                self._finish_callback(str(entry["dispatch_id"]), response)
            finally:
                if close_after:
                    close = getattr(transport, "close", None)
                    if callable(close):
                        close()
            sent = True
        return sent

    def _finish_callback(self, dispatch_id: str, response: Any) -> None:
        entry = self.journal.get(dispatch_id)
        if entry is None or entry.get("phase") != "callback_pending":
            raise RunnerJournalError("runner callback journal is not pending")
        run_id = str(entry.get("run_id") or "")
        workflow_id = str(entry.get("workflow_id") or "")
        outcome = str(entry.get("outcome") or "")
        uncertain = bool(entry.get("uncertain_delivery", False))
        response_run_id = str(getattr(response, "run_id", ""))
        response_outcome = _wire_value(getattr(response, "outcome", ""))
        response_status = _wire_value(getattr(response, "status", ""))
        if (
            response_run_id != run_id
            or response_outcome != outcome
            or response_status not in {"accepted", "duplicate"}
        ):
            raise RunnerTransportError(
                "Cloud returned a callback result for a different hosted run."
            )
        run_dir = self.config.data_dir / "runner" / "runs" / run_id
        self.journal.clear_temporary(dispatch_id)
        self.journal.record(
            dispatch_id,
            "finished",
            callback=None,
            recovery_binding=None,
        )
        self._record_local_result(run_id, workflow_id, run_dir, outcome)
        self.services.audit.log(
            "hosted_runner_terminal",
            dispatch_id=dispatch_id,
            run_id=run_id,
            outcome=outcome,
            uncertain_delivery=uncertain,
        )

    def _record_local_result(
        self,
        run_id: str,
        workflow_id: str,
        run_dir: Path,
        outcome: str,
    ) -> None:
        """Mirror Flow's closed terminal outcome for the local operator."""

        try:
            self.services.db.insert_run(run_id, str(run_dir), bundle_id=None)
            self.services.db.update_run(run_id, status=outcome)
            if outcome in {"HALTED_BEFORE_EFFECT", "RECONCILIATION_REQUIRED"}:
                self.services.db.insert_halt(
                    f"halt-{run_id}",
                    run_id,
                    workflow_id=workflow_id,
                    reason=(
                        "Delivery requires reconciliation."
                        if outcome == "RECONCILIATION_REQUIRED"
                        else "Flow halted the hosted run before an effect."
                    ),
                )
        except Exception:
            logger.exception("local hosted-run mirror failed")
