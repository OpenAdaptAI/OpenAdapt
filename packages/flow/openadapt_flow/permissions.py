"""Versioned names for the existing signed run permission artifacts.

The canonical class names and signed formats remain unchanged. Legacy imports
are deprecated in favor of these names and remain available for at least one
full release cycle. Imports emit no deprecation warnings.
"""

from openadapt_flow.qualification_admission import (
    QualificationAdmissionEnvelope as RunPermissionV1,
)
from openadapt_flow.qualification_admission_v2 import (
    QualificationAdmissionEnvelope as RunPermissionV2,
)

__all__ = ["RunPermissionV1", "RunPermissionV2"]
