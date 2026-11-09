"""Verifier audit: does an environment's own scorer accept wrong end states?

A benchmark or RL environment ships a scorer that turns a finished episode
into a reward. This kit tests that scorer, not an agent. It starts from a
reference trajectory that the environment itself counts as correct, applies
mechanical fault families to it (an unauthorized side effect, the wrong
record, a duplicate write, a partial write, a write after the success claim,
a success claim with no write), replays each edited trajectory from a fresh
reset, and asks the environment's own scorer for a verdict.

Ground truth never comes from the scorer. The target adapter reads the end
state through its own path (a database diff, a model dump, a Table API read)
and the kit compares that effect with the effect of the unedited reference.
A fault whose effect matches the reference is degenerate and is excluded. A
fault whose effect differs and that the scorer still accepts is a false
accept, reported as a counterexample with the effect diff. A legitimate
incidental change (an extra read, a repeated lookup) that the scorer rejects
is a false reject.

The kit is the method from "Finding Blind Spots in AppWorld and WorkArena
Task Verifiers" (Abrich, 2026), generalized to any environment that can
reset, replay a trajectory, score, and read back its state. See
``docs/VERIFIER_AUDIT.md``.
"""

from __future__ import annotations

from openadapt_flow.verifier_audit.families import (
    DEFAULT_FAMILIES,
    FaultFamily,
    Polarity,
    families_spec,
)
from openadapt_flow.verifier_audit.model import (
    AuditTarget,
    Call,
    CallKind,
    Episode,
    Verdict,
)
from openadapt_flow.verifier_audit.report import wilson_interval
from openadapt_flow.verifier_audit.runner import AuditConfig, run_audit

__all__ = [
    "AuditConfig",
    "AuditTarget",
    "Call",
    "CallKind",
    "DEFAULT_FAMILIES",
    "Episode",
    "FaultFamily",
    "Polarity",
    "Verdict",
    "families_spec",
    "run_audit",
    "wilson_interval",
]
