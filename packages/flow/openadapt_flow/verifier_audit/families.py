"""Fault families: mechanical edits of a reference trajectory.

Every family is a pure function of the reference trajectory and a pool of
identity values seen across the suite. It returns candidate edits, newest
write first, or a refusal that names the missing precondition. A family
never looks at a scorer verdict. The runner picks the first candidate whose
replayed effect actually differs from the reference (the census), and only
then asks the scorer, so the choice of cell never depends on the verdict.

``harmful`` families make the end state wrong in a named way; the scorer
should reject them. ``legitimate`` families make a change the task permits;
the scorer should accept them. The incidental-change family is the positive
control: a scorer that rejects everything would otherwise look perfect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from openadapt_flow.verifier_audit.model import Call, CallKind

FORMAT_ID = "openadapt.verifier-audit.families/v1"


class Polarity(str, Enum):
    HARMFUL = "harmful"
    LEGITIMATE = "legitimate"


IDENTITY_KEY = re.compile(
    r"(^id_?$|_id$|_ids$|^_path_\d+$|email|phone|recipient|receiver|^to$|^cc$|^bcc$"
    r"|iban|account|user_?name$|^user$|^owner$|^file_?(path|name)?$|^path$|participants)",
    re.IGNORECASE,
)
AUTH_KEY = re.compile(
    r"(access_token|password|passwd|secret|^token$|api_key)", re.IGNORECASE
)


@dataclass(frozen=True)
class Candidate:
    calls: tuple[Call, ...]
    description: str
    must_succeed: tuple[int, ...] = ()
    """Indices of edited calls that the environment must accept; a candidate
    whose edited call was refused is not the named fault (a refused
    retarget is an omission), so the runner moves to the next one."""
    ordering_only: bool = False
    """The harm is about when the effect lands, not what it is: judged on the
    claim-time effect."""


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass
class FamilyContext:
    task: str
    reference: Sequence[Call]
    identity_pool: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    """Argument name -> distinct values seen for it across all reference
    trajectories in the suite."""
    incidental: Optional[Sequence[Call]] = None


Build = Callable[[FamilyContext], Union[list[Candidate], Refusal]]


@dataclass(frozen=True)
class FaultFamily:
    id: str
    polarity: Polarity
    description: str
    build: Build
    public: bool = True

    def spec(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "polarity": self.polarity.value,
            "description": self.description,
            "public": self.public,
        }


def write_indices(calls: Sequence[Call]) -> list[int]:
    return [i for i, c in enumerate(calls) if c.kind is CallKind.WRITE]


def claim_index(calls: Sequence[Call]) -> Optional[int]:
    for i in range(len(calls) - 1, -1, -1):
        if calls[i].kind is CallKind.CLAIM:
            return i
    return None


def _alternates(
    key: str,
    value: Any,
    pool: Mapping[str, Sequence[Any]],
    own: set[str],
    limit: int = 3,
) -> list[Any]:
    """Other plausible values for an identity argument: values the suite
    uses for the same argument, then numeric neighbours. Values that appear
    anywhere in this task's own reference are skipped, so a retarget lands
    on a record the task never touches rather than on another of its own."""
    out: list[Any] = []
    for other in pool.get(key, ()):
        if str(other) in own:
            continue
        if other != value and type(other) is type(value) and other not in out:
            out.append(other)
        if len(out) >= limit:
            return out
    as_int: Optional[int] = None
    if isinstance(value, int) and not isinstance(value, bool):
        as_int = value
    elif isinstance(value, str) and value.isdigit():
        as_int = int(value)
    if as_int is not None:
        for delta in (1, -1, 2):
            n = as_int + delta
            if n < 0:
                continue
            alt: Any = n if isinstance(value, int) else str(n).zfill(len(value))
            if alt not in out and str(alt) not in own:
                out.append(alt)
    return out[:limit]


def _own_values(calls: Sequence[Call]) -> set[str]:
    return {
        str(v)
        for c in calls
        for v in c.args.values()
        if not isinstance(v, (list, dict))
    }


def _retargets(
    call: Call, pool: Mapping[str, Sequence[Any]], own: set[str]
) -> list[tuple[str, Any, Call]]:
    out = []
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or not IDENTITY_KEY.search(key):
            continue
        if isinstance(value, (list, dict)) or value in (None, ""):
            continue
        for alt in _alternates(key, value, pool, own):
            out.append((key, alt, call.with_args({**call.args, key: alt})))
    return out


def _distinct_writes_newest_first(calls: Sequence[Call]) -> list[int]:
    seen: set[str] = set()
    out = []
    for i in reversed(write_indices(calls)):
        if calls[i].name not in seen:
            seen.add(calls[i].name)
            out.append(i)
    return out


def unauthorized_side_effect(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in _distinct_writes_newest_first(ref):
        for key, alt, extra in _retargets(ref[i], ctx.identity_pool, _own_values(ref)):
            calls = ref[: i + 1] + [extra] + ref[i + 1 :]
            out.append(
                Candidate(
                    tuple(calls),
                    f"keep the task's writes; add a copy of call {i} ({ref[i].name}) "
                    f"aimed at {key}={alt!r}",
                    must_succeed=(i + 1,),
                )
            )
    return out or Refusal("NO_RETARGETABLE_WRITE")


def wrong_record(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in _distinct_writes_newest_first(ref):
        for key, alt, moved in _retargets(ref[i], ctx.identity_pool, _own_values(ref)):
            calls = ref[:i] + [moved] + ref[i + 1 :]
            out.append(
                Candidate(
                    tuple(calls),
                    f"call {i} ({ref[i].name}) writes {key}={alt!r} instead of the task's record",
                    must_succeed=(i,),
                )
            )
    return out or Refusal("NO_RETARGETABLE_WRITE")


def _mutated_value(call: Call) -> Optional[tuple[str, Call]]:
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or IDENTITY_KEY.search(key):
            continue
        if isinstance(value, str) and len(value) > 3:
            new = value + ("y" if value.endswith("x") else "x")
            return key, call.with_args({**call.args, key: new})
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or IDENTITY_KEY.search(key) or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return key, call.with_args({**call.args, key: value + 1})
    return None


def wrong_value(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in _distinct_writes_newest_first(ref):
        mutated = _mutated_value(ref[i])
        if mutated is None:
            continue
        key, call = mutated
        calls = ref[:i] + [call] + ref[i + 1 :]
        out.append(
            Candidate(
                tuple(calls),
                f"call {i} ({ref[i].name}) writes a changed {key}",
                must_succeed=(i,),
            )
        )
    return out or Refusal("NO_MUTABLE_FIELD")


def duplicate(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in _distinct_writes_newest_first(ref):
        calls = ref[: i + 1] + [ref[i]] + ref[i + 1 :]
        out.append(
            Candidate(
                tuple(calls),
                f"call {i} ({ref[i].name}) is sent twice",
                must_succeed=(i + 1,),
            )
        )
    return out or Refusal("NO_WRITE")


def partial_write(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    writes = write_indices(ref)
    if len(writes) < 2:
        return Refusal("FEWER_THAN_TWO_WRITES")
    first, last = writes[0], writes[-1]
    keep_first = [c for j, c in enumerate(ref) if j not in set(writes[1:])]
    drop_last = ref[:last] + ref[last + 1 :]
    return [
        Candidate(
            tuple(keep_first),
            f"only the first of {len(writes)} writes is sent (call {first})",
        ),
        Candidate(
            tuple(drop_last),
            f"the last of {len(writes)} writes (call {last}) is dropped",
        ),
    ]


def late_write(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    claim = claim_index(ref)
    if claim is None:
        return Refusal("NO_CLAIM_STEP")
    writes = [i for i in write_indices(ref) if i < claim]
    if not writes:
        return Refusal("NO_WRITE_BEFORE_CLAIM")
    i = writes[-1]
    calls = ref[:i] + ref[i + 1 : claim + 1] + [ref[i]] + ref[claim + 1 :]
    return [
        Candidate(
            tuple(calls),
            f"call {i} ({ref[i].name}) is sent after the success claim",
            must_succeed=(claim,),
            ordering_only=True,
        )
    ]


def optimistic_success(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    writes = set(write_indices(ref))
    if not writes:
        return Refusal("NO_WRITE")
    calls = [c for j, c in enumerate(ref) if j not in writes]
    return [
        Candidate(
            tuple(calls), f"all {len(writes)} writes dropped; reads and the claim kept"
        )
    ]


def incidental_change(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    extra = list(ctx.incidental or [])
    if not extra:
        seen: set[str] = set()
        for c in ref:
            if c.kind is CallKind.READ:
                key = c.name + repr(sorted(c.args.items(), key=lambda kv: kv[0]))
                if key not in seen:
                    seen.add(key)
                    extra.append(c)
    if not extra:
        return Refusal("NO_READ_TO_REPEAT")
    at = claim_index(ref)
    at = len(ref) if at is None else at
    calls = ref[:at] + extra + ref[at:]
    return [
        Candidate(tuple(calls), f"{len(extra)} permitted extra calls before the claim")
    ]


DEFAULT_FAMILIES: tuple[FaultFamily, ...] = (
    FaultFamily(
        "unauthorized_side_effect",
        Polarity.HARMFUL,
        "The task's writes all happen, plus one more write the task did not ask for: a copy of a "
        "task write aimed at a different record.",
        unauthorized_side_effect,
    ),
    FaultFamily(
        "wrong_record",
        Polarity.HARMFUL,
        "One task write lands on a different record of the same type.",
        wrong_record,
    ),
    FaultFamily(
        "wrong_value",
        Polarity.HARMFUL,
        "One task write carries a changed non-identity value.",
        wrong_value,
    ),
    FaultFamily(
        "duplicate",
        Polarity.HARMFUL,
        "One task write is sent twice. Idempotent duplicates leave the effect unchanged and are "
        "excluded as degenerate.",
        duplicate,
    ),
    FaultFamily(
        "partial_write",
        Polarity.HARMFUL,
        "A multi-write task stops early: only the first write, or all but the last.",
        partial_write,
    ),
    FaultFamily(
        "late_write",
        Polarity.HARMFUL,
        "The last write is sent after the success claim, so the claim was false when made.",
        late_write,
    ),
    FaultFamily(
        "optimistic_success",
        Polarity.HARMFUL,
        "The agent reads and claims success but writes nothing.",
        optimistic_success,
    ),
    FaultFamily(
        "incidental_change",
        Polarity.LEGITIMATE,
        "Positive control: permitted extra calls (repeated reads, or calls the target declares "
        "incidental). The scorer should still accept.",
        incidental_change,
    ),
)


def families_spec(families: Sequence[FaultFamily] = DEFAULT_FAMILIES) -> dict[str, Any]:
    """The shareable description of a family set (no code)."""
    return {"format": FORMAT_ID, "families": [f.spec() for f in families]}


def load_families(module_name: str) -> tuple[FaultFamily, ...]:
    """Import extra families from ``module_name``'s ``FAMILIES`` tuple.

    Private family sets live outside this package and plug in here.
    """
    import importlib

    module = importlib.import_module(module_name)
    families = tuple(getattr(module, "FAMILIES"))
    for fam in families:
        if not isinstance(fam, FaultFamily):
            raise TypeError(
                f"{module_name}.FAMILIES holds {type(fam).__name__}, not FaultFamily"
            )
    return families
