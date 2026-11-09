"""Generated verifiers vs hand-written ``verify.yaml`` under planted faults.

See ``PROTOCOL.md`` (committed before the first measured run). For each app in
``apps.py``:

1. drive one correct run (the demonstration) in headless Chromium and write it
   as a recording (``events.jsonl``, ``meta.json``, ``network.jsonl``);
2. build a verifier from it through each existing generation path, with no
   verifier text written by a person (``Z-*`` arms), one declared statement
   (``D-*``) or only the read path given (``P-*``);
3. run every planted-fault trial once, judge it with the hand-written
   verifier and every generated one, and compare each verdict with the
   ground truth read straight from the fault server's store.

Headless only, localhost only, no model calls, synthetic data only.

    uv run --frozen --extra dev --extra browser python -m benchmark.zero_config_verify.run
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import platform
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from benchmark.zero_config_verify.apps import APPS, TASKS, AppSpec

HERE = Path(__file__).resolve().parent
RESULTS_JSON = HERE / "results.json"
GENERATED_DIR = HERE / "generated"

ZERO_CONFIG = ("Z-net", "Z-net-probed", "Z-net-read", "Z-db", "Z-db-gate")
DECLARED = ("D-net-read-complete",)
READ_PATH_GIVEN = ("P-record-verify", "P-scaffold-verifier")
ARMS = ZERO_CONFIG + DECLARED + READ_PATH_GIVEN
HAND = "hand-written"
HAND_COMPLETE = "hand-written+complete"

_FLAG_LINE = re.compile(r"^\s*needs_operator_confirmation:\s*true\s*$", re.M)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


def start_app(spec: AppSpec) -> tuple[str, Any, Callable[[], None]]:
    module, func = spec.serve.split(":")
    url, db, stop = getattr(importlib.import_module(module), func)()
    return url.rstrip("/"), db, stop


def reset(db: Any, seed: bool) -> None:
    db.reset(seed_concurrent=seed)


def records(db: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in db.snapshot()["records"]]


# ---------------------------------------------------------------------------
# Driving the app (headless Chromium)
# ---------------------------------------------------------------------------


def drive(
    browser: Any,
    origin: str,
    task: dict[str, Any],
    *,
    recording: Optional[Path] = None,
    read_back: bool = False,
    sor_reader: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None,
) -> None:
    """Run the task's selector solution. With ``recording``, write the
    recording the recorder would: events, params and the network capture."""

    from openadapt_flow.network_capture import NetworkCapture

    context = browser.new_context(viewport={"width": 1280, "height": 900})
    capture = NetworkCapture(context).start() if recording is not None else None
    t0 = time.monotonic()
    events: list[dict[str, Any]] = []
    page = context.new_page()
    try:
        page.goto(origin + task["start_path"], wait_until="load")
        page.wait_for_timeout(300)
        steps = [s for s in task["solution"] if s["kind"] != "done"]
        save_index = max(i for i, s in enumerate(steps) if s["kind"] == "click")
        for index, step in enumerate(steps):
            if read_back and index == save_index:
                # The demonstration opens the store's record list before saving.
                page.evaluate("() => fetch('/api/db').then(r => r.text())")
            before = sor_reader() if sor_reader else None
            event: dict[str, Any] = {"i": len(events)}
            if step["kind"] == "fill":
                page.fill(step["selector"], step["text"])
                event.update(kind="type", text=step["text"])
            elif step["kind"] == "click":
                locator = page.locator(step["selector"])
                label = (locator.inner_text(timeout=5000) or "").strip()
                locator.click(timeout=5000)
                event.update(
                    kind="click",
                    structural={"role": "button", "name": label},
                    text=label,
                )
            elif step["kind"] == "wait":
                page.wait_for_timeout(int(float(step["seconds"]) * 1000))
                continue
            else:
                raise ValueError(f"unknown solution step {step['kind']!r}")
            page.wait_for_timeout(150)
            event["t"] = round(time.monotonic() - t0, 3)
            if sor_reader is not None:
                after = sor_reader()
                if before is not None:
                    event["sor_before"] = before
                if after is not None:
                    event["sor_after"] = after
            events.append(event)
        if read_back:
            # ...and again after the save, as an app showing the saved record would.
            page.evaluate("() => fetch('/api/db').then(r => r.text())")
            page.wait_for_timeout(200)
    finally:
        if capture is not None:
            capture.stop()
        context.close()
    if recording is not None:
        recording.mkdir(parents=True, exist_ok=True)
        (recording / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
        )
        (recording / "meta.json").write_text(
            json.dumps({"params": task["params"]}), encoding="utf-8"
        )
        assert capture is not None
        capture.write(recording, t0)


# ---------------------------------------------------------------------------
# Generation (existing paths only)
# ---------------------------------------------------------------------------


def _cli(argv: list[str]) -> tuple[int, str]:
    from openadapt_flow.__main__ import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(argv)
        except SystemExit as exc:  # ScaffoldRefused is a SystemExit
            if isinstance(exc.code, str):
                print(exc.code)
                code = 1
            else:
                code = int(exc.code or 0)
    return int(code or 0), out.getvalue()


def _load_generated(path: Path) -> tuple[Optional[Any], Optional[str], str]:
    """``(config, error, text)`` after deleting the confirmation flags."""

    from openadapt_flow.verify_run import parse_verify_config

    text = path.read_text(encoding="utf-8")
    accepted = _FLAG_LINE.sub("", text)
    try:
        data = yaml.safe_load(accepted) or {}
        return parse_verify_config(data, source=path.name), None, text
    except Exception as exc:  # noqa: BLE001 - the parse failure is the finding
        return None, f"{type(exc).__name__}: {exc}", text


def scaffold(recording: Path, out: Path, extra: list[str]) -> dict[str, Any]:
    code, output = _cli(["scaffold-verifier", str(recording), "-o", str(out), *extra])
    proposed = out / "record_check.proposed.yaml"
    draft = out / "effect_contract.yaml"
    result: dict[str, Any] = {"exit_code": code, "output": output.strip()}
    if proposed.is_file():
        result["file"] = proposed
        proof = out / "record_check.proof.json"
        if proof.is_file():
            status = json.loads(proof.read_text())
            result["generator_status"] = status.get("status")
            result["generator_misses"] = status.get("misses", [])
    elif draft.is_file():
        result["file"] = draft
        result["generator_status"] = "todo-draft"
    else:
        result["generator_status"] = "refused"
    discovery = out / "read_discovery.json"
    if discovery.is_file():
        result["read_discovery"] = json.loads(discovery.read_text())
    elif proposed.is_file() and (out / "record_check.proof.json").is_file():
        found = json.loads((out / "record_check.proof.json").read_text()).get(
            "read_discovery"
        )
        if found:
            result["read_discovery"] = found
    return result


def hand_written_text(spec: AppSpec, *, complete: bool) -> str:
    """The hand-written file, optionally with ``records_complete: true``
    (PROTOCOL.md, deviation of 2026-11-09)."""

    text = spec.verify_yaml.read_text()
    if complete:
        data = yaml.safe_load(text)
        data["verifier"]["records_complete"] = True
        text = yaml.safe_dump(data, sort_keys=False)
    return text


def verifier_only_yaml(spec: AppSpec, origin: str, dest: Path) -> Path:
    """The hand-written ``verifier:`` read path alone (no effects)."""

    from string import Template

    text = Template(hand_written_text(spec, complete=True)).safe_substitute(
        APP_URL=origin
    )
    data = yaml.safe_load(text)
    dest.write_text(
        yaml.safe_dump({"verifier": data["verifier"]}, sort_keys=False),
        encoding="utf-8",
    )
    return dest


def generate(
    spec: AppSpec, browser: Any, origin: str, db: Any, work: Path
) -> dict[str, dict[str, Any]]:
    """Every arm's generator output for this app, keyed by arm name."""

    from openadapt_flow.record_sor import RecordingObservation

    [demo] = spec.tasks([spec.demo])
    arms: dict[str, dict[str, Any]] = {}

    # 1. The app's own traffic.
    reset(db, spec.demo[4])
    rec_plain = work / "rec-plain"
    drive(browser, origin, demo, recording=rec_plain)
    arms["Z-net"] = scaffold(rec_plain, work / "out-Z-net", [])
    # Same recording; the generator may probe GET reads the write implies on
    # this local fixture, right after the demonstration's save.
    arms["Z-net-probed"] = scaffold(
        rec_plain, work / "out-Z-net-probed", ["--probe-reads"]
    )
    sqlite = getattr(db, spec.sqlite_attr) if spec.sqlite_attr else None
    for arm, extra in (("Z-db", []), ("Z-db-gate", ["--fault-gate"])):
        if sqlite is None:
            arms[arm] = {
                "generator_status": "not-applicable",
                "output": "the fault server keeps its store in memory: no database file",
            }
        else:
            arms[arm] = scaffold(
                rec_plain, work / f"out-{arm}", ["--db", str(sqlite), *extra]
            )

    # 2. The demonstration also reads the record list before and after saving.
    reset(db, spec.demo[4])
    rec_read = work / "rec-read"
    drive(browser, origin, demo, recording=rec_read, read_back=True)
    arms["Z-net-read"] = scaffold(rec_read, work / "out-Z-net-read", [])
    arms["D-net-read-complete"] = scaffold(
        rec_read, work / "out-D-net-read-complete", ["--complete-read"]
    )

    # 3. record --verify with only the read path: snapshots around the run and
    #    sor_before/sor_after on every event.
    reset(db, spec.demo[4])
    read_path = verifier_only_yaml(spec, origin, work / "read_path.yaml")
    rec_sor = work / "rec-sor"
    observation = RecordingObservation.start(read_path)
    drive(
        browser,
        origin,
        demo,
        recording=rec_sor,
        sor_reader=lambda: observation.observer.system_of_record,
    )
    contract, proposal, _diff = observation.finish(rec_sor, demo["params"])
    if contract is None:
        arms["P-record-verify"] = {
            "generator_status": "refused",
            "output": "; ".join(proposal.not_proposed) or "nothing proposed",
        }
    else:
        arms["P-record-verify"] = {
            "file": contract,
            "generator_status": "proposed",
            "output": "; ".join(proposal.not_proposed),
        }
    arms["P-scaffold-verifier"] = scaffold(
        rec_sor, work / "out-P-scaffold-verifier", ["--verifier", str(read_path)]
    )

    for arm, result in arms.items():
        result["inputs"] = ARM_INPUTS[arm]
        path = result.get("file")
        if path is None:
            result["runnable"] = False
            continue
        cfg, error, text = _load_generated(Path(path))
        result["config"] = cfg
        result["runnable"] = cfg is not None
        result["parse_error"] = error
        result["text"] = text
    return arms


ARM_INPUTS = {
    "Z-net": ["events.jsonl", "meta.json params", "network.jsonl (app's own traffic)"],
    "Z-net-probed": [
        "events.jsonl",
        "meta.json params",
        "network.jsonl (app's own traffic)",
        "--probe-reads (GET probes on the local fixture after the save)",
    ],
    "Z-net-read": [
        "events.jsonl",
        "meta.json params",
        "network.jsonl (app traffic plus GET /api/db before and after the save)",
    ],
    "Z-db": ["events.jsonl", "meta.json params", "network.jsonl", "--db <store file>"],
    "Z-db-gate": [
        "events.jsonl",
        "meta.json params",
        "network.jsonl",
        "--db <store file>",
        "--fault-gate",
    ],
    "D-net-read-complete": [
        "Z-net-read inputs",
        "--complete-read (a person's statement)",
    ],
    "P-record-verify": [
        "verifier: read path from the hand-written verify.yaml",
        "before/after reads around the demonstration",
        "meta.json params",
    ],
    "P-scaffold-verifier": [
        "verifier: read path from the hand-written verify.yaml",
        "events.jsonl with sor_before/sor_after",
        "sor_snapshots.json",
        "meta.json params",
        "network.jsonl",
    ],
}


# ---------------------------------------------------------------------------
# Ground truth and judging
# ---------------------------------------------------------------------------


def ground_truth(
    spec: AppSpec,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    params: dict[str, str],
) -> tuple[bool, str]:
    old = {r["id"]: r for r in before}
    new = {r["id"]: r for r in after}
    added = [r for rid, r in new.items() if rid not in old]
    lost = [rid for rid, r in old.items() if new.get(rid) != r]
    if lost:
        return False, f"{len(lost)} existing record(s) lost or changed"
    if len(added) != 1:
        return False, f"{len(added)} new record(s)"
    [rec] = added
    for fld, param in spec.fields.items():
        if str(rec.get(fld)) != str(params[param]):
            return False, f"new record's {fld} is not the task's value"
    for fld, value in spec.constants.items():
        if str(rec.get(fld)) != value:
            return False, f"new record's {fld} is {rec.get(fld)!r}"
    return True, "exactly one new record with the task's values"


def hand_written_config(spec: AppSpec, origin: str, *, complete: bool) -> Any:
    from openadapt_flow.rl_env.reward import render_verify_config

    return render_verify_config(hand_written_text(spec, complete=complete), origin)


def run_trials(
    spec: AppSpec,
    browser: Any,
    origin: str,
    db: Any,
    configs: dict[str, Any],
) -> list[dict[str, Any]]:
    from openadapt_flow.rl_env.reward import EpisodeJudge

    rows = []
    for task, case in zip(spec.tasks(spec.trials), spec.trials):
        fault, _rec, _kind, _text, seed = case
        reset(db, seed)
        before = records(db)
        judges = {
            name: EpisodeJudge(cfg, task["params"], episode_id=f"{task['id']}-{name}")
            for name, cfg in configs.items()
        }
        started = time.monotonic()
        drive(browser, origin, task)
        # A timeout-mode write commits, then the server holds the response.
        after = records(db)
        correct, why = ground_truth(spec, before, after, task["params"])
        designed = TASKS.FAULTS[fault][0]
        verdicts = {}
        for name, judge in judges.items():
            j = judge.judge("success", feasible=correct)
            verdicts[name] = {"verdict": j.verdict, "error": j.error}
            judge.close()
        rows.append(
            {
                "task": task["id"],
                "fault": fault,
                "seed_concurrent": seed,
                "params": task["params"],
                "ground_truth_correct": correct,
                "ground_truth_reason": why,
                "design_label_correct": designed,
                "label_agrees": designed == correct,
                "seconds": round(time.monotonic() - started, 2),
                "verdicts": verdicts,
            }
        )
        print(
            f"  {task['id']} {fault:<12} gt={'ok ' if correct else 'BAD'} "
            + " ".join(f"{n}={v['verdict'][:4]}" for n, v in verdicts.items()),
            flush=True,
        )
    return rows


def tally(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    per_fault: dict[str, Counter] = {}
    total: Counter = Counter()
    for row in rows:
        v = row["verdicts"].get(name)
        if v is None:
            continue
        accepted = v["verdict"] == "CONFIRMED"
        c = per_fault.setdefault(row["fault"], Counter())
        key = (
            ("true_accept" if accepted else "false_reject")
            if row["ground_truth_correct"]
            else ("false_accept" if accepted else "true_reject")
        )
        c[key] += 1
        c["runs"] += 1
        total[key] += 1
        total["runs"] += 1
        total["correct_runs" if row["ground_truth_correct"] else "wrong_runs"] += 1
        if v["verdict"] == "INDETERMINATE":
            c["indeterminate"] += 1
            total["indeterminate"] += 1
    return {
        "total": {
            k: total.get(k, 0)
            for k in (
                "runs",
                "correct_runs",
                "wrong_runs",
                "true_accept",
                "false_reject",
                "true_reject",
                "false_accept",
                "indeterminate",
            )
        },
        "per_fault": {f: dict(c) for f, c in per_fault.items()},
    }


def kill_check(arm: dict[str, Any], hand: dict[str, Any]) -> dict[str, Any]:
    if not arm.get("runnable"):
        return {"ready": False, "failed": ["no runnable verifier"]}
    g, h = arm["tally"]["total"], hand["total"]
    failed = []
    if g["false_accept"] > h["false_accept"]:
        failed.append(
            f"false accepts {g['false_accept']} > hand-written {h['false_accept']}"
        )
    if g["false_reject"] > 2 * h["false_reject"]:
        failed.append(
            f"false rejects {g['false_reject']} > 2 x hand-written {h['false_reject']}"
        )
    return {"ready": not failed, "failed": failed}


# ---------------------------------------------------------------------------
# Output hygiene: no local ports, temp paths or store files in committed data
# ---------------------------------------------------------------------------


def scrub(text: str, origin: str, work: Path, sqlite: Optional[Path]) -> str:
    text = text.replace(origin, "${APP_URL}")
    if sqlite is not None:
        text = text.replace(str(sqlite), "${STORE_DB}")
    text = text.replace(str(work.resolve()), "<work>").replace(str(work), "<work>")
    text = re.sub(r"http://127\.0\.0\.1:\d+", "${APP_URL}", text)
    text = re.sub(r"/[^\s'\"]*openadapt-mockloan-[^\s'\"]*", "${STORE_DB}", text)
    # Pydantic shortens long values ("/var/fol...xyz"), so the exact paths
    # above can miss; any remaining temp path becomes <work>.
    text = re.sub(r"/(?:var/folders|private/var|tmp)/[^\s'\"]*", "<work>", text)
    return text


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=HERE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: Optional[list[str]] = None) -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--only", help="comma-separated app names")
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="build the verifiers and stop (no trials, writes nothing)",
    )
    args = parser.parse_args(argv)
    apps = [a for a in APPS if not args.only or a.name in args.only.split(",")]

    report: dict[str, Any] = {
        "schema": "openadapt.zero-config-verify-results/v1",
        "protocol": "PROTOCOL.md",
        "git_head": _git_head(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": {},
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for spec in apps:
                print(f"== {spec.name}", flush=True)
                origin, db, stop = start_app(spec)
                work = Path(tempfile.mkdtemp(prefix=f"zcv-{spec.name}-"))
                sqlite = getattr(db, spec.sqlite_attr) if spec.sqlite_attr else None
                try:
                    arms = generate(spec, browser, origin, db, work)
                    for arm, result in arms.items():
                        print(
                            f"  {arm:<20} {result['generator_status']:<14} "
                            f"runnable={result['runnable']}",
                            flush=True,
                        )
                    if args.generate_only:
                        for arm, result in arms.items():
                            print(f"--- {arm}\n{result.get('output', '')}")
                            print(result.get("text") or result.get("parse_error"))
                        continue
                    configs = {
                        HAND: hand_written_config(spec, origin, complete=False),
                        HAND_COMPLETE: hand_written_config(spec, origin, complete=True),
                    }
                    configs.update(
                        {a: r["config"] for a, r in arms.items() if r["runnable"]}
                    )
                    rows = run_trials(spec, browser, origin, db, configs)
                    hand = tally(rows, HAND_COMPLETE)
                    app_out: dict[str, Any] = {
                        "held_out": spec.held_out,
                        "hand_written": {
                            "file": str(spec.verify_yaml.relative_to(HERE.parents[1])),
                            "tally": tally(rows, HAND),
                        },
                        "hand_written_complete": {
                            "file": "the same file plus records_complete: true",
                            "tally": hand,
                        },
                        "arms": {},
                        "trials": rows,
                    }
                    GENERATED_DIR.mkdir(exist_ok=True)
                    for arm, result in arms.items():
                        entry = {
                            "class": (
                                "zero-config"
                                if arm in ZERO_CONFIG
                                else "declared"
                                if arm in DECLARED
                                else "read-path-given"
                            ),
                            "inputs": result["inputs"],
                            "generator_status": result["generator_status"],
                            "generator_misses": result.get("generator_misses", []),
                            "generator_output": scrub(
                                result.get("output", ""), origin, work, sqlite
                            ),
                            "runnable": result["runnable"],
                            "read_discovery": json.loads(
                                scrub(
                                    json.dumps(result.get("read_discovery")),
                                    origin,
                                    work,
                                    sqlite,
                                )
                            ),
                            "parse_error": scrub(
                                result.get("parse_error") or "", origin, work, sqlite
                            )
                            or None,
                        }
                        if result.get("text"):
                            name = f"{spec.name}.{arm}.yaml"
                            (GENERATED_DIR / name).write_text(
                                scrub(result["text"], origin, work, sqlite),
                                encoding="utf-8",
                            )
                            entry["generated_file"] = f"generated/{name}"
                        if result["runnable"]:
                            entry["tally"] = tally(rows, arm)
                        entry["kill_check"] = kill_check(
                            {**entry, "runnable": result["runnable"]}, hand
                        )
                        app_out["arms"][arm] = entry
                    report["apps"][spec.name] = app_out
                finally:
                    stop()
                    shutil.rmtree(work, ignore_errors=True)
        finally:
            browser.close()
    if args.generate_only:
        return 0
    zero_ready = {
        app: [
            arm
            for arm, e in out["arms"].items()
            if e["class"] == "zero-config" and e["kill_check"]["ready"]
        ]
        for app, out in report["apps"].items()
    }
    report["verdict"] = {
        "zero_config_ready_arms": zero_ready,
        "zero_config_ready_on_both_apps": bool(zero_ready)
        and all(zero_ready.values())
        and len(zero_ready) == len(APPS),
    }
    text = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    RESULTS_JSON.write_text(text, encoding="utf-8")
    print(f"wrote {RESULTS_JSON}")
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
