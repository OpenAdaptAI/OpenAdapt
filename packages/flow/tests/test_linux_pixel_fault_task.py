"""Software-only request, image and owned-child controls; no X11/RDP launch."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.qualify import linux_pixel_fault_renderer as renderer
from openadapt_flow.qualify import linux_pixel_fault_task as tasks
from openadapt_flow.qualify.rdp_pairing import pairing_request, witness_digest
from tests.test_qualify_rdp_pixel_fault_controller import CONNECTION, GUEST, context_for
from tests.test_qualify_rdp_pixel_fault_controller import rig as rig

IDENTITY = {
    "schema": "openadapt.linux-x11-display/v1",
    "pid": 10,
    "uid": 1000,
    "start_ticks": 123,
    "socket_device": 1,
    "socket_inode": 2,
    "socket_ctime_ns": 3,
    "boot_id": "synthetic-boot",
}


def request():
    return pairing_request(
        nonce="c" * 32,
        connection_digest=CONNECTION,
        viewport=(300, 200),
        region=(0, 0, 52, 52),
        excluded_regions=((100, 60, 120, 80),),
    )


def fault(kind="ambiguity"):
    value = {
        "schema": renderer.FAULT,
        "nonce": "d" * 32,
        "session_identity_sha256": GUEST,
        "kind": kind,
        "target": {"x": 100, "y": 80, "width": 12, "height": 12},
        "screen": {"width": 300, "height": 200},
    }
    if kind == "ambiguity":
        value.update(
            duplicate={"x": 120, "y": 80, "width": 12, "height": 12}, search_pad=40
        )
    else:
        value["identifier_region"] = {"x": 180, "y": 80, "width": 40, "height": 20}
    return value


def test_renderer_grid_matches_every_existing_wire_pixel():
    ppm = renderer.witness_ppm("c" * 32, CONNECTION, GUEST)
    image = Image.open(io.BytesIO(ppm))
    digest = witness_digest("c" * 32, CONNECTION, GUEST)
    assert image.size == (52, 52)
    for y in range(52):
        for x in range(52):
            value = 0
            if 2 <= x < 50 and 2 <= y < 50:
                bit = ((y - 2) // 3) * 16 + (x - 2) // 3
                value = 255 if digest[bit // 8] & (128 >> (bit % 8)) else 0
            assert image.getpixel((x, y)) == (value, value, value)
    assert renderer.witness_ppm("e" * 32, CONNECTION, GUEST) != ppm
    assert renderer.witness_ppm("c" * 32, "f" * 64, GUEST) != ppm
    assert renderer.witness_ppm("c" * 32, CONNECTION, "f" * 64) != ppm


@pytest.mark.parametrize(
    "key,value",
    [
        ("nonce", "x" * 32),
        ("connection_digest", "f" * 64),
        ("lifetime_seconds", 0),
        ("lifetime_seconds", 301),
        ("lifetime_seconds", True),
        ("viewport", [301, 200]),
        ("region", [0, 0, 51, 52]),
        ("region", [-1, 0, 52, 52]),
        ("operation", "other"),
        ("text", "must not pass"),
    ],
)
def test_pairing_request_refuses_invalid_or_content_fields(key, value):
    data = request()
    data[key] = value
    with pytest.raises(ValueError):
        renderer.validate_request(data, CONNECTION, (300, 200))


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_existing_fault_geometry_is_accepted(kind):
    data = fault(kind)
    assert renderer.validate_request(data, GUEST, (300, 200))


@pytest.mark.parametrize(
    "change", ["binding", "overlap", "pad", "size", "viewport", "unknown", "content"]
)
def test_fault_geometry_rejects_unsafe_mutation(change):
    data = fault()
    if change == "binding":
        data["session_identity_sha256"] = CONNECTION
    elif change == "overlap":
        data["duplicate"] = copy.deepcopy(data["target"])
    elif change == "pad":
        data["search_pad"] = 1
    elif change == "size":
        data["duplicate"]["width"] = 13
    elif change == "viewport":
        data["screen"]["width"] = 301
    elif change == "unknown":
        data["kind"] = "click"
    else:
        data["text"] = "not permitted"
    with pytest.raises(ValueError):
        renderer.validate_request(data, GUEST, (300, 200))


@pytest.fixture
def owned(monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    server = real_popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = dict(IDENTITY, pid=server.pid)
    monkeypatch.setattr(renderer, "display_identity", lambda _: dict(identity))
    actual = []
    calls = []
    child_program = """
import sys,json,hashlib,time
e=json.load(sys.stdin)
if e['request']['nonce'] in {'e'*32, 'f'*32}:
    status = 'refused:map' if e['request']['nonce'] == 'e'*32 else 'refused:arbitrary detail'
    print(status,flush=True)
    sys.exit(1)
digest=hashlib.sha256(json.dumps(e['identity'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
print('ready:'+e['request']['nonce']+':'+digest,flush=True)
time.sleep(60)
"""

    def intercepted(argv, **kwargs):
        assert argv == [
            "/usr/bin/python3",
            "-I",
            "-S",
            str(Path(renderer.__file__).resolve()),
        ]
        assert set(kwargs["env"]) == {"PATH", "HOME", "DISPLAY"}
        calls.append((argv, kwargs))
        child = real_popen([sys.executable, "-I", "-S", "-c", child_program], **kwargs)
        actual.append(child)
        return child

    monkeypatch.setattr(tasks.subprocess, "Popen", intercepted)
    owner = tasks.LinuxPixelTasks(
        server=server,
        children=(),
        display=":999",
        viewport=(300, 200),
        home=tmp_path,
        session_binding_sha256=CONNECTION,
    )
    yield owner, server, identity, actual, calls
    owner.close()
    if server.poll() is None:
        server.terminate()
    server.wait(timeout=3)
    assert all(child.poll() is not None for child in actual)


def test_actual_owned_harmless_child_readiness_and_close(owned):
    owner, server, identity, children, calls = owned
    handle = owner.launch(request())
    assert handle.process is children[0] and len(calls) == 1
    assert (
        owner.wait_ready(handle)
        == f"ready:{handle.nonce}:{renderer.identity_digest(identity)}"
    )
    assert (
        handle.request_sha256
        == hashlib.sha256(
            json.dumps(request(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    owner.close()
    owner.close()
    assert children[0].poll() is not None and server.poll() is None


def test_nonce_cannot_launch_twice(owned):
    owner, _, _, children, _ = owned
    owner.launch(request())
    with pytest.raises(ValueError, match="nonce"):
        owner.launch(request())
    assert len(children) == 1


@pytest.mark.parametrize("change", ["server_exit", "generation", "source"])
def test_dead_or_replaced_owner_refuses_before_launch(owned, change, monkeypatch):
    owner, server, identity, children, _ = owned
    if change == "server_exit":
        server.terminate()
        server.wait(timeout=3)
    elif change == "generation":
        identity["start_ticks"] += 1
    else:
        monkeypatch.setattr(owner, "_source_digest", "0" * 64)
    with pytest.raises(ValueError, match="unavailable"):
        owner.launch(request())
    assert children == []


@pytest.mark.parametrize(
    "change", ["server_exit", "child_exit", "expired", "generation"]
)
def test_readiness_rechecks_live_owned_boundary(owned, change):
    owner, server, identity, _, _ = owned
    handle = owner.launch(request())
    if change == "server_exit":
        server.terminate()
        server.wait(timeout=3)
    elif change == "child_exit":
        handle.process.terminate()
        handle.process.wait(timeout=3)
    elif change == "expired":
        handle.deadline = 0
    else:
        identity["socket_inode"] += 1
    assert owner.wait_ready(handle) is None and handle.ready_status is None


def test_foreign_handle_is_not_read_or_closed(owned):
    owner, _, _, children, _ = owned
    from types import SimpleNamespace

    foreign = SimpleNamespace(process=None)
    assert owner.wait_ready(foreign) is None
    owner.close()
    assert not children


def test_factory_receives_real_connection_then_observed_guest(rig, monkeypatch):
    from openadapt_flow.qualify import rdp_pixel_fault_controller as controller

    original = controller.OwnedPixelTasks
    bindings = []

    def factory(binding):
        bindings.append(binding)
        return original(
            rig, guest_dir=r"C:\Synthetic\Owned", session_binding_sha256=binding
        )

    monkeypatch.setattr(
        controller,
        "OwnedPixelTasks",
        lambda *a, **kw: pytest.fail("default Windows owner must not be invoked"),
    )
    backend, close = rig.open(task_owner_factory=factory)
    context, step = context_for(backend)
    assert backend.qualification_pixel_fault_controller.apply_screen_fault(
        context, step
    )
    assert bindings == [CONNECTION, GUEST]
    close()
    assert rig.closed == list(reversed(rig.owners))


def test_factory_setup_failure_closes_created_owner_and_session(rig):
    from types import SimpleNamespace

    closed = []
    with pytest.raises(ValueError, match="task lifecycle"):
        rig.open(
            task_owner_factory=lambda _: SimpleNamespace(
                close=lambda: closed.append(True)
            )
        )
    assert closed == [True] and rig.session_closes == [True]


def test_fixture_markers_require_source_version_pixels():
    from tests.fixtures import rdp_write_app as app

    version = "sha256:" + hashlib.sha256(Path(app.__file__).read_bytes()).hexdigest()
    assert (
        len({app.marker_bytes(value) for value in (app.APP, version, app.ENVIRONMENT)})
        == 3
    )


@pytest.mark.parametrize(
    "change", ["none", "foreign_uid", "non_socket", "replaced_socket", "dead_peer"]
)
def test_kernel_peer_identity_refuses_foreign_replaced_or_dead_server(
    monkeypatch, change
):
    import socket
    import stat
    import struct
    from types import SimpleNamespace

    attrs = SimpleNamespace(
        st_mode=stat.S_IFSOCK, st_uid=os.getuid(), st_dev=1, st_ino=2, st_ctime_ns=3
    )
    reads = []

    def lstat(path):
        assert str(path) == "/tmp/.X11-unix/X999"
        reads.append(True)
        return SimpleNamespace(
            **{
                **vars(attrs),
                "st_mode": stat.S_IFREG if change == "non_socket" else attrs.st_mode,
                "st_ino": 4 if change == "replaced_socket" and len(reads) > 1 else 2,
            }
        )

    class Peer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, value):
            assert value == 2

        def connect(self, path):
            assert path == "/tmp/.X11-unix/X999"

        def getsockopt(self, level, name, size):
            assert (level, name, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.pack(
                "3i",
                123,
                os.getuid() + (1 if change == "foreign_uid" else 0),
                os.getgid(),
            )

    monkeypatch.setattr(renderer.socket, "socket", lambda *args: Peer())
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "read_text", lambda path: "synthetic-boot")

    def start(pid):
        assert pid == 123
        if change == "dead_peer":
            raise ValueError("not live")
        return 456

    monkeypatch.setattr(renderer, "process_start", start)
    if change == "none":
        result = renderer.display_identity(":999")
        assert result["pid"] == 123 and result["start_ticks"] == 456
        assert result["socket_inode"] == 2 and result["uid"] == os.getuid()
    else:
        with pytest.raises(ValueError):
            renderer.display_identity(":999")


@pytest.mark.parametrize("nonce,expected", [("e" * 32, "map"), ("f" * 32, None)])
def test_exited_renderer_can_report_only_fixed_refusal_stage(owned, nonce, expected):
    owner, _, _, _, _ = owned
    data = request()
    data["nonce"] = nonce
    handle = owner.launch(data)
    assert handle.process.wait(timeout=3) == 1
    assert owner.wait_ready(handle) is None
    assert handle.ready_status is None and handle.refusal_stage == expected


@pytest.mark.parametrize("stage", sorted(renderer.REFUSAL_STAGES))
def test_renderer_failure_exports_only_fixed_stage(monkeypatch, capsys, stage):
    from types import SimpleNamespace

    def failed(_envelope, *, stage):
        stage[0] = selected
        raise RuntimeError("arbitrary guest payload must not escape")

    selected = stage
    monkeypatch.setattr(renderer.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(renderer, "render", failed)
    assert renderer.main() == 1
    output = capsys.readouterr()
    assert output.out == f"refused:{stage}\n" and output.err == ""
