from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

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
        notice_bundle=tmp_path / "frozen-notices",
    )


def test_developer_id_signs_embedded_binaries_with_tauri_identity(tmp_path: Path) -> None:
    command = _build_command("Developer ID Application: OpenAdapt AI (TEAM123)", tmp_path)

    index = command.index("--codesign-identity")
    assert command[index + 1] == "Developer ID Application: OpenAdapt AI (TEAM123)"
    entitlements = command.index("--osx-entitlements-file")
    assert command[entitlements + 1] == str(build.ROOT / "src-tauri" / "Entitlements.plist")


def test_adhoc_build_does_not_enable_hardened_runtime_inside_onefile(tmp_path: Path) -> None:
    command = _build_command("-", tmp_path)

    assert "--codesign-identity" not in command
    assert "--osx-entitlements-file" not in command
    for module in build.EXCLUDED_MODULES:
        assert ["--exclude-module", module] == command[
            command.index(module) - 1 : command.index(module) + 1
        ]


def test_linux_runner_libgcc_is_excluded_at_pyinstaller_resolution() -> None:
    class MatchList:
        def __init__(self, patterns) -> None:
            self.patterns = tuple(re.compile(pattern) for pattern in patterns)

        def check_library(self, libname: str) -> bool:
            return any(pattern.fullmatch(Path(libname).name) for pattern in self.patterns)

    dylib = SimpleNamespace(_excludes=set(), MatchList=MatchList, exclude_list=MatchList(set()))
    dylib.include_library = lambda libname: not dylib.exclude_list.check_library(libname)

    assert (
        build.configure_system_runtime_boundary(
            platform="linux",
            dylib_module=dylib,
        )
        is True
    )
    assert dylib.include_library("/lib/x86_64-linux-gnu/libgcc_s.so.1") is False
    assert dylib.include_library("/lib/x86_64-linux-gnu/libstdc++.so.6") is True


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_non_linux_system_runtime_boundary_is_a_noop(platform: str) -> None:
    assert build.configure_system_runtime_boundary(platform=platform) is False


def test_frozen_runtime_bundles_required_third_party_notices(tmp_path: Path) -> None:
    command = _build_command("", tmp_path)

    values = [command[index + 1] for index, value in enumerate(command) if value == "--add-data"]
    assert values == [
        f"{tmp_path / 'onnxruntime' / 'LICENSE'}:third_party/onnxruntime",
        f"{tmp_path / 'onnxruntime' / 'ThirdPartyNotices.txt'}:third_party/onnxruntime",
        f"{build.RAPIDOCR_NOTICE_DIR / 'LICENSE'}:third_party/rapidocr",
        f"{build.RAPIDOCR_NOTICE_DIR / 'NOTICE'}:third_party/rapidocr",
        f"{tmp_path / 'frozen-notices'}:third_party/python",
    ]
    assert ["--collect-data", "engine"] == command[
        command.index("engine") - 1 : command.index("engine") + 1
    ]
    assert ["--hidden-import", "onnxruntime"] == command[
        command.index("onnxruntime") - 1 : command.index("onnxruntime") + 1
    ]
    assert ["--hidden-import", "shapely"] == command[
        command.index("shapely") - 1 : command.index("shapely") + 1
    ]
    assert "numpy.core.multiarray" not in command
    assert ["--exclude-module", "numpy"] == command[
        command.index("numpy") - 1 : command.index("numpy") + 1
    ]


def test_missing_onnxruntime_notice_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT\n")

    with pytest.raises(RuntimeError, match="ThirdPartyNotices.txt"):
        build.notice_data(tmp_path)


def test_python_distribution_guard_runs_without_build_extra(tmp_path: Path) -> None:
    """The recovery publication guard must not require Packaging/PyInstaller."""

    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "openadapt_desktop-0.8.0-py3-none-any.whl", "w") as archive:
        archive.writestr("openadapt_desktop/__init__.py", "")
    with tarfile.open(dist / "openadapt_desktop-0.8.0.tar.gz", "w:gz") as archive:
        payload = tmp_path / "__init__.py"
        payload.write_text("")
        archive.add(payload, arcname="openadapt_desktop-0.8.0/openadapt_desktop/__init__.py")

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(verify.ROOT / "scripts" / "verify_build_artifact.py"),
            "python-distribution",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("Verified Python distribution:") == 2


def test_windows_frozen_inventory_member_paths_are_normalized() -> None:
    windows_inventory = repr("third_party\\onnxruntime\\LICENSE") + "\n"

    normalized = verify.normalized_inventory(windows_inventory)
    assert "third_party/onnxruntime/LICENSE" in normalized
    assert "//" not in normalized
    member_keys = verify.frozen_member_keys([r"third_party\python\NOTICE-INVENTORY.json"])
    assert member_keys["third_party/python/NOTICE-INVENTORY.json"] == (
        r"third_party\python\NOTICE-INVENTORY.json"
    )


def test_frozen_inventory_rejects_copyleft_module_names() -> None:
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("'oa_atomacos._a11y'")
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("'pynput.keyboard._darwin'")
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("'scipy.fftpack'")
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("'av._core'")
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("libquadmath.0.dylib")
    assert verify.FORBIDDEN_FROZEN_MEMBERS.search("libx264.165.dylib")
    # Independently licensed media/vision components are governed by their
    # separate runtime boundary; generic libav names are not blanket-banned.
    assert not verify.FORBIDDEN_FROZEN_MEMBERS.search("cv2/.dylibs/libavcodec.61.dylib")
    assert verify.FORBIDDEN_EMBEDDED_VISION_MEMBERS.search("cv2/.dylibs/libavcodec.61.dylib")
    assert verify.FORBIDDEN_EMBEDDED_VISION_MEMBERS.search("'rapidocr_onnxruntime.main'")
    assert verify.FORBIDDEN_EMBEDDED_VISION_MEMBERS.search(
        "opencv_python-5.0.0.93.dist-info/LICENSE.txt"
    )
    assert not verify.FORBIDDEN_FROZEN_MEMBERS.search("'java.util'")
    assert not verify.FORBIDDEN_FROZEN_MEMBERS.search("'scipytools.helper'")


def test_frozen_notice_inventory_binds_concrete_archive_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootloader_notice = b"reviewed test Bootloader Exception notice\n"
    import hashlib

    monkeypatch.setattr(
        verify,
        "PYINSTALLER_NOTICE_SHA256",
        hashlib.sha256(bootloader_notice).hexdigest(),
    )
    monkeypatch.setattr(
        verify,
        "PYINSTALLER_EXCEPTION_MARKERS",
        ("Bootloader Exception",),
    )
    payloads = {
        "third_party/python/openadapt-desktop/001-LICENSE": b"desktop MIT\n",
        "third_party/python/openadapt-capture/001-LICENSE": b"capture MIT\n",
        "third_party/python/openadapt-privacy/001-LICENSE": b"privacy MIT\n",
        "third_party/python/openadapt-flow/001-LICENSE": b"flow MIT\n",
        "third_party/python/alembic/001-LICENSE": b"alembic MIT\n",
        "third_party/python/mako/001-LICENSE": b"mako MIT\n",
        "third_party/python/pympler/001-LICENSE": b"Apache\n",
        "third_party/python/pympler/002-NOTICE": b"Pympler notice\n",
        "third_party/python/sqlalchemy/001-LICENSE": b"sqlalchemy MIT\n",
        verify.PYINSTALLER_NOTICE_MEMBER: bootloader_notice,
    }
    packages = []
    for name in verify.REQUIRED_NOTICE_TOKENS:
        notices = []
        for member, payload in payloads.items():
            if f"/{name}/" not in member:
                continue
            import hashlib

            notices.append(
                {
                    "bundled_member": member,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        packages.append(
            {
                "name": name,
                "version": "1.0.0",
                "license_evidence": ["MIT"],
                "notices": notices,
            }
        )
    inventory = json.dumps(
        {
            "schema_version": 2,
            "runtime_roots": list(verify.FROZEN_RUNTIME_ROOTS),
            "packages": packages,
            "build_only_packages": [
                {
                    "name": verify.PYINSTALLER_DISTRIBUTION,
                    "version": verify.PYINSTALLER_VERSION,
                    "archive_import_roots": ["PyInstaller"],
                }
            ],
            "embedded_build_components": [
                {
                    "name": "pyinstaller-bootloader",
                    "source_distribution": verify.PYINSTALLER_DISTRIBUTION,
                    "source_version": verify.PYINSTALLER_VERSION,
                    "license_scope": ("GPL-2.0-or-later WITH PyInstaller-Bootloader-exception"),
                    "source_member": ("pyinstaller-6.21.0.dist-info/licenses/COPYING.txt"),
                    "bundled_member": verify.PYINSTALLER_NOTICE_MEMBER,
                    "sha256": hashlib.sha256(bootloader_notice).hexdigest(),
                    "bytes": len(bootloader_notice),
                    "required_markers": ["Bootloader Exception"],
                }
            ],
        }
    ).encode()

    build_only_roots = verify.validate_frozen_notice_inventory(
        inventory,
        members=set(payloads),
        extract_member=payloads.__getitem__,
    )
    assert build_only_roots == ("PyInstaller",)


def test_frozen_archive_rejects_build_only_python_modules() -> None:
    with pytest.raises(ValueError, match="build-only Python modules"):
        verify.reject_frozen_build_only_imports(
            modules={"openadapt_flow", "PyInstaller.building.api"},
            import_roots=("PyInstaller", "altgraph"),
        )

    verify.reject_frozen_build_only_imports(
        modules={"openadapt_flow", "pyimod02_importers", "pyi_rth_pkgutil"},
        import_roots=("PyInstaller", "altgraph"),
    )


def test_frozen_notice_inventory_rejects_copyleft_metadata() -> None:
    inventory = json.dumps(
        {
            "schema_version": 2,
            "runtime_roots": list(verify.FROZEN_RUNTIME_ROOTS),
            "packages": [
                {
                    "name": "oa-atomacos",
                    "version": "3.2.0",
                    "license_evidence": ["GPLv2"],
                    "notices": [],
                }
            ],
        }
    ).encode()

    with pytest.raises(ValueError, match="copyleft package"):
        verify.validate_frozen_notice_inventory(
            inventory,
            members=set(),
            extract_member=lambda member: b"",
        )


def test_frozen_notice_inventory_rejects_managed_vision_package() -> None:
    inventory = json.dumps(
        {
            "schema_version": 2,
            "runtime_roots": list(verify.FROZEN_RUNTIME_ROOTS),
            "packages": [
                {
                    "name": "opencv-python",
                    "version": "5.0.0.93",
                    "license_evidence": ["Apache-2.0"],
                    "notices": [],
                }
            ],
        }
    ).encode()

    with pytest.raises(ValueError, match="separately provisioned package"):
        verify.validate_frozen_notice_inventory(
            inventory,
            members=set(),
            extract_member=lambda member: b"",
        )


def test_frozen_notice_inventory_rejects_metadata_only_package() -> None:
    inventory = json.dumps(
        {
            "schema_version": 2,
            "runtime_roots": list(verify.FROZEN_RUNTIME_ROOTS),
            "packages": [
                {
                    "name": "metadata-only",
                    "version": "1.0.0",
                    "license_evidence": ["MIT"],
                    "notices": [],
                }
            ],
        }
    ).encode()

    with pytest.raises(ValueError, match="missing concrete notice"):
        verify.validate_frozen_notice_inventory(
            inventory,
            members=set(),
            extract_member=lambda member: b"",
        )
