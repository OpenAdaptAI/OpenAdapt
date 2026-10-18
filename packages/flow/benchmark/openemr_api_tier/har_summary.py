"""Write a redacted summary of the capture HAR (safe to commit).

Keeps method, path, query, status, content type, sizes and timing for every
non-static request, and the full field list of the duplicate check and the
save. Drops cookies and headers, and replaces the sign-in username and
password and every CSRF/session token with placeholders. All patient values
are synthetic.

    python -m benchmark.openemr_api_tier.har_summary <referral.har> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse as up
from pathlib import Path

from benchmark.openemr_api_tier.formtier import _STATIC

_TOKEN_KEYS = re.compile(r"csrf|token|pass|authuser|session", re.I)


def _clean(pairs: list[tuple[str, str]]) -> list[list[str]]:
    return [[k, "<redacted>" if _TOKEN_KEYS.search(k) else v] for k, v in pairs]


def main(har_path: str, out_path: str) -> None:
    har = json.loads(Path(har_path).read_text())
    t0 = None
    rows = []
    for e in har["log"]["entries"]:
        r, s = e["request"], e["response"]
        if _STATIC.search(r["url"]):
            continue
        u = up.urlsplit(r["url"])
        started = e["startedDateTime"]
        t0 = t0 or started
        pd = r.get("postData") or {}
        body = up.parse_qsl(pd.get("text") or "", keep_blank_values=True) if "urlencoded" in (pd.get("mimeType") or "") else []
        rows.append({
            "started": started[11:23], "ms": round(e.get("time") or 0, 1), "method": r["method"], "path": u.path,
            "query": _clean(up.parse_qsl(u.query, keep_blank_values=True)), "status": s["status"],
            "request_type": (pd.get("mimeType") or "").split(";")[0], "request_bytes": len(pd.get("text") or ""),
            "response_type": (s.get("content", {}).get("mimeType") or "").split(";")[0],
            "response_bytes": s.get("content", {}).get("size"),
            "body": _clean(body) if body else None,
        })
    Path(out_path).write_text(json.dumps({"requests": rows, "static_requests_dropped":
                                          len(har["log"]["entries"]) - len(rows)}, indent=1) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
