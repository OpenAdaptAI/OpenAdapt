"""EXPERIMENTAL runner-client LIBRARY (verification, lease logic, evidence,
command mapping) for the hosted control-plane / local-execution-plane runner
protocol. NO daemon, NO network loop, NO CLI verb — deliberately.

Scope: the merged ``/api/runners/*`` control-plane surface in openadapt-cloud
(``src/lib/runners.ts``) is mock-gated (410 in live) and its transport-facing
half is scheduled to CHANGE before any customer daemon can ship (poll cadence
and hosting economics, lease renewal + sleep reclaim, control-verb channel,
mandatory params-by-reference for regulated orgs — see
``docs/design/RUNNER_CLIENT_LIBRARY.md`` for the verified findings and the
required revisions). This package therefore contains only the transport-
agnostic half that SURVIVES that revision:

* :mod:`~openadapt_flow.runner.protocol` — strict typed models of the
  dispatch wire contract (contract drift is a refusal, not a best guess);
* :mod:`~openadapt_flow.runner.config` — the operator-authored local trust
  manifest (``runner.toml``): the exact bundles, profiles, policies, and
  param domains this machine will execute. No remote code delivery: an
  unknown digest is refused, ``bundle.url`` is never fetched;
* :mod:`~openadapt_flow.runner.verify` — independent local re-validation of
  the cloud-minted ``GovernedRunAuthorization`` (bundle hash, runtime-inputs
  digest, policy pin, param domains, egress posture) with a stable refusal
  matrix. Local gates are final;
* :mod:`~openadapt_flow.runner.lease` — the lease/visibility-timeout state
  machine as pure logic (acquire / start / renew / sleep detection / honest
  late completion), injectable clock, transport-agnostic;
* :mod:`~openadapt_flow.runner.evidence` — the PHI-free
  ``openadapt.run-evidence/v1`` event builders (schema-minimal: digests,
  counts, step ids — never free text or pixels);
* :mod:`~openadapt_flow.runner.outbox` — the durable, idempotent offline
  evidence queue (a run that finishes offline reports late, never never);
* :mod:`~openadapt_flow.runner.commands` — mapping of governed dispatch verbs
  onto the EXISTING CLI entry points (``run`` / ``resume``); unmappable verbs
  refuse.
"""

from openadapt_flow.runner.commands import (
    UnmappedVerbError,
    build_resume_argv,
    build_run_argv,
    map_control_verb,
)
from openadapt_flow.runner.config import (
    RunnerConfig,
    RunnerConfigError,
    TrustedBundle,
    load_runner_config,
)
from openadapt_flow.runner.lease import (
    CompletionDisposition,
    LeaseError,
    LeasePhase,
    LeaseTracker,
    SleepGap,
    StartRefused,
    WorkflowSerialization,
    server_reclaim_outcome,
)
from openadapt_flow.runner.outbox import EvidenceOutbox
from openadapt_flow.runner.protocol import (
    DispatchParseError,
    LeasedDispatch,
    RunnerDispatchPayload,
    parse_dispatch,
)
from openadapt_flow.runner.verify import (
    Refusal,
    RefusalCode,
    VerifiedDispatch,
    verify_dispatch,
)

__all__ = [
    "CompletionDisposition",
    "DispatchParseError",
    "EvidenceOutbox",
    "LeaseError",
    "LeasePhase",
    "LeaseTracker",
    "LeasedDispatch",
    "Refusal",
    "RefusalCode",
    "RunnerConfig",
    "RunnerConfigError",
    "RunnerDispatchPayload",
    "SleepGap",
    "StartRefused",
    "TrustedBundle",
    "UnmappedVerbError",
    "VerifiedDispatch",
    "WorkflowSerialization",
    "build_resume_argv",
    "build_run_argv",
    "load_runner_config",
    "map_control_verb",
    "parse_dispatch",
    "server_reclaim_outcome",
    "verify_dispatch",
]
