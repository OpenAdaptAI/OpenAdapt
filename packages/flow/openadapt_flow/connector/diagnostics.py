"""Bounded local diagnostics for the connector. Only the operator opens these files."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openadapt_flow import at_rest, crypto
from openadapt_flow.private_file import open_binary, windows_descriptor_has_private_acl

MAX_OUTPUT = 32 * 1024
_AAD = b"openadapt-flow/connector-diagnostics/v1"
_SCHEMA = "openadapt.connector-diagnostics/v1"


def _read_available(fd: int) -> bytes:
    if sys.platform != "win32":
        try:
            return os.read(fd, 8192)
        except BlockingIOError:
            return b""
    # select() accepts only sockets on Windows. Peek without blocking or
    # requiring a TTY; this also works on the supported Python 3.10/3.11.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    count = wintypes.DWORD()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(count), None):
        error = ctypes.get_last_error()
        if error in (109, 232):  # ERROR_BROKEN_PIPE, ERROR_NO_DATA
            return b""
        raise OSError(error, "child output pipe unavailable")
    return os.read(fd, min(count.value, 8192)) if count.value else b""


def child_output(proc: subprocess.Popen[bytes]) -> str:
    """Drain while the child runs. A descendant retaining stdout cannot hold the lease."""
    assert proc.stdout is not None
    tail = bytearray()
    deadline = None
    with proc.stdout:
        fd = proc.stdout.fileno()
        if os.name != "nt":
            os.set_blocking(fd, False)
        while True:
            finished = proc.poll() is not None
            if finished and deadline is None:
                deadline = time.monotonic() + 0.25
            chunk = _read_available(fd)
            if chunk:
                tail.extend(chunk)
                del tail[:-MAX_OUTPUT]
                if deadline is not None and time.monotonic() >= deadline:
                    break
            elif finished:
                break
            else:
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
    return tail.decode("utf-8", errors="replace")


def _check_private(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("connector diagnostics require a regular private file")
    if os.name == "nt":
        private = windows_descriptor_has_private_acl(fd)
    else:
        private = info.st_uid == os.geteuid() and not info.st_mode & 0o077
    if not private:
        raise ValueError("connector diagnostics require owner-only access")


def write_diagnostics(path: Path, data: dict[str, Any]) -> None:
    """Publish an owner-only sidecar atomically, honoring the operator's key/opt-out."""
    body = json.dumps(
        {"schema": _SCHEMA, "diagnostic": data}, ensure_ascii=False
    ).encode("utf-8")
    key = crypto.resolve_key(None)
    if key:
        body = crypto.encrypt_bytes(body, key, aad=_AAD)
    elif not at_rest.plaintext_allowed("connector diagnostics"):
        raise at_rest.AtRestRefused("connector diagnostics require an at-rest key")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".connector-diagnostics-")
    try:
        with os.fdopen(fd, "wb") as stream:
            _check_private(stream.fileno())
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_diagnostics(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(open_binary(path, flags), "rb") as stream:
        _check_private(stream.fileno())
        body = stream.read()
    if crypto.is_encrypted(body):
        body = crypto.decrypt_bytes(body, crypto.resolve_read_key(None), aad=_AAD)
    record = json.loads(body)
    if (
        not isinstance(record, dict)
        or record.get("schema") != _SCHEMA
        or not isinstance(record.get("diagnostic"), dict)
    ):
        raise ValueError("unrecognized connector diagnostic")
    return record["diagnostic"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read a private local connector diagnostic."
    )
    parser.add_argument("path", type=Path)
    try:
        print(
            json.dumps(
                read_diagnostics(parser.parse_args().path), indent=2, ensure_ascii=False
            )
        )
    except (OSError, ValueError, crypto.CryptoError, at_rest.LocalKeyError):
        parser.exit(
            1,
            "Unable to open the private connector diagnostic. Check its permissions and key.\n",
        )
