"""Eval routes — mock and live benchmark runs.

Seam parity with ``openadapt eval mock/run`` in openadapt/cli.py: same
openadapt_evals symbols (SmartMockAgent / ApiAgent / PolicyAgent, WAAMockAdapter
/ WAALiveAdapter, evaluate_agent_on_benchmark, compute_metrics) and the same
max_steps=15. Runs in a worker thread (it needs structured metrics back) tracked
as a job so the UI can poll/stream progress.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from ..deps import ensure_installed
from ..jobs import Job

router = APIRouter(tags=["eval"])

_MAX_STEPS = 15


@router.post("/eval/mock")
def run_mock(request: Request, tasks: int = Body(10, embed=True)) -> dict:
    ensure_installed("openadapt_evals", "openadapt-evals")

    def target(job: Job) -> dict:
        from openadapt_evals import (
            SmartMockAgent,
            WAAMockAdapter,
            compute_metrics,
            evaluate_agent_on_benchmark,
        )

        job.append(f"Running mock evaluation with {tasks} tasks...")
        agent = SmartMockAgent()
        adapter = WAAMockAdapter(num_tasks=tasks)
        results = evaluate_agent_on_benchmark(agent, adapter, max_steps=_MAX_STEPS)
        metrics = compute_metrics(results)
        job.append("Done.")
        return dict(metrics)

    job = request.app.state.jobs.start_thread(
        kind="eval", title=f"Mock eval ({tasks} tasks)", target=target
    )
    return {"job_id": job.id}


@router.post("/eval/run")
def run_eval(
    request: Request,
    checkpoint: str | None = Body(None),
    agent: str | None = Body(None),
    benchmark: str = Body("waa"),
    tasks: int = Body(10),
    server: str | None = Body(None),
    demo: str | None = Body(None),
) -> dict:
    ensure_installed("openadapt_evals", "openadapt-evals")
    if not checkpoint and not agent:
        raise HTTPException(
            status_code=400, detail="Specify either 'checkpoint' or 'agent'."
        )

    def target(job: Job) -> dict:
        from openadapt_evals import (
            ApiAgent,
            WAALiveAdapter,
            WAAMockAdapter,
            compute_metrics,
            evaluate_agent_on_benchmark,
        )

        if checkpoint:
            job.append(f"Loading model from: {checkpoint}")
            from openadapt_evals import PolicyAgent

            eval_agent = PolicyAgent(checkpoint_path=checkpoint)
        else:
            provider = "anthropic" if "claude" in (agent or "") else "openai"
            job.append(f"Using API agent: {provider}")
            demo_text = None
            if demo:
                with open(demo) as f:
                    demo_text = f.read()
            eval_agent = ApiAgent(provider=provider, demo=demo_text)

        if server:
            job.append(f"Connecting to: {server}")
            adapter = WAALiveAdapter(server_url=server)
        else:
            job.append(f"Using mock adapter with {tasks} tasks")
            adapter = WAAMockAdapter(num_tasks=tasks)

        job.append("Running evaluation...")
        results = evaluate_agent_on_benchmark(eval_agent, adapter, max_steps=_MAX_STEPS)
        metrics = compute_metrics(results)
        job.append("Done.")
        return dict(metrics)

    label = checkpoint or agent or "eval"
    job = request.app.state.jobs.start_thread(
        kind="eval", title=f"Eval: {label}", target=target
    )
    return {"job_id": job.id}
