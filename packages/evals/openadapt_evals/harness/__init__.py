"""Lightweight meta-benchmark harness: ONE ``Environment`` + ONE metrics row.

A **consolidation over existing openadapt-evals code**, not new infrastructure
and not an external harness. It unifies the repo's three parallel abstractions
-- ``BenchmarkAdapter`` (adapters), ``TaskVerifierRegistry`` (evaluation), and
the flow ``EffectVerifier`` -- behind a single
:class:`~openadapt_evals.harness.protocol.Environment` protocol whose
:meth:`~openadapt_evals.harness.protocol.Environment.verify` folds all three
scoring paths into one call, and drives them with one
:func:`~openadapt_evals.harness.runner.run_meta` that emits one JSONL metrics
row per ``(env, task, mode)``.

Surface:

- Protocol + task: :class:`Environment`, :class:`MetaTask`, :class:`Observation`,
  :class:`VerificationResult`.
- Conforming shims over existing adapters:
  :class:`BenchmarkAdapterEnvironment` (WAA live/mock, Local),
  :class:`MockMedAdapter`, :class:`OpenEMRAdapter`.
- Runner + policies: :func:`run_meta`, :func:`run_meta_suite`,
  :class:`MetaMetricsRow`, :class:`PolicyOutcome`, :func:`make_replay_policy`,
  :func:`make_hybrid_policy`, :func:`make_agent_policy`.
- Inspect eval-log export: :func:`to_inspect_eval_log`,
  :func:`from_inspect_eval_log`, :func:`write_inspect_eval_log`.
- Phase-2 stubs (NotImplementedError): :class:`OSWorldAdapter`,
  :class:`BrowserGymAdapter`.

Submodules are imported lazily (PEP 562) so importing this package never pulls
in ``openadapt_flow``, a browser, or a VM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    # protocol
    "Environment",
    "MetaTask",
    "Observation",
    "VerificationResult",
    # adapters (conforming shims)
    "BenchmarkAdapterEnvironment",
    "MockMedAdapter",
    "OpenEMRAdapter",
    # runner + policies
    "run_meta",
    "run_meta_suite",
    "MetaMetricsRow",
    "PolicyOutcome",
    "make_replay_policy",
    "make_hybrid_policy",
    "make_agent_policy",
    # inspect export
    "to_inspect_eval_log",
    "from_inspect_eval_log",
    "write_inspect_eval_log",
    # phase-2 stubs
    "OSWorldAdapter",
    "BrowserGymAdapter",
]

_EXPORTS = {
    "Environment": ("openadapt_evals.harness.protocol", "Environment"),
    "MetaTask": ("openadapt_evals.harness.protocol", "MetaTask"),
    "Observation": ("openadapt_evals.harness.protocol", "Observation"),
    "VerificationResult": ("openadapt_evals.harness.protocol", "VerificationResult"),
    "BenchmarkAdapterEnvironment": (
        "openadapt_evals.harness.adapters",
        "BenchmarkAdapterEnvironment",
    ),
    "MockMedAdapter": ("openadapt_evals.harness.adapters", "MockMedAdapter"),
    "OpenEMRAdapter": ("openadapt_evals.harness.adapters", "OpenEMRAdapter"),
    "run_meta": ("openadapt_evals.harness.runner", "run_meta"),
    "run_meta_suite": ("openadapt_evals.harness.runner", "run_meta_suite"),
    "MetaMetricsRow": ("openadapt_evals.harness.runner", "MetaMetricsRow"),
    "PolicyOutcome": ("openadapt_evals.harness.runner", "PolicyOutcome"),
    "make_replay_policy": ("openadapt_evals.harness.runner", "make_replay_policy"),
    "make_hybrid_policy": ("openadapt_evals.harness.runner", "make_hybrid_policy"),
    "make_agent_policy": ("openadapt_evals.harness.runner", "make_agent_policy"),
    "to_inspect_eval_log": (
        "openadapt_evals.harness.inspect_export",
        "to_inspect_eval_log",
    ),
    "from_inspect_eval_log": (
        "openadapt_evals.harness.inspect_export",
        "from_inspect_eval_log",
    ),
    "write_inspect_eval_log": (
        "openadapt_evals.harness.inspect_export",
        "write_inspect_eval_log",
    ),
    "OSWorldAdapter": ("openadapt_evals.harness.external", "OSWorldAdapter"),
    "BrowserGymAdapter": ("openadapt_evals.harness.external", "BrowserGymAdapter"),
}

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_evals.harness.adapters import (
        BenchmarkAdapterEnvironment,
        MockMedAdapter,
        OpenEMRAdapter,
    )
    from openadapt_evals.harness.external import BrowserGymAdapter, OSWorldAdapter
    from openadapt_evals.harness.inspect_export import (
        from_inspect_eval_log,
        to_inspect_eval_log,
        write_inspect_eval_log,
    )
    from openadapt_evals.harness.protocol import (
        Environment,
        MetaTask,
        Observation,
        VerificationResult,
    )
    from openadapt_evals.harness.runner import (
        MetaMetricsRow,
        PolicyOutcome,
        make_agent_policy,
        make_hybrid_policy,
        make_replay_policy,
        run_meta,
        run_meta_suite,
    )


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    return getattr(module, target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
