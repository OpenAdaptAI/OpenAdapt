"""Harness² with and without the OpenAdapt read-back scorer, on MockMed.

Three arms, each over the same MockMed task family:

* ``base``: no search. The base harness, re-run ``--rerun`` times per task.
* ``harness2``: Harness² as published. Proposers and the selector see the
  screens and the agent's own report (``evidence="screen"``); a model picks
  the final.
* ``harness2+readback``: the same search, but every trajectory ends with the
  read-back from the system of record, and the final is chosen by the
  read-back reward (``evidence="readback"``, ``reward_selection``).

Selection by reward picks a run that already succeeded, so the chosen run's
own outcome flatters it. The comparison is on fresh rollouts: the chosen
harness is re-run ``--rerun`` times per task and each re-run is scored by
read-back.

Every model call goes through one ``CostLedger`` with a hard cap. All data is
synthetic; requests leave under a recorded privacy opt-out.

    python benchmark/harness2_scorer/run.py --env-file PATH --cap 15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from harness2.core.spec import ChannelSpec
from harness2.core.tree import write_tree

from openadapt_flow import privacy
from openadapt_flow.integrations.harness2.adapter import (
    GUI_SURFACE,
    OpenAdaptGuiAdapter,
)
from openadapt_flow.integrations.harness2.agent import ModelGuiAgent
from openadapt_flow.integrations.harness2.llm import (
    BudgetExceeded,
    CostLedger,
    HttpChatModel,
    Price,
)
from openadapt_flow.integrations.harness2.proposers import ModelProposer
from openadapt_flow.integrations.harness2.selection import run_task
from openadapt_flow.integrations.harness2.tasks import mockmed_tasks

HERE = Path(__file__).resolve().parent

#: Provider -> (model, list price). Checked against the providers' price pages
#: when the run was made; the ledger charges these, so the cap is on list price.
DEFAULT_MODELS = {
    "openai": ("gpt-4.1-mini", Price(0.40, 1.60)),
    "openai-nano": ("gpt-4.1-nano", Price(0.10, 0.40)),
    "anthropic": ("claude-haiku-4-5", Price(1.00, 5.00)),
    "google": ("gemini-2.5-flash", Price(0.30, 2.50)),
}
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "openai-nano": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def load_env_file(path: Path) -> None:
    """Load the three provider keys from a dotenv file. Never prints them."""

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in KEY_ENV.values() and not os.environ.get(key):
            os.environ[key] = value.strip().strip("'\"")


def pick_provider(requested: str) -> str:
    order = [requested] if requested != "auto" else ["openai", "anthropic", "google"]
    for name in order:
        if os.environ.get(KEY_ENV[name]):
            return name
    raise SystemExit("no provider key is set")


def rerun(
    adapter: OpenAdaptGuiAdapter,
    task_id: str,
    tree: dict[str, str],
    root: Path,
    n: int,
    tag: str,
    start: int = 1,
) -> list[dict[str, Any]]:
    hdir = write_tree(GUI_SURFACE, tree, root / "harness") if tree else None
    out = []
    for i in range(start, start + n):
        rid = f"{tag}-{task_id.replace('/', '-')}-rerun{i}"
        rd = adapter.execute(task_id, hdir, rid)
        out.append({"run_id": rid, **(adapter.score(rd, task_id) or {})})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", type=Path)
    ap.add_argument(
        "--provider", default="auto", choices=["auto", "openai", "anthropic", "google"]
    )
    ap.add_argument("--model", help="proposer and selector model")
    ap.add_argument(
        "--agent-model",
        help="the GUI agent's model, as a DEFAULT_MODELS key (default: the proposer's)",
    )
    ap.add_argument("--cap", type=float, default=15.0)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--mode", default="agg", choices=["agg", "seq"])
    ap.add_argument("--rerun", type=int, default=3)
    ap.add_argument("--variants", default="")
    ap.add_argument("--arms", default="base,harness2,harness2+readback")
    ap.add_argument("--out", type=Path, default=Path("runs/harness2_scorer"))
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument(
        "--more-reruns",
        type=int,
        default=0,
        help="re-run each already chosen harness this many more times (reads --out)",
    )
    args = ap.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    provider = pick_provider(args.provider)
    model_id, price = DEFAULT_MODELS[provider]
    model_id = args.model or model_id
    os.environ.setdefault(
        privacy.OUTBOUND_OPT_OUT_ENV,
        "synthetic MockMed data only (benchmark/harness2_scorer)",
    )
    ledger = CostLedger(args.cap)
    model = HttpChatModel(provider, model_id, price, ledger)
    agent_model = model
    if args.agent_model:
        agent_id, agent_price = DEFAULT_MODELS[args.agent_model]
        agent_provider = args.agent_model.split("-")[0]
        agent_model = HttpChatModel(agent_provider, agent_id, agent_price, ledger)
    variants = tuple(v for v in args.variants.split(",") if v) or None
    tasks = mockmed_tasks(variants)
    arms = [a for a in args.arms.split(",") if a]
    print(
        f"provider={provider} model={model_id} agent={agent_model.name} cap=${args.cap} tasks={len(tasks)} arms={arms}",
        flush=True,
    )

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {
        "schema": "openadapt.harness2-scorer-run/v1",
        "provider": provider,
        "model": model_id,
        "agent_model": agent_model.name,
        "price_per_mtok": {
            "input": price.input_per_mtok,
            "output": price.output_per_mtok,
        },
        "cap_usd": args.cap,
        "k": args.k,
        "mode": args.mode,
        "rerun": args.rerun,
        "harness2_commit": "1cfe254e8d25b15f37048c3730134686abe60335",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "arms": {},
    }

    if args.more_reruns:
        old = json.loads((out / "results.json").read_text(encoding="utf-8"))
        results["arms"] = old["arms"]
        results["started_at"] = old.get("started_at", results["started_at"])
        results["more_reruns_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results["spent_before_more_reruns_usd"] = old.get("spent_usd", 0.0)

    def save() -> None:
        results["spent_usd"] = round(ledger.spent_usd, 4)
        results["model_calls"] = ledger.calls
        (out / "results.json").write_text(
            json.dumps(results, indent=2, default=str), "utf-8"
        )

    agent = ModelGuiAgent(agent_model, max_steps=args.max_steps)
    try:
        for arm in arms:
            evidence = "readback" if arm == "harness2+readback" else "screen"
            root = out / arm.replace("+", "_")
            adapter = OpenAdaptGuiAdapter(
                tasks, agent, root / "runs", evidence=evidence
            )
            prop = ModelProposer(
                ChannelSpec("claude_cli", model_id.replace("/", "_")),
                model=model,
                audit_dir=root / "audit",
            )
            arm_rows = results["arms"].setdefault(arm, {})
            for task in tasks:
                if task.task_id in arm_rows and arm_rows[task.task_id].get("reruns"):
                    if not args.more_reruns:
                        continue
                    row = arm_rows[task.task_id]
                    done = len(row["reruns"])
                    spent0 = ledger.spent_usd
                    row["reruns"] += rerun(
                        adapter,
                        task.task_id,
                        dict(row.get("chosen_tree") or {}),
                        root / "reruns" / task.task_id.replace("/", "-"),
                        args.more_reruns,
                        arm.replace("+", "_"),
                        start=done + 1,
                    )
                    row["usd"] = round(
                        row.get("usd", 0.0) + ledger.spent_usd - spent0, 4
                    )
                    outcomes = [r.get("outcome") for r in row["reruns"]]
                    print(
                        f"[{arm}] {task.task_id}: more reruns -> {outcomes}", flush=True
                    )
                    save()
                    continue
                spent0 = ledger.spent_usd
                t0 = time.time()
                row: dict[str, Any] = {}
                tree: dict[str, str] = {}
                if arm != "base":
                    summary = run_task(
                        task.task_id,
                        args.k,
                        adapter=adapter,
                        prop=prop,
                        work_dir=root / "work" / task.task_id.replace("/", "-"),
                        mode=args.mode,
                        selector="reward" if evidence == "readback" else "model",
                    )
                    tree = dict(summary["chosen_tree"] or {})
                    row.update(
                        chosen=summary["selection"].get("chosen_label"),
                        selection_ok=summary["selection"].get("ok"),
                        chosen_score=summary["chosen_score"],
                        rollouts=summary["rollouts"],
                        chosen_tree=tree,
                    )
                row["reruns"] = rerun(
                    adapter,
                    task.task_id,
                    tree,
                    root / "reruns" / task.task_id.replace("/", "-"),
                    args.rerun,
                    arm.replace("+", "_"),
                )
                row["usd"] = round(ledger.spent_usd - spent0, 4)
                row["wall_s"] = round(time.time() - t0, 1)
                arm_rows[task.task_id] = row
                outcomes = [r.get("outcome") for r in row["reruns"]]
                print(
                    f"[{arm}] {task.task_id}: chosen={row.get('chosen')} reruns={outcomes} ${row['usd']} total=${ledger.spent_usd:.3f}",
                    flush=True,
                )
                save()
    except BudgetExceeded as exc:
        results["stopped"] = f"budget: {exc}"
        print(f"stopped: {exc}", flush=True)
    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
