"""Reusable semantic subroutines: typed, content-addressed, admitted per use.

Compiled programs repeat the same low-level steps across workflows (search
for a record, pick a date). A **subroutine** is one such sequence lifted out
of the workflows that share it, with a typed signature, so it is verified
once and reused. This module is the first slice of that idea. It makes the
unit and its admission real; it does not change the runtime.

The unit
    :class:`Subroutine` is a typed signature (:class:`SubroutineSignature`)
    plus a body (:class:`~openadapt_flow.ir.ProgramGraph`). The signature
    names its typed inputs, the surface kind it runs on, and the boundary
    checks that must hold when it is entered and when it returns. Its identity
    is a SHA-256 over the canonical JSON of signature and body
    (:attr:`Subroutine.sha256`). Any change to a step, a check, an input or the
    surface is a new subroutine with a new digest.

What a body may hold (checked by :func:`body_problems`)
    Only reversible, single-surface action steps, branches and terminals that
    read nothing but declared inputs. Writes (effects, API bindings, submit
    bindings, dialog responses, irreversible or unreviewed risk), reads,
    secrets, surface switches, nested calls, loops, business decisions and a
    plaintext identity band are refused. The boundary is narrow on purpose.
    Widening it is later work, one refusal at a time.

The library
    :class:`SubroutineLibrary` stores subroutines by digest, keeps each name's
    versions in order, and records which digest was qualified and under which
    policy. Workflows that use a subroutine are registered with the library.
    Publishing a new version of a name returns a
    :class:`RequalificationPlan` that lists every registered workflow still
    pinned to an older digest. Those workflows are refused until they are
    re-embedded and re-qualified.

Using one in a workflow
    :func:`embed` copies a subroutine into ``Workflow.subflows`` under the
    key ``subroutine/<name>/<sha256>`` (:func:`subflow_key`) and wraps it:

    * an entry BRANCH that continues only when every entry check holds and
      otherwise ends the whole run with a ``halt`` terminal;
    * when exit checks are declared, each ``success`` terminal of the body
      becomes a BRANCH that returns only when every exit check holds and
      otherwise halts.

    The existing interpreter runs the wrapped graph unchanged, so the boundary
    checks are enforced at run time by today's guard semantics (an unmet guard
    takes the halt edge; nothing guesses).

Admission
    :func:`admit_workflow` finds every subroutine a workflow embeds (from the
    subflow keys, never from application names) and admits each use only
    when the digest is known to the library, is that name's current version,
    is qualified, is registered for this workflow, the embedded graph is
    byte-identical to what :func:`embed` produces, the workflow runs on the
    declared surface kind, and the workflow supplies every required input
    with the declared type. Any other case is a refusal with a stable code.

Detection
    :func:`mine_recurring_sequences` proposes candidate subroutines from the
    programs alone: it compares steps by shape (action kind, whether the step
    is parameter-bound, key chord, accessibility role) and never by labels,
    names, selectors, URLs or application names. A candidate is a proposal;
    it is never admitted or runnable.

Refusal messages name subroutines, inputs and codes only. They never echo
anchor text, recorded values or URL patterns. Deterministic and model-free;
no I/O beyond the JSON a caller asks for.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ir import (
    ActionKind,
    ExecutionTargetKind,
    ParamKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)

SUBROUTINE_SCHEMA: Literal["openadapt.subroutine/v1"] = "openadapt.subroutine/v1"
LIBRARY_SCHEMA: Literal["openadapt.subroutine-library/v1"] = (
    "openadapt.subroutine-library/v1"
)
CANDIDATE_SCHEMA: Literal["openadapt.subroutine-candidate/v1"] = (
    "openadapt.subroutine-candidate/v1"
)

#: Prefix of the ``Workflow.subflows`` key an embedded subroutine lives under.
SUBFLOW_PREFIX = "subroutine/"
#: State ids the wrapper adds. A body may not use this prefix.
RESERVED_STATE_PREFIX = "__sr_"
_ENTRY = "__sr_entry__"
_ENTRY_REFUSED = "__sr_entry_refused__"
_RETURN = "__sr_return__"
_EXIT_REFUSED = "__sr_exit_refused__"

_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_INPUT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

#: Action kinds a subroutine body may perform in this slice.
_ALLOWED_ACTIONS = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.DRAG,
        ActionKind.TYPE,
        ActionKind.SELECT_OPTION,
        ActionKind.KEY,
        ActionKind.HOTKEY,
        ActionKind.WAIT,
        ActionKind.SCROLL,
    }
)
_ALLOWED_STATES = frozenset({StateKind.ACTION, StateKind.BRANCH, StateKind.TERMINAL})


class SubroutineError(ValueError):
    """A subroutine, library document or library call is not usable."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- the unit -----------------------------------------------------------------


class SubroutineInput(BaseModel):
    """One typed input. Carries no example value, so no recorded data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_INPUT_PATTERN)
    type: ParamKind = ParamKind.STRING
    required: bool = True
    choices: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _choices_only_for_enum(self) -> "SubroutineInput":
        if self.choices and self.type is not ParamKind.ENUM:
            raise ValueError("only an enum input has choices")
        if self.type is ParamKind.ENUM and not self.choices:
            raise ValueError("an enum input needs its choices")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("enum choices must be unique")
        return self


def _is_affirmative(predicate: Predicate) -> bool:
    """True for a check that reads the live surface and can only pass on it.

    The same rule business decisions use: a TEXT_PRESENT with text or an
    ANCHOR_RESOLVES with an anchor, alone or inside an AND. A parameter
    comparison, an absence check, OR and NOT can hold without the expected
    screen, so they do not count.
    """

    if predicate.kind is PredicateKind.TEXT_PRESENT:
        return bool(predicate.text)
    if predicate.kind is PredicateKind.ANCHOR_RESOLVES:
        return predicate.anchor is not None
    if predicate.kind is PredicateKind.AND:
        return any(_is_affirmative(op) for op in predicate.operands)
    return False


class SubroutineSignature(BaseModel):
    """What a caller and the runtime can rely on at the subroutine boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.subroutine/v1"] = SUBROUTINE_SCHEMA
    name: str = Field(pattern=_NAME_PATTERN)
    intent: str = Field(min_length=1, max_length=240)
    inputs: tuple[SubroutineInput, ...] = ()
    surface_kind: ExecutionTargetKind
    entry_checks: tuple[Predicate, ...] = Field(
        min_length=1,
        description=(
            "Checks on the live surface before the first step. All must hold, "
            "and at least one must be affirmative (text present or an anchor "
            "that resolves)."
        ),
    )
    exit_checks: tuple[Predicate, ...] = Field(
        default=(),
        description="Checks that must all hold before the subroutine returns.",
    )

    @model_validator(mode="after")
    def _boundary(self) -> "SubroutineSignature":
        names = [item.name for item in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("subroutine input names must be unique")
        if not any(_is_affirmative(check) for check in self.entry_checks):
            raise ValueError(
                "a subroutine needs an affirmative entry check (text present or "
                "an anchor that resolves); parameters, absence checks, OR and "
                "NOT cannot show it is on the right screen"
            )
        if self.exit_checks and not any(
            _is_affirmative(check) for check in self.exit_checks
        ):
            raise ValueError("declared exit checks need one affirmative check")
        return self

    def input(self, name: str) -> Optional[SubroutineInput]:
        for item in self.inputs:
            if item.name == name:
                return item
        return None


class Subroutine(BaseModel):
    """A typed, content-addressed reusable unit: signature plus body."""

    model_config = ConfigDict(extra="forbid")

    signature: SubroutineSignature
    body: ProgramGraph

    @model_validator(mode="after")
    def _well_formed(self) -> "Subroutine":
        problems = body_problems(self)
        if problems:
            raise ValueError(
                f"subroutine {self.signature.name!r} is not admissible: "
                + "; ".join(problems)
            )
        return self

    @property
    def name(self) -> str:
        return self.signature.name

    @property
    def sha256(self) -> str:
        """Content identity over signature and body (canonical JSON)."""

        return _sha256(
            {
                "signature": self.signature.model_dump(mode="json"),
                "body": self.body.model_dump(mode="json"),
            }
        )


# -- body checks ----------------------------------------------------------------


def _param_refs(value: Any, problems: list[str], where: str) -> set[str]:
    """Every parameter name a JSON value refers to, found generically.

    A key named ``param`` or ending in ``_param`` holds one name; a key named
    ``params`` or ending in ``_params`` holds a list of names. Anything else
    under such a key is refused, so a reference this walk does not
    understand can never slip past the closure check.
    """

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            single = key == "param" or key.endswith("_param")
            many = key == "params" or key.endswith("_params")
            if (single or many) and item is not None:
                if single and isinstance(item, str):
                    found.add(item)
                elif (
                    many
                    and isinstance(item, (list, tuple))
                    and all(isinstance(name, str) for name in item)
                ):
                    found.update(item)
                else:
                    problems.append(f"{where}: unsupported parameter reference {key!r}")
                continue
            found |= _param_refs(item, problems, where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _param_refs(item, problems, where)
    return found


def _step_problems(step: Step, where: str) -> list[str]:
    problems: list[str] = []
    if step.action not in _ALLOWED_ACTIONS:
        problems.append(f"{where}: action {step.action.value!r} is not allowed")
    if step.effects:
        problems.append(f"{where}: declares a system-of-record effect")
    if step.api_binding is not None:
        problems.append(f"{where}: carries an API binding")
    if step.submit_binding is not None:
        problems.append(f"{where}: carries a submit binding")
    if step.dialog is not None:
        problems.append(f"{where}: answers a dialog")
    if step.risk != "reversible" or step.risk_review_required:
        problems.append(f"{where}: is not a reviewed reversible step")
    if step.secret:
        problems.append(f"{where}: types a secret")
    if step.read is not None or step.read_gate is not None:
        problems.append(f"{where}: reads a record field")
    if (
        step.surface is not None
        or step.opens
        or step.surface_origin is not None
        or step.closes_surface
    ):
        problems.append(f"{where}: changes or names a surface")
    return problems


#: Anchor fields that hold a plaintext identity band (screen text near the
#: target, or a record-row identity). A reusable unit may not carry either.
_IDENTITY_BAND_FIELDS = ("context_text", "structured_identity")


def _identity_band_paths(value: Any, path: str = "") -> list[str]:
    """JSON paths of every non-empty identity band anywhere in ``value``.

    Walked generically, so an anchor inside a step, a step guard, a wait,
    a transition guard, a boundary check or any nested predicate operand is
    found the same way. Paths name fields only, never the band's text.
    """

    found: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            item = value[key]
            here = f"{path}.{key}" if path else key
            if key in _IDENTITY_BAND_FIELDS and item not in (None, ""):
                found.append(here)
                continue
            found.extend(_identity_band_paths(item, here))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_identity_band_paths(item, f"{path}[{index}]"))
    return found


def body_problems(subroutine: Subroutine) -> list[str]:
    """Why a subroutine cannot be admitted as a unit (empty when it can).

    Messages name state ids, step ids and input names only.
    """

    sig = subroutine.signature
    body = subroutine.body
    problems: list[str] = []
    states = body.states
    if body.entry not in states:
        problems.append("the body's entry state is not defined")
    refs: set[str] = set()
    for label, checks in (("entry", sig.entry_checks), ("exit", sig.exit_checks)):
        for index, check in enumerate(checks):
            dumped = check.model_dump(mode="json")
            refs |= _param_refs(dumped, problems, "boundary check")
            for band in _identity_band_paths(dumped):
                problems.append(
                    f"{label} check {index}: keeps a plaintext identity band ({band})"
                )
    has_success = False
    for state_id, state in states.items():
        where = f"state {state_id!r}"
        if state.id != state_id:
            problems.append(f"{where}: key and id differ")
        if state_id.startswith(RESERVED_STATE_PREFIX):
            problems.append(f"{where}: uses the reserved prefix")
        if state.kind not in _ALLOWED_STATES:
            problems.append(f"{where}: kind {state.kind.value!r} is not allowed")
        if state.kind is not StateKind.TERMINAL and not state.transitions:
            # Falling off the graph returns to the caller without passing the
            # exit checks, so every path must end at a terminal.
            problems.append(f"{where}: has no outgoing transition")
        for transition in state.transitions:
            if transition.target not in states:
                problems.append(f"{where}: transition to an undefined state")
        if state.on_exception is not None and state.on_exception not in states:
            problems.append(f"{where}: exception handler is not defined")
        if state.kind is StateKind.ACTION:
            if state.step is None:
                problems.append(f"{where}: action state has no step")
            else:
                problems.extend(_step_problems(state.step, where))
        if state.kind is StateKind.TERMINAL and state.outcome in (None, "success"):
            has_success = True
        dumped_state = state.model_dump(mode="json")
        refs |= _param_refs(dumped_state, problems, where)
        for band in _identity_band_paths(dumped_state):
            problems.append(f"{where}: keeps a plaintext identity band ({band})")
    if not has_success:
        problems.append("the body has no success terminal to return through")
    declared = {item.name for item in sig.inputs}
    undeclared = sorted(refs - declared)
    if undeclared:
        problems.append(f"the body reads undeclared inputs {undeclared}")
    unused = sorted(declared - refs)
    if unused:
        problems.append(f"declared inputs are never read {unused}")
    return problems


# -- embedding --------------------------------------------------------------------


def subflow_key(subroutine: Subroutine) -> str:
    """The ``Workflow.subflows`` key that pins a subroutine by digest."""

    return f"{SUBFLOW_PREFIX}{subroutine.name}/{subroutine.sha256}"


def parse_subflow_key(key: str) -> Optional[tuple[str, str]]:
    """``(name, sha256)`` for a subroutine subflow key, else None.

    A key that starts with the prefix but is malformed raises: a workflow
    cannot hide a subroutine use behind a near-miss key.
    """

    if not key.startswith(SUBFLOW_PREFIX):
        return None
    parts = key[len(SUBFLOW_PREFIX) :].split("/")
    if (
        len(parts) != 2
        or not re.fullmatch(_NAME_PATTERN, parts[0])
        or not re.fullmatch(_SHA256_PATTERN, parts[1])
    ):
        raise SubroutineError("malformed subroutine subflow key")
    return parts[0], parts[1]


def _all_of(checks: tuple[Predicate, ...], intent: str) -> Predicate:
    if len(checks) == 1:
        return checks[0].model_copy(deep=True)
    return Predicate(
        kind=PredicateKind.AND,
        operands=[check.model_copy(deep=True) for check in checks],
        intent=intent,
    )


def wrapped_body(subroutine: Subroutine) -> ProgramGraph:
    """The body with its boundary checks as live guards around it."""

    sig = subroutine.signature
    states: dict[str, State] = {}
    for state_id, state in subroutine.body.states.items():
        copy = state.model_copy(deep=True)
        if (
            sig.exit_checks
            and copy.kind is StateKind.TERMINAL
            and copy.outcome in (None, "success")
        ):
            copy = State(
                id=state_id,
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=_all_of(sig.exit_checks, f"{sig.name} exit checks"),
                        target=_RETURN,
                        label="exit checks hold",
                    ),
                    Transition(target=_EXIT_REFUSED, label="exit checks failed"),
                ],
            )
        states[state_id] = copy
    states[_ENTRY] = State(
        id=_ENTRY,
        kind=StateKind.BRANCH,
        transitions=[
            Transition(
                guard=_all_of(sig.entry_checks, f"{sig.name} entry checks"),
                target=subroutine.body.entry,
                label="entry checks hold",
            ),
            Transition(target=_ENTRY_REFUSED, label="entry checks failed"),
        ],
    )
    states[_ENTRY_REFUSED] = State(
        id=_ENTRY_REFUSED,
        kind=StateKind.TERMINAL,
        outcome="halt",
        reason=f"subroutine {sig.name} entry checks did not hold",
    )
    if sig.exit_checks:
        states[_RETURN] = State(id=_RETURN, kind=StateKind.TERMINAL, outcome="success")
        states[_EXIT_REFUSED] = State(
            id=_EXIT_REFUSED,
            kind=StateKind.TERMINAL,
            outcome="halt",
            reason=f"subroutine {sig.name} exit checks did not hold",
        )
    return ProgramGraph(entry=_ENTRY, states=states)


def embed(workflow: Workflow, subroutine: Subroutine) -> tuple[Workflow, str]:
    """Return a copy of ``workflow`` with the subroutine embedded, and its key.

    The caller adds the SUBFLOW_CALL (or loop body) that uses the key. An
    existing subflow under the same key is replaced only by identical content.
    """

    key = subflow_key(subroutine)
    graph = wrapped_body(subroutine)
    existing = workflow.subflows.get(key)
    if existing is not None and _sha256(existing.model_dump(mode="json")) != _sha256(
        graph.model_dump(mode="json")
    ):
        raise SubroutineError(
            f"workflow already embeds a different body under subroutine "
            f"{subroutine.name!r}"
        )
    copy = workflow.model_copy(deep=True)
    copy.subflows = {**copy.subflows, key: graph}
    return copy, key


# -- library ----------------------------------------------------------------------


class SubroutineQualification(BaseModel):
    """Evidence that one exact digest passed qualification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_name: str = Field(min_length=1, max_length=128)
    evidence_sha256: str = Field(
        pattern=_SHA256_PATTERN,
        description="Digest of the retained qualification evidence.",
    )
    qualified_at: str = Field(default_factory=_now)


class SubroutineUse(BaseModel):
    """One workflow's pin on one subroutine digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=256)
    name: str = Field(pattern=_NAME_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class RequalificationPlan(BaseModel):
    """What publishing a new version of a subroutine puts back in review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    sha256: str
    version: int
    #: Registered uses pinned to an older digest of the same name. Each one is
    #: refused until it is re-embedded and its workflow is re-qualified.
    stale_uses: tuple[SubroutineUse, ...] = ()

    @property
    def workflows(self) -> tuple[str, ...]:
        return tuple(sorted({use.workflow_id for use in self.stale_uses}))


class SubroutineLibrary:
    """Content-addressed subroutines, their versions, qualifications and users.

    In memory, with :meth:`to_json` / :meth:`from_json`. Loading re-derives
    every digest from content, so an edited document is refused rather than
    trusted.
    """

    def __init__(self) -> None:
        self._by_digest: dict[str, Subroutine] = {}
        self._versions: dict[str, list[str]] = {}
        self._qualified: dict[str, SubroutineQualification] = {}
        self._uses: set[SubroutineUse] = set()

    # -- read --------------------------------------------------------------

    def get(self, sha256: str) -> Optional[Subroutine]:
        return self._by_digest.get(sha256)

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(self._versions.get(name, ()))

    def current(self, name: str) -> Optional[Subroutine]:
        versions = self._versions.get(name)
        return self._by_digest[versions[-1]] if versions else None

    def qualification(self, sha256: str) -> Optional[SubroutineQualification]:
        return self._qualified.get(sha256)

    def uses(self, name: Optional[str] = None) -> tuple[SubroutineUse, ...]:
        selected = (u for u in self._uses if name is None or u.name == name)
        return tuple(sorted(selected, key=lambda u: (u.name, u.workflow_id, u.sha256)))

    def dependents(self, name: str) -> tuple[str, ...]:
        """Workflow ids registered as using any version of ``name``."""

        return tuple(sorted({use.workflow_id for use in self.uses(name)}))

    # -- write -------------------------------------------------------------

    def publish(self, subroutine: Subroutine) -> RequalificationPlan:
        """Add a subroutine as its name's newest version (idempotent)."""

        digest = subroutine.sha256
        name = subroutine.name
        versions = self._versions.setdefault(name, [])
        if digest in self._by_digest:
            if versions[-1] != digest:
                raise SubroutineError(
                    f"subroutine {name!r} cannot go back to an older version; "
                    "publish a new change instead"
                )
        else:
            self._by_digest[digest] = Subroutine.model_validate(
                subroutine.model_dump(mode="json")
            )
            versions.append(digest)
        stale = tuple(use for use in self.uses(name) if use.sha256 != digest)
        return RequalificationPlan(
            name=name, sha256=digest, version=len(versions), stale_uses=stale
        )

    def qualify(
        self, sha256: str, *, policy_name: str, evidence_sha256: str
    ) -> SubroutineQualification:
        """Record that this exact digest passed qualification."""

        if sha256 not in self._by_digest:
            raise SubroutineError("cannot qualify a subroutine the library lacks")
        record = SubroutineQualification(
            sha256=sha256, policy_name=policy_name, evidence_sha256=evidence_sha256
        )
        self._qualified[sha256] = record
        return record

    def register(
        self, workflow_id: str, workflow: Workflow
    ) -> tuple[SubroutineUse, ...]:
        """Record every subroutine ``workflow`` embeds, replacing older pins.

        Registration is what lets a later version find this workflow. It does
        not admit anything.
        """

        found = tuple(
            SubroutineUse(workflow_id=workflow_id, name=name, sha256=digest)
            for name, digest in embedded_uses(workflow)
        )
        self._uses = {u for u in self._uses if u.workflow_id != workflow_id}
        self._uses.update(found)
        return found

    # -- persistence -------------------------------------------------------

    def to_json(self) -> str:
        payload = {
            "schema": LIBRARY_SCHEMA,
            "subroutines": {
                digest: sub.model_dump(mode="json")
                for digest, sub in sorted(self._by_digest.items())
            },
            "versions": {name: list(v) for name, v in sorted(self._versions.items())},
            "qualifications": [
                q.model_dump(mode="json") for _, q in sorted(self._qualified.items())
            ],
            "uses": [u.model_dump(mode="json") for u in self.uses()],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SubroutineLibrary":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SubroutineError("subroutine library is not JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != LIBRARY_SCHEMA:
            raise SubroutineError(f"not a subroutine library ({LIBRARY_SCHEMA})")
        library = cls()
        try:
            for digest, data in raw.get("subroutines", {}).items():
                sub = Subroutine.model_validate(data)
                if sub.sha256 != digest:
                    raise SubroutineError(
                        f"subroutine {sub.name!r} content does not match its digest"
                    )
                library._by_digest[digest] = sub
            for name, versions in raw.get("versions", {}).items():
                if len(set(versions)) != len(versions):
                    raise SubroutineError(f"subroutine {name!r} repeats a version")
                for digest in versions:
                    held = library._by_digest.get(digest)
                    if held is None or held.name != name:
                        raise SubroutineError(
                            f"subroutine {name!r} lists a version it does not hold"
                        )
                library._versions[name] = list(versions)
            listed = {d for v in library._versions.values() for d in v}
            if listed != set(library._by_digest):
                raise SubroutineError("a stored subroutine is not in any version list")
            for data in raw.get("qualifications", []):
                record = SubroutineQualification.model_validate(data)
                if record.sha256 not in library._by_digest:
                    raise SubroutineError("a qualification names an unknown digest")
                library._qualified[record.sha256] = record
            for data in raw.get("uses", []):
                library._uses.add(SubroutineUse.model_validate(data))
        except SubroutineError:
            raise
        except ValueError as exc:
            raise SubroutineError(
                f"subroutine library is malformed ({type(exc).__name__})"
            ) from None
        return library


# -- admission --------------------------------------------------------------------

RefusalCode = Literal[
    "malformed_key",
    "unknown_subroutine",
    "name_mismatch",
    "superseded",
    "not_qualified",
    "unregistered_use",
    "embedded_drift",
    "surface_undeclared",
    "multi_surface",
    "surface_kind_mismatch",
    "missing_input",
    "input_type_mismatch",
    "input_choices_mismatch",
    "secret_input",
]


class SubroutineRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: RefusalCode
    subflow: str
    message: str


class SubroutineAdmission(BaseModel):
    """Per-workflow verdict. ``admitted`` is True only with no refusals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    uses: tuple[SubroutineUse, ...] = ()
    refusals: tuple[SubroutineRefusal, ...] = ()

    @property
    def admitted(self) -> bool:
        return not self.refusals


def embedded_uses(workflow: Workflow) -> list[tuple[str, str]]:
    """``(name, sha256)`` for every subroutine subflow in ``workflow``."""

    found: list[tuple[str, str]] = []
    for key in sorted(workflow.subflows):
        parsed = parse_subflow_key(key)
        if parsed is not None:
            found.append(parsed)
    return found


def _caller_inputs_problems(
    workflow: Workflow, sig: SubroutineSignature
) -> list[tuple[RefusalCode, str]]:
    problems: list[tuple[RefusalCode, str]] = []
    secret = set(workflow.secret_params or [])
    # Only parameters the caller declares count. A name the embedded body
    # itself reads is not a value the caller supplies.
    supplied = set(workflow.param_specs) | set(workflow.params)
    for item in sig.inputs:
        if item.name in secret:
            problems.append(
                ("secret_input", f"input {item.name!r} is a secret in the caller")
            )
            continue
        spec = workflow.param_specs.get(item.name)
        if spec is None:
            if item.name not in supplied:
                if item.required:
                    problems.append(
                        ("missing_input", f"caller does not supply {item.name!r}")
                    )
                continue
            if item.type is not ParamKind.STRING:
                problems.append(
                    (
                        "input_type_mismatch",
                        f"input {item.name!r} needs type {item.type.value!r}; "
                        "the caller declares no type",
                    )
                )
            continue
        if spec.type is not item.type:
            problems.append(
                (
                    "input_type_mismatch",
                    f"input {item.name!r} needs type {item.type.value!r}, "
                    f"caller has {spec.type.value!r}",
                )
            )
            continue
        if item.type is ParamKind.ENUM and not set(spec.choices) <= set(item.choices):
            problems.append(
                (
                    "input_choices_mismatch",
                    f"caller allows choices for {item.name!r} that the "
                    "subroutine does not",
                )
            )
        if item.required and not spec.required and spec.example is None:
            problems.append(
                (
                    "missing_input",
                    f"input {item.name!r} is required but optional in the caller "
                    "with no default",
                )
            )
    return problems


def admit_workflow(
    workflow: Workflow,
    library: SubroutineLibrary,
    *,
    workflow_id: Optional[str] = None,
) -> SubroutineAdmission:
    """Admit every subroutine ``workflow`` embeds, or refuse with reasons.

    A workflow that embeds none is admitted with no uses: this gate adds
    nothing to a workflow that does not use subroutines.
    """

    wid = workflow_id or workflow.name
    refusals: list[SubroutineRefusal] = []
    uses: list[SubroutineUse] = []

    def refuse(code: RefusalCode, key: str, message: str) -> None:
        refusals.append(SubroutineRefusal(code=code, subflow=key, message=message))

    registered = set(library.uses())
    for key in sorted(workflow.subflows):
        try:
            parsed = parse_subflow_key(key)
        except SubroutineError:
            refuse("malformed_key", key[:96], "malformed subroutine subflow key")
            continue
        if parsed is None:
            continue
        name, digest = parsed
        use = SubroutineUse(workflow_id=wid, name=name, sha256=digest)
        uses.append(use)
        sub = library.get(digest)
        if sub is None:
            refuse("unknown_subroutine", key, f"library has no {name!r} at this digest")
            continue
        if sub.name != name:
            refuse("name_mismatch", key, "digest belongs to a different subroutine")
            continue
        current = library.current(name)
        if current is None or current.sha256 != digest:
            refuse(
                "superseded",
                key,
                f"{name!r} has a newer version; re-embed it and re-qualify "
                "this workflow",
            )
        if library.qualification(digest) is None:
            refuse("not_qualified", key, f"this version of {name!r} is not qualified")
        if use not in registered:
            refuse(
                "unregistered_use",
                key,
                "workflow is not registered as a user of this version, so a "
                "change to it would not re-qualify this workflow",
            )
        expected = wrapped_body(sub).model_dump(mode="json")
        if _sha256(workflow.subflows[key].model_dump(mode="json")) != _sha256(expected):
            refuse(
                "embedded_drift",
                key,
                f"embedded {name!r} differs from the published version",
            )
        sig = sub.signature
        if workflow.surfaces:
            refuse(
                "multi_surface",
                key,
                "subroutines in multi-surface workflows are not supported yet",
            )
        elif workflow.surface is None:
            refuse("surface_undeclared", key, "workflow declares no surface kind")
        elif workflow.surface != sig.surface_kind:
            refuse(
                "surface_kind_mismatch",
                key,
                f"{name!r} runs on {sig.surface_kind!r}, workflow on "
                f"{workflow.surface!r}",
            )
        for code, message in _caller_inputs_problems(workflow, sig):
            refuse(code, key, message)
    return SubroutineAdmission(
        workflow_id=wid, uses=tuple(uses), refusals=tuple(refusals)
    )


# -- detection ----------------------------------------------------------------------


def step_shape(step: Step) -> str:
    """An application-free token for comparing steps across workflows.

    Uses the action kind, whether a parameter feeds the step, the key chord
    and the accessibility role. Never labels, names, selectors, text, URLs
    or application names.
    """

    role = ""
    if step.anchor is not None and step.anchor.structural is not None:
        role = (step.anchor.structural.role or "").lower()
    bound = "p" if (step.param or step.target_param) else "-"
    chord = "+".join([*step.modifiers, step.key or ""]) if step.key else ""
    return f"{step.action.value}|{bound}|{chord}|{role}"


class SubroutineOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    step_ids: tuple[str, ...]
    params: tuple[str, ...] = ()


class SubroutineCandidate(BaseModel):
    """A recurring step sequence. A proposal, never admitted or runnable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.subroutine-candidate/v1"] = CANDIDATE_SCHEMA
    status: Literal["candidate"] = "candidate"
    admitted: Literal[False] = False
    runnable: Literal[False] = False
    shape: tuple[str, ...]
    occurrences: tuple[SubroutineOccurrence, ...]

    @property
    def workflows(self) -> tuple[str, ...]:
        return tuple(sorted({o.workflow_id for o in self.occurrences}))


def _straight_runs(workflow: Workflow) -> list[list[Step]]:
    """Maximal straight-line action runs the program walks in order."""

    if workflow.program is None:
        return [list(workflow.steps)] if workflow.steps else []
    runs: list[list[Step]] = []
    for graph in (workflow.program, *workflow.subflows.values()):
        incoming: dict[str, int] = defaultdict(int)
        for state in graph.states.values():
            for transition in state.transitions:
                incoming[transition.target] += 1
        incoming[graph.entry] += 1

        def single_next(state: State) -> Optional[str]:
            if len(state.transitions) == 1 and state.transitions[0].guard is None:
                return state.transitions[0].target
            return None

        continued: set[str] = set()
        for state in graph.states.values():
            nxt = single_next(state)
            if (
                state.kind is StateKind.ACTION
                and nxt is not None
                and incoming[nxt] == 1
            ):
                continued.add(nxt)
        for state_id, state in graph.states.items():
            if state.kind is not StateKind.ACTION or state_id in continued:
                continue
            run: list[Step] = []
            cursor: Optional[State] = state
            seen: set[str] = set()
            while (
                cursor is not None
                and cursor.kind is StateKind.ACTION
                and cursor.step is not None
                and cursor.id not in seen
            ):
                seen.add(cursor.id)
                run.append(cursor.step)
                nxt = single_next(cursor)
                if nxt is None or incoming[nxt] != 1:
                    break
                cursor = graph.states.get(nxt)
            if run:
                runs.append(run)
    return runs


def mine_recurring_sequences(
    workflows: Mapping[str, Workflow],
    *,
    min_length: int = 3,
    min_workflows: int = 2,
    max_length: int = 32,
) -> list[SubroutineCandidate]:
    """Propose step sequences that recur, by shape, across workflows.

    A sequence qualifies when at least ``min_workflows`` distinct workflows
    contain it. A candidate that is part of a longer candidate found in the
    same workflows is dropped. Output order is deterministic.
    """

    if min_length < 2 or min_workflows < 2:
        raise SubroutineError("a subroutine needs 2+ steps seen in 2+ workflows")
    hits: dict[tuple[str, ...], list[SubroutineOccurrence]] = defaultdict(list)
    for wid in sorted(workflows):
        for run in _straight_runs(workflows[wid]):
            shapes = [step_shape(step) for step in run]
            for length in range(min_length, min(max_length, len(run)) + 1):
                for start in range(0, len(run) - length + 1):
                    window = run[start : start + length]
                    params = tuple(
                        name
                        for step in window
                        for name in (step.param, step.target_param)
                        if name
                    )
                    hits[tuple(shapes[start : start + length])].append(
                        SubroutineOccurrence(
                            workflow_id=wid,
                            step_ids=tuple(step.id for step in window),
                            params=params,
                        )
                    )
    found = {
        shape: occ
        for shape, occ in hits.items()
        if len({o.workflow_id for o in occ}) >= min_workflows
    }

    def inside(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
        n = len(short)
        return any(long[i : i + n] == short for i in range(len(long) - n + 1))

    kept: list[SubroutineCandidate] = []
    for shape in sorted(found, key=lambda s: (-len(s), s)):
        wids = {o.workflow_id for o in found[shape]}
        if any(
            len(k.shape) > len(shape)
            and inside(shape, k.shape)
            and wids <= set(k.workflows)
            for k in kept
        ):
            continue
        kept.append(SubroutineCandidate(shape=shape, occurrences=tuple(found[shape])))
    return kept


def subroutine_from_steps(
    signature: SubroutineSignature, steps: Iterable[Step]
) -> Subroutine:
    """Build a straight-line subroutine from consecutive steps (deep copies)."""

    items = [step.model_copy(deep=True) for step in steps]
    if not items:
        raise SubroutineError("a subroutine needs at least one step")
    ids = [step.id for step in items]
    repeated = sorted({sid for sid in ids if ids.count(sid) > 1})
    if repeated:
        # A repeated id would make a later step silently replace an earlier
        # one, so the body would no longer be the steps it was built from.
        raise SubroutineError(f"step ids must be unique; repeated {repeated}")
    states: dict[str, State] = {}
    end = "end"
    for index, step in enumerate(items):
        target = end if index + 1 == len(items) else items[index + 1].id
        states[step.id] = State(
            id=step.id,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target=target)],
        )
    if end in states:
        raise SubroutineError("a step id collides with the terminal id")
    states[end] = State(id=end, kind=StateKind.TERMINAL, outcome="success")
    return Subroutine(
        signature=signature, body=ProgramGraph(entry=items[0].id, states=states)
    )
