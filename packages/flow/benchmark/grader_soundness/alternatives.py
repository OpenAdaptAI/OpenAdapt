"""Valid-alternative solutions: trajectories that reach the reference end state
by another path.

Each family edits the reference trajectory mechanically and returns candidate
edits. The runner replays each candidate without scoring and keeps the first
one whose end state equals the reference end state exactly. Only that one is
scored. A grader that fails it rejects a correct outcome: a false reject.

Equality of the end state is the definition of "valid" here. Candidates whose
end state differs are listed as not equivalent and never counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

from openadapt_flow.verifier_audit.families import (
    AUTH_KEY,
    IDENTITY_KEY,
    Candidate,
    FamilyContext,
    Refusal,
    claim_index,
    incidental_change,
    write_indices,
)
from openadapt_flow.verifier_audit.model import Call, CallKind, Episode, Verdict

Build = Callable[[FamilyContext], Union[list[Candidate], Refusal]]

MISSING_ID = "987654321"
"""An identity value no benchmark here uses, for the refused attempt."""


@dataclass
class AltContext(FamilyContext):
    foreign_reads: Sequence[Call] = ()
    """Read-only calls from other tasks of the same suite, for
    ``foreign_reads``. Empty when the target cannot replay them."""


def _insert_before_claim(ref: list[Call], extra: Sequence[Call]) -> list[Call]:
    at = claim_index(ref)
    at = len(ref) if at is None else at
    return ref[:at] + list(extra) + ref[at:]


def repeated_reads(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    # The kit's own positive control, without target-declared incidentals.
    return incidental_change(
        FamilyContext(task=ctx.task, reference=ctx.reference, identity_pool={})
    )


def foreign_reads(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    pool = list(getattr(ctx, "foreign_reads", ()) or ())
    if not pool:
        return Refusal("NO_FOREIGN_READS")
    ref = list(ctx.reference)
    own = {c.name for c in ref}
    picked: list[Call] = []
    seen: set[str] = set()
    for call in pool:
        if call.name in own or call.name in seen:
            continue
        seen.add(call.name)
        picked.append(call)
    if not picked:
        return Refusal("NO_FOREIGN_READS")
    out = []
    for size in (len(picked), 1):
        calls = _insert_before_claim(ref, picked[:size])
        out.append(
            Candidate(
                tuple(calls),
                f"{size} read-only calls from other tasks "
                f"({', '.join(c.name for c in picked[:size])}) before the claim",
            )
        )
    if len(picked) == 1:
        out = out[:1]
    return out


def reordered_writes(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    writes = write_indices(ref)
    if len(writes) < 2:
        return Refusal("FEWER_THAN_TWO_WRITES")
    out = []
    for a, b in zip(writes, writes[1:]):
        if ref[a] == ref[b]:
            continue
        calls = list(ref)
        calls[a], calls[b] = ref[b], ref[a]
        out.append(
            Candidate(
                tuple(calls),
                f"calls {a} ({ref[a].name}) and {b} ({ref[b].name}) sent in the other order",
            )
        )
    return out or Refusal("NO_DISTINCT_WRITE_PAIR")


def _missing_target(call: Call) -> Optional[tuple[str, Call]]:
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or not IDENTITY_KEY.search(key):
            continue
        if isinstance(value, bool) or isinstance(value, (list, dict)):
            continue
        if isinstance(value, int):
            alt: Any = int(MISSING_ID)
        elif isinstance(value, str) and value.isdigit():
            alt = MISSING_ID
        elif isinstance(value, str) and value:
            alt = "nonexistent-" + MISSING_ID
        else:
            continue
        return key, call.with_args({**call.args, key: alt})
    return None


def refused_attempt(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in write_indices(ref):
        found = _missing_target(ref[i])
        if found is None:
            continue
        key, bad = found
        calls = ref[:i] + [bad] + ref[i:]
        out.append(
            Candidate(
                tuple(calls),
                f"before call {i} ({ref[i].name}), the same call aimed at a "
                f"missing {key}, which the environment must refuse",
                must_succeed=(i,),  # here: the index that must be refused
            )
        )
    return out or Refusal("NO_IDENTITY_ARGUMENT")


def _changed(call: Call) -> Optional[Call]:
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or IDENTITY_KEY.search(key):
            continue
        if isinstance(value, str) and len(value) > 3:
            return call.with_args({**call.args, key: value + "x"})
    for key, value in call.args.items():
        if AUTH_KEY.search(key) or IDENTITY_KEY.search(key) or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return call.with_args({**call.args, key: value + 1})
    return None


def self_corrected_write(ctx: FamilyContext) -> Union[list[Candidate], Refusal]:
    ref = list(ctx.reference)
    out = []
    for i in write_indices(ref):
        bad = _changed(ref[i])
        if bad is None:
            continue
        calls = ref[:i] + [bad] + ref[i:]
        out.append(
            Candidate(
                tuple(calls),
                f"call {i} ({ref[i].name}) first sent with a changed value, then as in the reference",
            )
        )
    return out or Refusal("NO_MUTABLE_FIELD")


ALTERNATIVES: dict[str, Build] = {
    "repeated_reads": repeated_reads,
    "foreign_reads": foreign_reads,
    "reordered_writes": reordered_writes,
    "refused_attempt": refused_attempt,
    "self_corrected_write": self_corrected_write,
}


def audit_alternative(
    target: Any,
    task: str,
    family: str,
    ctx: FamilyContext,
    control: Episode,
    *,
    max_candidates: int = 6,
) -> dict[str, Any]:
    """Census, then score, one valid-alternative family on one task."""
    from openadapt_flow.verifier_audit.model import effect_diff

    built = ALTERNATIVES[family](ctx)
    base = {"task": task, "family": family, "polarity": "legitimate"}
    if isinstance(built, Refusal):
        return {**base, "status": "REFUSED", "reason": built.reason}
    attempts = []
    for cand in built[:max_candidates]:
        census = target.run(task, cand.calls, score=False)
        if census.verdict is Verdict.ERROR:
            attempts.append({"edit": cand.description, "harness_error": True})
            continue
        same = census.final_effect == control.final_effect
        if family == "refused_attempt":
            # The inserted call sits at must_succeed[0]; it must be refused,
            # and every other call accepted.
            bad = cand.must_succeed[0]
            refused_ok = bad in census.call_errors
        else:
            refused_ok = True  # the end state alone decides
        attempts.append(
            {
                "edit": cand.description,
                "same_end_state": same,
                "calls_as_expected": refused_ok,
            }
        )
        if not (same and refused_ok):
            continue
        ep = target.run(task, cand.calls, score=True)
        if ep.final_effect != census.final_effect:
            status = "EFFECT_NOT_REPRODUCIBLE"
        elif ep.verdict is Verdict.PASS:
            status = "ACCEPTED"
        elif ep.verdict is Verdict.FAIL:
            status = "FALSE_REJECT"
        else:
            status = "SCORER_ERROR"
        return {
            **base,
            "status": status,
            "edit": cand.description,
            "calls": [c.to_json() for c in cand.calls],
            "verdict": ep.verdict.value if ep.verdict else None,
            "effect_diff": effect_diff(control.final_effect, ep.final_effect),
            "call_errors": ep.call_errors,
            "scorer_detail": ep.scorer_detail,
            "census_attempts": attempts,
        }
    if attempts and all(a.get("harness_error") for a in attempts):
        status = "HARNESS_ERROR"
    else:
        status = "NOT_EQUIVALENT"
    return {**base, "status": status, "census_attempts": attempts}


def read_only_pool(target: Any, tasks: Sequence[str]) -> dict[str, list[Call]]:
    """For each task, the read calls of every other task's reference."""
    refs = {t: target.reference(t) for t in tasks}
    out: dict[str, list[Call]] = {}
    for task in tasks:
        out[task] = [
            c
            for other in tasks
            if other != task
            for c in refs[other]
            if c.kind is CallKind.READ
        ]
    return out
