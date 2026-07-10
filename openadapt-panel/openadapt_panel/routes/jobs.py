"""Generic job routes — list, inspect, live-stream, and stop background jobs."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["jobs"])


@router.get("/jobs")
def list_jobs(request: Request) -> dict:
    return {"jobs": request.app.state.jobs.list()}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return job.to_dict()


@router.post("/jobs/{job_id}/stop")
def stop_job(request: Request, job_id: str) -> dict:
    ok = request.app.state.jobs.stop(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job not found or not running.")
    return {"stopped": True}


@router.get("/jobs/{job_id}/stream")
async def stream_job(request: Request, job_id: str) -> StreamingResponse:
    """Server-Sent Events: incremental log lines + terminal status.

    EventSource can't set headers, so this route is reached with ?token=...
    (see auth.require_token). Emits one ``data:`` frame per new line, then a
    final frame with the job's terminal status.
    """
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")

    async def event_stream():
        sent = 0
        while True:
            if await request.is_disconnected():
                break
            snapshot = job.to_dict(tail=0)  # status only, cheap
            all_lines = job.to_dict(tail=10_000)["lines"]
            new = all_lines[sent:]
            for line in new:
                yield f"data: {json.dumps({'line': line})}\n\n"
            sent += len(new)
            if snapshot["status"] != "running":
                yield f"data: {json.dumps({'status': snapshot['status'], 'result': snapshot['result'], 'error': snapshot['error'], 'done': True})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
