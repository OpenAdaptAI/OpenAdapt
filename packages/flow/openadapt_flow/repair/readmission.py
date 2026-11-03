"""Re-qualify a repaired bundle, and re-admit it with signed lineage.

A repair never edits the bundle a run is pinned to. Self-repair writes a new
candidate bundle; this module turns that candidate into the next pinned
version, under the same admission the parent had:

1. **Re-qualify** (:func:`requalify`). The candidate runs the parent's own
   qualification campaign on a test copy of the app: the same verify.yaml,
   the same certify policy, and the same cases. Qualification certifies a
   copy, so the result is a new bundle with its own digest.
2. **Check what stays fixed** (:func:`fixed_contract_refusal`). Every
   consequential step keeps its write target, declared effects, identity rule
   and record check, and the certified template keeps its policy, effect
   requirements and identity and parameter contracts. A repair may re-target
   a control on screen; it may not change what is written, where, or for
   whom. Anything else needs a person's fresh admission, never inheritance.
3. **Sign the lineage** (:func:`admit`). After the person's one approval, or
   a signed standard change, a record signed with this machine's local
   admission key names the parent, the candidate, the re-qualified bundle,
   who approved it and how, and both qualification reports.
4. **Verify the chain** (:func:`lineage_refusal`). A pin to the parent (and a
   ``problems_only`` switch record bound to it) accepts a descendant only
   through a chain of such records: at most :data:`MAX_HOPS` hops, no cycle,
   every signature valid, no signing key revoked, every bundle staged and
   re-checked against its parent. A broken or unverifiable chain refuses; it
   never falls back to the candidate or the parent.

A production qualification admission and a managed dispatch are signed by an
authority outside this machine, so their repairs are not re-admitted here
(docs/COMPATIBILITY.md).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence

from openadapt_flow.ir import Step, Workflow

LINEAGE_SCHEMA: Literal["openadapt.repair-lineage/v1"] = "openadapt.repair-lineage/v1"
#: The most repairs a pin or a switch record carries before a person admits
#: the bundle afresh.
MAX_HOPS = 3
#: Lineage records, by the re-admitted bundle's digest, under the repair store.
READMISSIONS_DIR = "readmissions"
#: Signing-key fingerprints whose records no longer count (a JSON list).
REVOKED_KEYS_FILE = "revoked-keys.json"


class ReadmissionRefused(RuntimeError):
    """The repaired bundle can't be re-admitted; the parent stays pinned."""


@dataclass(frozen=True)
class Requalification:
    """How a repaired bundle is re-qualified: the parent's campaign, a test copy.

    Args:
        config: The parent's ``VerifyConfig`` (verify.yaml).
        hooks: ``QualifyHooks`` for the test copy of the app (sign-in, reset,
            read-back). None uses the hooks verify.yaml names.
        trials: Trials per case.
        run: The campaign runner, ``openadapt_flow.qualify.campaign.qualify``
            by default.
    """

    config: Any
    hooks: Any = None
    trials: int = 1
    run: Optional[Callable[..., Any]] = None


@dataclass(frozen=True)
class Requalified:
    """A candidate that passed its parent's campaign: the next version."""

    bundle_dir: Path
    digest: str
    policy_name: str
    policy_contract_sha256: str
    case_ids: tuple[str, ...]
    parent_report_sha256: str
    report_sha256: str
    result_sha256: Optional[str]
    #: The unqualified candidate the certified copy was made from.
    candidate_digest: Optional[str] = None


# -- the fixed contract ----------------------------------------------------------------


def _template(workflow: Workflow) -> Any:
    manifest = workflow.manifest
    if manifest is None or manifest.provenance is None:
        return None
    return manifest.provenance.governed_authorization_template


def _consequential(workflow: Workflow) -> dict[str, Step]:
    template = _template(workflow)
    identity_steps = set(template.required_identity_step_ids if template else ())
    return {
        step.id: step
        for step in workflow.steps
        if step.effects or step.api_binding is not None or step.id in identity_steps
    }


def _write_target(step: Step) -> dict[str, Any]:
    binding = step.api_binding
    return {
        "effects": sorted(effect.contract_hash() for effect in step.effects),
        "risk": step.risk,
        "identity_armed": step.identity_armed,
        "api": (
            None
            if binding is None
            else {
                "kind": binding.kind,
                "url_template": binding.url_template,
                "form_path": (
                    binding.web_form.form_path if binding.web_form is not None else None
                ),
                "effects": sorted(e.contract_hash() for e in binding.effects),
            }
        ),
    }


_TEMPLATE_FIXED = (
    "policy_name",
    "policy_contract_sha256",
    "execution_profile",
    "minimum_effect_tier",
    "qualified_effect_requirements",
    "required_identity_step_ids",
    "identity_contract_sha256",
    "parameter_contract_sha256",
)


def fixed_contract_refusal(parent: Workflow, child: Workflow) -> Optional[str]:
    """Why ``child`` changes what its parent writes, or None when it doesn't.

    Compares every consequential step (one that declares effects, carries an
    API binding, or must verify identity) and the certified template. The
    on-screen target of a step may differ; that is what a repair changes.
    """

    from openadapt_flow.jit.signature import record_check_digest

    if sorted(parent.params) != sorted(child.params):
        return "the repair changes the program's parameters"
    if record_check_digest(parent) != record_check_digest(child):
        return "the repair changes the program's record check"
    before, after = _consequential(parent), _consequential(child)
    if sorted(before) != sorted(after):
        return "the repair adds or removes a step that writes or checks identity"
    for step_id, step in before.items():
        if _write_target(step) != _write_target(after[step_id]):
            return (
                f"the repair changes what step {step_id!r} writes, where, or "
                "its identity rule"
            )
    parent_template, child_template = _template(parent), _template(child)
    if parent_template is None or child_template is None:
        return "a re-admitted bundle needs the certified authorization template"
    for field in _TEMPLATE_FIXED:
        if getattr(parent_template, field) != getattr(child_template, field):
            return f"the repair changes the certified {field.replace('_', ' ')}"
    return None


# -- re-qualification ----------------------------------------------------------------


def _case_ids(workflow: Workflow) -> tuple[str, ...]:
    project = workflow.qualification
    return tuple(sorted(case.id for case in project.cases)) if project else ()


def requalify(
    parent_bundle: Path | str,
    candidate_bundle: Path | str,
    out_dir: Path | str,
    requalification: Requalification,
) -> Requalified:
    """Run the parent's qualification campaign on the candidate, on a test copy.

    The campaign uses the parent's certify policy and the same cases, and its
    certified copy must keep the parent's policy pack version and fixed
    contract. Raises :class:`ReadmissionRefused` otherwise; the candidate is
    then never admitted.
    """

    parent = Workflow.load(parent_bundle)
    parent_template = _template(parent)
    provenance = parent.manifest.provenance if parent.manifest else None
    if (
        parent_template is None
        or provenance is None
        or not provenance.certified
        or not provenance.policy_name
    ):
        raise ReadmissionRefused(
            "the running bundle is not certified, so a repair of it can't be "
            "re-qualified under its policy"
        )
    case_ids = _case_ids(parent)
    if not case_ids:
        raise ReadmissionRefused("the running bundle names no qualification cases")
    run: Callable[..., Any]
    if requalification.run is not None:
        run = requalification.run
    else:
        from openadapt_flow.qualify.campaign import qualify

        run = qualify
    # Each case run's authorization id is its run id, and a process refuses an
    # authorization id it has already consumed. The parent was qualified with
    # the campaign's own run ids, possibly in this same process, so a
    # re-qualification names its runs afresh.
    run_id_prefix = f"requalify-{uuid.uuid4().hex[:16]}"
    try:
        result = run(
            Path(candidate_bundle),
            requalification.config,
            out=Path(out_dir),
            cases=list(case_ids),
            trials=requalification.trials,
            certify=provenance.policy_name,
            hooks=requalification.hooks,
            run_id_prefix=run_id_prefix,
        )
    except Exception as exc:  # noqa: BLE001 - every campaign failure refuses
        raise ReadmissionRefused(
            f"the repaired bundle failed re-qualification ({type(exc).__name__}: "
            f"{str(exc)[:300]})"
        ) from None
    if not getattr(result, "passed", False) or not getattr(result, "certified", False):
        raise ReadmissionRefused(
            "the repaired bundle did not pass and certify its parent's campaign"
        )
    child_dir = Path(result.bundle_dir)
    child = Workflow.load(child_dir)
    child_template = _template(child)
    if child_template is None or _case_ids(child) != case_ids:
        raise ReadmissionRefused(
            "re-qualification used other cases than the running bundle's"
        )
    refusal = fixed_contract_refusal(parent, child)
    if refusal is not None:
        raise ReadmissionRefused(refusal + "; it needs a person's fresh admission")
    # Workflow.load re-verified the seal of the certified copy.
    assert child.manifest is not None
    result_doc = getattr(result, "result", None)
    return Requalified(
        bundle_dir=child_dir,
        digest=child.manifest.content_digest,
        policy_name=child_template.policy_name,
        policy_contract_sha256=child_template.policy_contract_sha256,
        case_ids=case_ids,
        parent_report_sha256=parent_template.qualification_report_sha256,
        report_sha256=child_template.qualification_report_sha256,
        result_sha256=(
            hashlib.sha256(_canonical(result_doc)).hexdigest()
            if isinstance(result_doc, dict)
            else None
        ),
    )


# -- the signed lineage ----------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_path(store_root: Path | str, digest: str) -> Path:
    return Path(store_root) / READMISSIONS_DIR / f"{digest}.json"


def _read(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _key_dir(key_dir: Optional[Path]) -> Path:
    if key_dir is not None:
        return Path(key_dir)
    from openadapt_flow.jit.lifecycle import default_key_dir

    return default_key_dir()


def admit(
    store_root: Path | str,
    *,
    parent_bundle: Path | str,
    candidate_digest: str,
    candidate_id: str,
    requalified: Requalified,
    approved_by: str,
    route: str,
    key_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Sign the lineage record that re-admits ``requalified``.

    ``approved_by`` is the person who approved the repair, or
    ``standard-change:<policy>`` for a fix a signed repair policy allowed.
    Raises :class:`ReadmissionRefused` when the parent's chain is already
    :data:`MAX_HOPS` long or doesn't verify.
    """

    from openadapt_flow.execute.keys import (
        fingerprint_of,
        load_or_create_private_key,
        sign_bytes,
    )

    if not approved_by.strip():
        raise ReadmissionRefused("a re-admission names who approved the repair")
    parent = Workflow.load(parent_bundle)
    assert parent.manifest is not None
    parent_digest = parent.manifest.content_digest
    if parent_digest == requalified.digest:
        raise ReadmissionRefused("a repair must be a new bundle version")
    parent_record = _read(record_path(store_root, parent_digest))
    hop = 1
    if parent_record is not None:
        hop = int(parent_record.get("hop", 0)) + 1
    if hop > MAX_HOPS:
        raise ReadmissionRefused(
            f"this bundle has been repaired {MAX_HOPS} times since a person "
            "admitted it; the next version needs a person's fresh admission"
        )
    keys = _key_dir(key_dir)
    private = load_or_create_private_key(keys)
    body: dict[str, Any] = {
        "schema": LINEAGE_SCHEMA,
        "hop": hop,
        "digest": requalified.digest,
        "parent_digest": parent_digest,
        "parent_record_sha256": (
            hashlib.sha256(_canonical(parent_record)).hexdigest()
            if parent_record is not None
            else None
        ),
        "candidate_digest": candidate_digest,
        "candidate_id": candidate_id,
        "route": route,
        "approved_by": approved_by,
        "policy_name": requalified.policy_name,
        "policy_contract_sha256": requalified.policy_contract_sha256,
        "case_ids": list(requalified.case_ids),
        "parent_qualification_report_sha256": requalified.parent_report_sha256,
        "qualification_report_sha256": requalified.report_sha256,
        "qualification_result_sha256": requalified.result_sha256,
        "admitted_at": _now(),
        "signing_key": fingerprint_of(private.public_key()),
    }
    record = {**body, "signature": sign_bytes(private, _canonical(body))}
    path = record_path(store_root, requalified.digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
    tmp.replace(path)
    if lineage_refusal(store_root, parent_digest, requalified.digest, key_dir=keys):
        # Our own record must verify; refuse rather than leave a bad one.
        path.unlink(missing_ok=True)
        raise ReadmissionRefused("the new lineage record does not verify")
    return record


def _revoked(store_root: Path) -> set[str]:
    path = store_root / READMISSIONS_DIR / REVOKED_KEYS_FILE
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        # An unreadable revocation list revokes everything.
        return {"*"}
    return {str(item) for item in value} if isinstance(value, list) else {"*"}


def _bundle(
    store_root: Path, digest: str, bundles: Sequence[Path]
) -> Optional[Workflow]:
    for root in bundles:
        candidate = root / digest
        if candidate.is_dir():
            try:
                workflow = Workflow.load(candidate)
            except Exception:  # noqa: BLE001 - an unreadable bundle doesn't verify
                return None
            if (
                workflow.manifest is not None
                and workflow.manifest.content_digest == digest
            ):
                return workflow
    return None


def lineage_refusal(
    store_root: Path | str,
    root_digest: str,
    digest: str,
    *,
    key_dir: Optional[Path] = None,
) -> Optional[str]:
    """Why ``digest`` is not a re-admitted descendant of ``root_digest``.

    None when a chain of lineage records leads from ``digest`` back to
    ``root_digest``: at most :data:`MAX_HOPS` hops, no cycle, each record
    signed by the local admission key and not revoked, each parent record
    unchanged since its child was signed, and each bundle staged in the repair
    store, sealed, and keeping its parent's fixed contract.
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from openadapt_flow.execute.keys import fingerprint_of, verify_signature

    store_root = Path(store_root)
    if root_digest == digest:
        return None
    keys = _key_dir(key_dir)
    pub_file = keys / "keys" / "ed25519.pub"
    if not pub_file.is_file():
        return "no trusted admission key to verify the repair lineage"
    try:
        public = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(pub_file.read_text().strip())
        )
    except ValueError:
        return "the trusted admission key is unreadable"
    trusted = fingerprint_of(public)
    revoked = _revoked(store_root)
    bundles = [store_root / "bundles"]
    seen: set[str] = set()
    current = digest
    child_record: Optional[dict[str, Any]] = None
    for _ in range(MAX_HOPS + 1):
        if current == root_digest:
            return None
        if current in seen:
            return "the repair lineage has a cycle"
        seen.add(current)
        record = _read(record_path(store_root, current))
        if record is None:
            return f"no lineage record for bundle {current[:16]}"
        body = {k: v for k, v in record.items() if k != "signature"}
        if (
            body.get("schema") != LINEAGE_SCHEMA
            or body.get("digest") != current
            or body.get("signing_key") != trusted
            or not verify_signature(
                public, _canonical(body), str(record.get("signature", ""))
            )
        ):
            return f"the lineage record for {current[:16]} does not verify"
        if "*" in revoked or body.get("signing_key") in revoked:
            return "the lineage record was signed by a revoked admission key"
        hop = body.get("hop")
        if not isinstance(hop, int) or not 1 <= hop <= MAX_HOPS:
            return f"the repair lineage is longer than {MAX_HOPS} hops"
        if child_record is not None and child_record.get("hop") != hop + 1:
            return "the repair lineage's hops are out of order"
        parent_digest = str(body.get("parent_digest") or "")
        parent_record = _read(record_path(store_root, parent_digest))
        expected = (
            hashlib.sha256(_canonical(parent_record)).hexdigest()
            if parent_record is not None
            else None
        )
        if body.get("parent_record_sha256") != expected:
            return "a parent lineage record changed after its child was signed"
        if (hop == 1) != (parent_record is None):
            return "the repair lineage's first hop doesn't start at an admitted bundle"
        child = _bundle(store_root, current, bundles)
        parent = _bundle(store_root, parent_digest, bundles)
        if child is None or parent is None:
            return "a bundle in the repair lineage is not staged in the repair store"
        refusal = fixed_contract_refusal(parent, child)
        if refusal is not None:
            return refusal
        child_record = body
        current = parent_digest
    return f"the repair lineage is longer than {MAX_HOPS} hops"


def lineage_check(
    store_root: Path | str, *, key_dir: Optional[Path] = None
) -> Callable[[str, str], Optional[str]]:
    """``(pinned, actual) -> refusal`` for a pin or a switch record."""

    def check(pinned: str, actual: str) -> Optional[str]:
        return lineage_refusal(store_root, pinned, actual, key_dir=key_dir)

    return check
