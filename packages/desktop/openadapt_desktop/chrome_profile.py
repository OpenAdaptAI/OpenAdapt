"""Desktop-managed Google Chrome profiles that recordings and runs attach to.

A person opts in to one named profile at a time, after reading a notice that
the engine serves (its SHA-256 is what they acknowledge). Desktop then opens
the person's installed Google Chrome on a separate user-data directory that
Desktop owns, with remote debugging on a port Chrome picks. The person signs
in to their sites in that window once, with SSO or two-factor sign-in as
usual, and Chrome keeps those sign-ins in that profile. Recording and runs
reach the window through the existing attach contract
(``browser_cdp_endpoint``; Flow's ``select_attached_page``).

Why this mechanism (see ``docs/CHROME_PROFILE.md``):

* Chrome ignores ``--remote-debugging-port`` on its default user-data
  directory, so attaching to the everyday profile isn't supported.
* Copying the everyday profile would copy its cookie and password stores,
  which is an export of both, and on macOS decrypting them needs the
  keychain. Desktop never does either.
* An extension with native messaging needs a store listing or enterprise
  policy, a native host registered per browser and OS, and its own consent
  path. It adds an always-installed component for the same reach.

Safety rules this module keeps:

* Nothing here reads, copies, or exports cookies, saved passwords, or any
  file inside the profile other than Chrome's ``DevToolsActivePort`` and its
  lock file. Nothing here calls the macOS keychain or passes a keychain flag.
* Only the official Google Chrome build is launched. Chrome for Testing and
  Chromium builds are refused.
* Fail closed: an unacknowledged or changed notice, a default Chrome
  directory, a profile that is already open, a Chrome that exits after launch
  (it handed the window to a running Chrome), or an endpoint that doesn't
  match the process Desktop started all refuse, and a launch that can't be
  verified is closed again.
* Desktop closes only the Chrome process it started, through the handle it
  holds: ``Browser.close`` over that process's own endpoint, then the handle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from openadapt_desktop import child_process
from openadapt_desktop.private_fs import ensure_private_dir

NOTICE_TITLE = "Use a Chrome profile for OpenAdapt"

NOTICE_TEXT = """\
OpenAdapt Desktop will create a separate Chrome profile on this computer and \
open Google Chrome with it. It doesn't use your everyday Chrome profile.

- Sign in to your sites once in that window. Chrome keeps those sign-ins in \
this profile, so later recordings and runs start already signed in.
- While OpenAdapt has the profile open, Chrome allows local debugging access \
to it. OpenAdapt uses that access to record and run your tasks in that \
window. Other programs on this computer could use the same access, so close \
the window when you're done.
- OpenAdapt never reads, copies, or exports this profile's cookies or saved \
passwords, and it never opens your keychain.
- OpenAdapt closes only the Chrome window it opened. If this profile is \
already open in another Chrome window, OpenAdapt won't attach to it.
- You can turn this off and delete the profile in Desktop at any time.
"""

_PROFILE_ID_PREFIX = "cp-"
_LEDGER_NAME = "profiles.json"
_LEDGER_SCHEMA = "openadapt.desktop.chrome_profiles.v1"
_DEVTOOLS_FILE = "DevToolsActivePort"
_START_TIMEOUT_S = 30.0
_CLOSE_TIMEOUT_S = 10.0
_MAX_NAME = 60


class ChromeProfileError(Exception):
    """A refusal with a closed ``code`` and a plain message for the window."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def notice() -> dict[str, str]:
    """The consent notice and the hash a person acknowledges."""

    body = f"{NOTICE_TITLE}\n\n{NOTICE_TEXT}"
    return {
        "title": NOTICE_TITLE,
        "text": NOTICE_TEXT,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


# ----------------------------------------------------------------- discovery


def chrome_candidates(
    platform: str = sys.platform, env: dict[str, str] | None = None
) -> list[Path]:
    """Install paths of the official Google Chrome stable build, in order."""

    env = dict(os.environ if env is None else env)
    if platform == "darwin":
        app = Path("Google Chrome.app") / "Contents" / "MacOS" / "Google Chrome"
        return [Path("/Applications") / app, Path.home() / "Applications" / app]
    if platform == "win32":
        tail = Path("Google") / "Chrome" / "Application" / "chrome.exe"
        roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")]
        return [Path(root) / tail for root in roots if root]
    return []


def default_user_data_dirs(
    platform: str = sys.platform, env: dict[str, str] | None = None
) -> list[Path]:
    """Chrome's own default user-data directories, which Desktop never uses."""

    env = dict(os.environ if env is None else env)
    if platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Google"
        return [base / name for name in ("Chrome", "Chrome Beta", "Chrome Canary", "Chrome Dev")]
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        if not local:
            return []
        base = Path(local) / "Google"
        return [
            base / name / "User Data"
            for name in ("Chrome", "Chrome Beta", "Chrome SxS", "Chrome Dev")
        ]
    return [Path.home() / ".config" / "google-chrome"]


def find_chrome(platform: str = sys.platform, env: dict[str, str] | None = None) -> Path:
    """The installed Google Chrome executable, or a refusal."""

    for candidate in chrome_candidates(platform, env):
        lowered = str(candidate).lower()
        if "for testing" in lowered or "chromium" in lowered:
            continue
        if candidate.is_file():
            return candidate
    raise ChromeProfileError(
        "chrome_not_installed",
        "Google Chrome isn't installed in its usual place on this computer. "
        "Install Google Chrome, then try again.",
    )


def windows_elevated() -> bool:
    """Whether this Windows process runs with administrator rights.

    An elevated process can't keep a handle on Chrome: Chrome restarts itself
    without administrator rights through Explorer and the process Desktop
    started exits, so the window can't be verified or closed by its handle.
    """

    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - unknown counts as elevated (fail closed)
        return True


# --------------------------------------------------------------- lock checks


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill on Windows terminates the process; never probe with it.
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def profile_in_use(user_data_dir: Path, platform: str = sys.platform) -> bool:
    """Whether a Chrome process holds this user-data directory.

    On macOS and Linux Chrome keeps a ``SingletonLock`` symlink whose target
    is ``<host>-<pid>``. On Windows it holds ``lockfile`` open without
    sharing, so opening it fails while Chrome runs. Anything this can't
    read counts as in use.
    """

    if platform == "win32":
        lock = user_data_dir / "lockfile"
        if not lock.exists():
            return False
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        os.close(fd)
        return False
    lock = user_data_dir / "SingletonLock"
    if not os.path.lexists(lock):
        return False
    try:
        target = os.readlink(lock)
    except OSError:
        return True
    host, _, pid_text = target.rpartition("-")
    if host != socket.gethostname():
        return True
    try:
        return _pid_alive(int(pid_text))
    except ValueError:
        return True


# ------------------------------------------------------------- CDP helpers


def read_devtools_active_port(user_data_dir: Path) -> tuple[int, str] | None:
    """Chrome's ``DevToolsActivePort`` file: the port and the browser path."""

    try:
        lines = (user_data_dir / _DEVTOOLS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    path = lines[1].strip()
    if not (0 < port < 65536) or not path.startswith("/devtools/browser/"):
        return None
    return port, path


def http_json_version(port: int, timeout: float = 2.0) -> dict[str, Any]:
    """GET ``/json/version`` from loopback, with no proxy."""

    import httpx

    with httpx.Client(trust_env=False, timeout=timeout) as client:
        response = client.get(f"http://127.0.0.1:{port}/json/version")
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("unexpected /json/version body")
    return data


def verify_endpoint(version: dict[str, Any], port: int, browser_path: str) -> None:
    """Require ``/json/version`` to name the browser Desktop started."""

    ws = str(version.get("webSocketDebuggerUrl") or "")
    parsed = urlsplit(ws)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ChromeProfileError(
            "attach_unverified", "Chrome's debugging address isn't local. Nothing was attached."
        )
    if parsed.port != port or parsed.path != browser_path:
        raise ChromeProfileError(
            "attach_unverified",
            "Chrome's debugging address doesn't belong to the window OpenAdapt opened. "
            "Nothing was attached.",
        )
    browser = str(version.get("Browser") or "")
    if not browser.startswith("Chrome/"):
        raise ChromeProfileError(
            "attach_unverified",
            "The browser on that address isn't Google Chrome. Nothing was attached.",
        )


def send_browser_close(port: int, browser_path: str, timeout: float = 3.0) -> None:
    """Send ``Browser.close`` over the browser WebSocket (no Origin header)."""

    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {browser_path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
        if not reply.startswith(b"HTTP/1.1 101"):
            raise ConnectionError("Chrome refused the WebSocket upgrade")
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode("utf-8")
        mask = secrets.token_bytes(4)
        header = bytes([0x81, 0x80 | len(payload)]) if len(payload) < 126 else b""
        if not header:
            header = bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(header + mask + masked)


# ------------------------------------------------------------------ service


@dataclass
class _Owned:
    """A Chrome process Desktop started, and how to reach and close it."""

    process: Any
    port: int
    browser_path: str

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class ChromeProfileService:
    """Opt-in records, launch, verification, and close for Chrome profiles."""

    def __init__(
        self,
        data_dir: Path,
        *,
        platform: str = sys.platform,
        env: dict[str, str] | None = None,
        find: Callable[[], Path] | None = None,
        spawn: Callable[..., Any] = child_process.popen,
        json_version: Callable[[int], dict[str, Any]] = http_json_version,
        browser_close: Callable[[int, str], None] = send_browser_close,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        start_timeout: float = _START_TIMEOUT_S,
        elevated: Callable[[], bool] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "chrome-profiles"
        self._platform = platform
        self._env = env
        self._find = find or (lambda: find_chrome(platform, env))
        self._spawn = spawn
        self._json_version = json_version
        self._browser_close = browser_close
        self._clock = clock
        self._sleep = sleep
        self._start_timeout = start_timeout
        self._elevated = elevated or (windows_elevated if platform == "win32" else (lambda: False))
        self._owned: dict[str, _Owned] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ ledger

    @property
    def _ledger_path(self) -> Path:
        return self.root / _LEDGER_NAME

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != _LEDGER_SCHEMA:
            return {}
        profiles = data.get("profiles")
        return profiles if isinstance(profiles, dict) else {}

    def _write(self, profiles: dict[str, dict[str, Any]]) -> None:
        ensure_private_dir(self.root, root=self.data_dir)
        tmp = self._ledger_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"schema": _LEDGER_SCHEMA, "profiles": profiles}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._ledger_path)

    def _user_data_dir(self, profile_id: str) -> Path:
        return self.root / profile_id / "user-data"

    def _record(self, profile_id: str) -> dict[str, Any]:
        record = self._read().get(str(profile_id))
        if record is None:
            raise ChromeProfileError(
                "profile_unknown", "That Chrome profile isn't set up in OpenAdapt."
            )
        if record.get("notice_sha256") != notice()["sha256"]:
            raise ChromeProfileError(
                "consent_required",
                "The notice for Chrome profiles changed. Read it and agree again "
                "before OpenAdapt opens this profile.",
            )
        return record

    # ----------------------------------------------------------- queries

    def _alive(self, profile_id: str) -> _Owned | None:
        owned = self._owned.get(profile_id)
        if owned is None:
            return None
        if owned.process.poll() is not None:
            self._owned.pop(profile_id, None)
            return None
        return owned

    def list(self) -> dict[str, Any]:
        current = notice()["sha256"]
        with self._lock:
            profiles = []
            for profile_id, record in sorted(self._read().items()):
                owned = self._alive(profile_id)
                profiles.append(
                    {
                        "profile_id": profile_id,
                        "name": record.get("name", ""),
                        "acknowledged_at": record.get("acknowledged_at"),
                        "consent_current": record.get("notice_sha256") == current,
                        "open": owned is not None,
                        "browser_cdp_endpoint": owned.endpoint if owned else None,
                    }
                )
        return {"profiles": profiles, "notice_sha256": current}

    # ------------------------------------------------------------ opt in

    def create(self, name: str, notice_sha256: str, acknowledged: bool) -> dict[str, Any]:
        """Record the person's opt-in and make the profile's empty directory."""

        if acknowledged is not True or notice_sha256 != notice()["sha256"]:
            raise ChromeProfileError(
                "consent_required",
                "OpenAdapt sets up a Chrome profile only after you read and agree to the notice.",
            )
        clean = " ".join(str(name or "").split())[:_MAX_NAME]
        if not clean:
            raise ChromeProfileError("name_required", "Give the Chrome profile a name.")
        with self._lock:
            profiles = self._read()
            profile_id = _PROFILE_ID_PREFIX + secrets.token_hex(4)
            user_data = self._user_data_dir(profile_id)
            self._refuse_default_dir(user_data)
            ensure_private_dir(self.root, root=self.data_dir)
            ensure_private_dir(user_data.parent, root=self.data_dir)
            ensure_private_dir(user_data, root=self.data_dir)
            profiles[profile_id] = {
                "name": clean,
                "notice_sha256": notice_sha256,
                "acknowledged_at": self._clock(),
            }
            self._write(profiles)
        return {"profile_id": profile_id, "name": clean}

    def acknowledge(self, profile_id: str, notice_sha256: str, acknowledged: bool) -> dict:
        """Agree again to a changed notice for an existing profile."""

        if acknowledged is not True or notice_sha256 != notice()["sha256"]:
            raise ChromeProfileError(
                "consent_required", "Read and agree to the notice to keep using this profile."
            )
        with self._lock:
            profiles = self._read()
            if profile_id not in profiles:
                raise ChromeProfileError(
                    "profile_unknown", "That Chrome profile isn't set up in OpenAdapt."
                )
            profiles[profile_id]["notice_sha256"] = notice_sha256
            profiles[profile_id]["acknowledged_at"] = self._clock()
            self._write(profiles)
        return self.list()

    def revoke(self, profile_id: str, *, delete_data: bool = False) -> dict[str, Any]:
        """Turn a profile off. With ``delete_data`` also delete its directory."""

        with self._lock:
            if self._alive(profile_id) is not None:
                raise ChromeProfileError(
                    "profile_open", "Close this profile's Chrome window first. Nothing changed."
                )
            profiles = self._read()
            if profile_id not in profiles:
                raise ChromeProfileError(
                    "profile_unknown", "That Chrome profile isn't set up in OpenAdapt."
                )
            user_data = self._user_data_dir(profile_id)
            if delete_data and profile_in_use(user_data, self._platform):
                raise ChromeProfileError(
                    "profile_in_use",
                    "This profile is open in a Chrome window. Close it first. Nothing changed.",
                )
            del profiles[profile_id]
            self._write(profiles)
            if delete_data:
                shutil.rmtree(self.root / profile_id, ignore_errors=True)
        return self.list()

    def _refuse_default_dir(self, user_data: Path) -> None:
        resolved = user_data.resolve()
        for default in default_user_data_dirs(self._platform, self._env):
            try:
                default_resolved = default.resolve()
            except OSError:
                continue
            if resolved == default_resolved or default_resolved in resolved.parents:
                raise ChromeProfileError(
                    "default_profile_refused",
                    "OpenAdapt doesn't use Chrome's own profile folder. Nothing was opened.",
                )

    # -------------------------------------------------------- open/close

    def open(self, profile_id: str, url: str | None = None) -> dict[str, Any]:
        """Open Google Chrome on the profile and return its verified endpoint."""

        if url is not None and url != "":
            parsed = urlsplit(str(url))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ChromeProfileError(
                    "url_invalid", "Enter a page address that starts with http:// or https://."
                )
        with self._lock:
            self._record(profile_id)
            owned = self._alive(profile_id)
            if owned is not None:
                self._verify(owned)
                return self._opened(profile_id, owned)
            user_data = self._user_data_dir(profile_id)
            self._refuse_default_dir(user_data)
            ensure_private_dir(user_data, root=self.data_dir)
            if profile_in_use(user_data, self._platform):
                raise ChromeProfileError(
                    "profile_in_use",
                    "This profile is already open in a Chrome window that OpenAdapt "
                    "didn't open. Close that window and try again. Nothing was attached.",
                )
            if self._elevated():
                raise ChromeProfileError(
                    "elevated_refused",
                    "OpenAdapt is running as administrator, so Chrome would restart "
                    "itself in a window OpenAdapt can't verify. Start OpenAdapt "
                    "without administrator rights and try again. Nothing was opened.",
                )
            chrome = self._find()
            stale = user_data / _DEVTOOLS_FILE
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            args = [
                str(chrome),
                f"--user-data-dir={user_data}",
                "--remote-debugging-port=0",
                "--no-first-run",
                "--no-default-browser-check",
                str(url) if url else "about:blank",
            ]
            process = self._spawn(args, **self._spawn_kwargs())
            try:
                owned = self._await_endpoint(process, user_data)
                self._verify(owned)
            except BaseException:
                self._stop_process(process, None)
                raise
            self._owned[profile_id] = owned
            logger.info("opened Chrome profile {} (pid {})", profile_id, process.pid)
            return self._opened(profile_id, owned)

    def _spawn_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if self._platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # No new session on POSIX: it would make Python fork the engine
        # (child_process.py). Desktop closes the window itself on exit.
        return kwargs

    def _await_endpoint(self, process: Any, user_data: Path) -> _Owned:
        deadline = self._clock() + self._start_timeout
        while True:
            if process.poll() is not None:
                raise ChromeProfileError(
                    "profile_in_use",
                    "Chrome handed this profile to a window that was already open, so "
                    "OpenAdapt can't verify it. Close every window of this profile and "
                    "try again. Nothing was attached.",
                )
            found = read_devtools_active_port(user_data)
            if found is not None:
                return _Owned(process=process, port=found[0], browser_path=found[1])
            if self._clock() >= deadline:
                raise ChromeProfileError(
                    "attach_unverified",
                    "Chrome didn't report its debugging address in time. Nothing was attached.",
                )
            self._sleep(0.2)

    def _verify(self, owned: _Owned) -> None:
        if owned.process.poll() is not None:
            raise ChromeProfileError(
                "attach_unverified", "The Chrome window OpenAdapt opened has closed."
            )
        try:
            version = self._json_version(owned.port)
        except ChromeProfileError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure is unverified
            raise ChromeProfileError(
                "attach_unverified",
                "OpenAdapt couldn't reach the Chrome window it opened. Nothing was attached.",
            ) from exc
        verify_endpoint(version, owned.port, owned.browser_path)

    @staticmethod
    def _opened(profile_id: str, owned: _Owned) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "open": True,
            "browser_cdp_endpoint": owned.endpoint,
        }

    def close(self, profile_id: str) -> dict[str, Any]:
        """Close the Chrome process Desktop opened for this profile, if any."""

        with self._lock:
            owned = self._owned.pop(profile_id, None)
            if owned is None or owned.process.poll() is not None:
                return {"profile_id": profile_id, "open": False, "closed": False}
            self._stop_process(owned.process, owned)
        return {"profile_id": profile_id, "open": False, "closed": True}

    def close_all(self) -> None:
        """Close every Chrome process Desktop opened (engine exit)."""

        with self._lock:
            for profile_id in list(self._owned):
                try:
                    self.close(profile_id)
                except Exception as exc:  # noqa: BLE001 - shutdown keeps going
                    logger.warning("closing Chrome profile failed: {}", type(exc).__name__)

    def _stop_process(self, process: Any, owned: _Owned | None) -> None:
        """Close one process Desktop started: CDP ``Browser.close``, then the handle."""

        if process.poll() is not None:
            return
        if owned is not None:
            try:
                # Only the endpoint that still names this process's browser.
                self._verify(owned)
                self._browser_close(owned.port, owned.browser_path)
            except Exception as exc:  # noqa: BLE001 - fall back to the handle
                logger.info("Chrome Browser.close failed: {}", type(exc).__name__)
        try:
            process.wait(timeout=_CLOSE_TIMEOUT_S if owned is not None else 0.1)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:  # noqa: BLE001
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chrome terminate failed: {}", type(exc).__name__)


# ------------------------------------------------------------- dispatcher


_SERVICES: list[ChromeProfileService] = []


def close_all_owned() -> None:
    """Engine exit: close only the Chrome processes Desktop opened."""

    for service in list(_SERVICES):
        service.close_all()


def dispatch_handlers(data_dir: Path) -> dict[str, Callable[..., dict]]:
    """The window-wire commands, keyed on the frontend ``engine.ts`` names.

    The service is built on first use. A refusal comes back as
    ``{"ok": False, "code", "error"}`` so the window can show it; nothing is
    attached or opened on a refusal. Neither the tray nor the local agent
    socket lists these commands.
    """

    built: list[ChromeProfileService] = []

    def service() -> ChromeProfileService:
        if not built:
            built.append(ChromeProfileService(data_dir))
            _SERVICES.append(built[0])
        return built[0]

    def call(action: Callable[[], dict]) -> dict:
        try:
            return {"ok": True, **action()}
        except ChromeProfileError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message}

    def text(params: dict[str, Any], key: str) -> str:
        return str(params.get(key) or "")

    return {
        "chrome_profile_notice": lambda **_p: notice(),
        "chrome_profile_list": lambda **_p: call(service().list),
        "chrome_profile_create": lambda **p: call(
            lambda: service().create(
                text(p, "name"), text(p, "notice_sha256"), p.get("acknowledged") is True
            )
        ),
        "chrome_profile_acknowledge": lambda **p: call(
            lambda: service().acknowledge(
                text(p, "profile_id"), text(p, "notice_sha256"), p.get("acknowledged") is True
            )
        ),
        "chrome_profile_open": lambda **p: call(
            lambda: service().open(text(p, "profile_id"), p.get("url") or None)
        ),
        "chrome_profile_close": lambda **p: call(lambda: service().close(text(p, "profile_id"))),
        "chrome_profile_revoke": lambda **p: call(
            lambda: service().revoke(
                text(p, "profile_id"), delete_data=p.get("delete_data") is True
            )
        ),
    }
