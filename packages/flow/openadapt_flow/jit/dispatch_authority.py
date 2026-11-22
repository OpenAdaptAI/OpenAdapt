"""Verified authority for one managed admitted-program dispatch.

The public runtime, deployment factory, CLI and durable resume share this
handoff. It is an in-memory capability, never a process-global allow-list and
never part of the serialized dispatch. A resumed process verifies the signed
admission and current trust registry again before creating another capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from openadapt_flow.jit.hosted_authority import (
    HostedAuthorityError,
    admission_sha256,
    managed_admission_refusal,
    readback_sha256,
)
from openadapt_flow.runner.dispatch_envelope import (
    _BINDING_FACTORY,
    ManagedDispatchBinding,
    managed_authorization_mismatch,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256

_FACTORY = object()
_SESSION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _authorization_digest(authorization: Any) -> str:
    # Include runner-local safety fields as well as the Cloud-bound fields.
    return _digest(authorization.model_dump(mode="json"))


def _registry_digest(key_dir: Path) -> str:
    from openadapt_flow.jit.authority import PRINCIPALS_FILE, PrincipalRegistry

    public = key_dir / "keys" / "ed25519.pub"
    registry = key_dir / PRINCIPALS_FILE
    if public.is_symlink() or registry.is_symlink():
        raise HostedAuthorityError("the admission trust registry contains a link")
    raw = bytes.fromhex(public.read_text(encoding="utf-8").strip())
    if len(raw) != 32:
        raise HostedAuthorityError("the admission trust key is invalid")
    return _digest(
        {
            "admission_public_key": raw.hex(),
            "principals": PrincipalRegistry.load(key_dir).model_dump(
                mode="json", by_alias=True
            ),
        }
    )


def _session() -> tuple[str, str, str]:
    from openadapt_flow.runtime.durable.authority import (
        REMOTE_AUTHORITY_URL_ENV,
        REMOTE_DISPATCH_SESSION_ID_ENV,
    )

    session = os.environ.get(REMOTE_DISPATCH_SESSION_ID_ENV, "")
    origin = os.environ.get(REMOTE_AUTHORITY_URL_ENV, "")
    if not _SESSION.fullmatch(session) or not origin:
        raise HostedAuthorityError(
            "the managed program requires an exact dispatch session"
        )
    # These are public trust inputs. No credential is read or retained here.
    registry = os.environ.get("OPENADAPT_QUALIFICATION_SIGNERS_JSON", "")
    return session, origin, hashlib.sha256(registry.encode()).hexdigest()


class ProgramDispatchAuthority:
    """Opaque, single-execution authority returned by verify_program_dispatch."""

    def __init__(
        self,
        factory: object,
        *,
        binding: ManagedDispatchBinding,
        bundle: Path,
        key_dir: Path,
        policy_source: str,
        registry_digest: str,
        admission_digest: str,
        readback_digest: str,
    ) -> None:
        if factory is not _FACTORY:
            raise HostedAuthorityError("program dispatch authority must be verified")
        self._run_id = binding.run_id
        self._binding_digest = binding.binding_sha256
        self._authorization_digest = _authorization_digest(binding.authorization)
        self._bundle = bundle.resolve()
        self._key_dir = key_dir.resolve()
        self._policy_source = policy_source
        self._registry_digest = registry_digest
        self._admission_digest = admission_digest
        self._readback_digest = readback_digest
        self._session = _session()
        self._claim_lock = threading.Lock()
        self._claimed = False

    @property
    def key_dir(self) -> Path:
        """The public admission trust directory this capability verified."""
        return self._key_dir

    def refusal(
        self,
        workflow: Any,
        binding: Any,
        authorization: Any = None,
    ) -> str | None:
        """Recheck the exact dispatch and current public trust inputs."""
        from openadapt_flow.policy import load_policy, policy_contract_sha256

        try:
            if not isinstance(binding, ManagedDispatchBinding):
                return "the verified program authority lost its dispatch binding"
            current = binding.authorization if authorization is None else authorization
            if (
                binding.run_id != self._run_id
                or binding.binding_sha256 != self._binding_digest
                or dispatch_binding_sha256(binding.run_id, current)
                != self._binding_digest
                or _authorization_digest(current) != self._authorization_digest
                or binding.authorization != current
            ):
                return "the verified program authority belongs to another dispatch or authorization"
            if _session() != self._session:
                return "the managed dispatch session or signer registry changed"
            if _registry_digest(self._key_dir) != self._registry_digest:
                return "the admission trust registry changed after verification"
            if admission_sha256(self._bundle) != self._admission_digest:
                return "the signed program admission changed after verification"
            if readback_sha256(self._bundle) != self._readback_digest:
                return "the program read-back changed after verification"
            if (
                policy_contract_sha256(load_policy(self._policy_source))
                != current.admitted_policy_contract_sha256
            ):
                return "the managed program policy changed after verification"
            mismatch = current.validate_workflow(workflow)
            if mismatch is not None:
                return mismatch
            return managed_admission_refusal(
                self._bundle,
                workflow,
                current,
                profile="standard",
                key_dir=self._key_dir,
            )
        except Exception:  # noqa: BLE001 - an unreadable trust input grants nothing
            return "the verified program authority could not be revalidated"

    def claim(
        self,
        workflow: Any,
        binding: ManagedDispatchBinding,
        *,
        bundle_dir: Path,
        params: Mapping[str, Any] | None,
        worklists: dict[str, list[dict[str, str]]] | None,
        interstitials: list[Any],
    ) -> str | None:
        """Consume this verified handoff once, including a resumed leg."""
        from openadapt_flow.runtime.authorization import runtime_inputs_digest

        refusal = self.refusal(workflow, binding)
        if refusal is not None:
            return refusal
        if Path(bundle_dir).resolve() != self._bundle:
            return "the verified program authority belongs to another bundle path"
        if (
            runtime_inputs_digest(
                workflow, params, worklists, interstitials=interstitials
            )
            != binding.authorization.runtime_inputs_digest
        ):
            return "the verified program authority belongs to different runtime inputs"
        with self._claim_lock:
            if self._claimed:
                return "the verified program authority was already consumed by an execution"
            self._claimed = True
        return None


def verify_program_dispatch(
    binding: ManagedDispatchBinding,
    *,
    bundle_dir: Path | str,
    workflow: Any,
    params: Mapping[str, Any] | None,
    worklists: Mapping[str, list[dict[str, str]]] | None = None,
    policy_source: str | None,
    effect_verifier: Any,
    api_actuator: Any = None,
    interstitials: list[Any] | None = None,
    key_dir: Path | None = None,
) -> ManagedDispatchBinding:
    """Verify one dispatch before passing it to the public execution APIs.

    Call after resolving the runner's approval mode. The returned binding is
    single-use. Never serialize its authority or transfer it to another run.
    For resume, call again with the retained dispatch, frozen inputs, current
    trusted keys and current dispatch session; durable resume separately checks
    the exact retained run and the person's continuation approval.
    """
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.jit.lifecycle import default_key_dir
    from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    if not isinstance(binding, ManagedDispatchBinding):
        raise HostedAuthorityError("a verified managed dispatch binding is required")
    bundle = Path(bundle_dir)
    trusted = Path(key_dir) if key_dir is not None else default_key_dir()
    before = _registry_digest(trusted)
    refusal = managed_admission_refusal(
        bundle,
        workflow,
        binding.authorization,
        profile="standard",
        key_dir=trusted,
    )
    if refusal is not None:
        raise HostedAuthorityError(refusal)
    if (
        runtime_inputs_digest(
            workflow,
            params,
            None if worklists is None else dict(worklists),
            interstitials=interstitials,
        )
        != binding.authorization.runtime_inputs_digest
    ):
        raise HostedAuthorityError(
            "the managed dispatch does not bind these runtime inputs"
        )
    profile = execution_profile_contract(ExecutionProfile.STANDARD)
    report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=DeploymentConfig(policy=PolicySection(policy=policy_source)),
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        policy_source=policy_source,
        require_encryption=False,
        profile_contract=profile,
        effective_durable=True,
        effective_require_settled=True,
    )
    if not report.passed:
        raise HostedAuthorityError(
            "the managed program did not pass the local run gate"
        )
    local = build_runtime_authorization(
        workflow,
        report,
        params=None if params is None else dict(params),
        worklists=None if worklists is None else dict(worklists),
        interstitials=interstitials,
    )
    auth = binding.authorization
    if managed_authorization_mismatch(auth, local):
        raise HostedAuthorityError(
            "the managed dispatch differs from the locally admitted policy or program"
        )
    if _registry_digest(trusted) != before:
        raise HostedAuthorityError(
            "the admission trust registry changed during verification"
        )
    authority = ProgramDispatchAuthority(
        _FACTORY,
        binding=binding,
        bundle=bundle,
        key_dir=trusted,
        policy_source=report.policy_name,
        registry_digest=before,
        admission_digest=admission_sha256(bundle),
        readback_digest=readback_sha256(bundle),
    )
    bound = ManagedDispatchBinding(
        _BINDING_FACTORY,
        run_id=binding.run_id,
        authorization=auth,
        binding_sha256=binding.binding_sha256,
        program_admission_authority=authority,
    )
    refusal = authority.refusal(workflow, bound)
    if refusal is not None:
        raise HostedAuthorityError(refusal)
    return bound


def claim_program_execution(
    replayer: Any,
    workflow: Any,
    *,
    bundle_dir: Path,
    params: Mapping[str, Any] | None,
    worklists: dict[str, list[dict[str, str]]] | None,
) -> None:
    """The shared runtime consumes the proof before any backend callback."""
    binding = replayer.managed_dispatch_binding
    authority = getattr(binding, "program_admission_authority", None)
    if authority is None:
        return  # The shared qualification check refuses an unproved program.
    if not isinstance(authority, ProgramDispatchAuthority):
        raise ValueError("the managed program authority is invalid")
    if (
        replayer._draft_key_dir is not None
        and Path(replayer._draft_key_dir).resolve() != authority.key_dir
    ):
        raise ValueError("the managed program uses another admission trust directory")
    replayer._draft_key_dir = authority.key_dir
    refusal = authority.claim(
        workflow,
        binding,
        bundle_dir=bundle_dir,
        params=params,
        worklists=worklists,
        interstitials=list(replayer._interstitials),
    )
    if refusal is not None:
        raise ValueError(refusal)
