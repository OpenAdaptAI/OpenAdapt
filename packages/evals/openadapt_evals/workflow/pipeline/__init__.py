"""Pipeline stages for workflow extraction.

- scrub: Pass 0 PII scrubbing
- transcript: Pass 1 VLM transcript generation
- extract: Pass 2 structured workflow extraction from transcripts
- match: Pass 3 cosine similarity matching to canonical workflows
"""

from openadapt_evals.workflow.pipeline.extract import extract_workflow
from openadapt_evals.workflow.pipeline.match import (
    SIMILARITY_THRESHOLD,
    add_instance_to_canonical,
    create_canonical_from_workflow,
    match_workflow_to_canonical,
)

__all__ = [
    "SIMILARITY_THRESHOLD",
    "add_instance_to_canonical",
    "create_canonical_from_workflow",
    "extract_workflow",
    "match_workflow_to_canonical",
]
