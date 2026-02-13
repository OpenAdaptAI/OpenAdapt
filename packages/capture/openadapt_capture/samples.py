"""Example recordings and sample data loading.

This module provides access to bundled example recordings that can be used
for testing, demos, and as reference implementations.

Example usage:
    >>> from openadapt_capture.samples import list_examples, load_example
    >>> print(list_examples())
    ['turn-off-nightshift']
    >>> capture = load_example('turn-off-nightshift')
    >>> for action in capture.actions():
    ...     print(f"{action.type} at ({action.x}, {action.y})")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openadapt_capture.capture import CaptureSession

logger = logging.getLogger(__name__)

# Default example - the most complete bundled recording
DEFAULT_EXAMPLE = "turn-off-nightshift"


def get_examples_dir() -> Path:
    """Return the path to the bundled examples directory.

    Returns:
        Path to examples directory (may not exist if no examples bundled)
    """
    return Path(__file__).parent.parent / "examples"


def get_external_examples_dir() -> Path:
    """Return the path to external examples (e.g., in openadapt-capture repo root).

    This looks for examples in the repository root, which is useful during
    development when examples are not bundled in the package.

    Returns:
        Path to external examples directory
    """
    # Walk up to find repo root (contains pyproject.toml)
    current = Path(__file__).parent.parent
    for _ in range(5):  # Limit search depth
        if (current / "pyproject.toml").exists():
            # Check for demo directories at repo level
            for demo_name in ["turn-off-nightshift", "demo_new", "demo_capture"]:
                demo_path = current / demo_name
                if demo_path.exists() and (demo_path / "capture.db").exists():
                    return current
        current = current.parent
    return Path()  # Return empty path if not found


def list_examples() -> list[str]:
    """List available example recording names.

    Checks both bundled examples and external examples (repo root).

    Returns:
        List of example names that can be loaded with load_example()
    """
    examples = set()

    # Check bundled examples
    bundled_dir = get_examples_dir()
    if bundled_dir.exists():
        for path in bundled_dir.iterdir():
            if path.is_dir() and (path / "capture.db").exists():
                examples.add(path.name)

    # Check external examples (repo root demos)
    external_dir = get_external_examples_dir()
    if external_dir.exists():
        for path in external_dir.iterdir():
            if path.is_dir() and (path / "capture.db").exists():
                # Skip non-demo directories
                if path.name.startswith(("demo_", "turn-off")):
                    examples.add(path.name)

    return sorted(examples)


def get_example_path(name: str) -> Path:
    """Get the path to a specific example recording.

    Args:
        name: Example name (e.g., 'turn-off-nightshift')

    Returns:
        Path to the example directory

    Raises:
        FileNotFoundError: If example not found
    """
    # Check bundled examples first
    bundled_path = get_examples_dir() / name
    if bundled_path.exists() and (bundled_path / "capture.db").exists():
        return bundled_path

    # Check external examples
    external_path = get_external_examples_dir() / name
    if external_path.exists() and (external_path / "capture.db").exists():
        return external_path

    # Not found - provide helpful error
    available = list_examples()
    if available:
        raise FileNotFoundError(
            f"Example '{name}' not found. Available examples: {available}"
        )
    else:
        raise FileNotFoundError(
            f"Example '{name}' not found. No examples are currently available. "
            "Install openadapt-capture with examples or point to a capture directory."
        )


def load_example(name: str = DEFAULT_EXAMPLE) -> "CaptureSession":
    """Load an example recording as a CaptureSession.

    Args:
        name: Example name (default: 'turn-off-nightshift')

    Returns:
        CaptureSession object for the recording

    Raises:
        FileNotFoundError: If example not found

    Example:
        >>> capture = load_example('turn-off-nightshift')
        >>> print(f"Duration: {capture.duration:.1f}s")
        Duration: 59.5s
        >>> for action in capture.actions():
        ...     print(f"{action.type}: {action.x}, {action.y}")
    """
    from openadapt_capture.capture import CaptureSession

    example_path = get_example_path(name)
    return CaptureSession.load(example_path)


def get_example_info(name: str = DEFAULT_EXAMPLE) -> dict:
    """Get metadata about an example recording without fully loading it.

    Args:
        name: Example name

    Returns:
        Dictionary with recording metadata:
        - name: Recording name
        - path: Path to recording directory
        - has_video: Whether video.mp4 exists
        - has_audio: Whether audio.flac exists
        - has_transcript: Whether transcript.json exists
        - has_screenshots: Whether screenshots/ directory exists
        - screenshot_count: Number of screenshots
    """
    example_path = get_example_path(name)

    screenshots_dir = example_path / "screenshots"
    screenshot_count = 0
    if screenshots_dir.exists():
        screenshot_count = len(list(screenshots_dir.glob("*.png")))

    return {
        "name": name,
        "path": str(example_path),
        "has_video": (example_path / "video.mp4").exists(),
        "has_audio": (example_path / "audio.flac").exists(),
        "has_transcript": (example_path / "transcript.json").exists(),
        "has_screenshots": screenshots_dir.exists() and screenshot_count > 0,
        "screenshot_count": screenshot_count,
    }


def get_example_transcript(name: str = DEFAULT_EXAMPLE) -> Optional[dict]:
    """Get the transcript for an example recording.

    Args:
        name: Example name

    Returns:
        Transcript dict with 'text' and 'segments' keys, or None if not available
    """
    import json

    example_path = get_example_path(name)
    transcript_path = example_path / "transcript.json"

    if not transcript_path.exists():
        return None

    with open(transcript_path) as f:
        return json.load(f)


def get_example_screenshots(name: str = DEFAULT_EXAMPLE) -> list[Path]:
    """Get paths to all screenshots for an example recording.

    Args:
        name: Example name

    Returns:
        List of paths to screenshot PNG files, sorted by step number
    """
    example_path = get_example_path(name)
    screenshots_dir = example_path / "screenshots"

    if not screenshots_dir.exists():
        return []

    return sorted(screenshots_dir.glob("*.png"))


def load_example_for_retrieval(name: str = DEFAULT_EXAMPLE) -> dict:
    """Load example in a format suitable for demo retrieval libraries.

    This returns a dict with fields expected by openadapt-retrieval's
    MultimodalDemoRetriever.add_demo() method.

    Args:
        name: Example name

    Returns:
        Dict with demo_id, task, screenshot, platform, app_name, domain
    """
    capture = load_example(name)
    example_path = get_example_path(name)

    # Get first screenshot
    screenshots = get_example_screenshots(name)
    first_screenshot = str(screenshots[0]) if screenshots else None

    # Try to get task description from transcript
    task = capture.task_description
    if not task:
        transcript = get_example_transcript(name)
        if transcript:
            task = transcript.get("text", f"Demo: {name}")
        else:
            task = f"Demo: {name}"

    # Infer app name from task or name
    app_name = None
    if "settings" in name.lower() or "settings" in task.lower():
        app_name = "System Settings"
    elif "nightshift" in name.lower() or "night shift" in task.lower():
        app_name = "System Settings"
    elif "calculator" in task.lower():
        app_name = "Calculator"

    return {
        "demo_id": name,
        "task": task,
        "screenshot": first_screenshot,
        "platform": capture.platform,
        "app_name": app_name,
        "domain": None,  # Desktop demos don't have domains
        "metadata": {
            "duration": capture.duration,
            "step_count": len(get_example_screenshots(name)),
            "has_audio": (example_path / "audio.flac").exists(),
        },
    }
