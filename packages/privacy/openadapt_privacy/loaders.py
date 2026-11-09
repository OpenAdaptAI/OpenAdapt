"""Data loader interfaces for scrubbing pipelines.

This module defines abstract interfaces for loading GUI automation data
(actions, screenshots, recordings) that can be scrubbed. Implementations
are provided by downstream packages like openadapt-record.

Example usage with a custom loader:

    from openadapt_privacy.loaders import RecordingLoader, Recording, Action
    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

    class MyRecordingLoader(RecordingLoader):
        def load(self, path: str) -> Recording:
            # Load your recording format
            ...

        def save(self, recording: Recording, path: str) -> None:
            # Save scrubbed recording
            ...

    # Usage
    loader = MyRecordingLoader()
    scrubber = PresidioScrubbingProvider()

    recording = loader.load("recording.json")
    scrubbed = recording.scrub(scrubber)
    loader.save(scrubbed, "recording_scrubbed.json")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional

from PIL import Image

from openadapt_privacy.base import ScrubbingProvider
from openadapt_privacy.config import privacy_operation


class UnscrubbedScreenshot(RuntimeError):
    """Image redaction was requested but the pixels were never scrubbed.

    Raised instead of returning a Screenshot that looks scrubbed while still
    pointing at unredacted image bytes on disk.
    """


@dataclass
class Action:
    """Represents a single user action in a recording.

    Attributes:
        id: Unique identifier for the action.
        action_type: Type of action (click, type, scroll, etc.).
        timestamp: Unix timestamp when action occurred.
        text: Text content associated with the action.
        canonical_text: Normalized text (e.g., typed characters joined).
        window_title: Title of the window where action occurred.
        element_text: Text of the UI element interacted with.
        metadata: Additional action-specific data.
        screenshot_id: ID of associated screenshot, if any.
    """

    id: int
    action_type: str
    timestamp: float
    text: Optional[str] = None
    canonical_text: Optional[str] = None
    window_title: Optional[str] = None
    element_text: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    screenshot_id: Optional[int] = None

    def scrub(self, scrubber: ScrubbingProvider) -> "Action":
        """Return a new Action with PII/PHI scrubbed.

        Args:
            scrubber: The scrubbing provider to use.

        Returns:
            New Action instance with scrubbed text fields.
        """
        return Action(
            id=self.id,
            action_type=self.action_type,
            timestamp=self.timestamp,
            text=scrubber.scrub_text(self.text, is_separated=True) if self.text else None,
            canonical_text=(
                scrubber.scrub_text(self.canonical_text, is_separated=True)
                if self.canonical_text
                else None
            ),
            window_title=scrubber.scrub_text(self.window_title) if self.window_title else None,
            element_text=scrubber.scrub_text(self.element_text) if self.element_text else None,
            metadata=scrubber.scrub_dict(self.metadata) if self.metadata else {},
            screenshot_id=self.screenshot_id,
        )


@dataclass
class Screenshot:
    """Represents a screenshot from a recording.

    Attributes:
        id: Unique identifier for the screenshot.
        action_id: ID of the associated action.
        timestamp: Unix timestamp when screenshot was taken.
        image: PIL Image object (lazy-loaded in implementations).
        path: Path to the screenshot file.
    """

    id: int
    action_id: int
    timestamp: float
    image: Optional[Image.Image] = None
    path: Optional[str] = None

    def scrub(self, scrubber: ScrubbingProvider) -> "Screenshot":
        """Return a new Screenshot with PII/PHI scrubbed from the image.

        `path` is dropped on the returned Screenshot because the file it names
        holds the original, unredacted bytes. Only `image` carries scrubbed
        pixels; a scrubbed Screenshot must not name an unscrubbed file.

        Args:
            scrubber: The scrubbing provider to use.

        Returns:
            New Screenshot instance with scrubbed image.

        Raises:
            UnscrubbedScreenshot: If this Screenshot names image data on disk
                that was never loaded. Nothing could be redacted, and returning
                a Screenshot that still names the original file would report an
                unscrubbed image as scrubbed.
        """
        if self.image is None:
            if self.path is not None:
                raise UnscrubbedScreenshot(
                    f"Screenshot {self.id} names image data at {self.path!r} that was not "
                    "loaded, so no redaction was attempted. Load Screenshot.image before "
                    "scrubbing, or pass scrub_images=False to state explicitly that image "
                    "redaction is out of scope."
                )
            # No pixels and no file: there is genuinely nothing to redact.
            return Screenshot(
                id=self.id,
                action_id=self.action_id,
                timestamp=self.timestamp,
                image=None,
                path=None,
            )

        return Screenshot(
            id=self.id,
            action_id=self.action_id,
            timestamp=self.timestamp,
            image=scrubber.scrub_image(self.image),
            path=None,
        )


@dataclass
class Recording:
    """Represents a complete GUI automation recording.

    Attributes:
        id: Unique identifier for the recording.
        task_description: Description of the task being recorded.
        timestamp: Unix timestamp when recording started.
        actions: List of actions in the recording.
        screenshots: List of screenshots in the recording.
        metadata: Additional recording-specific data.
    """

    id: Optional[int] = None
    task_description: Optional[str] = None
    timestamp: Optional[float] = None
    actions: List[Action] = field(default_factory=list)
    screenshots: List[Screenshot] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def scrub(
        self,
        scrubber: ScrubbingProvider,
        scrub_images: bool = True,
    ) -> "Recording":
        """Return a new Recording with all PII/PHI scrubbed.

        Args:
            scrubber: The scrubbing provider to use.
            scrub_images: Whether to scrub screenshots (can be slow).

        Returns:
            New Recording instance with all content scrubbed.
        """
        with privacy_operation() as policy:
            policy_sha256 = policy.policy_digest()
            required_modalities = ["TEXT"]
            if scrub_images and self.screenshots:
                required_modalities.append("PIL_IMAGE")
            scrubber.validate_ready(required_modalities)

            scrubbed_task_description = (
                scrubber.scrub_text(self.task_description) if self.task_description else None
            )
            scrubbed_actions = [action.scrub(scrubber) for action in self.actions]
            scrubbed_screenshots = (
                [screenshot.scrub(scrubber) for screenshot in self.screenshots]
                if scrub_images
                else [
                    Screenshot(
                        id=screenshot.id,
                        action_id=screenshot.action_id,
                        timestamp=screenshot.timestamp,
                    )
                    for screenshot in self.screenshots
                ]
            )

            scrubbed_metadata = scrubber.scrub_dict(self.metadata) if self.metadata else {}
            scrubbed_metadata["_openadapt_privacy"] = {
                **scrubber.evidence(required_modalities, policy_sha256=policy_sha256),
                "omitted_modalities": [] if scrub_images else ["PIL_IMAGE"],
            }

        return Recording(
            id=self.id,
            task_description=scrubbed_task_description,
            timestamp=self.timestamp,
            actions=scrubbed_actions,
            screenshots=scrubbed_screenshots,
            metadata=scrubbed_metadata,
        )

    def iter_actions_with_screenshots(self) -> Iterator[tuple[Action, Optional[Screenshot]]]:
        """Iterate over actions paired with their screenshots.

        Yields:
            Tuples of (action, screenshot) where screenshot may be None.
        """
        screenshot_map = {s.action_id: s for s in self.screenshots}
        for action in self.actions:
            yield action, screenshot_map.get(action.id)


class RecordingLoader(ABC):
    """Abstract base class for loading and saving recordings.

    Implementations handle specific storage formats (JSON, SQLite, etc.).
    """

    @abstractmethod
    def load(self, source: str) -> Recording:
        """Load a recording from the given source.

        Args:
            source: Path or identifier for the recording.

        Returns:
            Loaded Recording instance.
        """
        raise NotImplementedError

    @abstractmethod
    def save(self, recording: Recording, destination: str) -> None:
        """Save a recording to the given destination.

        Args:
            recording: The Recording to save.
            destination: Path or identifier for the output.
        """
        raise NotImplementedError

    def load_and_scrub(
        self,
        source: str,
        scrubber: ScrubbingProvider,
        scrub_images: bool = True,
    ) -> Recording:
        """Load a recording and scrub it in one step.

        Args:
            source: Path or identifier for the recording.
            scrubber: The scrubbing provider to use.
            scrub_images: Whether to scrub screenshots.

        Returns:
            Scrubbed Recording instance.
        """
        recording = self.load(source)
        return recording.scrub(scrubber, scrub_images=scrub_images)


class DictRecordingLoader(RecordingLoader):
    """Simple loader that works with dict-based recordings.

    This is a reference implementation that can load/save recordings
    as JSON-serializable dictionaries. Useful for testing and as a
    template for custom implementations.
    """

    def load(self, source: str) -> Recording:
        """Load a recording from a JSON file.

        Args:
            source: Path to the JSON file.

        Returns:
            Loaded Recording instance.
        """
        import json

        with open(source, "r") as f:
            data = json.load(f)

        return self._dict_to_recording(data)

    def save(self, recording: Recording, destination: str) -> None:
        """Save a recording to a JSON file.

        Args:
            recording: The Recording to save.
            destination: Path for the output JSON file.
        """
        import json

        data = self._recording_to_dict(recording)
        with open(destination, "w") as f:
            json.dump(data, f, indent=2)

    def load_from_dict(self, data: dict) -> Recording:
        """Load a recording from a dictionary.

        Args:
            data: Dictionary representation of a recording.

        Returns:
            Recording instance.
        """
        return self._dict_to_recording(data)

    def to_dict(self, recording: Recording) -> dict:
        """Convert a recording to a dictionary.

        Args:
            recording: The Recording to convert.

        Returns:
            Dictionary representation.
        """
        return self._recording_to_dict(recording)

    def _dict_to_recording(self, data: dict) -> Recording:
        """Convert dict to Recording."""
        actions = [
            Action(
                id=a.get("id", i),
                action_type=a.get("action_type", "unknown"),
                timestamp=a.get("timestamp", 0),
                text=a.get("text"),
                canonical_text=a.get("canonical_text"),
                window_title=a.get("window_title"),
                element_text=a.get("element_text"),
                metadata=a.get("metadata", {}),
                screenshot_id=a.get("screenshot_id"),
            )
            for i, a in enumerate(data.get("actions", []))
        ]

        screenshots = [
            Screenshot(
                id=s.get("id", i),
                action_id=s.get("action_id", i),
                timestamp=s.get("timestamp", 0),
                path=s.get("path"),
            )
            for i, s in enumerate(data.get("screenshots", []))
        ]

        return Recording(
            id=data.get("id"),
            task_description=data.get("task_description"),
            timestamp=data.get("timestamp"),
            actions=actions,
            screenshots=screenshots,
            metadata=data.get("metadata", {}),
        )

    def _recording_to_dict(self, recording: Recording) -> dict:
        """Convert Recording to dict."""
        return {
            "id": recording.id,
            "task_description": recording.task_description,
            "timestamp": recording.timestamp,
            "actions": [
                {
                    "id": a.id,
                    "action_type": a.action_type,
                    "timestamp": a.timestamp,
                    "text": a.text,
                    "canonical_text": a.canonical_text,
                    "window_title": a.window_title,
                    "element_text": a.element_text,
                    "metadata": a.metadata,
                    "screenshot_id": a.screenshot_id,
                }
                for a in recording.actions
            ],
            "screenshots": [
                {
                    "id": s.id,
                    "action_id": s.action_id,
                    "timestamp": s.timestamp,
                    "path": s.path,
                }
                for s in recording.screenshots
            ],
            "metadata": recording.metadata,
        }
