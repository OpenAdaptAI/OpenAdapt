"""Does the record check cover every value the item typed or selected?

Under "Only ask a person about problems", a save is allowed without a person
only when the deployment's system-of-record read-back will confirm the values
the item entered. This module answers that statically, from the workflow:

- every typing or selection step before a save must take its value from a
  declared input (``Step.param``). A literal typed from the recording, or a
  secret, is never covered;
- every input an API binding sends (a ``{param}`` placeholder in its URL,
  query, headers or body) counts as entered on that path, like a typed value;
- every such input must be bound in that save's effects, as a ``field_equals``
  value or a ``match`` / ``exact_new_set`` selector read from the system of
  record. An idempotency key alone doesn't cover an input;
- every executable actuation path of the save declares at least one effect,
  and no effect is an unbound placeholder or an on-screen read-back;
- nothing is typed or selected after the last save. No record check names a
  value entered after the last save, yet an application that saves each field
  on entry writes it anyway;
- no save can be skipped (a guard with ``on_unmet="skip"``). The windows
  above assume every save runs: a skipped save leaves its values in the form
  for the next save to write, under a record check that doesn't name them.

:func:`save_contract_problems` holds the two contract rules E2 sets for every
save, so the pre-save gate enforces them at run time too: every executable
path declares an ``exact_new_set`` (a create; updates need the field-delta
check, which isn't built), and carries an idempotency key. ``field_equals``
and ``record_written`` judge the record after the save without its state
before, so on their own they confirm an update whose value was already there,
or a record that already existed.

Program-graph workflows aren't covered in phase 1: their order of steps isn't
linear, so every save in them fails coverage and goes to a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow
    from openadapt_flow.runtime.effects.effect import Effect


@dataclass(frozen=True)
class SaveCoverage:
    """Coverage of one save. PHI-free: step ids and input names only."""

    step_id: str
    #: Closed reason codes; empty when the save is covered.
    problems: tuple[str, ...]
    #: Declared inputs entered before this save that its effects don't bind.
    uncovered_inputs: tuple[str, ...] = ()

    @property
    def covered(self) -> bool:
        return not self.problems


def _effect_read_params(effect: "Effect") -> set[str]:
    """Inputs an effect reads back from the system of record."""

    names: set[str] = set()
    for expr in effect.match.values():
        if expr.param is not None:
            names.add(expr.param)
    for selector in effect.new_records:
        for expr in selector.values():
            if expr.param is not None:
                names.add(expr.param)
    if effect.field is not None and effect.value is not None:
        if effect.value.param is not None:
            names.add(effect.value.param)
    if effect.readback is not None:
        for field in effect.readback.fields:
            if field.value.param is not None:
                names.add(field.value.param)
    return names


def _api_template_params(step: "Step") -> Optional[set[str]]:
    """Inputs an API binding substitutes into its request, or None if unreadable.

    The actuator fills templates with ``str.format_map``, so a placeholder's
    input is the root of its field name (``{note}``, ``{note[0]}``,
    ``{note.upper}``), including fields nested in a format spec.
    """

    import string

    binding = step.api_binding
    if binding is None:
        return set()
    names: set[str] = set()
    formatter = string.Formatter()

    def scan(template: str) -> None:
        for _literal, field, spec, _conversion in formatter.parse(template):
            if field:
                root = field.split(".", 1)[0].split("[", 1)[0]
                if root and not root.isdigit():
                    names.add(root)
            if spec:
                scan(spec)

    def walk(node: object) -> None:
        if isinstance(node, str):
            scan(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    try:
        scan(binding.url_template)
        for value in binding.query.values():
            scan(value)
        for value in binding.headers.values():
            scan(value)
        walk(binding.body_template)
    except ValueError:
        return None
    return names


def _entry_steps(step: "Step") -> bool:
    from openadapt_flow.ir import ActionKind

    return step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)


def save_coverage(
    workflow: "Workflow",
    save: "Step",
    *,
    is_save: Optional[Callable[["Step"], bool]] = None,
) -> SaveCoverage:
    """Return the coverage of one save step in a linear workflow.

    ``is_save`` decides which earlier steps are saves (the window of entries
    for this save starts after the previous save). It defaults to the run
    gate's fail-closed consequential-step classifier.
    """

    from openadapt_flow.policy import iter_effect_paths

    problems: list[str] = []
    if workflow.program is not None:
        return SaveCoverage(step_id=save.id, problems=("program_graph",))
    if is_save is None:
        from openadapt_flow.run_gate import is_consequential

        def is_save(candidate: "Step") -> bool:
            return is_consequential(candidate, workflow)

    try:
        index = next(i for i, s in enumerate(workflow.steps) if s.id == save.id)
    except StopIteration:
        return SaveCoverage(step_id=save.id, problems=("save_not_in_workflow",))

    paths = list(iter_effect_paths(save))
    if not paths or any(not effects for _path, effects in paths):
        problems.append("no_record_check")
    path_reads: list[set[str]] = []
    uncovered: set[str] = set()
    for path, effects in paths:
        reads: set[str] = set()
        for effect in effects:
            if effect.needs_operator_confirmation:
                problems.append("placeholder_effect")
            if effect.readback is not None:
                problems.append("onscreen_readback")
            reads |= _effect_read_params(effect)
        path_reads.append(reads)
        if path == "api":
            # The API path enters its values through the request, not the
            # screen: each one must be read back on that path.
            sent = _api_template_params(save)
            if sent is None:
                problems.append("api_template_unreadable")
            else:
                uncovered |= sent - reads

    if any(
        is_save(step) and step.guard is not None and step.guard.on_unmet == "skip"
        for step in workflow.steps
    ):
        problems.append("save_can_be_skipped")

    entered: list[str] = []
    window_start = 0
    for earlier in range(index - 1, -1, -1):
        if is_save(workflow.steps[earlier]):
            window_start = earlier + 1
            break
    for step in [*workflow.steps[window_start:index], save]:
        if not _entry_steps(step):
            continue
        if step.secret:
            problems.append("secret_entry")
            continue
        if step.param is None:
            problems.append("recorded_value_entry")
            continue
        entered.append(step.param)

    uncovered |= {name for name in entered for reads in path_reads if name not in reads}
    if uncovered:
        problems.append("input_not_read_back")
    if not any(is_save(later) for later in workflow.steps[index + 1 :]):
        # The last save: anything typed or selected after it is saved, if at
        # all, by something no record check reads.
        for later in workflow.steps[index + 1 :]:
            if _entry_steps(later):
                problems.append("entry_after_last_save")
                if later.param is not None and not later.secret:
                    uncovered.add(later.param)
    # Stable, de-duplicated reason codes.
    seen: list[str] = []
    for problem in problems:
        if problem not in seen:
            seen.append(problem)
    return SaveCoverage(
        step_id=save.id,
        problems=tuple(seen),
        uncovered_inputs=tuple(sorted(uncovered)),
    )


def save_contract_problems(save: "Step") -> tuple[str, ...]:
    """E2's contract rules for one save, as closed reason codes.

    - ``update_needs_field_delta_check``: an executable path declares no
      ``exact_new_set``. Until the field-delta check exists, only a create
      whose added record set is compared with the state before the save can
      finish without a person.
    - ``write_without_idempotency_key``: an executable path carries no
      idempotency key (the ``clinical-write`` rule).
    """

    from openadapt_flow.policy import effect_has_idempotency_key, iter_effect_paths
    from openadapt_flow.runtime.effects.effect import EffectKind

    problems: list[str] = []
    paths = list(iter_effect_paths(save))
    if not paths or not all(
        any(effect.kind is EffectKind.EXACT_NEW_SET for effect in effects)
        for _path, effects in paths
    ):
        problems.append("update_needs_field_delta_check")
    if not effect_has_idempotency_key(save):
        problems.append("write_without_idempotency_key")
    return tuple(problems)


def workflow_coverage(workflow: "Workflow") -> list[SaveCoverage]:
    """Coverage of every save in a workflow, in step order."""

    from openadapt_flow.run_gate import is_consequential

    if workflow.program is not None:
        return [SaveCoverage(step_id="<program>", problems=("program_graph",))]
    return [
        save_coverage(workflow, step)
        for step in workflow.steps
        if is_consequential(step, workflow)
    ]
