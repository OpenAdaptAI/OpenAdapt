# Workflow Extraction and RAG Pipeline for Desktop Automation

> **Date**: 2026-03-18
> **Status**: DESIGN (comprehensive, ready for implementation)
> **Location**: `openadapt_evals/workflow/` (to be created)

---

## 1. Problem Statement

OpenAdapt records desktop sessions (screenshots, mouse/keyboard actions, timestamps) from two
sources: native desktop capture (`openadapt-capture`) and WAA VNC recordings on Azure VMs.
Currently these are raw action sequences with no higher-level structure. We need to:

1. Extract structured **workflows** from recordings
2. Cluster similar workflows across sessions into **canonical workflows**
3. Use canonical workflows for **retrieval-augmented agent execution**

This pipeline is the backbone of OpenAdapt's thesis: *trajectory-conditioned disambiguation*.
Accumulated workflows are the defensible moat -- not better reasoning.

---

## 2. Architecture Diagram

```
                          RECORDING PHASE
                          ===============

  openadapt-capture                    WAA VNC Recording
  (native desktop)                     (Azure VM)
  +-----------------+                  +-----------------+
  | Recording DB    |                  | meta.json       |
  | - ActionEvent   |                  | step_XX_before  |
  | - Screenshot    |                  | step_XX_after   |
  | - WindowEvent   |                  | .png files      |
  | - AudioInfo     |                  +---------+-------+
  +--------+--------+                            |
           |                                     |
           v                                     v
  +--------+-------------------------------------+--------+
  |              UNIFIED RECORDING ADAPTER                 |
  |  Normalizes both sources into RecordingSession         |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              PASS 0: PII SCRUBBING                     |
  |  openadapt-privacy (Presidio)                          |
  |  - scrub_image() on all screenshots                    |
  |  - scrub_text() on action descriptions                 |
  |  - scrub_dict() on metadata                            |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              PASS 1: EPISODE TRANSCRIPT                |
  |  VLM (Claude/GPT) + screenshots + actions              |
  |  Output: EpisodeTranscript                             |
  |  - Timestamped NL descriptions per action              |
  |  - App context, UI elements, user intent               |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              PASS 2: WORKFLOW EXTRACTION                |
  |  VLM (Claude/GPT) + transcript + screenshots           |
  |  Output: list[Workflow]                                |
  |  - Structured WorkflowStep objects                     |
  |  - Segmentation boundaries (where workflows start/end) |
  |  - Goal/intent classification                          |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              PASS 3: EMBEDDING + SIMILARITY MATCHING    |
  |  text-embedding-3-large (3072 dim)                     |
  |  - Embed each workflow description                      |
  |  - Cosine similarity > 0.85 = same canonical workflow  |
  |  - No match = new canonical workflow                   |
  |  - Canonical = LLM-merged template from instances      |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              WORKFLOW LIBRARY (FAISS index)             |
  |  openadapt-retrieval VectorIndex                       |
  |  - Persisted: embeddings.npy + index.json + faiss.index|
  |  - Metadata: CanonicalWorkflow JSON                    |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              RETRIEVAL FOR AGENT EXECUTION              |
  |  Task instruction -> embed -> nearest canonical workflow|
  |  -> inject as DemoController plan                      |
  |  -> RetrievalAugmentedAgent selects + executes         |
  +--------+----------------------------------------------+
           |
           v
  +--------+----------------------------------------------+
  |              CORRECTION FLYWHEEL                        |
  |  CorrectionStore captures failures                     |
  |  Corrections refine canonical workflows                |
  |  Re-matched periodically                               |
  +------------------------------------------------------+
```

---

## 3. Pydantic Class Definitions

All classes live in `openadapt_evals/workflow/models.py`. These are complete,
production-ready definitions.

### 3.1 Recording Normalization Layer

```python
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field


class RecordingSource(str, Enum):
    """Origin of the recording."""
    NATIVE_CAPTURE = "native_capture"     # openadapt-capture SQLite DB
    WAA_VNC = "waa_vnc"                   # WAA VNC screenshot pipeline
    SCREEN_RECORDING = "screen_recording"  # Video file + OCR
    IMPORTED = "imported"                  # External dataset


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
    timestamp: float                    # Seconds since recording start
    action_type: ActionType
    description: str                    # Human-readable, e.g., "Click cell A1"

    # Spatial (mouse actions)
    x: int | None = None
    y: int | None = None
    end_x: int | None = None           # For drag actions
    end_y: int | None = None

    # Keyboard
    key_name: str | None = None        # e.g., "Tab", "Enter", "a"
    typed_text: str | None = None      # For type actions
    modifiers: list[str] = Field(default_factory=list)  # ["ctrl", "shift"]

    # Context
    app_name: str | None = None        # Active application
    window_title: str | None = None    # Active window title
    ui_element: str | None = None      # Target UI element description

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
    platform: str = "unknown"            # "windows", "macos", "linux"
    screen_resolution: tuple[int, int] | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float = 0.0
    actions: list[NormalizedAction] = Field(default_factory=list)

    # Source-specific metadata
    source_path: str | None = None       # Path to original recording
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def action_count(self) -> int:
        return len(self.actions)

    @computed_field
    @property
    def app_names(self) -> list[str]:
        """Unique application names used in this session."""
        return sorted(set(
            a.app_name for a in self.actions
            if a.app_name is not None
        ))

    @computed_field
    @property
    def content_hash(self) -> str:
        """Deterministic hash for deduplication."""
        content = f"{self.task_description}|{self.action_count}|{self.duration_seconds}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
```

### 3.2 Episode Transcript (Pass 1 Output)

```python
class TranscriptEntry(BaseModel):
    """A single entry in the episode transcript.

    Maps 1:1 with a NormalizedAction but adds VLM-generated
    natural language understanding of what happened.
    """
    entry_index: int
    action_id: str                       # Links back to NormalizedAction
    timestamp_start: float               # Seconds since recording start
    timestamp_end: float | None = None   # End of this action's effect

    # VLM-generated fields
    narration: str                       # "The user clicks the File menu in LibreOffice Calc"
    intent: str                          # "Open the file management options"
    ui_element_description: str          # "File menu button in the top menu bar"
    app_context: str                     # "LibreOffice Calc - Untitled Spreadsheet"
    state_change: str                    # "File dropdown menu appears"

    # Classification
    action_type: ActionType
    is_corrective: bool = False          # Was this fixing a mistake?
    is_exploratory: bool = False         # Was this exploring the UI?
    is_goal_directed: bool = True        # Part of the main workflow?

    # Confidence
    vlm_confidence: float = 0.0          # 0-1 VLM self-assessed confidence

    # Screenshot references (scrubbed)
    screenshot_before_path: str | None = None
    screenshot_after_path: str | None = None


class EpisodeTranscript(BaseModel):
    """Complete VLM-generated transcript of a recording session.

    This is the output of Pass 1 and the input to Pass 2.
    """
    transcript_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str                      # Links back to RecordingSession
    task_description: str
    entries: list[TranscriptEntry]

    # Generation metadata
    vlm_model: str                       # e.g., "claude-sonnet-4-20250514"
    vlm_provider: str                    # e.g., "anthropic"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    generation_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Summary (VLM-generated)
    episode_summary: str = ""            # 1-3 sentence summary
    primary_goal: str = ""               # "Create a spreadsheet with annual asset changes"
    apps_used: list[str] = Field(default_factory=list)
    domain_classification: str = ""      # "spreadsheet", "document", "system_settings"

    @computed_field
    @property
    def duration_seconds(self) -> float:
        if not self.entries:
            return 0.0
        return self.entries[-1].timestamp_start - self.entries[0].timestamp_start

    @computed_field
    @property
    def goal_directed_count(self) -> int:
        return sum(1 for e in self.entries if e.is_goal_directed)

    @computed_field
    @property
    def corrective_count(self) -> int:
        return sum(1 for e in self.entries if e.is_corrective)
```

### 3.3 Workflow Extraction (Pass 2 Output)

```python
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
    description: str                     # "Type the header 'Year' in cell A1"
    think: str                           # "Need to label the first column"
    action: str                          # "Click cell A1, type 'Year'"
    expect: str                          # "Cell A1 contains 'Year'"

    # Classification
    action_type: ActionType
    is_prerequisite: bool = False        # Setup step (not core workflow)
    is_verification: bool = False        # Checking result of prior step
    is_optional: bool = False            # Could be skipped

    # Context
    app_name: str                        # "LibreOffice Calc"
    ui_element: str                      # "Cell A1 in the spreadsheet"
    screenshot_path: str | None = None   # Representative screenshot

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
    workflow_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str                            # "Enter spreadsheet headers"
    description: str                     # Detailed for embedding
    goal: str                            # "Create headers Year, CA/FA/OA changes"

    # Classification
    app_names: list[str]                 # ["LibreOffice Calc"]
    domain: str                          # "spreadsheet"
    complexity: str = "medium"           # "simple", "medium", "complex"
    tags: list[str] = Field(default_factory=list)
    # e.g., ["data-entry", "headers", "spreadsheet"]

    # Steps
    steps: list[WorkflowStep]

    # Timing
    total_duration_seconds: float
    estimated_step_count: int | None = None  # For templates: how many steps expected

    # Provenance
    session_id: str                      # Source RecordingSession
    transcript_id: str                   # Source EpisodeTranscript
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
            lines.extend([
                f"Step {i}:",
                f"  Think: {step.think}",
                f"  Action: {step.action}",
                f"  Expect: {step.expect}",
                "",
            ])
        return "\n".join(lines)
```

### 3.4 Canonical Workflows and Library (Pass 3 Output)

```python
class WorkflowInstance(BaseModel):
    """Reference to one instance of a workflow in a canonical group."""
    workflow_id: str
    session_id: str
    similarity_score: float              # Cosine similarity to canonical centroid
    step_count: int
    duration_seconds: float


class CanonicalWorkflow(BaseModel):
    """A canonical workflow merged from one or more instances.

    This is the retrievable unit. When an agent gets a new task,
    we retrieve CanonicalWorkflows and inject their steps.
    """
    canonical_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str                            # "Calculate annual asset changes in spreadsheet"
    description: str                     # Detailed merged description
    goal: str                            # Canonical goal statement

    # Classification
    app_names: list[str]
    domain: str
    complexity: str = "medium"
    tags: list[str] = Field(default_factory=list)

    # Canonical steps (merged from all instances)
    steps: list[WorkflowStep]

    # Instance metadata
    instance_count: int                  # How many recordings contributed
    instances: list[WorkflowInstance] = Field(default_factory=list)

    # Embedding (centroid of matched workflows)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None

    # Quality metrics
    avg_similarity: float = 0.0          # Average similarity of instances
    min_similarity: float = 0.0          # Lowest similarity instance
    confidence: float = 0.0              # Higher with more instances

    # Versioning
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
            lines.extend([
                f"Step {i}:",
                f"  Think: {step.think}",
                f"  Action: {step.action}",
                f"  Expect: {step.expect}",
                "",
            ])
        lines.append(
            "NOTE: Adapt steps as needed if the UI state differs from "
            "expectations. This is a reference, not a rigid script."
        )
        return "\n".join(lines)


class WorkflowLibrary(BaseModel):
    """The full workflow knowledge base.

    Persisted as JSON + FAISS index. Loaded at agent startup.
    """
    library_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Content
    canonical_workflows: list[CanonicalWorkflow] = Field(default_factory=list)
    raw_workflows: list[Workflow] = Field(default_factory=list)

    # Index metadata
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 3072
    index_path: str | None = None        # Path to FAISS index

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
        apps = set()
        for w in self.canonical_workflows:
            apps.update(w.app_names)
        return sorted(apps)
```

---

## 4. Pipeline Steps in Detail

### 4.1 Pass 0: PII Scrubbing

**Input**: Raw `RecordingSession` with screenshots and action descriptions.
**Output**: Scrubbed `RecordingSession`.

Uses `openadapt-privacy` (Presidio-backed):
- `scrub_image()` on every screenshot before VLM processing
- `scrub_text()` on action descriptions and typed text
- `scrub_dict()` on metadata dictionaries

This pass is a hard prerequisite for Pass 1. Screenshots sent to API VLMs must be
scrubbed first. The pipeline enforces this ordering -- no PII ever reaches the VLM.

### 4.2 Pass 1: Episode Transcript Generation

**Input**: Scrubbed `RecordingSession`.
**Output**: `EpisodeTranscript`.

**Batching strategy**: Group screenshots in sliding windows of 6-8 steps. Each window
includes the previous 2 screenshots for context continuity. The VLM sees:
1. The task instruction
2. The raw action descriptions
3. 6-8 before/after screenshot pairs

And must produce a `TranscriptEntry` for each action.

**Cost optimization**: For WAA recordings that already have `suggested_step` text in
`meta.json`, the VLM only needs to verify and enrich (cheaper). For `openadapt-capture`
recordings without step descriptions, full VLM narration is needed.

**Prompt structure** (simplified):

```
You are annotating a desktop recording. For each action, provide:
- narration: What the user did (natural language)
- intent: Why they did it
- ui_element_description: What UI element was targeted
- app_context: Current app and window
- state_change: What changed on screen
- is_corrective: Was this fixing a mistake? (true/false)
- is_exploratory: Was this exploring the UI? (true/false)
- vlm_confidence: How confident are you? (0.0-1.0)

Task: {task_description}
Actions: {batch_of_actions}
Screenshots: {batch_of_screenshot_pairs}
```

### 4.3 Pass 2: Workflow Extraction

**Input**: `EpisodeTranscript` + selected screenshots.
**Output**: `list[Workflow]`.

**Segmentation heuristics** (workflow boundary detection):
- Application switches
- Long pauses (>5 seconds)
- VLM-detected goal transitions
- Explicit markers in the transcript (e.g., "Now starting a different task")

**Step merging rules**:
- Click + Type in same element = one step ("Click cell A1 and type 'Year'")
- Navigation + Action = one step if same logical intent
- Corrective sequences (undo + redo, or click wrong target + click right target) =
  collapsed to the final correct action

**Parameter extraction**: Identify parameterizable parts that vary across instances.
For example, cell references (`A1`, `B2`), file names, typed values. These become
`WorkflowStep.parameters` entries that enable template instantiation.

### 4.4 Pass 3: Embedding and Similarity Matching

**Input**: Collection of `Workflow` objects from multiple recordings.
**Output**: `WorkflowLibrary` with `CanonicalWorkflow` objects.

#### 4.4.1 Embedding Input Construction

The embedding for each workflow combines multiple signals into a single text string:

```
[WORKFLOW] {name}
[GOAL] {goal}
[APPS] {", ".join(app_names)}
[DOMAIN] {domain}
[STEPS]
1. {step_1.description}
2. {step_2.description}
...
[TAGS] {", ".join(tags)}
```

This concatenated string is embedded using `text-embedding-3-large` (3072 dimensions).

**Rationale for text-embedding-3-large over multimodal embeddings**: Workflow retrieval
is primarily text-based (matching task instructions to workflow descriptions). The visual
component is already captured in the transcript. Text embeddings are faster, cheaper,
and do not require GPU at query time.

#### 4.4.2 Matching Algorithm: Cosine Similarity Threshold

The matching algorithm is intentionally simple:

```python
import numpy as np

SIMILARITY_THRESHOLD = 0.85

def match_workflow_to_canonical(
    new_workflow: Workflow,
    library: WorkflowLibrary,
) -> str | None:
    """Find the canonical workflow that matches, or return None.

    Returns canonical_id if similarity > 0.85, else None.
    """
    if not library.canonical_workflows:
        return None

    new_emb = np.array(new_workflow.embedding)

    best_score = -1.0
    best_canonical_id = None

    for canonical in library.canonical_workflows:
        if canonical.embedding is None:
            continue
        canon_emb = np.array(canonical.embedding)

        # Cosine similarity
        similarity = np.dot(new_emb, canon_emb) / (
            np.linalg.norm(new_emb) * np.linalg.norm(canon_emb)
        )

        if similarity > best_score:
            best_score = similarity
            best_canonical_id = canonical.canonical_id

    if best_score >= SIMILARITY_THRESHOLD:
        return best_canonical_id

    return None
```

**Why this instead of HDBSCAN or agglomerative clustering**:

- **Simplicity**: The algorithm is 20 lines. No hyperparameter tuning, no density
  estimation, no minimum cluster sizes. Just embed, compare, threshold.
- **Incremental**: New workflows can be added one at a time. No need to re-cluster
  the entire library. HDBSCAN requires re-running on the full dataset.
- **Deterministic**: Same input always produces the same output. No sensitivity to
  initialization or insertion order (unlike density-based methods).
- **Debuggable**: When a workflow matches (or does not match) a canonical, you can
  inspect the exact cosine similarity score. With HDBSCAN, the cluster assignment
  depends on the global density structure, which is hard to explain.

**The threshold of 0.85** is deliberately conservative. It means two workflows must be
very semantically similar to be considered the same. Tuning this down (e.g., 0.75)
would merge more aggressively; tuning up (e.g., 0.90) would create more singletons.
Start at 0.85 and adjust based on observed merge quality.

#### 4.4.3 Canonical Workflow Generation

When a new workflow matches an existing canonical (similarity > 0.85), the canonical
is updated:

1. Add the new workflow as an instance
2. Recompute the centroid embedding (average of all instance embeddings)
3. If `instance_count >= 3`, re-merge the canonical steps using an LLM:

```
You have {N} recordings of what appears to be the same workflow.
Merge them into ONE canonical workflow that:
- Uses the most common step sequence
- Includes optional steps only if they appear in >50% of instances
- Generalizes specific values into parameters where they vary
- Keeps the description general enough to match future similar tasks

Anchor workflow:
{anchor.to_demo_text()}

Other instances:
{instance_2.to_demo_text()}
{instance_3.to_demo_text()}
...

Output a single canonical workflow in the same format.
```

When a workflow has no match, it becomes its own singleton canonical workflow with
`instance_count=1`. It is still retrievable; it just has not been validated by
multiple observations yet.

---

## 5. Integration Points with Existing Repos

### 5.1 openadapt-retrieval

The `openadapt-retrieval` repo provides the retrieval primitives. Existing components
and reuse plan:

| Component | Status | Reuse Plan |
|-----------|--------|------------|
| `BaseEmbedder` (ABC) | Exists | Implement `TextEmbedder` subclass for text-embedding-3-large |
| `Qwen3VLEmbedder` | Exists | Continue using for multimodal demo-level retrieval |
| `CLIPEmbedder` | Exists | Fallback for lower-resource environments |
| `VectorIndex` (FAISS) | Exists | Directly reuse for `WorkflowLibrary` index |
| `MultimodalDemoRetriever` | Exists | Extend or compose for workflow-level retrieval |
| `EmbeddingStorage` | Exists | Reuse for persisting workflow embeddings |
| `CrossEncoderReranker` | Placeholder | Future: implement for workflow reranking |

**New component needed**: A `TextEmbedder` that wraps OpenAI's `text-embedding-3-large`
API, implementing `BaseEmbedder` but using the API instead of local inference.

### 5.2 openadapt-evals (Primary Host)

| Component | Integration |
|-----------|-------------|
| `DemoController` | Consumes `CanonicalWorkflow.to_demo_text()` directly. The multi-level format (GOAL/PLAN/REFERENCE TRAJECTORY with Think/Action/Expect) is already supported by `_parse_multilevel_demo()`. |
| `RetrievalAugmentedAgent` | Upgrade `_load_retriever()` to use `WorkflowLibrary` instead of raw demo files. The `.retrieve()` method returns `CanonicalWorkflow` which provides `.to_demo_text()`. |
| `CorrectionStore` | Feed corrections back: when a workflow step fails and gets corrected, update the canonical workflow. The `CorrectionEntry.correction_step` dict maps directly to `WorkflowStep` fields. |
| `plan_verify.py` | No changes needed. Already provides `verify_step()` and `verify_goal_completion()` that the controller uses for step-by-step verification. |
| `refine_demo.py` | The two-pass refinement (holistic + per-step) is conceptually the same as Pass 1 + Pass 2. Can be refactored to share VLM prompt infrastructure. |
| `scripts/` | Add `scripts/extract_workflows.py` CLI for batch processing recordings. |

### 5.3 openadapt-capture

| Component | Integration |
|-----------|-------------|
| `Recording` model | Read via `CaptureRecordingAdapter.from_db()` |
| `ActionEvent` | Map to `NormalizedAction` (name, mouse_x/y, key_name, key_char) |
| `Screenshot` | `png_data` bytes -> scrub -> include in transcript VLM calls |
| `WindowEvent` | Provides `title` and `state` for app context |
| `AudioInfo` | Future: use `transcribed_text` as additional transcript signal |

### 5.4 openadapt-privacy

| Component | Integration |
|-----------|-------------|
| `PresidioScrubber.scrub_image()` | Called on every screenshot before VLM processing |
| `PresidioScrubber.scrub_text()` | Called on action descriptions and transcript text |
| `TextScrubbingMixin.scrub_dict()` | Called on metadata dicts |

### 5.5 Viewer Integration

The extracted workflows provide new data for visualization:

| Feature | Data Source |
|---------|-------------|
| Workflow track overlay | `Workflow.steps` with `timestamp_start`/`timestamp_end` aligned to action timeline |
| Workflow library browser | `WorkflowLibrary.canonical_workflows` with search |
| Similarity visualization | 2D UMAP projection of workflow embeddings with canonical grouping |
| Retrieval debugging | Show which canonical workflow was retrieved for a given task and the similarity score |

---

## 6. Cost Estimates

### 6.1 Transcript Generation (Pass 1)

Each step has a before + after screenshot (~1000 tokens each at typical resolution).
Text prompt is ~500 tokens. With batching of 6-8 steps per call, each call is ~15K
input tokens + ~2K output tokens.

| Model | Cost per recording (20 steps) | Cost per 100 recordings |
|-------|-------------------------------|------------------------|
| GPT-4.1-mini | ~$0.15 | ~$15 |
| Claude Sonnet 4 | ~$0.45 | ~$45 |
| GPT-4.1 | ~$0.60 | ~$60 |

**Recommendation**: Use GPT-4.1-mini for the initial pass. It is sufficient for
narrating obvious UI interactions. Reserve Sonnet/GPT-4.1 for ambiguous steps
flagged with low confidence.

### 6.2 Workflow Extraction (Pass 2)

Pass 2 is cheaper because it operates primarily on text (the transcript from Pass 1)
with only selected screenshots for ambiguous segmentation decisions.

| Model | Cost per recording | Cost per 100 recordings |
|-------|-------------------|------------------------|
| GPT-4.1-mini | ~$0.05 | ~$5 |

### 6.3 Embedding Generation (Pass 3)

| Model | Cost per embedding | Cost per 100 workflows |
|-------|-------------------|----------------------|
| text-embedding-3-large (3072 dim) | ~$0.00013 per 1K tokens | ~$0.10 |

Embedding cost is negligible.

### 6.4 Canonical Workflow Merging

| Model | Cost per merge | Cost for 50 merges |
|-------|---------------|-------------------|
| GPT-4.1-mini | ~$0.08 | ~$4 |

### 6.5 Total Pipeline Cost

| Phase | Per recording | Per 100 recordings | Per 1000 recordings |
|-------|--------------|--------------------|--------------------|
| Pass 1: Transcript | $0.15 | $15 | $150 |
| Pass 2: Extraction | $0.05 | $5 | $50 |
| Pass 3: Matching | n/a | $4 (one-time) | $10 (one-time) |
| **Total** | **$0.20** | **$24** | **$210** |

---

## 7. Testing Strategy

### 7.1 Synthetic Data Design

Tests exercise the full pipeline without requiring VLM API calls or real recordings.
Deterministic mock data with known expected outputs.

**Four synthetic recording families**:

**Family A: "Settings Toggle" (3 variants)** -- expected to match the same canonical
- A1: "Turn off Wi-Fi in System Settings" (5 steps)
- A2: "Disable Bluetooth in Settings" (5 steps)
- A3: "Turn off Night Shift in Display Settings" (6 steps)

**Family B: "Spreadsheet Data Entry" (3 variants)** -- expected to match the same canonical
- B1: "Enter quarterly sales data" (8 steps)
- B2: "Create budget spreadsheet" (10 steps)
- B3: "Enter student grades" (7 steps)

**Family C: "Document Formatting" (2 variants)** -- expected to match the same canonical
- C1: "Change document font to Arial 14pt" (4 steps)
- C2: "Set heading to bold Helvetica" (5 steps)

**Singleton D: "Archive files"** -- expected to remain a standalone canonical
- D1: "Create zip archive of project folder" (3 steps)

### 7.2 Test Code

```python
# tests/test_recording_normalization.py

import pytest
from openadapt_evals.workflow.models import (
    RecordingSession, RecordingSource, NormalizedAction, ActionType,
)


class TestRecordingNormalization:
    """Verify WAA meta.json and openadapt-capture DB produce valid RecordingSession."""

    def test_waa_meta_json_parsing(self, synthetic_waa_recording):
        """meta.json with steps and PNG paths -> RecordingSession."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.action_count > 0
        assert session.source == RecordingSource.WAA_VNC

    def test_capture_db_parsing(self, synthetic_capture_db):
        """SQLite DB with ActionEvent/Screenshot -> RecordingSession."""
        session = CaptureRecordingAdapter.from_db(synthetic_capture_db)
        assert session.action_count > 0
        assert session.source == RecordingSource.NATIVE_CAPTURE

    def test_pii_scrubbing_applied(self, session_with_pii):
        """Verify PII is scrubbed from actions and descriptions."""
        scrubbed = scrub_recording_session(session_with_pii)
        for action in scrubbed.actions:
            assert "john@example.com" not in action.description
            assert "555-123-4567" not in (action.typed_text or "")

    def test_content_hash_deterministic(self):
        """Same session data produces same content hash."""
        session1 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test task",
            duration_seconds=10.0,
        )
        session2 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test task",
            duration_seconds=10.0,
        )
        assert session1.content_hash == session2.content_hash

    def test_app_names_extracted(self):
        """app_names computed field returns unique sorted app names."""
        session = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test",
            actions=[
                NormalizedAction(
                    timestamp=0, action_type=ActionType.CLICK,
                    description="Click", app_name="Calc",
                ),
                NormalizedAction(
                    timestamp=1, action_type=ActionType.CLICK,
                    description="Click", app_name="Writer",
                ),
                NormalizedAction(
                    timestamp=2, action_type=ActionType.CLICK,
                    description="Click", app_name="Calc",
                ),
            ],
        )
        assert session.app_names == ["Calc", "Writer"]


# tests/test_transcript_generation.py

class TestTranscriptGeneration:
    """Test Pass 1 with mocked VLM calls."""

    def test_transcript_from_synthetic_session(self, synthetic_session, mock_vlm):
        """Generate transcript from synthetic session using mocked VLM."""
        mock_vlm.return_value = SYNTHETIC_TRANSCRIPT_RESPONSE
        transcript = generate_transcript(synthetic_session, vlm=mock_vlm)
        assert len(transcript.entries) == len(synthetic_session.actions)
        assert all(e.narration for e in transcript.entries)

    def test_batching_respects_window_size(self, large_session, mock_vlm):
        """Verify batching sends correct number of screenshots per call."""
        generate_transcript(large_session, vlm=mock_vlm, batch_size=6)
        for call_args in mock_vlm.call_args_list:
            images = call_args.kwargs.get("images", [])
            assert len(images) <= 16  # 8 pairs max

    def test_cost_estimation(self, synthetic_session):
        """Verify cost estimation produces reasonable numbers."""
        estimate = estimate_transcript_cost(
            synthetic_session, model="gpt-4.1-mini",
        )
        assert estimate.total_input_tokens > 0
        assert 0 < estimate.estimated_cost_usd < 10.0

    def test_transcript_preserves_action_ids(self, synthetic_session, mock_vlm):
        """Each TranscriptEntry links back to its source NormalizedAction."""
        mock_vlm.return_value = SYNTHETIC_TRANSCRIPT_RESPONSE
        transcript = generate_transcript(synthetic_session, vlm=mock_vlm)
        action_ids = {a.action_id for a in synthetic_session.actions}
        for entry in transcript.entries:
            assert entry.action_id in action_ids


# tests/test_workflow_extraction.py

class TestWorkflowExtraction:
    """Test Pass 2 with synthetic transcripts."""

    def test_single_workflow_extraction(self, simple_transcript, mock_vlm):
        """Simple transcript -> single Workflow with correct steps."""
        workflows = extract_workflows(simple_transcript, vlm=mock_vlm)
        assert len(workflows) == 1
        assert workflows[0].step_count > 0

    def test_multi_workflow_segmentation(self, multi_task_transcript, mock_vlm):
        """Transcript spanning two tasks -> two Workflows."""
        workflows = extract_workflows(multi_task_transcript, vlm=mock_vlm)
        assert len(workflows) == 2

    def test_corrective_actions_filtered(
        self, transcript_with_corrections, mock_vlm,
    ):
        """Undo/redo sequences should be collapsed."""
        workflows = extract_workflows(
            transcript_with_corrections, vlm=mock_vlm,
        )
        step_descriptions = [s.description for s in workflows[0].steps]
        assert not any("undo" in d.lower() for d in step_descriptions)

    def test_step_merging(self, transcript_with_atomic_actions, mock_vlm):
        """Click + Type in same element -> one step."""
        workflows = extract_workflows(
            transcript_with_atomic_actions, vlm=mock_vlm,
        )
        # 2 atomic actions (click A1, type 'Year') -> 1 merged step
        assert workflows[0].step_count < len(
            transcript_with_atomic_actions.entries
        )

    def test_parameter_extraction(self, transcript_with_values, mock_vlm):
        """Cell references and typed values become parameters."""
        workflows = extract_workflows(
            transcript_with_values, vlm=mock_vlm,
        )
        step_with_params = [
            s for s in workflows[0].steps if s.parameters
        ]
        assert len(step_with_params) > 0

    def test_to_demo_text_format(self):
        """Workflow.to_demo_text() produces parseable DemoController format."""
        workflow = Workflow(
            name="Test workflow",
            description="A test",
            goal="Do the test",
            app_names=["TestApp"],
            domain="test",
            steps=[
                WorkflowStep(
                    step_index=0,
                    timestamp_start=0,
                    timestamp_end=1,
                    description="Click button",
                    think="Need to click",
                    action="Click the button",
                    expect="Button clicked",
                    action_type=ActionType.CLICK,
                    app_name="TestApp",
                    ui_element="Button",
                ),
            ],
            total_duration_seconds=1.0,
            session_id="test",
            transcript_id="test",
            recording_source=RecordingSource.WAA_VNC,
        )
        demo_text = workflow.to_demo_text()
        assert "GOAL:" in demo_text
        assert "PLAN:" in demo_text
        assert "REFERENCE TRAJECTORY:" in demo_text
        assert "Think:" in demo_text
        assert "Action:" in demo_text
        assert "Expect:" in demo_text


# tests/test_workflow_matching.py

import numpy as np


class TestWorkflowMatching:
    """Test Pass 3: cosine similarity matching."""

    def test_similar_workflows_match(self, settings_toggle_workflows):
        """Three 'toggle setting' workflows -> same canonical."""
        library = WorkflowLibrary()
        for wf in settings_toggle_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                # First one creates a new canonical
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        # All three should end up in the same canonical
        assert library.canonical_count == 1
        assert library.canonical_workflows[0].instance_count == 3

    def test_different_workflows_separate(self, mixed_workflows):
        """Settings toggles + spreadsheet entry -> separate canonicals."""
        library = build_library(mixed_workflows)
        assert library.canonical_count >= 2

    def test_singleton_becomes_own_canonical(self, singleton_workflow):
        """A unique workflow with no matches -> singleton canonical."""
        library = build_library([singleton_workflow])
        assert library.canonical_count == 1
        assert library.canonical_workflows[0].instance_count == 1

    def test_similarity_threshold_respected(self):
        """Workflows with similarity < 0.85 do NOT match."""
        emb_a = np.random.randn(3072).tolist()
        # Create a very different embedding
        emb_b = (-np.array(emb_a)).tolist()

        wf_a = Workflow(
            name="A", description="A", goal="A",
            app_names=["A"], domain="a",
            steps=[], total_duration_seconds=1.0,
            session_id="a", transcript_id="a",
            recording_source=RecordingSource.WAA_VNC,
            embedding=emb_a, embedding_model="test", embedding_dim=3072,
        )
        wf_b = Workflow(
            name="B", description="B", goal="B",
            app_names=["B"], domain="b",
            steps=[], total_duration_seconds=1.0,
            session_id="b", transcript_id="b",
            recording_source=RecordingSource.WAA_VNC,
            embedding=emb_b, embedding_model="test", embedding_dim=3072,
        )

        library = WorkflowLibrary()
        create_canonical_from_workflow(wf_a, library)

        match = match_workflow_to_canonical(wf_b, library)
        assert match is None  # Should NOT match

    def test_incremental_addition(self, existing_library, new_workflows):
        """Adding new workflows to existing library updates version."""
        original_version = existing_library.version
        updated = update_library(existing_library, new_workflows)
        assert updated.version == original_version + 1


# tests/test_retrieval.py

class TestWorkflowRetrieval:
    """Test retrieval pipeline end-to-end."""

    def test_exact_match_retrieval(self, workflow_library):
        """Task matching a canonical workflow returns it as top result."""
        results = retrieve_workflows(
            "Turn off Wi-Fi in System Settings",
            library=workflow_library,
            top_k=3,
        )
        assert results[0].canonical_id == "settings_toggle_canonical"

    def test_semantic_match_retrieval(self, workflow_library):
        """Semantically similar task finds the right workflow."""
        results = retrieve_workflows(
            "Disable the wireless network adapter",
            library=workflow_library,
            top_k=3,
        )
        assert "settings" in results[0].domain

    def test_no_match_returns_low_confidence(self, workflow_library):
        """Unrelated task returns low-confidence results."""
        results = retrieve_workflows(
            "Write a Python unit test for the parser",
            library=workflow_library,
            top_k=3,
        )
        assert all(r.similarity_score < 0.5 for r in results)

    def test_demo_text_compatible_with_demo_controller(
        self, canonical_workflow,
    ):
        """CanonicalWorkflow.to_demo_text() produces valid DemoController format."""
        demo_text = canonical_workflow.to_demo_text()
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )
        parsed = _parse_multilevel_demo(demo_text)
        assert parsed is not None
        assert parsed["goal"]
        assert len(parsed["trajectory"]) > 0
```

### 7.3 Edge Cases

| Edge Case | Expected Behavior |
|-----------|-------------------|
| Single-step workflow | Valid `Workflow` with 1 step, becomes singleton canonical |
| 50+ step workflow | Valid but flagged `complexity="complex"` |
| Multi-app workflow | `app_names` lists all apps, steps track app switches |
| Aborted workflow | Segmented into complete sub-workflow + incomplete fragment (discarded) |
| Identical recordings | Deduplicated by `content_hash` before matching |
| Partial step overlap | Matched if overall similarity > 0.85, canonical merges common steps |
| Empty recording | Rejected with validation error (no actions) |
| Missing screenshots | Transcript generated from action descriptions only (lower confidence) |

---

## 8. The Three Planner Strategies Compared

| Strategy | How | Cost | Best for |
|----------|-----|------|----------|
| API planner (Claude/GPT) | General reasoning | $0.30/episode | Novel tasks |
| Distilled planner (SFT open model) | Learned from API trajectories | $0/episode | Known task types |
| RAG planner (workflow retrieval) | Retrieve + execute canonical workflow | $0.01/episode | Seen workflows |

The RAG approach is most aligned with OpenAdapt's thesis: "trajectory-conditioned
disambiguation." Accumulated workflows are the defensible moat.

---

## 9. Key Design Decisions

**Decision 1: Cosine similarity threshold over clustering algorithms.** The number
of canonical workflows is unknown and grows incrementally over time. HDBSCAN and
agglomerative clustering require re-running on the full dataset and introduce
algorithmic complexity without clear benefit. A simple cosine similarity threshold
(>0.85 = same workflow) is deterministic, incremental, debuggable, and trivial to
implement. If we later find we need more sophisticated grouping, we can always add
it -- but start simple.

**Decision 2: Two separate embedding strategies.** Workflow-level retrieval uses
text embeddings (`text-embedding-3-large`, 3072 dim) because workflow matching is
fundamentally semantic text matching. Demo-level retrieval continues using multimodal
embeddings (Qwen3-VL, 512 dim) via `openadapt-retrieval` because screenshot visual
similarity matters at the demo level. These serve different purposes and should not
be conflated.

**Decision 3: Workflow extraction lives in openadapt-evals, not openadapt-retrieval.**
The extraction pipeline involves VLM calls, recording adapters, and benchmark
infrastructure -- all openadapt-evals concerns. openadapt-retrieval stays focused on
embedding, indexing, and retrieval primitives. The workflow pipeline *uses*
openadapt-retrieval's `VectorIndex` and `EmbeddingStorage` but owns the extraction logic.

**Decision 4: Multi-level demo format as the interchange format.** The existing
`DemoController` and `_parse_multilevel_demo()` already handle the
GOAL/PLAN/TRAJECTORY format with Think/Action/Expect steps. Rather than inventing a
new format, `CanonicalWorkflow.to_demo_text()` produces this exact format, ensuring
drop-in compatibility.

**Decision 5: PII scrubbing before VLM calls, not after.** Screenshots sent to API
VLMs must be scrubbed first. The pipeline enforces this by making Pass 0 (scrubbing)
a prerequisite for Pass 1. No PII ever reaches the VLM.

---

## 10. Implementation Priority

### Priority 1: Foundation (Weeks 1-2)

1. **Pydantic models** -- all classes defined in Section 3 above. These go into
   `openadapt_evals/workflow/models.py`.

2. **WAA recording adapter** -- parse existing `waa_recordings/*/meta.json` + step
   PNGs into `RecordingSession`. This is the fastest path to data because WAA
   recordings already exist on disk.

3. **Synthetic test fixtures** -- create the four families (A/B/C/D) of synthetic
   recordings with deterministic expected outputs.

### Priority 2: Transcript Pipeline (Weeks 3-4)

4. **Pass 1: Transcript generation** -- VLM-based transcript generation with batched
   screenshot processing. Start with existing WAA recordings.

5. **PII scrubbing integration** -- Wire openadapt-privacy into the pipeline before
   any VLM calls.

### Priority 3: Workflow Extraction (Weeks 5-6)

6. **Pass 2: Workflow extraction** -- VLM-based segmentation and step merging. The
   existing `refine_demo.py` two-pass architecture is a close template.

7. **DemoController integration** -- Verify that `Workflow.to_demo_text()` parses
   correctly via `_parse_multilevel_demo()`.

### Priority 4: Matching and Retrieval (Weeks 7-8)

8. **Text embedder for openadapt-retrieval** -- Add OpenAI `text-embedding-3-large`
   backend to openadapt-retrieval's `BaseEmbedder`.

9. **Cosine similarity matching** -- Match extracted workflows to existing canonicals,
   merge or create new ones.

10. **Retrieval integration** -- Update `RetrievalAugmentedAgent` to use
    `WorkflowLibrary`.

### Priority 5: Flywheel and Distillation (Weeks 9+)

11. **Correction flywheel** -- Feed `CorrectionStore` entries back into canonical
    workflow updates.

12. **openadapt-capture adapter** -- Normalize native capture recordings (second data
    source).

13. **Open VLM distillation** -- Use API-generated transcripts as SFT training data
    for an 8B model. Candidate: Qwen2.5-VL-7B fine-tuned on
    `(screenshot_pair, transcript_entry)` pairs. This eliminates API dependency for
    transcript generation at scale.

---

## 11. File Layout

```
openadapt_evals/workflow/
    __init__.py
    models.py              # All Pydantic classes from Section 3
    adapters/
        __init__.py
        waa.py             # WAARecordingAdapter.from_meta_json()
        capture.py         # CaptureRecordingAdapter.from_db()
    pipeline/
        __init__.py
        scrub.py           # Pass 0: PII scrubbing wrapper
        transcript.py      # Pass 1: VLM transcript generation
        extract.py         # Pass 2: Workflow extraction
        match.py           # Pass 3: Embedding + cosine similarity matching
    library.py             # WorkflowLibrary persistence (JSON + FAISS)
    retrieve.py            # retrieve_workflows() for agent use

scripts/
    extract_workflows.py   # CLI: batch process recordings through pipeline

tests/
    test_recording_normalization.py
    test_transcript_generation.py
    test_workflow_extraction.py
    test_workflow_matching.py
    test_retrieval.py
    conftest.py            # Synthetic fixtures (families A/B/C/D)
```

---

## 12. Reference Files

| File | Relevance |
|------|-----------|
| `openadapt-retrieval/.../demo_retriever.py` | `MultimodalDemoRetriever`, `DemoMetadata`, `RetrievalResult` -- core retrieval classes to extend |
| `openadapt-retrieval/.../index.py` | `VectorIndex` -- FAISS wrapper to reuse directly |
| `openadapt-retrieval/.../base.py` | `BaseEmbedder` ABC -- implement for text-embedding-3-large |
| `openadapt-retrieval/.../persistence.py` | `EmbeddingStorage` -- reuse for workflow embedding persistence |
| `openadapt-evals/.../demo_controller.py` | `DemoController`, `PlanStep`, `PlanState` -- consumes workflow output via `to_demo_text()` |
| `openadapt-evals/.../retrieval_agent.py` | `RetrievalAugmentedAgent` -- upgrade to use WorkflowLibrary |
| `openadapt-evals/.../plan_verify.py` | `verify_step()`, `verify_goal_completion()` -- used by DemoController |
| `openadapt-evals/.../correction_store.py` | `CorrectionStore`, `CorrectionEntry` -- feedback loop into workflow refinement |
| `openadapt-evals/scripts/refine_demo.py` | Two-pass VLM refinement -- architectural template for Pass 1 + Pass 2 |
| `openadapt-capture/.../models.py` | `Recording`, `ActionEvent`, `Screenshot`, `WindowEvent` -- source data for native capture adapter |
| `openadapt-privacy/.../base.py` | `ScrubbingProvider`, `scrub_image()`, `scrub_text()` -- PII scrubbing interface |
