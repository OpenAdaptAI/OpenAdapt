"""Desktop registers the Chrome extension's native host per recording (OpenAdapt#1872).

Every test registers into a temp ``NativeMessagingHosts`` folder or a fake home;
nothing touches a real browser profile.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openadapt_desktop import chrome_extension_host as host
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.controller import RecordingState
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
REPO = Path(__file__).resolve().parents[4]
FLOW_TEMPLATE = REPO / "packages/flow/chrome_extension/native_host/com.openadapt.extension.json"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")


@pytest.fixture
def host_script(tmp_path: Path) -> Path:
    """A stand-in host that records its argv, so the launcher can be run."""

    script = tmp_path / "flow" / "openadapt_extension_host.py"
    script.parent.mkdir()
    script.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    return script


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestRegister:
    def test_manifest_allows_exactly_one_origin_and_matches_flow_template(
        self, tmp_path: Path, host_script: Path
    ) -> None:
        hosts = tmp_path / "profile" / "NativeMessagingHosts"
        recording = tmp_path / "capture"
        recording.mkdir()
        active = tmp_path / "control" / ".extension-active-cap1"

        path = host.register(
            hosts, EXT_ID, recording, active, host_script=host_script, python=sys.executable
        )

        manifest = json.loads(path.read_text())
        assert manifest["allowed_origins"] == [f"chrome-extension://{EXT_ID}/"]
        assert manifest["path"] == str(hosts.resolve() / host.LAUNCHER_FILE)
        template = json.loads(FLOW_TEMPLATE.read_text())
        assert set(manifest) == set(template)
        assert (manifest["name"], manifest["type"]) == (template["name"], template["type"])
        assert path.name == f"{template['name']}.json"
        assert _mode(path) == 0o600
        assert _mode(hosts / host.LAUNCHER_FILE) == 0o700
        assert active.is_file()

    def test_launcher_points_host_at_recording_and_active_file(
        self, tmp_path: Path, host_script: Path
    ) -> None:
        hosts = tmp_path / "hosts"
        recording = tmp_path / "rec dir"
        recording.mkdir()
        active = tmp_path / "control" / ".extension-active-cap1"
        host.register(
            hosts, EXT_ID, recording, active, host_script=host_script, python=sys.executable
        )

        out = subprocess.run(
            [str(hosts / host.LAUNCHER_FILE), f"chrome-extension://{EXT_ID}/"],
            capture_output=True,
            text=True,
            check=True,
        )

        assert json.loads(out.stdout) == [
            "--recording",
            str(recording.resolve()),
            "--active-file",
            str(active),
            "--active-lease",
            str(host.LEASE_SECONDS),
            f"chrome-extension://{EXT_ID}/",
        ]
        assert active.read_text(encoding="utf-8") == host.ACTIVE_TEXT

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "*",
            "chrome-extension://*/",
            "ABCDEFGHIJKLMNOPABCDEFGHIJKLMNOP",
            "a" * 31,
            "q" * 32,
            EXT_ID + "\n",
            "\n" + EXT_ID,
            EXT_ID[:16] + "\n" + EXT_ID[16:],
            EXT_ID + "\r\n",
            EXT_ID + " ",
        ],
    )
    def test_bad_extension_id_writes_nothing(
        self, tmp_path: Path, host_script: Path, bad: str | None
    ) -> None:
        hosts = tmp_path / "hosts"
        recording = tmp_path / "capture"
        recording.mkdir()
        with pytest.raises(ValueError):
            host.register(
                hosts,
                bad,
                recording,
                tmp_path / "active",
                host_script=host_script,
                python=sys.executable,
            )
        assert not hosts.exists()
        assert not (tmp_path / "active").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["capture", "flow"]

    @pytest.mark.parametrize(
        "bad", [None, "", EXT_ID + "\n", "\n" + EXT_ID, EXT_ID[:16] + "\n" + EXT_ID[16:]]
    )
    def test_build_manifest_refuses_incomplete_id_match(
        self, tmp_path: Path, bad: str | None
    ) -> None:
        with pytest.raises(ValueError):
            host.build_manifest(bad, tmp_path / host.LAUNCHER_FILE)

    def test_missing_recording_folder_is_refused(self, tmp_path: Path, host_script: Path) -> None:
        with pytest.raises(ValueError, match="recording folder"):
            host.register(
                tmp_path / "hosts",
                EXT_ID,
                tmp_path / "gone",
                tmp_path / "active",
                host_script=host_script,
                python=sys.executable,
            )
        assert not (tmp_path / "hosts").exists()

    def test_foreign_registration_is_left_alone(self, tmp_path: Path, host_script: Path) -> None:
        hosts = tmp_path / "hosts"
        hosts.mkdir()
        foreign = {"name": host.HOST_NAME, "allowed_origins": ["chrome-extension://x/"]}
        (hosts / host.MANIFEST_FILE).write_text(json.dumps(foreign))
        (hosts / host.LAUNCHER_FILE).write_text("#!/bin/sh\nexec hand-made\n")
        recording = tmp_path / "capture"
        recording.mkdir()

        with pytest.raises(ValueError, match="another native host"):
            host.register(
                hosts,
                EXT_ID,
                recording,
                tmp_path / "active",
                host_script=host_script,
                python=sys.executable,
            )
        assert host.unregister(hosts) == []
        assert json.loads((hosts / host.MANIFEST_FILE).read_text()) == foreign
        assert (hosts / host.LAUNCHER_FILE).read_text() == "#!/bin/sh\nexec hand-made\n"

    def test_unregister_removes_only_desktop_files(self, tmp_path: Path, host_script: Path) -> None:
        hosts = tmp_path / "hosts"
        control = tmp_path / "control"
        recording = tmp_path / "capture"
        recording.mkdir()
        host.register(
            hosts,
            EXT_ID,
            recording,
            control / ".extension-active-cap1",
            host_script=host_script,
            python=sys.executable,
        )
        (hosts / "other.host.json").write_text("{}")
        (control / ".stop-cap1").write_text("")

        removed = host.unregister(hosts, control_dir=control)

        assert {p.name for p in removed} == {
            host.MANIFEST_FILE,
            host.LAUNCHER_FILE,
            ".extension-active-cap1",
        }
        assert sorted(p.name for p in hosts.iterdir()) == ["other.host.json"]
        assert (control / ".stop-cap1").exists()
        assert recording.is_dir()


class TestUserDirs:
    def test_per_user_dirs_under_a_fake_home(self, tmp_path: Path) -> None:
        assert host.user_manifest_dir("chrome", home=tmp_path, platform="darwin") == (
            tmp_path / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
        )
        assert host.user_manifest_dir("chromium", home=tmp_path, platform="linux") == (
            tmp_path / ".config/chromium/NativeMessagingHosts"
        )
        assert host.user_manifest_dir("chrome", home=tmp_path, platform="win32") is None
        assert host.user_manifest_dir("edge", home=tmp_path, platform="linux") is None


class _Controller:
    def __init__(self, db: IndexDB, capture_dir: Path) -> None:
        self.state = RecordingState.IDLE
        self.db = db
        self.capture_dir = capture_dir
        self.compiled = {"bundle_id": "bnd1", "bundle_path": "/tmp/b", "ok": True}

    @property
    def is_recording(self) -> bool:
        return self.state is RecordingState.RECORDING

    @property
    def current_capture_dir(self) -> Path | None:
        return self.capture_dir if self.is_recording else None

    @property
    def current_capture_id(self) -> str | None:
        return "cap1" if self.is_recording else None

    def start(self, task_description: str = "", *, metadata=None) -> str:
        self.state = RecordingState.RECORDING
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        if self.db.get_capture("cap1") is None:
            self.db.insert_capture("cap1", str(self.capture_dir), "2026-11-06T00:00:00+00:00")
        return "cap1"

    def stop(self) -> dict:
        self.state = RecordingState.IDLE
        return {"id": "cap1", "duration": 1.0, "event_count": 0, "size_bytes": 0}

    def compile_capture(self, capture_id, capture_dir):
        return self.compiled


class _Storage:
    def get_captures(self, limit=50, review_status=None):
        return []

    def get_storage_usage(self):
        return {"used_bytes": 0, "max_bytes": 1}


class _Audit:
    def log(self, *a, **k):
        pass


def _dispatcher(tmp_path: Path, **config: object):
    cfg = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING", **config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    controller = _Controller(db, cfg.data_dir / "captures" / "cap1")
    services = EngineServices(cfg, db=db, storage=_Storage(), audit=_Audit(), controller=controller)
    return EngineDispatcher(cfg, services=services), db


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr("openadapt_desktop.dispatch.sys.platform", "linux")
    monkeypatch.setattr(host.sys, "platform", "linux")


class TestDispatcherLifecycle:
    def test_registers_on_start_and_removes_on_stop(
        self, tmp_path: Path, host_script: Path, linux
    ) -> None:
        hosts = tmp_path / "profile" / "NativeMessagingHosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        try:
            disp.dispatch("start_recording", {})

            manifest = json.loads((hosts / host.MANIFEST_FILE).read_text())
            assert manifest["allowed_origins"] == [f"chrome-extension://{EXT_ID}/"]
            launcher = (hosts / host.LAUNCHER_FILE).read_text()
            capture = disp.services.controller.capture_dir
            assert str(capture.resolve()) in launcher
            active = disp.config.data_dir / "recording-control" / ".extension-active-cap1"
            assert active.is_file()

            disp.dispatch("stop_recording", {})

            assert not (hosts / host.MANIFEST_FILE).exists()
            assert not (hosts / host.LAUNCHER_FILE).exists()
            assert not active.exists()
            assert capture.is_dir()
        finally:
            db.close()

    def test_off_by_default_writes_nothing(self, tmp_path: Path, host_script: Path, linux) -> None:
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path, chrome_native_hosts_dir=hosts, chrome_extension_host_script=host_script
        )
        try:
            assert disp.dispatch("start_recording", {})["recording"] is True
            assert not hosts.exists()
            disp.dispatch("stop_recording", {})
            assert not hosts.exists()
        finally:
            db.close()

    @pytest.mark.parametrize("bad", ["*", "not-an-extension-id", EXT_ID + "\n", "\n" + EXT_ID])
    def test_bad_id_skips_registration_but_still_records(
        self, tmp_path: Path, host_script: Path, linux, bad: str
    ) -> None:
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=bad,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        try:
            assert disp.dispatch("start_recording", {})["recording"] is True
            assert not (hosts / host.MANIFEST_FILE).exists()
            assert not list((disp.config.data_dir / "recording-control").glob(".extension-*"))
        finally:
            db.close()

    def test_missing_host_script_skips_registration(self, tmp_path: Path, linux) -> None:
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=tmp_path / "missing.py",
        )
        try:
            assert disp.dispatch("start_recording", {})["recording"] is True
            assert not (hosts / host.MANIFEST_FILE).exists()
        finally:
            db.close()

    @pytest.mark.parametrize("platform", ["win32", "cygwin"])
    def test_unsupported_platform_skips(
        self, tmp_path: Path, host_script: Path, monkeypatch, platform: str
    ) -> None:
        monkeypatch.setattr("openadapt_desktop.dispatch.sys.platform", "linux")
        monkeypatch.setattr(host.sys, "platform", platform)
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        try:
            assert disp.dispatch("start_recording", {})["recording"] is True
            assert not hosts.exists()
        finally:
            db.close()

    def test_frozen_app_skips(self, tmp_path: Path, host_script: Path, linux, monkeypatch) -> None:
        monkeypatch.setattr(host.sys, "frozen", True, raising=False)
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        try:
            assert disp.dispatch("start_recording", {})["recording"] is True
            assert not hosts.exists()
        finally:
            db.close()

    def test_sweep_removes_stale_desktop_registration_even_when_disabled(
        self, tmp_path: Path, host_script: Path, linux
    ) -> None:
        hosts = tmp_path / "hosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        try:
            disp.dispatch("start_recording", {})  # then the engine "crashes"
            assert (hosts / host.MANIFEST_FILE).exists()
        finally:
            db.close()

        restarted, db2 = _dispatcher(tmp_path, chrome_native_hosts_dir=hosts)
        try:
            restarted.extension_host.sweep()
            assert not (hosts / host.MANIFEST_FILE).exists()
            assert not (hosts / host.LAUNCHER_FILE).exists()
            control = restarted.config.data_dir / "recording-control"
            assert not list(control.glob(".extension-active-*"))
        finally:
            db2.close()

    def test_stop_without_registration_does_not_touch_hosts(
        self, tmp_path: Path, host_script: Path, linux
    ) -> None:
        hosts = tmp_path / "hosts"
        hosts.mkdir()
        recording = tmp_path / "other"
        recording.mkdir()
        # A registration from a different Desktop session stays until sweep.
        host.register(
            hosts,
            EXT_ID,
            recording,
            tmp_path / "elsewhere" / ".extension-active-x",
            host_script=host_script,
            python=sys.executable,
        )
        disp, db = _dispatcher(tmp_path, chrome_native_hosts_dir=hosts)
        try:
            disp.dispatch("start_recording", {})
            disp.dispatch("stop_recording", {})
            assert (hosts / host.MANIFEST_FILE).exists()
        finally:
            db.close()


REAL_HOST = REPO / "packages/flow/chrome_extension/native_host/openadapt_extension_host.py"


def _frame(message: dict) -> bytes:
    import struct

    data = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def _read(stream) -> dict:
    import struct

    (length,) = struct.unpack("<I", stream.read(4))
    return json.loads(stream.read(length))


@pytest.mark.skipif(not REAL_HOST.is_file(), reason="Flow host script not in this checkout")
def test_registered_launcher_runs_real_host_into_capture_until_stop(tmp_path: Path, linux) -> None:
    """Desktop's launcher starts the real Flow host the way Chrome would."""

    hosts = tmp_path / "profile" / "NativeMessagingHosts"
    disp, db = _dispatcher(
        tmp_path,
        chrome_extension_id=EXT_ID,
        chrome_native_hosts_dir=hosts,
        chrome_extension_host_script=REAL_HOST,
    )
    try:
        disp.dispatch("start_recording", {})
        capture = disp.services.controller.capture_dir
        manifest = json.loads((hosts / host.MANIFEST_FILE).read_text())
        proc = subprocess.Popen(
            [manifest["path"], f"chrome-extension://{EXT_ID}/"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        secret = {
            "type": "EXTENSION_EVIDENCE",
            "evidence": {
                "kind": "target",
                "event": "input",
                "path": "input#pw",
                "name": "password",
                "input_type": "password",
                "value": "hunter2-live",
            },
        }
        proc.stdin.write(_frame(secret))
        proc.stdin.flush()
        assert _read(proc.stdout) == {"ok": True, "count": 1}

        disp.dispatch("stop_recording", {})  # Chrome keeps the host running
        proc.stdin.write(_frame(secret))
        proc.stdin.flush()
        assert _read(proc.stdout) == {"ok": False, "error": "not_recording"}
        proc.stdin.close()
        proc.wait(timeout=30)

        text = (capture / "extension_evidence.jsonl").read_text()
        assert "hunter2" not in text
        rows = [json.loads(line) for line in text.splitlines()]
        assert rows[0]["events"] == 1
        assert rows[1]["value"] == "<secret>"
        assert not (hosts / host.MANIFEST_FILE).exists()
    finally:
        db.close()


def _fail_unlink_for(monkeypatch, target: Path) -> None:
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == target:
            raise PermissionError("synthetic: cannot remove the marker")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)


def _fail_open_for(monkeypatch, target: Path) -> None:
    real_open = os.open

    def fake_open(path, flags, *args, **kwargs):
        if Path(path) == target and flags & os.O_WRONLY:
            raise PermissionError("synthetic: cannot overwrite the marker")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(host.os, "open", fake_open)


def _fail_utime_for(monkeypatch, target: Path) -> None:
    real_utime = os.utime

    def fake_utime(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError("synthetic: cannot change the marker's time")
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(host.os, "utime", fake_utime)


class TestRevokeMarker:
    def _marker(self, tmp_path: Path) -> Path:
        marker = tmp_path / ".extension-active-cap1"
        marker.write_text(host.ACTIVE_TEXT, encoding="utf-8")
        return marker

    def test_removed(self, tmp_path: Path) -> None:
        marker = self._marker(tmp_path)
        assert host.marker_revoked(marker) is False
        assert host.revoke_marker(marker) is True
        assert not marker.exists()

    def test_overwritten_and_expired_when_removal_fails(self, tmp_path: Path, monkeypatch) -> None:
        marker = self._marker(tmp_path)
        _fail_unlink_for(monkeypatch, marker)
        assert host.revoke_marker(marker) is True
        assert marker.read_text(encoding="utf-8") == host.REVOKED_TEXT
        assert marker.stat().st_mtime == 0

    def test_expired_lease_alone_counts(self, tmp_path: Path, monkeypatch) -> None:
        marker = self._marker(tmp_path)
        _fail_unlink_for(monkeypatch, marker)
        _fail_open_for(monkeypatch, marker)
        assert host.revoke_marker(marker) is True
        assert marker.read_text(encoding="utf-8") == host.ACTIVE_TEXT
        assert marker.stat().st_mtime == 0

    def test_every_route_failing_is_reported(self, tmp_path: Path, monkeypatch) -> None:
        marker = self._marker(tmp_path)
        _fail_unlink_for(monkeypatch, marker)
        _fail_open_for(monkeypatch, marker)
        _fail_utime_for(monkeypatch, marker)
        assert host.revoke_marker(marker) is False
        with pytest.raises(host.RevocationError) as err:
            host.unregister(None, control_dir=tmp_path)
        assert err.value.markers == [marker]

    @pytest.mark.parametrize("text", ["", "revoked\n", "active", "active\n\n"])
    def test_anything_but_exact_active_is_revoked(self, tmp_path: Path, text: str) -> None:
        marker = tmp_path / ".extension-active-cap1"
        marker.write_text(text, encoding="utf-8")
        assert host.marker_revoked(marker) is True

    def test_stale_lease_is_revoked(self, tmp_path: Path) -> None:
        marker = self._marker(tmp_path)
        old = marker.stat().st_mtime - host.LEASE_SECONDS - 5
        os.utime(marker, (old, old))
        assert host.marker_revoked(marker) is True


class TestStopFailsClosed:
    """Stop-time cleanup failures still end an already-running host (BLOCK on #2017)."""

    def _start(self, tmp_path: Path, host_script: Path):
        hosts = tmp_path / "profile" / "NativeMessagingHosts"
        disp, db = _dispatcher(
            tmp_path,
            chrome_extension_id=EXT_ID,
            chrome_native_hosts_dir=hosts,
            chrome_extension_host_script=host_script,
        )
        disp.dispatch("start_recording", {})
        active = disp.config.data_dir / "recording-control" / ".extension-active-cap1"
        assert host.marker_revoked(active) is False
        return disp, db, hosts, active

    def test_unlocatable_hosts_folder_still_revokes(
        self, tmp_path: Path, host_script: Path, linux, monkeypatch
    ) -> None:
        disp, db, _hosts, active = self._start(tmp_path, host_script)
        try:

            def broken() -> Path:
                raise RuntimeError("synthetic: no home folder")

            monkeypatch.setattr(disp.extension_host, "manifest_dir", broken)
            disp.dispatch("stop_recording", {})
            assert not active.exists()
            assert disp.extension_host.revoked
        finally:
            db.close()

    def test_failing_unregister_still_revokes(
        self, tmp_path: Path, host_script: Path, linux, monkeypatch
    ) -> None:
        disp, db, _hosts, active = self._start(tmp_path, host_script)
        try:

            def broken(*args, **kwargs):
                raise OSError("synthetic: cleanup failed")

            monkeypatch.setattr(host, "unregister", broken)
            disp.dispatch("stop_recording", {})
            assert host.marker_revoked(active)
            assert disp.extension_host.revoked
        finally:
            db.close()

    def test_unremovable_marker_is_overwritten(
        self, tmp_path: Path, host_script: Path, linux, monkeypatch
    ) -> None:
        disp, db, hosts, active = self._start(tmp_path, host_script)
        try:
            _fail_unlink_for(monkeypatch, active)
            disp.dispatch("stop_recording", {})
            assert active.read_text(encoding="utf-8") == host.REVOKED_TEXT
            assert host.marker_revoked(active)
            assert disp.extension_host.revoked
            assert not (hosts / host.MANIFEST_FILE).exists()
        finally:
            db.close()

    def test_unrevoked_marker_blocks_the_next_registration_until_revoked(
        self, tmp_path: Path, host_script: Path, linux, monkeypatch
    ) -> None:
        disp, db, hosts, active = self._start(tmp_path, host_script)
        try:
            with monkeypatch.context() as broken:
                _fail_unlink_for(broken, active)
                _fail_open_for(broken, active)
                _fail_utime_for(broken, active)
                disp.dispatch("stop_recording", {})
                assert not disp.extension_host.revoked
                assert active in disp.extension_host.unrevoked
                # Registration files still go, so Chrome can't start a new host.
                assert not (hosts / host.MANIFEST_FILE).exists()

                # The next recording still records, without extension evidence.
                assert disp.dispatch("start_recording", {})["recording"] is True
                assert not (hosts / host.MANIFEST_FILE).exists()
                disp.dispatch("stop_recording", {})
                assert not disp.extension_host.revoked

            # Once revocation works again, the next stop or start clears it.
            disp.extension_host.stop()
            assert disp.extension_host.revoked
            assert not active.exists()
            disp.dispatch("start_recording", {})
            assert (hosts / host.MANIFEST_FILE).exists()
            disp.dispatch("stop_recording", {})
        finally:
            db.close()

    def test_lease_is_renewed_while_recording_and_not_after_stop(
        self, tmp_path: Path, host_script: Path, linux, monkeypatch
    ) -> None:
        monkeypatch.setattr(host, "HEARTBEAT_SECONDS", 0.05)
        disp, db, _hosts, active = self._start(tmp_path, host_script)
        try:
            os.utime(active, (1000, 1000))
            deadline = time.monotonic() + 10
            while active.stat().st_mtime == 1000 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert active.stat().st_mtime > 1000
            assert host.marker_revoked(active) is False

            with monkeypatch.context() as broken:
                _fail_unlink_for(broken, active)
                _fail_open_for(broken, active)
                disp.dispatch("stop_recording", {})
            # Only the backdated lease revoked it; the heartbeat must not renew it.
            assert active.read_text(encoding="utf-8") == host.ACTIVE_TEXT
            time.sleep(0.3)
            assert active.stat().st_mtime == 0
            assert host.marker_revoked(active)
            assert disp.extension_host._heartbeat is None
        finally:
            db.close()


@pytest.mark.skipif(not REAL_HOST.is_file(), reason="Flow host script not in this checkout")
@pytest.mark.parametrize("failure", ["unlink", "unlink+overwrite", "everything"])
def test_real_host_refuses_after_a_failed_stop_cleanup(
    tmp_path: Path, linux, monkeypatch, failure: str
) -> None:
    """A host Chrome keeps running after stop writes nothing, even when cleanup fails."""

    hosts = tmp_path / "profile" / "NativeMessagingHosts"
    disp, db = _dispatcher(
        tmp_path,
        chrome_extension_id=EXT_ID,
        chrome_native_hosts_dir=hosts,
        chrome_extension_host_script=REAL_HOST,
    )
    evidence = {
        "type": "EXTENSION_EVIDENCE",
        "evidence": {"kind": "target", "event": "click", "path": "button#save"},
    }
    proc = None
    try:
        disp.dispatch("start_recording", {})
        capture = disp.services.controller.capture_dir
        active = disp.config.data_dir / "recording-control" / ".extension-active-cap1"
        manifest = json.loads((hosts / host.MANIFEST_FILE).read_text())
        proc = subprocess.Popen(
            [manifest["path"], f"chrome-extension://{EXT_ID}/"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(_frame(evidence))
        proc.stdin.flush()
        assert _read(proc.stdout) == {"ok": True, "count": 1}

        with monkeypatch.context() as broken:

            def no_hosts_dir() -> Path:
                raise RuntimeError("synthetic: no home folder")

            broken.setattr(disp.extension_host, "manifest_dir", no_hosts_dir)
            _fail_unlink_for(broken, active)
            if failure != "unlink":
                _fail_open_for(broken, active)
            if failure == "everything":
                _fail_utime_for(broken, active)
            disp.dispatch("stop_recording", {})
        assert disp.extension_host.revoked is (failure != "everything")
        if failure == "everything":
            # Nothing Desktop tried worked; the heartbeat has stopped, so the
            # lease runs out. Stand in for LEASE_SECONDS passing.
            stale = time.time() - host.LEASE_SECONDS - 1
            os.utime(active, (stale, stale))

        proc.stdin.write(_frame(evidence))
        proc.stdin.flush()
        assert _read(proc.stdout) == {"ok": False, "error": "not_recording"}
        proc.stdin.close()
        proc.wait(timeout=30)
        proc = None
        rows = (capture / "extension_evidence.jsonl").read_text().splitlines()
        assert json.loads(rows[0])["events"] == 1
        assert len(rows) == 2
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=30)
        db.close()
