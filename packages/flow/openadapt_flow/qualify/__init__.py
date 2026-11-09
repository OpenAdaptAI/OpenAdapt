"""Qualify a compiled workflow from a small ``verify.yaml``; no new code.

To qualify a workflow is to run it, before it may run unattended on a real
system, against the normal case plus deliberate faults, and confirm it
completes and is verified by reading back the system of record, and halts on
every fault instead of acting wrongly. See ``docs/QUALIFICATION_TERMS.md``.
"""

from openadapt_flow.qualify.campaign import (
    CASES,
    QualifyError,
    QualifyHooks,
    QualifyResult,
    TrialContext,
    qualify,
)
from openadapt_flow.qualify.config import (
    QualifyConfigError,
    VerifyConfig,
    load_verify_config,
    parse_verify_config,
)
from openadapt_flow.qualify.environment import WebEnvironmentObserver
from openadapt_flow.qualify.faults import WebFaultDriver

__all__ = [
    "CASES",
    "QualifyConfigError",
    "QualifyError",
    "QualifyHooks",
    "QualifyResult",
    "TrialContext",
    "VerifyConfig",
    "WebEnvironmentObserver",
    "WebFaultDriver",
    "load_verify_config",
    "parse_verify_config",
    "qualify",
]
