"""Run held-out items on a certified bundle and judge each with the oracle.

Each item is an ordinary Standard-profile certified run (the same path as
``qualify --run-params``): the run gate is evaluated on the certified bundle,
the governed authorization is built from it, and the engine reads back the
record through the engineer's verify.yaml. Independently, the workflow's
oracle snapshots the database before and after and judges the item.

The app is reset once per batch, not per item: items are distinct records, so
each is judged by its own delta, and collateral damage from earlier items
would show as a WRONG verdict on the oracle's collateral check.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ENGINE_VERIFIED = "VERIFIED"


def _engine_outcome(summary: dict[str, Any]) -> str:
    outcome = str(summary.get("transaction_outcome") or "")
    return outcome.split(".")[-1].upper() if outcome else "UNKNOWN"


def run_batch(
    wf: Any,
    installation: str,
    certified: Path,
    verify_path: Path,
    items: list[dict[str, str]],
    out: Path,
) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow as FlowWorkflow
    from openadapt_flow.qualify.campaign import (
        QualifyHooks,
        _default_hooks,
        certified_run,
        lookup_plan,
    )
    from openadapt_flow.qualify.config import load_verify_config

    out.mkdir(parents=True, exist_ok=True)
    config = load_verify_config(verify_path)
    workflow = FlowWorkflow.load(certified)
    hooks = _default_hooks(config, QualifyHooks())
    reset = hooks.reset
    hooks.reset = None  # one reset per batch
    surface, structure = lookup_plan(config)
    lookup = {"use_structural": structure, "deployment_surface": surface}
    setup = type("Setup", (), {"observer": None})()
    wf.reset(installation)
    wf.prepare_items(items, installation)
    if wf.batch_run:
        return _run_one_batch(wf, installation, workflow, certified, config, hooks, setup,
                              lookup, items, out, reset)
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    for n, item in enumerate(items, start=1):
        params = {k: v for k, v in item.items() if not k.startswith("_")}
        if wf.heldout_reset_each and n > 1:
            wf.reset(installation)
            wf.prepare_items([item], installation)
        before = wf.snapshot()
        t_item = time.time()
        try:
            summary = certified_run(
                workflow, certified, config, out=out / f"item-{n:02d}", params=params,
                hooks=hooks, setup=setup, policy=wf.policy,  # type: ignore[arg-type]
                run_id=f"{wf.id}-{installation}-{n:02d}-{int(t_item)}", lookup=lookup,
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - a refused run is a result
            summary, error = {}, f"{type(exc).__name__}: {exc}"[:800]
        time.sleep(0.5)
        after = wf.snapshot()
        verdict = wf.judge(item, before, after)
        engine = _engine_outcome(summary) if summary else "REFUSED"
        row = {
            "n": n,
            "item": item,
            "engine": engine,
            "oracle": verdict.verdict,
            "oracle_detail": verdict.detail,
            "failed_step_id": summary.get("failed_step_id"),
            "failed_error": summary.get("failed_error"),
            "model_calls": summary.get("model_calls"),
            "error": error,
            "seconds": round(time.time() - t_item, 1),
        }
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("n", "engine", "oracle", "failed_step_id", "seconds")}),
              flush=True)
    hooks.reset = reset
    return _result(rows, out, t0)


def _result(rows: list[dict[str, Any]], out: Path, t0: float) -> dict[str, Any]:
    from openadapt_flow.validation.behavioral_generalization import (
        HeldOutCaseResult,
        classify_case,
        inputs_digest,
        summarize,
    )

    behavioral = summarize([
        HeldOutCaseResult(
            inputs_sha256=inputs_digest({k: v for k, v in r["item"].items() if not k.startswith("_")}),
            engine_outcome=r["engine"],
            independent=r["oracle"],
            case_class=classify_case(r["engine"], r["oracle"]),
            failed_step_id=r["failed_step_id"],
            model_calls=int(r["model_calls"] or 0),
            seconds=r["seconds"],
        )
        for r in rows
    ])
    verified = sum(1 for r in rows if r["oracle"] == "VERIFIED")
    silent = sum(1 for r in rows if r["engine"] == ENGINE_VERIFIED and r["oracle"] != "VERIFIED")
    first_failure = next((r for r in rows if r["oracle"] != "VERIFIED"), None)
    result = {
        "n": len(rows),
        "oracle_verified": verified,
        "engine_verified": sum(1 for r in rows if r["engine"] == ENGINE_VERIFIED),
        "silent_wrong": silent,
        "wrong": sum(1 for r in rows if r["oracle"] == "WRONG"),
        "missing": sum(1 for r in rows if r["oracle"] == "MISSING"),
        "model_calls": sum(int(r["model_calls"] or 0) for r in rows),
        "all_verified": verified == len(rows) and len(rows) > 0,
        "first_failure": first_failure,
        "behavioral": behavioral.model_dump(exclude={"per_item"}) | {"score": behavioral.score},
        "seconds": round(time.time() - t0, 1),
        "items": rows,
    }
    (out / "batch.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def _with_worklists(worklists: dict[str, list[dict[str, str]]]):
    """Pass a run-time worklist to the replayer, as ``run --worklist`` does."""
    from contextlib import contextmanager

    from openadapt_flow.runtime import Replayer

    @contextmanager
    def patch():
        original = Replayer.run

        def run(self, workflow, *args, **kwargs):
            kwargs.setdefault("worklists", worklists)
            return original(self, workflow, *args, **kwargs)

        Replayer.run = run  # type: ignore[method-assign]
        try:
            yield
        finally:
            Replayer.run = original  # type: ignore[method-assign]

    return patch()


def _run_one_batch(wf, installation, workflow, certified, config, hooks, setup, lookup,
                   items, out, reset) -> dict[str, Any]:
    """A loop workflow: one run processes every item; the oracle judges each."""
    from openadapt_flow.qualify.campaign import certified_run

    t0 = time.time()
    params = wf.batch_params(items)
    worklists = {}
    if getattr(workflow, "data_sources", None):
        rel = next(iter(workflow.data_sources))
        worklists = {rel: [wf.worklist_row(i) for i in items]}
    before = wf.snapshot()
    error = None
    try:
        with _with_worklists(worklists):
            summary = certified_run(
                workflow, certified, config, out=out / "batch-run", params=params,
                hooks=hooks, setup=setup, policy=wf.policy,
                run_id=f"{wf.id}-{installation}-batch-{int(t0)}", lookup=lookup,
            )
    except Exception as exc:  # noqa: BLE001 - a refused run is a result
        summary, error = {}, f"{type(exc).__name__}: {exc}"[:800]
    time.sleep(0.5)
    after = wf.snapshot()
    hooks.reset = reset
    engine = _engine_outcome(summary) if summary else "REFUSED"
    ids = {int(i["ticket"]) for i in items if str(i.get("ticket", "")).isdigit()}
    collateral = wf.judge_batch_collateral(before, after, ids) if hasattr(wf, "judge_batch_collateral") else []
    rows = []
    for n, item in enumerate(items, start=1):
        verdict = wf.judge(item, before, after)
        rows.append({
            "n": n, "item": item, "engine": engine, "oracle": verdict.verdict,
            "oracle_detail": verdict.detail, "failed_step_id": summary.get("failed_step_id"),
            "failed_error": summary.get("failed_error"), "model_calls": summary.get("model_calls"),
            "error": error, "seconds": 0.0,
        })
    result = _result(rows, out, t0)
    result["batch"] = {"params": params, "worklist_rows": sum(len(v) for v in worklists.values()),
                       "collateral": collateral, "engine": engine, "error": error}
    if collateral:
        result["all_verified"] = False
    (out / "batch.json").write_text(json.dumps(result, indent=2, default=str))
    return result
