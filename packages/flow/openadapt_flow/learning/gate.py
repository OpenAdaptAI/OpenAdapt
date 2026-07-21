"""Program-level regression gate: compare two programs on SEMANTIC invariants.

PR #70 established the invariant a REPAIR must satisfy: it may change HOW a step
is performed (its locator / rung) but must NEVER silently weaken WHAT it means
(its identity band), how its effects are verified (effect coverage), or its risk
class. That gate (:class:`openadapt_flow.runtime.healing.RegressionGate`) rules
on a single heal patch. A learned program REVISION is the same risk at a larger
grain: a candidate :class:`~openadapt_flow.ir.ProgramGraph` may quietly drop an
armed step's identity band, a declared system-of-record effect, or downgrade an
irreversible step -- and still look like it "covers more traces".

The earlier version of this gate lifted PR #70's per-step check to every step id
that SURVIVES from the active program into the candidate. Two external reviews
found that this leaves the loop's safety open: it compares step IDs, not program
SEMANTICS. A candidate could silently

* remove an identity-armed safety step (no surviving id => not gated);
* remove a system-of-record effect VERIFICATION step;
* replace a consequential step with a step carrying a NEW id;
* add a new irreducible/irreversible step with NO effects;
* introduce a broad optional skip on the path to a write;
* change branch topology so a write becomes reachable under MORE conditions

-- and still PASS, because none of these touch a surviving step's before/after
anchor. New steps were never gated; removed steps were merely listed.

This module now closes that hole. It keeps the per-step check (surviving-id
identity / effect / risk -- the existing tests rely on it) AND adds a SEMANTIC
layer that traverses BOTH programs (their subflows too), matches consequential
actions by structural ROLE rather than raw ``step.id`` (so a renamed-but-
equivalent step is matched and a genuinely-new consequential step is caught),
and fails (quarantines) the candidate if it WEAKENS any of:

* **Reachable consequential actions** -- every write / irreversible action
  reachable in the active program must still be present and gated at least as
  strictly in the candidate.
* **Dominating identity checks** -- the set of identity-armed guards that MUST
  pass before a consequential action is reached must not shrink.
* **Effect contracts** -- an action that had a system-of-record ``effects``
  requirement must not lose it; a NEW consequential action must not be
  introduced WITHOUT effects.
* **Risk labels** -- an action's risk must not be silently downgraded
  (irreversible -> reversible).
* **Approval requirements** -- an action requiring operator confirmation
  (``Effect.needs_operator_confirmation``) must not lose it.
* **Execution domain** -- the candidate must not make a consequential action
  reachable under strictly MORE conditions (a dominating guard / branch
  condition dropped so a write happens in cases it did not before).

Deterministic and ``$0``: the surviving-id identity check reuses the same OCR
band verifier the pre-click gate uses; the semantic layer compares structure.
No model calls.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    Anchor,
    HealEvent,
    Predicate,
    ProgramGraph,
    StateKind,
    Step,
)
from openadapt_flow.runtime.effects.effect import Effect
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    GateResult,
    RegressionGate,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch


def _action_steps(
    graph: ProgramGraph, subflows: Optional[dict[str, ProgramGraph]] = None
) -> dict[str, Step]:
    """Map step id -> Step across a program graph AND its subflows (a loop
    body's steps count too)."""
    graphs = [graph, *(subflows or {}).values()]
    steps: dict[str, Step] = {}
    for g in graphs:
        for state in g.states.values():
            if state.kind is StateKind.ACTION and state.step is not None:
                steps[state.step.id] = state.step
    return steps


def _effect_key(effect: Effect) -> str:
    """A canonical, order-independent key identifying an effect's CONTRACT.

    Two effects with the same key assert the same thing about the system of
    record; a candidate that no longer contains a baseline effect's key has
    dropped that coverage."""
    match = ",".join(f"{k}={v}" for k, v in sorted(effect.match.items()))
    return (
        f"{effect.kind.value}|match[{match}]|field={effect.field}"
        f"|value={effect.value}|count={effect.expected_count}"
    )


def _effect_baseline_and_now(
    old_step: Step, new_step: Step
) -> tuple[dict[str, bool], dict[str, "object"]]:
    """Build the (baseline, now) effect maps :meth:`RegressionGate.evaluate`
    consumes, so a dropped/altered confirmed effect trips effect-regression.

    Each of the OLD step's effects is treated as CONFIRMED in the baseline; the
    matching "now" re-check returns True iff the NEW step still declares an
    effect with the same contract key. Dropping or mutating an effect => the
    re-check fails => effect regression."""
    new_keys = {_effect_key(e) for e in new_step.effects}
    baseline: dict[str, bool] = {}
    now: dict[str, object] = {}
    for effect in old_step.effects:
        key = _effect_key(effect)
        baseline[key] = True
        now[key] = lambda k=key: k in new_keys
    return baseline, now


# -- semantic-layer helpers ---------------------------------------------------
#
# These traverse the WHOLE program (top-level graph + subflows), matching
# consequential actions by structural ROLE (not raw step.id) so a renamed step
# is still the SAME action and a brand-new consequential step is caught. The
# dominance/reachability analysis below answers, for each consequential action,
# "which identity-armed guards and branch conditions MUST hold to reach it" --
# the thing a step-id diff cannot see.

#: Graph name of the top-level program in the flattened union graph.
_MAIN = "__main__"


def _norm_intent(intent: str) -> str:
    return " ".join((intent or "").split()).casefold()


def _role_str(step: Step) -> str:
    """Structural ROLE of an action step: its action kind + normalized intent.

    Stable across a rename of ``step.id`` (a renamed-but-equivalent step keeps
    its purpose), so two versions' corresponding actions match by role even when
    their ids differ, and a genuinely NEW purpose is a distinct role."""
    return f"{step.action.value}|{_norm_intent(step.intent)}"


def _step_armed(step: Step) -> bool:
    """Whether a step carries an identity-armed guard (a recorded context band,
    structured identity, or PHI-free identity template) -- the pre-click
    wrong-target check is active for it."""
    a = step.anchor
    if a is None:
        return False
    return bool(
        a.context_text
        or a.structured_identity
        or (a.identity_template and a.identity_template.tokens)
    )


def _needs_approval(step: Step) -> bool:
    """True when any effect requires operator confirmation before it may be
    trusted (an unconfirmed system-of-record binding)."""
    return any(getattr(e, "needs_operator_confirmation", False) for e in step.effects)


def _is_consequential(step: Step) -> bool:
    """A write / irreversible action: it changes a system of record, so its
    identity + effect + risk gating is the thing the gate must protect. A step
    is consequential if it is irreversible, declares system-of-record effects,
    or needs operator confirmation."""
    return step.risk == "irreversible" or bool(step.effects) or _needs_approval(step)


def _predicate_key(pred: Optional[Predicate]) -> str:
    """A canonical, order-independent key for a deterministic predicate, so the
    SAME branch/guard condition matches across versions even after a rename of
    the surrounding state."""
    if pred is None:
        return "TRUE"
    parts = [pred.kind.value]
    if pred.text is not None:
        parts.append(f"text={pred.text}")
    if pred.param is not None:
        parts.append(f"param={pred.param}")
    if pred.value is not None:
        parts.append(f"value={pred.value}")
    if pred.anchor is not None:
        parts.append(f"anchor={pred.anchor.template}@{pred.anchor.click_point}")
    if pred.operands:
        inner = ",".join(sorted(_predicate_key(o) for o in pred.operands))
        parts.append(f"({inner})")
    return "|".join(parts)


class _MustAnalysis(BaseModel):
    """The dominance/reachability result for one program.

    ``reachable`` is the set of union-graph node ids reachable from the entry;
    ``must_in`` maps each reachable node -> the set of guard TOKENS that hold on
    EVERY path from entry into it (identity-guard tokens ``id::<role>`` and
    branch/precondition tokens ``cond::<pred>``); ``step_node`` maps a step id to
    its node; ``role_to_id`` maps an armed step's role token back to a step id
    (for readable failure messages)."""

    reachable: set[str] = Field(default_factory=set)
    must_in: dict[str, set[str]] = Field(default_factory=dict)
    step_node: dict[str, str] = Field(default_factory=dict)
    role_to_id: dict[str, str] = Field(default_factory=dict)


def _gen(step: Optional[Step]) -> set[str]:
    """Tokens ESTABLISHED by passing through an action state: its identity
    arming (an ``id::`` token) and any HALT precondition guard (a ``cond::``
    token). A ``skip`` guard establishes nothing -- the step is a no-op when its
    predicate is unmet, so it is not a must-condition."""
    tokens: set[str] = set()
    if step is None:
        return tokens
    if _step_armed(step):
        tokens.add("id::" + _role_str(step))
    if step.guard is not None and step.guard.on_unmet == "halt":
        tokens.add("cond::" + _predicate_key(step.guard.predicate))
    return tokens


def _analyze(
    main: ProgramGraph, subflows: Optional[dict[str, ProgramGraph]]
) -> _MustAnalysis:
    """Flatten ``main`` + ``subflows`` into one node graph and compute, for each
    reachable node, the guard tokens that MUST hold to reach it (a forward
    "available guards" dataflow: meet == set intersection over paths).

    Subflow/loop dispatch edges are modeled conservatively: a ``subflow_call`` /
    ``loop`` node connects to its target subflow's entry, so a write inside a
    subflow inherits the caller's dominating guards. Cycles converge because the
    meet only ever shrinks a node's must-set."""
    graphs: dict[str, ProgramGraph] = {_MAIN: main}
    for name, g in (subflows or {}).items():
        graphs[name] = g

    def nid(gname: str, sid: str) -> str:
        return f"{gname}\x00{sid}"

    node_step: dict[str, Optional[Step]] = {}
    # adjacency: node -> list of (successor_node, edge_contribution_tokens)
    adj: dict[str, list[tuple[str, set[str]]]] = {}
    role_to_id: dict[str, str] = {}

    for gname, g in graphs.items():
        for sid, st in g.states.items():
            n = nid(gname, sid)
            step = st.step if st.kind is StateKind.ACTION else None
            node_step[n] = step
            if step is not None and _step_armed(step):
                role_to_id.setdefault("id::" + _role_str(step), step.id)
            base_gen = _gen(step)
            outs: list[tuple[str, set[str]]] = []
            for t in st.transitions:
                if t.target in g.states:
                    contrib = set(base_gen)
                    if t.guard is not None:
                        contrib.add("cond::" + _predicate_key(t.guard))
                    outs.append((nid(gname, t.target), contrib))
            if (
                st.kind is StateKind.SUBFLOW_CALL
                and st.subflow is not None
                and st.subflow in graphs
            ):
                outs.append((nid(st.subflow, graphs[st.subflow].entry), set(base_gen)))
            if (
                st.kind is StateKind.LOOP
                and st.loop is not None
                and st.loop.body in graphs
            ):
                outs.append(
                    (nid(st.loop.body, graphs[st.loop.body].entry), set(base_gen))
                )
            adj[n] = outs

    entry = nid(_MAIN, main.entry)
    if entry not in node_step:
        return _MustAnalysis()

    # reachability from entry
    reachable: set[str] = set()
    stack = [entry]
    while stack:
        n = stack.pop()
        if n in reachable:
            continue
        reachable.add(n)
        for succ, _ in adj.get(n, []):
            if succ not in reachable:
                stack.append(succ)

    preds: dict[str, list[tuple[str, set[str]]]] = {n: [] for n in reachable}
    for n in reachable:
        for succ, contrib in adj.get(n, []):
            if succ in reachable:
                preds[succ].append((n, contrib))

    # forward MUST dataflow; None == TOP (universe) until a node is first reached
    must: dict[str, Optional[set[str]]] = {n: None for n in reachable}
    must[entry] = set()
    changed = True
    while changed:
        changed = False
        for n in reachable:
            if n == entry:
                continue
            new_in: Optional[set[str]] = None
            for p, contrib in preds[n]:
                p_in = must[p]
                if p_in is None:
                    continue  # predecessor still TOP -> identity for intersection
                p_out = p_in | contrib
                new_in = set(p_out) if new_in is None else (new_in & p_out)
            if new_in is None:
                continue
            if must[n] is None or new_in != must[n]:
                must[n] = new_in
                changed = True

    must_in: dict[str, set[str]] = {n: (must[n] or set()) for n in reachable}
    step_node: dict[str, str] = {}
    for n in reachable:
        step = node_step[n]
        if step is not None:
            step_node[step.id] = n

    return _MustAnalysis(
        reachable=reachable,
        must_in=must_in,
        step_node=step_node,
        role_to_id=role_to_id,
    )


def _identity_guards(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t.startswith("id::")}


def _branch_conditions(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t.startswith("cond::")}


def _reach_conditions(step: Step, node_tokens: set[str]) -> set[str]:
    """Branch/precondition tokens that MUST hold for this action to EXECUTE: the
    conditions dominating its node PLUS its own HALT precondition guard (removing
    that guard makes the write happen in cases it previously did not)."""
    conds = _branch_conditions(node_tokens)
    if step.guard is not None and step.guard.on_unmet == "halt":
        conds = conds | {"cond::" + _predicate_key(step.guard.predicate)}
    return conds


def _match_actions(
    active_steps: dict[str, Step], cand_steps: dict[str, Step]
) -> tuple[dict[str, str], list[str]]:
    """Correspond active action steps to candidate ones -- by surviving id
    first, then by structural ROLE (so a renamed-but-equivalent step matches and
    a genuinely-new step is left unmatched).

    Returns ``(matched, new_candidate_ids)`` where ``matched`` maps an active
    step id to its candidate step id, and ``new_candidate_ids`` are candidate
    steps with no active counterpart (candidates for the "new consequential
    without effects" check)."""
    matched: dict[str, str] = {}
    used_cand: set[str] = set()
    for sid in active_steps:
        if sid in cand_steps:
            matched[sid] = sid
            used_cand.add(sid)

    cand_by_role: dict[str, list[str]] = {}
    for cid, cstep in cand_steps.items():
        if cid in used_cand:
            continue
        cand_by_role.setdefault(_role_str(cstep), []).append(cid)

    for sid, astep in active_steps.items():
        if sid in matched:
            continue
        bucket = cand_by_role.get(_role_str(astep))
        if bucket:
            cid = bucket.pop(0)
            matched[sid] = cid
            used_cand.add(cid)

    new_candidate_ids = [cid for cid in cand_steps if cid not in used_cand]
    return matched, new_candidate_ids


def _semantic_failures(
    active: ProgramGraph,
    candidate: ProgramGraph,
    active_subflows: Optional[dict[str, ProgramGraph]],
    candidate_subflows: Optional[dict[str, ProgramGraph]],
) -> list[str]:
    """Compare the two programs on the semantic safety invariants (see the module
    docstring). Returns one failure line per weakened invariant, naming the
    invariant and the action involved."""
    failures: list[str] = []
    active_steps = _action_steps(active, active_subflows)
    cand_steps = _action_steps(candidate, candidate_subflows)
    a_an = _analyze(active, active_subflows)
    c_an = _analyze(candidate, candidate_subflows)
    matched, new_candidate_ids = _match_actions(active_steps, cand_steps)

    def _label(step: Step) -> str:
        return f"{step.id!r} ({step.intent!r})"

    # -- every reachable consequential action in the active program -----------
    for sid, astep in active_steps.items():
        a_node = a_an.step_node.get(sid)
        if a_node is None or a_node not in a_an.reachable:
            continue  # unreachable in the active program -> not a live contract
        if not _is_consequential(astep):
            continue

        cid = matched.get(sid)
        if cid is None:
            failures.append(
                f"reachable consequential action {_label(astep)} is no longer "
                "present in the candidate (its write/verification would be "
                "silently dropped)"
            )
            continue
        cstep = cand_steps[cid]

        # effect contract must be preserved or strengthened
        a_keys = {_effect_key(e) for e in astep.effects}
        c_keys = {_effect_key(e) for e in cstep.effects}
        lost_effects = a_keys - c_keys
        if lost_effects:
            failures.append(
                f"effect contract weakened on {_label(astep)}: "
                f"{len(lost_effects)} system-of-record effect(s) dropped"
            )

        # risk must not be downgraded
        if astep.risk == "irreversible" and cstep.risk != "irreversible":
            failures.append(
                f"risk downgraded on {_label(astep)} "
                f"({astep.risk} -> {cstep.risk}): its refuse-when-unverifiable "
                "protection would be lost"
            )

        # operator-confirmation (approval) requirement must not be dropped
        if _needs_approval(astep) and not _needs_approval(cstep):
            failures.append(
                f"operator confirmation requirement dropped on {_label(astep)}: "
                "an unconfirmed system-of-record write would be trusted"
            )

        # the action's OWN identity arming must not be dropped (a renamed step
        # the per-step gate cannot match)
        if _step_armed(astep) and not _step_armed(cstep):
            failures.append(
                f"identity arming dropped on {_label(astep)}: the pre-click "
                "wrong-target check would be disabled"
            )

        c_node = c_an.step_node.get(cid)
        c_tokens = (
            c_an.must_in.get(c_node, set())
            if c_node is not None and c_node in c_an.reachable
            else set()
        )
        a_tokens = a_an.must_in.get(a_node, set())

        # dominating identity checks must not shrink
        lost_guards = _identity_guards(a_tokens) - _identity_guards(c_tokens)
        if lost_guards:
            names = sorted(a_an.role_to_id.get(t, t[4:]) for t in lost_guards)
            failures.append(
                f"dominating identity checks shrank for {_label(astep)}: "
                f"identity-armed guard(s) no longer required before it: "
                f"{', '.join(names)}"
            )

        # execution domain must not broaden (a dominating condition dropped so
        # the write becomes reachable under strictly more conditions)
        lost_conds = _reach_conditions(astep, a_tokens) - _reach_conditions(
            cstep, c_tokens
        )
        if lost_conds:
            failures.append(
                f"execution domain broadened for {_label(astep)}: "
                f"{len(lost_conds)} gating condition(s) that must hold to reach "
                "the write were dropped -- it is now reachable under more "
                "conditions"
            )

    # -- brand-new consequential actions must carry an effect contract --------
    for cid in new_candidate_ids:
        cstep = cand_steps[cid]
        c_node = c_an.step_node.get(cid)
        if c_node is None or c_node not in c_an.reachable:
            continue  # dead code -> not a live write
        if _is_consequential(cstep) and not cstep.effects:
            failures.append(
                f"new consequential action {_label(cstep)} introduced without "
                "effects (no system-of-record effect contract -- an unverifiable "
                "write)"
            )

    return failures


class StepGateVerdict(BaseModel):
    """The gate verdict for one surviving step."""

    step_id: str
    passed: bool
    result: GateResult


class ProgramGateReport(BaseModel):
    """Aggregate verdict of the program-level regression gate.

    ``passed`` iff every surviving step passed PR #70's per-step gate AND no
    SEMANTIC invariant was weakened. ``per_step`` holds the per-surviving-step
    verdicts; ``removed`` lists step ids present in the active program but absent
    from the candidate (surfaced for review), of which ``armed_removed`` were
    identity-armed; ``semantic_failures`` are the whole-program invariant
    violations (a dropped/renamed consequential action, a shrunk identity-guard
    set, a broadened execution domain, ...). ``failures`` aggregates BOTH the
    per-step failures and the semantic ones (what the loop surfaces as the
    quarantine reason)."""

    passed: bool
    per_step: list[StepGateVerdict] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    armed_removed: list[str] = Field(default_factory=list)
    semantic_failures: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        n = len(self.per_step)
        n_pass = sum(1 for v in self.per_step if v.passed)
        return (
            f"regression gate {n_pass}/{n} surviving steps pass; "
            f"{len(self.removed)} removed ({len(self.armed_removed)} armed); "
            f"{len(self.semantic_failures)} semantic violation(s); "
            f"passed={self.passed}"
        )


def _neutral_anchor() -> Anchor:
    """A minimal anchor for a step that declares none (keyboard/wait step): the
    gate's identity check treats it as unarmed, so it never blocks such a step."""
    return Anchor(template="", region=(0, 0, 0, 0), click_point=(0, 0))


def program_regression_gate(
    active: ProgramGraph,
    candidate: ProgramGraph,
    *,
    active_subflows: Optional[dict[str, ProgramGraph]] = None,
    candidate_subflows: Optional[dict[str, ProgramGraph]] = None,
    gate: Optional[RegressionGate] = None,
    band_verifier: BandVerifier = _default_band_verifier,
) -> ProgramGateReport:
    """Gate a learned program revision on WHAT it means, not which step ids it
    keeps.

    Two layers, both refuse-rather-than-guess:

    1. **Per-surviving-step** (PR #70, unchanged): for every step id present in
       BOTH programs, the identity band / effect coverage / risk class may not
       regress.
    2. **Semantic** (:func:`_semantic_failures`): traverse both programs and
       match consequential actions by ROLE, then fail if the candidate drops a
       reachable write, shrinks a write's dominating identity checks, weakens an
       effect contract, downgrades risk, drops an approval requirement, broadens
       a write's execution domain, or introduces a new consequential action with
       no effect contract.

    ``passed`` is False as soon as EITHER layer flags a weakening -- the
    candidate is then refused by the loop exactly as a regressing heal patch is
    quarantined."""
    gate = gate or RegressionGate()
    active_steps = _action_steps(active, active_subflows)
    cand_steps = _action_steps(candidate, candidate_subflows)

    per_step: list[StepGateVerdict] = []
    failures: list[str] = []
    for step_id, old_step in active_steps.items():
        new_step = cand_steps.get(step_id)
        if new_step is None:
            continue  # removed -> handled below + by the semantic layer
        old_anchor = old_step.anchor or _neutral_anchor()
        new_anchor = new_step.anchor or _neutral_anchor()
        # Build the reviewable patch the gate rules on (same object the heal
        # path uses); rung label is cosmetic here (no live resolution happened).
        patch = HealPatch.from_event(
            HealEvent(
                step_id=step_id,
                rung_used="template",
                old_anchor=old_anchor,
                new_anchor=new_anchor,
            )
        )
        effect_baseline, effect_now = _effect_baseline_and_now(old_step, new_step)
        result = gate.evaluate(
            patch,
            old_anchor,
            new_anchor,
            old_step=old_step,
            new_step=new_step,
            band_verifier=band_verifier,
            effect_baseline=effect_baseline,
            effect_now=effect_now,  # type: ignore[arg-type]
        )
        per_step.append(
            StepGateVerdict(step_id=step_id, passed=result.passed, result=result)
        )
        if not result.passed:
            failures.append(f"step '{step_id}': " + "; ".join(result.failures))

    removed = [sid for sid in active_steps if sid not in cand_steps]
    armed_removed = [
        sid
        for sid in removed
        if (a := active_steps[sid].anchor) is not None
        and bool(a.context_text or a.structured_identity or a.identity_template)
    ]

    semantic_failures = _semantic_failures(
        active, candidate, active_subflows, candidate_subflows
    )
    all_failures = failures + semantic_failures

    return ProgramGateReport(
        passed=not all_failures,
        per_step=per_step,
        removed=removed,
        armed_removed=armed_removed,
        semantic_failures=semantic_failures,
        failures=all_failures,
    )
