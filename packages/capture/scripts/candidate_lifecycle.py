#!/usr/bin/env python3
"""Install, inspect, and remove one exact Capture candidate wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

CONSUMER_RELEASES = {
    "openadapt-desktop": "0.16.0",
    "openadapt-flow": "1.34.0",
}


class CandidateLifecycleError(RuntimeError):
    """The candidate artifact or its clean install lifecycle is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(dist_dir: Path, manifest_path: Path) -> dict[str, str]:
    """Verify that the manifest accounts for exactly one wheel and one sdist."""

    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            raise CandidateLifecycleError(
                f"invalid SHA256 manifest line {line_number}: {raw_line!r}"
            )
        digest, name = parts
        name = name.removeprefix("*")
        if Path(name).name != name or any(char not in "0123456789abcdef" for char in digest):
            raise CandidateLifecycleError(
                f"unsafe SHA256 manifest line {line_number}: {raw_line!r}"
            )
        if name in expected:
            raise CandidateLifecycleError(f"duplicate SHA256 manifest entry: {name}")
        expected[name] = digest

    archives = sorted(
        path
        for path in dist_dir.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    names = {path.name for path in archives}
    if len([path for path in archives if path.suffix == ".whl"]) != 1:
        raise CandidateLifecycleError("the candidate must contain exactly one wheel")
    if len([path for path in archives if path.name.endswith(".tar.gz")]) != 1:
        raise CandidateLifecycleError("the candidate must contain exactly one source distribution")
    if names != set(expected):
        raise CandidateLifecycleError(
            "the SHA256 manifest and candidate archives differ: "
            f"manifest={sorted(expected)}, archives={sorted(names)}"
        )
    for archive in archives:
        actual = _sha256(archive)
        if actual != expected[archive.name]:
            raise CandidateLifecycleError(
                f"SHA256 mismatch for {archive.name}: expected {expected[archive.name]}, got {actual}"
            )
    return expected


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def _wheel_requirement(wheel: Path, *, with_linux_extra: bool) -> str:
    requirement = str(wheel.resolve())
    return f"{requirement}[linux]" if with_linux_extra else requirement


def run_lifecycle(
    wheel: Path,
    output: Path,
    *,
    candidate_sha: str,
    with_linux_extra: bool = False,
    with_ffmpeg_runtime: bool = False,
) -> dict[str, object]:
    """Run a network-resolved clean install and uninstall lifecycle."""

    with tempfile.TemporaryDirectory(prefix="openadapt-capture-candidate-") as temporary:
        root = Path(temporary)
        environment_dir = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_dir)
        python = _venv_python(environment_dir)
        environment = _clean_environment()
        environment["OPENADAPT_CAPTURE_DATA_DIR"] = str(root / "capture-data")

        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-input",
                _wheel_requirement(wheel, with_linux_extra=with_linux_extra),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        inspection = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import inspect, json, tempfile; "
                    "from pathlib import Path; "
                    "from importlib.metadata import distribution; "
                    "from openadapt_capture import CaptureSession, Recorder; "
                    "from openadapt_capture.capture import Action; "
                    "from openadapt_capture.cli import main; "
                    "from openadapt_capture.db import create_db; "
                    "from openadapt_capture.db.models import ActionEvent, Recording, WindowEvent; "
                    "from openadapt_capture.events import KeyDownEvent, KeyUpEvent; "
                    "from openadapt_capture.processing import process_events; "
                    "from openadapt_capture.video import VideoWriter; "
                    "dist=distribution('openadapt-capture'); "
                    "eps=[ep for ep in dist.entry_points "
                    "if ep.group == 'console_scripts' and ep.name == 'capture']; "
                    "assert len(eps) == 1 and eps[0].value == 'openadapt_capture.cli:main'; "
                    "recorder_parameters=inspect.signature(Recorder).parameters; "
                    "assert all(name in recorder_parameters for name in "
                    "('capture_dir', 'task_description', 'window')); "
                    "assert all(hasattr(Recorder, name) for name in "
                    "('__enter__', '__exit__', 'stop', 'wait_for_ready', "
                    "'is_recording', 'event_count')); "
                    "consumer_dir=Path(tempfile.mkdtemp()) / 'consumer-capture'; "
                    "recorder=Recorder(capture_dir=str(consumer_dir), "
                    "task_description='consumer contract', window=None, "
                    "control_enabled=False); "
                    "assert recorder.capture_dir == str(consumer_dir.resolve()); "
                    "assert recorder.task_description == 'consumer contract'; "
                    "assert recorder.is_recording is False; "
                    "assert all(callable(getattr(CaptureSession, name)) for name in "
                    "('load', 'actions', 'get_frame_at')); "
                    "assert all(isinstance(getattr(CaptureSession, name), property) "
                    "for name in ('pixel_ratio', 'window_capture', 'desktop_capture')); "
                    "assert all(callable(item) for item in (create_db, process_events)); "
                    "chord=process_events(["
                    "KeyDownEvent(timestamp=1.00, key_name='ctrl'), "
                    "KeyDownEvent(timestamp=1.05, key_char='s'), "
                    "KeyUpEvent(timestamp=1.10, key_char='s'), "
                    "KeyUpEvent(timestamp=1.15, key_name='ctrl')]); "
                    "assert len(chord) == 1 and chord[0].type.value == 'key.shortcut'; "
                    "print(json.dumps({'version': dist.version, "
                    "'capture_session': CaptureSession.__name__, "
                    "'recorder': Recorder.__name__, 'action': Action.__name__, "
                    "'database_models': [ActionEvent.__name__, Recording.__name__, "
                    "WindowEvent.__name__], "
                    "'events': [KeyDownEvent.__name__, KeyUpEvent.__name__], "
                    "'recorder_parameters': sorted(recorder_parameters), "
                    "'capture_session_contract': "
                    "['load', 'actions', 'get_frame_at', 'pixel_ratio', "
                    "'window_capture', 'desktop_capture'], "
                    "'video_writer': VideoWriter.__name__, "
                    "'cli': main.__name__}, sort_keys=True))"
                ),
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        inspected = json.loads(inspection.stdout)

        linux_extra_verified = False
        if with_linux_extra:
            if not sys.platform.startswith("linux"):
                raise CandidateLifecycleError("--with-linux-extra requires a Linux host")
            subprocess.run(
                [
                    str(python),
                    "-c",
                    (
                        "import gi; "
                        "gi.require_version('Atspi', '2.0'); "
                        "from gi.repository import Atspi; "
                        "from openadapt_capture.structural_observer.linux "
                        "import LinuxATSpiStructuralObserver; "
                        "assert Atspi is not None; "
                        "assert LinuxATSpiStructuralObserver is not None"
                    ),
                ],
                cwd=root,
                env=environment,
                check=True,
            )
            linux_extra_verified = True

        ffmpeg_runtime_verified = False
        if with_ffmpeg_runtime:
            ffmpeg_check = subprocess.run(
                [
                    str(python),
                    "-c",
                    (
                        "import json, os, shutil; "
                        "from openadapt_capture import ffmpeg_runtime, video; "
                        "installed=ffmpeg_runtime.install(); "
                        "os.environ['OPENADAPT_FFMPEG_PATH']=''; "
                        "os.environ['OPENADAPT_FFPROBE_PATH']=''; "
                        "os.environ['OPENADAPT_DESKTOP_FFMPEG_PATH']=''; "
                        "os.environ['PATH']=os.pathsep.join(part for part in "
                        "os.environ.get('PATH', '').split(os.pathsep) "
                        "if part and not shutil.which('ffmpeg', path=part)); "
                        "video._desktop_data_dirs=lambda: []; "
                        "provision=video.require_video_encoder(); "
                        "assert provision.source == 'capture install-ffmpeg', provision.source; "
                        "assert provision.executable == installed.ffmpeg; "
                        "assert ffmpeg_runtime.uninstall(); "
                        "assert ffmpeg_runtime.find_installed_runtime() is None; "
                        "print(json.dumps({'codec': provision.codec, "
                        "'muxer': provision.muxer, 'source': provision.source}, sort_keys=True))"
                    ),
                ],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            inspected["ffmpeg_runtime"] = json.loads(ffmpeg_check.stdout)
            ffmpeg_runtime_verified = True
        subprocess.run(
            [
                str(python),
                "-m",
                "openadapt_capture.cli",
                "--help",
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [str(python), "-m", "pip", "uninstall", "--yes", "openadapt-capture"],
            cwd=root,
            env=environment,
            check=True,
        )
        removed = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, sys; "
                    "sys.exit(0 if importlib.util.find_spec('openadapt_capture') is None else 1)"
                ),
            ],
            cwd=root,
            env=environment,
            check=False,
        )
        if removed.returncode != 0:
            raise CandidateLifecycleError("openadapt_capture remained importable after uninstall")

    evidence: dict[str, object] = {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "installed_version": inspected["version"],
        "imports": {
            "CaptureSession": inspected["capture_session"],
            "Recorder": inspected["recorder"],
        },
        "consumer_contract": {
            "action": inspected["action"],
            "capture_session": inspected["capture_session_contract"],
            "database_models": inspected["database_models"],
            "events": inspected["events"],
            "recorder_parameters": inspected["recorder_parameters"],
            "video_writer": inspected["video_writer"],
            "consumer_releases": CONSUMER_RELEASES,
            "demonstrated_chord": "key.shortcut",
            "verified": True,
        },
        "ffmpeg_runtime": {
            **inspected.get("ffmpeg_runtime", {}),
            "verified": ffmpeg_runtime_verified,
        },
        "linux_extra_verified": linux_extra_verified,
        "cli_entry_point": "openadapt_capture.cli:main",
        "uninstall_verified": True,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--with-linux-extra", action="store_true")
    parser.add_argument("--with-ffmpeg-runtime", action="store_true")
    args = parser.parse_args()
    if len(args.candidate_sha) != 40 or any(
        char not in "0123456789abcdef" for char in args.candidate_sha
    ):
        raise SystemExit("--candidate-sha must be a lowercase 40-character Git commit SHA")

    verify_manifest(args.dist, args.manifest)
    wheels = list(args.dist.glob("*.whl"))
    evidence = run_lifecycle(
        wheels[0],
        args.output,
        candidate_sha=args.candidate_sha,
        with_linux_extra=args.with_linux_extra,
        with_ffmpeg_runtime=args.with_ffmpeg_runtime,
    )
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
