# vendored from fd71338669ee:packages/flow/openadapt_flow/connector/executor.py, lines 875-889, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S7 pre-fix: a cancel after the child started is reported as failed (#2300).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional


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
    # vendored: fd71338669ee:packages/flow/openadapt_flow/connector/executor.py 875-889
    # fmt: off
    if job_cancelled(job.lease_job_id):
        status = "failed"
        error_code = "cancelled_by_control_plane"
        outcome_error = "cancelled by the control plane while running"
    elif report_target_kind != job.target_kind:
        status = "failed"
        error_code = "target_kind_mismatch"
        outcome_error = (
            "governed child report execution_target_kind does not match "
            "the signed dispatch target_kind (fail closed)"
        )
    else:
        outcome_error = None
        if status == "failed":
            error_code = "governed_run_failed"
    # fmt: on
    return status
