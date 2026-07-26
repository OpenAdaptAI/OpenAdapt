import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_overlay_has_no_capture_inclusion_path() -> None:
    commands = (ROOT / "src-tauri/src/commands.rs").read_text(encoding="utf-8")
    assert ".set_content_protected(true)" in commands
    assert ".set_content_protected(false)" not in commands
    assert "!include_in" not in commands

    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    overlay = next(
        window for window in config["app"]["windows"] if window["label"] == "control-overlay"
    )
    assert overlay["contentProtected"] is True
