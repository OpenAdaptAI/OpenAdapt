"""Launch a process in the interactive console session (session 1) from SYSTEM.

``prlctl exec`` runs commands as ``NT AUTHORITY\\SYSTEM`` in session 0, which
is isolated from the logged-on user's desktop — mss ``BitBlt`` and pyautogui
``SendInput`` there address a non-existent desktop and fail. This launcher is
the canonical "service starts a process on the user's desktop" pattern:

    WTSGetActiveConsoleSessionId -> WTSQueryUserToken -> DuplicateTokenEx
    -> CreateEnvironmentBlock -> CreateProcessAsUserW (lpDesktop=winsta0\\default)

Run it AS SYSTEM (which holds SeTcbPrivilege, required by WTSQueryUserToken):

    python session1_launch.py "<full command line to run in session 1>"

ctypes argtypes/restypes are declared explicitly so 64-bit HANDLEs are not
truncated to 32-bit ints (the classic silent-failure bug on Win64/ARM64).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

TOKEN_ALL_ACCESS = 0xF01FF
SECURITY_IMPERSONATION = 2  # SecurityImpersonation
TOKEN_PRIMARY = 1
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
INVALID_SESSION = 0xFFFFFFFF


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _declare() -> None:
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

    wtsapi32.WTSQueryUserToken.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL

    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateTokenEx.restype = wintypes.BOOL

    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL

    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL


def launch(cmdline: str) -> int:
    """Launch ``cmdline`` in the active console session. Return its PID."""
    _declare()
    session_id = kernel32.WTSGetActiveConsoleSessionId()
    if session_id == INVALID_SESSION:
        raise RuntimeError("no active console session")

    htoken = wintypes.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(htoken)):
        raise ctypes.WinError(ctypes.get_last_error())

    hdup = wintypes.HANDLE()
    if not advapi32.DuplicateTokenEx(
        htoken,
        TOKEN_ALL_ACCESS,
        None,
        SECURITY_IMPERSONATION,
        TOKEN_PRIMARY,
        ctypes.byref(hdup),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    env = ctypes.c_void_p()
    userenv.CreateEnvironmentBlock(ctypes.byref(env), hdup, False)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"
    pi = PROCESS_INFORMATION()

    ok = advapi32.CreateProcessAsUserW(
        hdup,
        None,
        ctypes.create_unicode_buffer(cmdline),
        None,
        None,
        False,
        CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
        env,
        None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(pi.dwProcessId)


def main() -> None:
    """Launch ``pythonw <script> <args...>`` in session 1.

    argv[1] is the Python script to run (forward slashes are fine — they are
    normalized here), argv[2:] are extra args passed through. pythonw.exe is
    derived from this interpreter so no Windows paths need to cross the host
    shell (which mangles backslashes).
    """
    import os

    if len(sys.argv) < 2:
        print("usage: session1_launch.py <script.py> [args...]",
              file=sys.stderr)
        sys.exit(2)
    script = os.path.abspath(sys.argv[1])
    extra = list(sys.argv[2:])
    if script.lower().endswith(".ps1"):
        # A WinForms/PowerShell UI target: run STA so WinForms works.
        parts = ["powershell.exe", "-STA", "-ExecutionPolicy", "Bypass",
                 "-File", f'"{script}"'] + extra
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        parts = [f'"{pythonw}"', f'"{script}"'] + extra
    cmdline = " ".join(parts)
    pid = launch(cmdline)
    session_id = kernel32.WTSGetActiveConsoleSessionId()
    print(f"LAUNCHED_SESSION session={session_id} pid={pid} cmd={cmdline}")


if __name__ == "__main__":
    main()
