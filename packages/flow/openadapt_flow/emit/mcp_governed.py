"""One governed execution and conservative response boundary for emitted tools.

The shared CLI owns qualification, authority, target and delivery checks. This
adapter binds a fresh child's typed report to its independently captured inputs;
a transported report is not a signed receipt or a new source of authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.run_invocation import derive_context, producer_supported
from openadapt_flow.surface_selection import resolve_backend_config

_MAX_REPORT_BYTES = 20 * 1024 * 1024

SERVER_TEMPLATE = '''\
"""Governed MCP tool. Target and readback come only from --config.

Start with --config deploy.yaml --profile standard and
--qualification-authority-file PATH. The existing CLI verifies that authority;
this server never creates it. Each call has a private params file and fresh run.
The outer result is never production proof. Regulated calls refuse before launch.
"""
from __future__ import annotations

import argparse
from pathlib import Path

{sdk_import}
from openadapt_flow.emit.mcp_governed import run_governed

BUNDLE_DIR = Path(__file__).resolve().parent / {bundle_subdir!r}
BUNDLE_DIGEST = {bundle_digest!r}
WORKFLOW_CONTRACT = {workflow_contract!r}
SURFACE = {surface!r}
DEPLOYMENT = None
PROFILE = None
AUTHORITY_FILE = None
mcp = FastMCP({name!r})


@mcp.tool()
def {func_name}({param_sig_first}url: str | None = None) -> dict:
    """{docstring}

    Args:
{param_docs}        url: Legacy target override. Every non-null value refuses.
    Returns:
        REFUSED means no child ran. Every launched call returns UNCERTAIN,
        with no production eligibility or automatic retry. Fresh bound local
        runtime claims may be reported separately; they are not authenticated
        production evidence. Reconcile actual effects before another attempt.
    """
    return run_governed(
        BUNDLE_DIR, {params_dict}, config=DEPLOYMENT, profile=PROFILE,
        authority_file=AUTHORITY_FILE, expected_bundle_digest=BUNDLE_DIGEST,
        expected_workflow_contract=WORKFLOW_CONTRACT, surface=SURFACE,
        legacy_url=url,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", choices=("standard", "regulated"), required=True)
    parser.add_argument("--qualification-authority-file", required=True)
    args = parser.parse_args()
    DEPLOYMENT = args.config
    PROFILE = args.profile
    AUTHORITY_FILE = args.qualification_authority_file
    mcp.run()
'''


class RuntimeReportedOutcome(BaseModel):
    """An allowlisted local runtime claim, never independent production proof."""

    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal[
        "VERIFIED", "COMPLETED_UNVERIFIED", "HALTED", "FAILED", "ROLLED_BACK"
    ]
    transaction_outcome: (
        Literal[
            "VERIFIED",
            "HALTED_BEFORE_EFFECT",
            "RECONCILIATION_REQUIRED",
            "FAILED_PLATFORM",
            "CANCELED",
            "REJECTED_POLICY",
            "COMPLETED_UNVERIFIED",
            "ROLLED_BACK",
            "SAVED_AWAITING_CHECK",
        ]
        | None
    )
    success: bool
    production_eligible: bool
    execution_completed: bool

    @model_validator(mode="after")
    def _consistent(self):
        if self.success != (self.outcome == "VERIFIED"):
            raise ValueError("inconsistent local runtime completion claim")
        if self.production_eligible and not (
            self.success
            and self.execution_completed
            and self.transaction_outcome == "VERIFIED"
        ):
            raise ValueError("inconsistent local runtime eligibility claim")
        return self


class MCPObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    success: Literal[False] = False
    production_eligible: Literal[False] = False
    retry_allowed: Literal[False] = False
    outcome: Literal["REFUSED", "UNCERTAIN"]
    transaction_outcome: Literal["RECONCILIATION_REQUIRED"] | None
    execution_may_have_occurred: bool
    producer_assurance: Literal[
        "NOT_LAUNCHED", "UNATTRIBUTED", "LOCAL_OWNED_PROCESS_UNAUTHENTICATED"
    ]
    run_dir: str | None = None
    runtime_reported: RuntimeReportedOutcome | None = None
    detail: str

    @field_validator("success", "production_eligible", "retry_allowed", mode="before")
    @classmethod
    def _false_only(cls, value):
        if value is not False:
            raise ValueError("outer production and retry claims are unavailable")
        return value

    @model_validator(mode="after")
    def _phase(self):
        refused = self.outcome == "REFUSED"
        if refused != (not self.execution_may_have_occurred):
            raise ValueError("observation delivery phase mismatch")
        if refused != (self.producer_assurance == "NOT_LAUNCHED"):
            raise ValueError("observation assurance phase mismatch")
        if refused != (self.transaction_outcome is None):
            raise ValueError("observation transaction phase mismatch")
        if (self.runtime_reported is not None) != (
            self.producer_assurance == "LOCAL_OWNED_PROCESS_UNAUTHENTICATED"
        ):
            raise ValueError("runtime claim attribution mismatch")
        return self


def _refused() -> dict[str, Any]:
    return MCPObservation(
        outcome="REFUSED",
        transaction_outcome=None,
        execution_may_have_occurred=False,
        producer_assurance="NOT_LAUNCHED",
        detail="No child ran. Only a serial Standard local invocation with a pinned "
        "deployment (--config deploy.yaml) and existing qualification authority "
        "is supported. Regulated "
        "and independent production-report assurance are unavailable. Per-call "
        "URL overrides and unsupported producer paths are refused.",
    ).model_dump(mode="json")


def _uncertain(run: Path, report: RunReport | None = None) -> dict[str, Any]:
    claim = None
    if (
        report is not None
        and report.execution_outcome is not None
        and report.execution_completed is not None
    ):
        claim = RuntimeReportedOutcome(
            outcome=report.execution_outcome,
            transaction_outcome=report.transaction_outcome,
            success=report.success,
            production_eligible=report.production_eligible,
            execution_completed=report.execution_completed,
        )
    return MCPObservation(
        outcome="UNCERTAIN",
        transaction_outcome="RECONCILIATION_REQUIRED",
        execution_may_have_occurred=True,
        run_dir=str(run),
        runtime_reported=claim,
        producer_assurance=(
            "UNATTRIBUTED" if claim is None else "LOCAL_OWNED_PROCESS_UNAUTHENTICATED"
        ),
        detail="The child may have executed. Its local report is not independently "
        "authenticated production evidence. Reconcile actual effects before another "
        "attempt; do not retry automatically.",
    ).model_dump(mode="json")


def _directory_identity(run: Path) -> tuple[int, int]:
    observed = run.lstat()
    if not stat.S_ISDIR(observed.st_mode) or not observed.st_ino:
        raise ValueError("run is not the owned directory")
    return observed.st_dev, observed.st_ino


def _file_identity(observed: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _report_bytes(
    run: Path,
    directory_identity: tuple[int, int] | None = None,
) -> bytes:
    from openadapt_flow import artifacts
    from openadapt_flow.private_file import open_binary

    expected = directory_identity or _directory_identity(run)
    if _directory_identity(run) != expected:
        raise ValueError("run directory was replaced")
    if artifacts.is_sealed(run):
        # Existing sealed-artifact reader; no plaintext/decryption fallback.
        data = artifacts.open_read(run / "report.json")
    else:
        path = run / "report.json"
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("regular report required")
        fd = open_binary(
            str(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(
                opened
            ) != _file_identity(before):
                raise ValueError("report entry changed during open")
            data = handle.read(_MAX_REPORT_BYTES + 1)
            after = os.fstat(handle.fileno())
        if _file_identity(before) != _file_identity(after) or _file_identity(
            after
        ) != _file_identity(path.lstat()):
            raise ValueError("report changed during read")
    if len(data) > _MAX_REPORT_BYTES or _directory_identity(run) != expected:
        raise ValueError("oversized or replaced report occurrence")
    return data


def _typed_report(data: bytes) -> RunReport:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("ambiguous report")
            result[key] = value
        return result

    def number(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("nonfinite report")
        return value

    def constant(_text: str) -> Any:
        raise ValueError("nonfinite report")

    json.loads(
        data, object_pairs_hook=pairs, parse_float=number, parse_constant=constant
    )
    return RunReport.model_validate_json(data, strict=True)


def run_governed(
    bundle: Path,
    params: dict[str, str],
    *,
    config: str | None,
    profile: str | None,
    authority_file: str | None,
    expected_bundle_digest: str,
    expected_workflow_contract: str,
    surface: str,
    legacy_url: str | None = None,
) -> dict[str, Any]:
    """Launch one pinned governed child, with no retry or direct-actuation path."""
    if (
        legacy_url is not None
        or profile != "standard"
        or not config
        or not authority_file
    ):
        return _refused()
    try:
        import yaml

        from openadapt_flow.deployment import (
            DeploymentConfig,
            incomplete_deployment_reasons,
        )
        from openadapt_flow.qualification import workflow_contract_sha256
        from openadapt_flow.runtime.authorization import runtime_inputs_digest

        params = dict(params)
        # Parse the exact captured config through the existing typed contract.
        # The child pins these bytes independently via its existing CLI guard.
        config_bytes = Path(config).read_bytes()
        cfg = DeploymentConfig.model_validate(yaml.safe_load(config_bytes) or {})
        if incomplete_deployment_reasons(cfg) or cfg.runtime.profile not in {
            None,
            profile,
        }:
            return _refused()
        if "kind" not in cfg.backend.model_fields_set or cfg.backend.kind != surface:
            return _refused()
        target = cfg.backend.url or cfg.backend.browser_page_url
        if surface == "web" and not target:
            return _refused()
        if not Path(authority_file).is_file():
            return _refused()
        workflow = Workflow.load(bundle)
        if (
            workflow.manifest is None
            or workflow.manifest.content_digest != expected_bundle_digest
            or workflow_contract_sha256(workflow) != expected_workflow_contract
        ):
            return _refused()
        startup = Namespace(
            profile=profile,
            config=config,
            config_sha256=hashlib.sha256(config_bytes).hexdigest(),
        )
        if not producer_supported(startup, cfg, workflow):
            return _refused()
        backend = resolve_backend_config(Namespace(), cfg, workflow)
        challenge = secrets.token_hex(32)
        expected_context = derive_context(
            challenge,
            cfg=cfg,
            backend=backend,
            effects=cfg.effects,
            actuation=cfg.actuation,
            profile=profile,
        )
        inputs_digest = runtime_inputs_digest(workflow, params, {})
        work = Path(tempfile.mkdtemp(prefix="openadapt_flow_mcp_"))
        run = work / "run"
        run.mkdir(mode=0o700)
        directory_identity = _directory_identity(run)
        params_file = work / "params.json"
        fd = os.open(params_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            if sys.platform == "win32":
                from openadapt_flow.private_file import (
                    windows_descriptor_has_private_acl,
                )

                if not windows_descriptor_has_private_acl(handle.fileno()):
                    raise ValueError("private parameter file ACL required")
            json.dump(params, handle, allow_nan=False)
        argv = [
            sys.executable,
            "-m",
            "openadapt_flow",
            "run",
            str(bundle),
            "--config",
            str(config),
            "--config-sha256",
            hashlib.sha256(config_bytes).hexdigest(),
            "--profile",
            profile,
            "--invocation-challenge",
            challenge,
            "--qualification-authority-file",
            str(authority_file),
            "--pin-digest",
            expected_bundle_digest,
            "--run-dir",
            str(run),
            "--params-file",
            str(params_file),
        ]
    except Exception:
        return _refused()
    # First possible child delivery boundary. Every failure after it is uncertain.
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            return _uncertain(run)
        report = _typed_report(_report_bytes(run, directory_identity))
        envelope = report.outcome_envelope
        if (
            envelope is None
            or report.invocation_context != expected_context
            or report.execution_profile != profile
            or report.bundle_content_digest != expected_bundle_digest
            or report.workflow_contract_sha256 != expected_workflow_contract
            or envelope.workflow_contract_sha256 != expected_workflow_contract
            or report.governed_runtime_inputs_digest != inputs_digest
            or not report.governed_authorization_id
            or report.execution_target_kind != surface
            or report.qualification_evidence_only
        ):
            return _uncertain(run)
        if surface == "web":
            if not isinstance(target, str):
                return _uncertain(run)
            parts = urlsplit(target)
            if (
                report.execution_entry_url != target
                or report.execution_origin != f"{parts.scheme}://{parts.netloc}"
            ):
                return _uncertain(run)
        # Correlation is local attribution only. No pinned producer/report
        # authentication contract exists here, even for sealed-set artifacts.
        return _uncertain(run, report)

    except Exception:
        return _uncertain(run)
    finally:
        try:
            params_file.unlink(missing_ok=True)
        except OSError:
            pass  # Private owner-only evidence may remain; preserve delivery status.
