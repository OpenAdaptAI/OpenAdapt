"""Independent effect verification against a system of record (not the screen).

The concrete runtime for the RFC ``Effect`` type
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.2). A screen/vision
postcondition cannot see a partial save, a phantom optimistic-UI success, a
duplicate submission, a lost update, or a double-delivered click -- the
fault-model study found 5 of 7 transactional fault classes silently mishandled
by screen verification. An :class:`EffectVerifier` reads the REAL system of
record (a FHIR/REST API, a document store) and rules CONFIRMED / REFUTED /
INDETERMINATE, halting (never guessing success) whenever it cannot certify the
declared write or independently read business outcome.

Public surface:

- Types: :class:`Effect`, :class:`EffectKind`, :class:`EffectState`,
  :class:`EffectVerdict`, :class:`Verdict`, :class:`EffectVerifier`.
- Verifiers: :class:`RestRecordVerifier` (JSON REST SoR, e.g. MockMed
  ``/api/db``), :class:`FhirEffectVerifier` (OpenEMR FHIR R4, primary),
  :class:`SqlRecordVerifier` (read-only SQL, enforced whitelist),
  :class:`FileArrivalVerifier` (file / SFTP arrival),
  :class:`DocumentHashVerifier` (filesystem document store).
- SQL table-delta audit (promoted from the Frappe Lending reference matrix):
  :func:`capture_table_counts`, :func:`audit_table_deltas`.
- Secret-isolated auth for declarative configs: :class:`AuthRef`.
- Compensation / reconciliation: :func:`reconcile_or_escalate`,
  :class:`Compensator`, :class:`RestCompensator`, :class:`CompensationResult`,
  :class:`ReconciliationTask`, :func:`build_reconciliation_task`.

The declarative deployment surface for all of this (one ``deployment.yaml``
section wiring a verifier, its secret-isolated auth, and its run-parameter
bindings) lives in :mod:`openadapt_flow.deployment`; the operator guide is
``docs/EFFECT_KIT.md``.
"""

from openadapt_flow.runtime.effects.auth import (  # noqa: F401
    AuthRef,
)
from openadapt_flow.runtime.effects.compensation import (  # noqa: F401
    CompensationAction,
    CompensationOutcome,
    CompensationResult,
    Compensator,
    ReconciliationTask,
    RestCompensator,
    build_reconciliation_task,
    reconcile_or_escalate,
    record_digest,
)
from openadapt_flow.runtime.effects.document_hash import (  # noqa: F401
    DocumentHashVerifier,
    sha256_file,
)
from openadapt_flow.runtime.effects.effect import (  # noqa: F401
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    ValueExpr,
    Verdict,
    record_matches,
)
from openadapt_flow.runtime.effects.fhir import (  # noqa: F401
    DEFAULT_OBSERVATION_PATHS,
    FhirEffectVerifier,
    extract_path,
)
from openadapt_flow.runtime.effects.file_arrival import (  # noqa: F401
    ArrivalTransport,
    FileArrivalVerifier,
)
from openadapt_flow.runtime.effects.rest import (  # noqa: F401
    RestRecordVerifier,
)
from openadapt_flow.runtime.effects.sql import (  # noqa: F401
    SqlRecordVerifier,
    assert_read_only_sql,
    audit_table_deltas,
    capture_table_counts,
)

__all__ = [
    "Effect",
    "EffectKind",
    "EffectState",
    "EffectVerdict",
    "EffectVerifier",
    "ValueExpr",
    "Verdict",
    "record_matches",
    "RestRecordVerifier",
    "FhirEffectVerifier",
    "DEFAULT_OBSERVATION_PATHS",
    "extract_path",
    "DocumentHashVerifier",
    "sha256_file",
    "reconcile_or_escalate",
    "Compensator",
    "RestCompensator",
    "CompensationResult",
    "CompensationAction",
    "CompensationOutcome",
    "ReconciliationTask",
    "build_reconciliation_task",
    "record_digest",
    "AuthRef",
    "SqlRecordVerifier",
    "assert_read_only_sql",
    "audit_table_deltas",
    "capture_table_counts",
    "FileArrivalVerifier",
    "ArrivalTransport",
]
