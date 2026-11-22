# vendored from 6d7e32b12990:packages/flow/openadapt_flow/connector/executor.py, lines 332-339, for the harness self-test only (re-indented)
# vendored from 6d7e32b12990:packages/flow/openadapt_flow/connector/executor.py, lines 885-909, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S7 post-fix: with no terminal report it halts for reconciliation (#2300 head).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional


# vendored: 6d7e32b12990:packages/flow/openadapt_flow/connector/executor.py 332-339
# fmt: off
def _reported_terminal_outcome(report: dict[str, Any]) -> bool:
    """True when the child's report records how its run ended."""
    return bool(report) and (
        report.get("execution_outcome") is not None
        or report.get("terminal_outcome") is not None
        or report.get("halt") is not None
        or isinstance(report.get("success"), bool)
    )
# fmt: on


def child_status(
    status: str,
    report: dict[str, Any],
    cancelled_flag: bool,
    target_kind_ok: bool = True,
) -> str:
    job = SimpleNamespace(lease_job_id="job", target_kind="kind")
    report_target_kind = "kind" if target_kind_ok else "other"

    def job_cancelled(_job_id: str) -> bool:
        return cancelled_flag

    error_code: Optional[str] = None
    # vendored: 6d7e32b12990:packages/flow/openadapt_flow/connector/executor.py 885-909
    # fmt: off
    cancelled = job_cancelled(job.lease_job_id)
    if cancelled and not _reported_terminal_outcome(report):
        # The cancel terminated a child that had already started, and it
        # left no terminal report: a write may have landed. That is not a
        # failure (a re-run could repeat the write); it stops for
        # reconciliation, like a child whose outcome never came back.
        status = "halt"
        error_code = "child_outcome_unknown"
        outcome_error = (
            "cancelled by the control plane while running; the governed "
            "child left no terminal report, so a write may have landed"
        )
    elif report_target_kind != job.target_kind:
        status = "failed"
        error_code = "target_kind_mismatch"
        outcome_error = (
            "governed child report execution_target_kind does not match "
            "the signed dispatch target_kind (fail closed)"
        )
    else:
        # A cancel that arrived after the child reported does not change
        # what the child did: its own outcome stands.
        outcome_error = None
        if status == "failed":
            error_code = "governed_run_failed"
    # fmt: on
    return status
