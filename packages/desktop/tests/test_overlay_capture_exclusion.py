import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_overlay_has_no_capture_inclusion_path() -> None:
    commands = (ROOT / "src-tauri/src/commands.rs").read_text(encoding="utf-8")
    assert ".set_content_protected(true)" in commands
    assert ".set_content_protected(false)" not in commands
    assert "!include_in" not in commands
    assert "set_control_overlay_layout" in commands
    layout_fn = commands.split("pub fn set_control_overlay_layout", 1)[1]
    assert "set_content_protected" not in layout_fn.split("/// Open a URL", 1)[0]

    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    overlay = next(
        window for window in config["app"]["windows"] if window["label"] == "control-overlay"
    )
    assert overlay["contentProtected"] is True
    assert overlay["focusable"] is False


def test_closed_overlay_frame_contract_does_not_grow_coach_fields() -> None:
    contract = (ROOT / "src/overlay/contract.ts").read_text(encoding="utf-8")
    generated = (ROOT / "src/overlay/generated/contract.ts").read_text(encoding="utf-8")
    assert "overlay://coach" not in generated
    assert "Open the claim screen" not in generated
    assert "hint:" not in generated
    assert "target_tracking: null" in contract
