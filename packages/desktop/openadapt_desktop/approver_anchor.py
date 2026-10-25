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

:func:`read_anchor` trusts the file only when no one but an administrator
can change it or what leads to it:

* POSIX: opened one folder at a time from ``/`` without following links,
  every folder and the file owned by root and writable by no one else (a
  sticky folder such as /tmp may sit above), no extended ACL granting a
  write (macOS), and the file has a single link.
* Windows: the file and its folder owned by Administrators, SYSTEM or
  TrustedInstaller, with no access entry of any kind letting anyone else
  write (an entry of a type the check doesn't know refuses); every folder
  above lets no one else replace what's inside; no links or junctions; a
  single hard link.

Anything else raises :class:`AnchorError`: Desktop then counts no person's
answer. :func:`write_anchor` is the elevated ``openadapt-desktop
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
    owned by root, without extended ACLs, written atomically without
    following links. Windows: run elevated; the folder is always created
    fresh with :data:`ANCHOR_FOLDER_SDDL` (one already there is moved aside,
    never adopted). The result is read back through :func:`read_anchor`.
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

#: macOS extended-ACL permissions (``acl_to_text`` names) that let someone
#: change a file or folder, or what it contains.
_MAC_WRITE_PERMS = frozenset(
    {
        "write",
        "add_file",
        "append",
        "add_subdirectory",
        "delete",
        "delete_child",
        "writeattr",
        "writeextattr",
        "writesecurity",
        "chown",
    }
)
_ACL_TYPE_EXTENDED = 0x00000100


def mac_acl_write_grants(acl_text: str) -> list[str]:
    """The entries of a macOS ``acl_to_text`` ACL that allow a write.

    Each entry is ``tag:uuid:name:id:KIND[,flag...]:perm[,perm...]``, where
    KIND is ``allow`` or ``deny`` and the flags include ``inherited`` and
    ``only_inherit`` (an entry that doesn't apply to the object itself).
    """

    grants = []
    for line in acl_text.splitlines():
        line = line.strip()
        fields = line.split(":")
        if not line or line.startswith("!#") or len(fields) < 2:
            continue
        kind = set(fields[-2].split(","))
        perms = set(fields[-1].split(","))
        if "allow" not in kind or "only_inherit" in kind | perms:
            continue
        if perms & _MAC_WRITE_PERMS:
            grants.append(line)
    return grants


def _mac_acl_text(fd: int) -> str:
    """The extended ACL on an open file or folder (``""`` when it has none)."""

    if sys.platform != "darwin":
        return ""
    import ctypes
    import errno as errno_codes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_to_text.restype = ctypes.c_void_p
    libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    acl = libc.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno_codes.ENOENT:
            return ""
        raise AnchorError("approver_anchor_unreadable", "Can't read a folder's access list.")
    try:
        text = libc.acl_to_text(acl, None)
        if not text:
            raise AnchorError("approver_anchor_unreadable", "Can't read a folder's access list.")
        try:
            return ctypes.string_at(text).decode("utf-8", "replace")
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(acl)


def _check_fd(
    fd: int, where: Path, trusted_uids: frozenset[int], *, folder: bool, sticky_ok: bool
) -> None:
    info = os.fstat(fd)
    if folder and not stat.S_ISDIR(info.st_mode):
        raise AnchorError("approver_anchor_untrusted", f"{where} isn't a folder.")
    if not folder:
        if not stat.S_ISREG(info.st_mode):
            raise AnchorError("approver_anchor_untrusted", f"{where} isn't a regular file.")
        if info.st_nlink != 1:
            raise AnchorError(
                "approver_anchor_untrusted",
                f"{where} has other hard links; the pin isn't trusted.",
            )
    if info.st_uid not in trusted_uids:
        raise AnchorError(
            "approver_anchor_untrusted",
            f"{where} isn't owned by the administrator; the pin isn't trusted.",
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
        sticky_ok and info.st_mode & stat.S_ISVTX
    ):
        raise AnchorError(
            "approver_anchor_untrusted", f"Others can write {where}; the pin isn't trusted."
        )
    grants = mac_acl_write_grants(_mac_acl_text(fd))
    if grants:
        raise AnchorError(
            "approver_anchor_untrusted",
            f"An access list lets others change {where} ({grants[0]}); the pin isn't trusted.",
        )


def _open_folder(directory: Path, trusted_uids: frozenset[int]) -> int:
    """Open ``directory`` one component at a time from ``/``, never following a
    link, checking each folder as it is opened (so a folder swapped after a
    check is never the one used). Folders above it may be sticky and
    world-writable, as /tmp is: other users can't replace root's entries."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory.anchor, flags)
    where = Path(directory.anchor)
    parts = directory.parts[1:]
    try:
        _check_fd(fd, where, trusted_uids, folder=True, sticky_ok=bool(parts))
        for index, part in enumerate(parts):
            where = where / part
            try:
                child = os.open(part, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except OSError as exc:
                raise AnchorError(
                    "approver_anchor_untrusted",
                    f"Can't open {where} as a folder without following links: {exc}",
                ) from None
            os.close(fd)
            fd = child
            _check_fd(fd, where, trusted_uids, folder=True, sticky_ok=index < len(parts) - 1)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_posix(path: Path, trusted_uids: frozenset[int]) -> bytes:
    folder = _open_folder(path.parent, trusted_uids)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=folder)
        except OSError as exc:
            raise AnchorError(
                "approver_anchor_untrusted", f"Can't open {path} without following links: {exc}"
            ) from None
    finally:
        os.close(folder)
    try:
        _check_fd(fd, path, trusted_uids, folder=False, sticky_ok=False)
        data = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_BYTES:
        raise AnchorError("approver_anchor_unreadable", f"{path} is too large to be the pin.")
    return data


def _strip_mac_acl(path: Path) -> None:
    """Remove any extended ACL (``chmod -N``), such as one inherited from
    ``/Library/Application Support``."""

    if sys.platform == "darwin":
        import subprocess

        subprocess.run(["/bin/chmod", "-h", "-N", str(path)], check=True, capture_output=True)


def _write_posix(path: Path, payload: bytes, trusted_uids: frozenset[int]) -> None:
    directory = path.parent
    missing: list[Path] = []
    probe = directory
    while not os.path.lexists(probe):
        missing.append(probe)
        probe = probe.parent
    os.close(_open_folder(probe, trusted_uids))
    for folder in reversed(missing):
        os.mkdir(folder, 0o755)
        os.chmod(folder, 0o755)
        _strip_mac_acl(folder)
    _strip_mac_acl(directory)
    folder_fd = _open_folder(directory, trusted_uids)
    try:
        temp = f".{ANCHOR_NAME}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o644, dir_fd=folder_fd)
        try:
            os.fchmod(fd, 0o644)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _strip_mac_acl(directory / temp)
        os.replace(temp, path.name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        os.fsync(folder_fd)
    finally:
        os.close(folder_fd)


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
#: DACL entry types (winnt.h). Allow entries of every kind are checked; an
#: entry of a type not listed here refuses the pin rather than be skipped.
_ALLOW_ACE_TYPES = frozenset({0, 5, 9, 11})  # allowed, object, callback, callback object
_DENY_ACE_TYPES = frozenset({1, 6, 10, 12})
_OBJECT_ACE_TYPES = frozenset({5, 6, 11, 12})
_INHERIT_ONLY_ACE = 0x08
#: Rights that change a file or folder, or what it contains: write/append
#: data (add file/subfolder), write EA/attributes, delete child, DELETE,
#: WRITE_DAC, WRITE_OWNER, GENERIC_ALL, GENERIC_WRITE, MAXIMUM_ALLOWED.
_WRITE_RIGHTS = (
    0x0002
    | 0x0004
    | 0x0010
    | 0x0040
    | 0x0100
    | 0x00010000
    | 0x00040000
    | 0x00080000
    | 0x02000000
    | 0x10000000
    | 0x40000000
)
#: Rights over a folder above the anchor's that would let someone replace
#: what's inside (adding new entries alone can't): delete child, DELETE,
#: WRITE_DAC, WRITE_OWNER, GENERIC_ALL, MAXIMUM_ALLOWED.
_REPLACE_RIGHTS = 0x0040 | 0x00010000 | 0x00040000 | 0x00080000 | 0x02000000 | 0x10000000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: The anchor folder's security: owned by Administrators, a protected DACL
#: (nothing inherited) with full control for Administrators and SYSTEM and
#: read-and-execute for Users, inherited by what is created inside.
ANCHOR_FOLDER_SDDL = "O:BAD:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;BU)"


@dataclass(frozen=True)
class Ace:
    """One entry of a DACL, as :func:`dacl_admin_only` needs it."""

    ace_type: int
    flags: int
    mask: int
    sid: str


def dacl_admin_only(
    owner_sid: str | None, aces: list[Ace] | None, *, rights: int = _WRITE_RIGHTS
) -> str | None:
    """Why this security descriptor lets someone other than an administrator
    change the object, or ``None`` when only administrators can.

    ``aces is None`` is a NULL DACL: everyone has full control. ``rights``
    is what counts as a change (all writes for the anchor and its folder;
    only replacing entries for the folders above).
    """

    if owner_sid not in TRUSTED_SIDS:
        return f"it is owned by {owner_sid}, not an administrator"
    if aces is None:
        return "it has no access control list, so everyone can change it"
    for ace in aces:
        if ace.ace_type in _DENY_ACE_TYPES:
            continue
        if ace.ace_type not in _ALLOW_ACE_TYPES:
            return f"it has an access entry of a type this check doesn't know ({ace.ace_type})"
        if ace.flags & _INHERIT_ONLY_ACE:
            continue
        if ace.mask & rights and ace.sid not in TRUSTED_SIDS:
            return f"{ace.sid} can change it"
    return None


def _check_windows(path: Path) -> None:
    """The file and its folder: only administrators can change them. Every
    folder above: only administrators can replace what's inside (ProgramData
    lets users add folders of their own, which is fine). No links anywhere."""

    folder = path.parent
    targets = [(ancestor, _REPLACE_RIGHTS) for ancestor in reversed(folder.parents)]
    targets += [(folder, _WRITE_RIGHTS), (path, _WRITE_RIGHTS)]
    for target, rights in targets:
        if not os.path.lexists(target):
            continue
        info = os.lstat(target)
        if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise AnchorError(
                "approver_anchor_untrusted", f"{target} is a link; the pin isn't trusted."
            )
        if target == path and os.stat(target).st_nlink != 1:
            raise AnchorError(
                "approver_anchor_untrusted",
                f"{target} has other hard links; the pin isn't trusted.",
            )
        owner, aces = _windows_security(target)
        reason = dacl_admin_only(owner, aces, rights=rights)
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
            ace_type, flags = header[0], header[1]
            mask = wintypes.DWORD.from_address(address + 4).value
            if ace_type in _OBJECT_ACE_TYPES:
                # Mask, then object flags and up to two GUIDs before the SID.
                object_flags = wintypes.DWORD.from_address(address + 8).value
                offset = 12 + (16 if object_flags & 0x1 else 0) + (16 if object_flags & 0x2 else 0)
            elif ace_type in _ALLOW_ACE_TYPES | _DENY_ACE_TYPES:
                offset = 8
            else:
                # Unknown: dacl_admin_only refuses it without reading a SID.
                aces.append(Ace(ace_type, flags, mask, ""))
                continue
            sid = ctypes.c_void_p(address + offset)
            aces.append(Ace(ace_type, flags, mask, _sid_string(advapi, kernel, sid)))
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
    """Create the anchor folder fresh with :data:`ANCHOR_FOLDER_SDDL`.

    A folder already there is never adopted: a user who made it first could
    keep a handle with WRITE_DAC open, or an entry this check skips, through
    any owner or ACL change. It is renamed aside (refused if something holds
    it open), and the new folder is created with its final security in one
    call.
    """

    import ctypes
    import time
    from ctypes import wintypes

    directory = path.parent
    for ancestor in reversed(directory.parents):
        _check_windows_ancestor(ancestor)
    if os.path.lexists(directory):
        aside = directory.with_name(f"{directory.name}.replaced-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.rename(directory, aside)
        except OSError as exc:
            raise AnchorError(
                "approver_anchor_untrusted",
                f"{directory} already exists and can't be moved aside ({exc}); a program may "
                "hold it open. Close it, or remove the folder, and pin again.",
            ) from None
        print(f"Moved the existing {directory} aside to {aside}.")

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi = _windll("advapi32", use_last_error=True)
    kernel = _windll("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        wintypes.LPCWSTR(ANCHOR_FOLDER_SDDL), 1, ctypes.byref(descriptor), None
    ):
        raise AnchorError("approver_anchor_unreadable", "Can't build the folder's security.")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(wintypes.LPCWSTR(str(directory)), ctypes.byref(attributes)):
            raise AnchorError(
                "approver_anchor_untrusted",
                f"Can't create {directory} (error {getattr(ctypes, 'get_last_error')()}); if something "
                "made it again meanwhile, pin again.",
            )
    finally:
        kernel.LocalFree(descriptor)
    _check_windows(path)
    temp = directory / f".{ANCHOR_NAME}.{os.getpid()}.tmp"
    with open(temp, "xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _check_windows_ancestor(target: Path) -> None:  # pragma: no cover - Windows only
    info = os.lstat(target)
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise AnchorError("approver_anchor_untrusted", f"{target} is a link; nothing was pinned.")
    owner, aces = _windows_security(target)
    reason = dacl_admin_only(owner, aces, rights=_REPLACE_RIGHTS)
    if reason:
        raise AnchorError("approver_anchor_untrusted", f"{target} isn't trusted: {reason}.")


def _known_windows_dir() -> Path | None:  # pragma: no cover - Windows only
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    if _windll("kernel32").GetSystemWindowsDirectoryW(buffer, 260):
        return Path(buffer.value)
    return None


__all__ = [
    "ANCHOR_FOLDER_SDDL",
    "ANCHOR_NAME",
    "Ace",
    "AnchorError",
    "TRUSTED_SIDS",
    "anchor_dir",
    "anchor_path",
    "dacl_admin_only",
    "mac_acl_write_grants",
    "normalize_root",
    "read_anchor",
    "write_anchor",
]
