"""Refinery demo: a recorded workflow becomes a task environment, then we audit its grader.

    python demo/refinery/pipeline.py            # from the committed capture
    python demo/refinery/pipeline.py --capture  # replay the recording first (headless Chromium)

Steps, all on synthetic MockMed data:

1. **Lift.** Read ``results/capture.json`` (the recording replayed once, with
   the API requests it sent and the system of record before and after). Each
   API request becomes a call; a mutating method is a write. A write field
   whose value is a recording input, or that names a record (``*_id``), is a
   task parameter; the rest are constants. Fields that appear in the store
   diff but in no request (``id``, ``source``, ``key``) are server-assigned.
   The lift is accepted only if the store diff is exactly the write bodies.
2. **Generate tasks.** Substitute each row of ``VARIANTS`` (synthetic) into the
   lifted trajectory: an instruction, the initial state (the store after
   reset, read back), the reference trajectory, and the expected effect.
3. **Generate graders.** Three success checks, each derived mechanically from
   the capture: the postcondition mined from the store diff (a matching row
   exists), a replay of the recorded calls, and the exact effect at claim time.
   The first is the seeded loophole: it never asks what else changed.
4. **Audit.** Run ``openadapt_flow.verifier_audit`` against the generated
   environment once per grader. Ground truth is the store diff read from
   MockMed's database, never the grader. Ship only a grader with no false
   accepts and no false rejects.

Writes ``results/refinery.json`` and one ``results/audit/<grader>.md`` per grader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow.verifier_audit import (
    AuditConfig,
    Call,
    CallKind,
    Episode,
    Verdict,
    run_audit,
)
from openadapt_flow.verifier_audit.families import IDENTITY_KEY
from openadapt_flow.verifier_audit.model import Effect, canonical_effect
from openadapt_flow.verifier_audit.report import render_markdown

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CAPTURE = RESULTS / "capture.json"
SCHEMA = "openadapt.refinery-demo/v1"

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
CLAIM = Call("complete", {}, CallKind.CLAIM)
"""The environment's done tool. The recording ends at Save; an RL episode
needs an explicit claim, so the lift appends one."""
READ_TOOL = Call("GET /api/db", {}, CallKind.READ)
"""The environment's read tool (the record list). The recording made no API
read; the audit's positive control uses this one."""
INITIAL_STATE = {"seed_concurrent": True}
"""Reset body: MockMed seeds one Consult encounter on p1 by another
clinician, so the store is not empty when a task starts."""

# Synthetic task variants: the recording's own values first, then two more.
VARIANTS: list[dict[str, str]] = [
    {"patient_id": "p1", "note": "Synthetic follow-up in two weeks"},
    {"patient_id": "p2", "note": "Synthetic blood pressure recheck"},
    {"patient_id": "p3", "note": "Synthetic flu vaccine reminder"},
]


# -- 1. Lift ---------------------------------------------------------------


def lift(capture: dict[str, Any]) -> dict[str, Any]:
    inputs = {str(v): k for k, v in capture["recording_inputs"].items()}
    steps, params, write_fields = [], {}, set()
    for req in capture["wire"]:
        kind = "write" if req["method"] in MUTATING else "read"
        args = dict(req["body"] or {})
        template = {}
        for key, value in args.items():
            if kind == "write":
                write_fields.add(key)
            if str(value) in inputs:
                params[inputs[str(value)]] = value
                template[key] = "{" + inputs[str(value)] + "}"
            elif kind == "write" and IDENTITY_KEY.search(key):
                params[key] = value
                template[key] = "{" + key + "}"
            else:
                template[key] = value
        steps.append(
            {"name": f"{req['method']} {req['path']}", "kind": kind, "args": template}
        )

    before = {r["id"]: r for r in capture["store_before"]}
    added = [r for r in capture["store_after"] if r["id"] not in before]
    server_assigned = sorted({k for r in added for k in r} - write_fields)
    observed = canonical_effect([_project(r, write_fields) for r in added])
    sent = canonical_effect(
        [
            {k: req["body"][k] for k in sorted(write_fields) if k in req["body"]}
            for req in capture["wire"]
            if req["method"] in MUTATING
        ]
    )
    return {
        "steps": steps,
        "parameters": params,
        "record_fields": sorted(write_fields),
        "server_assigned_fields": server_assigned,
        "store_diff_rows": len(added),
        "store_diff_equals_write_bodies": observed == sent,
    }


def _project(row: dict[str, Any], fields: Sequence[str] | set[str]) -> dict[str, Any]:
    return {k: row[k] for k in sorted(fields) if k in row}


# -- 2. Generate tasks -----------------------------------------------------


def _fill(template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in template.items():
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            out[key] = values[value[1:-1]]
        else:
            out[key] = value
    return out


def generate_tasks(lifted: dict[str, Any], initial_rows: list[dict]) -> list[dict]:
    tasks = []
    for values in VARIANTS:
        missing = set(lifted["parameters"]) - set(values)
        if missing:
            raise ValueError(f"variant {values} lacks parameters {sorted(missing)}")
        steps = [{**s, "args": _fill(s["args"], values)} for s in lifted["steps"]]
        writes = [s["args"] for s in steps if s["kind"] == "write"]
        digest = hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()[
            :10
        ]
        record = writes[0] if writes else {}
        fields = ", ".join(
            f"{k}={record[k]!r}" for k in lifted["record_fields"] if k in record
        )
        tasks.append(
            {
                "id": f"refinery-mockmed-{digest}",
                "instruction": f"Add this encounter and nothing else: {fields}. "
                "Call complete when it is saved.",
                "initial_state": {"reset": INITIAL_STATE, "rows": initial_rows},
                "reference": steps + [CLAIM.to_json()],
                "expected_effect": [{"op": "add", **w} for w in writes],
            }
        )
    return tasks


# -- 3. The generated environment and its graders --------------------------


class MockMedStore:
    """MockMed's system of record over HTTP (``fault_server``, ``?fault=ok``)."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> tuple[int, dict]:
        data = None if method == "GET" else json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def reset(self, body: dict) -> list[dict]:
        self.request("POST", "/api/reset", body)
        return self.rows()

    def rows(self) -> list[dict]:
        status, payload = self.request("GET", "/api/db")
        if status != 200:
            raise RuntimeError(f"GET /api/db returned {status}")
        return payload["records"]


def store_effect(initial: list[dict], now: list[dict], fields: Sequence[str]) -> Effect:
    """Rows added, removed or changed since reset, as record fields only."""
    before = {r["id"]: r for r in initial}
    after = {r["id"]: r for r in now}
    rows = [
        {"op": "add", **_project(r, fields)}
        for i, r in after.items()
        if i not in before
    ]
    rows += [
        {"op": "remove", **_project(r, fields)}
        for i, r in before.items()
        if i not in after
    ]
    rows += [
        {"op": "change", "from": _project(before[i], fields), "to": _project(r, fields)}
        for i, r in after.items()
        if i in before and _project(r, fields) != _project(before[i], fields)
    ]
    return canonical_effect(rows)


Grader = Callable[[dict, dict], bool]
"""(task, observation) -> pass. The observation holds the store rows at reset,
at the claim and at the end, and the calls the agent sent."""


def grade_mined_postcondition(task: dict, obs: dict) -> bool:
    # The row the recording saved, looked for in the final store. Nothing
    # else about the store is checked: that is the seeded loophole.
    return all(
        any(
            all(row.get(k) == v for k, v in want.items() if k != "op")
            for row in obs["final"]
        )
        for want in task["expected_effect"]
    )


def grade_trajectory_replay(task: dict, obs: dict) -> bool:
    # The agent must send exactly the recorded calls, in order.
    sent = [c for c in obs["calls"] if c["kind"] != "claim"]
    recorded = [c for c in task["reference"] if c["kind"] != "claim"]
    return sent == recorded


def grade_effect_at_claim(task: dict, obs: dict) -> bool:
    # The store diff at the claim and at the end both equal the expected one.
    want = canonical_effect(task["expected_effect"])
    fields = obs["fields"]
    return (
        obs["claim"] is not None
        and store_effect(obs["initial"], obs["claim"], fields) == want
        and store_effect(obs["initial"], obs["final"], fields) == want
    )


GRADERS: dict[str, tuple[Grader, str]] = {
    "mined_postcondition": (
        grade_mined_postcondition,
        "A row with the recorded field values exists in the final store "
        "(the record check mined from the capture's store diff).",
    ),
    "trajectory_replay": (
        grade_trajectory_replay,
        "The agent sent exactly the recorded calls, in order.",
    ),
    "effect_at_claim": (
        grade_effect_at_claim,
        "The store diff since reset equals the expected effect, both when the "
        "agent claims success and at the end.",
    ),
}


class RefineryEnv:
    """The generated environment, as a verifier-audit target."""

    def __init__(
        self, store: MockMedStore, tasks: list[dict], fields: Sequence[str], grader: str
    ) -> None:
        self.store = store
        self._tasks = {t["id"]: t for t in tasks}
        self.fields = list(fields)
        self.grader = grader
        self.name = f"refinery-mockmed/{grader}"

    def tasks(self) -> list[str]:
        return list(self._tasks)

    def instruction(self, task: str) -> str:
        return self._tasks[task]["instruction"]

    def reference(self, task: str) -> list[Call]:
        return [
            Call(c["name"], dict(c["args"]), CallKind(c["kind"]))
            for c in self._tasks[task]["reference"]
        ]

    def incidental_calls(self, task: str, reference: Sequence[Call]) -> list[Call]:
        return [READ_TOOL]

    def run(self, task: str, calls: Sequence[Call], *, score: bool) -> Episode:
        spec = self._tasks[task]
        initial = self.store.reset(spec["initial_state"]["reset"])
        claim: Optional[list[dict]] = None
        errors: dict[int, str] = {}
        for i, call in enumerate(calls):
            if call.kind is CallKind.CLAIM:
                claim = self.store.rows()
                continue
            method, path = call.name.split(" ", 1)
            status, payload = self.store.request(method, path, call.args)
            if status >= 400:
                errors[i] = f"{status} {payload.get('error', '')}".strip()
        final = self.store.rows()
        episode = Episode(
            final_effect=store_effect(initial, final, self.fields),
            claim_effect=store_effect(initial, claim, self.fields)
            if claim is not None
            else None,
            call_errors=errors,
        )
        if score:
            obs = {
                "initial": initial,
                "claim": claim,
                "final": final,
                "calls": [c.to_json() for c in calls],
                "fields": self.fields,
            }
            ok = GRADERS[self.grader][0](spec, obs)
            episode.verdict = Verdict.PASS if ok else Verdict.FAIL
            episode.scorer_detail = {"grader": self.grader}
        return episode


# -- 4. Audit --------------------------------------------------------------


def _case(cell: dict) -> dict:
    return {
        k: cell[k]
        for k in ("task", "family", "edit", "verdict", "effect_diff", "calls")
        if k in cell
    }


def audit(store: MockMedStore, tasks: list[dict], fields: Sequence[str]) -> dict:
    out = {}
    for name, (_fn, description) in GRADERS.items():
        report = run_audit(RefineryEnv(store, tasks, fields, name), AuditConfig())
        cells = report["cells"]
        out[name] = {
            "description": description,
            "tasks_excluded": report["tasks_excluded"],
            "statuses": dict(sorted(Counter(c["status"] for c in cells).items())),
            "families": [
                {k: row[k] for k in ("family", "polarity", "hits", "n", "statuses")}
                for row in report["summary"]["families"]
            ],
            "false_accepts": [_case(c) for c in cells if c["status"] == "FALSE_ACCEPT"],
            "false_rejects": [_case(c) for c in cells if c["status"] == "FALSE_REJECT"],
            "_report": report,
        }
    return out


def build(capture: dict) -> tuple[dict, dict[str, str]]:
    """Run steps 1-4 against a fresh in-process MockMed. Returns the results
    document and the per-grader Markdown reports."""
    from openadapt_flow.mockmed.fault_server import serve

    lifted = lift(capture)
    if not lifted["store_diff_equals_write_bodies"]:
        raise SystemExit("lift refused: the store diff is not the captured writes")
    base, _db, stop = serve()
    try:
        store = MockMedStore(base)
        tasks = generate_tasks(lifted, store.reset(INITIAL_STATE))
        audits = audit(store, tasks, lifted["record_fields"])
    finally:
        stop()
    markdown = {name: render_markdown(a.pop("_report")) for name, a in audits.items()}
    shipped = [
        name
        for name, a in audits.items()
        if not a["false_accepts"] and not a["false_rejects"] and not a["tasks_excluded"]
    ]
    doc = {
        "schema": SCHEMA,
        "data": "synthetic only (MockMed fixture; no real, customer or patient data)",
        "source": {
            "recording": capture["recording"],
            "recording_sha256": capture["recording_sha256"],
            "capture": "demo/refinery/results/capture.json",
            "capture_sha256": hashlib.sha256(
                json.dumps(capture, sort_keys=True).encode()
            ).hexdigest(),
        },
        "lift": lifted,
        "environment": {
            "tools": sorted(
                {s["name"] for s in lifted["steps"]} | {READ_TOOL.name, CLAIM.name}
            ),
            "tasks": tasks,
        },
        "audit": audits,
        "shipped_grader": shipped[0] if shipped else None,
    }
    return doc, markdown


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", action="store_true", help="replay the recording first")
    ap.add_argument("--out", type=Path, default=RESULTS)
    args = ap.parse_args(argv)
    if args.capture:
        sys.path.insert(0, str(HERE))
        import capture as capture_step

        CAPTURE.write_text(
            json.dumps(capture_step.capture(), indent=2, sort_keys=True) + "\n"
        )
    doc, markdown = build(json.loads(CAPTURE.read_text()))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "refinery.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n"
    )
    (args.out / "audit").mkdir(exist_ok=True)
    for name, text in markdown.items():
        (args.out / "audit" / f"{name}.md").write_text(text)
    for name, a in doc["audit"].items():
        print(
            f"{name:22s} false accepts {len(a['false_accepts']):2d}  "
            f"false rejects {len(a['false_rejects']):2d}  {a['statuses']}"
        )
    print(f"shipped grader: {doc['shipped_grader']}")
    return 0 if doc["shipped_grader"] else 1


if __name__ == "__main__":
    sys.exit(main())
