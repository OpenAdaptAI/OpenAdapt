"""Tests for the desktop IPC client + discovery."""

import json

from openadapt_tray.ipc import (
    DEFAULT_DISCOVERY_PATH,
    DesktopEndpoint,
    IPCClient,
    IPCMessage,
    IPCMessageType,
)


class TestIPCMessageTypes:
    """Tests for the extended IPC message vocabulary (spec §3d)."""

    def test_command_types_present(self):
        """All tray→desktop command types exist."""
        for name in [
            "START_RECORDING",
            "STOP_RECORDING",
            "GET_STATUS",
            "OPEN_WORKFLOW_LIBRARY",
            "OPEN_TEACH",
            "PAUSE_SYNC",
            "RESUME_SYNC",
        ]:
            assert hasattr(IPCMessageType, name)

    def test_event_types_present(self):
        """All desktop→tray event types exist."""
        for name in [
            "RECORDING_STARTED",
            "RECORDING_STOPPED",
            "RECORDING_ERROR",
            "STATUS_UPDATE",
            "COMPILE_PROGRESS",
            "SYNC_STATE",
            "BREAK_COUNT",
        ]:
            assert hasattr(IPCMessageType, name)

    def test_rl_training_type_removed(self):
        """The retired RL training-progress type must be gone."""
        assert not hasattr(IPCMessageType, "TRAINING_PROGRESS")


class TestIPCMessageSerialization:
    """Tests for message (de)serialization including the session token."""

    def test_roundtrip_with_token(self):
        """A command with a token round-trips through JSON."""
        msg = IPCMessage(
            type=IPCMessageType.START_RECORDING,
            data={"name": "w"},
            token="sess-123",
        )
        restored = IPCMessage.from_json(msg.to_json())
        assert restored.type == IPCMessageType.START_RECORDING
        assert restored.data == {"name": "w"}
        assert restored.token == "sess-123"

    def test_token_omitted_when_absent(self):
        """No token key is emitted when the message has none."""
        msg = IPCMessage(type=IPCMessageType.GET_STATUS)
        assert "token" not in json.loads(msg.to_json())

    def test_new_types_roundtrip(self):
        """New event types deserialize correctly."""
        for mt in [
            IPCMessageType.COMPILE_PROGRESS,
            IPCMessageType.SYNC_STATE,
            IPCMessageType.BREAK_COUNT,
        ]:
            restored = IPCMessage.from_json(
                IPCMessage(type=mt, data={"x": 1}).to_json()
            )
            assert restored.type == mt


class TestDesktopEndpointDiscovery:
    """Tests for the ~/.openadapt/desktop_ipc.json discovery file."""

    def test_load_missing_returns_none(self, tmp_path):
        """Missing discovery file → None (desktop not running)."""
        assert DesktopEndpoint.load(tmp_path / "nope.json") is None

    def test_load_valid(self, tmp_path):
        """A valid discovery file yields host/port/token."""
        f = tmp_path / "desktop_ipc.json"
        f.write_text(
            json.dumps(
                {"host": "127.0.0.1", "port": 51234, "token": "sess-abc"}
            )
        )
        ep = DesktopEndpoint.load(f)
        assert ep is not None
        assert ep.host == "127.0.0.1"
        assert ep.port == 51234
        assert ep.token == "sess-abc"

    def test_load_invalid_json_returns_none(self, tmp_path):
        """Corrupt discovery file → None, not a crash."""
        f = tmp_path / "desktop_ipc.json"
        f.write_text("{ not json")
        assert DesktopEndpoint.load(f) is None

    def test_load_without_port_returns_none(self, tmp_path):
        """A discovery file missing the port is unusable."""
        f = tmp_path / "desktop_ipc.json"
        f.write_text(json.dumps({"host": "127.0.0.1"}))
        assert DesktopEndpoint.load(f) is None

    def test_default_discovery_path(self):
        """The default discovery path is under ~/.openadapt/."""
        assert DEFAULT_DISCOVERY_PATH.name == "desktop_ipc.json"
        assert ".openadapt" in str(DEFAULT_DISCOVERY_PATH)


class TestIPCClientDiscovery:
    """Tests for building/refreshing the client from discovery."""

    def test_from_discovery_missing_returns_none(self, tmp_path):
        """No discovery file → from_discovery returns None."""
        assert IPCClient.from_discovery(tmp_path / "nope.json") is None

    def test_from_discovery_configures_client(self, tmp_path):
        """from_discovery wires host/port/token onto the client."""
        f = tmp_path / "desktop_ipc.json"
        f.write_text(json.dumps({"port": 40000, "token": "tok"}))
        client = IPCClient.from_discovery(f)
        assert client is not None
        assert client.port == 40000
        assert client.token == "tok"

    def test_refresh_from_discovery(self, tmp_path):
        """refresh_from_discovery updates an existing client in place."""
        f = tmp_path / "desktop_ipc.json"
        f.write_text(json.dumps({"port": 12345, "token": "t1"}))
        client = IPCClient()
        assert client.refresh_from_discovery(f) is True
        assert client.port == 12345
        assert client.token == "t1"

    def test_send_attaches_token(self):
        """send() attaches the client token to a tokenless message."""
        client = IPCClient(token="sess-xyz")
        sent = {}

        class FakeSock:
            def sendall(self, data):
                sent["data"] = data

        client._socket = FakeSock()
        assert client.send(IPCMessage(type=IPCMessageType.PAUSE_SYNC)) is True
        payload = json.loads(sent["data"].decode().strip())
        assert payload["token"] == "sess-xyz"
        assert payload["type"] == "pause_sync"

    def test_command_helpers_send_correct_types(self):
        """The new command helpers emit the right message types."""
        client = IPCClient(token="t")
        seen = []

        class FakeSock:
            def sendall(self, data):
                seen.append(json.loads(data.decode().strip())["type"])

        client._socket = FakeSock()
        client.send_open_workflow_library()
        client.send_open_teach("wf-1")
        client.send_pause_sync()
        client.send_resume_sync()
        assert seen == [
            "open_workflow_library",
            "open_teach",
            "pause_sync",
            "resume_sync",
        ]
