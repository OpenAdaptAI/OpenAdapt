"""One raw-descriptor byte contract for independently released packages.

Keep the CRT model and physical-byte assertions shared by package tests. Runtime
packages do not import this module or depend on each other's helper versions.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def crt_files(monkeypatch: pytest.MonkeyPatch):
    """Expose CRT text translation on POSIX; use the real CRT on Windows.

    A text writer can appear to round-trip when a text reader undoes CRLF
    expansion. Tests therefore also read the physical file through read_bytes.
    """
    if os.name == "nt":
        yield
        return
    binary = 1 << 29
    original_open, original_read = os.open, os.read
    original_write, original_close = os.write, os.close
    descriptors: dict[int, bool] = {}
    eof: set[int] = set()

    def crt_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags & ~binary, *args, **kwargs)
        descriptors[descriptor] = bool(flags & binary)
        return descriptor

    def crt_write(descriptor, data):
        if descriptors.get(descriptor, True):
            return original_write(descriptor, data)
        raw = bytes(data)
        original_write(descriptor, raw.replace(b"\n", b"\r\n"))
        return len(raw)

    def crt_read(descriptor, count):
        if descriptor in eof:
            return b""
        raw = original_read(descriptor, count)
        if descriptors.get(descriptor, True):
            return raw
        if b"\x1a" in raw:
            raw = raw.split(b"\x1a", 1)[0]
            eof.add(descriptor)
        return raw.replace(b"\r\n", b"\n")

    def crt_close(descriptor):
        descriptors.pop(descriptor, None)
        eof.discard(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
    monkeypatch.setattr(os, "open", crt_open)
    monkeypatch.setattr(os, "read", crt_read)
    monkeypatch.setattr(os, "write", crt_write)
    monkeypatch.setattr(os, "close", crt_close)
    yield


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = os.write(descriptor, view)
        assert count > 0
        view = view[count:]


def _read_all(descriptor: int) -> bytes:
    chunks = []
    while chunk := os.read(descriptor, 17):
        chunks.append(chunk)
    return b"".join(chunks)


def assert_descriptor_roundtrip(
    tmp_path: Path, opener: Callable, payload: bytes
) -> None:
    path = tmp_path / "arbitrary.bin"
    path.unlink(missing_ok=True)
    descriptor = opener(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
    finally:
        os.close(descriptor)
    assert path.read_bytes() == payload
    descriptor = opener(path, os.O_RDONLY)
    try:
        assert _read_all(descriptor) == payload
    finally:
        os.close(descriptor)
    descriptor = opener(path, os.O_WRONLY | os.O_APPEND)
    try:
        _write_all(descriptor, b"\r\n\x1a\n")
    finally:
        os.close(descriptor)
    assert path.read_bytes() == payload + b"\r\n\x1a\n"
    with pytest.raises(FileExistsError):
        opener(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def assert_directory_anchor_and_private_mode(tmp_path: Path, opener: Callable) -> None:
    if os.open not in os.supports_dir_fd and os.name == "nt":
        path = tmp_path / "anchored.bin"
        with pytest.raises(NotImplementedError):
            opener(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=0)
        assert not path.exists()
        return
    directory = opener(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = opener(
            "anchored.bin",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            _write_all(descriptor, bytes(range(256)))
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    path = tmp_path / "anchored.bin"
    assert path.read_bytes() == bytes(range(256))
    assert path.stat().st_mode & 0o777 == 0o600


def assert_explicit_text_mode_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: Callable
) -> None:
    text_flag = getattr(os, "O_TEXT", 1 << 28)
    monkeypatch.setattr(os, "O_TEXT", text_flag, raising=False)
    path = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="text mode"):
        opener(path, os.O_WRONLY | os.O_CREAT | text_flag, 0o600)
    assert not path.exists()


def raw_descriptor_owners(source: Path) -> list[str]:
    """Check every raw-I/O owner against the package-local binary opener."""
    covered = []
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        if not any(node.func.attr in {"read", "write"} for node in calls):
            continue
        assert not any(node.func.attr == "open" for node in calls), str(path)
        covered.append(path.relative_to(source).as_posix())
    return covered
