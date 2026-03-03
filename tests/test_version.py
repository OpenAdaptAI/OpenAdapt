"""Tests for the openadapt version module."""

import pytest

from openadapt import version


class TestVersion:
    """Test cases for version functionality."""

    def test_version_exists(self):
        """Test that version module exists and has __version__."""
        assert hasattr(version, '__version__')

    def test_version_format(self):
        """Test that __version__ follows semantic versioning format."""
        ver = version.__version__
        # Should be in format like "1.0.0" or "1.0.0-dev"
        parts = ver.split('.')
        assert len(parts) >= 2, f"Version {ver} should have at least major.minor"

        # First two parts should be integers
        assert parts[0].isdigit(), f"Major version should be numeric: {parts[0]}"
        assert parts[1].isdigit(), f"Minor version should be numeric: {parts[1]}"

    def test_version_is_string(self):
        """Test that version is a string."""
        assert isinstance(version.__version__, str)