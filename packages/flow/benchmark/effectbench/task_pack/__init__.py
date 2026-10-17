"""EffectBench first task pack — ~40 authored Silent-Wrong-Effect tasks.

Item #3 of the Silent Wrong-Effect benchmark: the first credible task pack
spanning all seven divergence categories (C1–C7) plus clean / idempotent /
refusal controls, on the pinned system-of-record environments the
``benchmark.environments`` registry (PR #173) indexes, authored against the
frozen EffectBench schema/oracle/metrics contract (PR #178).

Each task is a :class:`~openadapt_flow.benchmark.effectbench.TaskSpec` (goal =
INTENT only, never a step list) plus an
:class:`~openadapt_flow.benchmark.effectbench.OracleSpec` bound to the
environment's system-of-record READ recipe, DESIGNED so a green screen
(task-success) diverges from the correct business effect. The oracle reads
pre/post SoR state independently of the screen; the non-gameability attestations
(``isolated_from_agent`` / ``trial_unique_payload`` / ``refusal_controls`` /
``adversarially_audited``) are set truthfully — ``adversarially_audited`` is True
ONLY for the MockMed anchor, whose oracle the live red-team pass in :mod:`.audit`
actually exercised; the container-gated OpenEMR/Frappe tasks keep it False until
a container run audits them.

Three families:

- :mod:`.mockmed_tasks` — the CI-fast anchor (no Docker); RUNS LIVE end-to-end
  through :mod:`.driver` + ``score_episode``.
- :mod:`.openemr_tasks`, :mod:`.frappe_tasks` — container-gated (need Docker),
  AUTHORED + statically wired to their SQL/FHIR read recipes; marked
  ``needs_container``.

Public surface: :data:`~.pack.PACK`, :data:`~.pack.ALL_TASKS`,
:func:`~.pack.manifest`, :func:`~.pack.validate`, the count helpers, and (for
the anchor) :func:`~.driver.run_mockmed_pack` + :func:`~.audit.audit_mockmed_oracle`.
"""

from benchmark.effectbench.task_pack.pack import (  # noqa: F401
    ALL_TASKS,
    PACK,
    PackEntry,
    by_id,
    category_counts,
    category_substrate_matrix,
    entries_for,
    environment_counts,
    manifest,
    split_counts,
    substrate_counts,
    validate,
)

__all__ = [
    "PACK",
    "ALL_TASKS",
    "PackEntry",
    "by_id",
    "entries_for",
    "manifest",
    "validate",
    "category_counts",
    "substrate_counts",
    "environment_counts",
    "split_counts",
    "category_substrate_matrix",
]
