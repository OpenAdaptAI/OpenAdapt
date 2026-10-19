"""Contracts for the installed package's default runtime API."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _runtime_dependency_names() -> set[str]:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^dependencies = \[\n(?P<requirements>.*?)^\]\n",
        pyproject,
    )
    assert match is not None
    requirements = ast.literal_eval("[" + match.group("requirements") + "]")
    return {
        re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower()
        for requirement in requirements
    }


#: Opt-in stacks. A plain record and read must work without any of them.
OPTIONAL_MODULES = ("matplotlib", "pympler", "sounddevice", "soundfile", "alembic")


def test_opt_in_stacks_are_not_in_the_default_install() -> None:
    """Plots, narration, profiling and migrations are extras, not core."""
    runtime_dependencies = _runtime_dependency_names()
    for name in OPTIONAL_MODULES:
        assert name not in runtime_dependencies, name


def test_macos_accessibility_uses_permissive_runtime_dependencies() -> None:
    """The MIT package must not pull a GPL accessibility wrapper."""
    runtime_dependencies = _runtime_dependency_names()
    assert "oa-atomacos" not in runtime_dependencies
    assert "pyobjc-framework-applicationservices" in runtime_dependencies

    package_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "openadapt_capture").rglob("*.py")
    )
    assert "oa_atomacos" not in package_sources


def test_native_input_observers_do_not_ship_pynput() -> None:
    """The default runtime and packaged source stay outside pynput's LGPL path."""
    assert "pynput" not in _runtime_dependency_names()
    package_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "openadapt_capture").rglob("*.py")
    )
    assert "pynput" not in package_sources


def test_external_ffmpeg_keeps_core_recorder_video_first_and_pyav_free() -> None:
    """Video stays default without linking PyAV/FFmpeg into the MIT package."""
    assert "av" not in _runtime_dependency_names()
    package_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "openadapt_capture").rglob("*.py")
    )
    assert "import av" not in package_sources

    from openadapt_capture.config import config

    assert config.RECORD_VIDEO is True
    assert config.RECORD_IMAGES is False


def test_performance_plotting_is_opt_in() -> None:
    """A clean first shutdown must not build Matplotlib's font cache."""
    from openadapt_capture.config import Settings

    assert Settings(_env_file=None).PLOT_PERFORMANCE is False


def test_default_install_exposes_recorder_without_opt_in_stacks() -> None:
    """The package API, Recorder and CaptureSession import with the extras blocked."""
    script = f"""
import importlib.abc
import sys

BLOCKED = {OPTIONAL_MODULES!r}

class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"blocked optional {{fullname}}", name=fullname)
        return None

sys.meta_path.insert(0, BlockOptional())
import openadapt_capture
from openadapt_capture import CaptureSession, Recorder
from openadapt_capture import cli, recorder
assert Recorder is recorder.Recorder and CaptureSession is not None
leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("CORE_IMPORT_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "The default package API needed an opt-in extra.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CORE_IMPORT_OK" in result.stdout


def test_default_package_import_and_recorder_construction_do_not_import_av() -> None:
    """The video-first API remains importable without in-process PyAV."""
    script = """
import importlib.abc
import sys

class BlockAv(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "av" or fullname.startswith("av."):
            raise ModuleNotFoundError("blocked optional av", name="av")
        return None

sys.meta_path.insert(0, BlockAv())
from openadapt_capture import Recorder
recorder = Recorder("/tmp/openadapt-capture-core-import-contract")
assert recorder._recording_config.capture_video is None
assert recorder._recording_config.capture_images is None
assert "av" not in sys.modules
print("CORE_RECORDER_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "The default Recorder path imported optional PyAV.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CORE_RECORDER_OK" in result.stdout


def test_log_memory_without_pympler_refuses_before_creating_a_capture(
    monkeypatch, tmp_path
) -> None:
    """A missing [profile] extra must not leave a half-started recording."""
    import pytest

    from openadapt_capture import recorder

    monkeypatch.setitem(sys.modules, "pympler", None)
    monkeypatch.setitem(sys.modules, "pympler.tracker", None)

    def must_not_run(*args, **kwargs):
        raise AssertionError("create_recording ran before the Pympler check")

    monkeypatch.setattr(recorder, "create_recording", must_not_run)
    with pytest.raises(ImportError, match=r"openadapt-capture\[profile\]"):
        recorder.record("task", str(tmp_path / "capture"), log_memory=True)
    assert not (tmp_path / "capture").exists()


def test_missing_portaudio_gives_the_install_hint(monkeypatch) -> None:
    """sounddevice raises OSError without PortAudio; report it like a missing extra."""
    import importlib.abc

    import pytest

    from openadapt_capture import audio

    class NoPortAudio(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "sounddevice":
                raise OSError("PortAudio library not found")
            return None

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(audio, "_sounddevice", None)
    monkeypatch.setattr(sys, "meta_path", [NoPortAudio(), *sys.meta_path])
    with pytest.raises(ImportError, match="PortAudio"):
        audio.require_audio_dependencies()
