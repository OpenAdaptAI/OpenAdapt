"""MockMed: the CI-size smoke task. Add a triage note to the first referral.

MockMed is the synthetic clinic app bundled with openadapt-flow
(``openadapt_flow.mockmed``). It runs in-process, so this task needs no
Docker. Its fault server injects the break-it faults on the write:

* ``optimistic``: the server rejects the save, but the app already shows
  "saved" (a save that does not land);
* ``wrong_record``: the save lands on another patient.

and ``?drift=rename`` relabels the buttons ("Save Encounter" becomes
"Submit Encounter", "Open" becomes "View"): the UI change.

The read-back is ``GET /api/db``, the store the app writes to, read out of
band from the screen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

NAME = "mockmed"
APP = "a clinic's electronic medical record (MockMed)"
TASK_ID = "mockmed.add_note"
INSTRUCTION = (
    "Open the first referral, start a Triage encounter, and save this note: {note}"
)
PARAMS = {"note": str}

# Item schedule for a batch (1-based). Faults are on the write; the UI change
# stays on from its item to the end of the batch, like a vendor release.
FAULT_ITEMS = {30: "optimistic", 40: "wrong_record"}
UI_CHANGE_AT = 50
UI_CHANGE = "rename"
PORT = 18765


@dataclass
class Item:
    item: int
    values: dict[str, str]
    fault: str  # none | optimistic | wrong_record | ui_change
    query_fault: str  # what the fault server is asked for
    drift: str


def item(
    i: int, *, faults: bool = True, ui_change_at: Optional[int] = UI_CHANGE_AT
) -> Item:
    note = f"Synthetic follow-up note {i}: recheck in {7 + i % 14} days"
    qfault = FAULT_ITEMS.get(i, "ok") if faults else "ok"
    drift = UI_CHANGE if ui_change_at and i >= ui_change_at else ""
    fault = qfault if qfault != "ok" else ("ui_change" if drift else "none")
    return Item(
        item=i, values={"note": note}, fault=fault, query_fault=qfault, drift=drift
    )


class Server:
    """One MockMed fault server for a batch."""

    def __init__(self, port: int = PORT) -> None:
        from openadapt_flow.mockmed.fault_server import serve

        # A fixed port: an admission is bound to the app's origin, so the
        # program arm must run on the origin its canary ran on.
        base, self.db, self._stop = serve(port=port)
        self.base = base.rstrip("/")

    def url(self, it: Item) -> str:
        drift = f"&drift={it.drift}" if it.drift else ""
        return f"{self.base}/?fault={it.query_fault}&idempotency=demo{drift}#tasks"

    def records(self) -> list[dict[str, Any]]:
        snap = _json(f"{self.base}/api/db")
        return [r for r in (snap or {}).get("records", []) if isinstance(r, dict)]

    def reset(self) -> None:
        _json(f"{self.base}/api/reset", method="POST", body={})

    def close(self) -> None:
        self._stop()


def _json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = Request(  # noqa: S310 - loopback only
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urlopen(req, timeout=10) as resp:  # noqa: S310
        payload = resp.read().decode()
    return json.loads(payload) if payload else None


def check(records: list[dict[str, Any]], values: dict[str, str]) -> str:
    """``correct`` | ``missing`` | ``duplicate`` | ``wrong`` (after a reset)."""

    if not records:
        return "missing"
    good = [
        r
        for r in records
        if r.get("patient_id") == "p1"
        and r.get("type") == "Triage"
        and r.get("note") == values["note"]
    ]
    if len(good) == 1 and len(records) == 1:
        return "correct"
    if len(good) > 1:
        return "duplicate"
    return "wrong"


def setup(page: Any) -> None:
    """No sign-in: MockMed opens on its task list."""

    page.wait_for_load_state("networkidle")


def instruction(values: dict[str, str]) -> str:
    return INSTRUCTION.format(**values)


SetupFn = Callable[[Any], None]
