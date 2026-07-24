"""Exact-hash first-use provisioning for Desktop's local vision runtime.

OpenCV's upstream wheels include separately licensed codec libraries. They are
therefore not copied into OpenAdapt's MIT wheel, frozen sidecar, or installer.
Desktop downloads the exact reviewed NumPy, OpenCV, and RapidOCR wheels only
when a Flow or native-recording command first needs the vision stack, verifies
their published hashes,
extracts them into a versioned user-data cache, and loads them as an optional
runtime component. Re-running the command retries a failed download; a
partially prepared directory is never treated as ready.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

MANIFEST_PATH = Path(__file__).with_name("vision-runtime-manifest.json")
MARKER_NAME = ".complete.json"
MAX_EXTRACTED_BYTES = 700 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
LOCK_TIMEOUT_SECONDS = 300.0
LOCK_RETRY_SECONDS = 0.1
ALLOWED_DOWNLOAD_HOST = "files.pythonhosted.org"
RAPIDOCR_NOTICE_MEMBERS = (
    ("LICENSE", "LICENSE"),
    ("NOTICE", "NOTICE"),
)
RAPIDOCR_NOTICE_CONTRACT = {
    "rapidocr_onnxruntime-1.4.4.dist-info/licenses/LICENSE": (
        "3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333",
        11422,
    ),
    "rapidocr_onnxruntime-1.4.4.dist-info/licenses/NOTICE": (
        "c14087f8546efee022e49c5a1629f74f7fe5cf219c54ffa690f4dcc83dd10dfb",
        554,
    ),
}
SUPPORTED_TARGETS = frozenset(
    {
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
    }
)


class ManagedVisionRuntimeError(RuntimeError):
    """The separately provisioned vision runtime could not be made ready."""


@dataclass(frozen=True)
class Wheel:
    distribution: str
    version: str
    url: str
    sha256: str
    bytes: int
    record_member: str
    record_sha256: str
    license_expression: str


@dataclass(frozen=True)
class RuntimeContract:
    runtime_version: str
    target: str
    wheels: tuple[Wheel, ...]
    manifest_sha256: str

    @property
    def build_id(self) -> str:
        return f"{self.runtime_version}-{self.manifest_sha256[:12]}"


def current_target(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Return the release-manifest target for this host."""

    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if machine in {"amd64", "x64"}:
        machine = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        machine = "aarch64"
    target = {
        ("darwin", "aarch64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    }.get((system, machine))
    if target is None:
        raise ManagedVisionRuntimeError(
            f"no reviewed local vision runtime is published for {system}/{machine}"
        )
    return target


def _wheel(record: object) -> Wheel:
    if not isinstance(record, dict):
        raise ManagedVisionRuntimeError("vision runtime wheel record is not an object")
    try:
        wheel = Wheel(
            distribution=str(record["distribution"]),
            version=str(record["version"]),
            url=str(record["url"]),
            sha256=str(record["sha256"]),
            bytes=int(record["bytes"]),
            record_member=str(record["record_member"]),
            record_sha256=str(record["record_sha256"]),
            license_expression=str(record["license_expression"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedVisionRuntimeError("vision runtime wheel record is malformed") from exc
    parsed = urllib.parse.urlparse(wheel.url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".whl")
    ):
        raise ManagedVisionRuntimeError(
            f"vision runtime wheel URL is not an exact trusted PyPI file: {wheel.url}"
        )
    if (
        not wheel.distribution
        or not wheel.version
        or not wheel.license_expression
        or len(wheel.sha256) != 64
        or any(character not in "0123456789abcdef" for character in wheel.sha256)
        or len(wheel.record_sha256) != 64
        or any(character not in "0123456789abcdef" for character in wheel.record_sha256)
        or wheel.bytes <= 0
        or wheel.bytes > 150 * 1024 * 1024
    ):
        raise ManagedVisionRuntimeError(
            f"vision runtime wheel metadata is invalid for {wheel.distribution or 'unknown'}"
        )
    _safe_member(wheel.record_member)
    return wheel


def load_contract(
    *,
    manifest_path: Path = MANIFEST_PATH,
    target: str | None = None,
) -> RuntimeContract:
    """Load and strictly validate the embedded release-reviewed manifest."""

    try:
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedVisionRuntimeError("embedded vision runtime manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ManagedVisionRuntimeError("unsupported vision runtime manifest schema")
    if manifest.get("runtime") != "openadapt-managed-vision":
        raise ManagedVisionRuntimeError("unexpected vision runtime identity")
    runtime_version = manifest.get("runtime_version")
    shared = manifest.get("shared_wheels")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(runtime_version, str)
        or not runtime_version
        or not isinstance(shared, list)
        or not shared
        or not isinstance(artifacts, list)
    ):
        raise ManagedVisionRuntimeError("vision runtime manifest is incomplete")

    target_names = [artifact.get("target") for artifact in artifacts if isinstance(artifact, dict)]
    if set(target_names) != SUPPORTED_TARGETS or len(target_names) != len(SUPPORTED_TARGETS):
        raise ManagedVisionRuntimeError(
            "vision runtime manifest must cover each supported target exactly once"
        )
    selected_target = target or current_target()
    selected = next(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and artifact.get("target") == selected_target
        ),
        None,
    )
    if selected is None or not isinstance(selected.get("wheels"), list):
        raise ManagedVisionRuntimeError(
            f"vision runtime manifest has no artifact for {selected_target}"
        )
    wheels = tuple(_wheel(record) for record in [*shared, *selected["wheels"]])
    names = [wheel.distribution for wheel in wheels]
    if sorted(names) != ["numpy", "opencv-python", "rapidocr-onnxruntime"]:
        raise ManagedVisionRuntimeError(
            f"vision runtime must contain exact NumPy, OpenCV, and RapidOCR wheels, found {names}"
        )
    return RuntimeContract(
        runtime_version=runtime_version,
        target=selected_target,
        wheels=wheels,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
    )


def runtime_root() -> Path:
    """Return the versioned-cache parent, with an absolute offline override."""

    override = os.environ.get("OPENADAPT_VISION_RUNTIME_ROOT", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ManagedVisionRuntimeError(
                "OPENADAPT_VISION_RUNTIME_ROOT must be an absolute directory"
            )
        return path
    return Path.home() / ".openadapt" / "vision-runtime"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _download(
    wheel: Wheel,
    destination: Path,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> None:
    request = urllib.request.Request(
        wheel.url,
        headers={"User-Agent": "OpenAdapt-Desktop-managed-vision/1"},
    )
    try:
        response = opener(request, timeout=60)
        with response, destination.open("xb") as output:
            final_url = getattr(response, "geturl", lambda: wheel.url)()
            parsed = urllib.parse.urlparse(final_url)
            if parsed.scheme != "https" or parsed.hostname != ALLOWED_DOWNLOAD_HOST:
                raise ManagedVisionRuntimeError(
                    f"vision runtime download redirected outside trusted PyPI: {final_url}"
                )
            digest = hashlib.sha256()
            size = 0
            while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > wheel.bytes:
                    raise ManagedVisionRuntimeError(
                        f"{wheel.distribution} download exceeded its reviewed size"
                    )
                digest.update(chunk)
                output.write(chunk)
    except ManagedVisionRuntimeError:
        raise
    except (OSError, ValueError) as exc:
        raise ManagedVisionRuntimeError(f"could not download {wheel.distribution}: {exc}") from exc
    if size != wheel.bytes or digest.hexdigest() != wheel.sha256:
        raise ManagedVisionRuntimeError(
            f"{wheel.distribution} wheel failed exact size/hash verification"
        )


def _safe_member(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ManagedVisionRuntimeError(f"unsafe vision runtime wheel member: {name!r}")
    member = PurePosixPath(name)
    if member.is_absolute() or any(part in {"", ".", ".."} for part in member.parts):
        raise ManagedVisionRuntimeError(f"unsafe vision runtime wheel member: {name!r}")
    return member


def _extract_wheels(
    archives: tuple[tuple[Wheel, Path], ...],
    staging: Path,
) -> list[dict[str, object]]:
    """Extract exact wheels without links, traversal, duplicates, or overrun."""

    files: list[dict[str, object]] = []
    seen: set[str] = set()
    extracted_bytes = 0
    for wheel, archive in archives:
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                if info.is_dir():
                    continue
                member = _safe_member(info.filename)
                key = member.as_posix().casefold()
                if key in seen:
                    raise ManagedVisionRuntimeError(
                        f"duplicate vision runtime wheel member: {member}"
                    )
                seen.add(key)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ManagedVisionRuntimeError(
                        f"vision runtime wheel contains a symlink: {member}"
                    )
                extracted_bytes += info.file_size
                if extracted_bytes > MAX_EXTRACTED_BYTES:
                    raise ManagedVisionRuntimeError(
                        "vision runtime wheels exceed the extracted-size limit"
                    )
                destination = staging.joinpath(*member.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with package.open(info) as source, destination.open("xb") as output:
                    while chunk := source.read(DOWNLOAD_CHUNK_BYTES):
                        size += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                if size != info.file_size:
                    raise ManagedVisionRuntimeError(
                        f"short extraction for vision runtime member: {member}"
                    )
                files.append(
                    {
                        "member": member.as_posix(),
                        "sha256": digest.hexdigest(),
                        "bytes": size,
                    }
                )
    return sorted(files, key=lambda record: str(record["member"]))


def _install_runtime_notices(
    staging: Path,
    *,
    notice_root: Path | None = None,
) -> list[dict[str, object]]:
    """Install the reviewed RapidOCR license beside its provisioned runtime."""

    if notice_root is None:
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        notice_root = resource_root / "third_party" / "rapidocr"
    destination_root = staging / "rapidocr_onnxruntime-1.4.4.dist-info" / "licenses"
    records: list[dict[str, object]] = []
    for source_name, destination_name in RAPIDOCR_NOTICE_MEMBERS:
        source = notice_root / source_name
        if not source.is_file():
            raise ManagedVisionRuntimeError(
                f"reviewed RapidOCR {source_name} is missing from Desktop"
            )
        destination = destination_root / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digest, size = _hash_file(destination)
        expected = RAPIDOCR_NOTICE_CONTRACT[destination.relative_to(staging).as_posix()]
        if (digest, size) != expected:
            raise ManagedVisionRuntimeError(
                f"reviewed RapidOCR {source_name} bytes require a new release review"
            )
        records.append(
            {
                "member": destination.relative_to(staging).as_posix(),
                "sha256": digest,
                "bytes": size,
            }
        )
    return records


def _marker(contract: RuntimeContract, files: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_version": contract.runtime_version,
        "target": contract.target,
        "manifest_sha256": contract.manifest_sha256,
        "wheels": [
            {
                "distribution": wheel.distribution,
                "version": wheel.version,
                "url": wheel.url,
                "sha256": wheel.sha256,
                "bytes": wheel.bytes,
                "record_member": wheel.record_member,
                "record_sha256": wheel.record_sha256,
                "license_expression": wheel.license_expression,
            }
            for wheel in contract.wheels
        ],
        "files": files,
    }


def _record_inventory(path: Path, contract: RuntimeContract) -> dict[str, tuple[str, int]]:
    """Derive the admissible cache from signed-manifest-bound wheel RECORDs."""

    expected: dict[str, tuple[str, int]] = {}
    for wheel in contract.wheels:
        record_member = _safe_member(wheel.record_member)
        record_path = path.joinpath(*record_member.parts)
        if _is_link_like(record_path) or not record_path.is_file():
            raise ManagedVisionRuntimeError(
                f"managed runtime omitted trusted RECORD for {wheel.distribution}"
            )
        digest, record_size = _hash_file(record_path)
        if digest != wheel.record_sha256:
            raise ManagedVisionRuntimeError(
                f"managed runtime RECORD drifted for {wheel.distribution}"
            )
        try:
            rows = csv.reader(io.StringIO(record_path.read_text(encoding="utf-8")))
            saw_record = False
            for row in rows:
                if len(row) != 3:
                    raise ManagedVisionRuntimeError(
                        f"malformed trusted RECORD for {wheel.distribution}"
                    )
                member = _safe_member(row[0])
                member_name = member.as_posix()
                if member_name in expected:
                    raise ManagedVisionRuntimeError(
                        f"duplicate managed runtime member in trusted RECORDs: {member_name}"
                    )
                hash_field, size_field = row[1:]
                if member_name == wheel.record_member:
                    if hash_field or size_field:
                        raise ManagedVisionRuntimeError(
                            f"trusted RECORD self-entry drifted for {wheel.distribution}"
                        )
                    expected[member_name] = (wheel.record_sha256, record_size)
                    saw_record = True
                    continue
                if not hash_field.startswith("sha256=") or not size_field.isdigit():
                    raise ManagedVisionRuntimeError(
                        f"trusted RECORD omitted a file digest for {wheel.distribution}"
                    )
                encoded = hash_field.removeprefix("sha256=")
                decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                if len(decoded) != hashlib.sha256().digest_size:
                    raise ManagedVisionRuntimeError(
                        f"trusted RECORD has an invalid digest for {wheel.distribution}"
                    )
                expected[member_name] = (decoded.hex(), int(size_field))
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            raise ManagedVisionRuntimeError(
                f"could not parse trusted RECORD for {wheel.distribution}"
            ) from exc
        if not saw_record:
            raise ManagedVisionRuntimeError(
                f"trusted RECORD omitted itself for {wheel.distribution}"
            )
    for member, record in RAPIDOCR_NOTICE_CONTRACT.items():
        if member in expected:
            raise ManagedVisionRuntimeError(
                f"reviewed supplemental notice collides with wheel inventory: {member}"
            )
        expected[member] = record
    return expected


def _is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse directory links."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _actual_cache_files(path: Path) -> set[str]:
    actual: set[str] = set()
    for candidate in path.rglob("*"):
        if _is_link_like(candidate):
            raise ManagedVisionRuntimeError(
                f"managed runtime cache contains a symlink: {candidate}"
            )
        if candidate.is_file():
            actual.add(candidate.relative_to(path).as_posix())
    return actual


def _cache_is_valid(path: Path, contract: RuntimeContract) -> bool:
    if _is_link_like(path):
        return False
    try:
        marker = json.loads((path / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = _marker(contract, marker.get("files", []) if isinstance(marker, dict) else [])
    if not isinstance(marker, dict):
        return False
    for key in (
        "schema_version",
        "runtime_version",
        "target",
        "manifest_sha256",
        "wheels",
    ):
        if marker.get(key) != expected[key]:
            return False
    if not isinstance(marker.get("files"), list):
        return False
    try:
        expected_files = _record_inventory(path, contract)
        actual_files = _actual_cache_files(path)
    except (ManagedVisionRuntimeError, OSError):
        return False
    if actual_files != {*expected_files, MARKER_NAME}:
        return False
    for member_name, (expected_digest, expected_size) in expected_files.items():
        member = _safe_member(member_name)
        candidate = path.joinpath(*member.parts)
        digest, size = _hash_file(candidate)
        if size != expected_size or digest != expected_digest:
            return False
    return True


def _try_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _runtime_lock(
    lock_path: Path,
    *,
    status: Callable[[str], None],
    timeout: float = LOCK_TIMEOUT_SECONDS,
):
    """Serialize admission through activation; OS locks release on process death."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            try:
                _try_lock(stream)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ManagedVisionRuntimeError(
                        "timed out waiting for another OpenAdapt process to finish "
                        "preparing the local vision runtime"
                    ) from exc
                if not announced:
                    status(
                        "OpenAdapt: waiting for another process to finish preparing "
                        "the local vision runtime..."
                    )
                    announced = True
                time.sleep(LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            _unlock(stream)


def _activate(path: Path, contract: RuntimeContract) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    importlib.invalidate_caches()

    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            import cv2
            import numpy
            import rapidocr_onnxruntime
        except ImportError as exc:
            raise ManagedVisionRuntimeError(
                "managed vision runtime imports failed after verified extraction"
            ) from exc
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    expected = {wheel.distribution: wheel.version for wheel in contract.wheels}
    # Keep names data-driven from the signed manifest. Besides avoiding a
    # second source of truth, this prevents PyInstaller's metadata scanner from
    # copying these externally provisioned distributions into the sidecar.
    versions = {name: distribution(name).version for name in expected}
    expected_cv2 = ".".join(expected["opencv-python"].split(".")[:3])
    if (
        versions != expected
        or numpy.__version__ != expected["numpy"]
        or cv2.__version__ != expected_cv2
    ):
        raise ManagedVisionRuntimeError(
            f"managed vision runtime version drift: expected {expected}, found {versions}"
        )
    runtime_path = path.resolve()
    for module in (numpy, cv2, rapidocr_onnxruntime):
        module_path = Path(module.__file__ or "").resolve()
        if runtime_path not in module_path.parents:
            raise ManagedVisionRuntimeError(
                f"{module.__name__} loaded outside the verified managed runtime"
            )


def ensure_managed_vision_runtime(
    *,
    status: Callable[[str], None] | None = None,
) -> Path:
    """Ensure, verify, activate, and return the optional runtime directory."""

    status = status or (lambda message: print(message, file=sys.stderr, flush=True))
    contract = load_contract()
    root = runtime_root()
    final = root / contract.build_id / contract.target
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{contract.build_id}-{contract.target}.lock"
    with _runtime_lock(lock_path, status=status):
        if _cache_is_valid(final, contract):
            _activate(final, contract)
            return final

        status(
            "OpenAdapt: preparing the separately licensed local vision runtime "
            "(one-time download)."
        )
        staging = Path(tempfile.mkdtemp(prefix=".vision-", dir=root))
        archives: list[tuple[Wheel, Path]] = []
        try:
            for wheel in contract.wheels:
                status(f"OpenAdapt: downloading {wheel.distribution} {wheel.version}...")
                archive = staging / f"{wheel.distribution}-{wheel.version}.whl"
                _download(wheel, archive)
                archives.append((wheel, archive))
            payload = staging / "payload"
            payload.mkdir()
            status("OpenAdapt: verifying and installing the local vision runtime...")
            files = _extract_wheels(tuple(archives), payload)
            files.extend(_install_runtime_notices(payload))
            files.sort(key=lambda record: str(record["member"]))
            (payload / MARKER_NAME).write_text(
                json.dumps(_marker(contract, files), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                quarantine = final.with_name(
                    f"{final.name}.invalid-{os.getpid()}-{secrets.token_hex(4)}"
                )
                os.replace(final, quarantine)
            os.replace(payload, final)
        except Exception as exc:
            if isinstance(exc, ManagedVisionRuntimeError):
                detail = str(exc)
            else:
                detail = f"unexpected provisioning failure: {exc}"
            raise ManagedVisionRuntimeError(
                f"{detail}. Run the command again to retry; partial downloads were not activated."
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if not _cache_is_valid(final, contract):
            raise ManagedVisionRuntimeError(
                "managed vision runtime failed its post-install integrity check"
            )
        _activate(final, contract)
        status("OpenAdapt: local vision runtime is ready.")
        return final
