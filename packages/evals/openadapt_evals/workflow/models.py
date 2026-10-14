"""Pydantic models for the workflow extraction and RAG pipeline.

All classes for the recording normalization layer, episode transcripts,
workflow extraction, canonical workflows, and the workflow library.

See docs/design/workflow_extraction_pipeline.md Section 3 for the full
design specification.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field

# ---------------------------------------------------------------------------
# 3.1 Recording Normalization Layer
# ---------------------------------------------------------------------------


class RecordingSource(str, Enum):
    """Origin of the recording."""

    NATIVE_CAPTURE = "native_capture"  # openadapt-capture SQLite DB
    WAA_VNC = "waa_vnc"  # WAA VNC screenshot pipeline
    SCREEN_RECORDING = "screen_recording"  # Video file + OCR
    IMPORTED = "imported"  # External dataset


class ActionType(str, Enum):
    """Normalized action types across all recording sources."""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    KEY = "key"
    KEY_COMBO = "key_combo"
    SCROLL = "scroll"
    WAIT = "wait"
    APP_SWITCH = "app_switch"
    UNKNOWN = "unknown"


class NormalizedAction(BaseModel):
    """A single user action, normalized across recording sources.

    This is the atomic unit of recording data. One recording session
    contains many NormalizedActions.
    """

    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float  # Seconds since recording start
    action_type: ActionType
    description: str  # Human-readable, e.g., "Click cell A1"

    # Spatial (mouse actions)
    x: int | None = None
    y: int | None = None
    end_x: int | None = None  # For drag actions
    end_y: int | None = None

    # Keyboard
    key_name: str | None = None  # e.g., "Tab", "Enter", "a"
    typed_text: str | None = None  # For type actions
    modifiers: list[str] = Field(default_factory=list)  # ["ctrl", "shift"]

    # Context
    app_name: str | None = None  # Active application
    window_title: str | None = None  # Active window title
    ui_element: str | None = None  # Target UI element description

    # Screenshots
    screenshot_before_path: str | None = None
    screenshot_after_path: str | None = None

    # Raw source data (lossless)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class RecordingSession(BaseModel):
    """A complete recording session, normalized from any source.

    This is the input to Pass 1 (transcript generation).
    """

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    source: RecordingSource
    task_description: str
    platform: str = "unknown"  # "windows", "macos", "linux"
    screen_resolution: tuple[int, int] | None = None
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    duration_seconds: float = 0.0
    actions: list[NormalizedAction] = Field(default_factory=list)

    # Source-specific metadata
    source_path: str | None = None  # Path to original recording
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def action_count(self) -> int:
        return len(self.actions)

    @computed_field
    @property
    def app_names(self) -> list[str]:
        """Unique application names used in this session."""
        return sorted(
            set(a.app_name for a in self.actions if a.app_name is not None)
        )

    @computed_field
    @property
    def content_hash(self) -> str:
        """Deterministic hash for deduplication."""
        content = (
            f"{self.task_description}|{self.action_count}"
            f"|{self.duration_seconds}"
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 3.2 Episode Transcript (Pass 1 Output)
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """A single entry in the episode transcript.

    Maps 1:1 with a NormalizedAction but adds VLM-generated
    natural language understanding of what happened.
    """

    entry_index: int
    action_id: str  # Links back to NormalizedAction
    timestamp_start: float  # Seconds since recording start
    timestamp_end: float | None = None  # End of this action's effect

    # VLM-generated fields
    narration: str  # "The user clicks the File menu in LibreOffice Calc"
    intent: str  # "Open the file management options"
    ui_element_description: str  # "File menu button in the top menu bar"
    app_context: str  # "LibreOffice Calc - Untitled Spreadsheet"
    state_change: str  # "File dropdown menu appears"

    # Classification
    action_type: ActionType
    is_corrective: bool = False  # Was this fixing a mistake?
    is_exploratory: bool = False  # Was this exploring the UI?
    is_goal_directed: bool = True  # Part of the main workflow?

    # Confidence
    vlm_confidence: float = 0.0  # 0-1 VLM self-assessed confidence

    # Screenshot references (scrubbed)
    screenshot_before_path: str | None = None
    screenshot_after_path: str | None = None


class EpisodeTranscript(BaseModel):
    """Complete VLM-generated transcript of a recording session.

    This is the output of Pass 1 and the input to Pass 2.
    """

    transcript_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:16]
    )
    session_id: str  # Links back to RecordingSession
    task_description: str
    entries: list[TranscriptEntry]

    # Generation metadata
    vlm_model: str  # e.g., "claude-sonnet-4-20250514"
    vlm_provider: str  # e.g., "anthropic"
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    generation_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Summary (VLM-generated)
    episode_summary: str = ""  # 1-3 sentence summary
    primary_goal: str = ""  # "Create a spreadsheet with annual asset changes"
    apps_used: list[str] = Field(default_factory=list)
    domain_classification: str = ""  # "spreadsheet", "document", "system_settings"

    @computed_field
    @property
    def duration_seconds(self) -> float:
        if not self.entries:
            return 0.0
        return (
            self.entries[-1].timestamp_start - self.entries[0].timestamp_start
        )

    @computed_field
    @property
    def goal_directed_count(self) -> int:
        return sum(1 for e in self.entries if e.is_goal_directed)

    @computed_field
    @property
    def corrective_count(self) -> int:
        return sum(1 for e in self.entries if e.is_corrective)


# ---------------------------------------------------------------------------
# 3.3 Workflow Extraction (Pass 2 Output)
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """A single step in an extracted workflow.

    Unlike TranscriptEntry (which maps 1:1 with raw actions),
    a WorkflowStep may merge multiple actions (e.g., "navigate to
    cell A1 and type 'Year'" collapses click + type).
    """

    step_index: int
    timestamp_start: float
    timestamp_end: float

    # Step description (for embedding and retrieval)
    description: str  # "Type the header 'Year' in cell A1"
    think: str  # "Need to label the first column"
    action: str  # "Click cell A1, type 'Year'"
    expect: str  # "Cell A1 contains 'Year'"

    # Classification
    action_type: ActionType
    is_prerequisite: bool = False  # Setup step (not core workflow)
    is_verification: bool = False  # Checking result of prior step
    is_optional: bool = False  # Could be skipped

    # Context
    app_name: str  # "LibreOffice Calc"
    ui_element: str  # "Cell A1 in the spreadsheet"
    screenshot_path: str | None = None  # Representative screenshot

    # Provenance: which transcript entries this step was derived from
    source_entry_indices: list[int] = Field(default_factory=list)

    # Parameters (for template instantiation)
    parameters: dict[str, str] = Field(default_factory=dict)
    # e.g., {"cell": "A1", "value": "Year", "sheet": "Sheet2"}


class Workflow(BaseModel):
    """A complete extracted workflow from a recording session.

    A single recording may contain multiple workflows (e.g.,
    "create sheet" + "enter data" + "format cells").
    """

    workflow_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:16]
    )
    name: str  # "Enter spreadsheet headers"
    description: str  # Detailed for embedding
    goal: str  # "Create headers Year, CA/FA/OA changes"

    # Classification
    app_names: list[str]  # ["LibreOffice Calc"]
    domain: str  # "spreadsheet"
    complexity: str = "medium"  # "simple", "medium", "complex"
    tags: list[str] = Field(default_factory=list)
    # e.g., ["data-entry", "headers", "spreadsheet"]

    # Steps
    steps: list[WorkflowStep]

    # Timing
    total_duration_seconds: float
    estimated_step_count: int | None = None  # For templates

    # Provenance
    session_id: str  # Source RecordingSession
    transcript_id: str  # Source EpisodeTranscript
    recording_source: RecordingSource

    # Embedding (populated in Pass 3)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None

    # Matching (populated in Pass 3)
    canonical_workflow_id: str | None = None

    @computed_field
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @computed_field
    @property
    def content_hash(self) -> str:
        """For deduplication: hash of name + steps."""
        step_text = "|".join(s.description for s in self.steps)
        content = f"{self.name}|{step_text}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_demo_text(self) -> str:
        """Convert to multi-level demo format for DemoController.

        Returns text compatible with _parse_multilevel_demo().
        """
        lines = [
            f"GOAL: {self.goal}",
            "",
            "PLAN:",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  {i}. {step.description}")
        lines.append("")
        lines.append("REFERENCE TRAJECTORY:")
        for i, step in enumerate(self.steps, 1):
            lines.extend(
                [
                    f"Step {i}:",
                    f"  Think: {step.think}",
                    f"  Action: {step.action}",
                    f"  Expect: {step.expect}",
                    "",
                ]
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3.4 Canonical Workflows and Library (Pass 3 Output)
# ---------------------------------------------------------------------------


class WorkflowInstance(BaseModel):
    """Reference to one instance of a workflow in a canonical group."""

    workflow_id: str
    session_id: str
    similarity_score: float  # Cosine similarity to canonical centroid
    step_count: int
    duration_seconds: float


class CanonicalWorkflow(BaseModel):
    """A canonical workflow merged from one or more instances.

    This is the retrievable unit. When an agent gets a new task,
    we retrieve CanonicalWorkflows and inject their steps.
    """

    canonical_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:16]
    )
    name: str  # "Calculate annual asset changes in spreadsheet"
    description: str  # Detailed merged description
    goal: str  # Canonical goal statement

    # Classification
    app_names: list[str]
    domain: str
    complexity: str = "medium"
    tags: list[str] = Field(default_factory=list)

    # Canonical steps (merged from all instances)
    steps: list[WorkflowStep]

    # Instance metadata
    instance_count: int  # How many recordings contributed
    instances: list[WorkflowInstance] = Field(default_factory=list)

    # Embedding (centroid of matched workflows)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None

    # Quality metrics
    avg_similarity: float = 0.0  # Average similarity of instances
    min_similarity: float = 0.0  # Lowest similarity instance
    confidence: float = 0.0  # Higher with more instances

    # Versioning
    version: int = 1
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Corrections applied
    correction_count: int = 0
    last_correction_at: datetime | None = None

    def to_demo_text(self) -> str:
        """Convert to multi-level demo format for DemoController."""
        lines = [
            f"GOAL: {self.goal}",
            "",
            "PLAN:",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  {i}. {step.description}")
        lines.append("")
        lines.append("REFERENCE TRAJECTORY:")
        for i, step in enumerate(self.steps, 1):
            lines.extend(
                [
                    f"Step {i}:",
                    f"  Think: {step.think}",
                    f"  Action: {step.action}",
                    f"  Expect: {step.expect}",
                    "",
                ]
            )
        lines.append(
            "NOTE: Adapt steps as needed if the UI state differs from "
            "expectations. This is a reference, not a rigid script."
        )
        return "\n".join(lines)


class WorkflowLibrary(BaseModel):
    """The full workflow knowledge base.

    Persisted as JSON + FAISS index. Loaded at agent startup.
    """

    library_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:16]
    )
    version: int = 1
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Content
    canonical_workflows: list[CanonicalWorkflow] = Field(
        default_factory=list
    )
    raw_workflows: list[Workflow] = Field(default_factory=list)

    # Index metadata
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 3072
    index_path: str | None = None  # Path to FAISS index

    # Statistics
    total_recordings_processed: int = 0
    total_workflows_extracted: int = 0

    @computed_field
    @property
    def canonical_count(self) -> int:
        return len(self.canonical_workflows)

    @computed_field
    @property
    def domains(self) -> list[str]:
        return sorted(set(w.domain for w in self.canonical_workflows))

    @computed_field
    @property
    def app_coverage(self) -> list[str]:
        apps: set[str] = set()
        for w in self.canonical_workflows:
            apps.update(w.app_names)
        return sorted(apps)
