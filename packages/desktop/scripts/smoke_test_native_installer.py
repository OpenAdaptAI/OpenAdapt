#!/usr/bin/env python3
"""Structurally install, verify, and uninstall one native desktop artifact.

The helper intentionally uses only Python's standard library and native operating
system tools.  It is designed for clean CI runners: it refuses to replace an
existing application, never invokes a shell, and always attempts native uninstall
after an install attempt.

The default ``unsigned`` mode does not launch the GUI or make a signing claim.
Credential-aware modes add native signature, policy, and notarization checks.

Passing ``--launch-seconds N`` additionally launches the installed application
and requires the process to still be alive N seconds later, then terminates the
whole process tree before uninstalling.  This catches startup panics that the
structural checks cannot see (for example a Tauri plugin whose required config
is absent, which aborts every launch on every platform -- issue #26).  The
probe retries once to absorb slow cold starts on shared CI runners.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

SIGNING_MODES = (
    "unsigned",
    "adhoc",
    "developer-id-notarized",
    "authenticode",
    "gpg",
)


class SmokeTestError(RuntimeError):
    """Raised when an installer does not satisfy the lifecycle contract."""


@dataclass(frozen=True)
class SmokeTestResult:
    """Description of a successfully tested artifact."""

    platform: str
    installer_kind: str
    artifact: Path
    app_path: Path
    signing_mode: str
    expected_architecture: str | None


def _tail(value: str, limit: int = 2_000) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[-limit:]


def run_command(
    args: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    ok_returncodes: Iterable[int] | None = (0,),
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and enforce its allowed return codes."""

    command = [os.fspath(arg) for arg in args]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeTestError(f"could not run {command[0]!r}: {exc}") from exc

    if ok_returncodes is not None and result.returncode not in set(ok_returncodes):
        output = _tail("\n".join(part for part in (result.stdout, result.stderr) if part))
        detail = f"; output: {output}" if output else ""
        raise SmokeTestError(f"command {command!r} exited with {result.returncode}{detail}")
    return result


def _native_platform(value: str | None = None) -> str:
    value = value or sys.platform
    if value == "darwin":
        return "macos"
    if value in {"win32", "cygwin"}:
        return "windows"
    if value.startswith("linux"):
        return "linux"
    raise SmokeTestError(f"unsupported operating system: {value}")


def _is_lexical_path(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _absolute_unused_path(value: Path, *, label: str) -> Path:
    if not value.is_absolute():
        raise SmokeTestError(f"{label} must be an absolute path: {value}")
    path = value.resolve(strict=False)
    if path == Path(path.anchor):
        raise SmokeTestError(f"{label} may not be a filesystem root: {path}")
    if _is_lexical_path(path):
        raise SmokeTestError(f"refusing to overwrite or uninstall an existing {label}: {path}")
    return path


def _read_prefix(path: Path, size: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(size)


def _validate_artifact(value: Path, platform: str) -> tuple[Path, str]:
    if value.is_symlink():
        raise SmokeTestError(f"artifact may not be a symbolic link: {value}")
    try:
        artifact = value.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SmokeTestError(f"artifact does not exist: {value}") from exc

    suffix = artifact.suffix.lower()
    if platform == "macos" and artifact.is_dir() and suffix == ".app":
        _validate_macos_app(artifact)
        return artifact, "app"

    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise SmokeTestError(f"artifact must be a non-empty regular file: {artifact}")

    allowed = {
        "macos": {".dmg": "dmg"},
        "windows": {".msi": "msi", ".exe": "nsis"},
        "linux": {".deb": "deb", ".rpm": "rpm", ".appimage": "appimage"},
    }[platform]
    try:
        kind = allowed[suffix]
    except KeyError as exc:
        expected = ", ".join(sorted(allowed))
        raise SmokeTestError(
            f"artifact {artifact.name!r} is not a {platform} installer "
            f"(expected one of: {expected})"
        ) from exc

    signatures = {
        "msi": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "nsis": b"MZ",
        "deb": b"!<arch>\n",
        "rpm": b"\xed\xab\xee\xdb",
        "appimage": b"\x7fELF",
    }
    if kind in signatures and not _read_prefix(artifact, len(signatures[kind])).startswith(
        signatures[kind]
    ):
        raise SmokeTestError(f"artifact does not have a valid {kind} file signature: {artifact}")
    if kind == "dmg":
        if artifact.stat().st_size < 512:
            raise SmokeTestError(f"DMG artifact is too small to contain a UDIF trailer: {artifact}")
        with artifact.open("rb") as stream:
            stream.seek(-512, os.SEEK_END)
            if stream.read(4) != b"koly":
                raise SmokeTestError(f"artifact does not have a valid DMG UDIF trailer: {artifact}")

    return artifact, kind


def _validate_macos_app(app_path: Path) -> Path:
    if not app_path.is_dir() or app_path.suffix.lower() != ".app":
        raise SmokeTestError(f"expected an installed macOS .app bundle: {app_path}")
    info_path = app_path / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SmokeTestError(f"could not read app bundle metadata {info_path}: {exc}") from exc
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise SmokeTestError(f"app bundle has no CFBundleExecutable: {info_path}")
    executable = app_path / "Contents" / "MacOS" / executable_name
    if (
        not executable.is_file()
        or executable.stat().st_size == 0
        or not os.access(executable, os.X_OK)
    ):
        raise SmokeTestError(
            f"app bundle executable is missing, empty, or not executable: {executable}"
        )
    return executable


def _verify_macos_embedded_flow_runtime(app_path: Path, *, timeout: float) -> None:
    """Execute the packaged sidecar after Tauri's final signing pass.

    A structural signature check is insufficient for a PyInstaller one-file
    sidecar: re-signing only its launcher with hardened runtime can leave the
    embedded Python libraries unloadable. This subprocess crosses that exact
    extraction and library-validation boundary without launching the GUI.
    """

    sidecar = app_path / "Contents" / "MacOS" / "openadapt-engine"
    if not sidecar.is_file() or not os.access(sidecar, os.X_OK):
        raise SmokeTestError(f"installed app has no executable engine sidecar: {sidecar}")
    result = run_command(
        [sidecar, "__openadapt_flow__", "--version"],
        timeout=max(timeout, 60.0),
    )
    output = _combined_output(result)
    if "openadapt-flow 1.19.0" not in output:
        raise SmokeTestError(f"installed engine has the wrong Flow runtime: {_tail(output)}")


def _validate_installed_executable(app_path: Path, platform: str) -> None:
    if not app_path.is_file() or app_path.stat().st_size == 0:
        raise SmokeTestError(f"installed application is missing or empty: {app_path}")
    if platform != "windows" and not os.access(app_path, os.X_OK):
        raise SmokeTestError(f"installed application is not executable: {app_path}")


def _pe_architecture(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            dos_header = stream.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                raise SmokeTestError(f"installed executable has no valid DOS header: {path}")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            if pe_offset < 64 or pe_offset > path.stat().st_size - 6:
                raise SmokeTestError(f"installed executable has an invalid PE offset: {path}")
            stream.seek(pe_offset)
            pe_header = stream.read(6)
    except OSError as exc:
        raise SmokeTestError(f"could not inspect PE executable {path}: {exc}") from exc
    if pe_header[:4] != b"PE\x00\x00":
        raise SmokeTestError(f"installed executable has no valid PE signature: {path}")
    machine = struct.unpack_from("<H", pe_header, 4)[0]
    architectures = {0x8664: "x86_64", 0xAA64: "arm64"}
    if machine not in architectures:
        raise SmokeTestError(f"unsupported PE machine type 0x{machine:04x}: {path}")
    return architectures[machine]


def _elf_architecture(path: Path) -> str:
    try:
        header = _read_prefix(path, 20)
    except OSError as exc:
        raise SmokeTestError(f"could not inspect ELF executable {path}: {exc}") from exc
    if len(header) != 20 or header[:4] != b"\x7fELF":
        raise SmokeTestError(f"installed executable has no valid ELF header: {path}")
    byte_order = {1: "<", 2: ">"}.get(header[5])
    if byte_order is None:
        raise SmokeTestError(f"installed ELF executable has invalid byte order: {path}")
    machine = struct.unpack_from(f"{byte_order}H", header, 18)[0]
    architectures = {62: "x86_64", 183: "arm64"}
    if machine not in architectures:
        raise SmokeTestError(f"unsupported ELF machine type {machine}: {path}")
    return architectures[machine]


def _verify_expected_architecture(
    app_path: Path,
    *,
    platform: str,
    expected_architecture: str | None,
    timeout: float,
) -> None:
    if expected_architecture is None:
        return
    if platform == "macos":
        executable = _validate_macos_app(app_path)
        result = run_command(["lipo", "-archs", executable], timeout=timeout)
        actual_architectures = set(result.stdout.split())
        unsupported = actual_architectures.difference({"arm64", "x86_64"})
        if not actual_architectures or unsupported:
            raise SmokeTestError(
                f"could not determine supported Mach-O architectures for {executable}: "
                f"{sorted(actual_architectures)}"
            )
    elif platform == "windows":
        actual_architectures = {_pe_architecture(app_path)}
    else:
        actual_architectures = {_elf_architecture(app_path)}
    if expected_architecture not in actual_architectures:
        raise SmokeTestError(
            f"installed executable architecture mismatch: expected {expected_architecture}, "
            f"found {sorted(actual_architectures)} at {app_path}"
        )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _verify_macos_code_signature(path: Path, *, identity: str, timeout: float) -> None:
    run_command(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", path],
        timeout=timeout,
    )
    display = run_command(["codesign", "--display", "--verbose=4", path], timeout=timeout)
    lines = {line.strip() for line in _combined_output(display).splitlines()}
    if identity == "adhoc":
        if "Signature=adhoc" not in lines:
            raise SmokeTestError(f"expected an ad-hoc code signature on {path}")
        return
    if not any(line.startswith("Authority=Developer ID Application:") for line in lines):
        raise SmokeTestError(f"expected a Developer ID Application authority on {path}")
    if "TeamIdentifier=not set" in lines or not any(
        line.startswith("TeamIdentifier=") for line in lines
    ):
        raise SmokeTestError(f"Developer ID signature has no team identifier: {path}")


def _verify_macos_installed_signature(path: Path, *, signing_mode: str, timeout: float) -> None:
    identity = "adhoc" if signing_mode == "adhoc" else "developer-id"
    _verify_macos_code_signature(path, identity=identity, timeout=timeout)
    if signing_mode == "developer-id-notarized":
        run_command(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", path],
            timeout=timeout,
        )


def _verify_macos_release_artifact(artifact: Path, *, kind: str, timeout: float) -> None:
    _verify_macos_code_signature(artifact, identity="developer-id", timeout=timeout)
    if kind == "dmg":
        run_command(
            [
                "spctl",
                "--assess",
                "--type",
                "open",
                "--context",
                "context:primary-signature",
                "--verbose=4",
                artifact,
            ],
            timeout=timeout,
        )
    else:
        run_command(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", artifact],
            timeout=timeout,
        )
    run_command(["xcrun", "stapler", "validate", artifact], timeout=timeout)


def _normalize_fingerprint(value: str, *, lengths: set[int]) -> str:
    normalized = re.sub(r"[\s:]", "", value).upper()
    if len(normalized) not in lengths or re.fullmatch(r"[0-9A-F]+", normalized) is None:
        expected = " or ".join(str(length) for length in sorted(lengths))
        raise SmokeTestError(
            f"signing fingerprint must contain exactly {expected} hexadecimal characters"
        )
    return normalized


def _verify_authenticode(path: Path, *, fingerprint: str | None, timeout: float) -> None:
    expected = _normalize_fingerprint(fingerprint, lengths={40}) if fingerprint else ""
    script = (
        "& { param([string] $Target, [string] $ExpectedThumbprint) "
        "$signature = Get-AuthenticodeSignature -LiteralPath $Target; "
        "if ($signature.Status -ne 'Valid') { "
        "throw ('Authenticode status is ' + $signature.Status) }; "
        "if ($null -eq $signature.SignerCertificate) { throw 'Missing signer certificate' }; "
        "$thumbprint = $signature.SignerCertificate.Thumbprint.Replace(' ', '')"
        ".ToUpperInvariant(); "
        "if ($ExpectedThumbprint -and $thumbprint -ne $ExpectedThumbprint) { "
        "throw ('Signer thumbprint mismatch: ' + $thumbprint) }; "
        "Write-Output ('VALIDSIGNER=' + $thumbprint) }"
    )
    result = run_command(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            path,
            expected,
        ],
        timeout=timeout,
    )
    valid_lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("VALIDSIGNER=")
    ]
    if len(valid_lines) != 1 or not valid_lines[0].removeprefix("VALIDSIGNER="):
        raise SmokeTestError(f"PowerShell did not report a valid Authenticode signer: {path}")


def _terminate_process_tree(process: subprocess.Popen[bytes], timeout: float) -> None:
    """Stop the launched application and every child it spawned (sidecars)."""

    if process.poll() is None:
        if sys.platform == "win32":
            # taskkill /T is the only stdlib-adjacent way to reap the WebView2
            # and sidecar children before the uninstaller runs.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        else:
            import signal

            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=min(timeout, 10.0))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    try:
        process.wait(timeout=min(timeout, 10.0))
    except subprocess.TimeoutExpired as exc:
        raise SmokeTestError(
            f"launched application did not terminate after kill: pid {process.pid}"
        ) from exc


def _launch_probe(
    executable: Path,
    *,
    run_seconds: float,
    timeout: float,
    attempts: int = 2,
) -> None:
    """Launch the installed application and require it to stay alive.

    A startup panic (for example a plugin whose required configuration cannot
    deserialize) exits within roughly a second, so surviving ``run_seconds``
    is a meaningful "the app actually starts" gate.  One retry absorbs slow
    cold starts on shared CI runners without hiding deterministic crashes.
    """

    env = dict(os.environ)
    # Lets the AppImage runtime self-extract on runners without FUSE; the
    # variable is ignored by every other launcher.
    env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")

    last_failure = ""
    for attempt in range(1, attempts + 1):
        try:
            process = subprocess.Popen(
                [os.fspath(executable)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=(sys.platform != "win32"),
            )
        except OSError as exc:
            raise SmokeTestError(
                f"could not launch installed application {executable}: {exc}"
            ) from exc

        deadline = time.monotonic() + run_seconds
        returncode: int | None = None
        while time.monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                break
            time.sleep(0.25)

        if returncode is None:
            _terminate_process_tree(process, timeout)
            return

        try:
            stdout, stderr = process.communicate(timeout=min(timeout, 10.0))
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        output = "\n".join(
            stream.decode("utf-8", errors="replace") for stream in (stdout, stderr) if stream
        )
        last_failure = (
            f"launched application exited with status {returncode} within "
            f"{run_seconds:.0f}s: {executable}"
        )
        if output.strip():
            last_failure += f"; output: {_tail(output)}"
        if attempt < attempts:
            time.sleep(2.0)

    raise SmokeTestError(last_failure)


def _wait_for_absence(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + min(timeout, 10.0)
    while _is_lexical_path(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _is_lexical_path(path):
        raise SmokeTestError(f"uninstall left the application behind: {path}")


def _raise_lifecycle_errors(primary: Exception | None, cleanup_errors: list[Exception]) -> None:
    if primary is None and not cleanup_errors:
        return
    if primary is None:
        raise SmokeTestError(
            "uninstall verification failed: " + "; ".join(str(error) for error in cleanup_errors)
        ) from cleanup_errors[0]
    if cleanup_errors:
        raise SmokeTestError(
            f"installer verification failed: {primary}; cleanup also failed: "
            + "; ".join(str(error) for error in cleanup_errors)
        ) from primary
    if isinstance(primary, SmokeTestError):
        raise primary
    raise SmokeTestError(f"installer verification failed: {primary}") from primary


def _macos_smoke(
    artifact: Path,
    kind: str,
    app_path: Path,
    timeout: float,
    verify_installed: Callable[[Path], None],
) -> None:
    if app_path.suffix.lower() != ".app":
        raise SmokeTestError(f"macOS --app-path must end in .app: {app_path}")

    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    attached = False
    copied = False
    with tempfile.TemporaryDirectory(prefix="openadapt-installer-smoke-") as directory:
        temporary = Path(directory)
        mountpoint = temporary / "mounted"
        mountpoint.mkdir()
        source_app = artifact
        try:
            if kind == "dmg":
                run_command(
                    [
                        "hdiutil",
                        "attach",
                        artifact,
                        "-readonly",
                        "-nobrowse",
                        "-mountpoint",
                        mountpoint,
                    ],
                    timeout=timeout,
                )
                attached = True
                candidates = [
                    candidate
                    for candidate in mountpoint.iterdir()
                    if candidate.name == app_path.name
                    and candidate.is_dir()
                    and candidate.suffix.lower() == ".app"
                ]
                if len(candidates) != 1:
                    found = sorted(candidate.name for candidate in mountpoint.glob("*.app"))
                    raise SmokeTestError(
                        "DMG must contain exactly one root app named "
                        f"{app_path.name!r}; found {found}"
                    )
                source_app = candidates[0]

            _validate_macos_app(source_app)
            app_path.parent.mkdir(parents=True, exist_ok=True)
            run_command(["ditto", source_app, app_path], timeout=timeout)
            copied = True
            _validate_macos_app(app_path)
            verify_installed(app_path)
        except Exception as exc:
            primary = exc
        finally:
            if copied or _is_lexical_path(app_path):
                try:
                    if app_path.is_dir() and not app_path.is_symlink():
                        shutil.rmtree(app_path)
                    elif _is_lexical_path(app_path):
                        app_path.unlink()
                    _wait_for_absence(app_path, timeout)
                except Exception as exc:
                    cleanup_errors.append(exc)
            if attached:
                try:
                    run_command(["hdiutil", "detach", mountpoint], timeout=timeout)
                except Exception as exc:
                    cleanup_errors.append(exc)

    _raise_lifecycle_errors(primary, cleanup_errors)


def _windows_msi_smoke(
    artifact: Path,
    app_path: Path,
    timeout: float,
    verify_installed: Callable[[Path], None],
) -> None:
    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    attempted = False
    try:
        attempted = True
        run_command(
            ["msiexec.exe", "/i", artifact, "/qn", "/norestart"],
            timeout=timeout,
            ok_returncodes=(0, 3010),
        )
        _validate_installed_executable(app_path, "windows")
        verify_installed(app_path)
    except Exception as exc:
        primary = exc
    finally:
        if attempted:
            try:
                run_command(
                    ["msiexec.exe", "/x", artifact, "/qn", "/norestart"],
                    timeout=timeout,
                    # 1605 means no installed product remains after a failed install.
                    ok_returncodes=(0, 1605, 3010),
                )
                _wait_for_absence(app_path, timeout)
            except Exception as exc:
                cleanup_errors.append(exc)
    _raise_lifecycle_errors(primary, cleanup_errors)


def _windows_nsis_smoke(
    artifact: Path,
    app_path: Path,
    uninstaller_path: Path | None,
    timeout: float,
    verify_installed: Callable[[Path], None],
    verify_uninstaller: Callable[[Path], None],
) -> None:
    if uninstaller_path is None:
        raise SmokeTestError("Windows .exe installers require an explicit --uninstaller-path")

    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    attempted = False
    try:
        attempted = True
        run_command([artifact, "/S"], timeout=timeout)
        _validate_installed_executable(app_path, "windows")
        _validate_installed_executable(uninstaller_path, "windows")
        verify_installed(app_path)
        verify_uninstaller(uninstaller_path)
    except Exception as exc:
        primary = exc
    finally:
        if attempted:
            try:
                if uninstaller_path.is_file():
                    run_command([uninstaller_path, "/S"], timeout=timeout)
                elif _is_lexical_path(app_path):
                    raise SmokeTestError(
                        f"installer created {app_path} without the declared uninstaller "
                        f"{uninstaller_path}"
                    )
                _wait_for_absence(app_path, timeout)
                _wait_for_absence(uninstaller_path, timeout)
            except Exception as exc:
                cleanup_errors.append(exc)
    _raise_lifecycle_errors(primary, cleanup_errors)


def _sudo_prefix() -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if shutil.which("sudo") is None:
        raise SmokeTestError("native Linux package installation requires root or sudo")
    return ["sudo", "--non-interactive"]


def _probe(
    args: Sequence[str | os.PathLike[str]], timeout: float
) -> subprocess.CompletedProcess[str]:
    result = run_command(args, timeout=timeout, ok_returncodes=None)
    if result.returncode not in {0, 1}:
        raise SmokeTestError(
            f"package-state query {list(map(os.fspath, args))!r} exited with "
            f"unexpected status {result.returncode}"
        )
    return result


def _deb_payload_present(state: subprocess.CompletedProcess[str]) -> bool:
    if state.returncode != 0:
        return False
    abbreviation = state.stdout.strip()
    if len(abbreviation) < 2:
        raise SmokeTestError(f"invalid dpkg status abbreviation: {state.stdout!r}")
    # The second character is the current state. `n` (not installed) and `c`
    # (only configuration files remain) both mean the executable payload is gone.
    return abbreviation[1] not in {"n", "c"}


def _linux_deb_smoke(
    artifact: Path,
    app_path: Path,
    timeout: float,
    verify_installed: Callable[[Path], None],
) -> None:
    package_result = run_command(["dpkg-deb", "--field", artifact, "Package"], timeout=timeout)
    package_name = package_result.stdout.strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", package_name) is None:
        raise SmokeTestError(f"could not determine a safe DEB package name: {package_name!r}")
    query = ["dpkg-query", "--show", "--showformat=${db:Status-Abbrev}", package_name]
    if _probe(query, timeout).returncode == 0:
        raise SmokeTestError(f"refusing to replace an installed DEB package: {package_name}")

    prefix = _sudo_prefix()
    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    attempted = False
    try:
        attempted = True
        run_command([*prefix, "dpkg", "--install", artifact], timeout=timeout)
        state = _probe(query, timeout)
        if state.returncode != 0 or not state.stdout.strip().startswith("ii"):
            raise SmokeTestError(
                f"DEB package is not fully installed after dpkg completed: {state.stdout!r}"
            )
        _validate_installed_executable(app_path, "linux")
        verify_installed(app_path)
    except Exception as exc:
        primary = exc
    finally:
        if attempted:
            try:
                state = _probe(query, timeout)
                if _deb_payload_present(state):
                    run_command([*prefix, "dpkg", "--remove", package_name], timeout=timeout)
                if _deb_payload_present(_probe(query, timeout)):
                    raise SmokeTestError(f"DEB package remains installed: {package_name}")
                _wait_for_absence(app_path, timeout)
            except Exception as exc:
                cleanup_errors.append(exc)
    _raise_lifecycle_errors(primary, cleanup_errors)


def _linux_rpm_smoke(
    artifact: Path,
    app_path: Path,
    timeout: float,
    verify_installed: Callable[[Path], None],
) -> None:
    package_result = run_command(
        ["rpm", "--query", "--package", "--queryformat", "%{NAME}", artifact],
        timeout=timeout,
    )
    package_name = package_result.stdout.strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", package_name) is None:
        raise SmokeTestError(f"could not determine a safe RPM package name: {package_name!r}")
    query = ["rpm", "--query", package_name]
    if _probe(query, timeout).returncode == 0:
        raise SmokeTestError(f"refusing to replace an installed RPM package: {package_name}")

    prefix = _sudo_prefix()
    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    attempted = False
    try:
        attempted = True
        run_command([*prefix, "rpm", "--install", artifact], timeout=timeout)
        if _probe(query, timeout).returncode != 0:
            raise SmokeTestError(
                f"RPM package is not installed after rpm completed: {package_name}"
            )
        _validate_installed_executable(app_path, "linux")
        verify_installed(app_path)
    except Exception as exc:
        primary = exc
    finally:
        if attempted:
            try:
                if _probe(query, timeout).returncode == 0:
                    run_command([*prefix, "rpm", "--erase", package_name], timeout=timeout)
                if _probe(query, timeout).returncode == 0:
                    raise SmokeTestError(f"RPM package remains installed: {package_name}")
                _wait_for_absence(app_path, timeout)
            except Exception as exc:
                cleanup_errors.append(exc)
    _raise_lifecycle_errors(primary, cleanup_errors)


def _linux_appimage_smoke(
    artifact: Path,
    app_path: Path,
    timeout: float,
    verify_installed: Callable[[Path], None],
) -> None:
    primary: Exception | None = None
    cleanup_errors: list[Exception] = []
    copied = False
    try:
        app_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, app_path)
        copied = True
        app_path.chmod(app_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _validate_installed_executable(app_path, "linux")
        verify_installed(app_path)
    except Exception as exc:
        primary = exc
    finally:
        if copied or _is_lexical_path(app_path):
            try:
                app_path.unlink()
                _wait_for_absence(app_path, timeout)
            except Exception as exc:
                cleanup_errors.append(exc)
    _raise_lifecycle_errors(primary, cleanup_errors)


def _prepare_signature_verifier(
    *,
    platform: str,
    kind: str,
    artifact: Path,
    signing_mode: str,
    signing_fingerprint: str | None,
    timeout: float,
) -> Callable[[Path], None]:
    if signing_mode not in SIGNING_MODES:
        raise SmokeTestError(
            f"unknown signing mode {signing_mode!r}; expected one of {SIGNING_MODES}"
        )
    supported = {
        "macos": {"unsigned", "adhoc", "developer-id-notarized"},
        "windows": {"unsigned", "authenticode"},
        "linux": {"unsigned", "gpg"},
    }[platform]
    if signing_mode not in supported:
        raise SmokeTestError(
            f"signing mode {signing_mode!r} is not supported for {platform} artifacts"
        )
    if signing_fingerprint and signing_mode not in {"authenticode", "gpg"}:
        raise SmokeTestError(
            "--signing-fingerprint is supported only with authenticode or gpg mode"
        )
    if signing_mode == "unsigned":
        return lambda path: None
    if signing_mode == "gpg":
        if kind != "appimage":
            raise SmokeTestError("gpg mode is supported only for AppImage artifacts")
        if signing_fingerprint:
            _normalize_fingerprint(signing_fingerprint, lengths={40, 64})
        raise SmokeTestError(
            "gpg AppImage verification is unavailable in this self-contained helper: "
            "the embedded signature covers a format-specific digest that skips the "
            "signature/digest ELF sections and requires a pinned AppImage validator; "
            "raw gpg verification would not establish artifact integrity"
        )
    if signing_mode == "developer-id-notarized":
        _verify_macos_release_artifact(artifact, kind=kind, timeout=timeout)
        return lambda path: _verify_macos_installed_signature(
            path, signing_mode=signing_mode, timeout=timeout
        )
    if signing_mode == "adhoc":
        return lambda path: _verify_macos_installed_signature(
            path, signing_mode=signing_mode, timeout=timeout
        )

    _verify_authenticode(artifact, fingerprint=signing_fingerprint, timeout=timeout)
    return lambda path: _verify_authenticode(path, fingerprint=signing_fingerprint, timeout=timeout)


def smoke_test_installer(
    artifact_value: Path,
    app_path_value: Path,
    *,
    uninstaller_path_value: Path | None = None,
    allow_system_install: bool = False,
    signing_mode: str = "unsigned",
    signing_fingerprint: str | None = None,
    expected_architecture: str | None = None,
    timeout: float = 300.0,
    platform_value: str | None = None,
    launch_seconds: float = 0.0,
) -> SmokeTestResult:
    """Exercise a native artifact's complete install/uninstall lifecycle."""

    if timeout <= 0:
        raise SmokeTestError("timeout must be greater than zero")
    if launch_seconds < 0:
        raise SmokeTestError("launch seconds may not be negative")
    if expected_architecture not in {None, "arm64", "x86_64"}:
        raise SmokeTestError("expected architecture must be either 'arm64' or 'x86_64'")
    platform = _native_platform(platform_value)
    artifact, kind = _validate_artifact(artifact_value, platform)
    app_path = _absolute_unused_path(app_path_value, label="app path")
    uninstaller_path = None
    if uninstaller_path_value is not None:
        uninstaller_path = _absolute_unused_path(uninstaller_path_value, label="uninstaller path")
        if uninstaller_path == app_path:
            raise SmokeTestError("app path and uninstaller path must differ")

    if kind in {"msi", "nsis", "deb", "rpm"} and not allow_system_install:
        raise SmokeTestError(
            f"{kind} changes native installer/package state; pass --allow-system-install "
            "only on a disposable clean runner"
        )

    verify_signature = _prepare_signature_verifier(
        platform=platform,
        kind=kind,
        artifact=artifact,
        signing_mode=signing_mode,
        signing_fingerprint=signing_fingerprint,
        timeout=timeout,
    )

    def verify_installed(path: Path) -> None:
        _verify_expected_architecture(
            path,
            platform=platform,
            expected_architecture=expected_architecture,
            timeout=timeout,
        )
        verify_signature(path)
        if platform == "macos":
            _verify_macos_embedded_flow_runtime(path, timeout=timeout)
        if launch_seconds > 0:
            executable = _validate_macos_app(path) if platform == "macos" else path
            _launch_probe(executable, run_seconds=launch_seconds, timeout=timeout)

    if platform == "macos":
        _macos_smoke(artifact, kind, app_path, timeout, verify_installed)
    elif platform == "windows" and kind == "msi":
        _windows_msi_smoke(artifact, app_path, timeout, verify_installed)
    elif platform == "windows" and kind == "nsis":
        _windows_nsis_smoke(
            artifact,
            app_path,
            uninstaller_path,
            timeout,
            verify_installed,
            verify_signature,
        )
    elif platform == "linux" and kind == "deb":
        _linux_deb_smoke(artifact, app_path, timeout, verify_installed)
    elif platform == "linux" and kind == "rpm":
        _linux_rpm_smoke(artifact, app_path, timeout, verify_installed)
    elif platform == "linux" and kind == "appimage":
        _linux_appimage_smoke(artifact, app_path, timeout, verify_installed)
    else:  # pragma: no cover - mapping above is deliberately exhaustive
        raise SmokeTestError(f"no lifecycle implementation for {platform} {kind}")

    if _is_lexical_path(app_path):
        raise SmokeTestError(f"successful smoke test left the application installed: {app_path}")
    return SmokeTestResult(platform, kind, artifact, app_path, signing_mode, expected_architecture)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, verify, uninstall, and verify removal of one native installer."
    )
    parser.add_argument("--artifact", required=True, type=Path, help="Native installer path")
    parser.add_argument(
        "--app-path",
        required=True,
        type=Path,
        help="Absolute installed .app or executable path; it must not already exist",
    )
    parser.add_argument(
        "--uninstaller-path",
        type=Path,
        help="Absolute uninstaller path (required for a Windows NSIS .exe)",
    )
    parser.add_argument(
        "--allow-system-install",
        action="store_true",
        help="Allow MSI, NSIS, DEB, or RPM state changes on this disposable runner",
    )
    parser.add_argument(
        "--signing-mode",
        choices=SIGNING_MODES,
        default="unsigned",
        help="Native signature policy (default: structural unsigned smoke only)",
    )
    parser.add_argument(
        "--signing-fingerprint",
        help="Optional Authenticode certificate thumbprint or GPG fingerprint",
    )
    parser.add_argument(
        "--expected-architecture",
        choices=("arm64", "x86_64"),
        help="Require this machine type in the installed Mach-O, PE, or ELF executable",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="Per-command timeout in seconds"
    )
    parser.add_argument(
        "--launch-seconds",
        type=float,
        default=0.0,
        help=(
            "Launch the installed application and require the process to stay "
            "alive this many seconds before uninstalling (0 disables the probe)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = smoke_test_installer(
            args.artifact,
            args.app_path,
            uninstaller_path_value=args.uninstaller_path,
            allow_system_install=args.allow_system_install,
            signing_mode=args.signing_mode,
            signing_fingerprint=args.signing_fingerprint,
            expected_architecture=args.expected_architecture,
            timeout=args.timeout,
            launch_seconds=args.launch_seconds,
        )
    except SmokeTestError as exc:
        parser.exit(1, f"native installer smoke test failed: {exc}\n")
    architecture = result.expected_architecture or "unconstrained architecture"
    print(
        f"Verified {result.platform} {result.installer_kind} install/uninstall "
        f"with {result.signing_mode} signing policy and {architecture}: "
        f"{result.artifact}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
