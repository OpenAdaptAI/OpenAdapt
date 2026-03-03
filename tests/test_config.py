"""Tests for the openadapt config module."""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from openadapt.config import OpenAdaptSettings, reload_settings, settings


class TestConfig:
    """Test cases for config functionality."""

    def test_settings_instance_exists(self):
        """Test that global settings instance exists."""
        assert settings is not None
        assert isinstance(settings, OpenAdaptSettings)

    def test_default_values(self):
        """Test that default values are set correctly."""
        assert settings.default_model == "Qwen/Qwen3-VL-2B-Instruct"
        assert settings.default_device == "auto"
        assert settings.default_batch_size == 1
        assert settings.eval_max_steps == 15
        assert settings.server_port == 8080
        assert settings.capture_audio is True
        assert settings.capture_transcribe is False

    def test_path_types(self):
        """Test that path settings return Path objects."""
        assert isinstance(settings.capture_dir, Path)
        assert isinstance(settings.training_output_dir, Path)
        assert isinstance(settings.benchmark_results_dir, Path)
        assert isinstance(settings.model_cache_dir, Path)
        assert isinstance(settings.embedding_cache_dir, Path)

    def test_default_paths(self):
        """Test that default paths are under home directory."""
        home = Path.home()
        assert str(settings.capture_dir).startswith(str(home))
        assert str(settings.training_output_dir).startswith(str(home))
        assert str(settings.benchmark_results_dir).startswith(str(home))
        assert str(settings.model_cache_dir).startswith(str(home))

    def test_ensure_directories(self):
        """Test that ensure_directories creates directories."""
        # Use a temporary directory for testing
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            test_settings = OpenAdaptSettings(
                capture_dir=temp_path / "captures",
                training_output_dir=temp_path / "training",
                benchmark_results_dir=temp_path / "benchmarks",
                model_cache_dir=temp_path / "models",
                embedding_cache_dir=temp_path / "embeddings",
            )

            # Directories shouldn't exist yet
            assert not (temp_path / "captures").exists()
            assert not (temp_path / "training").exists()

            # Create directories
            test_settings.ensure_directories()

            # All directories should now exist
            assert (temp_path / "captures").exists()
            assert (temp_path / "training").exists()
            assert (temp_path / "benchmarks").exists()
            assert (temp_path / "models").exists()
            assert (temp_path / "embeddings").exists()

    def test_get_device_cpu_fallback(self):
        """Test that get_device returns cpu when torch is not available."""
        test_settings = OpenAdaptSettings(default_device="auto")

        # Mock torch import to fail
        with mock.patch.dict('sys.modules', {'torch': None}):
            with mock.patch('builtins.__import__', side_effect=ImportError):
                device = test_settings.get_device()
                assert device == "cpu"

    def test_get_device_manual_override(self):
        """Test that get_device respects manual device setting."""
        test_settings = OpenAdaptSettings(default_device="cuda")
        device = test_settings.get_device()
        assert device == "cuda"

    def test_env_prefix(self):
        """Test that environment variables with OPENADAPT_ prefix are loaded."""
        with mock.patch.dict(os.environ, {'OPENADAPT_SERVER_PORT': '9090'}):
            test_settings = OpenAdaptSettings()
            assert test_settings.server_port == 9090

    def test_api_key_settings(self):
        """Test that API key settings exist."""
        # These should be None by default
        assert settings.anthropic_api_key is None
        assert settings.openai_api_key is None
        assert settings.google_api_key is None
        assert settings.lambda_api_key is None

    def test_reload_settings(self):
        """Test that reload_settings creates a new instance."""
        original_settings = settings
        reloaded = reload_settings()

        assert isinstance(reloaded, OpenAdaptSettings)
        # Should have same values
        assert reloaded.default_model == original_settings.default_model
        assert reloaded.server_port == original_settings.server_port

    def test_azure_settings(self):
        """Test that Azure settings exist and default to None."""
        assert settings.azure_subscription_id is None
        assert settings.azure_ml_resource_group is None
        assert settings.azure_ml_workspace_name is None
        assert settings.azure_docker_image is None

    def test_grounding_settings(self):
        """Test that grounding settings exist."""
        assert settings.omniparser_url is None
        assert settings.uitars_url is None

    def test_retrieval_settings(self):
        """Test that retrieval settings have sensible defaults."""
        assert settings.retrieval_embedding_dim == 512
        assert settings.retrieval_top_k == 5