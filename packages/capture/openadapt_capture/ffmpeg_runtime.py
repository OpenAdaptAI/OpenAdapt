"""Opt-in provisioning of one pinned, hash-verified LGPL FFmpeg runtime.

The wheel and the source distribution carry no FFmpeg bytes. Nothing here runs
unless an operator asks for it by running ``capture install-ffmpeg``: Capture
never downloads on its own, and a missing runtime stays an error rather than a
silent fetch.

What the operator opts in to is one exact artifact per platform, built from the
upstream FFmpeg release tarball by ``openadapt-desktop`` and published as a
GitHub release asset. The build passes ``--disable-gpl``, ``--disable-nonfree``
and ``--disable-version3``, so it carries FFmpeg's default license. FFmpeg's own
``LICENSE.md``, installed beside the executables, states it: most files are
under the LGPL v2.1 or later, the GPL parts are optional and "None of these
parts are used by default, you have to explicitly pass ``--enable-gpl`` to
configure to activate them."

The archive digest is checked before the archive is opened, and every extracted
member's digest is checked before any file is made executable, so nothing
unverified is ever executable and nothing unverified is ever run.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

RECEIPT_SCHEMA = "openadapt.capture-ffmpeg-install/v1"
RECEIPT_NAME = "install.json"

#: Runtime revision. Bump this together with every digest below, and only from
#: a published ``openadapt-desktop`` ``ffmpeg-runtime-v*`` release whose
#: ``src-tauri/ffmpeg-runtime-manifest.json`` carries the same values.
RUNTIME_VERSION = "8.1.2-r1"
FFMPEG_VERSION = "8.1.2"
RELEASE_TAG = f"ffmpeg-runtime-v{RUNTIME_VERSION}"
RELEASE_BASE_URL = (
    "https://github.com/OpenAdaptAI/openadapt-desktop/releases/download/" f"{RELEASE_TAG}/"
)

#: SPDX expression for the pinned build, taken from FFmpeg's own ``LICENSE.md``
#: for a configuration that enables no GPL and no version-3 component.
LICENSE_EXPRESSION = "LGPL-2.1-or-later"

#: The corresponding source, which is what the LGPL asks a distributor to
#: offer. It is republished unmodified beside the binaries on the same release.
SOURCE_URL = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
SOURCE_SHA256 = "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
SOURCE_SIGNATURE_URL = f"{SOURCE_URL}.asc"
# The FFmpeg developers publish this OpenPGP fingerprint for the signature
# above. A fingerprint is a public identifier, not a credential.
SOURCE_SIGNATURE_FINGERPRINT = "FCF986EA15E6E293A5644F10B4322F04D67658D8"

_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_CHUNK_BYTES = 1 << 16


class FFmpegProvisionError(RuntimeError):
    """The pinned runtime could not be installed exactly as pinned."""


class UnsupportedPlatformError(FFmpegProvisionError):
    """No pinned build exists for this operating system and architecture."""


@dataclass(frozen=True)
class PinnedFile:
    """One archive member, pinned by digest and bounded by size."""

    member: str
    sha256: str
    max_bytes: int
    executable: bool = False


@dataclass(frozen=True)
class PinnedArtifact:
    """One platform's archive, pinned by digest and bounded by size."""

    target: str
    archive_sha256: str
    archive_max_bytes: int
    files: tuple[PinnedFile, ...]
    ffmpeg_member: str
    ffprobe_member: str
    license_member: str

    @property
    def build_id(self) -> str:
        return f"ffmpeg-{RUNTIME_VERSION}-{self.target}"

    @property
    def archive_name(self) -> str:
        return f"openadapt-{self.build_id}.zip"

    @property
    def url(self) -> str:
        return f"{RELEASE_BASE_URL}{self.archive_name}"


_LICENSE_FILES = (
    PinnedFile(
        member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
        sha256="246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30",
        max_bytes=1075093,
    ),
    PinnedFile(
        member="LICENSES/FFmpeg-LICENSE.md",
        sha256="2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af",
        max_bytes=1052922,
    ),
)

PINNED_ARTIFACTS: dict[str, PinnedArtifact] = {
    artifact.target: artifact
    for artifact in (
        PinnedArtifact(
            target="aarch64-apple-darwin",
            archive_sha256=("7cd08f97a97d3032f2093f06227fea1d12d4078dbb2c75e1109a9d3d24d7a266"),
            archive_max_bytes=7908219,
            files=(
                *_LICENSE_FILES,
                PinnedFile(
                    member="bin/ffmpeg",
                    sha256=("bc0189969e8ca336e4e49b63ef84effb5d301b1cf3209fc214a20abc0679b585"),
                    max_bytes=5889424,
                    executable=True,
                ),
                PinnedFile(
                    member="bin/ffprobe",
                    sha256=("a0dbc88c6d1b971c044121bbee55ac761b691ca9a0b9f6e39fa63613499d12d4"),
                    max_bytes=5535568,
                    executable=True,
                ),
            ),
            ffmpeg_member="bin/ffmpeg",
            ffprobe_member="bin/ffprobe",
            license_member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
        ),
        PinnedArtifact(
            target="x86_64-apple-darwin",
            archive_sha256=("a45ac3c766d94ff4bf77c87e1b2baccc29e1aca806b270d495f1698e6bf6776b"),
            archive_max_bytes=8185775,
            files=(
                *_LICENSE_FILES,
                PinnedFile(
                    member="bin/ffmpeg",
                    sha256=("dfb2197b1ef2b3da19ad41f4fbc337f2c50000390d75697a81e3516a32f1293a"),
                    max_bytes=7331584,
                    executable=True,
                ),
                PinnedFile(
                    member="bin/ffprobe",
                    sha256=("b4da8255066f2a99604ef532cc54a08c73fef9de8bdf8eecdebaddd8c0b7797c"),
                    max_bytes=6948320,
                    executable=True,
                ),
            ),
            ffmpeg_member="bin/ffmpeg",
            ffprobe_member="bin/ffprobe",
            license_member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
        ),
        PinnedArtifact(
            target="x86_64-unknown-linux-gnu",
            archive_sha256=("05b36093f1bc9476f7116056de504224f160fdec75484fe63ec539d6744c1ba8"),
            archive_max_bytes=8018223,
            files=(
                *_LICENSE_FILES,
                PinnedFile(
                    member="bin/ffmpeg",
                    sha256=("d1bb7bea5173ee9ef20b9fa7f6a290d53ae2d19fbdc90f14f491215a48b1c2b1"),
                    max_bytes=7143856,
                    executable=True,
                ),
                PinnedFile(
                    member="bin/ffprobe",
                    sha256=("6fd9ffcfa0a870c82b7cd0bf7b87cb75c8d44b82a157a415af5d5c2d3bee0d76"),
                    max_bytes=6766896,
                    executable=True,
                ),
            ),
            ffmpeg_member="bin/ffmpeg",
            ffprobe_member="bin/ffprobe",
            license_member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
        ),
        PinnedArtifact(
            target="x86_64-pc-windows-msvc",
            archive_sha256=("bd88874357d3ea6490e22e7911f31df777e4e291b3ba83f3bfef9dc9bd366080"),
            archive_max_bytes=8285917,
            files=(
                *_LICENSE_FILES,
                PinnedFile(
                    member="LICENSES/zlib.txt",
                    sha256=("e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2"),
                    max_bytes=1049578,
                ),
                PinnedFile(
                    member="bin/ffmpeg.exe",
                    sha256=("33e322b544f07118d4c1a8a56e9e84e5a5c06f86c9fcbf749359b2a1042eebd3"),
                    max_bytes=6898688,
                    executable=True,
                ),
                PinnedFile(
                    member="bin/ffprobe.exe",
                    sha256=("f65a1b8874446cce3bda7112b89c6172a8c12983eade2b040cf3672e4fa5740b"),
                    max_bytes=6568960,
                    executable=True,
                ),
            ),
            ffmpeg_member="bin/ffmpeg.exe",
            ffprobe_member="bin/ffprobe.exe",
            license_member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
        ),
    )
}


def _platform_name() -> str:
    return os.environ.get("OPENADAPT_PLATFORM_OVERRIDE") or sys.platform


def current_target(
    platform_name: str | None = None,
    machine: str | None = None,
) -> str:
    """Return the pinned build target for this machine.

    Raises:
        UnsupportedPlatformError: No pinned build covers this platform.
    """
    platform_name = platform_name or _platform_name()
    machine = (machine or os.environ.get("OPENADAPT_MACHINE_OVERRIDE") or platform.machine()).lower()
    if platform_name == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "aarch64-apple-darwin"
        if machine in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin"
    elif platform_name == "win32":
        if machine in {"amd64", "x86_64"}:
            return "x86_64-pc-windows-msvc"
    elif platform_name.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "x86_64-unknown-linux-gnu"
    raise UnsupportedPlatformError(
        f"No pinned OpenAdapt FFmpeg build exists for {platform_name}/{machine}. "
        "Install FFmpeg yourself and point Capture at it with "
        "OPENADAPT_FFMPEG_PATH, or pass Recorder(ffmpeg_path=...)."
    )


def current_artifact(target: str | None = None) -> PinnedArtifact:
    """Return the pinned artifact for this machine, or for an exact target."""
    return PINNED_ARTIFACTS[target or current_target()]


def data_root() -> Path:
    """Return Capture's own user-data root, separate from Desktop's."""
    if override := os.environ.get("OPENADAPT_CAPTURE_DATA_DIR"):
        return Path(override).expanduser()
    home = Path.home()
    platform_name = _platform_name()
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "ai.openadapt.capture"
    if platform_name == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "ai.openadapt.capture"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "ai.openadapt.capture"


def runtime_root() -> Path:
    """Return the directory that holds every installed FFmpeg runtime."""
    return data_root() / "ffmpeg"


def receipt_path() -> Path:
    """Return the path of the record of what is installed and where from."""
    return runtime_root() / RECEIPT_NAME


def _https_opener() -> urllib.request.OpenerDirector:
    """Build an opener that refuses any non-HTTPS or off-host redirect."""

    class StrictRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            parsed = urllib.parse.urlsplit(newurl)
            if parsed.scheme != "https":
                raise FFmpegProvisionError(
                    f"Refusing a non-HTTPS FFmpeg download redirect to {newurl}"
                )
            if parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise FFmpegProvisionError(
                    f"Refusing an FFmpeg download redirect to an unpinned host: {parsed.hostname}"
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(StrictRedirect)


def _download_to(url: str, destination: Path, max_bytes: int) -> str:
    """Stream ``url`` into ``destination`` under a byte cap, returning its digest."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
        raise FFmpegProvisionError(f"Refusing to download the FFmpeg runtime from {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "openadapt-capture"})
    digest = hashlib.sha256()
    written = 0
    try:
        with _https_opener().open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise FFmpegProvisionError(
                    f"The FFmpeg archive declares {declared} bytes, above the pinned "
                    f"bound of {max_bytes}"
                )
            with destination.open("wb") as sink:
                while chunk := response.read(_CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_bytes:
                        raise FFmpegProvisionError(
                            f"The FFmpeg archive exceeded its pinned bound of {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
    except urllib.error.URLError as exc:
        raise FFmpegProvisionError(f"Could not download the pinned FFmpeg runtime: {exc}") from exc
    return digest.hexdigest()


def _extract_member(
    archive: zipfile.ZipFile,
    pinned: PinnedFile,
    destination: Path,
) -> None:
    """Extract one pinned member under its byte cap, without an executable bit.

    The destination is built from the pinned constant, never from a name read
    out of the archive, so no archive member can direct a write outside the
    staging directory.
    """
    try:
        info = archive.getinfo(pinned.member)
    except KeyError as exc:
        raise FFmpegProvisionError(
            f"The FFmpeg archive has no pinned member {pinned.member!r}"
        ) from exc
    if info.is_dir() or info.file_size > pinned.max_bytes:
        raise FFmpegProvisionError(
            f"The FFmpeg archive member {pinned.member!r} is not a bounded regular file"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with archive.open(info, "r") as source:
        # 0o600 while unverified: the executable bit is set only after every
        # pinned digest below has matched.
        handle = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "wb") as sink:
            while chunk := source.read(_CHUNK_BYTES):
                written += len(chunk)
                if written > pinned.max_bytes:
                    raise FFmpegProvisionError(
                        f"The FFmpeg archive member {pinned.member!r} exceeded its "
                        f"pinned bound of {pinned.max_bytes} bytes"
                    )
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
    actual = digest.hexdigest()
    if actual != pinned.sha256:
        raise FFmpegProvisionError(
            f"The FFmpeg archive member {pinned.member!r} has digest {actual}, "
            f"not the pinned {pinned.sha256}"
        )


def _reject_unpinned_members(archive: zipfile.ZipFile, artifact: PinnedArtifact) -> None:
    """Refuse an archive that carries an executable member Capture did not pin.

    Everything the archive puts in ``bin/`` has to be one of the pinned,
    digest-checked members. The archive also carries plain-text provenance and
    license files, which are inert and are simply not installed.
    """
    pinned = {file.member for file in artifact.files}
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        if name.startswith("bin/") and name not in pinned:
            raise FFmpegProvisionError(
                f"The FFmpeg archive carries an unpinned executable member: {name}"
            )


@dataclass(frozen=True)
class InstalledRuntime:
    """One verified runtime on disk, as recorded by the install receipt."""

    ffmpeg: str
    ffprobe: str
    build_id: str
    runtime_version: str
    target: str
    url: str
    archive_sha256: str
    license_expression: str
    license_path: str
    source_url: str
    source_sha256: str
    installed_at: str

    def as_receipt(self) -> dict:
        return {
            "schema": RECEIPT_SCHEMA,
            "build_id": self.build_id,
            "runtime_version": self.runtime_version,
            "ffmpeg_version": FFMPEG_VERSION,
            "target": self.target,
            "url": self.url,
            "archive_sha256": self.archive_sha256,
            "license": {
                "expression": self.license_expression,
                "path": self.license_path,
                "source_url": self.source_url,
                "source_sha256": self.source_sha256,
                "source_signature_url": SOURCE_SIGNATURE_URL,
                "source_signature_fingerprint": SOURCE_SIGNATURE_FINGERPRINT,
            },
            "executables": {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe},
            "installed_at": self.installed_at,
        }


def read_receipt() -> dict | None:
    """Return the install receipt, or ``None`` when nothing is installed."""
    path = receipt_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != RECEIPT_SCHEMA:
        return None
    return payload


def find_installed_runtime() -> tuple[str, str | None] | None:
    """Return ``(ffmpeg, ffprobe)`` for a recorded install, or ``None``.

    This reads only Capture's own receipt and never downloads. A receipt whose
    executables are gone is treated as absent, so a half-deleted install falls
    through to the normal "no FFmpeg" error instead of a confusing one.
    """
    payload = read_receipt()
    if payload is None:
        return None
    executables = payload.get("executables")
    if not isinstance(executables, dict):
        return None
    ffmpeg = executables.get("ffmpeg")
    ffprobe = executables.get("ffprobe")
    if not isinstance(ffmpeg, str) or not Path(ffmpeg).is_file():
        return None
    root = runtime_root().resolve()
    try:
        Path(ffmpeg).resolve().relative_to(root)
    except ValueError:
        return None
    if isinstance(ffprobe, str) and Path(ffprobe).is_file():
        try:
            Path(ffprobe).resolve().relative_to(root)
        except ValueError:
            ffprobe = None
    else:
        ffprobe = None
    return ffmpeg, ffprobe


def plan(target: str | None = None) -> dict:
    """Describe exactly what an install would fetch, without fetching it."""
    artifact = current_artifact(target)
    return {
        "build_id": artifact.build_id,
        "runtime_version": RUNTIME_VERSION,
        "ffmpeg_version": FFMPEG_VERSION,
        "target": artifact.target,
        "url": artifact.url,
        "archive_sha256": artifact.archive_sha256,
        "license": LICENSE_EXPRESSION,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "install_dir": str(runtime_root() / artifact.build_id),
        "receipt": str(receipt_path()),
    }


def _staged_files(artifact: PinnedArtifact, staging: Path) -> Iterator[tuple[PinnedFile, Path]]:
    for pinned in artifact.files:
        yield pinned, staging / Path(*pinned.member.split("/"))


def install(
    *,
    target: str | None = None,
    force: bool = False,
) -> InstalledRuntime:
    """Download, verify and install the pinned FFmpeg runtime for this machine.

    The archive digest is checked before the archive is opened. Every pinned
    member's digest is checked as it is written, at mode ``0600``. Only when
    every digest has matched does anything become executable, and only then is
    the staged directory promoted and the receipt written.

    Raises:
        FFmpegProvisionError: The pinned artifact could not be obtained and
            verified exactly as pinned. Nothing is installed in that case.
    """
    artifact = current_artifact(target)
    root = runtime_root()
    final = root / artifact.build_id
    if final.is_dir() and not force:
        existing = find_installed_runtime()
        if existing is not None:
            ffmpeg, ffprobe = existing
            return InstalledRuntime(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe or "",
                build_id=artifact.build_id,
                runtime_version=RUNTIME_VERSION,
                target=artifact.target,
                url=artifact.url,
                archive_sha256=artifact.archive_sha256,
                license_expression=LICENSE_EXPRESSION,
                license_path=str(final / Path(*artifact.license_member.split("/"))),
                source_url=SOURCE_URL,
                source_sha256=SOURCE_SHA256,
                installed_at=(read_receipt() or {}).get("installed_at", ""),
            )
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".openadapt-ffmpeg-", dir=root) as scratch:
        scratch_root = Path(scratch)
        archive_path = scratch_root / artifact.archive_name
        actual = _download_to(artifact.url, archive_path, artifact.archive_max_bytes)
        if actual != artifact.archive_sha256:
            # Nothing has been opened, extracted, marked executable, or run.
            raise FFmpegProvisionError(
                f"The FFmpeg archive from {artifact.url} has digest {actual}, "
                f"not the pinned {artifact.archive_sha256}. Nothing was installed."
            )

        staging = scratch_root / "staging"
        staging.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            _reject_unpinned_members(archive, artifact)
            for pinned, destination in _staged_files(artifact, staging):
                _extract_member(archive, pinned, destination)

        # Every pinned digest matched above. Only now does anything become
        # executable.
        for pinned, destination in _staged_files(artifact, staging):
            if pinned.executable:
                mode = destination.stat().st_mode
                destination.chmod(mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)

        if final.exists():
            replaced = scratch_root / "replaced"
            final.replace(replaced)
        staging.replace(final)

    ffmpeg = final / Path(*artifact.ffmpeg_member.split("/"))
    ffprobe = final / Path(*artifact.ffprobe_member.split("/"))
    installed = InstalledRuntime(
        ffmpeg=str(ffmpeg),
        ffprobe=str(ffprobe),
        build_id=artifact.build_id,
        runtime_version=RUNTIME_VERSION,
        target=artifact.target,
        url=artifact.url,
        archive_sha256=artifact.archive_sha256,
        license_expression=LICENSE_EXPRESSION,
        license_path=str(final / Path(*artifact.license_member.split("/"))),
        source_url=SOURCE_URL,
        source_sha256=SOURCE_SHA256,
        installed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    receipt = receipt_path()
    temporary = receipt.with_name(f".{receipt.name}.partial")
    temporary.write_text(
        json.dumps(installed.as_receipt(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(receipt)
    return installed


def uninstall() -> bool:
    """Remove every installed runtime and its receipt. Returns whether any went."""
    root = runtime_root()
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True
