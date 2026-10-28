"""DRAFT to ADMITTED: the states of a compiled CUA run, and what admission needs.

A bundle compiled from a CUA run starts as a ``DRAFT``. A draft never runs
unattended. :meth:`openadapt_flow.runtime.Replayer.run`, which every
execution path goes through (``replay``, ``run``, ``serve-execute``, an
emitted MCP server, :meth:`~openadapt_flow.jit.CompiledAgent.run`), refuses a
bundle marked as a draft unless it carries a valid admission
(:func:`ensure_runnable`). The only run a draft gets is an attended canary.

A draft is admitted in one of three modes, all writing the same signed
manifest: a registered agent under a signed admission policy, or a
registered admitter key (:func:`admit_with_key`, see
:mod:`openadapt_flow.jit.authority`), or a person (:func:`admit`). For a
person, ``ADMITTED`` needs all of these for the same bundle digest, in this
order:

1. ``params``: a named person confirms the task's parameters
   (:func:`confirm_params`). This recompiles the bundle, so it comes before
   the review, and it can't run after one.
2. ``reviewed``: a named person reviewed every step of that program
   (:func:`record_review`).
3. ``readback``: every write step has an independent read-back through the
   verifier attached for the canary (:func:`readback_coverage`). Only a read
   of the system of record (tier 1) is ``independent_readback``. A read
   through the application's screens, in the acting session or a separate
   one, is ``screen_readback`` and does not count.
4. ``canary``: the bundle ran once on the target app, attended by a named
   person, and ended ``VERIFIED`` with zero model calls
   (:func:`record_canary`).
5. ``signed``: :func:`admit` writes an admission manifest that records who
   admitted it (``admitted_by_principal_id``) and when, signed with the
   local Ed25519 admission key.

The draft state is ``Workflow.origin`` (:data:`DRAFT_ORIGIN`), inside the
content digest, so deleting the ``jit_draft.json`` label frees nothing.

A plain name is a person's name, checked by identity only: it can't be the
identity that made the recording, or an ``agent:``, ``model:``, ``bot:`` or
similar identity. Flow can't prove a person typed the name. The command line
also requires an interactive terminal for a person. An agent or admitter key
acts as a registered :class:`~openadapt_flow.jit.authority.Principal`, and
it can't be the identity that made the recording either. Who made the
recording comes from the draft's ``recorder`` block, signed with the local
admission key over the bundle digest (:func:`sign_recorder`), not from the
writable fields of ``candidate.json``. An agent can't act on a draft whose
recording names no agent principal.

``REJECTED`` is terminal: :func:`reject` moves the draft aside, and the next
run records again.
"""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.assurance import INDEPENDENT_READBACK, SCREEN_READBACK

__all__ = [
    "ADMISSION_FILE",
    "DRAFT_MARKER",
    "DRAFT_ORIGIN",
    "ADMISSION_SCHEMA",
    "TRANSITIONS",
    "AdmissionError",
    "BundleState",
    "admission_status",
    "admit",
    "check_principal",
    "confirm_params",
    "default_key_dir",
    "mark_draft",
    "readback_coverage",
    "record_canary",
    "record_review",
    "refuse_unadmitted_draft",
    "DraftNotAdmitted",
    "draft_label",
    "ensure_runnable",
    "reject",
    "resolve_actor",
    "sign_recorder",
    "state_of",
    "verify_admission",
    "admit_with_key",
    "is_jit_bundle",
    "is_not_a_person_name",
    "needs_person_queue",
]

CANDIDATE_FILE = "candidate.json"
REVIEW_FILE = "review.json"
CANARY_FILE = "canary.json"
PARAMS_FILE = "params.json"
#: Written into a draft's bundle, for people and tools that read the folder.
#: It is a label only: the draft state is :data:`DRAFT_ORIGIN`.
DRAFT_MARKER = "jit_draft.json"
#: ``Workflow.origin`` of a bundle compiled from an agent run. It is inside
#: the content digest, so a copy stays a draft and removing it makes a
#: different program that nothing admitted.
DRAFT_ORIGIN = "agent_run"
#: The fields of ``candidate.json`` that say who made the recording. They are
#: signed with the bundle digest in the candidate's ``recorder`` block.
RECORDER_FIELDS = ("recorded_by", "agent_principal_id", "dialect")
EVENTS_FILE = "lifecycle.jsonl"
ADMISSION_FILE = "admission.json"
#: Every canary run, one JSON object per line. ``canary.json`` is the latest.
CANARIES_FILE = "canaries.jsonl"
#: Written when a draft is outside an agent's admission policy.
NEEDS_PERSON_FILE = "needs_person.json"
#: An admission carried to a repaired copy of an admitted program, which keeps
#: the program's record check (see :func:`admit_repair`). ``repair_probe``
#: lets the copy run only behind a gate that stops before the first save;
#: ``repair`` follows a person's approval, or a signed standard change.
REPAIR_MODES = ("repair", "repair_probe")
ADMISSION_MODES = ("person", "admitter_key", "agent_policy", *REPAIR_MODES)
#: How a standard change names itself as the approver of a repair.
STANDARD_CHANGE_APPROVER = "standard-change:"
ADMISSION_SCHEMA = "openadapt-flow/jit-admission/v1"
KEY_DIR_ENV = "OPENADAPT_JIT_KEY_DIR"


_NOT_PRINCIPALS = frozenset(
    {
        "",
        "agent",
        "model",
        "assistant",
        "cua",
        "llm",
        "ai",
        "bot",
        "system",
        "service",
        "auto",
        "automatic",
    }
)
_NOT_PRINCIPAL_PREFIXES = (
    "agent:",
    "model:",
    "cua:",
    "llm:",
    "ai:",
    "bot:",
    "service:",
    "svc:",
    "system:",
)


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


def _sign_record(record: Mapping[str, Any], key_dir: Optional[Path]) -> str:
    """Sign an evidence record with the local admission key, so evidence
    written into the cache directory by anything else counts for nothing."""

    from openadapt_flow.execute.keys import load_or_create_private_key, sign_bytes

    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    body = {k: v for k, v in record.items() if k != "signature"}
    return sign_bytes(load_or_create_private_key(keys), _canonical(body))


def _record_signed(record: Mapping[str, Any], key_dir: Optional[Path]) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from openadapt_flow.execute.keys import verify_signature

    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    pub_file = keys / "keys" / "ed25519.pub"
    if not pub_file.is_file():
        return False
    public = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(pub_file.read_text().strip())
    )
    body = {k: v for k, v in record.items() if k != "signature"}
    return verify_signature(public, _canonical(body), str(record.get("signature", "")))


def default_key_dir() -> Path:
    """Where the admission signing key lives: ``$OPENADAPT_JIT_KEY_DIR`` or
    ``~/.openadapt/jit``. It is kept out of the cache directory, so write
    access to a bundle is not enough to re-sign it. Anyone who can write the
    key directory can."""

    return Path(os.environ.get(KEY_DIR_ENV) or Path.home() / ".openadapt" / "jit")


def _load_workflow(bundle_dir: Path) -> Any:
    from openadapt_flow.ir import Workflow

    return Workflow.load(bundle_dir)


def _digest(workflow: Any) -> str:
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not digest:
        raise AdmissionError("the compiled bundle carries no content digest")
    return str(digest)


def _transition(program: Path, to: BundleState) -> None:
    """Refuse a transition :data:`TRANSITIONS` does not allow."""

    if (program / "rejected.json").is_file():
        current = BundleState.REJECTED
    elif (program / "bundle" / ADMISSION_FILE).is_file():
        current = BundleState.ADMITTED
    else:
        current = BundleState.DRAFT
    if to not in TRANSITIONS[current]:
        raise AdmissionError(f"a {current.value} bundle can't become {to.value}")


def mark_draft(bundle_dir: Path | str) -> None:
    """Mark ``bundle_dir`` as a draft.

    Sets ``Workflow.origin`` to :data:`DRAFT_ORIGIN` and re-seals the bundle,
    so the draft state is part of the content digest: deleting a file can't
    turn a draft into a plain bundle. Then writes the :data:`DRAFT_MARKER`
    label. Call it before anything pins the digest (the recorder signature,
    a review, a canary).
    """

    bundle = Path(bundle_dir)
    workflow = _load_workflow(bundle)
    if workflow.encrypted:
        raise AdmissionError("a JIT draft is compiled unencrypted; this one is not")
    if workflow.origin != DRAFT_ORIGIN:
        workflow.origin = DRAFT_ORIGIN
        workflow.save(bundle)
    _write(
        bundle / DRAFT_MARKER,
        {
            "state": BundleState.DRAFT.value,
            "note": "compiled from a computer-use agent run; runs only after "
            "admission (openadapt-flow jit)",
        },
    )


class DraftNotAdmitted(AdmissionError):
    """An execution path was asked to run a JIT draft that is not admitted."""


def _origin_on_disk(bundle: Path) -> Optional[str]:
    """``origin`` from a plaintext ``workflow.json``, without loading it.

    For the checks that run before a bundle is loaded. An encrypted bundle
    can't be read here; :meth:`Replayer.run` passes the loaded workflow.
    """

    path = bundle / "workflow.json"
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return None
    origin = data.get("origin") if isinstance(data, dict) else None
    return origin if isinstance(origin, str) else None


def is_jit_bundle(bundle_dir: Path | str, workflow: Any = None) -> bool:
    """True when ``bundle_dir`` came from an agent run: its workflow's
    ``origin`` is set, or it carries the draft marker or a JIT admission."""

    bundle = Path(bundle_dir)
    origin = (
        getattr(workflow, "origin", None)
        if workflow is not None
        else _origin_on_disk(bundle)
    )
    return bool(
        origin
        or (bundle / DRAFT_MARKER).is_file()
        or (bundle / ADMISSION_FILE).is_file()
    )


def ensure_runnable(
    bundle_dir: Path | str,
    *,
    workflow: Any = None,
    attended_by: Any = None,
    key_dir: Optional[Path] = None,
    execution_origin: Optional[str] = None,
    effect_verifier: Any = None,
    at_run: bool = False,
    stops_before_save: bool = False,
) -> None:
    """Refuse to execute a JIT draft that has no valid admission.

    A bundle is a JIT bundle when its workflow's ``origin`` is
    :data:`DRAFT_ORIGIN` (inside the content digest), or it carries the draft
    marker or an admission manifest (:func:`is_jit_bundle`). Any other bundle
    passes. A JIT bundle passes when :func:`verify_admission` accepts its
    admission, or when ``attended_by`` is attending this run (the canary): a
    person's name, a registered admitter key, or a registered agent under a
    valid signed admission policy (:func:`resolve_actor`). ``attended_by`` is
    for the in-process JIT API. No CLI or server path passes it.

    With ``at_run`` (:meth:`Replayer.run`), an admitted bundle must also match
    its admission on this run: the program's parameters are the admitted
    task's, ``execution_origin`` is the origin the canary ran on, and
    ``effect_verifier`` gives every write step an independent read-back.

    A repaired copy whose admission is only a ``repair_probe`` runs only when
    ``stops_before_save`` says the run's gate stops before the first save.

    Raises:
        DraftNotAdmitted: the draft is not admitted, this run does not match
            its admission, or ``attended_by`` can't attend it.
    """

    bundle = Path(bundle_dir)
    if not is_jit_bundle(bundle, workflow):
        return
    if attended_by is not None:
        try:
            resolve_actor(attended_by, bundle.parent, key_dir=key_dir)
        except AdmissionError as exc:
            raise DraftNotAdmitted(f"{bundle}: {exc}") from None
        return
    try:
        manifest = verify_admission(bundle, key_dir=key_dir)
    except AdmissionError as exc:
        raise DraftNotAdmitted(
            f"refused: {bundle} is a DRAFT compiled from an agent run and is not "
            f"admitted ({exc}). Review, canary and admit it first "
            "(openadapt-flow jit status)."
        ) from None
    if manifest.get("admission_mode") == "repair_probe" and not stops_before_save:
        raise DraftNotAdmitted(
            f"refused: {bundle} is a repair a person has not approved yet; it runs "
            "only as a probe that stops before the save. Nothing ran."
        )
    if not at_run:
        return
    try:
        _check_admitted_run(
            manifest,
            workflow if workflow is not None else _load_workflow(bundle),
            execution_origin=execution_origin,
            effect_verifier=effect_verifier,
        )
    except AdmissionError as exc:
        raise DraftNotAdmitted(
            f"refused: {bundle} was compiled from an agent run and admitted, and "
            f"this run does not match its admission ({exc}). Nothing ran."
        ) from None


def _check_admitted_run(
    manifest: Mapping[str, Any],
    workflow: Any,
    *,
    execution_origin: Optional[str],
    effect_verifier: Any,
) -> None:
    """What every run of an admitted bundle must match, on any path.

    The JIT API (``CompiledAgent.run``) also checks the task signature and
    the Flow policy; a replay outside it has neither to compare.
    """

    if sorted(workflow.params) != sorted(manifest.get("params") or []):
        raise AdmissionError(
            f"the program's parameters {sorted(workflow.params)} are not the "
            f"admitted task's {sorted(manifest.get('params') or [])}"
        )
    canary_origin = (manifest.get("canary") or {}).get("target_origin")
    if not execution_origin or execution_origin != canary_origin:
        raise AdmissionError(
            f"the canary ran on {canary_origin}, and this run is on "
            f"{execution_origin or 'an origin it does not name'}; admit a canary "
            "on this app"
        )
    problems = _readback_problems(readback_coverage(workflow, effect_verifier))
    if problems:
        raise AdmissionError(
            "this run's verifier gives a write step no independent read-back: "
            + "; ".join(problems)
        )


def draft_label(
    bundle_dir: Path | str, *, key_dir: Optional[Path] = None
) -> Optional[str]:
    """``"DRAFT"`` when ``bundle_dir`` is a JIT draft with no valid admission,
    else None. For paths that describe a bundle without running it."""

    try:
        ensure_runnable(bundle_dir, key_dir=key_dir)
    except DraftNotAdmitted:
        return "DRAFT"
    return None


def refuse_unadmitted_draft(
    bundle_dir: Path | str, *, key_dir: Optional[Path] = None
) -> None:
    """Raise ``SystemExit`` when ``bundle_dir`` is a JIT draft without a valid
    admission. For the ``replay`` and ``run`` commands."""

    try:
        ensure_runnable(bundle_dir, key_dir=key_dir)
    except DraftNotAdmitted as exc:
        raise SystemExit(str(exc)) from None


# -- principals ----------------------------------------------------------------


def is_not_a_person_name(name: str) -> bool:
    """True for a blank name or one that names an agent, a model or a service."""

    lower = name.strip().lower()
    return lower in _NOT_PRINCIPALS or lower.startswith(_NOT_PRINCIPAL_PREFIXES)


def sign_recorder(
    program_dir: Path | str,
    candidate: Mapping[str, Any],
    *,
    key_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Return ``candidate`` with a signed ``recorder`` block.

    The block holds :data:`RECORDER_FIELDS`, the task key and the bundle's
    current content digest, signed with the local admission key. Every
    action on the draft checks it (:func:`resolve_actor`), so editing
    ``candidate.json`` or the bundle can't change who made the recording.
    Flow re-signs it when it recompiles the draft (:func:`confirm_params`).
    """

    program = Path(program_dir)
    block: dict[str, Any] = {f: candidate.get(f) for f in RECORDER_FIELDS}
    block["task_key"] = (candidate.get("task") or {}).get("key")
    block["digest"] = _digest(_load_workflow(program / "bundle"))
    block["signature"] = _sign_record(block, key_dir)
    return {**candidate, "recorder": block}


def _recorder(program: Path, key_dir: Optional[Path]) -> dict[str, Any]:
    """The draft's signed recorder block, checked against its candidate and
    its bundle (:func:`sign_recorder`).

    Raises:
        AdmissionError: no draft record, an unsigned block, a candidate or
            bundle changed after the recording.
    """

    candidate = _read(program / CANDIDATE_FILE)
    if candidate is None:
        raise AdmissionError(
            f"no draft record ({CANDIDATE_FILE}) next to the bundle, so who made "
            "the recording is unknown; a copied draft runs only with an admission"
        )
    block = candidate.get("recorder")
    if not isinstance(block, dict) or not _record_signed(block, key_dir):
        raise AdmissionError(
            "the draft's recorder is not signed by the local admission key; "
            "record the draft again"
        )
    task_key = (candidate.get("task") or {}).get("key")
    for field in (*RECORDER_FIELDS, "task_key"):
        value = task_key if field == "task_key" else candidate.get(field)
        if block.get(field) != value:
            raise AdmissionError(
                f"{CANDIDATE_FILE} changed after it was recorded ({field}); "
                "record the draft again"
            )
    try:
        digest = _digest(_load_workflow(program / "bundle"))
    except Exception as exc:  # noqa: BLE001 - integrity failure is a refusal
        raise AdmissionError(f"the bundle failed its integrity check: {exc}") from exc
    if block.get("digest") != digest:
        raise AdmissionError(
            "the draft's recorder was signed for another bundle; the bundle "
            "changed after the recording. Record the draft again"
        )
    return block


def _is_recording_agent(name: str, recorder: Mapping[str, Any]) -> bool:
    """``name`` is the agent principal the signed recorder block names."""

    recording_agent = recorder.get("agent_principal_id")
    return bool(recording_agent) and (
        name.strip().lower() == str(recording_agent).strip().lower()
    )


def _self_check(
    name: str,
    recorder: Mapping[str, Any],
    *,
    agent: bool,
    allow_self: bool = False,
) -> None:
    """``name`` is not the identity that made the recording.

    ``agent``: ``name`` is a registered agent. An agent acts only when the
    recording names the agent that made it; otherwise Flow can't show they
    differ.

    ``allow_self``: the agent's signed admission policy lets it act on its
    own recording (``allow_self_admission``). Then the recording agent passes,
    but only as the agent principal the signed recorder block names; a match
    on any other recorded field is still refused.
    """

    recorded = {
        str(recorder[f]).strip().lower() for f in RECORDER_FIELDS if recorder.get(f)
    }
    if name.strip().lower() in recorded and not (
        agent and allow_self and _is_recording_agent(name, recorder)
    ):
        raise AdmissionError(
            f"{name!r} made the recording; a run can't approve its own contract "
            "unless its signed admission policy allows self-admission"
        )
    if agent and not recorder.get("agent_principal_id"):
        raise AdmissionError(
            f"the recording agent is unknown (the draft names no agent "
            f"principal), so {name!r} can't be shown not to be it. A person or "
            "an admitter key acts on this draft"
        )


def _policy_allows_self(actor: Any) -> bool:
    """A registered agent whose (re-verified) policy allows self-admission."""

    signed = getattr(actor, "policy", None)
    return bool(
        getattr(actor, "kind", None) == "agent"
        and signed is not None
        and signed.policy.allow_self_admission
    )


def _actor(
    actor: Any,
    recorder: Callable[[], Mapping[str, Any]],
    key_dir: Optional[Path],
) -> tuple[str, Optional[Any]]:
    """Check who ``actor`` is, then that it did not make the recording."""

    from openadapt_flow.jit import authority

    if isinstance(actor, authority.Principal):
        keys = Path(key_dir) if key_dir is not None else default_key_dir()
        try:
            authority.recheck(actor, keys)
        except authority.AuthorityError as exc:
            raise AdmissionError(str(exc)) from None
        _self_check(
            actor.principal_id,
            recorder(),
            agent=actor.kind == "agent",
            allow_self=_policy_allows_self(actor),
        )
        return actor.principal_id, actor
    name = _person_name(actor)
    _self_check(name, recorder(), agent=False)
    return name, None


def resolve_actor(
    actor: Any, program_dir: Path | str, *, key_dir: Optional[Path] = None
) -> tuple[str, Optional[Any]]:
    """Who is acting on the draft in ``program_dir``, as
    ``(principal_id, principal)``.

    A plain string is a person's name (:func:`check_principal`). A
    :class:`~openadapt_flow.jit.authority.Principal` is a registered admitter
    key or agent; it is re-verified against the registry, and an agent's
    policy against its signature and expiry. Neither can be the identity that
    made the recording, read from the draft's signed recorder block
    (:func:`sign_recorder`), and an agent can't act when that block names no
    agent principal.
    """

    return _actor(actor, lambda: _recorder(Path(program_dir), key_dir), key_dir)


def _person_name(principal_id: Any) -> str:
    if not isinstance(principal_id, str) or not principal_id.strip():
        raise AdmissionError("a named human principal is required")
    name = principal_id.strip()
    if is_not_a_person_name(name):
        raise AdmissionError(
            f"{name!r} is not a human principal; a model or agent never authorizes "
            "on its name alone (register its key under a signed admission policy)"
        )
    return name


def check_principal(
    principal_id: Any, program_dir: Path | str, *, key_dir: Optional[Path] = None
) -> str:
    """Return ``principal_id`` if a person by that name can act on the draft
    in ``program_dir``.

    A registered agent or admitter key acts as a
    :class:`~openadapt_flow.jit.authority.Principal` instead
    (:func:`resolve_actor`); its name alone gives it nothing.

    Raises:
        AdmissionError: no name, an unregistered agent or model identity, the
            identity that made the recording, or a recorder block that does
            not verify.
    """

    name = _person_name(principal_id)
    _self_check(name, _recorder(Path(program_dir), key_dir), agent=False)
    return name


# -- read-back coverage ----------------------------------------------------------


def readback_coverage(workflow: Any, verifier: Any) -> list[dict[str, Any]]:
    """For every write step, what reads its effects back.

    A write step is any step :func:`openadapt_flow.run_gate.is_consequential`
    flags (fail-closed). Each effect gets ``evidence``:

    * ``independent_readback``: the contract is not an on-screen read, and the
      attached verifier reads the system of record (tier 1).
    * ``screen_readback``: the contract, or the verifier's read of it, goes
      through the application's screens: the acting session, or a separate
      signed-in session (tier 2). Runtime ``VERIFIED`` accepts tier 2
      (``VERIFIED_EFFECT_TIER``); admission is stricter.
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
            elif tier.reads_system_of_record():
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
    params = _evidence_at(program / PARAMS_FILE, digest)
    task_params = _task_params(candidate)
    readback = (canary or {}).get("readback")
    problems = _readback_problems(readback) if readback is not None else None
    try:
        state = (state_of(program, key_dir=key_dir) or BundleState.DRAFT).value
    except AdmissionError as exc:
        state = f"REFUSED: {exc}"
    return {
        "state": state,
        "digest": digest,
        "policy": candidate.get("policy"),
        "steps": [
            {"id": s.id, "action": s.action.value, "intent": s.intent}
            for s in workflow.steps
        ],
        "requirements": {
            "params": {
                "met": not task_params or params is not None,
                "task_params": task_params,
                "by": (params or {}).get("principal_id"),
            },
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
        "needs_person": _evidence_at(program / NEEDS_PERSON_FILE, digest),
        # A policy such as clinical-write needs one on every write; a person
        # names it when confirming the parameters.
        "idempotency_keys": idempotency_keys(workflow),
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


def _task_params(candidate: Mapping[str, Any]) -> list[str]:
    return sorted((candidate.get("task") or {}).get("params") or {})


def _has_terminal() -> bool:
    """True when stdin and stdout are an interactive terminal."""

    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_ok(confirm: object) -> bool:
    from openadapt_flow.authoring import _confirm_is_ok

    return bool(_confirm_is_ok(confirm))


def confirm_params(
    program_dir: Path | str,
    *,
    principal_id: Any,
    accept_params: Sequence[str],
    confirm: object = None,
    key_dir: Optional[Path] = None,
    idempotency_key: Optional[str | Sequence[str]] = None,
) -> dict[str, Any]:
    """A named person confirms which typed values become the task's parameters.

    ``idempotency_key`` names the parameter that identifies one saved record,
    such as a claim number the app keeps unique. The person asserts that; it
    can't be read from one run. The parameter must be one the mined record
    check already selects the saved record by. It becomes that check's
    at-most-once key (:attr:`Effect.idempotency_key`), so a run counts the
    records bearing the run's value and refuses a duplicate. Without a
    parameter the record check selects by, it refuses and names the ones it
    could take.

    Several parameters (a list, such as last name, first name and birth date)
    name a key the record is unique by only together. Each must be one the
    record check selects by; they become its
    :attr:`Effect.composite_key`, and a run refuses when more than one record
    carries all of them. A one-item list is the single-parameter key.

    ``accept_params`` must be exactly the task's parameters. They are applied
    by recompiling the recording
    (:func:`~openadapt_flow.compiler.param_confirm.apply_decisions`), which can
    renumber the steps. So this comes first: after it, :func:`admission_status`
    lists the steps of the program that will run, and those are the steps
    :func:`record_review` asks for. Once a review exists it refuses; reject the
    draft to change the parameters.
    """

    from openadapt_flow.compiler.param_confirm import (
        apply_decisions,
        decisions_from_accept_list,
        load_proposals,
    )

    program = Path(program_dir)
    candidate = _require_draft(program)
    name, _ = resolve_actor(principal_id, program, key_dir=key_dir)
    if not _confirm_ok(confirm):
        raise AdmissionError("confirm_params accepts confirm empty, ok, yes, or True")
    task_params = _task_params(candidate)
    if sorted(accept_params) != task_params:
        raise AdmissionError(
            f"accept_params {sorted(accept_params)} must be the task's "
            f"parameters {task_params}"
        )
    if (program / REVIEW_FILE).is_file():
        raise AdmissionError(
            "the draft was already reviewed; parameters are confirmed before the "
            "review. Reject the draft to change them"
        )
    bundle = program / "bundle"
    if candidate.get("params_applied") is None:
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
            mark_draft(bundle)
        candidate["params_applied"] = sorted(accept_params)
        # The recompiled bundle has a new digest; the recorder is unchanged.
        candidate = sign_recorder(program, candidate, key_dir=key_dir)
        _write(program / CANDIDATE_FILE, candidate)
    workflow = _load_workflow(bundle)
    if sorted(workflow.params) != task_params:
        raise AdmissionError(
            f"the program's parameters {sorted(workflow.params)} do not match "
            f"the task's {task_params}"
        )
    key_params = _key_params(idempotency_key)
    if key_params and _set_idempotency_key(bundle, workflow, key_params):
        # The key is part of the digest; the recorder is unchanged.
        workflow = _load_workflow(bundle)
        candidate = sign_recorder(program, candidate, key_dir=key_dir)
        _write(program / CANDIDATE_FILE, candidate)
    record = {
        "principal_id": name,
        "confirmed_at": _now(),
        "digest": _digest(workflow),
        "params": sorted(accept_params),
        "steps": [s.id for s in workflow.steps],
        "idempotency_key": idempotency_keys(workflow),
    }
    _write(program / PARAMS_FILE, record)
    _event(program, "params_confirmed", principal_id=name, digest=record["digest"])
    return record


def _record_checks(workflow: Any) -> list[tuple[Any, Any]]:
    """(step, effect) for every mined record-written check."""

    return [
        (step, effect)
        for step in workflow.steps
        for effect in step.effects
        if effect.kind.value == "record_written"
        and not effect.needs_operator_confirmation
    ]


def idempotency_key_candidates(workflow: Any) -> list[str]:
    """Parameters a record-written check selects its record by."""

    return sorted(
        {
            expr.param
            for _, effect in _record_checks(workflow)
            for expr in (effect.match or {}).values()
            if getattr(expr, "param", None)
        }
    )


def idempotency_keys(workflow: Any) -> list[dict[str, Any]]:
    """The at-most-once keys the record checks carry, for the record."""

    keys: list[dict[str, Any]] = []
    for step, effect in _record_checks(workflow):
        if effect.idempotency_key is not None:
            keys.append(
                {
                    "step_id": step.id,
                    "field": effect.key_field,
                    "param": getattr(effect.idempotency_key, "param", None),
                }
            )
        elif effect.composite_key:
            keys.append(
                {
                    "step_id": step.id,
                    "fields": list(effect.composite_key),
                    "params": [
                        getattr(expr, "param", None)
                        for expr in effect.composite_key.values()
                    ],
                }
            )
    return keys


def _key_params(idempotency_key: Optional[str | Sequence[str]]) -> list[str]:
    """The key's parameter names: one for a string, several for a list."""

    if idempotency_key is None:
        return []
    if isinstance(idempotency_key, str):
        return [idempotency_key]
    params = list(idempotency_key)
    if not params or any(not isinstance(p, str) or not p for p in params):
        raise AdmissionError("idempotency_key names a parameter or a list of them")
    if len(set(params)) != len(params):
        raise AdmissionError(f"idempotency_key {params} names a parameter twice")
    return params


def _set_idempotency_key(bundle: Path, workflow: Any, params: list[str]) -> bool:
    """Key every record check that selects by all of ``params``. True if it
    changed. One parameter keys one field; several make a composite key."""

    from openadapt_flow.runtime.effects import ValueExpr

    for param in params:
        if param not in workflow.params:
            raise AdmissionError(
                f"idempotency_key {param!r} is not one of the task's parameters "
                f"{sorted(workflow.params)}"
            )
    bound: list[tuple[Any, dict[str, str]]] = []
    for step, effect in _record_checks(workflow):
        want: dict[str, str] = {}
        for param in params:
            fields = [
                field
                for field, expr in (effect.match or {}).items()
                if getattr(expr, "param", None) == param
            ]
            if len(fields) != 1:
                break
            want[fields[0]] = param
        if len(want) != len(params):
            continue
        current = {
            field: getattr(expr, "param", None)
            for field, expr in effect.key_selector().items()
        }
        if current and current != want:
            shown = ", ".join(f"{f}={p}" for f, p in current.items())
            raise AdmissionError(
                f"the record check on {step.id} already has the key {shown}; "
                "reject the draft to change it"
            )
        bound.append((effect, want))
    if not bound:
        named = params[0] if len(params) == 1 else params
        raise AdmissionError(
            f"no record check selects its record by {named!r}, so it can't be "
            "the idempotency key; parameters a record check selects by: "
            f"{idempotency_key_candidates(workflow) or 'none'}"
        )
    if all(effect.key_selector() for effect, _ in bound):
        return False
    for effect, want in bound:
        if len(want) == 1:
            ((field, param),) = want.items()
            effect.idempotency_key = ValueExpr(param=param)
            effect.key_field = field
        else:
            effect.composite_key = {
                field: ValueExpr(param=param) for field, param in want.items()
            }
    workflow.save(bundle)
    return True


def record_review(
    program_dir: Path | str,
    *,
    principal_id: str,
    reviewed_steps: Sequence[str],
    confirm: object = None,
    key_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """A named person reviewed every step of the program that will run.

    A task with parameters needs :func:`confirm_params` first.
    ``reviewed_steps`` must be exactly the bundle's step ids, in order. The
    review pins the bundle's digest; a canary or admission for any other
    digest refuses.
    """

    program = Path(program_dir)
    candidate = _require_draft(program)
    name = check_principal(principal_id, program, key_dir=key_dir)
    if not _confirm_ok(confirm):
        raise AdmissionError("review accepts confirm empty, ok, yes, or True")
    workflow = _load_workflow(program / "bundle")
    digest = _digest(workflow)
    task_params = _task_params(candidate)
    params = _evidence_at(program / PARAMS_FILE, digest)
    if task_params and params is None:
        raise AdmissionError(
            f"confirm the parameters {task_params} first (confirm_params); it "
            "recompiles the program, and the review is of the result"
        )
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
        "digest": digest,
        "steps": step_ids,
        "params": task_params,
    }
    _write(program / REVIEW_FILE, record)
    _event(program, "reviewed", principal_id=name, digest=digest)
    return record


def record_canary(
    program_dir: Path | str,
    *,
    principal_id: Any,
    digest: str,
    report: Any,
    readback: Sequence[Mapping[str, Any]],
    target_origin: str,
    run_dir: Optional[Path],
    key_dir: Optional[Path] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    """Record an attended canary run. It passes only when it ended
    ``VERIFIED`` with zero model calls and every write step had an
    independent read-back. Every run is also appended to ``canaries.jsonl``,
    which an admission policy's ``min_clean_canaries`` counts.

    ``environment`` is the app environment the canary ran in (the app's
    origin plus, when the caller names one, its environment id). The
    admission pins it, and a run in another environment is refused."""

    program = Path(program_dir)
    _require_draft(program)
    name, principal = resolve_actor(principal_id, program, key_dir=key_dir)
    outcome = str(report.execution_outcome) if report is not None else "refused"
    model_calls = int(report.model_calls) if report is not None else None
    problems = _readback_problems(readback)
    record = {
        "principal_id": name,
        "principal_kind": principal.kind if principal is not None else "person",
        "ran_at": _now(),
        "digest": digest,
        "target_origin": target_origin,
        "outcome": outcome,
        "model_calls": model_calls,
        "run_dir": str(run_dir) if run_dir else None,
        "readback": list(readback),
        "passed": outcome == "VERIFIED" and model_calls == 0 and not problems,
    }
    if environment is not None:
        # Only written when known, so canaries recorded before it verify.
        record["environment"] = environment
    record["signature"] = _sign_record(record, key_dir)
    _write(program / CANARY_FILE, record)
    with (program / CANARIES_FILE).open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    _event(program, "canary", principal_id=name, digest=digest, passed=record["passed"])
    return record


def admit(
    program_dir: Path | str,
    *,
    principal_id: str,
    confirm: object = None,
    key_dir: Optional[Path] = None,
    require_distinct_reviewer_and_admitter: bool = False,
) -> dict[str, Any]:
    """A person admits a DRAFT and the signed admission manifest is written.

    Needs an interactive terminal (stdin and stdout), the same as
    ``jit admit --principal``: a name with no terminal behind it could come
    from any script or agent. Headless callers use :func:`admit_with_key`.

    Refuses unless the review, the read-back coverage and a passing canary
    are all for the bundle's current digest, there are no placeholder
    effects, and the program passes its policy. With
    ``require_distinct_reviewer_and_admitter`` (or the same flag on the draft),
    the admitting principal must not be the reviewer.
    """

    program = Path(program_dir)
    candidate = _require_draft(program)
    _transition(program, BundleState.ADMITTED)
    _person_name(principal_id)
    if not _confirm_ok(confirm):
        raise AdmissionError("admit accepts confirm empty, ok, yes, or True")
    name = check_principal(principal_id, program, key_dir=key_dir)
    if not _has_terminal():
        # A name is only a claim. Without a terminal, any script or agent
        # could make it, so a headless admission goes through a registered key.
        raise AdmissionError(
            f"admit() by name ({name!r}) is for a person at an interactive "
            "terminal, and there is none. A script or agent admits with a "
            "registered key: admit_with_key, with an admitter key (a CI job) or "
            "as an agent under a signed admission policy"
        )
    distinct = bool(
        require_distinct_reviewer_and_admitter
        or candidate.get("require_distinct_reviewer_and_admitter")
    )
    bundle = program / "bundle"
    workflow = _load_workflow(bundle)
    digest = _digest(workflow)
    review = _evidence_at(program / REVIEW_FILE, digest)
    if review is None:
        raise AdmissionError(
            "no human review of this bundle; review it first (the digest changed "
            "or there was no review)"
        )
    if distinct and review["principal_id"].strip().lower() == name.lower():
        raise AdmissionError(
            f"{name!r} reviewed this draft; the policy requires a different "
            "principal to admit it"
        )
    canary = _evidence_gates(program, candidate, workflow, digest, key_dir)
    assert canary is not None
    return _write_manifest(
        program,
        candidate,
        digest,
        name=name,
        mode="person",
        review=review,
        canary=canary,
        key_dir=key_dir,
        extra={"require_distinct_reviewer_and_admitter": distinct},
    )


def _evidence_gates(
    program: Path,
    candidate: Mapping[str, Any],
    workflow: Any,
    digest: str,
    key_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """The checks every admission mode makes: parameters confirmed, a
    passing canary for this digest, independent read-back on every write
    step, no placeholder effect, and the Flow risk policy. Returns the canary.
    """

    from openadapt_flow.policy import evaluate_policy, load_policy

    task_params = _task_params(candidate)
    if task_params and _evidence_at(program / PARAMS_FILE, digest) is None:
        raise AdmissionError(f"confirm the parameters {task_params} first")
    canary = _evidence_at(program / CANARY_FILE, digest)
    if canary is None or not canary.get("passed"):
        raise AdmissionError("no passing canary on the target app for this bundle")
    if not _record_signed(canary, key_dir):
        raise AdmissionError(
            "the canary record is not signed by the local admission key; it was "
            "not written by a canary run"
        )
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
    return canary


def _write_manifest(
    program: Path,
    candidate: Mapping[str, Any],
    digest: str,
    *,
    name: str,
    mode: str,
    review: Optional[Mapping[str, Any]],
    canary: Mapping[str, Any],
    key_dir: Optional[Path],
    principal: Any = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    from openadapt_flow.execute.keys import (
        fingerprint_of,
        load_or_create_private_key,
        sign_bytes,
    )

    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    private = load_or_create_private_key(keys)
    recorder = _recorder(program, keys)
    review = review or {}
    body = {
        "schema": ADMISSION_SCHEMA,
        "admission_mode": mode,
        "state": BundleState.ADMITTED.value,
        "workflow_id": candidate.get("workflow_id"),
        "digest": digest,
        "task": candidate.get("task"),
        "params": _task_params(candidate),
        "policy": candidate.get("policy"),
        "admitted_by_principal_id": name,
        "admitted_at": _now(),
        "reviewed_by_principal_id": review.get("principal_id"),
        "reviewed_at": review.get("reviewed_at"),
        "reviewed_steps": review.get("steps"),
        "canary": {
            k: canary[k]
            for k in (
                "principal_id",
                "ran_at",
                "target_origin",
                "outcome",
                "model_calls",
                "environment",
            )
            if k in canary
        },
        # What the person approved, apart from the anchors: a repair that
        # re-anchors a step keeps it, a changed record check does not.
        "record_check_digest": _record_check_digest(program),
        "readback": canary["readback"],
        "recorded_by": recorder.get("recorded_by"),
        "agent_principal_id": recorder.get("agent_principal_id"),
        "dialect": recorder.get("dialect"),
        "model_key": candidate.get("model_key"),
        "signing_key": fingerprint_of(private.public_key()),
        **(extra or {}),
    }
    if mode == "agent_policy" and _is_recording_agent(name, recorder):
        # Only written when true, so manifests from before self-admission
        # existed verify unchanged.
        body["self_admitted"] = True
    if principal is not None:
        # The admitting key signs the body too, so the manifest names a key
        # the registry holds, not just a name.
        body["admitter_public_key"] = principal.public_key
        body["admitter_signature"] = principal.sign(body)
    manifest = {**body, "signature": sign_bytes(private, _canonical(body))}
    _write(program / "bundle" / ADMISSION_FILE, manifest)
    # The marker stays: the bundle remains a draft to anything that can't
    # verify this manifest.
    _event(program, "admitted", principal_id=name, digest=digest, mode=mode)
    return manifest


def _record_check_digest(program: Path) -> str:
    from openadapt_flow.jit.signature import record_check_digest

    return record_check_digest(_load_workflow(program / "bundle"))


def admit_with_key(
    program_dir: Path | str,
    *,
    principal: Any,
    key_dir: Optional[Path] = None,
    now: Optional[Any] = None,
) -> dict[str, Any]:
    """Admit without a person at a terminal. Returns ``{"outcome": ...}``.

    ``principal`` is a :class:`~openadapt_flow.jit.authority.Principal` from
    :func:`~openadapt_flow.jit.authority.principal_from_key`:

    * an **admitter key** a person registered (a CI job). It admits when the
      evidence every mode needs holds (:func:`_evidence_gates`); no step
      review is required, because the person who provisioned the key stands
      behind what it admits.
    * an **agent** under a signed admission policy. Flow re-verifies the
      policy's signature and expiry, that it names the agent, the draft's
      scope (app host, actions, effects, Flow policy), the read-back, and at
      least ``min_clean_canaries`` passing canaries for this digest with
      none failing. A draft outside the policy, or with a failed canary, is
      not admitted: it is written to the ``needs_person`` queue and the
      outcome is ``needs_person``.

    Either way the principal can't be the identity that made the recording.

    Raises:
        AdmissionError: an unregistered key, an invalid or expired policy,
            self-admission, too few canaries, or missing evidence.
    """

    from openadapt_flow.jit import authority

    program = Path(program_dir)
    candidate = _require_draft(program)
    _transition(program, BundleState.ADMITTED)
    if not isinstance(principal, authority.Principal):
        raise AdmissionError(
            "admit_with_key takes a registered Principal (principal_from_key); a "
            "name alone is a person, who admits with admit()"
        )
    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    name, _ = resolve_actor(principal, program, key_dir=keys)
    workflow = _load_workflow(program / "bundle")
    digest = _digest(workflow)
    if principal.kind == "admitter_key":
        canary = _evidence_gates(program, candidate, workflow, digest, keys)
        assert canary is not None
        manifest = _write_manifest(
            program,
            candidate,
            digest,
            name=name,
            mode="admitter_key",
            review=_evidence_at(program / REVIEW_FILE, digest),
            canary=canary,
            key_dir=keys,
            principal=principal,
        )
        return {"outcome": "admitted", "manifest": manifest}

    signed = principal.policy
    assert signed is not None
    registry = authority.PrincipalRegistry.load(keys)
    try:
        policy = authority.verify_policy(signed, registry, now=now)
    except authority.AuthorityError as exc:
        raise AdmissionError(str(exc)) from None
    canary = _evidence_at(program / CANARY_FILE, digest)
    # Only canary runs signed by the local admission key count; a line
    # anything else appended to canaries.jsonl is ignored.
    runs = [
        r
        for r in authority.canary_runs(program / CANARIES_FILE, digest)
        if _record_signed(r, keys)
    ]
    clean = [r for r in runs if r.get("passed")]
    failed = [r for r in runs if not r.get("passed")]
    reasons = authority.scope_problems(
        policy, workflow, candidate, (canary or {}).get("target_origin")
    )
    reasons += [
        f"canary at {r.get('ran_at')} by {r.get('principal_id')} ended "
        f"{r.get('outcome')}"
        for r in failed
    ]
    reasons += [
        f"{row}" for row in _readback_problems((canary or {}).get("readback") or [])
    ]
    other_hosts = sorted(
        {
            str(r.get("target_origin"))
            for r in clean
            if r.get("target_origin") != (canary or {}).get("target_origin")
        }
    )
    if other_hosts:
        reasons.append(f"canaries ran on more than one app: {other_hosts}")
    if reasons:
        record = {
            "at": _now(),
            "digest": digest,
            "agent_principal_id": name,
            "policy_id": policy.policy_id,
            "policy_digest": policy.digest(),
            "reasons": reasons,
        }
        _write(program / NEEDS_PERSON_FILE, record)
        _event(program, "needs_person", principal_id=name, digest=digest)
        return {"outcome": "needs_person", "reasons": reasons}
    if len(clean) < policy.min_clean_canaries:
        raise AdmissionError(
            f"{len(clean)} clean canaries for this bundle; the policy needs "
            f"{policy.min_clean_canaries}"
        )
    canary = _evidence_gates(program, candidate, workflow, digest, keys)
    assert canary is not None
    manifest = _write_manifest(
        program,
        candidate,
        digest,
        name=name,
        mode="agent_policy",
        review=_evidence_at(program / REVIEW_FILE, digest),
        canary=canary,
        key_dir=keys,
        principal=principal,
        extra={
            "policy_id": policy.policy_id,
            "policy_digest": policy.digest(),
            "policy_signer": policy.signer,
            "policy_expires_at": policy.expires_at,
            "signed_policy": signed.model_dump(mode="json", by_alias=True),
            "clean_canaries": len(clean),
        },
    )
    (program / NEEDS_PERSON_FILE).unlink(missing_ok=True)
    return {"outcome": "admitted", "manifest": manifest}


def needs_person_queue(cache_dir: Path | str) -> list[dict[str, Any]]:
    """Drafts an agent's policy did not cover, waiting for a person."""

    queue = []
    for path in sorted(Path(cache_dir).glob(f"**/{NEEDS_PERSON_FILE}")):
        program = path.parent
        if (program / "bundle" / ADMISSION_FILE).is_file():
            continue
        if (program / "rejected.json").is_file():
            continue
        queue.append({"program": str(program), **(_read(path) or {})})
    return queue


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
    _verify_mode(body, keys)
    try:
        digest = _digest(_load_workflow(bundle))
    except Exception as exc:  # noqa: BLE001 - integrity failure is a refusal
        raise AdmissionError(f"the bundle failed its integrity check: {exc}") from exc
    if digest != body.get("digest"):
        raise AdmissionError("the bundle is not the one that was admitted")
    return manifest


def _verify_mode(body: Mapping[str, Any], keys: Path) -> None:
    """The checks a key or policy admission adds to the local signature."""

    from openadapt_flow.jit import authority

    mode = body.get("admission_mode", "person")
    if mode not in ADMISSION_MODES:
        raise AdmissionError(f"unknown admission mode {mode!r}")
    if mode in REPAIR_MODES:
        _verify_repair(body, keys)
        return
    name = str(body.get("admitted_by_principal_id") or "")
    if mode != "agent_policy":
        _self_check(name, body, agent=False)
        if body.get("self_admitted"):
            raise AdmissionError("only an agent under a policy can be self-admitted")
    if mode == "person":
        if is_not_a_person_name(name):
            raise AdmissionError(f"{name!r} is not a person; a model never authorizes")
        return
    inner = {k: v for k, v in body.items() if k != "admitter_signature"}
    try:
        role, _ = authority.verify_admitter(
            inner, str(body.get("admitter_signature") or ""), keys
        )
    except authority.AuthorityError as exc:
        raise AdmissionError(str(exc)) from None
    if mode == "admitter_key":
        if role != "admitter_key":
            raise AdmissionError("the admitter is not a registered admitter key")
        return
    if role != "agent":
        raise AdmissionError("the admitter is not a registered agent")
    try:
        signed = authority.SignedAdmissionPolicy.model_validate(body["signed_policy"])
        # Expiry was checked when it admitted; an admitted bundle keeps running
        # after its policy expires. The signature must still verify.
        policy = authority.verify_policy(
            signed, authority.PrincipalRegistry.load(keys), check_expiry=False
        )
    except (KeyError, ValueError, authority.AuthorityError) as exc:
        raise AdmissionError(f"the admission's policy does not verify: {exc}") from None
    if policy.digest() != body.get("policy_digest") or name not in (
        policy.agent_principals
    ):
        raise AdmissionError("the admission does not match the policy it names")
    # Checked only now: whether the recording agent may admit its own draft
    # comes from the signed policy, which has just verified.
    _self_check(name, body, agent=True, allow_self=policy.allow_self_admission)
    if bool(body.get("self_admitted", False)) != _is_recording_agent(name, body):
        raise AdmissionError(
            "the manifest's self_admitted does not match who recorded the draft "
            "and who admitted it"
        )


# -- repairs -------------------------------------------------------------------

#: Fields a repaired copy's admission must carry over from the prior one.
_REPAIR_KEPT = ("workflow_id", "task", "params", "policy", "canary", "readback")


def admit_repair(
    prior_bundle: Path | str,
    repaired_bundle: Path | str,
    *,
    candidate_id: str,
    approved_by: Optional[str] = None,
    route: Optional[str] = None,
    probe_only: bool = False,
    key_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Carry an admitted program's admission to a repaired copy of it.

    A repair (``openadapt_flow.repair``) re-anchors steps or adds a bridge;
    it never changes the record check. So the copy keeps the prior admission
    when its record check (:func:`~openadapt_flow.jit.signature.record_check_digest`)
    and parameters are the prior program's, and it is admitted for the same
    task, policy, app and read-back. The new manifest embeds the prior one,
    which must still verify.

    ``probe_only`` writes a ``repair_probe`` admission: the copy runs only
    behind a gate that stops before the first save (see
    :func:`ensure_runnable`). Otherwise ``approved_by`` is the person who
    approved the repair, or ``standard-change:<policy digest>`` for a fix a
    signed repair policy allowed.

    Raises:
        AdmissionError: the prior program is not admitted, the copy changes
            its record check or parameters, or no approver is named.
    """

    from openadapt_flow.execute.keys import (
        fingerprint_of,
        load_or_create_private_key,
        sign_bytes,
    )
    from openadapt_flow.jit.signature import record_check_digest

    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    prior = verify_admission(prior_bundle, key_dir=keys)
    if prior.get("admission_mode") == "repair_probe":
        raise AdmissionError("a repair probe's admission can't carry to another copy")
    prior_workflow = _load_workflow(Path(prior_bundle))
    repaired = _load_workflow(Path(repaired_bundle))
    if sorted(repaired.params) != sorted(prior.get("params") or []):
        raise AdmissionError("the repair changes the program's parameters")
    check = record_check_digest(repaired)
    if check != record_check_digest(prior_workflow) or (
        prior.get("record_check_digest") not in (None, check)
    ):
        raise AdmissionError(
            "the repair changes the program's record check; it needs a new admission"
        )
    if probe_only:
        name = "self-repair:probe"
    else:
        name = (approved_by or "").strip()
        if not name:
            raise AdmissionError("a repair's admission names who approved it")
        if not name.startswith(STANDARD_CHANGE_APPROVER) and is_not_a_person_name(name):
            raise AdmissionError(f"{name!r} is not a person; a model never approves")
    private = load_or_create_private_key(keys)
    body: dict[str, Any] = {
        "schema": ADMISSION_SCHEMA,
        "admission_mode": "repair_probe" if probe_only else "repair",
        "state": BundleState.ADMITTED.value,
        **{k: prior.get(k) for k in _REPAIR_KEPT},
        **{k: prior.get(k) for k in RECORDER_FIELDS},
        "digest": _digest(repaired),
        "record_check_digest": check,
        "admitted_by_principal_id": name,
        "admitted_at": _now(),
        "repair_of": {
            "candidate_id": candidate_id,
            "route": route,
            "prior_digest": prior.get("digest"),
            "prior_manifest": dict(prior),
        },
        "signing_key": fingerprint_of(private.public_key()),
    }
    manifest = {**body, "signature": sign_bytes(private, _canonical(body))}
    _write(Path(repaired_bundle) / ADMISSION_FILE, manifest)
    return manifest


def _verify_repair(body: Mapping[str, Any], keys: Path) -> None:
    """A repaired copy's admission: the prior one verifies, and nothing that
    was admitted changed except the anchors and steps the repair touched."""

    repair = body.get("repair_of")
    if not isinstance(repair, Mapping):
        raise AdmissionError("a repair admission names no prior admission")
    prior = repair.get("prior_manifest")
    if not isinstance(prior, Mapping) or not _record_signed(prior, keys):
        raise AdmissionError("the prior admission's signature does not verify")
    prior_body = {k: v for k, v in prior.items() if k != "signature"}
    if prior_body.get("state") != BundleState.ADMITTED.value:
        raise AdmissionError("the prior manifest does not admit its bundle")
    if prior_body.get("admission_mode") == "repair_probe":
        raise AdmissionError("a repair can't follow a repair probe")
    _verify_mode(prior_body, keys)
    if repair.get("prior_digest") != prior_body.get("digest"):
        raise AdmissionError("the repair names another prior bundle")
    for key in (*_REPAIR_KEPT, *RECORDER_FIELDS):
        if body.get(key) != prior_body.get(key):
            raise AdmissionError(f"the repair changed the admitted {key}")
    pinned = prior_body.get("record_check_digest")
    if not body.get("record_check_digest") or (
        pinned is not None and pinned != body.get("record_check_digest")
    ):
        raise AdmissionError("the repair changed the admitted record check")
    if body.get("admission_mode") == "repair":
        name = str(body.get("admitted_by_principal_id") or "")
        if not name.startswith(STANDARD_CHANGE_APPROVER):
            if is_not_a_person_name(name):
                raise AdmissionError(
                    f"{name!r} is not a person; a model never approves"
                )
            _self_check(name, body, agent=False)


def reject(
    program_dir: Path | str,
    *,
    principal_id: Any,
    reason: str = "",
    key_dir: Optional[Path] = None,
) -> Path:
    """Move a DRAFT to REJECTED. The draft is kept, renamed, for the record."""

    program = Path(program_dir)
    candidate = _require_draft(program)
    _transition(program, BundleState.REJECTED)

    def recorder() -> Mapping[str, Any]:
        try:
            return _recorder(program, key_dir)
        except AdmissionError:
            # Rejecting only sets the draft aside, so a draft whose recorder
            # no longer verifies can still be rejected. Check the unsigned
            # fields instead.
            return candidate

    name, _ = _actor(principal_id, recorder, key_dir)
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
