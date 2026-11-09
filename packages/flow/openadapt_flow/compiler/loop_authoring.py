"""Author a data-driven LOOP program from a single-demonstration linear body.

The AUTHORING WIRE the runtime has been waiting for. The Phase-2 interpreter
already executes a ``LOOP`` state over a worklist safely -- bounded, ``$0``,
zero-model, identity-gated and effect-verified PER ITERATION, halt-on-ambiguity
(``runtime.replayer._exec_loop_state`` / ``_interpret_program``). What has been
missing is the compile-time step that turns "one demonstration of one row" into
a ``ProgramGraph`` whose single ``LOOP`` iterates that demonstrated body once
per record of a declared worklist.

This module is that step, and NOTHING more: it reuses ``lift_to_program`` to
scaffold the demonstrated linear body as the loop's per-row subflow, wraps it in
one ``LOOP`` state over a ``Relation`` (``LoopSpec``/``Relation`` exactly as the
interpreter expects), and binds each worklist record's columns to the body's
:class:`~openadapt_flow.ir.ParamSpec` slots. It invents NO new runtime, NO new
safety surface, and NO new IR -- every governance guarantee (per-iteration
identity re-resolution, per-iteration effect verification, ``max_iterations``
bounding, fail-safe HALT on ambiguity) rides along on the already-built,
already-tested interpreter because the emitted graph is the exact shape the
interpreter's loop tests hand-author today.

The column -> parameter mapping is EXPLICIT and VALIDATED: a column that maps to
no parameter, a parameter with no column and no demo default, a mapping onto a
secret, or a worklist longer than the loop bound all FAIL LOUDLY at authoring
time (:class:`LoopAuthoringError`) rather than emitting a silently demo-bound or
under-bound bundle that would only surface at run time.
"""

from __future__ import annotations

from typing import Optional

from openadapt_flow.ir import (
    LoopSpec,
    ProgramGraph,
    Relation,
    State,
    StateKind,
    Transition,
    Workflow,
    lift_to_program,
)

#: Default names for the emitted graph's single relation / body subflow / loop.
DEFAULT_RELATION = "worklist"
DEFAULT_BODY_ID = "loop_body"
LOOP_STATE_ID = "for_each"
DONE_STATE_ID = "__loop_done__"


class LoopAuthoringError(ValueError):
    """A worklist / parameter-mapping mismatch.

    Raised at AUTHORING time so a demo-bound or under-bound loop can never be
    compiled into a bundle that would only fail (or, worse, silently mis-bind)
    at run time. Mirrors the compiler's fail-loud posture on param leakage.
    """


def body_param_names(body: Workflow) -> set[str]:
    """The parameter names the demonstrated body BINDS via ``Step.param``.

    These are the slots a worklist row fills per iteration. Secret params are
    excluded: a secret's value is injected from ``OPENADAPT_FLOW_SECRET_<PARAM>``
    at replay and must NEVER travel in a worklist.
    """
    names: set[str] = set()
    for step in body.steps:
        if step.param and not step.secret:
            names.add(step.param)
    return names


def worklist_columns(records: list[dict[str, str]]) -> list[str]:
    """The (uniform) column set of a worklist, validated non-ragged.

    Every record must share the same keys; a ragged worklist (a row missing a
    column another row has) is a data error we refuse rather than paper over
    with an implicit empty binding.
    """
    if not records:
        return []
    first = set(records[0])
    for i, rec in enumerate(records[1:], start=1):
        if set(rec) != first:
            missing = sorted(first - set(rec))
            extra = sorted(set(rec) - first)
            raise LoopAuthoringError(
                f"worklist record {i} has a different column set than record 0 "
                f"(missing {missing}, extra {extra}); every record must share "
                "the same columns"
            )
    return sorted(first)


def resolve_column_map(
    columns: list[str],
    body: Workflow,
    column_map: Optional[dict[str, str]],
) -> dict[str, str]:
    """Validate and resolve the worklist-column -> body-parameter mapping.

    ``column_map`` maps each worklist column name to a workflow parameter name.
    When ``None``, an IDENTITY map is assumed (each column binds the parameter
    of the same name). Either way the result is validated exhaustively; any
    mismatch raises :class:`LoopAuthoringError` listing EVERY problem at once.
    """
    param_specs = body.param_specs
    secret = set(body.secret_params or [])
    bound = body_param_names(body)
    known_params = set(param_specs) | bound

    effective = dict(column_map) if column_map else {c: c for c in columns}

    errors: list[str] = []

    # 1. Every mapped source column must exist in the worklist header.
    for col in effective:
        if col not in columns:
            errors.append(
                f"mapping references column '{col}' not present in the worklist "
                f"header {columns}"
            )
    # 2. Every worklist column must be mapped -- no silently dropped data.
    for col in columns:
        if col not in effective:
            errors.append(
                f"worklist column '{col}' is not mapped to any workflow "
                f"parameter (add a '{col}=<param>' mapping or drop the column)"
            )
    # 3. Every target parameter must be a real, non-secret workflow parameter.
    for col, param in effective.items():
        if param in secret:
            errors.append(
                f"column '{col}' maps to SECRET parameter '{param}'; secrets are "
                "injected from the environment at replay, never from a worklist"
            )
        elif param not in known_params:
            errors.append(
                f"column '{col}' maps to unknown workflow parameter '{param}' "
                f"(known parameters: {sorted(known_params)})"
            )
    # 4. Every parameter the body BINDS must be filled per row OR carry a demo
    #    default; otherwise the run would HALT on a missing required param.
    covered = set(effective.values())
    for pname in sorted(bound):
        if pname in covered:
            continue
        spec = param_specs.get(pname)
        if spec is None or spec.example is None:
            errors.append(
                f"workflow parameter '{pname}' is bound by the demonstrated body "
                "but is neither supplied by the worklist nor carries a demo "
                f"default; add a column that maps to '{pname}'"
            )

    if errors:
        joined = "\n  - ".join(errors)
        raise LoopAuthoringError(
            "cannot author data-driven loop -- worklist/parameter mismatch:\n  - "
            + joined
        )
    return effective


def author_data_driven_loop(
    body: Workflow,
    records: list[dict[str, str]],
    *,
    column_map: Optional[dict[str, str]] = None,
    relation: str = DEFAULT_RELATION,
    max_iterations: int = 1000,
    loop_var: str = "",
    name: Optional[str] = None,
) -> Workflow:
    """Wrap a demonstrated linear ``body`` in a ``LOOP`` over ``records``.

    Returns a NEW :class:`~openadapt_flow.ir.Workflow` whose ``program`` is a
    single ``LOOP`` state iterating a ``Relation`` built from ``records`` (each
    record's columns remapped to the body's parameters), whose ``body`` subflow
    is the mechanical lift of the demonstrated linear steps. The interpreter
    runs that body once per record with the record merged into the run params --
    so every existing safety gate fires per iteration, unchanged.

    Args:
        body: A single-demonstration compiled workflow (the linear body). Its
            ``steps``, ``param_specs``, ``secret_params`` and templates are
            preserved; only ``program`` / ``subflows`` / ``data_sources`` /
            ``name`` are added.
        records: The worklist -- one dict per record, keyed by worklist column.
        column_map: Optional worklist-column -> body-parameter mapping; identity
            when omitted. Validated by :func:`resolve_column_map`.
        relation: Name of the emitted worklist relation.
        max_iterations: Hard fail-safe bound on iterations (the runtime HALTs a
            worklist longer than this rather than running unbounded).
        loop_var: Optional human label for the loop variable (reports only).
        name: Name for the looped workflow (default: ``"<body>-for-each"``).

    Raises:
        LoopAuthoringError: on an empty body, an empty worklist, a ragged
            worklist, a column/parameter mismatch, or a worklist that already
            exceeds ``max_iterations``.
    """
    if not body.steps:
        raise LoopAuthoringError("the demonstrated body has no steps to iterate over")
    if not records:
        raise LoopAuthoringError(
            "the worklist is empty; supply at least one record (an empty "
            "worklist would author a loop whose body never runs)"
        )
    if relation in body.data_sources:
        raise LoopAuthoringError(
            f"relation name '{relation}' already exists in the body's "
            "data_sources; choose a different --relation name"
        )
    if DEFAULT_BODY_ID in body.subflows:
        raise LoopAuthoringError(
            f"subflow id '{DEFAULT_BODY_ID}' is already used by the body; the "
            "authoring path cannot wrap a body that already reserves it"
        )

    columns = worklist_columns(records)
    effective = resolve_column_map(columns, body, column_map)

    rows: list[dict[str, str]] = [
        {param: record[col] for col, param in effective.items()} for record in records
    ]

    if len(rows) > max_iterations:
        raise LoopAuthoringError(
            f"worklist has {len(rows)} record(s), exceeding max_iterations="
            f"{max_iterations}; raise the bound or shorten the worklist (the "
            "runtime HALTs rather than iterate past the bound)"
        )

    # Scaffold the demonstrated linear body as the per-row subflow. Built from a
    # deep copy so the emitted subflow owns independent Step objects (no shared
    # mutable state with the returned workflow's ``steps``).
    body_graph = lift_to_program(body.model_copy(deep=True))

    program = ProgramGraph(
        entry=LOOP_STATE_ID,
        states={
            LOOP_STATE_ID: State(
                id=LOOP_STATE_ID,
                kind=StateKind.LOOP,
                loop=LoopSpec(
                    relation=relation,
                    body=DEFAULT_BODY_ID,
                    var=loop_var,
                    max_iterations=max_iterations,
                ),
                transitions=[
                    Transition(target=DONE_STATE_ID, label="worklist exhausted")
                ],
            ),
            DONE_STATE_ID: State(
                id=DONE_STATE_ID,
                kind=StateKind.TERMINAL,
                outcome="success",
                reason="all worklist records processed",
            ),
        },
    )

    return body.model_copy(
        deep=True,
        update={
            "name": name or f"{body.name}-for-each",
            "program": program,
            "subflows": {**body.subflows, DEFAULT_BODY_ID: body_graph},
            "data_sources": {
                **body.data_sources,
                relation: Relation(
                    name=relation,
                    rows=rows,
                    description=(
                        f"data-driven worklist authored over {len(rows)} record(s)"
                    ),
                ),
            },
            # Drop the source bundle's sealed manifest -- ``save`` reseals a
            # fresh integrity/provenance manifest over the looped bundle.
            "manifest": None,
        },
    )
