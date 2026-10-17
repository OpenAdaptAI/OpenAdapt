"""Reproducible, non-gameable system-of-record environments for the benchmark.

This package is the single index over the pinned, containerized applications the
Silent Wrong-Effect benchmark drives. Each environment exposes an *independent
system-of-record* (SQL / REST / HTTP-JSON) that an effect oracle reads through a
channel the agent never touches — so success is judged by the persisted record,
never by the rendered screen or the agent's self-report.

The environment *fixtures* (bring-up, snapshot/reset, seed state) already live in
sibling directories (``benchmark/openemr_local``, ``benchmark/frappe_lending``,
``benchmark/openimis_claims``) and in ``openadapt_flow.mockmed``. This package
does not duplicate them; it provides:

- :mod:`benchmark.environments.registry` — a typed, importable catalog mapping
  each environment to its substrate, its system-of-record channel, its ports and
  credential locations, its pinned image digests (loaded from the per-environment
  ``environment.lock.json``), and its licensing/packaging boundary. This is the
  machine-readable contract the oracle harness and the task pack consume.
- :mod:`benchmark.environments.verify` — a verification harness that proves
  (a) every environment pins exact image digests (never a floating tag), and
  (b) the CI-fast MockMed anchor's system-of-record is queryable and
  non-gameable (a partial-save fault the screen would report as success is
  visible in the record).
"""

from __future__ import annotations

from benchmark.environments.registry import (
    ENVIRONMENTS,
    Environment,
    LicenseStatus,
    SystemOfRecord,
    all_environments,
    get,
    registry_snapshot,
)

__all__ = [
    "ENVIRONMENTS",
    "Environment",
    "LicenseStatus",
    "SystemOfRecord",
    "all_environments",
    "get",
    "registry_snapshot",
]
