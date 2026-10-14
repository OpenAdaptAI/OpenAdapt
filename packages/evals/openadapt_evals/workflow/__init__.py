"""Workflow extraction and RAG pipeline for desktop automation.

This package provides:
- Pydantic models for normalized recordings, transcripts, workflows, and libraries
- Recording adapters (WAA VNC, openadapt-capture)
- Pipeline stages: transcript generation, workflow extraction, matching

See docs/design/workflow_extraction_pipeline.md for the full design.
"""

from openadapt_evals.workflow.models import (
    ActionType,
    CanonicalWorkflow,
    EpisodeTranscript,
    NormalizedAction,
    RecordingSession,
    RecordingSource,
    TranscriptEntry,
    Workflow,
    WorkflowInstance,
    WorkflowLibrary,
    WorkflowStep,
)

__all__ = [
    "ActionType",
    "CanonicalWorkflow",
    "EpisodeTranscript",
    "NormalizedAction",
    "RecordingSession",
    "RecordingSource",
    "TranscriptEntry",
    "Workflow",
    "WorkflowInstance",
    "WorkflowLibrary",
    "WorkflowStep",
]
