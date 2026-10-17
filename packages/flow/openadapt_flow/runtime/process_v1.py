"""Runtime for the mixed-capability ``openadapt.process-contract/v1``.

V1 is a small extension of ProcessContract.  It keeps the existing admitted
Flow child, adds an admitted Python child and a typed human child, and moves
cross-child values onto verified content-addressed artifact edges.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from openadapt_types.authentication import (
    AuthenticationReceiptV1,
    AuthenticationRunBindingV1,
    AuthenticationTaskContractV1,
    validate_authentication_receipt,
)
from openadapt_types.execute import ExecuteTerminalOutcomeV1
from openadapt_types.human_decision import (
    HumanDecisionReceiptV1,
    HumanDecisionTaskV1,
    sign_human_decision_task_hmac,
)
from openadapt_types.process_capability import (
    ArtifactDataClassification,
    ArtifactRefV1,
    ArtifactStorageBoundary,
    ArtifactVerificationState,
    CodeCapabilityAdmissionEnvelopeV1,
    CodeCapabilityManifestV1,
    ProcessEvidenceReceiptV1,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.admitted_composition import (
    AdmittedChildSpec,
    CodeChildSpec,
    HumanChildSpec,
    ProcessContract,
    ProcessContractError,
    live_bundle_content_digest,
    load_child_envelope,
    predecessor_map,
    resolve_pointer,
    topological_order,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification_admission import (
    QualificationAdmissionError,
    QualificationSignerTrust,
    expected_from_payload,
    verify_qualification_admission,
)
from openadapt_flow.runtime.admitted_composition import (
    AdmittedCapability,
    AdmittedChildExecutor,
    execute,
)


class ProcessV1Error(ProcessContractError):
    """The mixed-capability process cannot continue safely."""


def _canonical(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ProcessV1Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child: str
    kind: str
    outcome: str
    receipt_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    oracle_tier: int = Field(default=0, ge=0, le=3)
    model_used: bool = False
    external_network_used: bool = False


class _ArtifactVerificationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.artifact-verification/v1"]
    outcome: Literal["verified"]
    oracle_tier: Literal[2, 3]
    artifact_digests: tuple[str, ...] = ()
    child_receipt_digests: tuple[str, ...] = ()

    @field_validator("artifact_digests", "child_receipt_digests")
    @classmethod
    def _exact_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("verification digests must be unique")
        if any(
            len(value) != 71
            or not value.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in value[7:])
            for value in values
        ):
            raise ValueError("verification contains an invalid digest")
        return tuple(sorted(values))


class ProcessV1State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "openadapt.process-execution/v1"
    process_execution_id: str
    process_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    status: str = "running"
    outcome: str | None = None
    event_head: str | None = None
    completed: dict[str, ProcessV1Step] = Field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    waiting_child: str | None = None
    human_receipt_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class HumanCompletionV1:
    receipt: HumanDecisionReceiptV1
    authentication_binding: AuthenticationRunBindingV1 | None = None
    authentication_receipt: AuthenticationReceiptV1 | None = None


class HumanCompletionProviderV1(Protocol):
    def __call__(
        self,
        child: HumanChildSpec,
        task: HumanDecisionTaskV1,
        authentication: AuthenticationTaskContractV1 | None,
        request_dir: Path,
    ) -> HumanCompletionV1 | None: ...


@dataclass(frozen=True)
class ProcessV1Result:
    state: ProcessV1State
    state_path: Path
    receipt_path: Path | None


def _completion_file(request_dir: Path) -> HumanCompletionV1 | None:
    """Load the closed local envelope written by a trusted attended adapter."""

    path = request_dir / "human-completion.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) - {
            "schema_version",
            "receipt",
            "authentication_binding",
            "authentication_receipt",
        }:
            raise ValueError("unknown completion fields")
        if raw.get("schema_version") != "openadapt.process-human-completion/v1":
            raise ValueError("unsupported completion schema")
        receipt = HumanDecisionReceiptV1.model_validate(raw.get("receipt"))
        binding_raw = raw.get("authentication_binding")
        auth_receipt_raw = raw.get("authentication_receipt")
        binding = (
            AuthenticationRunBindingV1.model_validate(binding_raw)
            if binding_raw is not None
            else None
        )
        auth_receipt = (
            AuthenticationReceiptV1.model_validate(auth_receipt_raw)
            if auth_receipt_raw is not None
            else None
        )
    except (OSError, ValueError) as exc:
        raise ProcessV1Error(
            f"the trusted human completion envelope is invalid: {exc}"
        ) from exc
    return HumanCompletionV1(
        receipt=receipt,
        authentication_binding=binding,
        authentication_receipt=auth_receipt,
    )


def _process_digest(contract: ProcessContract, parent: Path) -> str:
    """Seal the contract and each referenced file into one package digest."""

    inventory: list[dict[str, str]] = []
    try:
        for flow_child in contract.children:
            bundle = resolve_pointer(parent, flow_child.bundle)
            workflow = Workflow.load(bundle)
            live_bundle_digest = live_bundle_content_digest(workflow, bundle)
            if live_bundle_digest != flow_child.bundle_content_digest:
                raise ProcessV1Error(
                    f"Flow child {flow_child.name!r} bundle digest differs"
                )
            inventory.append(
                {
                    "kind": "flow_admission",
                    "child": flow_child.name,
                    "pointer": flow_child.envelope,
                    "digest": _file_digest(
                        resolve_pointer(parent, flow_child.envelope)
                    ),
                }
            )
            inventory.append(
                {
                    "kind": "flow_bundle",
                    "child": flow_child.name,
                    "pointer": flow_child.bundle,
                    "digest": f"sha256:{live_bundle_digest}",
                }
            )
        for code_child in contract.code_children:
            for kind, pointer in (
                ("code_manifest", code_child.manifest),
                ("code_admission", code_child.admission),
                ("code_source", code_child.source_archive),
            ):
                inventory.append(
                    {
                        "kind": kind,
                        "child": code_child.name,
                        "pointer": pointer,
                        "digest": _file_digest(parent / pointer),
                    }
                )
        for human_child in contract.human_children:
            if human_child.authentication_template:
                inventory.append(
                    {
                        "kind": "authentication_template",
                        "child": human_child.name,
                        "pointer": human_child.authentication_template,
                        "digest": _file_digest(
                            parent / human_child.authentication_template
                        ),
                    }
                )
    except ProcessV1Error:
        raise
    except (OSError, ValueError) as exc:
        raise ProcessV1Error(
            f"a ProcessContract package reference is unreadable: {exc}"
        ) from exc

    def inventory_key(item: dict[str, str]) -> tuple[str, str, str]:
        return item["kind"], item["child"], item["pointer"]

    sorted_inventory = sorted(inventory, key=inventory_key)
    payload: dict[str, Any] = {
        "schema_version": "openadapt.process-package/v1",
        "contract": contract.model_dump(mode="json"),
        "inventory": sorted_inventory,
    }
    return _digest(_canonical(payload))


def _append_event(run: Path, state: ProcessV1State, value: Mapping[str, Any]) -> None:
    journal = run / "process-events.jsonl"
    record = {
        "schema_version": "openadapt.process-event/v1",
        "sequence": len(journal.read_bytes().splitlines()) + 1
        if journal.is_file()
        else 1,
        "previous_digest": state.event_head,
        "recorded_at": _utc(),
        **value,
    }
    digest = _digest(_canonical(record))
    with journal.open("ab") as handle:
        handle.write(_canonical({**record, "digest": digest}) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    state.event_head = digest


def _verify_events(run: Path, expected: str | None) -> None:
    journal = run / "process-events.jsonl"
    previous = None
    if journal.is_file():
        for sequence, line in enumerate(journal.read_bytes().splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProcessV1Error("the process event journal is invalid") from exc
            digest = record.pop("digest", None)
            if (
                record.get("sequence") != sequence
                or record.get("previous_digest") != previous
            ):
                raise ProcessV1Error("the process event journal order does not verify")
            if digest != _digest(_canonical(record)):
                raise ProcessV1Error("the process event journal digest does not verify")
            previous = digest
    if previous != expected:
        raise ProcessV1Error("the process state does not bind the event journal head")


def _state(contract: ProcessContract, parent: Path, run: Path) -> ProcessV1State:
    path = run / "process-execution.json"
    digest = _process_digest(contract, parent)
    if path.is_file():
        state = ProcessV1State.model_validate_json(path.read_text(encoding="utf-8"))
        if state.process_digest != digest:
            raise ProcessV1Error("the ProcessContract changed after execution started")
        _verify_events(run, state.event_head)
        return state
    run.mkdir(parents=True, exist_ok=True)
    state = ProcessV1State(
        process_execution_id=f"process-{secrets.token_hex(16)}",
        process_digest=digest,
    )
    _append_event(run, state, {"event": "process_started"})
    _write_json(path, state.model_dump(mode="json"))
    return state


def _save(run: Path, state: ProcessV1State) -> None:
    _write_json(run / "process-execution.json", state.model_dump(mode="json"))


def _verified_terminal_receipt(
    state: ProcessV1State, run: Path, private_key: bytes
) -> Path:
    path = run / "process-evidence-receipt.json"
    if not path.is_file():
        raise ProcessV1Error("a terminal process has no evidence receipt")
    try:
        receipt = ProcessEvidenceReceiptV1.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        public_key = Ed25519PrivateKey.from_private_bytes(private_key).public_key()
        public_key.verify(
            base64.b64decode(receipt.signature, validate=True),
            _canonical(receipt.unsigned_payload()),
        )
    except Exception as exc:
        raise ProcessV1Error(
            "the terminal process evidence receipt is invalid"
        ) from exc
    if (
        receipt.process_execution_id != state.process_execution_id
        or receipt.process_digest != state.process_digest
        or receipt.outcome.value != state.outcome
        or receipt.evidence_root_digest != state.event_head
    ):
        raise ProcessV1Error("the terminal receipt does not bind the process state")
    return path


def _verify_code_admission(
    envelope: CodeCapabilityAdmissionEnvelopeV1,
    *,
    trusted_signers: Mapping[str, bytes],
    revoked: set[str],
) -> None:
    payload = envelope.payload
    if payload.admission_id in revoked:
        raise ProcessV1Error("the code admission is revoked")
    now = datetime.now(timezone.utc)
    not_before = datetime.fromisoformat(payload.not_before.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(payload.expires_at.replace("Z", "+00:00"))
    if now < not_before or now >= expires:
        raise ProcessV1Error("the code admission is not live")
    public = trusted_signers.get(payload.issuer_key_id)
    if public is None:
        raise ProcessV1Error("the code admission signer is not trusted")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            base64.b64decode(envelope.signature, validate=True),
            payload.canonical_bytes(),
        )
    except Exception as exc:
        raise ProcessV1Error("the code admission signature is invalid") from exc


def _extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            path = PurePosixPath(item.filename)
            mode = (item.external_attr >> 16) & 0o170000
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                raise ProcessV1Error("the code archive contains an unsafe path")
            if mode == 0o120000:
                raise ProcessV1Error("the code archive contains a symbolic link")
        source.extractall(destination)


def _execute_code(
    parent: Path,
    child: CodeChildSpec,
    inputs: Mapping[str, Any],
    run: Path,
    *,
    trusted_signers: Mapping[str, bytes],
    revoked: set[str],
    runtime_environment_digest: str,
    allow_trusted_code: bool,
) -> tuple[ProcessV1Step, list[dict[str, Any]], dict[str, Any] | None]:
    try:
        manifest = CodeCapabilityManifestV1.model_validate_json(
            (parent / child.manifest).read_text(encoding="utf-8")
        )
        admission = CodeCapabilityAdmissionEnvelopeV1.model_validate_json(
            (parent / child.admission).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ProcessV1Error(
            f"code child {child.name!r} has invalid contracts"
        ) from exc
    archive = parent / child.source_archive
    if not archive.is_file() or _file_digest(archive) != manifest.source_archive_digest:
        raise ProcessV1Error(f"code child {child.name!r} source digest differs")
    payload = admission.payload
    bindings = {
        "capability_id": manifest.capability_id,
        "capability_version_id": manifest.capability_version_id,
        "manifest_digest": manifest.digest,
        "permission_contract_digest": manifest.permissions.digest,
        "input_schema_digest": manifest.input_schema_digest,
        "output_schema_digest": manifest.output_schema_digest,
        "effect_contract_digest": manifest.effect_contract_digest,
        "oracle_contract_digest": manifest.oracle_contract_digest,
        "qualification_campaign_digest": manifest.qualification_campaign_digest,
        "runtime_environment_digest": runtime_environment_digest,
    }
    if any(getattr(payload, key) != value for key, value in bindings.items()):
        raise ProcessV1Error(f"code child {child.name!r} admission binding differs")
    _verify_code_admission(admission, trusted_signers=trusted_signers, revoked=revoked)
    if manifest.permissions.isolation_profile.value != "trusted_local":
        raise ProcessV1Error("this runner implements only trusted_local code")
    if not allow_trusted_code:
        raise ProcessV1Error("trusted_local code requires explicit operator approval")
    expected_python = ".".join(manifest.runtime_version.split(".")[:2])
    if expected_python != f"{sys.version_info.major}.{sys.version_info.minor}":
        raise ProcessV1Error("the admitted Python runtime version differs")

    run.mkdir(parents=True, exist_ok=True)
    output = run / f"outputs-{secrets.token_hex(8)}"
    output.mkdir()
    input_path = run / "input.json"
    _write_json(input_path, dict(inputs))
    with tempfile.TemporaryDirectory(prefix="openadapt-process-code-") as temporary:
        root = Path(temporary)
        _extract(archive, root)
        lockfile = root / manifest.lockfile_path
        if not lockfile.is_file() or _file_digest(lockfile) != manifest.lockfile_digest:
            raise ProcessV1Error("the code lockfile digest differs")
        entrypoint = root / manifest.entrypoint[0]
        if not entrypoint.is_file():
            raise ProcessV1Error("the code entrypoint is missing")
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(entrypoint), *manifest.entrypoint[1:]],
                cwd=root,
                env={
                    "OPENADAPT_PROCESS_INPUT": str(input_path),
                    "OPENADAPT_PROCESS_OUTPUT": str(output),
                    "PYTHONIOENCODING": "utf-8",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=manifest.permissions.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessV1Error(
                f"code child {child.name!r} exceeded its timeout"
            ) from exc
    limit = manifest.permissions.output_limit_bytes
    (run / "stdout.bin").write_bytes(completed.stdout[:limit])
    (run / "stderr.bin").write_bytes(completed.stderr[:limit])
    if completed.returncode:
        raise ProcessV1Error(f"code child {child.name!r} failed")

    artifacts: list[dict[str, Any]] = []
    for declared in manifest.outputs:
        path = output / declared.relative_path
        if not path.is_file():
            if declared.required:
                raise ProcessV1Error(f"code output {declared.name!r} is missing")
            continue
        if path.stat().st_size > limit:
            raise ProcessV1Error(f"code output {declared.name!r} exceeds its limit")
        digest = _file_digest(path)
        artifacts.append(
            {
                "artifact_id": f"artifact-{digest.removeprefix('sha256:')}",
                "content_digest": digest,
                "size_bytes": path.stat().st_size,
                "media_type": declared.media_type,
                "logical_name": declared.name,
                "producer": child.name,
                "source_path": str(path),
            }
        )
    verification = None
    if child.role == "verifier":
        path = output / "verification.json"
        if not path.is_file():
            raise ProcessV1Error("a verifier child did not emit verification.json")
        try:
            parsed_verification = _ArtifactVerificationV1.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except ValueError as exc:
            raise ProcessV1Error(
                "the artifact verifier did not verify its inputs"
            ) from exc
        verification = parsed_verification.model_dump(mode="json")
    receipt_payload = {
        "child": child.name,
        "manifest_digest": manifest.digest,
        "admission_digest": admission.artifact_digest,
        "artifact_digests": sorted(item["content_digest"] for item in artifacts),
        "verification": verification,
        "finished_at": _utc(),
    }
    receipt_digest = _digest(_canonical(receipt_payload))
    _write_json(
        run / "code-receipt.json", {**receipt_payload, "receipt_digest": receipt_digest}
    )
    return (
        ProcessV1Step(
            child=child.name,
            kind="code",
            outcome="verified" if child.role == "verifier" else "completed_unverified",
            receipt_digest=receipt_digest,
            oracle_tier=(
                int(verification["oracle_tier"]) if verification is not None else 0
            ),
            external_network_used=manifest.permissions.network_mode.value != "none",
        ),
        artifacts,
        verification,
    )


def _human_key(run: Path) -> bytes:
    path = run / ".human-task.key"
    if path.is_file():
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ProcessV1Error("the human task key permissions are too broad")
        key = path.read_bytes()
        if len(key) != 32:
            raise ProcessV1Error("the human task key is invalid")
        return key
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def _issue_human(
    contract: ProcessContract,
    state: ProcessV1State,
    child: HumanChildSpec,
    parent: Path,
    run: Path,
) -> tuple[HumanDecisionTaskV1, AuthenticationTaskContractV1 | None, str]:
    request = run / "human" / child.name
    request.mkdir(parents=True, exist_ok=True)
    challenge = _digest(secrets.token_bytes(32))
    task = sign_human_decision_task_hmac(
        key=_human_key(run),
        fields={
            "task_id": f"task-{secrets.token_hex(16)}",
            "task_revision": 1,
            "tenant_id": None,
            "runner_id": "runner-local",
            "run_id": state.process_execution_id,
            "pause_id": f"pause-{secrets.token_hex(16)}",
            "capability_digest": state.process_digest,
            "bundle_digest": state.process_digest,
            "task_kind": "human_step",
            "delivery_state": "not_delivered",
            "risk_class": child.risk_class,
            "substrate": child.substrate,
            "question": {"template": "complete_human_step", "safe_slots": {}},
            "evidence": {"sensitive_evidence_local_only": True},
            "allowed_actions": ("verify_and_resume", "reject", "escalate"),
            "required_authn": child.required_authn,
            "created_at": _utc(),
            "expires_at": datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + 3600, timezone.utc
            )
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "nonce": f"nonce-{secrets.token_hex(16)}",
            "issuer_key_id": "runner-human-task-key",
        },
    )
    authentication = None
    if child.authentication_template:
        template = json.loads(
            (parent / child.authentication_template).read_text(encoding="utf-8")
        )
        if not isinstance(template, dict):
            raise ProcessV1Error("the authentication template must be an object")
        forbidden = {"task_id", "human_decision_task_digest", "schema_version"} & set(
            template
        )
        if forbidden:
            raise ProcessV1Error(
                "the authentication template contains live binding fields"
            )
        authentication = AuthenticationTaskContractV1(
            task_id=f"auth-{secrets.token_hex(16)}",
            human_decision_task_digest=task.digest,
            **template,
        )
    _write_json(request / "human-task.json", task.model_dump(mode="json"))
    if authentication is not None:
        _write_json(
            request / "authentication-task.json",
            authentication.model_dump(mode="json"),
        )
    _write_json(
        request / "run-binding.json",
        {
            "app_version_digest": state.process_digest,
            "process_execution_id": state.process_execution_id,
            "step_id": child.name,
            "challenge_digest": challenge,
        },
    )
    return task, authentication, challenge


def _load_human(
    child: HumanChildSpec, run: Path
) -> tuple[HumanDecisionTaskV1, AuthenticationTaskContractV1 | None, str]:
    request = run / "human" / child.name
    task = HumanDecisionTaskV1.model_validate_json(
        (request / "human-task.json").read_text(encoding="utf-8")
    )
    auth_path = request / "authentication-task.json"
    authentication = (
        AuthenticationTaskContractV1.model_validate_json(
            auth_path.read_text(encoding="utf-8")
        )
        if auth_path.is_file()
        else None
    )
    binding = json.loads((request / "run-binding.json").read_text(encoding="utf-8"))
    return task, authentication, str(binding["challenge_digest"])


def _accept_human(
    state: ProcessV1State,
    child: HumanChildSpec,
    task: HumanDecisionTaskV1,
    authentication: AuthenticationTaskContractV1 | None,
    challenge: str,
    completion: HumanCompletionV1,
    run: Path,
) -> ProcessV1Step:
    receipt = completion.receipt
    if not receipt.verify_hmac(_human_key(run)):
        raise ProcessV1Error("the human receipt signature is invalid")
    expected_request_digest = _digest(
        _canonical(
            {
                "task_digest": task.digest,
                "process_digest": state.process_digest,
                "child": child.name,
            }
        )
    )
    if (
        receipt.task_id != task.task_id
        or receipt.task_revision != task.task_revision
        or receipt.pause_id != task.pause_id
        or receipt.capability_digest != state.process_digest
        or receipt.request_digest != expected_request_digest
        or receipt.action not in task.allowed_actions
    ):
        raise ProcessV1Error("the human receipt does not bind this pause")
    now = datetime.now(timezone.utc)
    decided = datetime.fromisoformat(receipt.decided_at.replace("Z", "+00:00"))
    created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(task.expires_at.replace("Z", "+00:00"))
    if decided < created or decided > expires or now > expires:
        raise ProcessV1Error("the human receipt is outside the task validity window")
    digests = [receipt.digest]
    if child.task_kind == "authenticate" and receipt.succeeded:
        binding = completion.authentication_binding
        auth_receipt = completion.authentication_receipt
        if authentication is None or binding is None or auth_receipt is None:
            raise ProcessV1Error(
                "authentication requires trusted live binding evidence"
            )
        if (
            binding.app_version_digest != state.process_digest
            or binding.process_execution_id != state.process_execution_id
            or binding.step_id != child.name
            or binding.challenge_digest != challenge
        ):
            raise ProcessV1Error("the authentication run binding differs")
        accepted = validate_authentication_receipt(
            authentication, binding, auth_receipt
        )
        digests.append(accepted.receipt_digest)
    state.human_receipt_digests = tuple(
        sorted(set(state.human_receipt_digests) | set(digests))
    )
    payload = {"child": child.name, "human_receipt_digests": sorted(digests)}
    digest = _digest(_canonical(payload))
    _write_json(
        run / "human" / child.name / "process-receipt.json",
        {**payload, "receipt_digest": digest},
    )
    if not receipt.succeeded:
        terminal = {
            "delivery_uncertain": "reconciliation_required",
            "rejected": "rejected_policy",
        }.get(receipt.state.value, "halted_before_effect")
        return ProcessV1Step(
            child=child.name,
            kind="human",
            outcome=terminal,
            receipt_digest=digest,
            oracle_tier=0,
        )
    return ProcessV1Step(
        child=child.name,
        kind="human",
        outcome=(
            "completed_unverified" if child.task_kind == "actuate" else "verified"
        ),
        receipt_digest=digest,
        oracle_tier=0 if child.task_kind == "actuate" else 2,
    )


def _flow_child(
    contract: ProcessContract,
    parent: Path,
    child: AdmittedChildSpec,
    inputs: Mapping[str, Any],
    run: Path,
    *,
    child_run: AdmittedChildExecutor,
    qualification_signers: Mapping[str, QualificationSignerTrust],
    revoked_qualification_admissions: set[str],
) -> ProcessV1Step:
    envelope = load_child_envelope(parent, child)
    payload = envelope.payload
    if (
        payload.admission_id != child.admission_id
        or payload.workflow_version_id != child.workflow_version_id
        or payload.bundle_content_digest != child.bundle_content_digest
    ):
        raise ProcessV1Error(f"Flow child {child.name!r} envelope binding differs")
    bundle = resolve_pointer(parent, child.bundle)
    workflow = Workflow.load(bundle)
    if live_bundle_content_digest(workflow, bundle) != payload.bundle_content_digest:
        raise ProcessV1Error(f"Flow child {child.name!r} bundle digest differs")
    try:
        verify_qualification_admission(
            envelope,
            trusted_signers=qualification_signers,
            expected=expected_from_payload(payload),
            revoked_admission_ids=revoked_qualification_admissions,
            now=datetime.now(timezone.utc),
        )
    except QualificationAdmissionError as exc:
        raise ProcessV1Error(
            f"Flow child {child.name!r} admission refused: {exc}"
        ) from exc
    result = execute(
        AdmittedCapability(
            name=child.name,
            admission_id=payload.admission_id,
            workflow_version_id=payload.workflow_version_id,
            bundle_content_digest=payload.bundle_content_digest,
        ),
        envelope,
        {
            key: json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            for key, value in inputs.items()
        },
        workflow=workflow,
        bundle_dir=bundle,
        run_dir=run,
        child=child.name,
        child_run=child_run,
    )
    outcomes = {
        "VERIFIED": "verified",
        "HALTED": "halted_before_effect",
        "FAILED": "failed_platform",
        "ROLLED_BACK": "rolled_back_verified",
        "RECONCILIATION_REQUIRED": "reconciliation_required",
    }
    outcome = outcomes.get(result.outcome, "failed_platform")
    receipt_path = Path(result.report_path) if result.report_path else None
    receipt_digest = (
        _file_digest(receipt_path)
        if receipt_path is not None and receipt_path.is_file()
        else _digest(_canonical({"child": child.name, "outcome": outcome}))
    )
    return ProcessV1Step(
        child=child.name,
        kind="flow",
        outcome=outcome,
        receipt_digest=receipt_digest,
        oracle_tier=(2 if outcome in {"verified", "rolled_back_verified"} else 0),
        model_used=result.model_calls > 0,
    )


def _inputs(
    contract: ProcessContract,
    state: ProcessV1State,
    child: str,
    run: Path,
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    values = dict(supplied)
    capability = contract.capability(child)
    if isinstance(capability, CodeChildSpec) and capability.role == "verifier":
        for predecessor in predecessor_map(contract)[child]:
            prior = state.completed.get(predecessor)
            if (
                prior is None
                or prior.kind != "human"
                or prior.outcome != "completed_unverified"
            ):
                continue
            completion_path = run / "human" / predecessor / "human-completion.json"
            if not completion_path.is_file():
                raise ProcessV1Error(
                    "a human actuation verifier has no retained completion envelope"
                )
            values[f"verify_{predecessor}_receipt"] = {
                "receipt_digest": prior.receipt_digest,
                "local_path": str(completion_path),
            }
    edges = [edge for edge in contract.artifact_edges if edge.to_child == child]
    edges.extend(
        edge
        for edge in contract.artifact_edges
        if edge.verifier_child == child and edge.to_child != child
    )
    for edge in edges:
        matches = [
            item
            for item in state.artifacts.values()
            if item["producer"] == edge.from_child
            and item["logical_name"] == edge.from_output
        ]
        if len(matches) != 1:
            raise ProcessV1Error("an artifact edge does not name one produced artifact")
        artifact = matches[0]
        store_path = PurePosixPath(str(artifact["store_path"]))
        expected_name = artifact["content_digest"].removeprefix("sha256:")
        if (
            store_path.is_absolute()
            or ".." in store_path.parts
            or store_path.parts != ("artifacts", expected_name)
        ):
            raise ProcessV1Error("an artifact store pointer is invalid")
        local_path = run / Path(*store_path.parts)
        if (
            not local_path.is_file()
            or _file_digest(local_path) != artifact["content_digest"]
        ):
            raise ProcessV1Error("a stored artifact digest differs before consumption")
        is_verifier = edge.verifier_child == child
        if not is_verifier and artifact["verification_state"] != "verified":
            raise ProcessV1Error("an unverified artifact cannot cross an edge")
        target = (
            edge.to_input
            if not is_verifier
            else f"verify_{edge.from_child}_{edge.from_output}"
        )
        values[target] = {
            "artifact_ref": {
                key: value
                for key, value in artifact.items()
                if key not in {"producer", "store_path"}
            },
            "local_path": str(local_path),
        }
    return values


def execute_process_contract_v1(
    contract: ProcessContract,
    *,
    parent_dir: Path | str,
    run_dir: Path | str,
    inputs: Mapping[str, Any] | None,
    child_run: AdmittedChildExecutor,
    qualification_signers: Mapping[str, QualificationSignerTrust],
    code_signers: Mapping[str, bytes],
    runtime_environment_digest: str,
    receipt_private_key: bytes,
    receipt_issuer_key_id: str,
    environment_id: str,
    runner_id: str,
    allow_trusted_code: bool,
    revoked_qualification_admissions: set[str] | None = None,
    revoked_code_admissions: set[str] | None = None,
    human_completion_provider: HumanCompletionProviderV1 | None = None,
) -> ProcessV1Result:
    """Run or resume one ProcessContract v1 without a planner."""

    if contract.schema_version != "openadapt.process-contract/v1":
        raise ProcessV1Error("the v1 runtime requires ProcessContract v1")
    if len(receipt_private_key) != 32:
        raise ProcessV1Error("the process evidence key must contain 32 bytes")
    parent = Path(parent_dir).resolve()
    run = Path(run_dir).resolve()
    state = _state(contract, parent, run)
    if state.status == "terminal":
        return ProcessV1Result(
            state,
            run / "process-execution.json",
            _verified_terminal_receipt(state, run, receipt_private_key),
        )
    state.status = "running"
    state.waiting_child = None
    supplied = dict(inputs or {})
    unexpected_inputs = sorted(set(supplied) - set(contract.inputs))
    missing_inputs = sorted(set(contract.inputs) - set(supplied))
    if unexpected_inputs or missing_inputs:
        raise ProcessV1Error(
            "process inputs differ from the declared set: "
            f"missing={missing_inputs}, unexpected={unexpected_inputs}"
        )
    predecessors = predecessor_map(contract)

    for name in topological_order(contract):
        if name in state.completed:
            continue
        child = contract.capability(name)
        for predecessor in predecessors[name]:
            prior = state.completed.get(predecessor)
            if prior is None:
                raise ProcessV1Error(f"child {name!r} has an incomplete predecessor")
            verifier = isinstance(child, CodeChildSpec) and child.role == "verifier"
            if prior.outcome != "verified" and not (
                verifier and prior.outcome == "completed_unverified"
            ):
                raise ProcessV1Error(f"child {predecessor!r} is not verified")
        child_inputs = _inputs(contract, state, name, run, supplied)
        child_run_dir = run / "children" / name

        if isinstance(child, AdmittedChildSpec):
            record = _flow_child(
                contract,
                parent,
                child,
                child_inputs,
                child_run_dir,
                child_run=child_run,
                qualification_signers=qualification_signers,
                revoked_qualification_admissions=revoked_qualification_admissions
                or set(),
            )
        elif isinstance(child, CodeChildSpec):
            record, artifacts, verification = _execute_code(
                parent,
                child,
                child_inputs,
                child_run_dir,
                trusted_signers=code_signers,
                revoked=revoked_code_admissions or set(),
                runtime_environment_digest=runtime_environment_digest,
                allow_trusted_code=allow_trusted_code,
            )
            for artifact in artifacts:
                source = Path(artifact.pop("source_path"))
                store = (
                    run
                    / "artifacts"
                    / artifact["content_digest"].removeprefix("sha256:")
                )
                store.parent.mkdir(parents=True, exist_ok=True)
                if not store.is_file():
                    shutil.copyfile(source, store)
                if _file_digest(store) != artifact["content_digest"]:
                    raise ProcessV1Error("the content-addressed artifact store differs")
                artifact_ref = ArtifactRefV1(
                    artifact_id=artifact["artifact_id"],
                    content_digest=artifact["content_digest"],
                    size_bytes=artifact["size_bytes"],
                    media_type=artifact["media_type"],
                    logical_name=artifact["logical_name"],
                    producer_execution_id=state.process_execution_id,
                    producer_output_name=artifact["logical_name"],
                    storage_boundary=ArtifactStorageBoundary(child.storage_boundary),
                    data_classification=ArtifactDataClassification(
                        child.data_classification
                    ),
                    verification_state=ArtifactVerificationState.PENDING,
                    verifier_receipt_digest=None,
                    metadata_digest=None,
                    created_at=_utc(),
                )
                state.artifacts[artifact["artifact_id"]] = {
                    **artifact_ref.model_dump(mode="json"),
                    "producer": child.name,
                    "store_path": store.relative_to(run).as_posix(),
                }
            if verification is not None:
                verified = set(verification.get("artifact_digests", ()))
                expected = {
                    item["content_digest"]
                    for item in state.artifacts.values()
                    if any(
                        edge.verifier_child == name
                        and edge.from_child == item["producer"]
                        and edge.from_output == item["logical_name"]
                        for edge in contract.artifact_edges
                    )
                }
                if verified != expected:
                    raise ProcessV1Error(
                        "the verifier receipt does not cover the exact artifact set"
                    )
                for artifact in state.artifacts.values():
                    if artifact["content_digest"] in verified:
                        artifact["verification_state"] = "verified"
                        artifact["verifier_receipt_digest"] = record.receipt_digest
                        ArtifactRefV1.model_validate(
                            {
                                key: value
                                for key, value in artifact.items()
                                if key not in {"producer", "store_path"}
                            }
                        )
                        producer = state.completed.get(artifact["producer"])
                        if producer is not None:
                            producer.outcome = "verified"
                            producer.oracle_tier = record.oracle_tier
                expected_child_receipts = {
                    state.completed[pred].receipt_digest
                    for pred in predecessors[name]
                    if pred in state.completed
                    and state.completed[pred].kind == "human"
                    and state.completed[pred].outcome == "completed_unverified"
                }
                declared_child_receipts = set(
                    verification.get("child_receipt_digests", ())
                )
                if declared_child_receipts != expected_child_receipts:
                    raise ProcessV1Error(
                        "the verifier receipt does not cover the exact human receipt set"
                    )
                for pred in predecessors[name]:
                    prior = state.completed.get(pred)
                    if (
                        prior is not None
                        and prior.receipt_digest in declared_child_receipts
                    ):
                        prior.outcome = "verified"
                        prior.oracle_tier = record.oracle_tier
        else:
            request = run / "human" / name / "human-task.json"
            if request.is_file():
                task, authentication, challenge = _load_human(child, run)
            else:
                task, authentication, challenge = _issue_human(
                    contract, state, child, parent, run
                )
                _append_event(
                    run,
                    state,
                    {
                        "event": "human_task_issued",
                        "child": name,
                        "task_digest": task.digest,
                    },
                )
            artifact_inputs = {
                key: value
                for key, value in child_inputs.items()
                if isinstance(value, dict) and "artifact_ref" in value
            }
            if artifact_inputs:
                _write_json(request.parent / "artifact-inputs.json", artifact_inputs)
            completion = (
                human_completion_provider(child, task, authentication, request.parent)
                if human_completion_provider is not None
                else _completion_file(request.parent)
            )
            if completion is None:
                state.status = "waiting_human"
                state.waiting_child = name
                _save(run, state)
                return ProcessV1Result(state, run / "process-execution.json", None)
            _inputs(contract, state, name, run, supplied)
            record = _accept_human(
                state, child, task, authentication, challenge, completion, run
            )
            completion_path = request.parent / "human-completion.json"
            if human_completion_provider is not None or not completion_path.is_file():
                _write_json(
                    completion_path,
                    {
                        "schema_version": "openadapt.process-human-completion/v1",
                        "receipt": completion.receipt.model_dump(mode="json"),
                        "authentication_binding": (
                            completion.authentication_binding.model_dump(mode="json")
                            if completion.authentication_binding is not None
                            else None
                        ),
                        "authentication_receipt": (
                            completion.authentication_receipt.model_dump(mode="json")
                            if completion.authentication_receipt is not None
                            else None
                        ),
                    },
                )

        state.completed[name] = record
        _append_event(
            run,
            state,
            {
                "event": "child_completed",
                "child": name,
                "outcome": record.outcome,
                "receipt_digest": record.receipt_digest,
            },
        )
        if record.outcome == "reconciliation_required":
            state.status = "terminal"
            state.outcome = record.outcome
            _append_event(
                run, state, {"event": "process_terminal", "outcome": state.outcome}
            )
            _save(run, state)
            receipt = _final_receipt(
                contract,
                state,
                run,
                private_key=receipt_private_key,
                issuer_key_id=receipt_issuer_key_id,
                environment_id=environment_id,
                runner_id=runner_id,
            )
            return ProcessV1Result(state, run / "process-execution.json", receipt)
        if record.outcome not in {"verified", "completed_unverified"}:
            state.status = "terminal"
            state.outcome = record.outcome
            _append_event(
                run, state, {"event": "process_terminal", "outcome": state.outcome}
            )
            _save(run, state)
            receipt = _final_receipt(
                contract,
                state,
                run,
                private_key=receipt_private_key,
                issuer_key_id=receipt_issuer_key_id,
                environment_id=environment_id,
                runner_id=runner_id,
            )
            return ProcessV1Result(state, run / "process-execution.json", receipt)
        _save(run, state)

    for artifact in state.artifacts.values():
        portable = {
            key: value
            for key, value in artifact.items()
            if key not in {"producer", "store_path"}
        }
        ArtifactRefV1.model_validate(portable)
        store_relative = PurePosixPath(str(artifact["store_path"]))
        expected_name = artifact["content_digest"].removeprefix("sha256:")
        if store_relative.parts != ("artifacts", expected_name):
            raise ProcessV1Error("an artifact store pointer is invalid")
        store = run / Path(*store_relative.parts)
        if not store.is_file() or _file_digest(store) != artifact["content_digest"]:
            raise ProcessV1Error("an artifact changed before terminal verification")
    pending = [
        item
        for item in state.artifacts.values()
        if item["verification_state"] != "verified"
    ]
    state.status = "terminal"
    process_oracle_tier = min(
        (item.oracle_tier for item in state.completed.values()), default=0
    )
    state.outcome = (
        "verified"
        if (
            all(item.outcome == "verified" for item in state.completed.values())
            and not pending
            and not any(item.model_used for item in state.completed.values())
            and process_oracle_tier >= 2
        )
        else "failed_platform"
    )
    _append_event(run, state, {"event": "process_terminal", "outcome": state.outcome})
    _save(run, state)
    receipt = _final_receipt(
        contract,
        state,
        run,
        private_key=receipt_private_key,
        issuer_key_id=receipt_issuer_key_id,
        environment_id=environment_id,
        runner_id=runner_id,
    )
    return ProcessV1Result(state, run / "process-execution.json", receipt)


def _final_receipt(
    contract: ProcessContract,
    state: ProcessV1State,
    run: Path,
    *,
    private_key: bytes,
    issuer_key_id: str,
    environment_id: str,
    runner_id: str,
) -> Path:
    outcome = ExecuteTerminalOutcomeV1(str(state.outcome))
    artifacts = [
        {
            key: value
            for key, value in item.items()
            if key not in {"producer", "store_path"}
        }
        for item in sorted(
            state.artifacts.values(), key=lambda value: value["artifact_id"]
        )
    ]
    fields = {
        "receipt_id": f"receipt-{secrets.token_hex(16)}",
        "process_execution_id": state.process_execution_id,
        "process_digest": state.process_digest,
        "app_package_digest": state.process_digest,
        "environment_id": environment_id,
        "runner_id": runner_id,
        "outcome": outcome,
        "oracle_tier": min(
            (item.oracle_tier for item in state.completed.values()), default=0
        ),
        "delivery_uncertain": outcome
        is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
        "model_used": any(item.model_used for item in state.completed.values()),
        "external_network_used": any(
            item.external_network_used for item in state.completed.values()
        ),
        "child_receipt_digests": tuple(
            sorted(item.receipt_digest for item in state.completed.values())
        ),
        "human_receipt_digests": state.human_receipt_digests,
        "artifact_graph_digest": _digest(
            _canonical(
                {
                    "artifacts": artifacts,
                    "edges": [
                        edge.model_dump(mode="json") for edge in contract.artifact_edges
                    ],
                }
            )
        ),
        "evidence_root_digest": state.event_head,
        "issued_at": _utc(),
        "issuer_key_id": issuer_key_id,
    }
    placeholder = base64.b64encode(b"\x00" * 64).decode("ascii")
    unsigned = ProcessEvidenceReceiptV1.model_validate(
        {**fields, "signature": placeholder}
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(
        _canonical(unsigned.unsigned_payload())
    )
    receipt = unsigned.model_copy(
        update={"signature": base64.b64encode(signature).decode("ascii")}
    )
    path = run / "process-evidence-receipt.json"
    _write_json(path, receipt.model_dump(mode="json"))
    return path


def load_code_signer_trust(path: Path | str) -> tuple[dict[str, bytes], set[str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(raw) - {"keys", "revoked_admission_ids"}:
        raise ProcessV1Error("the code signer registry has unknown fields")
    keys: dict[str, bytes] = {}
    for key_id, value in raw.get("keys", {}).items():
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 32:
            raise ProcessV1Error("a code signer public key has an invalid length")
        keys[str(key_id)] = decoded
    return keys, {str(value) for value in raw.get("revoked_admission_ids", ())}


__all__ = [
    "HumanCompletionProviderV1",
    "HumanCompletionV1",
    "ProcessV1Error",
    "ProcessV1Result",
    "execute_process_contract_v1",
    "load_code_signer_trust",
]
