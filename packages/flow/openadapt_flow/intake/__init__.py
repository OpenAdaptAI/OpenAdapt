"""Bounded intake extraction: referral document -> typed workflow parameters.

This step runs before a workflow, never inside it. It reads a referral PDF, a
fax or scan image, or an email, proposes a value for each of the workflow's
typed parameters (``Workflow.param_specs``), validates each value against its
spec, and marks low-confidence values for a person to confirm. The output is a
JSON object that ``openadapt-flow run --params-file`` accepts.

Two extraction modes:

* ``local`` (default): deterministic label matching over the document text.
  No network and no model.
* ``agent``: the calling agent (for example the coding agent that invoked the
  CLI) does the reading. :func:`build_agent_request` sends it the document text
  after openadapt-privacy scrubbing, numbered by line. The agent answers with a
  line number per field, and :func:`apply_agent_response` reads the value from
  that line of the *unscrubbed* text locally, so identifiers stay on the
  machine. This package makes no model call in either mode.

Runtime UI execution is unchanged: replay still makes zero model calls.
"""

from openadapt_flow.intake.document import IntakeDocument, read_document
from openadapt_flow.intake.extract import (
    DEFAULT_CONFIRM_THRESHOLD,
    ExtractedField,
    IntakeNotConfirmed,
    IntakeResult,
    apply_agent_response,
    build_agent_request,
    extract_fields,
)

__all__ = [
    "DEFAULT_CONFIRM_THRESHOLD",
    "ExtractedField",
    "IntakeDocument",
    "IntakeNotConfirmed",
    "IntakeResult",
    "apply_agent_response",
    "build_agent_request",
    "extract_fields",
    "read_document",
]
