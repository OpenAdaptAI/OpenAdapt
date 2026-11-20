"""``openadapt-verify audit --demo``: audit two scripts' success checks.

The demo starts the quickstart's synthetic REST store (``POST /items`` saves,
``GET /items`` lists every record) on 127.0.0.1 and audits one of two
deliberately simple scripts that save an item through the audit proxy:

- ``naive`` trusts the save's 2xx reply;
- ``readback`` then lists the items through the app and looks for its own.

Run either script on its own with ``python -m openadapt_verify.audit_demo
naive`` (it reads ``OPENADAPT_AUDIT_URL`` and ``OPENADAPT_AUDIT_VALUES``).
All data is synthetic and is deleted afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Optional, Sequence

SCRIPTS = ("naive", "readback")

#: What every run saves; ``{run}`` makes each run's item distinct.
PARAMS = {"code": "audit-item-{run}", "quantity": "3"}


def _post(url: str, values: dict[str, Any]) -> int:
    request = urllib.request.Request(
        url + "/items",
        data=json.dumps(values).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as reply:
        return reply.status


def naive() -> int:
    """Save, and call a 2xx reply success."""

    url = os.environ["OPENADAPT_AUDIT_URL"]
    values = json.loads(os.environ["OPENADAPT_AUDIT_VALUES"])
    try:
        status = _post(url, values)
    except OSError as exc:
        print(f"save failed: {exc}")
        return 1
    print(f"saved ({status})")
    return 0


def readback() -> int:
    """Save, then list the items through the app and look for this one."""

    url = os.environ["OPENADAPT_AUDIT_URL"]
    values = json.loads(os.environ["OPENADAPT_AUDIT_VALUES"])
    try:
        _post(url, values)
    except OSError as exc:
        # The reply may be lost after the save landed: look before failing.
        print(f"save reply failed ({exc}); checking the list")
    with urllib.request.urlopen(url + "/items", timeout=10) as reply:
        items = json.loads(reply.read())["items"]
    if any(
        item["code"] == values["code"] and item["quantity"] == values["quantity"]
        for item in items
    ):
        print("saved and found in the list")
        return 0
    print("not found in the list")
    return 1


def run(script: str = "naive", runs: Optional[int] = None) -> Any:
    from openadapt_verify import audit, quickstart

    if script not in SCRIPTS:
        raise ValueError(f"unknown demo script {script!r}; choose from {SCRIPTS}")
    with tempfile.TemporaryDirectory(prefix="openadapt-verify-audit-demo-") as tmp:
        store = quickstart.RestReadPath(Path(tmp))
        try:
            # The contract reads the store directly, not through the proxy.
            return audit.audit(
                store.contract(),
                PARAMS,
                [sys.executable, "-m", "openadapt_verify.audit_demo", script],
                upstream=store.server.base,
                runs=runs,
                progress=audit.print_progress,
            )
        finally:
            store.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    script = args[0] if args else "naive"
    return {"naive": naive, "readback": readback}[script]()


if __name__ == "__main__":
    raise SystemExit(main())
