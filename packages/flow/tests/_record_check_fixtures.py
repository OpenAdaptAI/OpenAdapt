"""Synthetic recordings for the record-check synthesis tests.

Each builder performs one save against a fixture system of record, then the
reads an app makes to show the saved record, and writes them as a recording:
``events.jsonl`` (the Save click), ``meta.json`` (the parameter values) and
``network.jsonl`` in the ``openadapt.network-capture/1`` format that
``record --capture-network`` writes. Made-up patients only.

A test helper (leading underscore: pytest does not collect it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import requests

SCHEMA = "openadapt.network-capture/1"
NOTE = "Synthetic triage note: follow up in two weeks"


class Capture:
    """Records requests the way the recorder's network capture lays them out."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def _line(
        self, method: str, url: str, resp: requests.Response, t: float, **extra: Any
    ) -> None:
        line: dict[str, Any] = {
            "method": method,
            "url": url,
            "resource_type": "xhr",
            "frame_url": url,
            "status": resp.status_code,
            "response_type": (resp.headers.get("content-type") or "").split(";")[0],
            "response_text": resp.text,
            "seq": len(self.lines),
            "t": t,
        }
        line.update(extra)
        self.lines.append(line)

    def get(
        self, url: str, t: float, headers: Optional[dict] = None
    ) -> requests.Response:
        resp = requests.get(url, timeout=5, headers=headers or {})
        self._line("GET", url, resp, t)
        return resp

    def post_json(self, url: str, body: dict, t: float) -> requests.Response:
        resp = requests.post(url, json=body, timeout=5)
        self._line(
            "POST", url, resp, t, request_type="application/json", body=json.dumps(body)
        )
        return resp

    def write(self, recording: Path) -> None:
        path = recording / "network.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"schema": SCHEMA, "exchanges": len(self.lines)}) + "\n"
            )
            for line in self.lines:
                fh.write(json.dumps(line, sort_keys=True) + "\n")


def write_recording(
    recording: Path,
    params: dict[str, str],
    *,
    save_t: float = 2.0,
    extra_events: Optional[list[dict]] = None,
) -> Path:
    recording.mkdir(parents=True, exist_ok=True)
    events = list(extra_events or []) + [
        {
            "i": len(extra_events or []),
            "kind": "click",
            "x": 300,
            "y": 400,
            "t": save_t,
            "structural": {"role": "button", "name": "Save encounter"},
        }
    ]
    (recording / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    (recording / "meta.json").write_text(
        json.dumps({"params": params}), encoding="utf-8"
    )
    return recording


def effect_e2e_recording(recording: Path, base_url: str) -> Path:
    """Save one encounter on the effect_e2e service; the app then shows the
    chart (GET /api/records) and its banner (GET /api/ui/last-save)."""

    requests.post(f"{base_url}/api/reset", json={}, timeout=5)
    cap = Capture()
    cap.get(f"{base_url}/api/records", 1.0)
    cap.post_json(
        f"{base_url}/api/encounter",
        {"patient_id": "p1", "type": "Triage", "note": NOTE},
        2.1,
    )
    cap.get(f"{base_url}/api/ui/last-save", 2.4)
    cap.get(f"{base_url}/api/records", 2.6)
    write_recording(recording, {"patient_id": "p1", "note": NOTE})
    cap.write(recording)
    return recording


def mockmed_recording(recording: Path, base_url: str, db: Any) -> Path:
    """Save one encounter on the MockMed fault server; the app reads /api/db."""

    db.reset()
    cap = Capture()
    base = base_url.rstrip("/")
    cap.get(f"{base}/api/db", 1.0)
    cap.post_json(
        f"{base}/api/encounter",
        {"patient_id": "p1", "type": "Triage", "note": NOTE},
        2.1,
    )
    cap.get(f"{base}/api/db", 2.5)
    write_recording(recording, {"patient_id": "p1", "note": NOTE})
    cap.write(recording)
    return recording
