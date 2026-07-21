from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_frozen_engine as build
from scripts import verify_build_artifact as verify


def _build_command(identity: str, tmp_path: Path) -> list[str]:
    onnxruntime_dir = tmp_path / "onnxruntime"
    onnxruntime_dir.mkdir()
    (onnxruntime_dir / "LICENSE").write_text("MIT\n")
    (onnxruntime_dir / "ThirdPartyNotices.txt").write_text("notices\n")
    return build.build_command(
        signing_identity=identity,
        platform="darwin",
        onnxruntime_dir=onnxruntime_dir,
    )


def test_developer_id_signs_embedded_binaries_with_tauri_identity(tmp_path: Path) -> None:
    command = _build_command("Developer ID Application: OpenAdapt AI (TEAM123)", tmp_path)

    index = command.index("--codesign-identity")
    assert command[index + 1] == "Developer ID Application: OpenAdapt AI (TEAM123)"


def test_adhoc_build_does_not_enable_hardened_runtime_inside_onefile(tmp_path: Path) -> None:
    command = _build_command("-", tmp_path)

    assert "--codesign-identity" not in command
    for module in build.EXCLUDED_MODULES:
        assert ["--exclude-module", module] == command[
            command.index(module) - 1 : command.index(module) + 1
        ]


def test_frozen_runtime_bundles_required_third_party_notices(tmp_path: Path) -> None:
    command = _build_command("", tmp_path)

    values = [command[index + 1] for index, value in enumerate(command) if value == "--add-data"]
    assert values == [
        f"{tmp_path / 'onnxruntime' / 'LICENSE'}:third_party/onnxruntime",
        f"{tmp_path / 'onnxruntime' / 'ThirdPartyNotices.txt'}:third_party/onnxruntime",
        f"{build.RAPIDOCR_NOTICE_DIR / 'LICENSE'}:third_party/rapidocr",
        f"{build.RAPIDOCR_NOTICE_DIR / 'NOTICE'}:third_party/rapidocr",
    ]


def test_missing_onnxruntime_notice_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT\n")

    with pytest.raises(RuntimeError, match="ThirdPartyNotices.txt"):
        build.notice_data(tmp_path)


def test_windows_frozen_inventory_member_paths_are_normalized() -> None:
    windows_inventory = repr("third_party\\rapidocr\\LICENSE") + "\n"

    normalized = verify.normalized_inventory(windows_inventory)
    assert "third_party/rapidocr/LICENSE" in normalized
    assert "//" not in normalized
