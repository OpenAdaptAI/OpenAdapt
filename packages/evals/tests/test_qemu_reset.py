"""Tests for QEMUResetManager."""

import subprocess
from unittest.mock import MagicMock, patch

from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager


class TestResetWindows:
    """Tests for QEMUResetManager.reset_windows()."""

    def test_reset_success(self):
        """reset_windows returns True when SSH command succeeds."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            assert mgr.reset_windows() is True

        # Verify the SSH command was called with the right docker exec
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "ssh" in cmd[0]
        assert "azureuser@10.0.0.1" in cmd
        # The docker exec command should include system_reset and nc
        docker_part = cmd[-1]
        assert "system_reset" in docker_part
        assert "nc" in docker_part
        assert "7100" in docker_part

    def test_reset_ssh_failure(self):
        """reset_windows returns False when SSH command fails."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"Connection refused"
            )
            assert mgr.reset_windows() is False

    def test_reset_timeout(self):
        """reset_windows returns False when SSH command times out."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=30)
            assert mgr.reset_windows() is False

    def test_custom_container_name(self):
        """reset_windows uses the configured container name."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", container_name="my-container")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            mgr.reset_windows()

        docker_part = mock_run.call_args[0][0][-1]
        assert "my-container" in docker_part

    def test_custom_monitor_port(self):
        """reset_windows uses the configured QEMU monitor port."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", qemu_monitor_port=9999)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            mgr.reset_windows()

        docker_part = mock_run.call_args[0][0][-1]
        assert "9999" in docker_part


class TestWaitForWaaReady:
    """Tests for QEMUResetManager.wait_for_waa_ready()."""

    def test_immediate_ready(self):
        """wait_for_waa_ready returns True when server responds immediately."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", timeout_seconds=10)
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("requests.get", return_value=mock_resp):
            assert mgr.wait_for_waa_ready() is True

    def test_ready_after_retries(self):
        """wait_for_waa_ready returns True after a few failed probes."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", timeout_seconds=60)

        import requests as req_mod

        # Fail twice, then succeed
        side_effects = [
            req_mod.ConnectionError("refused"),
            req_mod.ConnectionError("refused"),
            MagicMock(ok=True),
        ]

        with patch("requests.get", side_effect=side_effects), \
             patch("time.sleep"):
            assert mgr.wait_for_waa_ready(check_interval=1) is True

    def test_timeout(self):
        """wait_for_waa_ready returns False on timeout."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", timeout_seconds=1)

        import requests as req_mod

        with patch("requests.get", side_effect=req_mod.ConnectionError("refused")), \
             patch("time.sleep"):
            assert mgr.wait_for_waa_ready(check_interval=1) is False


class TestRestartWindows:
    """Tests for QEMUResetManager.restart_windows()."""

    def test_full_restart_success(self):
        """restart_windows succeeds when reset + probe both work."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", timeout_seconds=10)

        with patch.object(mgr, "reset_windows", return_value=True), \
             patch.object(mgr, "wait_for_waa_ready", return_value=True):
            success, msg = mgr.restart_windows()

        assert success is True
        assert "ready" in msg.lower()

    def test_restart_reset_fails(self):
        """restart_windows fails when QEMU reset fails."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")

        with patch.object(mgr, "reset_windows", return_value=False):
            success, msg = mgr.restart_windows()

        assert success is False
        assert "failed" in msg.lower()

    def test_restart_wait_timeout(self):
        """restart_windows fails when WAA server does not come back."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1", timeout_seconds=5)

        with patch.object(mgr, "reset_windows", return_value=True), \
             patch.object(mgr, "wait_for_waa_ready", return_value=False):
            success, msg = mgr.restart_windows()

        assert success is False
        assert "did not come back" in msg.lower()


class TestIsQemuMonitorReachable:
    """Tests for QEMUResetManager.is_qemu_monitor_reachable()."""

    def test_reachable(self):
        """is_qemu_monitor_reachable returns True when monitor responds."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"QEMU 8.2.2 monitor", stderr=b""
            )
            assert mgr.is_qemu_monitor_reachable() is True

    def test_unreachable_no_qemu_in_output(self):
        """is_qemu_monitor_reachable returns False when output has no QEMU."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            assert mgr.is_qemu_monitor_reachable() is False

    def test_unreachable_ssh_fails(self):
        """is_qemu_monitor_reachable returns False on SSH failure."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"Connection refused"
            )
            assert mgr.is_qemu_monitor_reachable() is False

    def test_unreachable_timeout(self):
        """is_qemu_monitor_reachable returns False on timeout."""
        mgr = QEMUResetManager(vm_ip="10.0.0.1")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=15)
            assert mgr.is_qemu_monitor_reachable() is False
