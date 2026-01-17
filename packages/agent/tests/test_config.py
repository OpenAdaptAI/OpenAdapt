"""Tests for configuration."""

import tempfile
from pathlib import Path
import json

import pytest

from openadapt_agent.config import (
    AgentConfig,
    SafetyMode,
    ConfirmationMode,
    ExecutionConfig,
    AuditConfig,
)


class TestAgentConfig:
    """Tests for the AgentConfig class."""

    def test_default_config(self):
        """Test default configuration values."""
        config = AgentConfig()

        assert config.safety_mode == SafetyMode.STANDARD
        assert config.confirmation_mode == ConfirmationMode.CLI
        assert config.confirmation_timeout_seconds == 60.0
        assert config.execution.max_steps == 100
        assert config.audit.enabled is True

    def test_config_from_kwargs(self):
        """Test creating config from keyword arguments."""
        config = AgentConfig(
            safety_mode=SafetyMode.STRICT,
            confirmation_mode=ConfirmationMode.GUI,
        )

        assert config.safety_mode == SafetyMode.STRICT
        assert config.confirmation_mode == ConfirmationMode.GUI

    def test_config_from_json_file(self):
        """Test loading config from JSON file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            config_data = {
                "safety_mode": "strict",
                "confirmation_mode": "gui",
                "execution": {
                    "max_steps": 50,
                    "step_timeout_seconds": 15.0,
                },
            }
            json.dump(config_data, f)
            f.flush()

            config = AgentConfig.from_file(Path(f.name))

            assert config.safety_mode == SafetyMode.STRICT
            assert config.confirmation_mode == ConfirmationMode.GUI
            assert config.execution.max_steps == 50
            assert config.execution.step_timeout_seconds == 15.0

    def test_execution_config(self):
        """Test ExecutionConfig defaults and overrides."""
        exec_config = ExecutionConfig()

        assert exec_config.max_steps == 100
        assert exec_config.step_timeout_seconds == 30.0
        assert exec_config.retry_count == 3
        assert exec_config.screenshot_on_error is True

        custom = ExecutionConfig(max_steps=50, retry_count=5)
        assert custom.max_steps == 50
        assert custom.retry_count == 5

    def test_audit_config(self):
        """Test AuditConfig defaults and overrides."""
        audit_config = AuditConfig()

        assert audit_config.enabled is True
        assert audit_config.include_screenshots is True
        assert audit_config.retention_days == 30

        custom = AuditConfig(enabled=False, retention_days=7)
        assert custom.enabled is False
        assert custom.retention_days == 7


class TestSafetyMode:
    """Tests for SafetyMode enum."""

    def test_all_modes(self):
        """Test that all safety modes are accessible."""
        modes = [
            SafetyMode.DISABLED,
            SafetyMode.PERMISSIVE,
            SafetyMode.STANDARD,
            SafetyMode.STRICT,
            SafetyMode.PARANOID,
        ]
        assert len(modes) == 5

    def test_mode_values(self):
        """Test safety mode string values."""
        assert SafetyMode.DISABLED.value == "disabled"
        assert SafetyMode.STANDARD.value == "standard"
        assert SafetyMode.PARANOID.value == "paranoid"


class TestConfirmationMode:
    """Tests for ConfirmationMode enum."""

    def test_all_modes(self):
        """Test that all confirmation modes are accessible."""
        modes = [
            ConfirmationMode.CLI,
            ConfirmationMode.GUI,
            ConfirmationMode.CALLBACK,
            ConfirmationMode.QUEUE,
            ConfirmationMode.AUTO_DENY,
            ConfirmationMode.AUTO_ALLOW,
        ]
        assert len(modes) == 6

    def test_mode_values(self):
        """Test confirmation mode string values."""
        assert ConfirmationMode.CLI.value == "cli"
        assert ConfirmationMode.GUI.value == "gui"
        assert ConfirmationMode.AUTO_DENY.value == "auto_deny"
