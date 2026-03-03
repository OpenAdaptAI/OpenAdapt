"""Tests for the main openadapt module and its lazy import functionality."""

import pytest

import openadapt


class TestOpenAdapt:
    """Test cases for main openadapt module."""

    def test_version_attribute(self):
        """Test that openadapt has __version__ attribute."""
        assert hasattr(openadapt, '__version__')
        assert isinstance(openadapt.__version__, str)

    def test_version_matches_expected(self):
        """Test that version matches expected value."""
        # From the __init__.py file
        assert openadapt.__version__ == "1.0.6"

    def test_all_exports(self):
        """Test that __all__ contains expected exports."""
        expected_exports = [
            "__version__",
            # From capture
            "Capture",
            "CaptureSession",
            "Recorder",
            "Action",
            "EventType",
            "MouseButton",
            # From evals
            "BenchmarkAdapter",
            "BenchmarkTask",
            "ApiAgent",
            "evaluate_agent_on_benchmark",
            # From viewer
            "PageBuilder",
            "HTMLBuilder",
            # From ml
            "QwenVLAdapter",
            "Trainer",
            # From grounding (optional)
            "Grounder",
            "OmniGrounder",
            "GeminiGrounder",
            # From retrieval (optional)
            "DemoRetriever",
            "DemoLibrary",
        ]

        assert hasattr(openadapt, '__all__')
        assert all(item in openadapt.__all__ for item in expected_exports)

    def test_lazy_import_error_handling(self):
        """Test that lazy imports fail gracefully when dependencies are missing."""
        # These should raise ImportError with helpful messages since the dependencies aren't installed

        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.Capture
        assert "openadapt_capture" in str(exc_info.value)

        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.BenchmarkAdapter
        assert "openadapt_evals" in str(exc_info.value)

        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.PageBuilder
        assert "openadapt_viewer" in str(exc_info.value)

        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.QwenVLAdapter
        assert "openadapt_ml" in str(exc_info.value)

    def test_grounding_import_error_message(self):
        """Test that grounding imports show helpful error messages."""
        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.Grounder
        error_msg = str(exc_info.value)
        assert "requires openadapt-grounding" in error_msg
        assert "pip install openadapt[grounding]" in error_msg

    def test_retrieval_import_error_message(self):
        """Test that retrieval imports show helpful error messages."""
        with pytest.raises(ImportError) as exc_info:
            _ = openadapt.DemoRetriever
        error_msg = str(exc_info.value)
        assert "requires openadapt-retrieval" in error_msg
        assert "pip install openadapt[retrieval]" in error_msg

    def test_invalid_attribute_error(self):
        """Test that accessing non-existent attributes raises AttributeError."""
        with pytest.raises(AttributeError) as exc_info:
            _ = openadapt.NonExistentClass
        assert "module 'openadapt' has no attribute 'NonExistentClass'" in str(exc_info.value)

    def test_docstring(self):
        """Test that module has proper docstring."""
        assert openadapt.__doc__ is not None
        assert "OpenAdapt - GUI automation with ML" in openadapt.__doc__
        assert "pip install openadapt[" in openadapt.__doc__