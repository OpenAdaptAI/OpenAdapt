"""Tests for VM IP auto-detection (resolve_vm_ip)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.infrastructure.vm_ip import (
    _ip_from_azure_query,
    _ip_from_pool_registry,
    resolve_vm_ip,
)


class TestResolveVmIp:
    """Tests for resolve_vm_ip() resolution order."""

    def test_explicit_ip_returned_immediately(self):
        """Explicit IP is returned without checking registry or Azure."""
        assert resolve_vm_ip(explicit_ip="1.2.3.4") == "1.2.3.4"

    def test_pool_registry_used_when_no_explicit_ip(self):
        """Pool registry IP is used when no explicit IP is given."""
        with patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_pool_registry",
            return_value="10.0.0.5",
        ):
            assert resolve_vm_ip() == "10.0.0.5"

    def test_azure_fallback_when_no_registry(self):
        """Azure query is used when pool registry doesn't exist."""
        with patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_pool_registry",
            return_value=None,
        ), patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_azure_query",
            return_value="10.0.0.99",
        ):
            assert resolve_vm_ip() == "10.0.0.99"

    def test_error_when_no_vm_found(self):
        """RuntimeError raised when no VM can be found by any method."""
        with patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_pool_registry",
            return_value=None,
        ), patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_azure_query",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="No running VM found"):
                resolve_vm_ip()

    def test_explicit_ip_skips_all_lookups(self):
        """When explicit IP is given, registry and Azure are never called."""
        with patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_pool_registry",
        ) as mock_reg, patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_azure_query",
        ) as mock_az:
            result = resolve_vm_ip(explicit_ip="5.5.5.5")
            assert result == "5.5.5.5"
            mock_reg.assert_not_called()
            mock_az.assert_not_called()

    def test_registry_checked_before_azure(self):
        """Pool registry is checked before Azure query (fast path first)."""
        call_order = []

        def mock_registry():
            call_order.append("registry")
            return "10.0.0.1"

        def mock_azure():
            call_order.append("azure")
            return "10.0.0.2"

        with patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_pool_registry",
            side_effect=mock_registry,
        ), patch(
            "openadapt_evals.infrastructure.vm_ip._ip_from_azure_query",
            side_effect=mock_azure,
        ):
            result = resolve_vm_ip()
            assert result == "10.0.0.1"
            assert call_order == ["registry"]  # Azure never called


class TestIpFromPoolRegistry:
    """Tests for _ip_from_pool_registry()."""

    def test_reads_first_active_worker(self, tmp_path, monkeypatch):
        """Returns IP of first non-deleted/non-failed worker."""
        registry = tmp_path / "benchmark_results" / "vm_pool_registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "workers": [
                {"name": "waa-pool-00", "ip": "10.0.0.1", "status": "ready"},
                {"name": "waa-pool-01", "ip": "10.0.0.2", "status": "ready"},
            ]
        }))
        monkeypatch.chdir(tmp_path)
        assert _ip_from_pool_registry() == "10.0.0.1"

    def test_skips_deleted_workers(self, tmp_path, monkeypatch):
        """Skips workers with 'deleted' status."""
        registry = tmp_path / "benchmark_results" / "vm_pool_registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "workers": [
                {"name": "waa-pool-00", "ip": "10.0.0.1", "status": "deleted"},
                {"name": "waa-pool-01", "ip": "10.0.0.2", "status": "ready"},
            ]
        }))
        monkeypatch.chdir(tmp_path)
        assert _ip_from_pool_registry() == "10.0.0.2"

    def test_skips_failed_workers(self, tmp_path, monkeypatch):
        """Skips workers with 'failed' status."""
        registry = tmp_path / "benchmark_results" / "vm_pool_registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "workers": [
                {"name": "waa-pool-00", "ip": "10.0.0.1", "status": "failed"},
            ]
        }))
        monkeypatch.chdir(tmp_path)
        assert _ip_from_pool_registry() is None

    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        """Returns None when registry file doesn't exist."""
        monkeypatch.chdir(tmp_path)
        assert _ip_from_pool_registry() is None

    def test_returns_none_on_malformed_json(self, tmp_path, monkeypatch):
        """Returns None when registry file contains invalid JSON."""
        registry = tmp_path / "benchmark_results" / "vm_pool_registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("not json")
        monkeypatch.chdir(tmp_path)
        assert _ip_from_pool_registry() is None


class TestIpFromAzureQuery:
    """Tests for _ip_from_azure_query()."""

    def test_returns_pool_vm_ip(self):
        """Returns IP from waa-pool-00 query."""
        mock_mgr = MagicMock()
        mock_mgr.get_vm_ip.return_value = "10.0.0.50"

        with patch(
            "openadapt_evals.infrastructure.azure_vm.AzureVMManager",
            return_value=mock_mgr,
        ):
            assert _ip_from_azure_query() == "10.0.0.50"
            mock_mgr.get_vm_ip.assert_called_once_with("waa-pool-00")

    def test_falls_back_to_legacy_name(self):
        """Falls back to waa-eval-vm when waa-pool-00 returns None."""
        mock_mgr = MagicMock()
        mock_mgr.get_vm_ip.side_effect = [None, "10.0.0.99"]

        with patch(
            "openadapt_evals.infrastructure.azure_vm.AzureVMManager",
            return_value=mock_mgr,
        ):
            assert _ip_from_azure_query() == "10.0.0.99"
            calls = mock_mgr.get_vm_ip.call_args_list
            assert calls[0][0] == ("waa-pool-00",)
            assert calls[1][0] == ("waa-eval-vm",)

    def test_returns_none_on_azure_error(self):
        """Returns None when Azure SDK/CLI is unavailable."""
        with patch(
            "openadapt_evals.infrastructure.azure_vm.AzureVMManager",
            side_effect=ImportError("azure not installed"),
        ):
            assert _ip_from_azure_query() is None
