"""The desktop shell never gives a child process a console window.

The installed app is a Windows GUI program, so it has no console to share with
a child. A console child started without ``CREATE_NO_WINDOW`` gets a new,
visible console. On Desktop 0.30.3 the FFmpeg probes did exactly that, and two
empty terminal windows opened on top of the app at first launch.

Every child the Rust shell starts must therefore be built by
``windowless_command``. The engine sidecar goes through the Tauri shell plugin,
which hides its console itself; the engine's own children share that hidden
console.
"""

from __future__ import annotations

import re
from pathlib import Path

SHELL_SOURCES = Path(__file__).resolve().parents[1] / "src-tauri" / "src"
HELPER = "fn windowless_command("
SPAWN = re.compile(r"\bCommand::new\s*\(")


def _helper_body(source: str) -> str:
    start = source.index(HELPER)
    brace = source.index("{", start)
    depth = 0
    for offset, char in enumerate(source[brace:], brace):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    raise AssertionError("windowless_command has no closing brace")


def _spawn_sites(root: Path = SHELL_SOURCES) -> list[tuple[str, int]]:
    """Every ``Command::new(`` under ``root`` that is outside the helper."""
    sites: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*.rs")):
        source = path.read_text(encoding="utf-8")
        helper = _helper_body(source) if HELPER in source else ""
        helper_start = source.index(helper) if helper else -1
        for match in SPAWN.finditer(source):
            inside = helper and helper_start <= match.start() < helper_start + len(helper)
            if not inside:
                line = source.count("\n", 0, match.start()) + 1
                sites.append((path.name, line))
    return sites


def test_the_helper_asks_windows_for_no_console_window() -> None:
    body = _helper_body((SHELL_SOURCES / "ffmpeg.rs").read_text(encoding="utf-8"))
    assert "#[cfg(windows)]" in body
    assert "CREATE_NO_WINDOW: u32 = 0x0800_0000" in body
    assert "command.creation_flags(CREATE_NO_WINDOW)" in body


def test_every_child_the_shell_starts_goes_through_the_helper() -> None:
    assert _spawn_sites() == [], (
        "start child processes with windowless_command, not Command::new, "
        "so no console window opens on Windows"
    )


def test_the_scan_catches_a_new_unflagged_spawn(tmp_path: Path) -> None:
    (tmp_path / "ffmpeg.rs").write_text(
        (SHELL_SOURCES / "ffmpeg.rs").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "new_feature.rs").write_text(
        'fn probe() {\n    let _ = std::process::Command::new("git");\n}\n', encoding="utf-8"
    )
    assert _spawn_sites(tmp_path) == [("new_feature.rs", 2)]
