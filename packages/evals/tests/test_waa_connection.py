"""Tests for WAAConnection auto-recovery module.

Tests cover instantiation and basic behavior without requiring
a real SSH connection or WAA server.
"""

from unittest.mock import patch

import pytest

from openadapt_evals.infrastructure.waa_connection import WAAConnection


class TestWAAConnectionInit:
    """Test that WAAConnection can be instantiated with various configs."""

    def test_default_params(self):
        conn = WAAConnection(vm_ip="10.0.0.1")
        assert conn.vm_ip == "10.0.0.1"
        assert conn.username == "azureuser"
        assert conn.local_port == 5001
        assert conn.remote_port == 5000
        assert conn.eval_local_port == 5050
        assert conn.eval_remote_port == 5050
        assert conn.health_check_interval == 30
        assert conn.max_retries == 5
        assert conn.retry_delay == 5

    def test_custom_params(self):
        conn = WAAConnection(
            vm_ip="192.168.1.100",
            username="ubuntu",
            local_port=6001,
            remote_port=6000,
            eval_local_port=7050,
            eval_remote_port=7050,
            health_check_interval=10,
            max_retries=3,
            retry_delay=2,
        )
        assert conn.vm_ip == "192.168.1.100"
        assert conn.username == "ubuntu"
        assert conn.local_port == 6001
        assert conn.remote_port == 6000
        assert conn.eval_local_port == 7050
        assert conn.eval_remote_port == 7050
        assert conn.health_check_interval == 10
        assert conn.max_retries == 3
        assert conn.retry_delay == 2

    def test_initial_state(self):
        conn = WAAConnection(vm_ip="10.0.0.1")
        assert conn._proc is None
        assert conn._watchdog_thread is None
        assert not conn._stop_event.is_set()


class TestSSHCommand:
    """Test SSH command construction."""

    def test_build_ssh_cmd(self):
        conn = WAAConnection(
            vm_ip="172.173.66.131",
            username="azureuser",
            local_port=5001,
            remote_port=5000,
            eval_local_port=5050,
            eval_remote_port=5050,
        )
        cmd = conn._build_ssh_cmd()
        assert cmd[0] == "ssh"
        assert "-N" in cmd
        assert "5001:localhost:5000" in cmd[cmd.index("-L") + 1]
        assert "azureuser@172.173.66.131" in cmd
        assert "ServerAliveInterval=30" in cmd
        assert "ServerAliveCountMax=3" in cmd


class TestContextManager:
    """Test context manager protocol without real connections."""

    @patch.object(WAAConnection, "start")
    @patch.object(WAAConnection, "close")
    def test_context_manager_calls_start_and_close(self, mock_close, mock_start):
        with WAAConnection(vm_ip="10.0.0.1") as conn:
            assert isinstance(conn, WAAConnection)
        mock_start.assert_called_once()
        mock_close.assert_called_once()

    @patch.object(WAAConnection, "start")
    @patch.object(WAAConnection, "close")
    def test_context_manager_closes_on_exception(self, mock_close, mock_start):
        with pytest.raises(RuntimeError):
            with WAAConnection(vm_ip="10.0.0.1"):
                raise RuntimeError("test error")
        mock_close.assert_called_once()


class TestEnsureHealthy:
    """Test ensure_healthy retry logic."""

    @patch.object(WAAConnection, "_is_healthy", return_value=True)
    def test_healthy_returns_immediately(self, mock_healthy):
        conn = WAAConnection(vm_ip="10.0.0.1", max_retries=3)
        conn.ensure_healthy()
        # Should only check once if healthy
        assert mock_healthy.call_count >= 1

    @patch.object(WAAConnection, "_is_healthy", return_value=False)
    @patch.object(WAAConnection, "_reconnect", return_value=False)
    def test_raises_after_max_retries(self, mock_reconnect, mock_healthy):
        conn = WAAConnection(vm_ip="10.0.0.1", max_retries=2, retry_delay=0)
        with pytest.raises(ConnectionError, match="not healthy after 2 retries"):
            conn.ensure_healthy()


class TestCloseIdempotent:
    """Test that close() is safe to call multiple times."""

    def test_close_without_start(self):
        conn = WAAConnection(vm_ip="10.0.0.1")
        # Should not raise
        conn.close()
        conn.close()
