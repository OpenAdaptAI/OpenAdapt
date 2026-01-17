"""OpenAdapt - GUI automation with ML.

This is the meta-package that provides unified access to all OpenAdapt packages.
Install specific extras to get the functionality you need:

    pip install openadapt[capture]     # GUI capture only
    pip install openadapt[ml]          # ML models
    pip install openadapt[evals]       # Benchmarking
    pip install openadapt[all]         # Everything
"""

__version__ = "1.0.0"

# Lazy imports to avoid pulling in heavy dependencies unless needed


def __getattr__(name: str):
    """Lazy import handler for optional packages."""
    # Capture package
    if name in (
        "Capture",
        "CaptureSession",
        "Recorder",
        "Action",
        "EventType",
        "MouseButton",
    ):
        from openadapt_capture import (  # noqa: F401
            Action,
            Capture,
            CaptureSession,
            EventType,
            MouseButton,
            Recorder,
        )

        return locals()[name]

    # Evals package
    if name in (
        "BenchmarkAdapter",
        "BenchmarkTask",
        "ApiAgent",
        "evaluate_agent_on_benchmark",
    ):
        from openadapt_evals import (  # noqa: F401
            ApiAgent,
            BenchmarkAdapter,
            BenchmarkTask,
            evaluate_agent_on_benchmark,
        )

        return locals()[name]

    # Viewer package
    if name in ("PageBuilder", "HTMLBuilder"):
        from openadapt_viewer import HTMLBuilder, PageBuilder  # noqa: F401

        return locals()[name]

    # ML package (heavy - only import if explicitly requested)
    if name in ("QwenVLAdapter", "train", "Trainer"):
        from openadapt_ml import QwenVLAdapter  # noqa: F401
        from openadapt_ml.training import Trainer  # noqa: F401

        return locals().get(name)

    # Grounding package (optional)
    if name in ("Grounder", "OmniGrounder", "GeminiGrounder"):
        try:
            from openadapt_grounding import (  # noqa: F401
                GeminiGrounder,
                Grounder,
                OmniGrounder,
            )

            return locals()[name]
        except ImportError:
            raise ImportError(
                f"{name} requires openadapt-grounding. "
                "Install with: pip install openadapt[grounding]"
            )

    # Retrieval package (optional)
    if name in ("DemoRetriever", "DemoLibrary"):
        try:
            from openadapt_retrieval import DemoLibrary, DemoRetriever  # noqa: F401

            return locals()[name]
        except ImportError:
            raise ImportError(
                f"{name} requires openadapt-retrieval. "
                "Install with: pip install openadapt[retrieval]"
            )

    raise AttributeError(f"module 'openadapt' has no attribute '{name}'")


__all__ = [
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
