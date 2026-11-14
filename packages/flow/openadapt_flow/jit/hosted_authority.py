"""The run authority of an admitted agent-compiled program on a managed runner.

A governed authorization template summarises a passing qualification
campaign (fault cases, identity policies, trusted-runner case results), and
``Workflow.load`` refuses any template it cannot rebuild from that sealed
certification. A program compiled from an agent run (``openadapt wrap``,
``jit``) has no campaign. Its authority is its signed admission
(``admission.json``): a named person, a registered admitter key or an agent
under a signed policy, an attended canary on the target, and an independent
read-back of every write (:mod:`openadapt_flow.jit.lifecycle`). Every local
run of such a program already rests on that admission
(``lifecycle.ensure_runnable``).

This module carries the same admission to a managed Standard run without
inventing a second envelope:

* :func:`run_shape` is computed once at publish from Flow's own run gate. It
  holds the value-free fields a hosted :class:`GovernedRunAuthorization`
  needs (policy, profile, effect tier and requirements, identity steps) and
  the admission's SHA-256. It grants nothing: the runner re-derives every
  field locally and refuses a dispatch that disagrees.
* :func:`managed_admission_refusal` is what ``openadapt-flow run
  --managed-dispatch-file`` checks instead of a qualification admission when
  the dispatch carries none: Standard only, an admission that verifies
  against the trusted admission key directory (the runner stages the key the
  organization registered), and an ``approval_source`` that names this exact
  admission.
* :func:`readback_effects` is the read-back verifier the run uses: the
  ``verify.yaml`` bundled at publish. :func:`hosted_admission` binds its
  SHA-256 into the signed admission, so a swapped file is refused.
* :func:`hosted_admission` writes the copy of the admission that goes to the
  cloud: person and record identifiers become keyed HMACs
  (:mod:`openadapt_flow.jit.principal_ids`), and the copy is signed again with
  the same local admission key after the original verified.

The bundled read-back replaces the deployment's effects verifier only on
this path: a managed run with no production authority file and no
qualification admission, whose admission and read-back verify. Any other run
ignores the bundled file and reads back through its own deployment config.

Regulated runs are refused: they need the qualification campaign.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

__all__ = [
    "APPROVAL_PREFIX",
    "HOSTED_FIELD",
    "READBACK_PATH",
    "RUN_SHAPE_SCHEMA",
    "HostedAuthorityError",
    "admission_sha256",
    "approval_source",
    "hosted_admission",
    "hosted_readback",
    "is_admitted_program",
    "managed_admission_refusal",
    "readback_effects",
    "readback_sha256",
    "run_shape",
]

RUN_SHAPE_SCHEMA = "openadapt.admitted-program-run-shape/v1"
#: ``approval_source`` of a hosted run whose authority is a program admission.
APPROVAL_PREFIX = "hosted:program-admission:"
#: Where the publish step puts the confirmed read-back check in the bundle.
READBACK_PATH = "readback/verify.yaml"
#: The block :func:`hosted_admission` adds to the admission it signs again.
HOSTED_FIELD = "hosted"
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
#: Where an admission names a principal (person, recorder, agent, reviewer).
_PRINCIPAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("admitted_by_principal_id",),
    ("reviewed_by_principal_id",),
    ("canary", "principal_id"),
    ("recorded_by",),
    ("agent_principal_id",),
    ("teach_authorization", "authorizer"),
)
_CONFIRMED_COMMENT = re.compile(r"# Confirmed by (.+) at (\S+) from (\S+)\.\n")
_CONFIRMED_PROBE = " (confirmed by "


class HostedAuthorityError(ValueError):
    """The admitted program cannot run under a hosted admission authority."""


def _admission_file(bundle_dir: Path | str) -> Path:
    from openadapt_flow.jit.lifecycle import ADMISSION_FILE

    return Path(bundle_dir) / ADMISSION_FILE


def is_admitted_program(bundle_dir: Path | str) -> bool:
    """True when the bundle carries an admission manifest and a read-back file.

    Only a shape check: :func:`managed_admission_refusal` verifies them.
    """

    bundle = Path(bundle_dir)
    admission = _admission_file(bundle)
    readback = bundle / READBACK_PATH
    return (
        admission.is_file()
        and not admission.is_symlink()
        and readback.is_file()
        and not readback.is_symlink()
    )


def admission_sha256(bundle_dir: Path | str) -> str:
    """SHA-256 of the exact ``admission.json`` bytes in the bundle."""

    path = _admission_file(bundle_dir)
    if path.is_symlink() or not path.is_file():
        raise HostedAuthorityError("the bundle carries no admission manifest")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_source(admission_digest: str) -> str:
    """The hosted ``approval_source`` that names one exact admission."""

    if not _SHA256.fullmatch(admission_digest):
        raise HostedAuthorityError("the admission digest is invalid")
    return APPROVAL_PREFIX + admission_digest


def readback_sha256(bundle_dir: Path | str) -> str:
    """SHA-256 of the bundled ``readback/verify.yaml`` bytes."""

    path = Path(bundle_dir) / READBACK_PATH
    if path.is_symlink() or not path.is_file():
        raise HostedAuthorityError("the bundle carries no read-back check")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readback_effects(bundle_dir: Path | str) -> Any:
    """The bundled ``verify.yaml``'s verifier, as a deployment effects section."""

    from openadapt_flow.verify_run import load_verify_config

    path = Path(bundle_dir) / READBACK_PATH
    if path.is_symlink() or not path.is_file():
        raise HostedAuthorityError("the bundle carries no read-back check")
    try:
        return load_verify_config(path).verifier
    except Exception as exc:  # noqa: BLE001 - any loader refusal is a refusal
        raise HostedAuthorityError(
            "the bundled read-back check is not a valid verify-run configuration"
        ) from exc


def run_shape(
    bundle_dir: Path | str,
    workflow: Any,
    *,
    policy_source: str,
) -> dict[str, Any]:
    """The value-free Standard run shape of an admitted program.

    Computed with the run gate the managed runner evaluates for the same
    bundle and policy, so the fields agree with the authorization the runner
    builds locally. Values, identities and credentials are never in it.
    """

    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.run_gate import evaluate_run_gate

    bundle = Path(bundle_dir)
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not digest:
        raise HostedAuthorityError("the bundle carries no content digest")
    profile = execution_profile_contract(ExecutionProfile.STANDARD)
    report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=DeploymentConfig(policy=PolicySection(policy=policy_source)),
        effect_verifier=None,
        policy_source=policy_source,
        require_encryption=False,
        profile_contract=profile,
        effective_durable=profile.require_durable,
        effective_require_settled=profile.require_settled,
    )
    if (
        report.execution_profile != ExecutionProfile.STANDARD.value
        or report.policy_contract_sha256 is None
        or report.minimum_effect_tier is None
        or report.bundle_content_digest != digest
    ):
        raise HostedAuthorityError(
            "the run gate did not produce a Standard run shape for this bundle"
        )
    return {
        "schema_version": RUN_SHAPE_SCHEMA,
        "bundle_content_digest": digest,
        "admission_sha256": admission_sha256(bundle),
        "admitted_policy_name": report.policy_name,
        "admitted_policy_contract_sha256": report.policy_contract_sha256,
        "execution_profile": report.execution_profile,
        "minimum_effect_tier": report.minimum_effect_tier,
        "qualified_effect_requirements": [
            item.model_dump(mode="json")
            for item in report.qualified_effect_requirements
        ],
        # Run-gate order, which the runner's local authorization keeps.
        "required_identity_step_ids": list(report.required_identity_step_ids),
    }


def managed_admission_refusal(
    bundle_dir: Path | str,
    workflow: Any,
    authorization: Any,
    *,
    profile: Optional[str],
    key_dir: Optional[Path] = None,
) -> Optional[str]:
    """Why a managed dispatch cannot rest on the program's admission, or None.

    ``key_dir`` is the trusted admission key directory (default
    :func:`openadapt_flow.jit.lifecycle.default_key_dir`). On a managed runner
    it must hold only the public key the organization registered, at
    ``<key_dir>/keys/ed25519.pub``: :func:`~openadapt_flow.jit.lifecycle.verify_admission`
    trusts whatever key is staged there.

    The caller applies the bundled read-back (:func:`readback_effects`) only
    when this returns None.
    """

    from openadapt_flow.jit.lifecycle import (
        AdmissionError,
        is_jit_bundle,
        verify_admission,
    )

    bundle = Path(bundle_dir)
    if profile != "standard":
        return "an admitted program runs hosted only under the Standard profile"
    if authorization is None:
        return "the managed dispatch carries no run authorization"
    if getattr(authorization, "qualification_admission", None) is not None:
        return "the dispatch carries a qualification admission"
    if getattr(authorization, "execution_profile", None) != "standard":
        return "the run authorization is not for the Standard profile"
    if not is_jit_bundle(bundle, workflow) or not is_admitted_program(bundle):
        return "the bundle is not an admitted program with a read-back check"
    try:
        manifest = verify_admission(bundle, key_dir=key_dir)
        digest = admission_sha256(bundle)
        readback_effects(bundle)
        bound = readback_sha256(bundle)
    except (AdmissionError, HostedAuthorityError):
        return "the program's admission or read-back check does not verify"
    hosted = manifest.get(HOSTED_FIELD)
    if not isinstance(hosted, Mapping) or hosted.get("readback_sha256") != bound:
        return "the bundled read-back check is not the one the admission binds"
    if getattr(authorization, "approval_source", None) != approval_source(digest):
        return "the run authorization does not name this program's admission"
    return None


# -- the copy of the admission that goes to the cloud ---------------------------


def _pseudonymized(manifest: Mapping[str, Any], keys: Path) -> dict[str, Any]:
    """``manifest`` with every principal a keyed HMAC unless it may be shown.

    Returns the manifest unchanged when nothing needed replacing. Otherwise
    the body is signed again with the local admission key. An embedded prior
    admission (a repair) is treated the same way first.
    """

    from openadapt_flow.jit.principal_ids import pseudonym

    body = copy.deepcopy({k: v for k, v in manifest.items() if k != "signature"})
    changed = False
    for path in _PRINCIPAL_PATHS:
        node: Any = body
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or not isinstance(node.get(path[-1]), str):
            continue
        value = pseudonym(node[path[-1]], keys)
        if value != node[path[-1]]:
            node[path[-1]] = value
            changed = True
    repair = body.get("repair_of")
    if isinstance(repair, dict) and isinstance(repair.get("prior_manifest"), Mapping):
        prior = repair["prior_manifest"]
        replaced = _pseudonymized(prior, keys)
        if replaced != prior:
            repair["prior_manifest"] = replaced
            changed = True
    if not changed:
        return dict(manifest)
    if body.get("admitter_signature"):
        # The registered admitter signed the plaintext names; a changed body
        # would no longer carry that signature.
        raise HostedAuthorityError(
            "the admission names a person or record identifier and was signed "
            "by a registered admitter key, so its hosted copy cannot replace "
            "the identifier. Use machine handles (ci:, agent:, service:) for "
            "the admitter and the recorder"
        )
    return {**body, "signature": _sign(body, keys)}


def _sign(body: Mapping[str, Any], keys: Path) -> str:
    """Sign with the existing local admission key; never create one."""

    from openadapt_flow.jit.lifecycle import _sign_record

    if not (Path(keys) / "keys" / "ed25519.pem").is_file():
        raise HostedAuthorityError("the local admission signing key is missing")
    return _sign_record(body, keys)


def hosted_readback(text: str, key_dir: Path) -> str:
    """The confirmed ``verify.yaml`` text with its confirmer pseudonymized.

    ``openadapt wrap`` writes a ``# Confirmed by NAME at ...`` line and puts
    the name in each new-record probe. Both become the principal's HMAC
    unless the name may be shown. Text that names no such principal is
    returned unchanged.
    """

    from openadapt_flow.jit.principal_ids import pseudonym

    header = _CONFIRMED_COMMENT.match(text)
    try:
        document = yaml.safe_load(text[header.end() :] if header else text)
    except yaml.YAMLError as exc:
        raise HostedAuthorityError("the read-back check is not valid YAML") from exc
    changed = False
    if header and pseudonym(header.group(1), key_dir) != header.group(1):
        changed = True
    effects = document.get("effects") if isinstance(document, dict) else None
    for effect in effects if isinstance(effects, list) else []:
        probe = effect.get("probe") if isinstance(effect, dict) else None
        if not isinstance(probe, str) or not probe.endswith(")"):
            continue
        head, marker, name = probe.rpartition(_CONFIRMED_PROBE)
        if not marker:
            continue
        value = pseudonym(name[:-1], key_dir)
        if value != name[:-1]:
            effect["probe"] = f"{head}{marker}{value})"
            changed = True
    if not changed:
        return text
    prefix = ""
    if header:
        prefix = (
            f"# Confirmed by {pseudonym(header.group(1), key_dir)} at "
            f"{header.group(2)} from {header.group(3)}.\n"
        )
    return prefix + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def hosted_admission(
    manifest: Mapping[str, Any],
    *,
    key_dir: Path,
    readback_digest: str,
) -> dict[str, Any]:
    """The admission copy the cloud receives, signed with the local key.

    The caller has already verified ``manifest`` with
    :func:`~openadapt_flow.jit.lifecycle.verify_admission`. The copy keeps
    every field, replaces each principal that may not be shown with its
    keyed HMAC (:mod:`openadapt_flow.jit.principal_ids`), and adds a
    ``hosted`` block binding the SHA-256 of the bundled ``verify.yaml``.
    The managed runner refuses a read-back file with another digest.
    """

    if not _SHA256.fullmatch(readback_digest):
        raise HostedAuthorityError("the read-back digest is invalid")
    original = {k: v for k, v in manifest.items() if k != HOSTED_FIELD}
    if HOSTED_FIELD in manifest:
        # Only an original admission is copied; its signature excludes no
        # field, so a manifest that already has the block is a hosted copy.
        raise HostedAuthorityError("the admission is already a hosted copy")
    body = {
        k: v for k, v in _pseudonymized(original, key_dir).items() if k != "signature"
    }
    body[HOSTED_FIELD] = {
        "readback_sha256": readback_digest,
        "principals": "hmac-sha256",
    }
    return {**body, "signature": _sign(body, key_dir)}
