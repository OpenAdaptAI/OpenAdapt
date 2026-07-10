"""Train routes — start (subprocess), status (training_log.json), stop (sentinel).

Training runs as an ``openadapt train start`` subprocess so the panel reuses the
CLI's tested seam with openadapt-ml rather than importing it in-process. Status
tails the ``training_log.json`` the training loop already writes; stop writes the
``STOP_TRAINING`` sentinel openadapt-ml honors (clean stop) — same as
``openadapt train stop``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Body, Request

from ..deps import ensure_installed
from ..jobs import cli_argv

router = APIRouter(tags=["train"])


@router.post("/train/start")
def start_training(
    request: Request,
    capture: str = Body(...),
    config: str | None = Body(None),
    model: str | None = Body(None),
    output: str = Body("training_output"),
) -> dict:
    ensure_installed("openadapt_ml", "openadapt-ml")
    args = ["train", "start", "--capture", capture, "--output", output, "--no-open"]
    if config:
        args += ["--config", config]
    if model:
        args += ["--model", model]

    job = request.app.state.jobs.start_process(
        kind="train",
        title=f"Training on {capture}",
        argv=cli_argv(*args),
        stop_sentinel=Path(output) / "STOP_TRAINING",
    )
    return {"job_id": job.id, "output": output}


@router.get("/train/status")
def training_status(output: str = "training_output") -> dict:
    log_path = Path(output) / "training_log.json"
    if not log_path.exists():
        return {"active": False}
    try:
        with open(log_path) as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"active": True, "error": f"could not read log: {exc}"}
    return {"active": True, **log}


@router.post("/train/stop")
def stop_training(output: str = Body("training_output", embed=True)) -> dict:
    stop_file = Path(output) / "STOP_TRAINING"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.touch()
    return {"stopped": True, "detail": "Training will stop after the current step."}
