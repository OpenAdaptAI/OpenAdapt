"""Hold a private Windows directory open while a ledger entry is written.

Windows cannot open a directory as a file descriptor, so the POSIX checks in
the qualification attempt ledger (``O_DIRECTORY | O_NOFOLLOW``, ``fstat``,
owner and mode) have no direct equivalent. This module gives the same
guarantees another way:

- The directory is opened as a handle with ``FILE_FLAG_OPEN_REPARSE_POINT``,
  so a junction or symbolic link is opened as itself and refused.
- The handle does not share delete access, so nobody can rename, delete or
  replace the directory until the caller closes it.
- The directory's real location (``GetFinalPathNameByHandleW``, which resolves
  every junction above it) must sit inside this user's profile, whose default
  access list is private to the user. A ledger elsewhere, such as
  ``C:\\Users\\Public`` or a network share, is refused.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DRIVE_FIXED = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


class HeldDirectory:
    """An open, undeletable handle on one directory; close it when done."""

    def __init__(self, kernel32, handle, final_path: str) -> None:
        self._kernel32 = kernel32
        self._handle: Optional[int] = handle
        self.final_path = final_path

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _open(kernel32, path: Path):
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise ValueError("qualification attempt ledger is not a private directory")
    return handle


def _final_path(kernel32, handle) -> str:
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            raise ValueError("qualification attempt ledger could not be inspected")
        if length < size:
            return buffer.value
        size = length + 1


def _within(child: str, parent: str) -> bool:
    child = os.path.normcase(child.rstrip("\\"))
    parent = os.path.normcase(parent.rstrip("\\"))
    return child == parent or child.startswith(parent + "\\")


def _profile_final_path(kernel32) -> str:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise ValueError("qualification attempt ledger has no user profile")
    handle = kernel32.CreateFileW(
        profile,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise ValueError("qualification attempt ledger has no user profile")
    try:
        return _final_path(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def hold_private_directory(path: Path) -> HeldDirectory:
    """Open ``path`` so it cannot move, after proving it is private.

    Raises ``ValueError`` when the path is a reparse point, is not a
    directory, is not on a fixed local drive, or resolves outside this user's
    profile.
    """
    kernel32 = _kernel32()
    handle = _open(kernel32, path)
    try:
        info = _ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ValueError("qualification attempt ledger could not be inspected")
        attributes = info.dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not (
            attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise ValueError("qualification attempt ledger is not a private directory")
        final_path = _final_path(kernel32, handle)
        # GetFinalPathNameByHandleW returns \\?\C:\... for a local volume and
        # \\?\UNC\... for a share; only a fixed local drive is accepted.
        if not final_path.startswith("\\\\?\\") or final_path[5:6] != ":":
            raise ValueError("qualification attempt ledger is not on a local drive")
        if kernel32.GetDriveTypeW(final_path[4:6] + "\\") != _DRIVE_FIXED:
            raise ValueError("qualification attempt ledger is not on a local drive")
        if not _within(final_path, _profile_final_path(kernel32)):
            raise ValueError(
                "qualification attempt ledger is outside this user's profile"
            )
        return HeldDirectory(kernel32, handle, final_path)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
