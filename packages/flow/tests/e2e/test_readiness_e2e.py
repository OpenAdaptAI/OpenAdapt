"""Shadow mode to a Readiness Scorecard, end to end in headless Chromium.

A scripted "person" does 20 synthetic MockMed tasks with the network capture
on; ``readiness import`` turns the recordings into reference episodes. The
store is reset to its "before" state, and a scripted agent (this file, run as
a subprocess through ``SubprocessLauncher``) does the same tasks on the
guarded tab. It has 5 planted field errors and 2 planted wrong-record cases.
The shadow hold stops every save; the comparator must find exactly the
planted errors, and the app's own store must show no write.

Synthetic MockMed in front of the ``effect_e2e`` record store; localhost only.

Set ``OPENADAPT_WRITE_EVIDENCE=1`` to rewrite ``benchmark/readiness/results.json``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PATIENTS = {
    "p1": "Jane Sample",
    "p2": "Alex Testcase",
    "p3": "Sam Specimen",
}
#: Planted agent errors: episode index -> what the agent does differently.
FIELD_ERRORS = {
    1: {"type": "flip"},
    4: {"note": "Synthetic note O4 for review"},  # letter O for zero
    7: {"note": ""},
    10: {"type": "flip"},
    13: {"note": "Synthetic note 13 for reviews"},
}
WRONG_RECORD = {16: "next", 19: "next"}
EVIDENCE = Path(__file__).resolve().parents[2] / "benchmark" / "readiness" / "results.json"


def _episode(i: int) -> dict[str, Any]:
    pid = list(PATIENTS)[i % 3]
    return {
        "task_id": f"readiness-mockmed-{i:04d}",
        "patient": pid,
        "type": "Triage" if i % 2 == 0 else "Consult",
        "note": f"Synthetic note {i:02d} for review",
    }


def _agent_plan(i: int) -> dict[str, Any]:
    plan = dict(_episode(i))
    errors = FIELD_ERRORS.get(i, {})
    if errors.get("type") == "flip":
        plan["type"] = "Consult" if plan["type"] == "Triage" else "Triage"
    if "note" in errors:
        plan["note"] = errors["note"]
    if i in WRONG_RECORD:
        ids = list(PATIENTS)
        plan["patient"] = ids[(ids.index(plan["patient"]) + 1) % 3]
    return plan


def _do_task(page: Any, plan: dict[str, Any], think_s: float, log: Any = None) -> None:
    def act(kind: str, fn: Any) -> None:
        time.sleep(think_s)
        if log is not None:
            log(kind)
        fn()

    act("click", lambda: page.click(f"#open-{plan['patient']}"))
    act("click", lambda: page.click("#new-encounter"))
    act("click", lambda: page.click(f"#type-{plan['type'].lower()}"))
    act("click", lambda: page.click("#note"))
    act("type", lambda: page.fill("#note", plan["note"]))
    act("click", lambda: page.click("#save-encounter"))


# -- the scripted agent (run as a subprocess) ---------------------------------------


def agent_main() -> int:
    from playwright.sync_api import sync_playwright

    plan = json.loads(Path(os.environ["READINESS_E2E_PLAN"]).read_text())[
        os.environ["OPENADAPT_TASK_ID"]
    ]
    assert os.environ["OPENADAPT_TASK"].strip()
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(os.environ["OPENADAPT_GUARD_CDP_URL"])
        base = os.environ["OPENADAPT_GUARD_PAGE_URL"].split("?")[0].split("#")[0]
        page = next(
            p for c in browser.contexts for p in c.pages if p.url.startswith(base)
        )
        page.wait_for_selector("#tasks-table")
        _do_task(page, plan, think_s=0.25)
        page.wait_for_timeout(800)
    Path(os.environ["OPENADAPT_USAGE_PATH"]).write_text(
        json.dumps(
            {
                "schema": "openadapt.agent-usage/v1",
                "calls": 6,
                "input_tokens": 12000,
                "output_tokens": 600,
                "model": "claude-sonnet-5-5",
                "cost_usd": None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(agent_main())


# -- the test -------------------------------------------------------------------------------

import pytest  # noqa: E402

pytestmark = pytest.mark.timeout(1200)


def _chromium_or_skip() -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")


def _record_people(app_url: str, src: Path, n: int) -> None:
    """The person does each task in a headless browser with network capture."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow.network_capture import NetworkCapture

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for i in range(n):
            ep = _episode(i)
            folder = src / f"task-{i:02d}"
            rec = folder / "recording"
            rec.mkdir(parents=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            capture = NetworkCapture(context).start()
            t0 = time.monotonic()
            events: list[dict[str, Any]] = []
            page = context.new_page()
            page.goto(app_url)
            page.wait_for_selector("#tasks-table")
            _do_task(
                page,
                ep,
                think_s=0.15,
                log=lambda kind: events.append(
                    {"t": round(time.monotonic() - t0, 3), "kind": kind}
                ),
            )
            page.wait_for_selector("#saved-banner", timeout=10000)
            capture.stop()
            capture.write(rec, t0)
            (rec / "events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in events)
            )
            context.close()
            (folder / "task.json").write_text(
                json.dumps(
                    {
                        "schema": "openadapt.readiness-task/v1",
                        "task_id": ep["task_id"],
                        "instruction": "Open the patient, add an encounter of the "
                        "given type with the note, and save.",
                        "inputs": {"type": ep["type"], "note": ep["note"]},
                        "expected_record": {
                            "patient_id": ep["patient"],
                            "name": PATIENTS[ep["patient"]],
                        },
                        "save": {"method": "POST", "path": "/api/encounter"},
                    }
                )
            )
        browser.close()


def test_shadow_run_to_scorecard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _chromium_or_skip()
    from benchmark.agent_guard.app import serve
    from openadapt_flow.readiness.compare import compare_runs
    from openadapt_flow.readiness.launcher import SubprocessLauncher
    from openadapt_flow.readiness.reference import import_episodes
    from openadapt_flow.readiness.runner import RunnerConfig, run_episodes
    from openadapt_flow.readiness.scorecard import write_scorecard

    n = 20
    app = serve(tmp_path / "app.sqlite")
    try:
        app_url = f"{app.base_url}/?fault=ok#tasks"
        _record_people(app_url, tmp_path / "people", n)
        assert app.writes == n  # the person's saves landed
        imported = import_episodes(tmp_path / "people", tmp_path / "episodes", app_url=app_url)
        assert imported.refused == [], imported.refused
        assert len(imported.imported) == n

        # The sandbox's "before" state, restored before every agent task.
        app.store.reset()
        app.writes = 0
        before = app.store.read_encounters()

        def reset() -> None:
            app.store.reset()
            app.writes = 0

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({_episode(i)["task_id"]: _agent_plan(i) for i in range(n)}))
        monkeypatch.setenv("READINESS_E2E_PLAN", str(plan))
        launcher = SubprocessLauncher(
            [sys.executable, str(Path(__file__).resolve())], label="scripted-agent"
        )
        cfg = RunnerConfig(
            app_url=app_url,
            screen_selector="#patient-banner",
            timeout_s=90.0,
            linger_s=1.0,
            reset=reset,
        )
        started = time.monotonic()
        manifest = run_episodes(tmp_path / "episodes", tmp_path / "runs", launcher, cfg)
        run_seconds = round(time.monotonic() - started, 1)

        # No write reached the app: read the app's own state.
        assert app.writes == 0
        assert app.store.read_encounters() == before
        assert manifest["landed_writes"] == 0
        statuses = {t["task_id"]: t["status"] for t in manifest["tasks"]}
        assert set(statuses.values()) == {"HELD"}, statuses

        cases = compare_runs(tmp_path / "episodes", tmp_path / "runs")
        card = write_scorecard(cases, tmp_path / "card", hourly_cost=40.0)
    finally:
        app.stop()

    planted_fields = set()
    for i, errors in FIELD_ERRORS.items():
        planted_fields |= {(_episode(i)["task_id"], name) for name in errors}
    planted_wrong = {_episode(i)["task_id"] for i in WRONG_RECORD}
    found_fields = {
        (e["task_id"], e["field"])
        for e in card["field_errors"]
        if e["kind"] in ("mismatched", "missing", "extra")
    }
    found_wrong = {c["task_id"] for c in cases["cases"] if c["status"] == "WRONG_RECORD"}

    def pr(found: set, planted: set) -> tuple[float, float]:
        hit = len(found & planted)
        return (hit / len(found) if found else 1.0, hit / len(planted))

    field_pr, wrong_pr = pr(found_fields, planted_fields), pr(found_wrong, planted_wrong)
    assert field_pr == (1.0, 1.0), (found_fields, planted_fields)
    assert wrong_pr == (1.0, 1.0), (found_wrong, planted_wrong)
    assert card["matched"] == n - len(FIELD_ERRORS) - len(WRONG_RECORD)
    assert card["risks"]["wrong_record"] == len(WRONG_RECORD)
    assert card["landed_writes"] == 0 and card["scope_ok"]
    # Uncompared never counts as matched.
    for case in cases["cases"]:
        if any(r["kind"] == "uncompared" for r in case["fields"]):
            assert case["status"] != "MATCHED"
    # The JSON carries no values.
    text = (tmp_path / "card" / "scorecard.json").read_text()
    for i in range(n):
        assert _episode(i)["note"] not in text
        assert _agent_plan(i)["note"] not in text or not _agent_plan(i)["note"]
    for name in PATIENTS.values():
        assert name not in text
    assert card["model_cost_usd"]["unknown_count"] == 0
    assert card["savings"]["est_value_usd"] is not None

    if os.environ.get("OPENADAPT_WRITE_EVIDENCE") == "1":
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            json.dumps(
                {
                    "schema": "openadapt.readiness-evidence/v1",
                    "fixture": "MockMed (synthetic) in front of the effect_e2e SQLite store",
                    "episodes": n,
                    "planted": {
                        "field_errors": sorted(map(list, planted_fields)),
                        "wrong_record": sorted(planted_wrong),
                    },
                    "found": {
                        "field_errors": sorted(map(list, found_fields)),
                        "wrong_record": sorted(found_wrong),
                    },
                    "field_error_precision_recall": list(field_pr),
                    "wrong_record_precision_recall": list(wrong_pr),
                    "app_writes_after_run": 0,
                    "app_store_unchanged": True,
                    "run_seconds": run_seconds,
                    "run_statuses": sorted(set(statuses.values())),
                    "scorecard": card,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
