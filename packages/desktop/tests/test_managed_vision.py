from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from engine import managed_vision as vision


def _wheel(tmp_path: Path, name: str, members: dict[str, bytes]) -> tuple[vision.Wheel, Path]:
    archive = tmp_path / f"{name}.whl"
    record_member = f"{name.replace('-', '_')}-1.0.0.dist-info/RECORD"
    record_lines = []
    for member, payload in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        record_lines.append(f"{member},sha256={digest},{len(payload)}")
    record_lines.append(f"{record_member},,")
    members = {**members, record_member: ("\n".join(record_lines) + "\n").encode()}
    with zipfile.ZipFile(archive, "w") as package:
        for member, payload in members.items():
            package.writestr(member, payload)
    payload = archive.read_bytes()
    record_payload = members[record_member]
    return (
        vision.Wheel(
            distribution=name,
            version="1.0.0",
            url=f"https://files.pythonhosted.org/packages/test/{archive.name}",
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
            record_member=record_member,
            record_sha256=hashlib.sha256(record_payload).hexdigest(),
            license_expression="MIT",
        ),
        archive,
    )


def test_embedded_manifest_covers_exact_supported_targets() -> None:
    payload = json.loads(vision.MANIFEST_PATH.read_text())

    assert payload["schema_version"] == 1
    assert (
        payload["runtime_version"]
        == "rapidocr-1.4.4-opencv-5.0.0.93-numpy-2.4.2-r3"
    )
    assert {artifact["target"] for artifact in payload["artifacts"]} == (vision.SUPPORTED_TARGETS)
    for target in vision.SUPPORTED_TARGETS:
        contract = vision.load_contract(target=target)
        assert [wheel.distribution for wheel in contract.wheels] == [
            "rapidocr-onnxruntime",
            "numpy",
            "opencv-python",
        ]
        assert all(
            wheel.url.startswith("https://files.pythonhosted.org/") for wheel in contract.wheels
        )


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
    ],
)
def test_current_target_is_explicit(
    system: str,
    machine: str,
    expected: str,
) -> None:
    assert vision.current_target(system=system, machine=machine) == expected


def test_extract_wheels_hashes_members(tmp_path: Path) -> None:
    opencv = _wheel(
        tmp_path,
        "opencv-python",
        {
            "cv2/__init__.py": b"__version__ = '1.0.0'\n",
            "opencv_python-1.0.0.dist-info/LICENSE.txt": b"terms\n",
        },
    )
    rapidocr = _wheel(
        tmp_path,
        "rapidocr-onnxruntime",
        {
            "rapidocr_onnxruntime/__init__.py": b"",
            "rapidocr_onnxruntime-1.0.0.dist-info/LICENSE": b"terms\n",
        },
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    files = vision._extract_wheels((opencv, rapidocr), staging)  # noqa: SLF001

    assert {record["member"] for record in files} == {
        "cv2/__init__.py",
        "opencv_python-1.0.0.dist-info/LICENSE.txt",
        "opencv_python-1.0.0.dist-info/RECORD",
        "rapidocr_onnxruntime/__init__.py",
        "rapidocr_onnxruntime-1.0.0.dist-info/LICENSE",
        "rapidocr_onnxruntime-1.0.0.dist-info/RECORD",
    }
    for record in files:
        path = staging / str(record["member"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_install_runtime_notices_copies_exact_bytes_and_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_root = tmp_path / "notices"
    notice_root.mkdir()
    (notice_root / "LICENSE").write_bytes(b"Apache-2.0 terms\n")
    (notice_root / "NOTICE").write_bytes(b"model attribution\n")
    staging = tmp_path / "runtime"
    staging.mkdir()
    monkeypatch.setattr(
        vision,
        "RAPIDOCR_NOTICE_CONTRACT",
        {
            "rapidocr_onnxruntime-1.4.4.dist-info/licenses/LICENSE": (
                hashlib.sha256(b"Apache-2.0 terms\n").hexdigest(),
                len(b"Apache-2.0 terms\n"),
            ),
            "rapidocr_onnxruntime-1.4.4.dist-info/licenses/NOTICE": (
                hashlib.sha256(b"model attribution\n").hexdigest(),
                len(b"model attribution\n"),
            ),
        },
    )

    records = vision._install_runtime_notices(  # noqa: SLF001
        staging,
        notice_root=notice_root,
    )

    assert {record["member"] for record in records} == {
        "rapidocr_onnxruntime-1.4.4.dist-info/licenses/LICENSE",
        "rapidocr_onnxruntime-1.4.4.dist-info/licenses/NOTICE",
    }
    for record in records:
        path = staging / str(record["member"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_extract_wheels_rejects_traversal(tmp_path: Path) -> None:
    malicious = _wheel(
        tmp_path,
        "opencv-python",
        {"../outside": b"nope"},
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(vision.ManagedVisionRuntimeError, match="unsafe"):
        vision._extract_wheels((malicious,), staging)  # noqa: SLF001


def test_link_like_rejects_windows_junctions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(Path, "is_junction", lambda _path: True, raising=False)

    assert vision._is_link_like(tmp_path) is True  # noqa: SLF001


def test_cache_admission_rejects_link_like_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = vision.load_contract(target="aarch64-apple-darwin")
    monkeypatch.setattr(vision, "_is_link_like", lambda path: path == tmp_path)

    assert vision._cache_is_valid(tmp_path, contract) is False  # noqa: SLF001


def test_runtime_lock_serializes_concurrent_first_use(tmp_path: Path) -> None:
    lock_path = tmp_path / "vision.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with vision._runtime_lock(  # noqa: SLF001
            lock_path,
            status=lambda _message: None,
            timeout=2,
        ):
            first_entered.set()
            assert release_first.wait(2)

    def second() -> None:
        assert first_entered.wait(2)
        with vision._runtime_lock(  # noqa: SLF001
            lock_path,
            status=lambda _message: None,
            timeout=2,
        ):
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(2)
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(2)
    second_thread.join(2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()


def test_download_rejects_hash_drift(tmp_path: Path) -> None:
    payload = b"wrong bytes"
    wheel = vision.Wheel(
        distribution="opencv-python",
        version="1.0.0",
        url="https://files.pythonhosted.org/packages/test/opencv.whl",
        sha256="0" * 64,
        bytes=len(payload),
        record_member="opencv_python-1.0.0.dist-info/RECORD",
        record_sha256="0" * 64,
        license_expression="MIT",
    )

    class Response(io.BytesIO):
        def geturl(self) -> str:
            return wheel.url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    with pytest.raises(vision.ManagedVisionRuntimeError, match="hash"):
        vision._download(  # noqa: SLF001
            wheel,
            tmp_path / "download.whl",
            opener=lambda *_args, **_kwargs: Response(payload),
        )


def test_ensure_reuses_only_hash_valid_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    wheels = (
        _wheel(
            source_dir,
            "numpy",
            {"numpy/__init__.py": b"__version__ = '1.0.0'\n"},
        ),
        _wheel(
            source_dir,
            "rapidocr-onnxruntime",
            {"rapidocr_onnxruntime/__init__.py": b""},
        ),
        _wheel(
            source_dir,
            "opencv-python",
            {"cv2/__init__.py": b"__version__ = '1.0.0'\n"},
        ),
    )
    contract = vision.RuntimeContract(
        runtime_version="test-r1",
        target="aarch64-apple-darwin",
        wheels=tuple(wheel for wheel, _ in wheels),
        manifest_sha256="a" * 64,
    )
    monkeypatch.setattr(vision, "load_contract", lambda: contract)
    monkeypatch.setenv("OPENADAPT_VISION_RUNTIME_ROOT", str(tmp_path / "runtime"))
    activations: list[Path] = []
    monkeypatch.setattr(vision, "_activate", lambda path, _contract: activations.append(path))
    downloads = 0

    def fake_download(wheel: vision.Wheel, destination: Path) -> None:
        nonlocal downloads
        downloads += 1
        source = next(path for candidate, path in wheels if candidate == wheel)
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(vision, "_download", fake_download)

    first = vision.ensure_managed_vision_runtime(status=lambda _message: None)
    second = vision.ensure_managed_vision_runtime(status=lambda _message: None)

    assert first == second
    assert downloads == 3
    assert activations == [first, first]
    (first / "cv2" / "__init__.py").write_text("tampered\n")
    marker_path = first / vision.MARKER_NAME
    marker = json.loads(marker_path.read_text())
    cv2_record = next(
        record for record in marker["files"] if record["member"] == "cv2/__init__.py"
    )
    cv2_record["sha256"] = hashlib.sha256(b"tampered\n").hexdigest()
    cv2_record["bytes"] = len(b"tampered\n")
    marker_path.write_text(json.dumps(marker))

    repaired = vision.ensure_managed_vision_runtime(status=lambda _message: None)

    assert repaired == first
    assert downloads == 6
    assert (repaired / "cv2" / "__init__.py").read_text() == ("__version__ = '1.0.0'\n")
    assert list(first.parent.glob(f"{first.name}.invalid-*"))

    (repaired / "cv2.py").write_text("unexpected cache shadow\n")
    marker = json.loads(marker_path.read_text())
    marker["files"].append(
        {
            "member": "cv2.py",
            "sha256": hashlib.sha256(b"unexpected cache shadow\n").hexdigest(),
            "bytes": len(b"unexpected cache shadow\n"),
        }
    )
    marker_path.write_text(json.dumps(marker))

    repaired_again = vision.ensure_managed_vision_runtime(status=lambda _message: None)

    assert repaired_again == first
    assert downloads == 9
    assert not (repaired_again / "cv2.py").exists()
