from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from scripts import sync_frozen_dependencies as sync
from scripts.sync_frozen_dependencies import frozen_dependency_environment

ROOT = Path(__file__).resolve().parents[1]


def test_every_platform_requires_fixed_cryptography_50() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [
        Requirement(value) for value in project["dependencies"] if value.startswith("cryptography")
    ]

    assert len(requirements) == 1
    assert str(requirements[0].specifier) == "<51,>=50"
    assert requirements[0].marker is None


def test_intel_macos_build_uses_static_homebrew_openssl(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "libssl.a").touch()
    (lib / "libcrypto.a").touch()

    environment = frozen_dependency_environment(
        system="Darwin",
        machine="x86_64",
        source={"OPENSSL_DIR": str(tmp_path)},
    )

    assert environment["OPENSSL_DIR"] == str(tmp_path)
    assert environment["OPENSSL_STATIC"] == "1"


def test_intel_macos_build_refuses_missing_static_openssl(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="requires static OpenSSL libraries"):
        frozen_dependency_environment(
            system="Darwin",
            machine="x86_64",
            source={"OPENSSL_DIR": str(tmp_path)},
        )


def test_intel_macos_sync_forces_a_fresh_source_build(tmp_path: Path, monkeypatch) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "libssl.a").touch()
    (lib / "libcrypto.a").touch()
    monkeypatch.setenv("OPENSSL_DIR", str(tmp_path))
    monkeypatch.setattr(sync.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sync.platform, "machine", lambda: "x86_64")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", run)

    assert sync.main() == 0
    command, kwargs = calls[-1]
    assert command == [
        "uv",
        "sync",
        "--locked",
        "--extra",
        "build",
        "--reinstall-package",
        "cryptography",
        "--no-binary-package",
        "cryptography",
    ]
    assert kwargs["check"] is True
    assert kwargs["env"]["OPENSSL_STATIC"] == "1"
