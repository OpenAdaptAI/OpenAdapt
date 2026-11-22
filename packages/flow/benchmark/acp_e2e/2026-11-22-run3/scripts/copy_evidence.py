"""Copy run 3's evidence into the repo by explicit allowlist only, then gate it.

Usage: copy_evidence.py RUN_DIR DEST [--check-only]

Nothing is copied by default. Each rule below names a source pattern and how
the file is copied; any file no rule names stays behind. Fail closed:

* readiness task folders go through Flow's own allowlist
  (openadapt_flow.readiness.export.export_task, include_values=False): run.json,
  egress.json and usage.json with only their schema's keys. case.json and
  proposed_write.json carry field values and stay behind; so do egress/,
  pseudonyms.json, held requests, screenshots and the agent's log.
* scorecard.json through the same export allowlist.
* guard_ledger_row.json with only the J3 keys (no values by design).
* ledger summary, export and push payloads (value-free rows by P2's design;
  the gate below still greps them).
* the drivers (.py, .sh, .mjs), a fixed list of PNG screenshots that show no
  record values (the dashboard pages and the rendered scorecards), the
  derived results_*.json summaries this run's drivers write, the README.

Then the gate: every synthetic PHI value the run used (MockMed's patient
names, MRNs and DOBs, every note the scripted person, the agents and the
fallback typed, and every real value in each pseudonyms.json the proxy
wrote) and the API key prefix are searched for in every copied file. Any
hit deletes nothing silently: the script lists the hits and exits 1.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

FLOW = Path("/Users/abrichr/oa/worktrees/acp-demo-merge/packages/flow")
sys.path.insert(0, str(FLOW))

GUARD_ROW_KEYS = {
    "schema", "task_id", "verdict", "approver_kind", "approval_opened_at",
    "approval_decided_at", "fields_checked", "fields_differing", "hold_ms",
    "readback_tier", "diff_path", "guard_verdict", "verdict_reason",  # codes, no values (P5 40d4e9b14)
}
LEDGER_FILES = ("summary.json", "summary.txt", "export.jsonl", "payloads.jsonl")
PNGS = (
    "3-dashboard/dashboard-ledger.png",
    "3-dashboard/dashboard-readiness.png",
    "1-shadow/card/scorecard.png",
    "5-claude/card/scorecard.png",
)
DRIVER_SUFFIXES = {".py", ".sh", ".mjs"}
RESULTS = re.compile(r"^results_[a-z0-9_]+\.json$")
API_KEY_PREFIX = "sk-ant-"
#: Values the scrubber put in a pseudonyms.json that are not personal data,
#: named one by one so the exclusion is visible: the demo app's own product
#: name (it is in every task id, readiness-mockmed-NNNN, and in the repo's
#: source). Every other map value is searched.
NOT_PHI = {"MockMed"}


def _readiness_dirs(run: Path) -> list[tuple[Path, str]]:
    out = []
    for base in ("1-shadow/runs", "1-shadow/adversarial/runs", "5-claude/runs"):
        for d in sorted((run / base).glob("readiness-*")):
            if d.is_dir():
                out.append((d, f"{base}/{d.name}"))
    return out


def copy(run: Path, dest: Path) -> dict[str, list[str]]:
    from openadapt_flow.readiness.export import export_task

    report: dict[str, list[str]] = {"copied": [], "left": []}
    for src, rel in _readiness_dirs(run):
        r = export_task(src, dest / rel, include_values=False)
        report["copied"] += [f"{rel}/{n}" for n in r["copied"]]
        report["left"] += [f"{rel}/{n}" for n in r["left"]]
    for card in ("1-shadow/card", "1-shadow/adversarial/card", "5-claude/card"):
        sc = run / card / "scorecard.json"
        if sc.is_file():
            r = export_task(run / card / "__none__", dest / card, extra={"scorecard.json": sc})
            report["copied"] += [f"{card}/{n}" for n in r["copied"]]
            report["left"] += [f"{card}/{n}" for n in r["left"]]
    for row in sorted(run.glob("2-gated/runs/*/guard_ledger_row.json")) + sorted(
        run.glob("2b-watch/run/guard_ledger_row.json")
    ):
        doc = json.loads(row.read_text())
        rel = row.relative_to(run)
        if not isinstance(doc, dict) or set(doc) - GUARD_ROW_KEYS:
            report["left"].append(f"{rel}: unexpected keys")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        report["copied"].append(str(rel))
    for name in LEDGER_FILES:
        src = run / "3-ledger" / name
        if src.is_file():
            (dest / "3-ledger").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / "3-ledger" / name)
            report["copied"].append(f"3-ledger/{name}")
    for rel in PNGS:
        src = run / rel
        if src.is_file() and src.stat().st_size < 1_500_000:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
            report["copied"].append(rel)
    for folder in ("scripts", "merge"):
        for src in sorted((run / folder).glob("*")):
            if src.is_file() and src.suffix in DRIVER_SUFFIXES:
                (dest / folder).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest / folder / src.name)
                report["copied"].append(f"{folder}/{src.name}")
    for src in sorted(run.glob("results_*.json")):
        if RESULTS.match(src.name):
            shutil.copyfile(src, dest / src.name)
            report["copied"].append(src.name)
    return report


def phi_values(run: Path) -> set[str]:
    """Every synthetic PHI value the run used."""
    values: set[str] = set()
    app_js = (FLOW / "openadapt_flow/mockmed/static/app.js").read_text()
    for name, dob in re.findall(r"name: '([^']+)', dob: '([^']+)'", app_js):
        values |= {name, dob}
    for pid in re.findall(r"\{ id: '(p\d|g\d)'", app_js):
        values.add(f"MRN {pid.upper()}")
    from benchmark.agent_guard import run as bench
    from tests.e2e import test_readiness_e2e as rt
    from tests.e2e import test_wrap_fallback_e2e as wf

    values |= {bench.NOTE, bench.OTHER_NOTE, wf.NOTE, "Canary test note"}
    for i in range(20):
        values.add(rt._episode(i)["note"])
        values.add(rt._agent_plan(i)["note"])
    for pj in run.rglob("pseudonyms.json"):
        try:
            doc = json.loads(pj.read_text())
        except (OSError, ValueError):
            continue
        for v in _strings(doc):
            if not re.fullmatch(r"\[[A-Z]+_\d+\]", v) and not v.startswith("openadapt."):
                values.add(v)
    return {v for v in values if v and len(v) >= 4 and v not in NOT_PHI}


def _strings(doc):
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, dict):
        for v in doc.values():
            yield from _strings(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _strings(v)


def gate(dest: Path, values: set[str]) -> list[str]:
    hits = []
    needles = sorted(values) + [API_KEY_PREFIX]
    for f in sorted(dest.rglob("*")):
        if not f.is_file() or f.suffix == ".png":
            continue
        if f.name == "copy_evidence.py":
            continue  # names the key prefix itself; holds no run value
        text = f.read_text(errors="replace").casefold()
        for n in needles:
            if n.casefold() in text:
                hits.append(f"{f.relative_to(dest)}: {n if n == API_KEY_PREFIX else '<synthetic value, ' + str(len(n)) + ' chars>'}")
    return hits


if __name__ == "__main__":
    run, dest = Path(sys.argv[1]), Path(sys.argv[2])
    if "--check-only" not in sys.argv:
        report = copy(run, dest)
        (dest / "copy_report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"copied {len(report['copied'])}, left {len(report['left'])}")
    values = phi_values(run)
    hits = gate(dest, values)
    print(f"gate: {len(values)} synthetic values + the API key prefix searched; {len(hits)} hit(s); excluded as not personal data: {sorted(NOT_PHI)}")
    for h in hits:
        print("  HIT", h)
    sys.exit(1 if hits else 0)
