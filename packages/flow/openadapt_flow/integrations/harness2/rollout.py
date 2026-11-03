"""One GUI rollout: fresh app copy, agent, read-back, files on disk.

The order is fixed, because the reward depends on it:

1. start a fresh copy of the app;
2. register the episode with the reward worker, which reads the baseline;
3. let the agent drive the copy until it reports or runs out of steps;
4. read the store again and score;
5. stop the copy.

The run directory holds everything a later reader needs and nothing more:

* ``run.json``: task, agent, harness digest, report, signal, cost, timing;
* ``steps.jsonl``: every observe-act step as the agent saw it;
* ``report.md``: the agent's own final report (its "deliverable");
* ``readback.json``: the read-back and score (:class:`~.scorer.EpisodeScore`);
* ``reward/``: the worker's signed receipt and evidence.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.integrations.harness2.agent import AgentResult, GuiAgent, PageDriver
from openadapt_flow.integrations.harness2.apps import AppTarget
from openadapt_flow.integrations.harness2.scorer import (
    EpisodeScore,
    Penalties,
    ReadBackScorer,
)
from openadapt_flow.integrations.harness2.tasks import GuiTask

RUN_FILE = "run.json"
STEPS_FILE = "steps.jsonl"
REPORT_FILE = "report.md"
READBACK_FILE = "readback.json"


def harness_digest(harness_text: str) -> str:
    return "sha256:" + hashlib.sha256(harness_text.encode("utf-8")).hexdigest()


def episode_id_for(run_id: str) -> str:
    """A reward-worker episode id (opaque id pattern) for a Harness² run id."""

    return "episode_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def is_complete(run_dir: Path) -> bool:
    return (run_dir / RUN_FILE).is_file() and (run_dir / READBACK_FILE).is_file()


def _reusable(run_dir: Path, task: GuiTask, harness_text: str) -> bool:
    """A finished run of this task, under this harness, that did not crash."""

    if not is_complete(run_dir):
        return False
    try:
        run = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        run.get("task_id") == task.task_id
        and run.get("harness_digest") == harness_digest(harness_text)
        and not run.get("error")
        and run.get("runtime_signal") != "failed_platform"
    )


def run_rollout(
    task: GuiTask,
    harness_text: str,
    agent: GuiAgent,
    run_dir: Path,
    *,
    target: AppTarget,
    run_id: str,
    penalties: Penalties = Penalties(),
    headless: bool = True,
    driver_factory: Optional[Callable[[Any], Any]] = None,
) -> Path:
    """Run one rollout into ``run_dir``; idempotent by completed run directory.

    ``driver_factory(page)`` picks the surface the agent sees: the DOM
    (:class:`~.agent.PageDriver`, the default) or pixels only
    (:class:`~.pixels.PixelDriver` over a Flow backend).
    """

    run_dir = Path(run_dir)
    if _reusable(run_dir, task, harness_text):
        return run_dir
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    episode_id = episode_id_for(run_id)
    started = time.time()
    session = target.start()
    result: Optional[AgentResult] = None
    surface = ""
    error = ""
    try:
        scorer = ReadBackScorer(
            task, session.oracle_recipe(), run_dir / "reward", penalties=penalties
        )
        scorer.begin(episode_id)
        try:
            result, surface = _drive(
                agent,
                task,
                harness_text,
                session.entry_url(task),
                headless,
                driver_factory or PageDriver,
            )
        except Exception as exc:  # noqa: BLE001 - a dead agent is still scored
            error = f"{type(exc).__name__}: {exc}"
        signal = result.runtime_signal if result is not None else "failed_platform"
        score = scorer.score(episode_id, signal)
    finally:
        session.stop()
        drop_worker_secrets(run_dir / "reward" / "worker")
    _write(
        run_dir,
        task,
        agent,
        harness_text,
        run_id,
        result,
        score,
        error,
        started,
        surface,
    )
    return run_dir


def drop_worker_secrets(worker_dir: Path) -> None:
    """Delete the worker's per-rollout signing key and bearer token.

    Each rollout's worker is used once. The receipts it signed stay
    verifiable with ``keys/ed25519.pub``; the private key and token have no
    further use, and leaving them in a run directory invites committing them.
    """

    for rel in ("keys/ed25519.pem", "token"):
        (worker_dir / rel).unlink(missing_ok=True)


def _drive(
    agent: GuiAgent,
    task: GuiTask,
    harness_text: str,
    url: str,
    headless: bool,
    driver_factory: Callable[[Any], Any],
) -> tuple[AgentResult, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            from openadapt_flow.backends.playwright_backend import prepare_ws_guard

            # Guard the page's WebSockets from its first document (wire check).
            prepare_ws_guard(page)
            page.goto(url)
            page.wait_for_selector("#app *", timeout=10_000)
            driver = driver_factory(page)
            surface = str(getattr(driver, "surface", "dom"))
            return agent.run(driver, task.prompt(), harness_text), surface
        finally:
            browser.close()


def _write(
    run_dir: Path,
    task: GuiTask,
    agent: GuiAgent,
    harness_text: str,
    run_id: str,
    result: Optional[AgentResult],
    score: EpisodeScore,
    error: str,
    started: float,
    surface: str = "",
) -> None:
    steps = result.steps if result is not None else []
    with (run_dir / STEPS_FILE).open("w", encoding="utf-8") as fh:
        for step in steps:
            fh.write(json.dumps(asdict(step)) + "\n")
    if result is not None and result.finished:
        (run_dir / REPORT_FILE).write_text(
            f"status: {result.status}\n\n{result.report}\n", encoding="utf-8"
        )
    (run_dir / READBACK_FILE).write_text(
        json.dumps(score.to_dict(), indent=2), encoding="utf-8"
    )
    run: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task.task_id,
        "agent": agent.name,
        "surface": surface,
        "harness_digest": harness_digest(harness_text),
        "status": result.status if result is not None else "crashed",
        "report": result.report if result is not None else "",
        "runtime_signal": result.runtime_signal
        if result is not None
        else "failed_platform",
        "steps": len(steps),
        "agent_cost_usd": round(result.cost_usd, 6) if result is not None else 0.0,
        "agent_model_calls": result.model_calls if result is not None else 0,
        "wall_s": round(time.time() - started, 2),
        "error": error,
    }
    # Written last: its presence marks the run complete.
    (run_dir / RUN_FILE).write_text(json.dumps(run, indent=2), encoding="utf-8")


def load_run(run_dir: Path) -> dict[str, Any]:
    return dict(json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8")))


def load_score(run_dir: Path) -> EpisodeScore:
    data = json.loads((Path(run_dir) / READBACK_FILE).read_text(encoding="utf-8"))
    return EpisodeScore.from_dict(data)


def load_steps(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / STEPS_FILE
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


__all__ = [
    "drop_worker_secrets",
    "READBACK_FILE",
    "REPORT_FILE",
    "RUN_FILE",
    "STEPS_FILE",
    "episode_id_for",
    "harness_digest",
    "is_complete",
    "load_run",
    "load_score",
    "load_steps",
    "run_rollout",
]
