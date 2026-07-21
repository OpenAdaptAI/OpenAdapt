"""Leaderboard submission format + reproducibility manifest + a verifier.

A leaderboard entry is a single JSON document that is FULLY REPRODUCIBLE: it
carries the raw per-episode rows, the headline SWER recomputed from them, and a
reproducibility manifest (benchmark version, task-pack fingerprint, pinned
dependency versions, seeds, arm name). Anyone can re-derive every headline from
the raw rows -- :func:`score_submission` does exactly that and refuses a
submission whose claimed numbers do not match the recomputation.

Submission document shape::

    {
      "effectbench_submission_version": 1,
      "system": {"name": ..., "description": ..., "url": ...},
      "reproducibility": { ...see reproducibility_manifest()... },
      "results": { "swer": {...}, "over_halt": {...}, ... },
      "episodes": [ EpisodeRecord, ... ]   # raw rows -- the source of truth
    }
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from typing import Any, Optional, Sequence

from effectbench import __version__
from effectbench.metrics import summarize
from effectbench.schema import EpisodeRecord
from effectbench.tasks.mockmed import MOCKMED_TASKS

SUBMISSION_VERSION = 1


def pack_manifest() -> dict[str, Any]:
    """The machine-readable manifest of the public MockMed anchor pack.

    Sequestered test-split tasks (none in the public sample today) would redact
    their oracle wiring + payload; the field is emitted so a submission records
    which pack version it ran against.
    """
    tasks = []
    for t in MOCKMED_TASKS:
        spec = t.spec
        sequestered = spec.split == "test"
        row: dict[str, Any] = {
            "task_id": spec.task_id,
            "title": spec.title,
            "category": spec.category.value,
            "substrate": spec.substrate.value,
            "split": spec.split,
            "reversible": spec.reversible,
            "effect_declared": spec.effect_declared,
            "correct_action_available": t.correct_action_available,
            "oracle": {
                "channel": spec.oracle.channel.value,
                "isolated_from_agent": spec.oracle.isolated_from_agent,
                "trial_unique_payload": spec.oracle.trial_unique_payload,
                "refusal_controls": spec.oracle.refusal_controls,
                "adversarially_audited": spec.oracle.adversarially_audited,
            },
        }
        if sequestered:
            row["goal"] = "<sequestered>"
            row["expected_effect_hash"] = "<sequestered>"
        else:
            row["goal"] = spec.goal
            row["expected_effect_hash"] = spec.expected_effect.contract_hash()
        tasks.append(row)
    return {
        "schema_version": 1,
        "suite": "mockmed-anchor",
        "n_tasks": len(tasks),
        "tasks": tasks,
    }


def pack_fingerprint() -> str:
    """A stable SHA-256 over the public pack manifest (detects task drift)."""
    payload = json.dumps(pack_manifest(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reproducibility_manifest(
    *, trials: int, seeds: Optional[Sequence[int]] = None
) -> dict[str, Any]:
    """Everything needed to reproduce a run: versions, pack fingerprint, seeds."""
    return {
        "effectbench_version": __version__,
        "pack": "mockmed-anchor",
        "pack_fingerprint": pack_fingerprint(),
        "trials_per_task": trials,
        # Trial i seeds its trial-unique payload with i; the default seed set is
        # range(trials). A submission may pin an explicit list.
        "seeds": list(seeds) if seeds is not None else list(range(trials)),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "dependencies": _dependency_versions(),
    }


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        import pydantic

        versions["pydantic"] = pydantic.VERSION
    except Exception:  # pragma: no cover - pydantic is a hard dep
        pass
    return versions


def _results_block(episodes: Sequence[EpisodeRecord], arm: str) -> dict[str, Any]:
    s = summarize(episodes, arm=arm)
    return {
        "arm": arm,
        "n_episodes": s.n_episodes,
        "n_tasks": s.n_tasks,
        "swer": s.swer.model_dump(),
        "swer_wrong_write": s.swer_wrong_write.model_dump(),
        "swer_phantom": s.swer_phantom.model_dump(),
        "over_halt": s.over_halt.model_dump(),
        "task_success": s.task_success.model_dump(),
        "screen_success": s.screen_success.model_dump(),
        "success_effect_gap": s.success_effect_gap,
        "pass_hat_k": s.pass_hat_k,
        "outcome_counts": s.outcome_counts,
        "cells": [c.model_dump() for c in s.cells],
    }


def build_submission(
    *,
    system_name: str,
    episodes: Sequence[EpisodeRecord],
    trials: int,
    description: str = "",
    url: str = "",
    seeds: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """Assemble a fully reproducible leaderboard submission for one arm."""
    return {
        "effectbench_submission_version": SUBMISSION_VERSION,
        "system": {
            "name": system_name,
            "description": description,
            "url": url,
        },
        "reproducibility": reproducibility_manifest(trials=trials, seeds=seeds),
        "results": _results_block(episodes, system_name),
        "episodes": [json.loads(e.model_dump_json()) for e in episodes],
    }


def score_submission(submission: dict[str, Any]) -> dict[str, Any]:
    """Recompute the headline from a submission's RAW rows and verify agreement.

    Returns ``{"ok": bool, "recomputed": {...}, "errors": [...]}``. A submission
    is accepted only when the claimed ``results`` match the numbers recomputed
    from ``episodes`` (the raw rows are the single source of truth) and the pack
    fingerprint matches this benchmark version.
    """
    errors: list[str] = []
    version = submission.get("effectbench_submission_version")
    if version != SUBMISSION_VERSION:
        errors.append(
            f"unsupported submission version {version!r} "
            f"(expected {SUBMISSION_VERSION})"
        )

    raw = submission.get("episodes")
    if not isinstance(raw, list) or not raw:
        errors.append("submission carries no raw episode rows to reproduce from")
        return {"ok": False, "recomputed": {}, "errors": errors}

    try:
        episodes = [EpisodeRecord.model_validate(row) for row in raw]
    except Exception as exc:  # noqa: BLE001 - report any malformed row
        return {
            "ok": False,
            "recomputed": {},
            "errors": errors + [f"malformed episode row: {exc}"],
        }

    arm = submission.get("results", {}).get("arm") or episodes[0].arm
    recomputed = _results_block(episodes, arm)

    claimed = submission.get("results", {})
    for key in ("swer", "over_halt", "task_success", "screen_success"):
        claim = claimed.get(key, {})
        recomp = recomputed[key]
        if (
            claim.get("numerator") != recomp["numerator"]
            or claim.get("denominator") != recomp["denominator"]
        ):
            errors.append(
                f"claimed {key} {claim.get('numerator')}/{claim.get('denominator')} "
                f"!= recomputed {recomp['numerator']}/{recomp['denominator']}"
            )

    repro = submission.get("reproducibility", {})
    if repro.get("pack_fingerprint") not in (None, pack_fingerprint()):
        errors.append(
            "reproducibility.pack_fingerprint does not match this benchmark's "
            "task pack (the submission ran a different pack version)"
        )

    return {"ok": not errors, "recomputed": recomputed, "errors": errors}
