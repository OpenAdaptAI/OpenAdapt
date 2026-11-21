"""Record a scripted HTTP demonstration as a HAR file (the trace input)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests


class Recorder:
    """A requests.Session whose every exchange becomes a HAR entry."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.session = requests.Session()
        self.session.headers.update(headers or {})
        self.entries: list[dict[str, Any]] = []
        self.session.hooks["response"].append(self._hook)

    def _hook(self, r: requests.Response, *args: Any, **kwargs: Any) -> None:
        req = r.request
        body = req.body.decode() if isinstance(req.body, bytes) else req.body
        mime = req.headers.get("Content-Type", "")
        entry: dict[str, Any] = {
            "request": {
                "method": req.method,
                "url": req.url,
                # Sign-in material is never kept in the trace file.
                "headers": [
                    {"name": k, "value": "REDACTED" if k.lower() in ("authorization", "cookie") else v}
                    for k, v in req.headers.items()
                ],
            },
            "response": {
                "status": r.status_code,
                "content": {"mimeType": r.headers.get("Content-Type", ""), "text": r.text[:20000]},
            },
        }
        if body is not None:
            entry["request"]["postData"] = {"mimeType": mime, "text": body}
        self.entries.append(entry)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps({"log": {"version": "1.2", "creator": {"name": "verify-draft-bench"}, "entries": self.entries}}, indent=1),
            encoding="utf-8",
        )
