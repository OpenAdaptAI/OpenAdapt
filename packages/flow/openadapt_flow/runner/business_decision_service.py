"""Non-actuating customer-runner service for typed business decisions.

This service only projects qualified tasks and stores signed answers. It does
not resume a run, create an executor, observe an application, or actuate it.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.crypto import CryptoError, resolve_key
from openadapt_flow.deployment import load_deployment
from openadapt_flow.hosted import HostedError, resolve_host
from openadapt_flow.interop.business_decision_cloud import (
    BusinessDecisionCloudKeys,
    BusinessDecisionCloudRefused,
    build_qualified_business_decision_cloud_relay,
    refuse_unmatched_business_decision_cloud_answer,
)
from openadapt_flow.interop.business_decision_supervisor import (
    BusinessDecisionSupervisor,
    BusinessDecisionSupervisorReport,
)
from openadapt_flow.interop.decision_relay_transport import (
    HttpxRelayTransport,
    RelayRefused,
    resolve_runner_token,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.policy import Policy, load_policy, policy_contract_sha256
from openadapt_flow.private_file import PrivateFileAclError
from openadapt_flow.private_file import (
    windows_descriptor_has_private_acl as _windows_descriptor_has_private_acl,
)
from openadapt_flow.runner.config import RunnerConfig, RunnerConfigError
from openadapt_flow.runtime.durable.approval import ResumeRefused
from openadapt_flow.runtime.durable.checkpoint import (
    ENC_SUFFIX,
    MANIFEST_FILENAME,
    CheckpointStore,
)

KEY_SCHEMA = "openadapt.business-decision-runner-keys/v1"
DEFAULT_WAIT_S = 25.0
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0


@dataclass(frozen=True)
class BusinessDecisionKeyMaterial:
    """Secret material loaded only from a protected customer-local file."""

    keys: BusinessDecisionCloudKeys
    privacy_key: bytes
    checkpoint_key: Optional[str]


@dataclass(frozen=True)
class BusinessDecisionHealth:
    """PHI-free process health for logs and a service supervisor."""

    state: str
    cycles: int
    consecutive_failures: int
    active_tasks: int
    published: int
    already_published: int
    uncertain: int
    not_projectable: int
    refused: int
    answers_recorded: int
    receipts_confirmed: int
    unmatched_refusals_confirmed: int

    def as_json(self) -> str:
        return json.dumps(
            {
                "schema_version": "openadapt.business-decision-supervisor-health/v1",
                "state": self.state,
                "cycles": self.cycles,
                "consecutive_failures": self.consecutive_failures,
                "active_tasks": self.active_tasks,
                "published": self.published,
                "already_published": self.already_published,
                "uncertain": self.uncertain,
                "not_projectable": self.not_projectable,
                "refused": self.refused,
                "answers_recorded": self.answers_recorded,
                "receipts_confirmed": self.receipts_confirmed,
                "unmatched_refusals_confirmed": self.unmatched_refusals_confirmed,
            },
            sort_keys=True,
        )


def _secret_bytes(raw: object, label: str) -> bytes:
    if not isinstance(raw, str):
        raise RunnerConfigError(f"business decision key {label} is invalid")
    try:
        value = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise RunnerConfigError(f"business decision key {label} is not base64") from exc
    if len(value) < 32:
        raise RunnerConfigError(f"business decision key {label} is too short")
    return value


def _key_id(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not 8 <= len(raw) <= 128:
        raise RunnerConfigError(f"business decision key id {label} is invalid")
    return raw


def load_business_decision_key_material(path: Path) -> BusinessDecisionKeyMaterial:
    """Load a service-identity-owned private key file without a symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise RunnerConfigError(
            "business decision key file cannot be inspected"
        ) from exc
    if stat.S_ISLNK(before.st_mode):
        raise RunnerConfigError("business decision key file must not be a link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RunnerConfigError(
            "business decision key file cannot be opened safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        changed = False
        if os.name == "nt":
            try:
                after = os.lstat(path)
                changed = (
                    stat.S_ISLNK(after.st_mode)
                    or metadata.st_ino != before.st_ino
                    or metadata.st_dev != before.st_dev
                    or after.st_ino != before.st_ino
                    or after.st_dev != before.st_dev
                )
            except OSError:
                changed = True
        unsafe_permissions = os.name != "nt" and (
            not hasattr(os, "geteuid")
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        )
        if os.name == "nt" and not changed:
            try:
                unsafe_permissions = not _windows_descriptor_has_private_acl(descriptor)
            except PrivateFileAclError as exc:
                raise RunnerConfigError(str(exc)) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 64 * 1024
            or unsafe_permissions
            or changed
        ):
            raise RunnerConfigError(
                "business decision key file is not a private regular file"
            )
        encoded = os.read(descriptor, 64 * 1024 + 1)
    finally:
        os.close(descriptor)
    if len(encoded) > 64 * 1024:
        raise RunnerConfigError("business decision key file is too large")
    try:
        raw = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunnerConfigError("business decision key file is not valid JSON") from exc
    required = {
        "schema_version",
        "task_signing_key",
        "task_issuer_key_id",
        "qualification_signing_key",
        "qualification_issuer_key_id",
        "answer_signing_key",
        "answer_issuer_key_id",
        "receipt_signing_key",
        "receipt_issuer_key_id",
        "role_mapping_key",
        "privacy_key",
    }
    allowed = required | {"checkpoint_key"}
    if not isinstance(raw, dict) or set(raw) - allowed or not required <= set(raw):
        raise RunnerConfigError("business decision key file does not match its schema")
    if raw["schema_version"] != KEY_SCHEMA:
        raise RunnerConfigError("business decision key file schema is unsupported")
    keys = BusinessDecisionCloudKeys(
        task_signing_key=_secret_bytes(raw["task_signing_key"], "task"),
        task_issuer_key_id=_key_id(raw["task_issuer_key_id"], "task"),
        qualification_signing_key=_secret_bytes(
            raw["qualification_signing_key"], "qualification"
        ),
        qualification_issuer_key_id=_key_id(
            raw["qualification_issuer_key_id"], "qualification"
        ),
        answer_signing_key=_secret_bytes(raw["answer_signing_key"], "answer"),
        answer_issuer_key_id=_key_id(raw["answer_issuer_key_id"], "answer"),
        receipt_signing_key=_secret_bytes(raw["receipt_signing_key"], "receipt"),
        receipt_issuer_key_id=_key_id(raw["receipt_issuer_key_id"], "receipt"),
        role_mapping_key=_secret_bytes(raw["role_mapping_key"], "role mapping"),
    )
    checkpoint_key = raw.get("checkpoint_key")
    if checkpoint_key is not None and (
        not isinstance(checkpoint_key, str) or not checkpoint_key
    ):
        raise RunnerConfigError("business decision checkpoint key is invalid")
    return BusinessDecisionKeyMaterial(
        keys=keys,
        privacy_key=_secret_bytes(raw["privacy_key"], "privacy"),
        checkpoint_key=checkpoint_key,
    )


def _checkpoint_key(
    material: BusinessDecisionKeyMaterial, _run_dir: Path
) -> Optional[str]:
    return material.checkpoint_key or resolve_key(None)


def _load_exact_run_workflow(
    run_dir: Path,
    *,
    checkpoint_key: Optional[str],
    runner_config: RunnerConfig,
    policy: Policy,
    execution_profile: str,
) -> Workflow:
    checkpoints = CheckpointStore(run_dir, key=checkpoint_key)
    manifest_path = checkpoints.checkpoints_dir / MANIFEST_FILENAME
    encrypted_manifest_path = manifest_path.with_name(manifest_path.name + ENC_SUFFIX)
    try:
        with checkpoints.state_lock():
            manifest_source_encrypted = encrypted_manifest_path.is_file()
            manifest = checkpoints.read_manifest()
            if manifest_source_encrypted != encrypted_manifest_path.is_file():
                raise OSError("the durable manifest changed while it was loaded")
    except (CryptoError, OSError, ValueError) as exc:
        raise BusinessDecisionCloudRefused(
            "the decision run cannot be loaded with the authorized local key"
        ) from exc
    if manifest is None:
        raise BusinessDecisionCloudRefused("the decision run has no durable manifest")
    try:
        checkpoints.validate_namespace(manifest)
    except (OSError, ValueError, ResumeRefused) as exc:
        raise BusinessDecisionCloudRefused(
            "the decision run durable namespace is not authoritative"
        ) from exc
    bundle_dir = Path(manifest.bundle_dir).resolve()
    try:
        workflow = Workflow.load(bundle_dir, key=checkpoint_key)
    except (CryptoError, OSError, ValueError) as exc:
        raise BusinessDecisionCloudRefused(
            "the decision bundle cannot be loaded with the authorized local key"
        ) from exc
    if workflow.manifest is None:
        raise BusinessDecisionCloudRefused("the decision bundle is not sealed")
    trusted = runner_config.bundles.get(workflow.manifest.content_digest)
    if trusted is None or trusted.path.resolve() != bundle_dir:
        raise BusinessDecisionCloudRefused("the decision bundle is not trusted locally")
    if not workflow.encrypted and not trusted.allow_unencrypted:
        raise BusinessDecisionCloudRefused(
            "the trusted bundle requires encryption at rest"
        )
    if not manifest_source_encrypted and not trusted.allow_unencrypted:
        raise BusinessDecisionCloudRefused(
            "the trusted durable manifest requires encryption at rest"
        )
    if trusted.policy != policy.name:
        raise BusinessDecisionCloudRefused(
            "the decision bundle policy differs from runner trust"
        )
    authorization = manifest.governed_authorization
    if (
        execution_profile not in {"demo", "standard", "regulated"}
        or authorization is None
        or authorization.execution_profile != execution_profile
        or authorization.admitted_policy_name != policy.name
        or authorization.admitted_policy_contract_sha256
        != policy_contract_sha256(policy)
    ):
        raise BusinessDecisionCloudRefused(
            "the durable run authorization differs from the selected profile or policy"
        )
    authorization_error = authorization.validate_execution(
        workflow,
        bundle_dir=bundle_dir,
        params=manifest.params,
        worklists=manifest.worklists,
        continuation=True,
    )
    if authorization_error is not None:
        raise BusinessDecisionCloudRefused(
            "the durable run authorization no longer matches the sealed workflow "
            "or its exact runtime inputs"
        )
    return workflow


def build_business_decision_supervisor(
    *,
    runs_root: Path,
    runner_config: RunnerConfig,
    profile: str,
    origin: str,
) -> BusinessDecisionSupervisor:
    """Build a relay-only supervisor from local trusted configuration."""

    if runner_config.business_decisions is None:
        raise RunnerConfigError("[business_decisions] is required for this service")
    profile_path = runner_config.profiles.get(profile)
    if profile_path is None:
        raise RunnerConfigError(f"runner profile {profile!r} is not configured")
    deployment = load_deployment(profile_path)
    remote = deployment.human_decisions.remote
    if not remote.enabled or remote.tenant_id is None or remote.runner_id is None:
        raise RunnerConfigError(
            "remote human decisions are not enabled for this profile"
        )
    policy_source = deployment.policy.policy
    if not policy_source:
        raise RunnerConfigError("the deployment profile must name a policy")
    policy = load_policy(policy_source)
    execution_profile = deployment.runtime.profile
    if execution_profile is None:
        raise RunnerConfigError(
            "the business decision service requires a named execution profile"
        )
    material = load_business_decision_key_material(
        runner_config.business_decisions.key_file
    )
    token = resolve_runner_token()
    transport = HttpxRelayTransport(origin, token)

    def relay_factory(run_dir, store, relay_transport, at):
        checkpoint_key = _checkpoint_key(material, run_dir)
        workflow = _load_exact_run_workflow(
            run_dir,
            checkpoint_key=checkpoint_key,
            runner_config=runner_config,
            policy=policy,
            execution_profile=execution_profile,
        )
        return build_qualified_business_decision_cloud_relay(
            workflow,
            policy,
            store,
            relay_transport,
            runner_token=token,
            tenant_id=remote.tenant_id,
            runner_id=remote.runner_id,
            keys=material.keys,
            privacy_key=material.privacy_key,
            at=at,
        )

    def refuse_unmatched(delivery, at):
        return refuse_unmatched_business_decision_cloud_answer(
            transport,
            delivery,
            runner_token=token,
            tenant_id=remote.tenant_id,
            runner_id=remote.runner_id,
            answer_signing_key=material.keys.answer_signing_key,
            expected_answer_issuer_key_id=material.keys.answer_issuer_key_id,
            receipt_signing_key=material.keys.receipt_signing_key,
            receipt_issuer_key_id=material.keys.receipt_issuer_key_id,
            at=at,
        )

    return BusinessDecisionSupervisor(
        runs_root,
        transport=transport,
        relay_factory=relay_factory,
        checkpoint_key_resolver=lambda run_dir: _checkpoint_key(material, run_dir),
        unmatched_refuser=refuse_unmatched,
    )


class BusinessDecisionServiceLoop:
    """Bounded relay-only loop. It never calls a continuation function."""

    def __init__(
        self, supervisor: BusinessDecisionSupervisor, *, wait_s: float = DEFAULT_WAIT_S
    ) -> None:
        if not 0 <= wait_s <= DEFAULT_WAIT_S:
            raise ValueError("poll wait must be between 0 and 25 seconds")
        self.supervisor = supervisor

        self.wait_s = wait_s
        self.stop_event = threading.Event()
        self.cycles = self.failures = self.answers = self.receipts = self.unmatched = 0
        self.last_report: Optional[BusinessDecisionSupervisorReport] = None

    def serve_once(self) -> BusinessDecisionHealth:
        try:
            report = self.supervisor.serve_once(wait_s=self.wait_s)
        except (BusinessDecisionCloudRefused, RelayRefused, OSError, ValueError):
            self.failures += 1
            return self.health("degraded")
        self.cycles += 1
        self.failures = 0
        self.last_report = report
        self.answers += int(report.answer_recorded)
        self.receipts += int(report.receipt_confirmed)
        self.unmatched += int(report.unmatched_refusal_confirmed)
        return self.health("ready")

    def health(self, state: str) -> BusinessDecisionHealth:
        report = self.last_report
        publish = report.publishes if report is not None else None
        active_tasks = 0
        if publish is not None:
            active_tasks = (
                publish.published
                + publish.already_published
                + publish.uncertain
                + publish.refused
            )
        return BusinessDecisionHealth(
            state=state,
            cycles=self.cycles,
            consecutive_failures=self.failures,
            active_tasks=active_tasks,
            published=0 if publish is None else publish.published,
            already_published=0 if publish is None else publish.already_published,
            uncertain=0 if publish is None else publish.uncertain,
            not_projectable=0 if publish is None else publish.not_projectable,
            refused=0 if publish is None else publish.refused,
            answers_recorded=self.answers,
            receipts_confirmed=self.receipts,
            unmatched_refusals_confirmed=self.unmatched,
        )

    def run(self, emit: Callable[[BusinessDecisionHealth], None]) -> None:
        while not self.stop_event.is_set():
            health = self.serve_once()
            emit(health)
            if health.state == "degraded":
                delay = min(
                    BACKOFF_CAP_S,
                    BACKOFF_BASE_S * 2 ** min(self.failures - 1, 6),
                )
                self.stop_event.wait(delay)
            elif self.wait_s == 0:
                # A zero wait is useful for tests and single-cycle probes. A
                # continuous service must still yield between empty polls.
                self.stop_event.wait(BACKOFF_BASE_S)


def resolve_business_decision_origin(value: Optional[str]) -> str:
    try:
        return resolve_host(value)
    except HostedError as exc:
        raise RunnerConfigError(str(exc)) from exc
