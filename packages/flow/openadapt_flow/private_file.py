"""Shared private-file checks for local trust and key handoffs."""

from __future__ import annotations

import os


def open_binary(
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    flags: int,
    mode: int | None = None,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open a raw-byte descriptor without CRT newline or Ctrl-Z translation.

    Callers retain their creation, no-follow, inheritance and permission policy.
    Python's ``os.read`` and ``os.write`` otherwise use text-mode descriptors on
    Windows, even when their input and output are bytes.
    """
    if flags & getattr(os, "O_TEXT", 0):
        raise ValueError("raw-byte descriptors cannot use text mode")
    flags |= getattr(os, "O_BINARY", 0)
    if dir_fd is None:
        return os.open(path, flags) if mode is None else os.open(path, flags, mode)
    return (
        os.open(path, flags, dir_fd=dir_fd)
        if mode is None
        else os.open(path, flags, mode, dir_fd=dir_fd)
    )


class PrivateFileAclError(ValueError):
    """The active platform cannot prove that a private file has a safe ACL."""


def windows_descriptor_has_private_acl(descriptor: int) -> bool:
    """Require an opened Windows file to be private to its service identity."""

    try:
        import msvcrt

        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        get_osfhandle = getattr(msvcrt, "get_osfhandle", None)
        if not callable(get_osfhandle):
            raise AttributeError("Windows descriptor conversion is unavailable")
        handle = get_osfhandle(descriptor)
        security = win32security.GetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner = security.GetSecurityDescriptorOwner()
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        current = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        if not win32security.EqualSid(owner, current):
            return False
        dacl = security.GetSecurityDescriptorDacl()
        if dacl is None:
            return False
        allowed = (
            current,
            win32security.ConvertStringSidToSid("S-1-5-18"),
            win32security.ConvertStringSidToSid("S-1-5-32-544"),
        )
        for index in range(dacl.GetAceCount()):
            header, mask, sid = dacl.GetAce(index)
            ace_type = header[0]
            if ace_type == ntsecuritycon.ACCESS_DENIED_ACE_TYPE:
                continue
            if ace_type != ntsecuritycon.ACCESS_ALLOWED_ACE_TYPE:
                return False
            if mask and not any(win32security.EqualSid(sid, item) for item in allowed):
                return False
        return True
    except (ImportError, OSError, AttributeError) as exc:
        raise PrivateFileAclError(
            "Windows private-file ACL verification is unavailable; install the "
            "OpenAdapt Windows runtime"
        ) from exc


__all__ = ["PrivateFileAclError", "open_binary", "windows_descriptor_has_private_acl"]
