"""Wait until the stub control plane saw poll -> failed callback -> ack.

Exits 0 once the request log shows, in order, a poll, a ``failed`` run
callback for the stub's ungoverned job, and a ``failed`` ack of that job whose
error names the missing safety policy. Exits 1 on timeout or a wrong outcome.
Also fails if the given token appears anywhere in the log.

    python wait_for_stub.py --log requests.json --timeout 120 --token-env CI_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

JOB_ID = "bjob_ungoverned_1"


def check(records: list[dict[str, Any]]) -> tuple[bool, str]:
    paths = [r.get("path") for r in records]
    if "/api/connector/poll" not in paths:
        return False, "no poll yet"
    first_poll = paths.index("/api/connector/poll")
    callbacks = [i for i, p in enumerate(paths) if p == "/api/internal/run-callback"]
    acks = [i for i, p in enumerate(paths) if p == "/api/connector/ack"]
    if not callbacks:
        return False, f"{paths.count('/api/connector/poll')} polls, no callback yet"
    if not acks:
        return False, "callback seen, no ack yet"
    cb, ack = callbacks[0], acks[0]
    if not first_poll < cb < ack:
        raise SystemExit(f"requests out of order: {paths}")
    cb_body = records[cb].get("body") or {}
    ack_body = records[ack].get("body") or {}
    if cb_body.get("status") != "failed":
        raise SystemExit(f"callback status is not failed: {cb_body}")
    if "x-run-token" not in records[cb].get("headers", []):
        raise SystemExit("callback carried no x-run-token header")
    if ack_body.get("job_id") != JOB_ID or ack_body.get("status") != "failed":
        raise SystemExit(f"unexpected ack: {ack_body}")
    if "safety" not in (ack_body.get("error") or ""):
        raise SystemExit(f"ack error does not name the safety refusal: {ack_body}")
    return True, f"poll -> failed callback -> ack after {len(records)} requests"


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--log", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--token-env", default=None)
    args = parser.parse_args()
    log = Path(args.log)
    token = os.environ.get(args.token_env) if args.token_env else None
    deadline = time.monotonic() + args.timeout
    status = "no log yet"
    while time.monotonic() < deadline:
        try:
            text = log.read_text(encoding="utf-8")
            records = json.loads(text)
        except (OSError, ValueError):
            time.sleep(1)
            continue
        if token and token in text:
            print("the connector token appears in the stub log", file=sys.stderr)
            return 1
        done, status = check(records)
        if done:
            print(status)
            return 0
        time.sleep(1)
    print(f"timed out after {args.timeout:.0f} s: {status}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
