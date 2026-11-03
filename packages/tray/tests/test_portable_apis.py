"""Fail on any platform-only API the tray uses without a guard.

The tray runs on macOS, Windows and Linux, but most CI jobs run on one OS.
``signal.SIGKILL`` raised AttributeError on Windows only after merge. This
scan catches that class of bug on any host, before merge.

A use is allowed when its line carries a ``# posix-only:`` or
``# windows-only:`` comment that says how it is guarded, or when it is in the
platform module for that OS. ``getattr(module, "NAME", default)`` is always
allowed, since it cannot raise.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "openadapt_tray"

# signal names that exist on Windows (CPython's signal module there).
WINDOWS_SIGNAL_NAMES = {
    "SIGABRT",
    "SIGFPE",
    "SIGILL",
    "SIGINT",
    "SIGSEGV",
    "SIGTERM",
    "SIG_DFL",
    "SIG_IGN",
    "NSIG",
    "Signals",
    "Handlers",
    "default_int_handler",
    "getsignal",
    "raise_signal",
    "set_wakeup_fd",
    "signal",
    "strsignal",
    "valid_signals",
}
WINDOWS_ONLY_SIGNAL_NAMES = {"SIGBREAK", "CTRL_C_EVENT", "CTRL_BREAK_EVENT"}

POSIX_ONLY_OS = {
    "fchmod",
    "fchown",
    "fork",
    "getegid",
    "geteuid",
    "getgid",
    "getgroups",
    "getpgid",
    "getpgrp",
    "getuid",
    "killpg",
    "setpgid",
    "setsid",
    "sysconf",
    "uname",
}
POSIX_ONLY_MODULES = {"fcntl", "grp", "pwd", "resource", "termios"}
WINDOWS_ONLY_MODULES = {"msvcrt", "winreg", "winsound", "_winapi"}
WINDOWS_ONLY_SUBPROCESS_PREFIXES = ("CREATE_", "STARTF_", "STARTUPINFO", "SW_")

POSIX_MODULES = {"platform/macos.py", "platform/linux.py"}
WINDOWS_MODULES = {"platform/windows.py"}


def _violations(path: Path, src: Path = SRC) -> list[str]:
    relative = path.relative_to(src).as_posix()
    lines = path.read_text(encoding="utf-8").splitlines()
    found = []

    def report(node: ast.AST, kind: str, what: str) -> None:
        line = lines[node.lineno - 1]
        if kind == "posix" and ("# posix-only:" in line or relative in POSIX_MODULES):
            return
        if kind == "windows" and (
            "# windows-only:" in line or relative in WINDOWS_MODULES
        ):
            return
        found.append(f"{relative}:{node.lineno}: {kind}-only {what}")

    module_level = set()
    for node in ast.parse("\n".join(lines)).body:
        module_level.add(node)

    for node in ast.walk(ast.parse("\n".join(lines))):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner, name = node.value.id, node.attr
            if owner == "signal" and name in WINDOWS_ONLY_SIGNAL_NAMES:
                report(node, "windows", f"signal.{name}")
            elif (
                owner == "signal"
                and name.isupper()
                and (name not in WINDOWS_SIGNAL_NAMES)
            ):
                report(node, "posix", f"signal.{name}")
            elif owner == "os" and name in POSIX_ONLY_OS:
                report(node, "posix", f"os.{name}")
            elif owner == "subprocess" and name.startswith(
                WINDOWS_ONLY_SUBPROCESS_PREFIXES
            ):
                report(node, "windows", f"subprocess.{name}")
            elif owner == "ctypes" and name in {"windll", "WinDLL", "WinError"}:
                report(node, "windows", f"ctypes.{name}")
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root = name.split(".")[0]
                # Only module-level imports run on every platform; imports
                # inside a function or platform branch are already guarded.
                top = any(node is item for item in module_level)
                if root in POSIX_ONLY_MODULES and top:
                    report(node, "posix", f"import {name}")
                if root in WINDOWS_ONLY_MODULES and top:
                    report(node, "windows", f"import {name}")
    return found


def test_no_unguarded_platform_only_apis():
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        violations += _violations(path)
    assert violations == [], "\n".join(violations)


def test_the_scan_catches_the_bug_that_reached_main(tmp_path):
    """The exact pattern from the Windows failure must be reported."""
    fake_src = tmp_path / "openadapt_tray"
    fake_src.mkdir()
    (fake_src / "local_record.py").write_text(
        "import signal\n"
        "def stop(p):\n"
        "    p.send_signal(signal.SIGKILL)\n"
        "    p.send_signal(signal.SIGTERM)\n"
        "    p.send_signal(getattr(signal, 'CTRL_BREAK_EVENT', 2))\n"
    )
    assert _violations(fake_src / "local_record.py", fake_src) == [
        "local_record.py:3: posix-only signal.SIGKILL"
    ]
