"""The EffectBench first task pack — assembly, manifest, and validation.

This is the single entry point over the three task families (MockMed anchor +
the container-gated OpenEMR and Frappe families). It:

- assembles the unified list of :class:`PackEntry` (one per authored task),
  each carrying its :class:`~openadapt_flow.benchmark.effectbench.TaskSpec`, the
  pinned environment it targets, and whether it runs LIVE here or needs a
  container;
- validates every task against the ``benchmark.environments`` registry (the
  substrate matches, the environment exists, and the oracle channel is a read
  path the registry declares independent of the agent); and
- emits the machine-readable :func:`manifest` (task_id, category, substrate,
  oracle, split, axes) with the **sequestered test split redacted** — a
  test-split task exposes only its identity/category/substrate/split, never its
  oracle wiring or trial payload, so a public leaderboard cannot overfit it.

The pack deliberately keeps a held-out ``split == "test"`` subset across
categories and substrates; :func:`split_counts` reports the balance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from benchmark.effectbench.task_pack.frappe_tasks import FRAPPE_TASKS
from benchmark.effectbench.task_pack.mockmed_tasks import MOCKMED_TASKS
from benchmark.effectbench.task_pack.openemr_tasks import OPENEMR_TASKS
from openadapt_flow.benchmark.effectbench import TaskSpec
from openadapt_flow.benchmark.effectbench.schema import DivergenceCategory, Substrate

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PackEntry:
    """One authored task plus where and how it is exercised."""

    spec: TaskSpec
    environment: str
    #: Runs live end-to-end here (MockMed anchor) vs needs a container run.
    live_validated: bool
    correct_action_available: bool

    @property
    def needs_container(self) -> bool:
        return not self.live_validated


def _entries() -> tuple[PackEntry, ...]:
    entries: list[PackEntry] = []
    for t in MOCKMED_TASKS:
        entries.append(
            PackEntry(
                spec=t.spec,
                environment="mockmed",
                live_validated=True,
                correct_action_available=t.correct_action_available,
            )
        )
    for family in (OPENEMR_TASKS, FRAPPE_TASKS):
        for ct in family:
            entries.append(
                PackEntry(
                    spec=ct.spec,
                    environment=ct.needs_container,
                    live_validated=False,
                    correct_action_available=ct.correct_action_available,
                )
            )
    return tuple(entries)


PACK: tuple[PackEntry, ...] = _entries()
ALL_TASKS: tuple[TaskSpec, ...] = tuple(e.spec for e in PACK)


def by_id(task_id: str) -> PackEntry:
    for e in PACK:
        if e.spec.task_id == task_id:
            return e
    raise KeyError(task_id)


def entries_for(
    *, environment: str | None = None, split: str | None = None
) -> tuple[PackEntry, ...]:
    out = PACK
    if environment is not None:
        out = tuple(e for e in out if e.environment == environment)
    if split is not None:
        out = tuple(e for e in out if e.spec.split == split)
    return out


# ---------------------------------------------------------------------------
# Validation against the pinned environments registry (PR #173)
# ---------------------------------------------------------------------------


def validate() -> None:
    """Fail-closed structural validation of the whole pack.

    Checks (raises ``ValueError`` on any violation): unique task ids; goals are
    intent (not obviously a numbered step list); every environment is in the
    registry with a matching substrate and an actor-isolated system of record;
    the oracle channel is one the environment actually exposes; and the
    non-gameability attestations are internally consistent (a released/live task
    that claims ``adversarially_audited`` must, and container tasks must not
    until a container run).
    """
    from benchmark.environments import get as get_env

    seen: set[str] = set()
    for e in PACK:
        spec = e.spec
        if spec.task_id in seen:
            raise ValueError(f"duplicate task_id {spec.task_id!r}")
        seen.add(spec.task_id)

        # goal is intent, never a step list.
        if _looks_like_step_list(spec.goal):
            raise ValueError(f"{spec.task_id}: goal looks like a step list, not intent")

        env = get_env(e.environment)
        if env.substrate != spec.substrate.value:
            raise ValueError(
                f"{spec.task_id}: substrate {spec.substrate.value!r} != "
                f"environment {env.name!r} substrate {env.substrate!r}"
            )
        if not env.sor.isolated_from_actor:
            raise ValueError(
                f"{spec.task_id}: environment {env.name!r} SoR is not actor-isolated"
            )
        _validate_oracle_channel(spec, env)
        _validate_effect_params(spec)

        # Non-gameability: container tasks are NOT release-eligible until a
        # container red-team pass, so they must not claim adversarial audit.
        if e.needs_container and spec.oracle.adversarially_audited:
            raise ValueError(
                f"{spec.task_id}: container task claims adversarially_audited "
                "before a container run"
            )
        # A live-validated task that claims the audit must have refusal-control
        # or trial-unique backing that the audit actually exercised — we assert
        # the attestations are at least set truthfully (audited MockMed tasks
        # carry trial_unique_payload).
        if spec.oracle.adversarially_audited and not spec.oracle.trial_unique_payload:
            raise ValueError(
                f"{spec.task_id}: adversarially_audited without a trial-unique "
                "payload is not defensible"
            )


_CHANNELS_BY_ENV = {
    "mockmed": {"rest"},  # GET /api/db JSON boundary (http_json read as REST)
    "openemr_local": {"sql", "fhir", "rest"},
    "frappe_lending": {"sql", "rest"},
    "openimis_claims": {"sql"},
}


def _validate_oracle_channel(spec: TaskSpec, env: Any) -> None:
    allowed = _CHANNELS_BY_ENV.get(env.name, set())
    if spec.oracle.channel.value not in allowed:
        raise ValueError(
            f"{spec.task_id}: oracle channel {spec.oracle.channel.value!r} is not "
            f"a read path {env.name!r} exposes {sorted(allowed)}"
        )


def _validate_effect_params(spec: TaskSpec) -> None:
    """Every ``{param: ...}`` reference in the effect must be a known pack param.

    Guards the trial-unique-payload attestation: an effect that references a run
    param the driver never binds would silently verify nothing.
    """
    from benchmark.effectbench.task_pack._authoring import (
        PARAM_NOTE,
        PARAM_RECORD_KEY,
        PARAM_TARGET,
    )

    known = {PARAM_NOTE, PARAM_RECORD_KEY, PARAM_TARGET}
    eff = spec.expected_effect
    refs: list[str] = []
    for v in eff.match.values():
        if v.param is not None:
            refs.append(v.param)
    for maybe in (eff.value, eff.idempotency_key):
        if maybe is not None and maybe.param is not None:
            refs.append(maybe.param)
    unknown = [r for r in refs if r not in known]
    if unknown:
        raise ValueError(f"{spec.task_id}: effect references unknown params {unknown}")
    if spec.oracle.trial_unique_payload and not refs:
        raise ValueError(
            f"{spec.task_id}: claims trial_unique_payload but the effect binds no "
            "run param (nothing trial-unique is checked)"
        )


def _looks_like_step_list(goal: str) -> bool:
    stripped = goal.strip()
    # A numbered/'->'-chained imperative recipe is a step list; a single
    # intent sentence (possibly compound) is fine.
    if stripped[:2] in {"1.", "1)"}:
        return True
    return " -> " in stripped or "\n-" in stripped or "\n1." in stripped


# ---------------------------------------------------------------------------
# Manifest (machine-readable; sequestered test split redacted)
# ---------------------------------------------------------------------------

_REDACTED = "<sequestered: withheld from the public split>"


def _entry_manifest(e: PackEntry) -> dict[str, Any]:
    spec = e.spec
    sequestered = spec.split == "test"
    row: dict[str, Any] = {
        "task_id": spec.task_id,
        "title": spec.title,
        "category": spec.category.value,
        "substrate": spec.substrate.value,
        "environment": e.environment,
        "split": spec.split,
        "reversible": spec.reversible,
        "effect_declared": spec.effect_declared,
        "live_validated": e.live_validated,
        "needs_container": e.needs_container,
        "correct_action_available": e.correct_action_available,
        "oracle": {
            "channel": spec.oracle.channel.value,
            "isolated_from_agent": spec.oracle.isolated_from_agent,
            "trial_unique_payload": spec.oracle.trial_unique_payload,
            "refusal_controls": spec.oracle.refusal_controls,
            "adversarially_audited": spec.oracle.adversarially_audited,
        },
    }
    if sequestered:
        # Withhold the wiring + payload that a leaderboard could overfit.
        row["oracle"]["description"] = _REDACTED
        row["oracle"]["config"] = _REDACTED
        row["expected_effect_hash"] = _REDACTED
        row["goal"] = _REDACTED
    else:
        row["goal"] = spec.goal
        row["oracle"]["description"] = spec.oracle.description
        row["oracle"]["config"] = spec.oracle.config
        row["expected_effect_hash"] = spec.expected_effect.contract_hash()
    return row


def manifest() -> dict[str, Any]:
    """The machine-readable manifest of the pack (test split redacted)."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "description": (
            "EffectBench first task pack: authored TaskSpec + OracleSpec across "
            "the 7 divergence categories on pinned system-of-record environments. "
            "Sequestered (split == 'test') tasks redact oracle wiring + payload."
        ),
        "counts": {
            "total": len(PACK),
            "by_category": category_counts(),
            "by_substrate": substrate_counts(),
            "by_environment": environment_counts(),
            "by_split": split_counts(),
            "live_validated": sum(1 for e in PACK if e.live_validated),
            "needs_container": sum(1 for e in PACK if e.needs_container),
        },
        "tasks": [_entry_manifest(e) for e in PACK],
    }


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _count(key) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in PACK:
        out[key(e)] = out.get(key(e), 0) + 1
    return dict(sorted(out.items()))


def category_counts() -> dict[str, int]:
    return _count(lambda e: e.spec.category.value)


def substrate_counts() -> dict[str, int]:
    return _count(lambda e: e.spec.substrate.value)


def environment_counts() -> dict[str, int]:
    return _count(lambda e: e.environment)


def split_counts() -> dict[str, int]:
    return _count(lambda e: e.spec.split)


def category_substrate_matrix() -> dict[str, dict[str, int]]:
    """The per (category x substrate) cell counts, the benchmark's unit."""
    matrix: dict[str, dict[str, int]] = {}
    for cat in DivergenceCategory:
        row: dict[str, int] = {}
        for sub in Substrate:
            n = sum(
                1 for e in PACK if e.spec.category is cat and e.spec.substrate is sub
            )
            if n:
                row[sub.value] = n
        if row:
            matrix[cat.value] = row
    return matrix


def iter_tasks() -> Iterable[TaskSpec]:
    return ALL_TASKS


# ---------------------------------------------------------------------------
# CLI: regenerate the committed manifest + print a coverage report
# ---------------------------------------------------------------------------

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"


def write_manifest() -> None:
    validate()
    MANIFEST_PATH.write_text(json.dumps(manifest(), indent=2, sort_keys=False) + "\n")


def main() -> None:
    write_manifest()
    m = manifest()
    print(f"EffectBench task pack: {m['counts']['total']} tasks")
    print(f"  by category   : {m['counts']['by_category']}")
    print(f"  by environment: {m['counts']['by_environment']}")
    print(f"  by split      : {m['counts']['by_split']}")
    print(
        f"  live-validated: {m['counts']['live_validated']} | "
        f"needs-container: {m['counts']['needs_container']}"
    )
    print(f"wrote {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
