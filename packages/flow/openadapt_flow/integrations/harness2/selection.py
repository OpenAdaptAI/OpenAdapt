"""Choose the final harness by read-back reward, and run one task end to end.

Harness² chooses its final with a model reading anonymised candidates,
because its benchmarks have no scorer to consult. With a read-back scorer the
choice can be the candidate whose rollout the system of record rewarded most.
:func:`reward_selection` returns Harness²'s own ``Selection`` record, so the
rest of its pipeline (judging, summaries) reads it unchanged.

:func:`run_task` is the loop for one task: ``k`` base rollouts, one Harness²
recursion mode (its code, imported), a selection by reward or by a model, and
the read-back of the chosen run, and a trace export of every rollout
(``<work_dir>/traces``, see :mod:`.traces`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from harness2.core import recursion
from harness2.core import selection as h2_selection
from harness2.core import tree as h2_tree
from harness2.core.proposer import Proposer
from harness2.core.spec import RunView

from openadapt_flow.integrations.harness2 import traces
from openadapt_flow.integrations.harness2.adapter import OpenAdaptGuiAdapter

#: Where an unscored candidate ranks: below a safe stop, above any penalty.
_UNSCORED_RANK = -0.01


def _rank(adapter: OpenAdaptGuiAdapter, label: str, rv: RunView, task_id: str) -> tuple:
    s = adapter.score(rv.run_dir, task_id) if rv.ok else None
    if s is None:
        return (float("-inf"), 0, 0, label)
    reward = s["reward"] if s["reward"] is not None else _UNSCORED_RANK
    # A run that did the task right first (on the timeout trap that can be an
    # unscored single save a person reconciles, which beats never saving),
    # then the higher reward, then the earlier label.
    return (float(reward), int(s["all_pass"]), 0, label)


def reward_selection(
    task_id: str,
    k: int,
    mode: str,
    candidates: dict[str, RunView],
    *,
    adapter: OpenAdaptGuiAdapter,
    work_dir: Path,
) -> h2_selection.Selection:
    """Pick the candidate the read-back rewarded most. No model call."""

    sel = h2_selection.Selection(task_id=task_id, mode=mode, k=k, evidence="readback")
    pool = {lbl: rv for lbl, rv in candidates.items() if rv.ok}
    sel.dropped = sorted(set(candidates) - set(pool))
    sel.n_candidates = len(pool)
    if not pool:
        sel.error = "no candidate rollout completed"
        return sel
    ranked = sorted(
        (_rank(adapter, lbl, rv, task_id) for lbl, rv in pool.items()),
        key=lambda r: (-r[1], -r[0], r[3]),
    )
    best = ranked[0]
    sel.chosen_label = best[3]
    sel.chosen_id = best[3]
    sel.id_map = {lbl: lbl for lbl in pool}
    sel.chosen_run_id = pool[best[3]].run_id
    sel.chosen_is_base = best[3].startswith("base")
    sel.ok = True
    sel.why = "highest read-back reward: " + ", ".join(
        f"{r[3]}={r[0]:+.2f}" for r in ranked
    )
    root = Path(work_dir) / "selection" / f"{mode}{k}"
    root.mkdir(parents=True, exist_ok=True)
    rec = sel.to_dict()
    rec["chosen_run_dir"] = str(pool[best[3]].run_dir)
    (root / "selection.json").write_text(
        json.dumps(rec, indent=2, default=str), "utf-8"
    )
    return sel


def run_task(
    task_id: str,
    k: int,
    *,
    adapter: OpenAdaptGuiAdapter,
    prop: Proposer,
    work_dir: Path,
    mode: str = "seq",
    selector: str = "reward",
    selector_prop: Optional[Proposer] = None,
) -> dict[str, Any]:
    """Base rollouts, one recursion mode, selection, read-back of the chosen run.

    ``selector="reward"`` picks by read-back; ``selector="model"`` uses
    Harness²'s own model selector (``selector_prop`` or ``prop``).
    """

    if selector not in ("reward", "model"):
        raise ValueError(f"selector must be 'reward' or 'model', got {selector!r}")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    safe = task_id.replace("/", "-")

    def run_id_of(label: str) -> str:
        return f"{work_dir.name}-{safe}-{mode}-k{k}-{label}"

    surface = adapter.substrate.surface()
    base_tree = h2_tree.read_tree(surface, adapter.base_harness())
    base: list[RunView] = []
    for i in range(1, k + 1):
        label = f"base{i}"
        rid = run_id_of(label)
        try:
            rd = adapter.execute(task_id, None, rid)
            base.append(
                RunView(label, rid, rd, ok=True, delivered=adapter.delivered(rd))
            )
        except Exception as exc:  # noqa: BLE001 - recorded like Harness² does
            base.append(
                RunView(label, rid, work_dir / "missing", False, False, error=str(exc))
            )
    result = recursion.run_mode(
        mode,
        task_id,
        k,
        base,
        adapter=adapter,
        prop=prop,
        work_dir=work_dir,
        run_id_of=run_id_of,
        base_tree=base_tree,
    )
    candidates = h2_selection.selection_pool(mode, k, result.rollouts, base)
    if selector == "reward":
        sel = reward_selection(
            task_id, k, mode, candidates, adapter=adapter, work_dir=work_dir
        )
    else:
        sel = h2_selection.run_selection(
            task_id,
            k,
            mode,
            candidates,
            adapter=adapter,
            prop=selector_prop or prop,
            work_dir=work_dir,
            pool_kind="finals",
        )
    chosen = candidates.get(sel.chosen_label)
    chosen_tree = result.trees.get(sel.chosen_label, base_tree)
    summary = {
        "task_id": task_id,
        "k": k,
        "mode": mode,
        "selector": selector,
        "evidence": adapter.evidence,
        "selection": sel.to_dict(),
        "chosen_score": adapter.score(chosen.run_dir, task_id) if chosen else None,
        "chosen_tree": chosen_tree,
        "rollouts": {
            lbl: adapter.score(rv.run_dir, task_id) if rv.ok else None
            for lbl, rv in result.rollouts.items()
        },
        "mode_result": result.to_dict(),
        "proposer_usd": round(sum(s.usd for s in result.stages) + sel.usd, 6),
    }
    labelled = [(lbl, rv) for lbl, rv in result.rollouts.items() if rv.ok]
    traces.export(
        work_dir / "traces",
        [
            traces.from_rollout(rv.run_dir, adapter.task(task_id), candidate=lbl)
            for lbl, rv in labelled
        ],
        harnesses={
            lbl: work_dir / "harnesses" / lbl
            for lbl, _rv in labelled
            if (work_dir / "harnesses" / lbl).is_dir()
        },
    )
    summary["traces"] = str(work_dir / "traces")
    (work_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return summary


__all__ = ["reward_selection", "run_task"]
