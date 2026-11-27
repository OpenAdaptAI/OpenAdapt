"""Formal UI State Machine (PortalFSM) compiler and pre-execution safety model checker.

Compiles passive shadow traces into a typed finite-state machine over portal states
and wire-verified transitions, then statically verifies three safety properties
before any click is executed:
  1. No Double-Commit: no reachable path may traverse a mutating commit edge twice
     for the same obligation without an explicit idempotency key.
  2. Consent Dominance: every path reaching a consent-gated commit edge must carry
     a verified capability token for that scope.
  3. Subject Isolation: every opaque literal bound on a commit edge must originate
     from the active episode's subject_key (blocking cross-patient/tenant leaks).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from openadapt_kernel.copilot import ShadowTrace
from openadapt_kernel.ledger import ObligationLedger

if TYPE_CHECKING:
    from openadapt_kernel.canonical import FieldType


@dataclass(frozen=True)
class FSMState:
    state_id: str
    origin: str
    url_pattern: str
    dom_invariants: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FSMTransition:
    transition_id: str
    from_state: str
    to_state: str
    action_label: str
    http_method: str = "GET"
    is_commit: bool = False
    idempotency_key: Optional[str] = None
    required_consent_scope: Optional[str] = None
    bound_fields: Tuple[str, ...] = ()


@dataclass
class SafetyViolation:
    property_name: str
    detail: str
    path: List[str] = field(default_factory=list)


@dataclass
class SafetyProof:
    safe: bool
    checked_properties: List[str]
    violations: List[SafetyViolation] = field(default_factory=list)


@dataclass
class PortalFSM:
    fsm_id: str
    initial_state: str
    states: Dict[str, FSMState] = field(default_factory=dict)
    transitions: List[FSMTransition] = field(default_factory=list)

    def add_state(self, state: FSMState) -> None:
        self.states[state.state_id] = state

    def add_transition(self, tr: FSMTransition) -> None:
        self.transitions.append(tr)

    @classmethod
    def compile_from_traces(
        cls,
        fsm_id: str,
        traces: Sequence[ShadowTrace],
        *,
        required_consent_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> "PortalFSM":
        """Induce a typed 3-state (ENTRY -> STAGED -> VERIFIED_COMMIT) FSM from shadow traces."""
        if not traces:
            raise ValueError("compile_from_traces requires at least one ShadowTrace")
        origin = traces[0].portal_origin
        readback = traces[0].readback_url
        fields = tuple(sorted({k for t in traces for k in t.submitted_fields.keys()}))

        fsm = cls(fsm_id=fsm_id, initial_state="ENTRY")
        fsm.add_state(FSMState("ENTRY", origin, origin, ("form_mounted",)))
        fsm.add_state(FSMState("STAGED", origin, origin, ("fields_staged", "zero_dirty_mismatch")))
        fsm.add_state(FSMState("VERIFIED_COMMIT", origin, readback, ("independent_readback_verified",)))
        fsm.add_transition(
            FSMTransition(
                transition_id="stage_form",
                from_state="ENTRY",
                to_state="STAGED",
                action_label="stage_form",
                http_method="NONE",
                is_commit=False,
                bound_fields=fields,
            )
        )
        fsm.add_transition(
            FSMTransition(
                transition_id="commit_and_readback",
                from_state="STAGED",
                to_state="VERIFIED_COMMIT",
                action_label="submit",
                http_method="POST",
                is_commit=True,
                idempotency_key=idempotency_key,
                required_consent_scope=required_consent_scope,
                bound_fields=fields,
            )
        )
        return fsm


def verify_fsm_safety(
    fsm: PortalFSM,
    *,
    ledger: Optional[ObligationLedger] = None,
    planned_bindings: Optional[Dict[str, str]] = None,
    opaque_fields: Optional[Sequence[str]] = None,
    active_subject_key: Optional[str] = None,
    literal_subjects: Optional[Dict[str, str]] = None,
    max_depth: int = 12,
) -> SafetyProof:
    """Statically verify No Double-Commit, Consent Dominance, and Subject Isolation."""
    violations: List[SafetyViolation] = []
    checked = ["no_double_commit", "consent_dominance", "subject_isolation"]

    adj: Dict[str, List[FSMTransition]] = {}
    for tr in fsm.transitions:
        adj.setdefault(tr.from_state, []).append(tr)

    # Bounded DFS over all reachable paths up to max_depth to detect double-commit cycles
    stack: List[Tuple[str, List[str], Set[str]]] = [(fsm.initial_state, [], set())]
    while stack:
        state_id, path, committed_without_key = stack.pop()
        if len(path) >= max_depth:
            continue
        for tr in adj.get(state_id, []):
            next_path = [*path, tr.transition_id]
            next_committed = set(committed_without_key)
            if tr.is_commit:
                if tr.transition_id in committed_without_key and not tr.idempotency_key:
                    violations.append(
                        SafetyViolation(
                            property_name="no_double_commit",
                            detail=f"Commit edge {tr.transition_id!r} reachable twice without idempotency_key",
                            path=next_path,
                        )
                    )
                    continue
                if not tr.idempotency_key:
                    next_committed.add(tr.transition_id)
                if tr.required_consent_scope:
                    if ledger is None or not ledger.has_consent(tr.required_consent_scope):
                        violations.append(
                            SafetyViolation(
                                property_name="consent_dominance",
                                detail=(
                                    f"Commit edge {tr.transition_id!r} requires consent scope "
                                    f"{tr.required_consent_scope!r}"
                                ),
                                path=next_path,
                            )
                        )
            # Avoid infinite non-commit self-loops while still exploring commit re-entries
            if next_path.count(tr.transition_id) <= 2:
                stack.append((tr.to_state, next_path, next_committed))

    # Property 3: Subject isolation + SSA provenance check on planned commit bindings
    if planned_bindings and opaque_fields:
        if ledger is not None:
            for pv in ledger.verify_ssa_provenance(planned_bindings, opaque_fields):
                violations.append(
                    SafetyViolation(
                        property_name="subject_isolation",
                        detail=f"Field {pv.field_name!r} failed SSA provenance: {pv.reason}",
                    )
                )
        if active_subject_key and literal_subjects:
            for fname in opaque_fields:
                if fname in planned_bindings:
                    owner = literal_subjects.get(fname)
                    if owner is not None and owner != active_subject_key:
                        violations.append(
                            SafetyViolation(
                                property_name="subject_isolation",
                                detail=(
                                    f"Cross-subject taint on {fname!r}: belongs to {owner!r}, "
                                    f"active subject is {active_subject_key!r}"
                                ),
                            )
                        )

    return SafetyProof(safe=not violations, checked_properties=checked, violations=violations)


def _norm_policy_str(value: str) -> str:
    return " ".join(str(value).strip().split()).casefold()


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    conditions: Dict[str, str]
    required_disposition: str


@dataclass(frozen=True)
class PolicyDecisionTable:
    table_id: str
    rules: Sequence[PolicyRule]
    require_human_if_unmatched: bool = True


@dataclass(frozen=True)
class PolicyDecisionResult:
    allowed: bool
    reason: str
    matched_rule_id: Optional[str] = None
    expected_disposition: Optional[str] = None
    requires_prepare: bool = False


_POLICY_CMP_RE = re.compile(r"^(<=|>=|!=|<|>)\s*(-?\d+(?:\.\d+)?)$")


def _policy_value_matches(actual: str, expected: str) -> bool:
    if expected == "*":
        return bool(actual)
    if actual == expected:
        return True
    m = _POLICY_CMP_RE.match(expected)
    if m:
        op, rhs = m.group(1), float(m.group(2))
        num_m = re.search(r"-?\d+(?:\.\d+)?", actual.replace(",", ""))
        if num_m:
            lhs = float(num_m.group(0))
            return {"<": lhs < rhs, "<=": lhs <= rhs, ">": lhs > rhs, ">=": lhs >= rhs, "!=": lhs != rhs}[op]
    return False


def evaluate_policy_decision(
    table: PolicyDecisionTable, observed_facts: Mapping[str, str], proposed_disposition: str
) -> PolicyDecisionResult:
    """Evaluate a proposed disposition against a declarative policy routing table."""
    norm_facts = {_norm_policy_str(k): _norm_policy_str(v) for k, v in observed_facts.items()}
    norm_proposed = _norm_policy_str(proposed_disposition)
    for rule in table.rules:
        if all(
            (nk := _norm_policy_str(ck)) in norm_facts
            and _policy_value_matches(norm_facts[nk], _norm_policy_str(cv))
            for ck, cv in rule.conditions.items()
        ):
            if norm_proposed != _norm_policy_str(rule.required_disposition):
                return PolicyDecisionResult(
                    False, "policy_disposition_mismatch", rule.rule_id, rule.required_disposition
                )
            return PolicyDecisionResult(True, "policy_rule_matched", rule.rule_id, rule.required_disposition)
    if table.require_human_if_unmatched:
        return PolicyDecisionResult(False, "uncodified_judgment_requires_prepare", requires_prepare=True)
    return PolicyDecisionResult(True, "no_matching_policy_rule_allowed")


PreStepAction = Literal["ALLOW", "PREPARE_ASK", "REFUSE", "VETO"]


@dataclass(frozen=True)
class PreStepPolicyResult:
    allowed: bool
    action: PreStepAction
    reason: str
    missing_params: Tuple[str, ...] = ()
    detail: str = ""


def _url_matches_pattern(url: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(url, pattern):
        return True
    try:
        return bool(re.search(pattern, url))
    except re.error:
        return False


def _lookup_dossier_param(mapping: Mapping[str, str], key: str) -> Optional[str]:
    clean = key.lstrip("$")
    for cand in (key, clean, f"${clean}"):
        if cand in mapping:
            return mapping[cand]
    return None


def evaluate_pre_step_policy(
    *,
    stage: Literal["step_0", "pre_navigation", "pre_fill"],
    required_params: Sequence[str] = (),
    dossier: Optional[Mapping[str, str]] = None,
    policy_table: Optional[PolicyDecisionTable] = None,
    observed_facts: Optional[Mapping[str, str]] = None,
    task_disposition: Optional[str] = None,
    target_url: str = "",
    allow_patterns: Sequence[str] = (),
    deny_patterns: Sequence[str] = (),
    field_name: str = "",
    proposed_value: str = "",
    field_type: FieldType = "casefold_ws",
    grounded_values: Optional[Mapping[str, str]] = None,
) -> PreStepPolicyResult:
    """Enforce Step-0 param/contradiction checks, pre-navigation access control, and pre-fill grounding."""
    dos = dossier or {}
    if stage == "step_0":
        missing = tuple(p for p in required_params if not str(_lookup_dossier_param(dos, p) or "").strip())
        if missing:
            return PreStepPolicyResult(False, "PREPARE_ASK", "missing_required_params", missing, f"Missing: {list(missing)}")
        if policy_table is not None and task_disposition:
            res = evaluate_policy_decision(policy_table, observed_facts or {}, task_disposition)
            if not res.allowed and res.reason == "policy_disposition_mismatch":
                return PreStepPolicyResult(
                    False, "REFUSE", "policy_contradiction",
                    detail=f"Disposition {task_disposition!r} conflicts with {res.matched_rule_id!r} ({res.expected_disposition!r})",
                )
            if not res.allowed and res.requires_prepare:
                return PreStepPolicyResult(False, "PREPARE_ASK", res.reason, detail="Uncodified policy judgment")
        return PreStepPolicyResult(True, "ALLOW", "step_0_passed")

    if stage == "pre_navigation":
        url = (target_url or "").strip()
        if any(_url_matches_pattern(url, pat) for pat in deny_patterns):
            return PreStepPolicyResult(False, "REFUSE", "access_denied_by_denylist", detail=f"Blocked: {url!r}")
        if allow_patterns and not any(_url_matches_pattern(url, pat) for pat in allow_patterns):
            return PreStepPolicyResult(False, "REFUSE", "access_denied_outside_allowlist", detail=f"Disallowed: {url!r}")
        return PreStepPolicyResult(True, "ALLOW", "navigation_allowed")

    # stage == "pre_fill"
    from openadapt_kernel import canonical as _can

    val = str(proposed_value or "")
    if not val.strip():
        return PreStepPolicyResult(False, "VETO", "empty_proposed_value", detail=f"Empty fill for {field_name!r}")
    sources: Dict[str, str] = {**dos, **(grounded_values or {})}
    norm_val = _can.normalize_casefold_ws(val)
    for src_val in sources.values():
        if not src_val:
            continue
        if _can.canonical_equal(src_val, val, field_type):
            return PreStepPolicyResult(True, "ALLOW", "grounded_value_verified")
        if field_type == "casefold_ws" and len(norm_val) >= 3:
            norm_src = _can.normalize_casefold_ws(src_val)
            if norm_val == norm_src or re.search(rf"(?<![a-z0-9]){re.escape(norm_val)}(?![a-z0-9])", norm_src):
                return PreStepPolicyResult(True, "ALLOW", "grounded_value_verified")
    return PreStepPolicyResult(False, "VETO", "ungrounded_fill_value", detail=f"Ungrounded value {val!r} for {field_name!r}")


_NL_WHEN_THEN_RE = re.compile(r"(?:when|if)\s+(.+?)\s+then\s+([A-Za-z0-9_\- ]+)", re.IGNORECASE)
_NL_KV_PAIR_RE = re.compile(r"([A-Za-z0-9_]+)\s*(?:=|is)\s*([A-Za-z0-9_*.-]+)", re.IGNORECASE)
_NL_ASK_PARAM_RE = re.compile(
    r"\bask\s+(?:the\s+)?user\s+(?:for|to\s+provide|to\s+confirm)\s+(?:the\s+)?([A-Za-z0-9_ -]{2,40}?)(?=\s+(?:before|prior|if|when|to\s+[a-z])|[.,;]|$)",
    re.IGNORECASE,
)
_NL_SLASH_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])(/[A-Za-z0-9_./-]+)")
_NL_ADMIN_AREA_RE = re.compile(r"\b(admin(?:\s+settings|\s+panel|\s+console)?|billing\s+settings|system\s+settings)\b", re.IGNORECASE)
_NL_PROHIBITED_VERB_RE = re.compile(
    r"\b(?:never|do\s+not|must\s+not|cannot|forbid(?:den)?\s+to)\s+(delete|remove|purge|modify|edit|change|overwrite|cancel|transfer|submit|approve|refund|close|access|navigate\s+to)\s+(?:any\s+|the\s+|a\s+)?([A-Za-z0-9_ /-]{2,40}?)(?=[.,;]|\s+(?:without|unless|before)|$)",
    re.IGNORECASE,
)


def _slug_param(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def compile_policy_rules_to_table(
    policy_specs: Sequence[Mapping[str, Any]],
    *,
    table_id: str = "compiled_step0_policy",
    require_human_if_unmatched: bool = True,
    llm_compiler_fn: Optional[Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]]] = None,
    enable_english_regex_heuristics: bool = True,
    require_llm_span_anchors: bool = False,
    source_texts: Optional[Mapping[str, str]] = None,
) -> Tuple[PolicyDecisionTable, Dict[str, Any]]:
    """Compile structured or natural-language policy specs into a PolicyDecisionTable and PreStepPolicyGate config."""
    from openadapt_kernel.ledger import SpanAnchor, verify_span_anchor

    rules: List[PolicyRule] = []
    seen_rule_ids: Set[str] = set()
    required_params: List[str] = []
    allow_patterns: List[str] = []
    deny_patterns: List[str] = []
    ask_user_triggers: List[str] = []
    irreversible_actions: List[str] = []
    require_human = require_human_if_unmatched
    grounding_required = False
    anchor_corpus: Dict[str, str] = dict(source_texts or {})

    def _extend_unique(dst: List[str], items: Any) -> None:
        if isinstance(items, str):
            items = [items]
        for item in items or ():
            s = str(item).strip()
            if s and s not in dst:
                dst.append(s)

    def _add_rule(rule: PolicyRule) -> None:
        if rule.rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule.rule_id)
            rules.append(rule)

    for idx, spec in enumerate(policy_specs):
        kind = str(
            spec.get("policy_template_id") or spec.get("category") or spec.get("kind") or spec.get("type") or ""
        ).strip().lower()
        rule_id = str(spec.get("rule_id") or spec.get("id") or f"compiled_rule_{idx}")
        desc = str(spec.get("description") or spec.get("rule_text") or spec.get("text") or "").strip()
        if desc:
            anchor_corpus.setdefault(f"spec_{idx}", desc)
            anchor_corpus.setdefault(rule_id, desc)

        if kind in ("missing_params", "required_params"):
            _extend_unique(required_params, spec.get("params") or spec.get("fields") or spec.get("required_params"))
            if enable_english_regex_heuristics:
                for m in _NL_ASK_PARAM_RE.finditer(desc):
                    slug = _slug_param(m.group(1))
                    if slug:
                        _extend_unique(required_params, slug)
                        _extend_unique(ask_user_triggers, slug)
        elif kind == "access_management":
            _extend_unique(allow_patterns, spec.get("allow_patterns") or spec.get("allow"))
            _extend_unique(deny_patterns, spec.get("deny_patterns") or spec.get("deny"))
            for m in _NL_SLASH_PATH_RE.finditer(desc):
                _extend_unique(deny_patterns, f"*{m.group(1)}*")
            if enable_english_regex_heuristics:
                for m in _NL_ADMIN_AREA_RE.finditer(desc):
                    head = m.group(1).strip().lower().split()[0]
                    _extend_unique(deny_patterns, f"*{head}*")
        elif kind == "ask_the_user":
            require_human = True
            _extend_unique(ask_user_triggers, spec.get("triggers") or spec.get("condition"))
            if enable_english_regex_heuristics:
                for m in _NL_ASK_PARAM_RE.finditer(desc):
                    slug = _slug_param(m.group(1))
                    if slug:
                        _extend_unique(required_params, slug)
                        _extend_unique(ask_user_triggers, slug)
            if desc and not ask_user_triggers:
                _extend_unique(ask_user_triggers, desc)
            conds = spec.get("conditions") or spec.get("when")
            if isinstance(conds, Mapping) and conds:
                _add_rule(PolicyRule(rule_id, {str(k): str(v) for k, v in conds.items()}, "ask_user"))
        elif kind == "irreversible_actions":
            require_human = True
            _extend_unique(irreversible_actions, spec.get("actions") or spec.get("targets"))
            conds = spec.get("conditions") or spec.get("when")
            if isinstance(conds, Mapping) and conds:
                _add_rule(
                    PolicyRule(
                        rule_id,
                        {str(k): str(v) for k, v in conds.items()},
                        str(spec.get("required_disposition") or spec.get("then") or "require_human_confirmation"),
                    )
                )
            elif desc:
                _extend_unique(irreversible_actions, desc)
        elif kind == "hallucinated_information":
            grounding_required = True
        else:
            conds = spec.get("conditions") or spec.get("when")
            disp = spec.get("required_disposition") or spec.get("then")
            if isinstance(conds, Mapping) and conds and disp:
                _add_rule(PolicyRule(rule_id, {str(k): str(v) for k, v in conds.items()}, str(disp).strip()))
            elif desc and enable_english_regex_heuristics:
                wt_m = _NL_WHEN_THEN_RE.search(desc)
                if wt_m:
                    parsed_conds = {k.strip(): v.strip() for k, v in _NL_KV_PAIR_RE.findall(wt_m.group(1))}
                    if parsed_conds:
                        _add_rule(PolicyRule(rule_id, parsed_conds, wt_m.group(2).strip().rstrip(".")))

        if enable_english_regex_heuristics and desc and not (isinstance(spec.get("conditions") or spec.get("when"), Mapping)):
            for p_idx, pm in enumerate(_NL_PROHIBITED_VERB_RE.finditer(desc)):
                verb = _slug_param(pm.group(1))
                target = _slug_param(pm.group(2))
                if verb and target:
                    _add_rule(PolicyRule(f"{rule_id}_prohibit_{p_idx}", {"action": verb, "target": target}, "REFUSE"))
                    if kind == "irreversible_actions":
                        _extend_unique(irreversible_actions, f"{verb}:{target}")

        if kind not in ("missing_params", "required_params") and spec.get("required_params"):
            _extend_unique(required_params, spec.get("required_params"))
        if kind != "access_management":
            _extend_unique(allow_patterns, spec.get("allow_patterns"))
            _extend_unique(deny_patterns, spec.get("deny_patterns"))

    if llm_compiler_fn is not None:
        compiled = llm_compiler_fn(policy_specs) or {}
        _extend_unique(required_params, compiled.get("required_params"))
        _extend_unique(allow_patterns, compiled.get("allow_patterns"))
        _extend_unique(deny_patterns, compiled.get("deny_patterns"))
        _extend_unique(ask_user_triggers, compiled.get("ask_user_triggers"))
        _extend_unique(irreversible_actions, compiled.get("irreversible_actions"))
        for r_idx, raw_r in enumerate(compiled.get("rules") or ()):
            if isinstance(raw_r, PolicyRule):
                if not require_llm_span_anchors:
                    _add_rule(raw_r)
            elif isinstance(raw_r, Mapping):
                if require_llm_span_anchors:
                    raw_a = raw_r.get("span_anchor")
                    if isinstance(raw_a, SpanAnchor):
                        anch = raw_a
                    elif isinstance(raw_a, Mapping) and all(k in raw_a for k in ("source_key", "start", "end", "quote")):
                        anch = SpanAnchor(str(raw_a["source_key"]), int(raw_a["start"]), int(raw_a["end"]), str(raw_a["quote"]))
                    else:
                        continue
                    if not verify_span_anchor(anchor_corpus, anch):
                        continue
                c_map = raw_r.get("conditions") or raw_r.get("when")
                disp = raw_r.get("required_disposition") or raw_r.get("then")
                if isinstance(c_map, Mapping) and c_map and disp:
                    _add_rule(
                        PolicyRule(
                            str(raw_r.get("rule_id") or f"llm_rule_{r_idx}"),
                            {str(k): str(v) for k, v in c_map.items()},
                            str(disp).strip(),
                        )
                    )

    table = PolicyDecisionTable(table_id=table_id, rules=tuple(rules), require_human_if_unmatched=require_human)
    gate_config: Dict[str, Any] = {
        "policy_table_id": table_id,
        "required_params": required_params,
        "allow_patterns": allow_patterns,
        "deny_patterns": deny_patterns,
        "ask_user_triggers": ask_user_triggers,
        "irreversible_actions": irreversible_actions,
        "require_human_if_unmatched": require_human,
        "grounding_required": grounding_required,
    }
    return table, gate_config


def evaluate_reactive_policy_on_dossier_update(
    policy_table: PolicyDecisionTable,
    *,
    pre_step_config: Mapping[str, Any],
    updated_dossier: Mapping[str, str],
    proposed_action: str,
    target_url: str = "",
    target_value: str = "",
    field_name: str = "",
    field_type: FieldType = "casefold_ws",
) -> PreStepPolicyResult:
    """Re-evaluate compiled policy table and gate predicates mid-trajectory at zero LLM calls."""
    if target_url:
        nav_res = evaluate_pre_step_policy(
            stage="pre_navigation",
            target_url=target_url,
            allow_patterns=pre_step_config.get("allow_patterns") or (),
            deny_patterns=pre_step_config.get("deny_patterns") or (),
        )
        if not nav_res.allowed:
            return nav_res

    norm_action = _norm_policy_str(proposed_action)
    for irrev in pre_step_config.get("irreversible_actions") or ():
        n_irrev = _norm_policy_str(str(irrev))
        if n_irrev and (norm_action == n_irrev or n_irrev in norm_action or norm_action in n_irrev):
            return PreStepPolicyResult(
                False,
                "PREPARE_ASK",
                "irreversible_action_requires_confirmation",
                detail=f"Action {proposed_action!r} matches irreversible rule {irrev!r}",
            )

    norm_dossier = {_norm_policy_str(k): _norm_policy_str(v) for k, v in updated_dossier.items()}
    for trig in pre_step_config.get("ask_user_triggers") or ():
        n_trig = _norm_policy_str(str(trig))
        if n_trig and (
            n_trig in norm_action
            or norm_dossier.get(n_trig) in ("true", "yes", "1")
        ):
            return PreStepPolicyResult(
                False,
                "PREPARE_ASK",
                "ask_user_trigger_fired",
                detail=f"Mid-trajectory trigger fired: {trig!r}",
            )

    if policy_table.rules:
        facts = {**{str(k): str(v) for k, v in updated_dossier.items()}, "action": proposed_action}
        dec = evaluate_policy_decision(policy_table, facts, proposed_action)
        if not dec.allowed and dec.reason == "policy_disposition_mismatch":
            return PreStepPolicyResult(
                False,
                "REFUSE",
                "reactive_policy_contradiction",
                detail=f"Action {proposed_action!r} conflicts with {dec.matched_rule_id!r} (expected {dec.expected_disposition!r})",
            )
        if not dec.allowed and dec.requires_prepare:
            return PreStepPolicyResult(
                False,
                "PREPARE_ASK",
                dec.reason,
                detail="Uncodified mid-trajectory facts require PREPARE",
            )

    if target_value and pre_step_config.get("grounding_required", True):
        fill_res = evaluate_pre_step_policy(
            stage="pre_fill",
            field_name=field_name or proposed_action,
            proposed_value=target_value,
            field_type=field_type,
            dossier=updated_dossier,
        )
        if not fill_res.allowed:
            return fill_res

    return PreStepPolicyResult(True, "ALLOW", "reactive_policy_passed")
