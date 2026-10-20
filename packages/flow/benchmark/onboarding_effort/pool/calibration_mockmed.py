"""Harness calibration only (not in the pool): MockMed triage note.

MockMed's triage save is the repository's tutorial workflow, so it is tuned
and excluded from every result. It exercises the implementer end to end
without a Docker fixture.
"""

from __future__ import annotations

import json
import random
from typing import Any
from urllib.request import Request, urlopen

from benchmark.onboarding_effort.pool.base import FIRST, Verdict, Workflow, click, fill

PORT = 9490
BASE = f"http://127.0.0.1:{PORT}"
_server: Any = None

NOTES = [
    "Synthetic follow-up in ten days",
    "Synthetic recheck blood pressure",
    "Synthetic review lab panel",
    "Synthetic call back next week",
    "Synthetic refer to physio",
]


def ensure_server() -> None:
    global _server
    if _server is not None:
        return
    try:
        urlopen(BASE + "/api/db", timeout=2).read()
        return  # already served (another process)
    except Exception:  # noqa: BLE001
        pass
    from openadapt_flow.mockmed.fault_server import serve

    _server = serve(PORT)


def _json(path: str, body: Any = None) -> Any:
    req = Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class MockMedTriage(Workflow):
    id = "cal-mockmed-triage"
    app = "mockmed"
    policy = "clinical-write"
    shape = "single"
    inputs = ("note",)
    intent = "Open Jane Sample's referral, start a Triage encounter, save the note."
    second_installation = "screen 1600x1000 at device scale 2"
    second_kind = "screen"
    # ?idempotency=demo keeps one triage encounter per patient.
    heldout_reset_each = True

    def installation_viewport(self, installation: str) -> tuple[int, int]:
        return (1600, 1000) if installation != FIRST else self.viewport

    def device_scale(self, installation: str) -> float:
        return 2.0 if installation != FIRST else 1.0

    def reset(self, installation: str) -> None:
        ensure_server()
        _json("/api/reset", {})

    def entry_url(self, installation: str) -> str:
        return BASE + "/?fault=ok&idempotency=demo#tasks"

    def start(self, page: Any, installation: str) -> None:
        page.goto(self.entry_url(installation))
        page.wait_for_selector(".open-btn")

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        return {"note": f"{rng.choice(NOTES)} {split} {n:02d} {rng.randrange(10**6):06d}"}

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        click(page, rec, ".open-btn")
        click(page, rec, "#new-encounter")
        click(page, rec, "#type-triage")
        fill(page, rec, "#note", item["note"], "note")
        click(page, rec, "#save-encounter")
        page.wait_for_selector("#saved-banner", state="visible")

    def verify_yaml(self, installation: str) -> str:
        return f"""schema_version: openadapt.verify/v1
app:
  name: MockMed (harness calibration)
  url: {self.entry_url(installation)}
  version: mockmed@repo
  session: benchmark.onboarding_effort.hooks:open_session
  reset:
    http: {{method: POST, url: {BASE}/api/reset, body: {{}}}}
verifier:
  kind: rest
  base_url: {BASE}
  records_path: /api/db
  records_key: records
record:
  name: encounter
  match: [note]
  duplicate_key: note
  fields:
    note: note
    patient_id: {{literal: p1}}
    type: {{literal: Triage}}
save_step: auto
inputs:
  source: recorded
campaign:
  id: onboarding-cal-mockmed
  trials: 3
  policy: clinical-write
"""

    def snapshot(self) -> Any:
        return _json("/api/db")["records"]

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        old = {r["id"] for r in before}
        new = [r for r in after if r["id"] not in old]
        mine = [r for r in new if r.get("note") == item["note"]]
        changed = [r for r in after if r["id"] in old and r not in before]
        if not mine:
            return Verdict("MISSING", {"new": len(new)})
        ok = (
            len(mine) == 1
            and mine[0].get("patient_id") == "p1"
            and mine[0].get("type") == "Triage"
            and not changed
        )
        return Verdict("VERIFIED" if ok else "WRONG", {"rows": mine, "changed": changed})
