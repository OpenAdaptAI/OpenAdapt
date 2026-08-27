"""Flow-owned hosted-runner verification and execution library.

Desktop owns the authenticated register, poll, and callback transport loop.
This package owns the transport-independent trust, admission, one-use,
governed-execution, and terminal-classification boundary:

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
  refuse;
* :mod:`~openadapt_flow.runner.hosted_adapter` — the strict Cloud lease wire,
  protected local trust, managed child bridge, and no-replay result contract.
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
from openadapt_flow.runner.dispatch_envelope import (
    ManagedDispatchEnvelope,
    ManagedDispatchEnvelopeError,
    read_managed_dispatch_envelope,
    write_managed_dispatch_envelope,
)
from openadapt_flow.runner.hosted_adapter import (
    RUNNER_RENEWAL_HEADER,
    CallbackRequest,
    CallbackResponse,
    DeliveryAuthority,
    HostedDispatch,
    HostedDispatchRefusal,
    HostedRecoveryBinding,
    HostedRunnerAdapter,
    HostedRunnerTransport,
    HostedRunResult,
    PollRequest,
    RegisterCapabilities,
    RegisterRequest,
    RegisterResponse,
    registration_renewal_headers,
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
    "CallbackRequest",
    "CallbackResponse",
    "DeliveryAuthority",
    "DispatchParseError",
    "EvidenceOutbox",
    "HostedDispatch",
    "HostedDispatchRefusal",
    "HostedRecoveryBinding",
    "HostedRunResult",
    "HostedRunnerAdapter",
    "HostedRunnerTransport",
    "LeaseError",
    "ManagedDispatchEnvelope",
    "ManagedDispatchEnvelopeError",
    "LeasePhase",
    "LeaseTracker",
    "LeasedDispatch",
    "Refusal",
    "RefusalCode",
    "PollRequest",
    "RUNNER_RENEWAL_HEADER",
    "RegisterCapabilities",
    "RegisterRequest",
    "RegisterResponse",
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
    "read_managed_dispatch_envelope",
    "parse_dispatch",
    "registration_renewal_headers",
    "server_reclaim_outcome",
    "verify_dispatch",
    "write_managed_dispatch_envelope",
]
