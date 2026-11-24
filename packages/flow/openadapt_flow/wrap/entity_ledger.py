"""Cross-tab EntityLedger and CheckpointEnforcer for ``openadapt wrap``.

:class:`EntityLedger` records entities observed on source surfaces, staged
writes on destination surfaces, and post-commit confirmation outputs across
tabs/surfaces during a wrapped agent run.

:class:`CheckpointEnforcer` evaluates a compiled :class:`CheckpointSpec`
against an :class:`EntityLedger` before and after each irreversible commit:

- Ignores pixel coordinates, viewports, selectors, and navigation order so
  frontier agents can adapt to UI layout changes.
- Blocks commits when required source reads were skipped, cross-tab entity
  values do not match, or prior confirmation writebacks are missing.
- Escalates or blocks when policy thresholds are exceeded without operator
  approval, or when required anchor/policy fields are missing (stopping on
  uncertainty).
- Inspects post-commit responses so failed or unconfirmed writes never pass
  silently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openadapt_flow.wrap.checkpoint_spec import (
    CheckpointSpec,
    normalize_scalar,
    values_match,
)

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"
DECISION_ESCALATE = "escalate"

OUTCOME_COMPLETE = "complete"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ESCALATED = "escalated"
OUTCOME_INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class CheckpointViolation:
    """A single checkpoint failure detected during enforcement."""

    step_index: int
    checkpoint_id: str
    code: str
    action: str  # DECISION_BLOCK | DECISION_ESCALATE
    commit_action: str
    detail: str


@dataclass(frozen=True)
class CommitGateDecision:
    """Before-click decision for a single irreversible commit attempt."""

    commit_action: str
    decision: str  # DECISION_ALLOW | DECISION_BLOCK | DECISION_ESCALATE
    violations: tuple[CheckpointViolation, ...]


@dataclass(frozen=True)
class EnforcementReport:
    """Outcome of enforcing a CheckpointSpec over an agent trajectory."""

    trajectory_id: str
    outcome: str  # OUTCOME_COMPLETE | OUTCOME_BLOCKED | OUTCOME_ESCALATED | OUTCOME_INCOMPLETE
    commits_attempted: tuple[str, ...]
    commits_executed: tuple[str, ...]
    commits_blocked: tuple[str, ...]
    violations: tuple[CheckpointViolation, ...]
    checked_invariants: int
    checked_source_reads: int
    checked_policy_rules: int
    checked_confirmations: int


@dataclass(frozen=True)
class SuiteMetrics:
    """Aggregate metrics across a labeled suite of clean and failure trajectories."""

    total_trajectories: int
    clean_count: int
    bad_count: int
    clean_auto_completed: int
    clean_false_escalations: int
    bad_caught: int
    bad_silent_errors: int
    silent_error_rate: float
    auto_completion_rate: float
    escalation_precision: float


class EntityLedger:
    """Cross-surface entity ledger for an ``openadapt wrap`` session.

    Tracks what was read on each surface/section, what was staged for writing
    on each surface, and what confirmation fields were returned by prior
    commits. Carries no pixel coordinates or DOM selectors.
    """

    def __init__(self) -> None:
        self._reads: dict[tuple[str, str], Any] = {}
        self._sections: set[tuple[str, str]] = set()
        self._staged_writes: dict[str, dict[str, Any]] = {}
        self._commit_confirmations: dict[str, dict[str, Any]] = {}

    def record_read(
        self, surface: str, section: str, entities: Mapping[str, Any]
    ) -> None:
        """Record non-empty entity observations on ``(surface, section)``."""
        if not entities:
            return
        self._sections.add((surface, section))
        for field_name, value in entities.items():
            if value is not None and value != "" and value != []:
                self._reads[(surface, str(field_name))] = value

    def record_write(self, surface: str, field_name: str, value: Any) -> None:
        """Record a staged write, selection, attachment, or note on ``surface``."""
        self._staged_writes.setdefault(surface, {})[str(field_name)] = value

    def record_confirmation(
        self, commit_action: str, fields: Mapping[str, Any]
    ) -> None:
        """Record verified post-commit confirmation outputs for ``commit_action``."""
        self._commit_confirmations[commit_action] = dict(fields)

    def has_section(self, surface: str, section: str) -> bool:
        """Return True if ``(surface, section)`` has been observed."""
        return (surface, section) in self._sections

    def has_read(self, surface: str, field_name: str) -> bool:
        """Return True if ``(surface, field_name)`` has a non-empty observation."""
        return (surface, field_name) in self._reads

    def get_read(self, surface: str, field_name: str) -> Any:
        """Return the last observed value for ``(surface, field_name)``."""
        return self._reads.get((surface, field_name))

    def staged_payload(
        self, surface: str, explicit_payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Merge staged writes on ``surface`` with an explicit commit payload."""
        merged: dict[str, Any] = dict(self._staged_writes.get(surface, {}))
        if explicit_payload:
            merged.update(explicit_payload)
        return merged

    def get_staged_write(self, surface: str, field_name: str) -> Any:
        """Return the staged value for ``(surface, field_name)``, if any."""
        return self._staged_writes.get(surface, {}).get(field_name)

    def get_confirmation(self, commit_action: str) -> dict[str, Any] | None:
        """Return recorded confirmation fields for ``commit_action``, if any."""
        return self._commit_confirmations.get(commit_action)


def _policy_holds(value: Any, operator: str, threshold: Any) -> bool:
    if operator == "lte":
        try:
            return float(value) <= float(threshold)
        except (TypeError, ValueError):
            return False
    if operator == "gte":
        try:
            return float(value) >= float(threshold)
        except (TypeError, ValueError):
            return False
    if operator == "in":
        allowed = {normalize_scalar(item) for item in threshold}
        return normalize_scalar(value) in allowed
    return False


class CheckpointEnforcer:
    """Enforce a compiled :class:`CheckpointSpec` using an :class:`EntityLedger`."""

    def __init__(
        self,
        spec: CheckpointSpec,
        ledger: EntityLedger | None = None,
    ) -> None:
        self.spec = spec
        self.ledger = ledger if ledger is not None else EntityLedger()
        self.checked_invariants = 0
        self.checked_source_reads = 0
        self.checked_policy_rules = 0
        self.checked_confirmations = 0

    def observe_event(self, event: Mapping[str, Any]) -> None:
        """Update the ledger from a non-commit navigation, read, or write event."""
        surface = str(event.get("surface", "primary"))
        section = str(event.get("section", "default"))
        kind = str(event.get("kind", ""))

        read_entities = event.get("read_entities") or {}
        if isinstance(read_entities, Mapping) and read_entities:
            self.ledger.record_read(surface, section, read_entities)

        if kind in ("type", "attach", "note", "select"):
            fname = event.get("field_name") or event.get("field_label")
            fval = event.get("value") if "value" in event else event.get("text")
            if fname is not None and fval is not None:
                self.ledger.record_write(surface, str(fname), fval)

    def check_before_commit(
        self,
        *,
        step_index: int,
        surface: str,
        commit_action: str,
        payload: Mapping[str, Any] | None = None,
        human_approved: bool = False,
    ) -> CommitGateDecision:
        """Run all pre-commit checkpoints for ``commit_action`` before execution."""
        merged_payload = self.ledger.staged_payload(surface, payload)
        step_violations: list[CheckpointViolation] = []

        # 1. Required source reads
        for req in self.spec.required_source_reads:
            if req.before_commit != commit_action:
                continue
            self.checked_source_reads += 1
            if not self.ledger.has_section(req.surface, req.section):
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=req.id,
                        code="skipped_source_read",
                        action=DECISION_BLOCK,
                        commit_action=commit_action,
                        detail=(
                            f"Required source section {req.surface}.{req.section} "
                            f"was not read before {commit_action}."
                        ),
                    )
                )
                continue
            missing_fields = [
                f
                for f in req.required_fields
                if not self.ledger.has_read(req.surface, f)
            ]
            if missing_fields:
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=req.id,
                        code="missing_source_field",
                        action=DECISION_BLOCK,
                        commit_action=commit_action,
                        detail=(
                            f"Required fields {missing_fields} on "
                            f"{req.surface}.{req.section} were not observed before {commit_action}."
                        ),
                    )
                )

        # 2. Cross-tab entity consistency invariants
        for inv in self.spec.cross_tab_invariants:
            if inv.enforced_on_commit != commit_action:
                continue
            self.checked_invariants += 1
            if not self.ledger.has_read(inv.source_surface, inv.source_field):
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=inv.id,
                        code="missing_source_anchor",
                        action=DECISION_BLOCK,
                        commit_action=commit_action,
                        detail=(
                            f"Cannot match {inv.dest_surface}.{inv.dest_field}: "
                            f"source anchor {inv.source_surface}.{inv.source_field} was never read."
                        ),
                    )
                )
                continue
            dst_val = merged_payload.get(inv.dest_field)
            if dst_val in (None, "", []):
                if self.spec.stop_on_uncertainty:
                    step_violations.append(
                        CheckpointViolation(
                            step_index=step_index,
                            checkpoint_id=inv.id,
                            code="missing_dest_field",
                            action=DECISION_BLOCK,
                            commit_action=commit_action,
                            detail=(
                                f"Destination field {inv.dest_surface}.{inv.dest_field} "
                                f"is missing on {commit_action}; stopping on uncertainty."
                            ),
                        )
                    )
                continue
            src_val = self.ledger.get_read(inv.source_surface, inv.source_field)
            if not values_match(src_val, dst_val, inv.match_mode):
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=inv.id,
                        code="cross_tab_entity_mismatch",
                        action=DECISION_BLOCK,
                        commit_action=commit_action,
                        detail=(
                            f"Entity mismatch on {inv.dest_field} ({inv.match_mode}): "
                            f"{inv.source_surface}.{inv.source_field}={src_val!r} != "
                            f"{inv.dest_surface}.{inv.dest_field}={dst_val!r}."
                        ),
                    )
                )

        # 3. Prior confirmation writebacks required before this commit
        for conf in self.spec.confirmation_checks:
            if conf.writeback_before_commit != commit_action:
                continue
            self.checked_confirmations += 1
            prior_out = self.ledger.get_confirmation(conf.commit_action)
            if not prior_out:
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=conf.id,
                        code="missing_prior_confirmation",
                        action=DECISION_BLOCK,
                        commit_action=commit_action,
                        detail=(
                            f"Prior commit {conf.commit_action} has no confirmation "
                            f"before {commit_action}."
                        ),
                    )
                )
                continue
            if conf.writeback_surface and conf.writeback_field:
                wb_val = self.ledger.get_staged_write(
                    conf.writeback_surface, conf.writeback_field
                )
                if wb_val is None or str(wb_val).strip() == "":
                    step_violations.append(
                        CheckpointViolation(
                            step_index=step_index,
                            checkpoint_id=conf.id,
                            code="missing_confirmation_writeback",
                            action=DECISION_BLOCK,
                            commit_action=commit_action,
                            detail=(
                                f"Required writeback to "
                                f"{conf.writeback_surface}.{conf.writeback_field} "
                                f"is missing before {commit_action}."
                            ),
                        )
                    )
                else:
                    for rf in conf.required_response_fields:
                        expected_token = prior_out.get(rf)
                        if expected_token and not values_match(
                            expected_token, wb_val, "contains_ci"
                        ):
                            step_violations.append(
                                CheckpointViolation(
                                    step_index=step_index,
                                    checkpoint_id=conf.id,
                                    code="confirmation_writeback_mismatch",
                                    action=DECISION_BLOCK,
                                    commit_action=commit_action,
                                    detail=(
                                        f"Writeback {conf.writeback_surface}.{conf.writeback_field} "
                                        f"does not contain {rf}={expected_token!r}."
                                    ),
                                )
                            )

        # 4. Pre-commit policy and approval rules
        for rule in self.spec.approval_rules:
            if rule.commit_action != commit_action or rule.surface != surface:
                continue
            self.checked_policy_rules += 1
            raw_val = merged_payload.get(rule.field_name)
            if raw_val in (None, ""):
                if self.spec.stop_on_uncertainty and not human_approved:
                    step_violations.append(
                        CheckpointViolation(
                            step_index=step_index,
                            checkpoint_id=rule.id,
                            code="uncertain_policy_field",
                            action=DECISION_ESCALATE,
                            commit_action=commit_action,
                            detail=(
                                f"Policy field {rule.field_name} missing on {commit_action}; "
                                f"stopping on uncertainty."
                            ),
                        )
                    )
                continue
            if (
                not _policy_holds(raw_val, rule.operator, rule.threshold)
                and not human_approved
            ):
                step_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=rule.id,
                        code="policy_violation",
                        action=rule.on_violation,
                        commit_action=commit_action,
                        detail=(
                            f"Policy {rule.id} violated: {rule.field_name}={raw_val!r} "
                            f"failed {rule.operator} {rule.threshold!r} without approval."
                        ),
                    )
                )

        if step_violations:
            actions = {v.action for v in step_violations}
            decision = (
                DECISION_BLOCK if DECISION_BLOCK in actions else DECISION_ESCALATE
            )
            return CommitGateDecision(
                commit_action=commit_action,
                decision=decision,
                violations=tuple(step_violations),
            )

        return CommitGateDecision(
            commit_action=commit_action,
            decision=DECISION_ALLOW,
            violations=(),
        )

    def check_after_commit(
        self,
        *,
        step_index: int,
        surface: str,
        commit_action: str,
        response: Mapping[str, Any] | None,
    ) -> tuple[CheckpointViolation, ...]:
        """Inspect post-commit response and record confirmation fields in the ledger."""
        resp = response or {}
        post_violations: list[CheckpointViolation] = []
        for conf in self.spec.confirmation_checks:
            if conf.commit_action != commit_action or conf.surface != surface:
                continue
            self.checked_confirmations += 1
            status = resp.get("status")
            if status not in conf.expected_status_codes:
                post_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=conf.id,
                        code="confirmation_status_failed",
                        action=DECISION_ESCALATE,
                        commit_action=commit_action,
                        detail=(
                            f"Commit {commit_action} returned HTTP {status!r}, "
                            f"expected {conf.expected_status_codes}."
                        ),
                    )
                )
                continue
            missing_resp = [
                rf
                for rf in conf.required_response_fields
                if resp.get(rf) in (None, "", False)
            ]
            if missing_resp:
                post_violations.append(
                    CheckpointViolation(
                        step_index=step_index,
                        checkpoint_id=conf.id,
                        code="missing_confirmation_field",
                        action=DECISION_ESCALATE,
                        commit_action=commit_action,
                        detail=(
                            f"Commit {commit_action} response missing required "
                            f"confirmation fields {missing_resp}."
                        ),
                    )
                )
                continue
            self.ledger.record_confirmation(
                commit_action, {rf: resp[rf] for rf in conf.required_response_fields}
            )
        return tuple(post_violations)

    def enforce_trajectory(self, trajectory: Mapping[str, Any]) -> EnforcementReport:
        """Enforce ``self.spec`` across a complete recorded or replayed trajectory."""
        traj_id = str(trajectory.get("id", "unknown-trajectory"))
        events = list(trajectory.get("events") or [])

        commits_attempted: list[str] = []
        commits_executed: list[str] = []
        commits_blocked: list[str] = []
        violations: list[CheckpointViolation] = []

        for idx, ev in enumerate(events):
            step_i = int(ev.get("i", idx))
            surface = str(ev.get("surface", "primary"))
            kind = str(ev.get("kind", ""))

            self.observe_event(ev)

            if kind != "submit" and ev.get("action_type") != "irreversible_commit":
                continue

            commit_action = str(ev.get("commit_action", f"{surface}:submit"))
            commits_attempted.append(commit_action)
            payload = (
                ev.get("payload") if isinstance(ev.get("payload"), Mapping) else None
            )
            gate = self.check_before_commit(
                step_index=step_i,
                surface=surface,
                commit_action=commit_action,
                payload=payload,
                human_approved=bool(ev.get("human_approved", False)),
            )
            if gate.decision != DECISION_ALLOW:
                violations.extend(gate.violations)
                commits_blocked.append(commit_action)
                continue

            commits_executed.append(commit_action)
            response = (
                ev.get("response") if isinstance(ev.get("response"), Mapping) else None
            )
            post_violations = self.check_after_commit(
                step_index=step_i,
                surface=surface,
                commit_action=commit_action,
                response=response,
            )
            violations.extend(post_violations)

        if violations:
            actions = {v.action for v in violations}
            outcome = (
                OUTCOME_BLOCKED if DECISION_BLOCK in actions else OUTCOME_ESCALATED
            )
        elif set(self.spec.commit_sequence).issubset(set(commits_executed)) and bool(
            self.spec.commit_sequence
        ):
            outcome = OUTCOME_COMPLETE
        else:
            outcome = OUTCOME_INCOMPLETE

        return EnforcementReport(
            trajectory_id=traj_id,
            outcome=outcome,
            commits_attempted=tuple(commits_attempted),
            commits_executed=tuple(commits_executed),
            commits_blocked=tuple(commits_blocked),
            violations=tuple(violations),
            checked_invariants=self.checked_invariants,
            checked_source_reads=self.checked_source_reads,
            checked_policy_rules=self.checked_policy_rules,
            checked_confirmations=self.checked_confirmations,
        )


def evaluate_suite(
    spec: CheckpointSpec,
    labeled_trajectories: Sequence[Mapping[str, Any]],
) -> SuiteMetrics:
    """Evaluate a labeled suite of clean and failure trajectories against ``spec``."""
    clean_count = 0
    bad_count = 0
    clean_auto_completed = 0
    clean_false_escalations = 0
    bad_caught = 0
    bad_silent_errors = 0

    for item in labeled_trajectories:
        is_clean = bool(item["is_clean"])
        enforcer = CheckpointEnforcer(spec)
        report = enforcer.enforce_trajectory(item)

        if is_clean:
            clean_count += 1
            if report.outcome == OUTCOME_COMPLETE:
                clean_auto_completed += 1
            else:
                clean_false_escalations += 1
        else:
            bad_count += 1
            if report.outcome in (OUTCOME_BLOCKED, OUTCOME_ESCALATED):
                bad_caught += 1
            else:
                bad_silent_errors += 1

    total_escalations = bad_caught + clean_false_escalations
    silent_error_rate = (bad_silent_errors / bad_count) if bad_count else 0.0
    auto_completion_rate = (clean_auto_completed / clean_count) if clean_count else 0.0
    escalation_precision = (
        (bad_caught / total_escalations) if total_escalations else 1.0
    )

    return SuiteMetrics(
        total_trajectories=len(labeled_trajectories),
        clean_count=clean_count,
        bad_count=bad_count,
        clean_auto_completed=clean_auto_completed,
        clean_false_escalations=clean_false_escalations,
        bad_caught=bad_caught,
        bad_silent_errors=bad_silent_errors,
        silent_error_rate=silent_error_rate,
        auto_completion_rate=auto_completion_rate,
        escalation_precision=escalation_precision,
    )


__all__ = [
    "CheckpointEnforcer",
    "CheckpointViolation",
    "CommitGateDecision",
    "DECISION_ALLOW",
    "DECISION_BLOCK",
    "DECISION_ESCALATE",
    "EnforcementReport",
    "EntityLedger",
    "OUTCOME_BLOCKED",
    "OUTCOME_COMPLETE",
    "OUTCOME_ESCALATED",
    "OUTCOME_INCOMPLETE",
    "SuiteMetrics",
    "evaluate_suite",
]
