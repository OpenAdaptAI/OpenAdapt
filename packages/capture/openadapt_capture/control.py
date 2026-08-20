"""Authenticated local control for an active Capture recorder.

The control endpoint is deliberately small.  It listens only on the IPv4
loopback interface, and every request and response is authenticated with a
per-session capability that exists only in an owner-only runtime file.  The
capability never appears in command-line arguments, logs, or recording
artifacts.

``status_recording`` and ``stop_recording`` are the public client contract used
by the OpenAdapt launcher, Flow, and Desktop.  They do not inspect Capture's
database or private recorder state.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

CONTROL_SCHEMA_VERSION = "openadapt.capture-control.v1"
TERMINAL_STATE_SCHEMA_VERSION = "openadapt.capture-terminal.v1"
TERMINAL_STATE_FILENAME = "capture-state.json"
_MAX_MESSAGE_BYTES = 64 * 1024
_REQUEST_CLOCK_SKEW_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_TIMEOUT_SECONDS = 15 * 60.0
_MAX_CONTROL_REQUEST_THREADS = 16


class CaptureControlError(RuntimeError):
    """The requested recorder control operation did not complete safely."""


class CaptureControlUnavailable(CaptureControlError):
    """No unambiguous live recorder session is available."""


class CaptureControlAuthenticationError(CaptureControlError):
    """The control peer or runtime descriptor could not be authenticated."""


@dataclass(frozen=True)
class RecorderStatus:
    """A privacy-bounded status snapshot returned by the recorder."""

    session_id: str
    pid: int
    process_started_at: float
    capture_dir: str
    phase: str
    ready: bool
    complete: bool
    integrity_verified: bool
    event_counts: dict[str, int]
    error_code: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RecorderStatus":
        try:
            counts_raw = payload["event_counts"]
            if not isinstance(counts_raw, dict):
                raise TypeError("event_counts must be an object")
            counts = {
                str(name): int(value)
                for name, value in counts_raw.items()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
            return cls(
                session_id=str(payload["session_id"]),
                pid=int(payload["pid"]),
                process_started_at=float(payload["process_started_at"]),
                capture_dir=str(payload["capture_dir"]),
                phase=str(payload["phase"]),
                ready=payload["ready"] is True,
                complete=payload["complete"] is True,
                integrity_verified=payload["integrity_verified"] is True,
                event_counts=counts,
                error_code=(
                    str(payload["error_code"]) if payload.get("error_code") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureControlAuthenticationError(
                "The recorder returned an invalid status payload."
            ) from exc


@dataclass(frozen=True)
class _ControlDescriptor:
    session_id: str
    pid: int
    process_started_at: float
    capture_dir: str
    host: str
    port: int
    created_at: float
    path: Path
    token: str = field(repr=False)

    def public_fields(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "session_id": self.session_id,
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "capture_dir": self.capture_dir,
            "endpoint": {"host": self.host, "port": self.port},
            "created_at": self.created_at,
        }

    def serialized(self) -> dict[str, Any]:
        fields = self.public_fields()
        fields["token"] = self.token
        fields["descriptor_mac"] = _descriptor_mac(self.token, fields)
        return fields


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _message_mac(token: str, value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "mac"}
    return hmac.new(
        token.encode("ascii"),
        _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()


def _descriptor_mac(token: str, value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "descriptor_mac"}
    return hmac.new(
        token.encode("ascii"),
        _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()


def _default_runtime_dir() -> Path:
    configured = os.environ.get("OPENADAPT_CAPTURE_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise CaptureControlUnavailable(
                "LOCALAPPDATA is unavailable. Capture cannot create an owner-only "
                "control directory."
            )
        return Path(local_app_data) / "OpenAdapt" / "Capture" / "control"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "openadapt" / "capture"
    try:
        uid = os.getuid()
    except AttributeError as exc:  # pragma: no cover - defensive platform guard
        raise CaptureControlUnavailable("The operating-system user is unavailable.") from exc
    return Path(tempfile.gettempdir()) / f"openadapt-capture-{uid}"


def _is_windows_reparse_point(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    attributes = get_attributes(str(path))
    if attributes == 0xFFFFFFFF:
        raise OSError(ctypes.get_last_error(), f"Cannot inspect {path}")
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _windows_current_user_sid() -> str:
    """Return the current process token's SID without invoking a shell."""

    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required))
        if not required.value:
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        if not sid_pointer:
            raise OSError("The current process token has no user SID")
        sid_string = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return sid_string.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _set_and_verify_windows_owner_acl(path: Path, *, _apply: bool = True) -> None:
    """Apply and verify a protected DACL containing only the current user.

    A best-effort ``chmod`` is not an authentication boundary on Windows.  This
    function uses a protected DACL and refuses the control channel if Windows
    cannot establish or verify it.
    """

    from ctypes import wintypes

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", wintypes.BYTE),
            ("ace_flags", wintypes.BYTE),
            ("ace_size", wintypes.WORD),
        ]

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", _AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sid = _windows_current_user_sid()
    sddl_revision_1 = 1
    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    if _apply:
        security_descriptor = ctypes.c_void_p()
        descriptor_size = wintypes.ULONG()
        sddl = f"O:{sid}D:P(A;;GA;;;{sid})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            sddl_revision_1,
            ctypes.byref(security_descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise OSError(ctypes.get_last_error(), "Cannot build the owner-only DACL")
        try:
            if not advapi32.SetFileSecurityW(
                str(path),
                owner_security_information
                | dacl_security_information
                | protected_dacl_security_information,
                security_descriptor,
            ):
                raise OSError(ctypes.get_last_error(), f"Cannot protect {path}")
        finally:
            kernel32.LocalFree(security_descriptor)

    current_sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(current_sid)):
        raise OSError(ctypes.get_last_error(), "Cannot parse the current-user SID")
    try:
        security_information = owner_security_information | dacl_security_information
        needed = wintypes.DWORD()
        advapi32.GetFileSecurityW(
            str(path),
            security_information,
            None,
            0,
            ctypes.byref(needed),
        )
        if not needed.value:
            raise OSError(ctypes.get_last_error(), f"Cannot inspect the DACL for {path}")
        current = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetFileSecurityW(
            str(path),
            security_information,
            current,
            needed,
            ctypes.byref(needed),
        ):
            raise OSError(ctypes.get_last_error(), f"Cannot inspect the DACL for {path}")

        owner_sid = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorOwner(
            current,
            ctypes.byref(owner_sid),
            ctypes.byref(owner_defaulted),
        ) or not owner_sid.value or not advapi32.EqualSid(owner_sid, current_sid):
            raise PermissionError(f"The current user does not own {path}")

        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            current,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise OSError(ctypes.get_last_error(), f"Cannot read the DACL for {path}")
        if not dacl_present.value or not dacl.value:
            raise PermissionError(f"The DACL for {path} is absent or unrestricted")

        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            current,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise OSError(ctypes.get_last_error(), f"Cannot inspect DACL control for {path}")
        if not control.value & 0x1000:  # SE_DACL_PROTECTED
            raise PermissionError(f"The DACL for {path} permits inherited access")

        acl_size = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(acl_size),
            ctypes.sizeof(acl_size),
            2,  # AclSizeInformation
        ):
            raise OSError(ctypes.get_last_error(), f"Cannot inspect DACL entries for {path}")
        if acl_size.ace_count != 1:
            raise PermissionError(f"The DACL for {path} is not current-user-only")

        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise OSError(ctypes.get_last_error(), f"Cannot inspect the DACL entry for {path}")
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
        if ace.header.ace_type != 0 or ace.header.ace_flags != 0:  # ACCESS_ALLOWED_ACE_TYPE
            raise PermissionError(f"The DACL for {path} has an invalid access entry")
        if ace.mask not in {0x10000000, 0x001F01FF}:  # GENERIC_ALL or FILE_ALL_ACCESS
            raise PermissionError(f"The DACL for {path} does not grant exact full access")
        ace_sid = ctypes.c_void_p(ace_pointer.value + _AccessAllowedAce.sid_start.offset)
        if not advapi32.EqualSid(ace_sid, current_sid):
            raise PermissionError(f"The DACL for {path} grants access to another identity")
    finally:
        kernel32.LocalFree(current_sid)


def _macos_extended_acl_present(descriptor: int, path: Path) -> bool:
    """Return whether a file descriptor has a valid non-empty macOS ACL."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    libc.acl_get_entry.restype = ctypes.c_int
    libc.acl_valid.argtypes = [ctypes.c_void_p]
    libc.acl_valid.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int

    ctypes.set_errno(0)
    acl = libc.acl_get_fd_np(descriptor, 0x00000100)  # ACL_TYPE_EXTENDED
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise OSError(error, f"Cannot inspect the extended ACL for {path}")
    try:
        if libc.acl_valid(acl) != 0:
            raise OSError(ctypes.get_errno(), f"The extended ACL for {path} is invalid")
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = libc.acl_get_entry(acl, 0, ctypes.byref(entry))  # ACL_FIRST_ENTRY
        if result == 0:
            return True
        error = ctypes.get_errno()
        if result == -1 and error == errno.EINVAL:
            return False
        raise OSError(error, f"Cannot inspect extended ACL entries for {path}")
    finally:
        libc.acl_free(acl)


def _clear_and_verify_macos_acl(descriptor: int, path: Path) -> None:
    """Remove all macOS extended ACL entries and verify their absence."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_init.argtypes = [ctypes.c_int]
    libc.acl_init.restype = ctypes.c_void_p
    libc.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.acl_set_fd_np.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int

    empty_acl = libc.acl_init(0)
    if not empty_acl:
        raise OSError(ctypes.get_errno(), f"Cannot allocate an empty ACL for {path}")
    try:
        if libc.acl_set_fd_np(descriptor, empty_acl, 0x00000100) != 0:
            raise OSError(ctypes.get_errno(), f"Cannot clear the extended ACL for {path}")
    finally:
        libc.acl_free(empty_acl)
    if _macos_extended_acl_present(descriptor, path):
        raise PermissionError(f"The extended ACL for {path} still grants access")


def _protect_path(path: Path, *, directory: bool) -> None:
    if sys.platform == "win32":
        if _is_windows_reparse_point(path):
            raise PermissionError(f"Refusing a reparse-point control path: {path}")
        _set_and_verify_windows_owner_acl(path)
        return

    mode = 0o700 if directory else 0o600
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_kind(details.st_mode):
            raise PermissionError(f"The control path has the wrong file type: {path}")
        if details.st_uid != os.getuid():
            raise PermissionError(f"The current user does not own {path}")
        os.fchmod(descriptor, mode)
        if sys.platform == "darwin":
            _clear_and_verify_macos_acl(descriptor, path)
        protected = os.fstat(descriptor)
        if stat.S_IMODE(protected.st_mode) != mode:
            raise PermissionError(
                f"Owner-only permissions could not be established for {path}"
            )
    finally:
        os.close(descriptor)


def _secure_runtime_dir(runtime_dir: str | os.PathLike[str] | None = None) -> Path:
    path = Path(runtime_dir).expanduser() if runtime_dir is not None else _default_runtime_dir()
    path = path.absolute()
    if path.is_symlink() or (path.exists() and _is_windows_reparse_point(path)):
        raise PermissionError(f"Refusing an indirect control directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _protect_path(path, directory=True)
    return path


def _write_json_atomic(path: Path, payload: dict[str, Any], *, owner_only: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PermissionError(f"Refusing an unsafe metadata path: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = _canonical_json(payload) + b"\n"
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if owner_only:
            _protect_path(temporary, directory=False)
        os.replace(temporary, path)
        if owner_only:
            _protect_path(path, directory=False)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_terminal_state(capture_dir: str | os.PathLike[str], payload: dict[str, Any]) -> Path:
    """Atomically persist non-secret terminal metadata in the capture."""

    path = Path(capture_dir) / TERMINAL_STATE_FILENAME
    _write_json_atomic(path, payload, owner_only=True)
    return path


def _read_json_bounded(path: Path, *, require_owner_only: bool) -> dict[str, Any]:
    if require_owner_only:
        _protect_path(path, directory=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if details.st_size > _MAX_MESSAGE_BYTES:
            raise CaptureControlAuthenticationError("The control descriptor is too large.")
        raw = os.read(descriptor, _MAX_MESSAGE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise CaptureControlAuthenticationError("The control descriptor is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureControlAuthenticationError(
            "The control descriptor is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise CaptureControlAuthenticationError("The control descriptor is invalid.")
    return payload


def _parse_descriptor(path: Path) -> _ControlDescriptor:
    payload = _read_json_bounded(path, require_owner_only=True)
    try:
        token = payload["token"]
        descriptor_mac = payload["descriptor_mac"]
        endpoint = payload["endpoint"]
        if not isinstance(token, str) or len(token) < 48:
            raise ValueError("invalid capability")
        if not isinstance(descriptor_mac, str) or not isinstance(endpoint, dict):
            raise ValueError("invalid authentication fields")
        if payload["schema_version"] != CONTROL_SCHEMA_VERSION:
            raise ValueError("unsupported schema")
        if not hmac.compare_digest(descriptor_mac, _descriptor_mac(token, payload)):
            raise ValueError("descriptor authentication failed")
        session_id = str(uuid.UUID(str(payload["session_id"])))
        if path.name != f"{session_id}.json":
            raise ValueError("descriptor filename does not match the session")
        host = str(endpoint["host"])
        if host != "127.0.0.1":
            raise ValueError("non-loopback endpoint")
        port = int(endpoint["port"])
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return _ControlDescriptor(
            session_id=session_id,
            pid=int(payload["pid"]),
            process_started_at=float(payload["process_started_at"]),
            capture_dir=str(payload["capture_dir"]),
            host=host,
            port=port,
            created_at=float(payload["created_at"]),
            path=path,
            token=token,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureControlAuthenticationError(
            f"The control descriptor {path.name!r} failed authentication."
        ) from exc


def _windows_process_live(pid: int, *, _kernel32: Any | None = None) -> bool | None:
    """Return exact Windows process signal state, or ``None`` if unknown."""

    from ctypes import wintypes

    kernel32 = _kernel32
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_invalid_parameter = 87

    handle = kernel32.OpenProcess(
        synchronize,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        return False if error == error_invalid_parameter else None
    try:
        result = int(kernel32.WaitForSingleObject(handle, 0))
        if result == wait_object_0:
            return False
        if result == wait_timeout:
            return True
        return None
    finally:
        kernel32.CloseHandle(handle)


def _process_instance_live(pid: int, process_started_at: float) -> bool:
    if pid <= 0:
        return False
    windows_live: bool | None = None
    if sys.platform == "win32":
        try:
            windows_live = _windows_process_live(pid)
        except OSError:
            windows_live = None
        if windows_live is False:
            # A signaled process object proves that no recorder is live at PID.
            # This check must precede psutil because Windows can retain the dead
            # object while denying later metadata queries for it.
            return False
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
        if actual != process_started_at:
            return False
        if sys.platform == "win32":
            # A live or unknown kernel result plus the exact creation identity is
            # conservatively live. Only a signaled object authorizes cleanup.
            return True
        if not process.is_running():
            return False
        if process.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
            return False
        try:
            process.wait(timeout=0)
        except psutil.TimeoutExpired:
            return True
        return False
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, OSError):
        # An uninspectable process is not proof of a stale endpoint.
        return True


def _mark_crashed_if_bound(descriptor: _ControlDescriptor) -> None:
    path = Path(descriptor.capture_dir) / TERMINAL_STATE_FILENAME
    try:
        payload = _read_json_bounded(path, require_owner_only=True)
    except (FileNotFoundError, OSError, CaptureControlError):
        return
    if (
        payload.get("schema_version") != TERMINAL_STATE_SCHEMA_VERSION
        or payload.get("session_id") != descriptor.session_id
        or payload.get("pid") != descriptor.pid
        or payload.get("process_started_at") != descriptor.process_started_at
        or payload.get("complete") is True
    ):
        return
    payload.update(
        {
            "phase": "crashed",
            "complete": False,
            "integrity_verified": False,
            "error_code": "recorder_process_exited",
            "finalized_at": time.time(),
        }
    )
    try:
        write_terminal_state(descriptor.capture_dir, payload)
    except OSError:
        return


def _remove_descriptor_if_exact(descriptor: _ControlDescriptor) -> bool:
    try:
        current = _parse_descriptor(descriptor.path)
    except (FileNotFoundError, CaptureControlError, OSError):
        return False
    if (
        current.session_id != descriptor.session_id
        or current.pid != descriptor.pid
        or current.process_started_at != descriptor.process_started_at
        or current.host != descriptor.host
        or current.port != descriptor.port
        or not hmac.compare_digest(current.token, descriptor.token)
    ):
        return False
    try:
        descriptor.path.unlink()
        return True
    except FileNotFoundError:
        return False


def discover_recorders(
    runtime_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return live session IDs and remove only proven-stale descriptors."""

    root = _secure_runtime_dir(runtime_dir)
    live: list[str] = []
    for path in sorted(root.glob("*.json")):
        try:
            descriptor = _parse_descriptor(path)
        except CaptureControlAuthenticationError:
            # An unauthenticated file cannot authorize deletion or discovery.
            continue
        if _process_instance_live(descriptor.pid, descriptor.process_started_at):
            live.append(descriptor.session_id)
            continue
        _mark_crashed_if_bound(descriptor)
        _remove_descriptor_if_exact(descriptor)
    return live


def _select_descriptor(
    session_id: str | None,
    runtime_dir: str | os.PathLike[str] | None,
) -> _ControlDescriptor:
    root = _secure_runtime_dir(runtime_dir)
    descriptors: list[_ControlDescriptor] = []
    for path in sorted(root.glob("*.json")):
        try:
            candidate = _parse_descriptor(path)
        except CaptureControlAuthenticationError:
            continue
        if not _process_instance_live(candidate.pid, candidate.process_started_at):
            _mark_crashed_if_bound(candidate)
            _remove_descriptor_if_exact(candidate)
            continue
        descriptors.append(candidate)

    if session_id is not None:
        try:
            normalized = str(uuid.UUID(session_id))
        except ValueError as exc:
            raise CaptureControlUnavailable("The Capture session ID is invalid.") from exc
        matches = [item for item in descriptors if item.session_id == normalized]
        if len(matches) != 1:
            raise CaptureControlUnavailable(
                f"No live Capture recorder has session ID {normalized}."
            )
        return matches[0]
    if not descriptors:
        raise CaptureControlUnavailable("No live Capture recorder was found.")
    if len(descriptors) > 1:
        ids = ", ".join(item.session_id for item in descriptors)
        raise CaptureControlUnavailable(
            f"More than one Capture recorder is active. Select a session ID: {ids}"
        )
    return descriptors[0]


def _recv_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= _MAX_MESSAGE_BYTES:
        block = connection.recv(min(4096, _MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if len(chunks) > _MAX_MESSAGE_BYTES:
        raise CaptureControlAuthenticationError("The control message is too large.")
    line, separator, remainder = bytes(chunks).partition(b"\n")
    if not separator or remainder:
        raise CaptureControlAuthenticationError("The control message framing is invalid.")
    return line


def _request(
    descriptor: _ControlDescriptor,
    command: str,
    *,
    timeout: float,
) -> RecorderStatus:
    timeout = float(timeout)
    if not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 0 and {_MAX_TIMEOUT_SECONDS} seconds")
    request_id = str(uuid.uuid4())
    request: dict[str, Any] = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "command": command,
        "session_id": descriptor.session_id,
        "pid": descriptor.pid,
        "process_started_at": descriptor.process_started_at,
        "request_id": request_id,
        "issued_at": time.time(),
        "timeout_seconds": timeout,
    }
    request["mac"] = _message_mac(descriptor.token, request)
    try:
        with socket.create_connection(
            (descriptor.host, descriptor.port), timeout=min(timeout + 2.0, _MAX_TIMEOUT_SECONDS)
        ) as connection:
            connection.settimeout(min(timeout + 2.0, _MAX_TIMEOUT_SECONDS))
            connection.sendall(_canonical_json(request) + b"\n")
            raw = _recv_line(connection)
    except (OSError, TimeoutError) as exc:
        if not _process_instance_live(descriptor.pid, descriptor.process_started_at):
            _mark_crashed_if_bound(descriptor)
            _remove_descriptor_if_exact(descriptor)
        raise CaptureControlUnavailable(
            "The Capture recorder control endpoint did not respond."
        ) from exc
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureControlAuthenticationError(
            "The recorder returned an invalid control response."
        ) from exc
    if not isinstance(response, dict):
        raise CaptureControlAuthenticationError("The control response is invalid.")
    response_mac = response.get("mac")
    if not isinstance(response_mac, str) or not hmac.compare_digest(
        response_mac, _message_mac(descriptor.token, response)
    ):
        raise CaptureControlAuthenticationError(
            "The recorder control response failed authentication."
        )
    if (
        response.get("schema_version") != CONTROL_SCHEMA_VERSION
        or response.get("request_id") != request_id
        or response.get("session_id") != descriptor.session_id
        or response.get("pid") != descriptor.pid
        or response.get("process_started_at") != descriptor.process_started_at
    ):
        raise CaptureControlAuthenticationError(
            "The recorder response does not match the requested process instance."
        )
    if response.get("ok") is not True:
        error_code = str(response.get("error_code") or "control_request_failed")
        raise CaptureControlError(f"Capture control failed: {error_code}")
    status = RecorderStatus.from_payload(response)
    if status.session_id != descriptor.session_id:
        raise CaptureControlAuthenticationError(
            "The recorder status belongs to a different session."
        )
    return status


def status_recording(
    session_id: str | None = None,
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
    timeout: float = 5.0,
) -> RecorderStatus:
    """Return the status of one exact active Capture session."""

    descriptor = _select_descriptor(session_id, runtime_dir)
    return _request(descriptor, "status", timeout=timeout)


def stop_recording(
    session_id: str | None = None,
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RecorderStatus:
    """Stop one exact recorder and return only after verified finalization.

    The stop operation is idempotent inside the recorder.  Concurrent or
    repeated requests share the same termination event and finalization result.
    A timeout or failed integrity check raises ``CaptureControlError`` and never
    returns a success-shaped status.
    """

    descriptor = _select_descriptor(session_id, runtime_dir)
    status = _request(descriptor, "stop", timeout=timeout)
    if not status.complete or not status.integrity_verified or status.phase != "complete":
        raise CaptureControlError(
            f"Capture stop did not produce a verified complete session ({status.phase})."
        )
    return status


class _LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._request_slots = threading.BoundedSemaphore(_MAX_CONTROL_REQUEST_THREADS)
        super().__init__(*args, **kwargs)

    def verify_request(self, request: socket.socket, client_address: tuple[str, int]) -> bool:
        return client_address[0] == "127.0.0.1"

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _ControlRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        owner: RecorderControlServer = self.server.owner  # type: ignore[attr-defined]
        owner._handle(self.request)


class RecorderControlServer:
    """Recorder-owned loopback server.  This is not the public client API."""

    def __init__(
        self,
        *,
        capture_dir: str,
        snapshot: Callable[[], dict[str, Any]],
        stop: Callable[[float], dict[str, Any]],
        session_id: str | None = None,
        runtime_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.capture_dir = str(Path(capture_dir).resolve())
        self.session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
        self.pid = os.getpid()
        self.process_started_at = psutil.Process(self.pid).create_time()
        self._snapshot = snapshot
        self._stop = stop
        self._runtime_dir_arg = runtime_dir
        self._token = secrets.token_urlsafe(48)
        self._server: _LoopbackServer | None = None
        self._thread: threading.Thread | None = None
        self._descriptor: _ControlDescriptor | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._seen_requests: dict[str, float] = {}
        self._seen_lock = threading.Lock()

    @property
    def descriptor_path(self) -> Path | None:
        return self._descriptor.path if self._descriptor is not None else None

    def start(self) -> "RecorderControlServer":
        runtime_dir = _secure_runtime_dir(self._runtime_dir_arg)
        server = _LoopbackServer(("127.0.0.1", 0), _ControlRequestHandler)
        server.owner = self  # type: ignore[attr-defined]
        host, port = server.server_address
        descriptor = _ControlDescriptor(
            session_id=self.session_id,
            pid=self.pid,
            process_started_at=self.process_started_at,
            capture_dir=self.capture_dir,
            host=str(host),
            port=int(port),
            created_at=time.time(),
            path=runtime_dir / f"{self.session_id}.json",
            token=self._token,
        )
        try:
            _write_json_atomic(descriptor.path, descriptor.serialized(), owner_only=True)
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._descriptor = descriptor
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"capture-control-{self.session_id[:8]}",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            _remove_descriptor_if_exact(descriptor)
            server.server_close()
            self._server = None
            self._descriptor = None
            self._thread = None
            self._closed = True
            self._token = ""
            raise
        return self

    def _authenticate_request(self, request: dict[str, Any]) -> tuple[str, float]:
        supplied_mac = request.get("mac")
        if not isinstance(supplied_mac, str) or not hmac.compare_digest(
            supplied_mac, _message_mac(self._token, request)
        ):
            raise CaptureControlAuthenticationError("authentication_failed")
        try:
            issued_at = float(request["issued_at"])
            request_id = str(uuid.UUID(str(request["request_id"])))
            timeout = float(request["timeout_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureControlAuthenticationError("invalid_request") from exc
        if abs(time.time() - issued_at) > _REQUEST_CLOCK_SKEW_SECONDS:
            raise CaptureControlAuthenticationError("expired_request")
        if not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
            raise CaptureControlAuthenticationError("invalid_timeout")
        if (
            request.get("schema_version") != CONTROL_SCHEMA_VERSION
            or request.get("session_id") != self.session_id
            or request.get("pid") != self.pid
            or request.get("process_started_at") != self.process_started_at
        ):
            raise CaptureControlAuthenticationError("recorder_instance_mismatch")
        with self._seen_lock:
            if request_id in self._seen_requests:
                raise CaptureControlAuthenticationError("replayed_request")
            oldest_valid = time.time() - _REQUEST_CLOCK_SKEW_SECONDS
            self._seen_requests = {
                seen_id: seen_at
                for seen_id, seen_at in self._seen_requests.items()
                if seen_at >= oldest_valid
            }
            if len(self._seen_requests) >= 4096:
                # Never clear still-valid request IDs. Saturation must fail
                # closed instead of reopening the replay window.
                raise CaptureControlAuthenticationError("request_limit")
            self._seen_requests[request_id] = issued_at
        return request_id, timeout

    def _response(
        self,
        request_id: str,
        *,
        ok: bool,
        status: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "request_id": request_id,
            "session_id": self.session_id,
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "ok": ok,
        }
        if status:
            for key in (
                "capture_dir",
                "phase",
                "ready",
                "complete",
                "integrity_verified",
                "event_counts",
                "error_code",
            ):
                if key in status:
                    response[key] = status[key]
        if error_code:
            response["error_code"] = error_code
        response["mac"] = _message_mac(self._token, response)
        return response

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(5.0)
        request_id = str(uuid.uuid4())
        try:
            raw = _recv_line(connection)
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise CaptureControlAuthenticationError("invalid_request")
            request_id, timeout = self._authenticate_request(request)
            command = request.get("command")
            if command == "status":
                status = self._snapshot()
            elif command == "stop":
                status = self._stop(timeout)
            else:
                raise CaptureControlAuthenticationError("unsupported_command")
            complete = status.get("complete") is True
            verified = status.get("integrity_verified") is True
            clean_completion = (
                complete
                and verified
                and status.get("phase") == "complete"
                and status.get("error_code") is None
            )
            if command == "stop" and not clean_completion:
                response = self._response(
                    request_id,
                    ok=False,
                    status=status,
                    error_code=str(status.get("error_code") or "finalization_incomplete"),
                )
            else:
                response = self._response(request_id, ok=True, status=status)
        except CaptureControlAuthenticationError:
            # Do not reveal whether a token, session, or process field was wrong.
            response = self._response(
                request_id,
                ok=False,
                error_code="authentication_failed",
            )
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
            response = self._response(
                request_id,
                ok=False,
                error_code="invalid_request",
            )
        try:
            connection.sendall(_canonical_json(response) + b"\n")
        except OSError:
            pass

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            server = self._server
            descriptor = self._descriptor
            if server is not None:
                server.shutdown()
                server.server_close()
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=5.0)
            if descriptor is not None:
                _remove_descriptor_if_exact(descriptor)
            self._token = ""

    def __enter__(self) -> "RecorderControlServer":
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


__all__ = [
    "CONTROL_SCHEMA_VERSION",
    "TERMINAL_STATE_FILENAME",
    "TERMINAL_STATE_SCHEMA_VERSION",
    "CaptureControlAuthenticationError",
    "CaptureControlError",
    "CaptureControlUnavailable",
    "RecorderStatus",
    "discover_recorders",
    "status_recording",
    "stop_recording",
]
