"""Shared private-file checks for local trust and key handoffs."""

from __future__ import annotations


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


__all__ = ["PrivateFileAclError", "windows_descriptor_has_private_acl"]
