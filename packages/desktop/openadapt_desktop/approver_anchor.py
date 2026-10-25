"""The approver root an administrator pins, in a file this user can't write.

A person's answer counts only when it is signed by a key on Flow's signed
approver roster, and the roster is only as good as the root Desktop pins. A
pin this user's account can write (a Keychain item, a file under the home
folder, an environment variable) is one an agent running as this user can
replace with its own root, and then approve its own saves. This module reads a
pin that needs an administrator to write:

* macOS: ``/Library/Application Support/OpenAdapt/approver-root.json``
* Linux: ``/etc/openadapt/approver-root.json``
* Windows: ``%ProgramData%\\OpenAdapt\\approver-root.json`` (the known
  ProgramData folder, never the environment variable)

:func:`read_anchor` trusts the file only when the file and every folder above
it are owned by the administrator (root, or Administrators/SYSTEM on Windows),
none of them is a symlink (or reparse point), and nobody else can write any of
them. Anything else raises :class:`AnchorError`: Desktop then counts no
person's answer. :func:`write_anchor` is the elevated ``openadapt-desktop
approver-pin`` command.

An administrator (or a process with a cached ``sudo``) can still change it;
that is the boundary this file draws, not more.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

ANCHOR_NAME = "approver-root.json"
#: The largest anchor file read; a root pin is about 100 bytes.
_MAX_BYTES = 4096

Code = Literal["approver_anchor_untrusted", "approver_anchor_unreadable", "not_elevated"]


class AnchorError(RuntimeError):
    """The anchor exists but can't be trusted; ``code`` says why."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code: Code = code


def anchor_dir(platform: str | None = None) -> Path:
    """The administrator-owned folder that holds the anchor on ``platform``."""

    platform = platform or sys.platform
    if platform == "darwin":
        return Path("/Library/Application Support/OpenAdapt")
    if platform == "win32":
        return _program_data() / "OpenAdapt"
    return Path("/etc/openadapt")


def anchor_path(platform: str | None = None) -> Path:
    return anchor_dir(platform) / ANCHOR_NAME


def normalize_root(value: object) -> str:
    """A 32-byte Ed25519 public key as 64 lowercase hex digits, or raise."""

    text = str(value or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise AnchorError(
            "approver_anchor_unreadable",
            "The administrator's approver pin isn't a 64-digit hex public key.",
        )
    return text


def read_anchor(path: Path | None = None, *, trusted_uids: Iterable[int] = (0,)) -> str | None:
    """The administrator's pinned root, or ``None`` when no anchor exists.

    Raises :class:`AnchorError` when an anchor (or its folder) exists but
    this user could have written it, or it doesn't parse. ``trusted_uids``
    is for tests: real callers trust only root.
    """

    return _read_anchor(Path(os.path.abspath(path or anchor_path())), frozenset(trusted_uids))


def _read_anchor(path: Path, trusted_uids: frozenset[int]) -> str | None:
    if not os.path.lexists(path):
        return None
    if sys.platform == "win32":
        data = _read_windows(path)
    else:
        data = _read_posix(path, trusted_uids)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AnchorError(
            "approver_anchor_unreadable", f"{path} isn't the approver pin's JSON."
        ) from None
    if not isinstance(parsed, dict):
        raise AnchorError("approver_anchor_unreadable", f"{path} isn't the approver pin's JSON.")
    return normalize_root(parsed.get("root_public_key"))


def write_anchor(
    root_public_key: str,
    path: Path | None = None,
    *,
    trusted_uids: Iterable[int] = (0,),
) -> Path:
    """Pin ``root_public_key`` for this computer. Needs an administrator.

    POSIX: run as root; the folder is created 0755 and the file 0644, both
    owned by root, written atomically without following links. Windows: run
    elevated; the folder's ACL is replaced so only Administrators and SYSTEM
    can change it. The result is read back through :func:`read_anchor`.
    """

    root = normalize_root(root_public_key)
    path = Path(os.path.abspath(path or anchor_path()))
    payload = (json.dumps({"root_public_key": root}, indent=2) + "\n").encode("utf-8")
    uids = frozenset(trusted_uids)
    if sys.platform == "win32":
        _require_windows_admin()
        _write_windows(path, payload)
    else:
        if os.geteuid() not in uids:
            raise AnchorError(
                "not_elevated",
                "Pinning the approver root needs an administrator: run it with sudo.",
            )
        _write_posix(path, payload, uids)
    pinned = _read_anchor(path, uids)
    if pinned != root:
        raise AnchorError("approver_anchor_untrusted", f"{path} didn't read back as {root}.")
    return path


# -- POSIX ------------------------------------------------------------------------


def _check_posix_dirs(directory: Path, trusted_uids: frozenset[int]) -> None:
    """Every folder from ``/`` to ``directory``: a real folder only the
    administrator can change (a sticky world-writable folder such as /tmp is
    allowed above it, since its other users can't replace root's entries)."""

    chain = [Path(directory.anchor)]
    for part in directory.parts[1:]:
        chain.append(chain[-1] / part)
    for current in chain:
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise AnchorError(
                "approver_anchor_unreadable", f"Can't check {current}: {exc}"
            ) from None
        if stat.S_ISLNK(info.st_mode):
            raise AnchorError(
                "approver_anchor_untrusted", f"{current} is a symlink; the pin isn't trusted."
            )
        if not stat.S_ISDIR(info.st_mode):
            raise AnchorError("approver_anchor_untrusted", f"{current} isn't a folder.")
        if info.st_uid not in trusted_uids:
            raise AnchorError(
                "approver_anchor_untrusted",
                f"{current} isn't owned by the administrator; the pin isn't trusted.",
            )
        writable_by_others = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable_by_others and not (info.st_mode & stat.S_ISVTX and current != directory):
            raise AnchorError(
                "approver_anchor_untrusted",
                f"Others can write {current}; the pin isn't trusted.",
            )


def _check_posix_file(info: os.stat_result, path: Path, trusted_uids: frozenset[int]) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise AnchorError("approver_anchor_untrusted", f"{path} isn't a regular file.")
    if info.st_uid not in trusted_uids:
        raise AnchorError(
            "approver_anchor_untrusted",
            f"{path} isn't owned by the administrator; the pin isn't trusted.",
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AnchorError(
            "approver_anchor_untrusted", f"Others can write {path}; the pin isn't trusted."
        )
    if info.st_nlink != 1:
        raise AnchorError(
            "approver_anchor_untrusted", f"{path} has other hard links; the pin isn't trusted."
        )


def _read_posix(path: Path, trusted_uids: frozenset[int]) -> bytes:
    _check_posix_dirs(path.parent, trusted_uids)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AnchorError(
            "approver_anchor_untrusted", f"Can't open {path} without following links: {exc}"
        ) from None
    try:
        _check_posix_file(os.fstat(fd), path, trusted_uids)
        data = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_BYTES:
        raise AnchorError("approver_anchor_unreadable", f"{path} is too large to be the pin.")
    return data


def _write_posix(path: Path, payload: bytes, trusted_uids: frozenset[int]) -> None:
    directory = path.parent
    missing: list[Path] = []
    probe = directory
    while not os.path.lexists(probe):
        missing.append(probe)
        probe = probe.parent
    _check_posix_dirs(probe, trusted_uids)
    for folder in reversed(missing):
        os.mkdir(folder, 0o755)
        os.chmod(folder, 0o755)
    _check_posix_dirs(directory, trusted_uids)
    temp = directory / f".{ANCHOR_NAME}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o644)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# -- Windows ----------------------------------------------------------------------

#: Owners and writers an administrator-only pin may have: Administrators,
#: SYSTEM, TrustedInstaller.
TRUSTED_SIDS = frozenset(
    {
        "S-1-5-32-544",
        "S-1-5-18",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x08
#: Rights that change a file or folder, or what it contains: write/append
#: data (add file/subfolder), write EA/attributes, delete child, DELETE,
#: WRITE_DAC, WRITE_OWNER, GENERIC_ALL, GENERIC_WRITE.
_WRITE_RIGHTS = (
    0x0002
    | 0x0004
    | 0x0010
    | 0x0040
    | 0x0100
    | 0x00010000
    | 0x00040000
    | 0x00080000
    | 0x10000000
    | 0x40000000
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class Ace:
    """One entry of a DACL, as :func:`dacl_admin_only` needs it."""

    ace_type: int
    flags: int
    mask: int
    sid: str


def dacl_admin_only(owner_sid: str | None, aces: list[Ace] | None) -> str | None:
    """Why this security descriptor lets someone other than an administrator
    change the object, or ``None`` when only administrators can.

    ``aces is None`` is a NULL DACL: everyone has full control.
    """

    if owner_sid not in TRUSTED_SIDS:
        return f"it is owned by {owner_sid}, not an administrator"
    if aces is None:
        return "it has no access control list, so everyone can change it"
    for ace in aces:
        if ace.ace_type != _ACCESS_ALLOWED_ACE_TYPE or ace.flags & _INHERIT_ONLY_ACE:
            continue
        if ace.mask & _WRITE_RIGHTS and ace.sid not in TRUSTED_SIDS:
            return f"{ace.sid} can change it"
    return None


def _check_windows(path: Path) -> None:
    for target in (path.parent, path):
        if not os.path.lexists(target):
            continue
        attributes = getattr(os.lstat(target), "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise AnchorError(
                "approver_anchor_untrusted", f"{target} is a link; the pin isn't trusted."
            )
        owner, aces = _windows_security(target)
        reason = dacl_admin_only(owner, aces)
        if reason:
            raise AnchorError("approver_anchor_untrusted", f"{target} isn't trusted: {reason}.")


def _read_windows(path: Path) -> bytes:  # pragma: no cover - Windows only
    _check_windows(path)
    with open(path, "rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise AnchorError("approver_anchor_unreadable", f"{path} is too large to be the pin.")
    # Checked again after the read: a swap between check and read is refused.
    _check_windows(path)
    return data


def _windows_security(target: Path) -> tuple[str | None, list[Ace] | None]:  # pragma: no cover
    import ctypes
    from ctypes import wintypes

    advapi = _windll("advapi32", use_last_error=True)
    kernel = _windll("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    owner_info, dacl_info = 0x1, 0x4
    status = advapi.GetNamedSecurityInfoW(
        wintypes.LPCWSTR(str(target)),
        1,  # SE_FILE_OBJECT
        owner_info | dacl_info,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        raise AnchorError(
            "approver_anchor_unreadable", f"Can't read the security of {target} ({status})."
        )
    try:
        owner_sid = _sid_string(advapi, kernel, owner)
        if not dacl.value:
            return owner_sid, None

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        size = AclSizeInformation()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise AnchorError("approver_anchor_unreadable", f"Can't read the ACL of {target}.")
        aces: list[Ace] = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise AnchorError("approver_anchor_unreadable", f"Can't read the ACL of {target}.")
            address = int(ace.value or 0)
            if not address:
                raise AnchorError("approver_anchor_unreadable", f"Can't read the ACL of {target}.")
            header = (ctypes.c_ubyte * 4).from_address(address)
            mask = wintypes.DWORD.from_address(address + 4).value
            sid = ctypes.c_void_p(address + 8)
            aces.append(Ace(header[0], header[1], mask, _sid_string(advapi, kernel, sid)))
        return owner_sid, aces
    finally:
        kernel.LocalFree(descriptor)


def _windll(name: str, **kwargs: object):  # pragma: no cover - Windows only
    import ctypes

    return getattr(ctypes, "WinDLL")(name, **kwargs)


def _sid_string(advapi, kernel, sid) -> str:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise AnchorError("approver_anchor_unreadable", "Can't read a security identifier.")
    try:
        return str(text.value)
    finally:
        kernel.LocalFree(text)


def _program_data() -> Path:  # pragma: no cover - Windows only
    """The ProgramData known folder; never ``%ProgramData%``, which whoever
    starts the process sets."""

    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder = Guid(
        0x62AB5D82,
        0xFDC1,
        0x4DC3,
        (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97),
    )
    shell = _windll("shell32")
    ole = _windll("ole32")
    result = ctypes.c_wchar_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(result)) != 0:
        raise AnchorError("approver_anchor_unreadable", "Can't find the ProgramData folder.")
    try:
        return Path(str(result.value))
    finally:
        ole.CoTaskMemFree(result)


def _require_windows_admin() -> None:  # pragma: no cover - Windows only

    if not _windll("shell32").IsUserAnAdmin():
        raise AnchorError(
            "not_elevated",
            "Pinning the approver root needs an administrator: run it from an "
            "elevated (Run as administrator) prompt.",
        )


def _write_windows(path: Path, payload: bytes) -> None:  # pragma: no cover - Windows only
    import subprocess

    directory = path.parent
    if os.path.lexists(directory) and (
        getattr(os.lstat(directory), "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise AnchorError(
            "approver_anchor_untrusted", f"{directory} is a link; remove it and pin again."
        )
    directory.mkdir(parents=False, exist_ok=True)
    system_root = _known_windows_dir()
    if system_root is None:
        raise AnchorError("approver_anchor_unreadable", "Can't find the Windows folder.")
    icacls = system_root / "System32" / "icacls.exe"
    for arguments in (
        ["/setowner", "*S-1-5-32-544"],
        [
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-32-544:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-545:(OI)(CI)RX",
        ],
    ):
        subprocess.run([str(icacls), str(directory), *arguments], check=True, capture_output=True)
    _check_windows(directory / ANCHOR_NAME)
    temp = directory / f".{ANCHOR_NAME}.{os.getpid()}.tmp"
    with open(temp, "xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _known_windows_dir() -> Path | None:  # pragma: no cover - Windows only
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    if _windll("kernel32").GetSystemWindowsDirectoryW(buffer, 260):
        return Path(buffer.value)
    return None


__all__ = [
    "ANCHOR_NAME",
    "Ace",
    "AnchorError",
    "TRUSTED_SIDS",
    "anchor_dir",
    "anchor_path",
    "dacl_admin_only",
    "normalize_root",
    "read_anchor",
    "write_anchor",
]
