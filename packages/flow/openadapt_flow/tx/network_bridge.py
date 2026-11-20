"""Bridge OpenAdapt's ``network.jsonl`` (``openadapt.network-capture/1``) to HAR 1.2.

Connects ``openadapt-flow record --backend web --capture-network`` recordings
directly into ``openadapt_flow.tx.induce.induce_lens_from_har`` without
duplicating the recorder's capture format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence, Union
from urllib.parse import parse_qsl, urlencode, urlsplit

NETWORK_FILE = "network.jsonl"
NETWORK_SCHEMA = "openadapt.network-capture/1"


def network_capture_entries_to_har(
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Convert ``openadapt.network-capture/1`` exchange dicts into a HAR 1.2 document."""
    har_entries: list[dict[str, Any]] = []
    for item in entries:
        if item.get("failed"):
            continue
        method = str(item.get("method") or "GET").upper()
        url = str(item.get("url") or "")
        if not url:
            continue
        parsed = urlsplit(url)
        query_list = [
            {"name": k, "value": v}
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        ]

        req_mime = str(item.get("request_type") or "")
        post_data: dict[str, Any] | None = None
        if "form" in item and isinstance(item["form"], list):
            pairs = [
                (str(pair[0]), str(pair[1]))
                for pair in item["form"]
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
            post_data = {
                "mimeType": req_mime or "application/x-www-form-urlencoded",
                "text": urlencode(pairs),
                "params": [{"name": k, "value": v} for k, v in pairs],
            }
        elif isinstance(item.get("body"), str):
            post_data = {
                "mimeType": req_mime or "application/json",
                "text": item["body"],
            }

        req_headers = [
            {"name": str(h), "value": "<redacted>"}
            for h in (item.get("request_headers") or [])
        ]
        if req_mime:
            req_headers.append({"name": "Content-Type", "value": req_mime})

        request_obj: dict[str, Any] = {
            "method": method,
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": req_headers,
            "queryString": query_list,
            "headersSize": -1,
            "bodySize": len(post_data["text"]) if post_data else 0,
        }
        if post_data is not None:
            request_obj["postData"] = post_data

        resp_headers: list[dict[str, str]] = []
        resp_mime = str(item.get("response_type") or "application/json")
        if resp_mime:
            resp_headers.append({"name": "Content-Type", "value": resp_mime})
        if isinstance(item.get("location"), str):
            resp_headers.append({"name": "Location", "value": item["location"]})

        resp_text = str(item.get("response_text") or "")
        response_obj: dict[str, Any] = {
            "status": int(item["status"]) if item.get("status") is not None else 0,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": resp_headers,
            "content": {
                "size": len(resp_text.encode("utf-8")),
                "mimeType": resp_mime,
                "text": resp_text,
            },
            "redirectURL": str(item.get("location") or ""),
            "headersSize": -1,
            "bodySize": len(resp_text.encode("utf-8")),
        }

        har_entries.append(
            {
                "startedDateTime": "1970-01-01T00:00:00Z",
                "time": 0,
                "request": request_obj,
                "response": response_obj,
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
            }
        )

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "openadapt-flow-network-bridge", "version": "1.0"},
            "entries": har_entries,
        }
    }


def load_network_capture_as_har(path: Union[str, Path]) -> dict[str, Any]:
    """Load either a recording directory containing ``network.jsonl`` or a ``network.jsonl`` file."""
    target = Path(path)
    if target.is_dir():
        target = target / NETWORK_FILE
    if not target.is_file():
        raise FileNotFoundError(f"network capture not found: {target}")

    lines = [
        line.strip()
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        raise ValueError(f"empty network capture: {target}")

    header = json.loads(lines[0])
    if not isinstance(header, dict) or header.get("schema") != NETWORK_SCHEMA:
        raise ValueError(
            f"unsupported network capture schema in {target}: {header.get('schema')!r}"
        )

    exchanges = [json.loads(line) for line in lines[1:]]
    return network_capture_entries_to_har(exchanges)
