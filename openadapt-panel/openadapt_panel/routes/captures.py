"""Captures routes — list recordings, generate/serve the HTML viewer, record.

Seam parity with ``openadapt capture list/view/start`` in openadapt/cli.py:
listing uses ``openadapt_capture.Capture`` and the ``recording.db`` marker;
the viewer uses ``openadapt_capture.create_html``. Recording is long-running so
it runs as a CLI subprocess job (stop via signal), mirroring the CLI's Ctrl+C.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from ..deps import ensure_installed
from ..jobs import cli_argv

router = APIRouter(tags=["captures"])


def _default_capture_root() -> str:
    """Where to look for captures — settings.capture_dir if resolvable, else '.'.

    Mirrors the CLI's ``--path`` default of '.' when config isn't importable.
    """
    try:
        from openadapt.config import settings

        root = Path(settings.capture_dir)
        if root.exists():
            return str(root)
    except Exception:
        pass
    return "."


@router.get("/captures")
def list_captures(path: str | None = None) -> dict:
    ensure_installed("openadapt_capture", "openadapt-capture")
    from openadapt_capture import Capture

    root = Path(path or _default_capture_root())
    captures: list[dict] = []
    if root.exists():
        for capture_dir in sorted(root.iterdir()):
            if not (capture_dir.is_dir() and (capture_dir / "recording.db").exists()):
                continue
            try:
                cap = Capture.load(str(capture_dir))
                n_actions = sum(1 for _ in cap.actions())
                captures.append(
                    {
                        "name": capture_dir.name,
                        "path": str(capture_dir),
                        "actions": n_actions,
                        "description": cap.task_description or "",
                        "has_viewer": (capture_dir / "viewer.html").exists(),
                    }
                )
                cap.close()
            except Exception:
                continue
    return {"root": str(root), "captures": captures}


@router.post("/captures/viewer")
def build_viewer(path: str = Body(..., embed=True)) -> dict:
    ensure_installed("openadapt_capture", "openadapt-capture")
    from openadapt_capture import create_html

    capture_path = Path(path)
    if not capture_path.exists():
        raise HTTPException(status_code=404, detail=f"Capture not found: {path}")

    output_path = capture_path / "viewer.html"
    create_html(str(capture_path), str(output_path))
    return {"viewer_url": f"/api/captures/viewer/file?path={capture_path}"}


@router.get("/captures/viewer/file")
def serve_viewer(path: str) -> FileResponse:
    viewer = Path(path) / "viewer.html"
    if not viewer.exists():
        raise HTTPException(status_code=404, detail="viewer.html not generated yet")
    return FileResponse(viewer)


@router.post("/captures/record")
def start_recording(
    request: Request,
    name: str = Body(...),
    video: bool = Body(True),
    audio: bool = Body(False),
) -> dict:
    ensure_installed("openadapt_capture", "openadapt-capture")
    args = ["capture", "start", "--name", name]
    args.append("--video" if video else "--no-video")
    args.append("--audio" if audio else "--no-audio")
    job = request.app.state.jobs.start_process(
        kind="capture",
        title=f"Recording: {name}",
        argv=cli_argv(*args),
    )
    return {"job_id": job.id}
