"""Probe whether a lab system enforces If-Match, for the pack's
``precondition.enforcement`` record.

On a synthetic record: a PUT and a DELETE with a version that can't match
must be refused (the status is printed), and must change nothing; a PUT with
the version read must succeed, and a second PUT with that now-stale version
must be refused. Also prints how the audit log records the refused requests.
Run with the lab up: ``python benchmark/api_scoped/probe_precondition.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from lab import MedplumLab  # noqa: E402

WRONG = 'W/"openadapt-probe-0123456789abcdef"'


def main() -> int:
    lab = MedplumLab()
    env = lab.env()
    c = httpx.Client(
        base_url=env["MEDPLUM_URL"],
        headers={
            "Authorization": "Bearer " + env["MEDPLUM_TOKEN"],
            "Accept": "application/fhir+json",
        },
        timeout=30,
    )
    made = c.post(
        "/fhir/R4/Patient",
        json={"resourceType": "Patient", "name": [{"family": "Probe"}]},
    ).json()
    path = f"/fhir/R4/Patient/{made['id']}"
    read = c.get(path)
    etag = read.headers.get("ETag")
    body = {k: v for k, v in read.json().items() if k != "meta"}
    body["gender"] = "female"
    out: dict[str, object] = {"server": c.get("/healthcheck").json().get("version")}
    out["read_etag_present"] = bool(etag)
    out["put_wrong_version"] = c.put(path, json=body, headers={"If-Match": WRONG}).status_code
    out["unchanged_after_wrong_put"] = "gender" not in c.get(path).json()
    out["put_read_version"] = c.put(path, json=body, headers={"If-Match": etag}).status_code
    out["put_stale_version"] = c.put(path, json=body, headers={"If-Match": etag}).status_code
    out["delete_wrong_version"] = c.delete(path, headers={"If-Match": WRONG}).status_code
    out["present_after_wrong_delete"] = c.get(path).status_code == 200
    time.sleep(1.5)
    events = c.get(
        "/fhir/R4/AuditEvent",
        params={"entity": path.split("/R4/")[1], "_count": "50"},
    ).json()
    out["refused_requests_logged_as"] = sorted(
        {
            ",".join(s.get("code", "") for s in e["resource"].get("subtype", []))
            for e in events.get("entry", [])
            if e["resource"].get("outcome") not in (None, "0")
        }
    )
    c.delete(path)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
