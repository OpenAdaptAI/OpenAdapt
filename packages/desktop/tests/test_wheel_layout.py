"""The wheel must install one namespaced import package, never a generic name.

``pip install openadapt-desktop`` once put a package called ``engine`` straight
into site-packages. The launcher installs several OpenAdapt packages into one
environment, and any other distribution or local module named ``engine`` would
shadow it or be shadowed by it. These tests build the real wheel and read the
archive a user installs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.verify_build_artifact import verify_python_distributions

ROOT = Path(__file__).resolve().parents[1]
IMPORT_PACKAGE = "openadapt_desktop"


def _version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel")
    out_dir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    (wheel,) = out_dir.glob("openadapt_desktop-*.whl")
    return wheel


def test_wheel_installs_only_the_namespaced_package(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as wheel:
        top_level = {name.split("/", 1)[0] for name in wheel.namelist()}

    assert top_level == {IMPORT_PACKAGE, f"openadapt_desktop-{_version()}.dist-info"}


def test_console_script_runs_from_the_installed_wheel(built_wheel: Path, tmp_path: Path) -> None:
    with zipfile.ZipFile(built_wheel) as wheel:
        entry_points = next(
            wheel.read(name).decode("utf-8")
            for name in wheel.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        )
        site = tmp_path / "site-packages"
        wheel.extractall(site)

    scripts = dict(line.split(" = ", 1) for line in entry_points.splitlines() if " = " in line)
    assert scripts == {"openadapt-desktop": f"{IMPORT_PACKAGE}.cli:main"}

    module, function = scripts["openadapt-desktop"].split(":")
    # Put the extracted wheel first so the run proves the installed files work,
    # not the editable checkout. ``-I`` keeps PYTHONPATH and the cwd out.
    probe = textwrap.dedent(
        f"""
        import importlib, sys
        site = sys.argv[1]
        sys.path.insert(0, site)
        module = importlib.import_module({module!r})
        assert module.__file__.startswith(site), module.__file__
        sys.argv = ["openadapt-desktop", "--help"]
        sys.exit(getattr(module, {function!r})())
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(site)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage: openadapt-desktop" in result.stdout


def _write_distributions(root: Path, package: str) -> None:
    dist = root / "dist"
    dist.mkdir()
    version = "9.9.9"
    with zipfile.ZipFile(dist / f"openadapt_desktop-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"{package}/__init__.py", "")
        wheel.writestr(f"openadapt_desktop-{version}.dist-info/METADATA", "")
    sdist_root = root / f"openadapt_desktop-{version}"
    (sdist_root / package).mkdir(parents=True)
    (sdist_root / package / "__init__.py").write_text("", encoding="utf-8")
    with tarfile.open(dist / f"openadapt_desktop-{version}.tar.gz", "w:gz") as sdist:
        sdist.add(sdist_root, arcname=sdist_root.name)


def test_release_gate_refuses_a_generic_top_level_package(tmp_path: Path) -> None:
    _write_distributions(tmp_path, "engine")

    with pytest.raises(SystemExit):
        verify_python_distributions(argparse.ArgumentParser(), tmp_path)


def test_release_gate_accepts_the_namespaced_package(tmp_path: Path) -> None:
    _write_distributions(tmp_path, IMPORT_PACKAGE)

    verify_python_distributions(argparse.ArgumentParser(), tmp_path)
