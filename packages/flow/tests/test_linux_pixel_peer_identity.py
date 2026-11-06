"""Real Linux Unix-peer generation controls, without X11, RDP or GUI inputs."""

from __future__ import annotations

import errno
import os
import selectors
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from openadapt_flow.qualify import linux_pixel_fault_renderer as renderer
from openadapt_flow.qualify.linux_pixel_fault_task import LinuxPixelTasks

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel peers")

DISPLAY = ":49123"

# This child only accepts/closes owned Unix connections and handles fixed pipe
# commands. It does not implement X11, inspect requests or launch other programs.
SERVICE = """
import os
import selectors
import socket
import sys

path = sys.argv[1]
selector = selectors.DefaultSelector()

def listener(name):
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(name)
    peer.listen(16)
    return peer

server = listener(path)
selector.register(server, selectors.EVENT_READ, 'socket')
selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'stdin')
print('ready', flush=True)
try:
    running = True
    while running:
        for key, _ in selector.select():
            if key.data == 'socket':
                connection, _ = server.accept()
                connection.close()
                continue
            command = sys.stdin.buffer.readline()
            if not command or command == b'stop\\n':
                running = False
                break
            if command != b'rebind\\n':
                raise RuntimeError('unknown owned control')
            # Allocate the new inode while the old socket is still open, then
            # atomically replace only this service's private socket pathname.
            replacement = listener(path + '.next')
            os.replace(path + '.next', path)
            selector.unregister(server)
            server.close()
            server = replacement
            selector.register(server, selectors.EVENT_READ, 'socket')
            print('rebound', flush=True)
finally:
    selector.close()
    server.close()
"""


def _line(child: subprocess.Popen[bytes]) -> bytes:
    assert child.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(3), "owned peer did not become ready"
        return child.stdout.readline()


def _command(child: subprocess.Popen[bytes], value: bytes) -> None:
    assert child.stdin is not None and child.poll() is None
    child.stdin.write(value + b"\n")
    child.stdin.flush()


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        _command(child, b"stop")
    assert child.wait(timeout=3) == 0


@contextmanager
def _service(path: Path) -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-u", "-c", SERVICE, str(path)],
        cwd=path.parent,
        env={"PATH": os.defpath, "OPENADAPT_NO_OUTBOUND": "1"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _line(child) == b"ready\n"
        yield child
    finally:
        try:
            if child.poll() is None:
                try:
                    assert child.stdin is not None
                    child.stdin.write(b"stop\n")
                    child.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Only the exact Popen object created above can be signalled.
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        finally:
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    stream.close()
        assert child.poll() is not None


@pytest.fixture
def socket_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short private directory also avoids AF_UNIX's pathname-length limit,
    # independently of the caller's potentially long pytest basetemp path.
    with tempfile.TemporaryDirectory(prefix="oa-peer-", dir="/tmp") as directory:
        root = Path(directory)
        assert (
            root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o777 == 0o700
        )
        path = root / "display.sock"

        def owned_path(value: str) -> Path:
            if value == "/tmp/.X11-unix/X49123":
                return path
            if value.startswith("/tmp/.X11-unix/"):
                raise AssertionError("unregistered display path")
            return Path(value)

        # This is the only production interception. Socket credentials,
        # lstat, process state/start and boot observations remain genuine.
        monkeypatch.setattr(renderer, "Path", owned_path)
        yield path


def _owner(child: subprocess.Popen[bytes], home: Path) -> LinuxPixelTasks:
    identity = renderer.display_identity(DISPLAY)
    return LinuxPixelTasks(
        server=child,
        children=(),
        display=DISPLAY,
        viewport=(300, 200),
        home=home,
        session_binding_sha256=renderer.identity_digest(identity),
    )


def _refuses_without_rendering(owner: LinuxPixelTasks) -> None:
    assert owner._current() is False
    # A missing generation must refuse before even request validation. If the
    # guard regresses, this invalid request still cannot spawn a GUI renderer.
    with pytest.raises(ValueError, match="owned display generation is unavailable"):
        owner.launch({})
    assert owner._handles == []


def test_live_kernel_peer_belongs_to_the_exact_owned_child(socket_path: Path) -> None:
    with _service(socket_path) as child:
        identity = renderer.display_identity(DISPLAY)
        observed = socket_path.lstat()
        fields = Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()
        assert identity["pid"] == child.pid and child.poll() is None
        assert identity["uid"] == os.getuid() == observed.st_uid
        assert identity["start_ticks"] == int(fields[19]) > 0
        assert identity["socket_device"] == observed.st_dev
        assert identity["socket_inode"] == observed.st_ino
        assert identity["socket_ctime_ns"] == observed.st_ctime_ns
        assert (
            identity["boot_id"]
            == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        assert renderer.display_identity(DISPLAY) == identity
        owner = _owner(child, socket_path.parent)
        assert owner._current() is True
        owner.close()
        assert child.poll() is None


def test_another_owned_process_cannot_claim_the_kernel_peer(socket_path: Path) -> None:
    with (
        _service(socket_path) as peer,
        _service(socket_path.parent / "other.sock") as other,
    ):
        assert renderer.display_identity(DISPLAY)["pid"] == peer.pid != other.pid
        with pytest.raises(ValueError, match="does not belong to the owned server"):
            _owner(other, socket_path.parent)
        assert peer.poll() is None and other.poll() is None


def test_same_process_rebinding_changes_the_observed_socket_generation(
    socket_path: Path,
) -> None:
    with _service(socket_path) as child:
        old_identity = renderer.display_identity(DISPLAY)
        owner = _owner(child, socket_path.parent)
        _command(child, b"rebind")
        assert _line(child) == b"rebound\n"
        new_identity = renderer.display_identity(DISPLAY)
        assert new_identity["pid"] == old_identity["pid"] == child.pid
        assert new_identity["start_ticks"] == old_identity["start_ticks"]
        assert new_identity["socket_inode"] != old_identity["socket_inode"]
        assert renderer.identity_digest(new_identity) != renderer.identity_digest(
            old_identity
        )
        _refuses_without_rendering(owner)
        current = _owner(child, socket_path.parent)
        assert current._current() is True
        owner.close()
        assert current._current() is True and child.poll() is None
        current.close()


def test_replacement_server_is_not_adopted_by_the_old_owner(socket_path: Path) -> None:
    with _service(socket_path) as first:
        old_identity = renderer.display_identity(DISPLAY)
        owner = _owner(first, socket_path.parent)
        socket_path.unlink()
        with _service(socket_path) as second:
            new_identity = renderer.display_identity(DISPLAY)
            assert new_identity["pid"] == second.pid != first.pid
            assert renderer.identity_digest(new_identity) != renderer.identity_digest(
                old_identity
            )
            _refuses_without_rendering(owner)
            assert first.poll() is None and second.poll() is None
            current = _owner(second, socket_path.parent)
            assert current._current() is True
            owner.close()
            assert first.poll() is None and second.poll() is None
            assert current._current() is True
            current.close()


@pytest.mark.parametrize("unavailable", ["exited_peer", "removed_socket"])
def test_unavailable_kernel_peer_refuses_without_adoption(
    socket_path: Path, unavailable: str
) -> None:
    with _service(socket_path) as child:
        owner = _owner(child, socket_path.parent)
        if unavailable == "exited_peer":
            _stop(child)
            assert socket_path.is_socket()
            expected_errno = errno.ECONNREFUSED
        else:
            socket_path.unlink()
            assert child.poll() is None
            expected_errno = errno.ENOENT
        with pytest.raises(OSError) as refused:
            renderer.display_identity(DISPLAY)
        assert refused.value.errno == expected_errno
        _refuses_without_rendering(owner)
        owner.close()
        if unavailable == "removed_socket":
            assert child.poll() is None
