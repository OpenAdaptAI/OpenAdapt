"""DRAFT to ADMITTED: the states of a compiled CUA run, and what admission needs.

A bundle compiled from a CUA run starts as a ``DRAFT``. A draft never runs
unattended: :meth:`~openadapt_flow.jit.CompiledAgent.run` refuses to execute
it, and the only run a draft gets is an attended canary. A draft becomes
``ADMITTED`` only when all four of these hold for the same bundle digest:

1. ``reviewed``: a named human principal reviewed every step
   (:func:`record_review`). The review also confirms the task's parameters,
   which recompiles the bundle, so the review pins the final digest.
2. ``readback``: every write step has an independent read-back. Each effect
   on a write step is checked against the verifier attached for the canary
   (:func:`readback_coverage`). A read through the system of record, or
   through a separate session, is ``independent_readback``. A read through
   the acting screen is ``screen_readback`` and does not count.
3. ``canary``: the bundle ran once on the target app, attended by a named
   principal, and ended ``VERIFIED`` with zero model calls
   (:func:`record_canary`).
4. ``signed``: :func:`admit` writes an admission manifest that records who
   admitted it (``admitted_by_principal_id``) and when, signed with the
   local Ed25519 admission key.

Who can be a principal is checked by identity only: the principal can't be
the agent that made the recording, or any ``agent:`` or ``model:`` identity.
A model never authorizes anything, and a CUA run can't approve its own
contract. Flow can't prove a person typed the name; it records the name it
was given and refuses the identities it knows are not people.

``REJECTED`` is terminal: :func:`reject` moves the draft aside, and the next
run records again.
"""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ADMISSION_FILE",
    "ADMISSION_SCHEMA",
    "TRANSITIONS",
    "AdmissionError",
    "BundleState",
    "admission_status",
    "admit",
    "check_principal",
    "default_key_dir",
    "readback_coverage",
    "record_canary",
    "record_review",
    "reject",
    "state_of",
    "verify_admission",
]

CANDIDATE_FILE = "candidate.json"
REVIEW_FILE = "review.json"
CANARY_FILE = "canary.json"
EVENTS_FILE = "lifecycle.jsonl"
ADMISSION_FILE = "admission.json"
ADMISSION_SCHEMA = "openadapt-flow/jit-admission/v1"
KEY_DIR_ENV = "OPENADAPT_JIT_KEY_DIR"

INDEPENDENT_READBACK = "independent_readback"
SCREEN_READBACK = "screen_readback"

_NOT_PRINCIPALS = frozenset(
    {"", "agent", "model", "assistant", "cua", "system", "auto", "automatic"}
)
_NOT_PRINCIPAL_PREFIXES = ("agent:", "model:", "cua:", "llm:")


class AdmissionError(RuntimeError):
    """The draft can't move to the requested state as it stands."""


class BundleState(str, Enum):
    DRAFT = "DRAFT"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"


#: The only transitions there are. Nothing leaves ADMITTED or REJECTED; a
#: changed program is a new recording and a new draft.
TRANSITIONS: Mapping[BundleState, frozenset[BundleState]] = {
    BundleState.DRAFT: frozenset({BundleState.ADMITTED, BundleState.REJECTED}),
    BundleState.ADMITTED: frozenset(),
    BundleState.REJECTED: frozenset(),
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read(path: Path) -> Optional[dict[str, Any]]:
    return json.loads(path.read_text()) if path.is_file() else None


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _event(program_dir: Path, kind: str, **detail: Any) -> None:
    with (program_dir / EVENTS_FILE).open("a") as f:
        f.write(json.dumps({"at": _now(), "event": kind, **detail}) + "\n")


def _canonical(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def default_key_dir() -> Path:
    """Where the admission signing key lives: ``$OPENADAPT_JIT_KEY_DIR`` or
    ``~/.openadapt/jit``. Keep it out of the cache directory, so whoever can
    edit a bundle can't also re-sign it."""

    return Path(os.environ.get(KEY_DIR_ENV) or Path.home() / ".openadapt" / "jit")


def _load_workflow(bundle_dir: Path) -> Any:
    from openadapt_flow.ir import Workflow

    return Workflow.load(bundle_dir)


def _digest(workflow: Any) -> str:
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not digest:
        raise AdmissionError("the compiled bundle carries no content digest")
    return str(digest)


# -- principals ----------------------------------------------------------------


def check_principal(principal_id: Any, candidate: Mapping[str, Any]) -> str:
    """Return ``principal_id`` if it can review, attend or admit this draft.

    Raises:
        AdmissionError: no name, an agent or model identity, or the identity
            that made the recording.
    """

    if not isinstance(principal_id, str) or not principal_id.strip():
        raise AdmissionError("a named human principal is required")
    name = principal_id.strip()
    lower = name.lower()
    if lower in _NOT_PRINCIPALS or lower.startswith(_NOT_PRINCIPAL_PREFIXES):
        raise AdmissionError(
            f"{name!r} is not a human principal; a model or agent never authorizes"
        )
    recorded = {
        str(v).strip().lower()
        for v in (
            candidate.get("recorded_by"),
            candidate.get("agent_principal_id"),
            candidate.get("dialect"),
        )
        if v
    }
    if lower in recorded:
        raise AdmissionError(
            f"{name!r} made the recording; a run can't approve its own contract"
        )
    return name


# -- read-back coverage ----------------------------------------------------------


def readback_coverage(workflow: Any, verifier: Any) -> list[dict[str, Any]]:
    """For every write step, what reads its effects back.

    A write step is any step :func:`openadapt_flow.run_gate.is_consequential`
    flags (fail-closed). Each effect gets ``evidence``:

    * ``independent_readback``: the contract is not an on-screen read, and the
      attached verifier reads it outside the acting session (tier 1 or 2).
    * ``screen_readback``: the contract, or the verifier's read of it, goes
      through the acting screen.
    * ``placeholder``: no observed system-of-record delta was bound.
    * ``unverified``: no verifier is attached, or it declares no tier.

    A step is ``ok`` only when it has at least one effect and every effect is
    ``independent_readback``.
    """

    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.verification import (
        declared_effect_is_independent_read,
        verifier_effect_tier,
    )

    rows: list[dict[str, Any]] = []
    for step in workflow.steps:
        if not is_consequential(step, workflow):
            continue
        effects = []
        for effect in step.effects:
            tier = verifier_effect_tier(verifier, effect) if verifier else None
            if effect.needs_operator_confirmation:
                evidence = "placeholder"
            elif not declared_effect_is_independent_read(effect):
                evidence = SCREEN_READBACK
            elif tier is None:
                evidence = "unverified"
            elif tier.is_independent_of_actuation():
                evidence = INDEPENDENT_READBACK
            else:
                evidence = SCREEN_READBACK
            effects.append(
                {
                    "kind": effect.kind.value,
                    "evidence": evidence,
                    "tier": tier.name if tier is not None else None,
                }
            )
        rows.append(
            {
                "step_id": step.id,
                "effects": effects,
                "ok": bool(effects)
                and all(e["evidence"] == INDEPENDENT_READBACK for e in effects),
            }
        )
    return rows


def _readback_problems(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    problems = []
    for row in rows:
        if row["ok"]:
            continue
        if not row["effects"]:
            problems.append(f"{row['step_id']}: a write step with no read-back")
        for e in row["effects"]:
            if e["evidence"] != INDEPENDENT_READBACK:
                problems.append(f"{row['step_id']}: {e['kind']} is {e['evidence']}")
    return problems


# -- state ---------------------------------------------------------------------


def state_of(
    program_dir: Path | str, *, key_dir: Optional[Path] = None
) -> Optional[BundleState]:
    """``ADMITTED`` when the bundle carries a manifest that verifies, ``DRAFT``
    when a candidate waits, ``REJECTED`` for a rejected draft, else ``None``.

    Raises:
        AdmissionError: the bundle carries a manifest that does not verify.
    """

    program = Path(program_dir)
    if (program / "rejected.json").is_file():
        return BundleState.REJECTED
    if (program / "bundle" / ADMISSION_FILE).is_file():
        # A manifest that does not verify admits nothing, and the bundle is
        # not a draft either: runs refuse it (see verify_admission).
        verify_admission(program / "bundle", key_dir=key_dir)
        return BundleState.ADMITTED
    if (program / CANDIDATE_FILE).is_file():
        return BundleState.DRAFT
    return None


def _evidence_at(path: Path, digest: str) -> Optional[dict[str, Any]]:
    record = _read(path)
    if record is None or record.get("digest") != digest:
        return None
    return record


def admission_status(
    program_dir: Path | str, *, key_dir: Optional[Path] = None
) -> dict[str, Any]:
    """What the draft has and what it still needs, pinned to its digest."""

    program = Path(program_dir)
    candidate = _read(program / CANDIDATE_FILE)
    if candidate is None:
        raise AdmissionError(f"no draft in {program}")
    workflow = _load_workflow(program / "bundle")
    digest = _digest(workflow)
    review = _evidence_at(program / REVIEW_FILE, digest)
    canary = _evidence_at(program / CANARY_FILE, digest)
    readback = (canary or {}).get("readback")
    problems = _readback_problems(readback) if readback is not None else None
    try:
        state = (state_of(program, key_dir=key_dir) or BundleState.DRAFT).value
    except AdmissionError as exc:
        state = f"REFUSED: {exc}"
    return {
        "state": state,
        "digest": digest,
        "steps": [
            {"id": s.id, "action": s.action.value, "intent": s.intent}
            for s in workflow.steps
        ],
        "requirements": {
            "reviewed": {
                "met": review is not None,
                "by": (review or {}).get("principal_id"),
            },
            "readback": {
                "met": problems == [],
                "problems": problems
                if problems is not None
                else ["checked at the canary; no canary for this digest yet"],
            },
            "canary": {
                "met": bool(canary and canary.get("passed")),
                "outcome": (canary or {}).get("outcome"),
            },
        },
    }


# -- transitions ---------------------------------------------------------------


def _require_draft(program: Path) -> dict[str, Any]:
    candidate = _read(program / CANDIDATE_FILE)
    if candidate is None:
        raise AdmissionError("no draft to act on")
    if (program / "rejected.json").is_file():
        raise AdmissionError("the draft was rejected")
    if (program / "bundle" / ADMISSION_FILE).is_file():
        raise AdmissionError("the bundle already carries an admission manifest")
    return candidate


def _confirm_ok(confirm: object) -> bool:
    from openadapt_flow.authoring import _confirm_is_ok

    return bool(_confirm_is_ok(confirm))


def record_review(
    program_dir: Path | str,
    *,
    principal_id: str,
    reviewed_steps: Sequence[str],
    accept_params: Sequence[str],
    confirm: object = None,
) -> dict[str, Any]:
    """A named human reviewed the steps and confirmed the parameters.

    ``accept_params`` must be exactly the task's parameters. They are applied
    by recompiling the recording
    (:func:`~openadapt_flow.compiler.param_confirm.apply_decisions`), and
    ``reviewed_steps`` must then be exactly the recompiled bundle's step ids,
    in order: the review is of the program that will run. The review pins
    that bundle's digest; a canary or admission for any other digest refuses.
    """

    from openadapt_flow.compiler.param_confirm import (
        apply_decisions,
        decisions_from_accept_list,
        load_proposals,
    )

    program = Path(program_dir)
    candidate = _require_draft(program)
    name = check_principal(principal_id, candidate)
    if not _confirm_ok(confirm):
        raise AdmissionError("review accepts confirm empty, ok, yes, or True")
    task_params = sorted((candidate.get("task") or {}).get("params") or {})
    if sorted(accept_params) != task_params:
        raise AdmissionError(
            f"accept_params {sorted(accept_params)} must be the task's "
            f"parameters {task_params}"
        )
    bundle = program / "bundle"
    if accept_params and not candidate.get("params_applied"):
        try:
            decisions = decisions_from_accept_list(
                load_proposals(bundle), list(accept_params)
            )
        except ValueError as exc:
            raise AdmissionError(str(exc)) from exc
        if decisions:
            apply_decisions(
                candidate["compiled_recording"],
                bundle,
                name=candidate["task"]["task_id"],
                decisions=decisions,
                mine_effects=bool(candidate.get("mine_effects", True)),
                bind_effect_params=True,
            )
        candidate["params_applied"] = sorted(accept_params)
        _write(program / CANDIDATE_FILE, candidate)
    workflow = _load_workflow(bundle)
    if sorted(workflow.params) != task_params:
        raise AdmissionError(
            f"the program's parameters {sorted(workflow.params)} do not match "
            f"the task's {task_params}"
        )
    step_ids = [s.id for s in workflow.steps]
    if list(reviewed_steps) != step_ids:
        raise AdmissionError(
            f"reviewed_steps must be every step of the program, in order: {step_ids}"
        )
    record = {
        "principal_id": name,
        "reviewed_at": _now(),
        "digest": _digest(workflow),
        "steps": step_ids,
        "params": sorted(accept_params),
    }
    _write(program / REVIEW_FILE, record)
    _event(program, "reviewed", principal_id=name, digest=record["digest"])
    return record


def record_canary(
    program_dir: Path | str,
    *,
    principal_id: str,
    digest: str,
    report: Any,
    readback: Sequence[Mapping[str, Any]],
    target_origin: str,
    run_dir: Optional[Path],
) -> dict[str, Any]:
    """Record an attended canary run. It passes only when it ended
    ``VERIFIED`` with zero model calls and every write step had an
    independent read-back."""

    program = Path(program_dir)
    candidate = _require_draft(program)
    name = check_principal(principal_id, candidate)
    outcome = str(report.execution_outcome) if report is not None else "refused"
    model_calls = int(report.model_calls) if report is not None else None
    problems = _readback_problems(readback)
    record = {
        "principal_id": name,
        "ran_at": _now(),
        "digest": digest,
        "target_origin": target_origin,
        "outcome": outcome,
        "model_calls": model_calls,
        "run_dir": str(run_dir) if run_dir else None,
        "readback": list(readback),
        "passed": outcome == "VERIFIED" and model_calls == 0 and not problems,
    }
    _write(program / CANARY_FILE, record)
    _event(program, "canary", principal_id=name, digest=digest, passed=record["passed"])
    return record


def admit(
    program_dir: Path | str,
    *,
    principal_id: str,
    confirm: object = None,
    key_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Move a DRAFT to ADMITTED and write the signed admission manifest.

    Refuses unless the review, the read-back coverage and a passing canary
    are all for the bundle's current digest, there are no placeholder
    effects, and the program passes its policy.
    """

    from openadapt_flow.execute.keys import (
        fingerprint_of,
        load_or_create_private_key,
        sign_bytes,
    )
    from openadapt_flow.policy import evaluate_policy, load_policy

    program = Path(program_dir)
    candidate = _require_draft(program)
    name = check_principal(principal_id, candidate)
    if not _confirm_ok(confirm):
        raise AdmissionError("admit accepts confirm empty, ok, yes, or True")
    bundle = program / "bundle"
    workflow = _load_workflow(bundle)
    digest = _digest(workflow)
    review = _evidence_at(program / REVIEW_FILE, digest)
    if review is None:
        raise AdmissionError(
            "no human review of this bundle; review it first (the digest changed "
            "or there was no review)"
        )
    canary = _evidence_at(program / CANARY_FILE, digest)
    if canary is None or not canary.get("passed"):
        raise AdmissionError("no passing canary on the target app for this bundle")
    problems = _readback_problems(canary.get("readback") or [])
    if problems:
        raise AdmissionError(
            "a write step lacks an independent read-back: " + "; ".join(problems)
        )
    placeholders = sorted(
        {
            s.id
            for s in workflow.steps
            for e in s.effects
            if e.needs_operator_confirmation
        }
    )
    if placeholders:
        raise AdmissionError(
            f"placeholder effects on {placeholders}; nothing to verify"
        )
    policy = str(candidate.get("policy") or "")
    if not policy:
        raise AdmissionError("the draft names no policy")
    report = evaluate_policy(
        workflow, load_policy(policy), require_current_risk_certification=True
    )
    if not report.passed:
        raise AdmissionError(f"not certified under {policy!r}:\n{report.render()}")
    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    private = load_or_create_private_key(keys)
    body = {
        "schema": ADMISSION_SCHEMA,
        "state": BundleState.ADMITTED.value,
        "workflow_id": candidate.get("workflow_id"),
        "digest": digest,
        "task": candidate.get("task"),
        "params": review["params"],
        "policy": policy,
        "admitted_by_principal_id": name,
        "admitted_at": _now(),
        "reviewed_by_principal_id": review["principal_id"],
        "reviewed_at": review["reviewed_at"],
        "reviewed_steps": review["steps"],
        "canary": {
            k: canary[k]
            for k in (
                "principal_id",
                "ran_at",
                "target_origin",
                "outcome",
                "model_calls",
            )
        },
        "readback": canary["readback"],
        "recorded_by": candidate.get("recorded_by"),
        "model_key": candidate.get("model_key"),
        "signing_key": fingerprint_of(private.public_key()),
    }
    manifest = {**body, "signature": sign_bytes(private, _canonical(body))}
    _write(bundle / ADMISSION_FILE, manifest)
    _event(program, "admitted", principal_id=name, digest=digest)
    return manifest


def verify_admission(
    bundle_dir: Path | str, *, key_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Return the admission manifest if it is signed by the local admission
    key and pins the bundle's current digest.

    Raises:
        AdmissionError: no manifest, a bad signature, an unknown key, or a
            bundle that changed after admission.
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from openadapt_flow.execute.keys import fingerprint_of, verify_signature

    bundle = Path(bundle_dir)
    manifest = _read(bundle / ADMISSION_FILE)
    if manifest is None:
        raise AdmissionError("no admission manifest")
    if manifest.get("schema") != ADMISSION_SCHEMA:
        raise AdmissionError("not a JIT admission manifest")
    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    pub_file = keys / "keys" / "ed25519.pub"
    if not pub_file.is_file():
        raise AdmissionError(f"no trusted admission key at {pub_file}")
    public = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(pub_file.read_text().strip())
    )
    body = {k: v for k, v in manifest.items() if k != "signature"}
    if body.get("signing_key") != fingerprint_of(public) or not verify_signature(
        public, _canonical(body), str(manifest.get("signature", ""))
    ):
        raise AdmissionError("the admission manifest's signature does not verify")
    if not body.get("admitted_by_principal_id"):
        raise AdmissionError("the admission manifest names no principal")
    if body.get("state") != BundleState.ADMITTED.value:
        raise AdmissionError("the manifest does not admit the bundle")
    try:
        digest = _digest(_load_workflow(bundle))
    except Exception as exc:  # noqa: BLE001 - integrity failure is a refusal
        raise AdmissionError(f"the bundle failed its integrity check: {exc}") from exc
    if digest != body.get("digest"):
        raise AdmissionError("the bundle is not the one that was admitted")
    return manifest


def reject(program_dir: Path | str, *, principal_id: str, reason: str = "") -> Path:
    """Move a DRAFT to REJECTED. The draft is kept, renamed, for the record."""

    program = Path(program_dir)
    candidate = _require_draft(program)
    name = check_principal(principal_id, candidate)
    _write(
        program / "rejected.json",
        {"principal_id": name, "reason": reason, "rejected_at": _now()},
    )
    _event(program, "rejected", principal_id=name)
    stamp = (
        time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 10**9:09d}"
    )
    kept = program.parent / f"rejected-{stamp}"
    program.rename(kept)
    return kept
