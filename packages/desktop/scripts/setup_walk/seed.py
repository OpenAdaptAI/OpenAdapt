"""Record the MockMed triage demonstration and give it to a fresh engine.

The recording is the one Flow's tutorial makes (``openadapt_flow.tutorial``):
MockMed served through its transactional fault server with ``?fault=ok``, so
a save reaches a backend store. The person's part (open the referral, start a
triage encounter, type the note, save) goes through Flow's ordinary
``Recorder``.

Desktop's recorder does not yet keep the app's network traffic
(``record --capture-network`` is a separate change), and MockMed's page never
reads its store back after a save. So the walk writes ``network.jsonl`` in
the ``openadapt.network-capture/1`` format itself: the app's real save
request, captured from the browser, followed by one read of ``GET /api/db``
made right after the save. That read stands in for the screen an app shows
after saving; Flow's own record-check tests build the same MockMed recording
(``tests/_record_check_fixtures.py::mockmed_recording``). Nothing else is
added. All data is synthetic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

SCHEMA = "openadapt.network-capture/1"
NOTE = "Synthetic follow-up in two weeks"
ENTRY_QUERY = "?fault=ok&idempotency=demo#tasks"
TASK = "Add a triage note"


def _get(url: str) -> tuple[int, str, str]:
    with urlopen(Request(url), timeout=10) as response:  # noqa: S310 - loopback
        return response.status, response.headers.get("content-type", ""), response.read().decode()


def _post(url: str, body: dict) -> None:
    data = json.dumps(body).encode()
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10):  # noqa: S310 - loopback
        pass


def record(base_url: str, recording: Path) -> Path:
    """Record the demonstration with the app's save request captured."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    base = base_url.rstrip("/")
    _post(f"{base}/api/reset", {})
    entry = f"{base}/{ENTRY_QUERY}"
    backend, close = PlaywrightBackend.launch(entry, headless=True)
    started = time.monotonic()
    lines: list[dict[str, Any]] = []

    def on_finished(request: Any) -> None:
        if "/api/" not in request.url:
            return
        response = request.response()
        line = {
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "frame_url": request.frame.url,
            "status": response.status if response else 0,
            "response_type": (
                (response.headers.get("content-type") if response else "") or ""
            ).split(";")[0],
            "response_text": response.text() if response else "",
            "seq": len(lines),
            "t": round(time.monotonic() - started, 3),
        }
        if request.post_data:
            line["request_type"] = "application/json"
            line["body"] = request.post_data
        lines.append(line)

    try:
        page = backend.page
        page.on("requestfinished", on_finished)
        recorder = Recorder(backend, recording, app_url=entry)
        # Stamp requests on the recorder's own clock, as the recorder's
        # network capture does, so a request lines up with its step.
        started = getattr(recorder, "_t0", started)

        def center(selector: str) -> tuple[int, int]:
            locator = page.locator(selector).first
            locator.wait_for(state="visible")
            box = locator.bounding_box()
            assert box is not None
            return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)

        recorder.click(*center(".open-btn"))
        recorder.click(*center("#new-encounter"))
        recorder.click(*center("#type-triage"))
        recorder.click(*center("#note"))
        recorder.type_text(NOTE, param="note")
        recorder.click(*center("#save-encounter"))
        page.wait_for_selector("#saved-banner", state="visible")
        page.wait_for_timeout(300)
        recorder.finish()
    finally:
        close()

    # The read after the save (see the module docstring).
    status, content_type, text = _get(f"{base}/api/db")
    lines.append(
        {
            "method": "GET",
            "url": f"{base}/api/db",
            "resource_type": "xhr",
            "frame_url": entry,
            "status": status,
            "response_type": content_type.split(";")[0],
            "response_text": text,
            "seq": len(lines),
            "t": round(time.monotonic() - started, 3),
        }
    )
    with (recording / "network.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": SCHEMA, "exchanges": len(lines)}) + "\n")
        for line in lines:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    return recording


def seed(bridge: Any, recording: Path, base_url: str, capture_id: str = "triage-note") -> str:
    """Register the recording as a finished capture and compile it."""

    db = bridge.db
    db.insert_capture(capture_id, str(recording), "2026-10-19T00:00:00Z", task_description=TASK)
    db.set_task_target(
        capture_id, {"backend": "web", "url": f"{base_url.rstrip('/')}/{ENTRY_QUERY}"}
    )
    result = bridge.invoke("compile_recording", {"capture_id": capture_id})
    if not result.get("ok"):
        raise SystemExit(f"compile failed: {result}")
    return str(result["workflow_id"])
