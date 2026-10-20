#!/usr/bin/env python3
"""Regenerate the OpenEMR referral demo fixture (fixtures/openemr-referral/).

Everything here is compiled or recorded by OpenAdapt Flow, on synthetic data:

1. spec.json: the certified OpenEMR referral bundle (clinical-write; content
   digest in folds.json), compiled from one recorded referral on a local
   OpenEMR test copy with made-up patients, wrapped by ``openadapt-flow
   for-each`` in a loop over worklist.csv (three synthetic referrals), then
   emitted by ``openadapt-flow visualize --format json --profile
   public-synthetic``. The public projection carries no recorded text or
   values.
2. folds.json: the field section each loop-body step fills, derived from the
   operator-local spec by the parameter each step types. Parameter names only.
3. trace.json: the four certified runs of that bundle (standard profile, no
   model calls), committed under ``CERTIFIED_PATH``, played in run order as
   loop iterations. Referrals 1-3 are VERIFIED; referral 4 stops
   (HALTED_BEFORE_EFFECT) on OpenEMR's duplicate-patient match. Each run's
   digest is checked against the bundle and each Seal against the report it
   signs. Per step the trace keeps the step id, duration, the rung that found
   the target, and the check verdicts; never a typed value. It ends with the
   stopped referral's Seal.
4. media.json and media/: references and pictures for the site, all from
   synthetic data. ``recorded/step_NNN.webp`` is the bundle's recorded target
   crop (templates/step_NNN.png) for each click step; ``run/rN/step_NNN.webp``
   is the screen after each step of referral 1 and of the stopped referral,
   640 px wide. The trace's ``frame`` fields and media.json hold references
   only; a host resolves them to URLs.

Run from the repository root with a Python that has openadapt-flow installed:

    python packages/program-graph/scripts/build_openemr_fixture.py \\
        --bundle ~/oa/scratch/qualify-referral/qual10/bundle \\
        --runs ~/oa/scratch/qualify-referral/certified-referrals-qual10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
FLOW = REPO / "packages" / "flow"
OUT = PKG / "fixtures" / "openemr-referral"
BUNDLE = FLOW / "registry" / "bundles" / "openemr" / "new-referral"
# The committed copy of the certified runs (reports, Seals, key; no pictures).
# Seal links pin to the commit that added it.
CERTIFIED_PATH = "packages/flow/benchmark/scoreboard/raw/openemr_referral/certified/2026-10-20"
CERTIFIED_COMMIT = "d23eeb6a6"
CERTIFIED_URL = f"https://github.com/OpenAdaptAI/OpenAdapt/blob/{CERTIFIED_COMMIT}/{CERTIFIED_PATH}"
FRAME_WIDTH = 640

# Field sections, in the order the form asks for them. Keys are the bundle's
# parameter names.
SECTIONS = [
    ("name", "Name", ["title", "fname", "lname"]),
    ("birth", "Date of birth and sex", ["DOB", "sex"]),
    ("address", "Address", ["street", "city", "state_label", "postal_code"]),
    ("contact", "Phone, email and country", ["phone_home", "email", "country_code"]),
]
SAVE = ("save", "Create the chart")
SECTION_OF = {p: sid for sid, _, params in SECTIONS for p in params}
EFFECT_STOP = "halts if the system-of-record effect is not confirmed"


def flow(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "openadapt_flow", *args], cwd=FLOW, check=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fold(local: dict) -> dict:
    """Assign each loop-body step to the field section it fills.

    A step belongs to the section of the next step that types a parameter
    (the click that focuses a field, Ctrl+A, the scroll that brings it into
    view). A key press right after a typed value (the Enter that picks a
    dropdown option) stays with that value. Steps after the last typed value
    form the save section.
    """
    nodes = local["nodes"]
    body = [n for n in nodes if n["kind"] == "action"]
    assigned: list[str | None] = [None] * len(body)
    for i, n in enumerate(body):
        if n["action"] == "type" and n["param"]:
            assigned[i] = SECTION_OF[n["param"]]
        elif n["action"] == "key" and i and body[i - 1]["action"] == "type":
            assigned[i] = SECTION_OF[body[i - 1]["param"]]
    nxt = SAVE[0]
    for i in range(len(body) - 1, -1, -1):
        if assigned[i] is None:
            assigned[i] = nxt
        elif body[i]["action"] == "type":
            nxt = assigned[i]
    groups = []
    for sid, title, _ in [*SECTIONS, (*SAVE, [])]:
        members = [n for n, a in zip(body, assigned) if a == sid]
        fields = [n["param"] for n in members if n["param"]]
        checks = sum(len(n["effects"]) for n in members)
        parts = [f"{len(members)} steps"]
        if fields:
            parts.append(f"{len(fields)} {'field' if len(fields) == 1 else 'fields'}")
        if checks:
            parts.append("reads the chart back")
        groups.append(
            {"id": sid, "title": title, "subtitle": " · ".join(parts), "nodes": [n["id"] for n in members]}
        )
    flat = [i for g in groups for i in g["nodes"]]
    assert flat == [n["id"] for n in body], "sections must be contiguous, in order"
    save_ids = set(groups[-1]["nodes"])
    stops = [
        {"node": n["id"], "label": "not confirmed"}
        for n in body
        if n["id"] in save_ids and n["halts"]
    ]
    assert any(EFFECT_STOP in n["halts"] for n in body if n["id"] in save_ids), "expected the save step's read-back stop"
    return {"groups": groups, "stops": stops}


def webp(src: Path, dst: Path, width: int | None = None) -> None:
    from PIL import Image

    image = Image.open(src).convert("RGB")
    if width and image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(dst, "WEBP", quality=62, method=6)


def recorded_media(spec: dict) -> dict:
    loop = next(n["id"] for n in spec["nodes"] if n["kind"] == "loop")
    prefix = f"{loop}::s::"
    out = {}
    for crop in sorted((BUNDLE / "templates").glob("step_???.png")):
        node = prefix + crop.stem
        ref = f"recorded/{crop.stem}.webp"
        webp(crop, OUT / "media" / ref)
        out[node] = {
            "recorded": {"crop": ref, "frame": None},
            "alt": "The step's target as recorded, cropped from the synthetic recording",
        }
    return out


def trace(spec: dict, runs: Path, digest: str) -> dict:
    """The certified runs, played in order as loop iterations.

    ``runs`` holds summary.json and one directory per referral
    (``<n>-<ref>/`` with engine/report.json, engine/seal.json, result.json and
    engine/steps/*.png). Each run is checked against the bundle digest and its
    Seal against the report it signs.
    """
    ids = {n["id"] for n in spec["nodes"]}
    loop = next(n["id"] for n in spec["nodes"] if n["kind"] == "loop")
    prefix = f"{loop}::s::"
    summary = json.loads((runs / "summary.json").read_text())
    assert summary["bundle_content_digest"] == digest, "the runs used a different bundle"
    rows = sorted(summary["runs"], key=lambda r: r["n"])

    events: list[dict] = []
    sources: list[dict] = []
    clock = 0.0
    with_frames = {1}
    for row in rows:
        iteration = row["n"]
        run = runs / f"{row['n']}-{row['ref']}"
        report_path = run / "engine" / "report.json"
        result_path = run / "result.json"
        seal_path = run / "engine" / "seal.json"
        report = json.loads(report_path.read_text())
        result = json.loads(result_path.read_text())
        seal = json.loads(seal_path.read_text())
        assert report["bundle_content_digest"] == digest, f"{row['ref']} ran a different bundle"
        assert report["execution_outcome"] == row["execution_outcome"]
        assert report["model_calls"] == 0
        assert seal["subject_sha256"] == sha256(report_path) == row["report_sha256"], f"{row['ref']}: the Seal signs another report"
        assert seal["outcome"] == report["execution_outcome"]
        by_step = {r["step_id"]: r for r in report["results"]}
        events.append({"node": loop, "status": "ok", "iteration": iteration, "at_ms": round(clock), "duration_ms": 0})
        halted = False
        for timing in result["steps"]:
            step_id, ms = timing["step_id"], timing["elapsed_ms"]
            r = by_step[step_id]
            node = prefix + step_id
            assert node in ids, f"{node} is not in the spec"
            event = {
                "node": node,
                "status": "ok" if r["ok"] else "halted",
                "iteration": iteration,
                "at_ms": round(clock),
                "duration_ms": round(ms),
            }
            if r.get("resolution"):
                event["rung"] = r["resolution"]["rung"]
            identity = (r.get("identity") or {}).get("status")
            if identity:
                event["identity"] = "verified" if identity == "verified" else "not_verified"
            if r.get("postconditions_ok") is not None:
                event["screen_check"] = "passed" if r["postconditions_ok"] else "failed"
            checks = r.get("effect_results") or []
            if checks:
                confirmed = sum("CONFIRMED" in c for c in checks)
                event["effect"] = "confirmed" if confirmed == len(checks) else "refuted"
                event["effect_checks"] = {"confirmed": confirmed, "total": len(checks)}
            frame_src = run / "engine" / "steps" / f"{step_id}_after.png"
            if frame_src.exists():
                event["frame_src"] = frame_src
                event["frame"] = f"run/r{iteration}/{step_id}.webp"
            if not r["ok"]:
                assert report["halt"]["state_id"] == step_id
                assert "No matches were found." in report["halt"]["reason"]
                assert report["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
                event["reason"] = (
                    "The screen check after this step failed: OpenEMR found a matching chart "
                    "instead of showing that no match exists. The run stopped before it "
                    "saved anything, so it created no duplicate."
                )
                halted = True
            events.append(event)
            clock += ms
        sources.append(
            {
                "referral": iteration,
                "execution_outcome": report["execution_outcome"],
                "transaction_outcome": report["transaction_outcome"],
                "new_charts": row["new_charts"],
                "report_sha256": row["report_sha256"],
                "seal": f"{CERTIFIED_URL}/{row['n']}-{row['ref']}/engine/seal.json",
                "seal_outcome": seal["outcome"],
            }
        )
        if halted:
            with_frames.add(iteration)
            break
    assert events[-1]["status"] == "halted", "expected the runs to end with the stopped referral"
    # Frames for the first referral and the one that stopped; references only.
    for event in events:
        src = event.pop("frame_src", None)
        if src is None:
            continue
        if event["iteration"] in with_frames:
            webp(src, OUT / "media" / event["frame"], FRAME_WIDTH)
        else:
            event.pop("frame")
    last = sources[-1]
    verified = [s for s in sources if s["seal_outcome"] == "VERIFIED"]
    return {
        "trace_version": 1,
        "outcome": "halted",
        "label": f"{len(sources)} referrals, recorded",
        "total_ms": round(clock),
        "events": events,
        "receipt": {
            "kind": "seal",
            "status": last["transaction_outcome"],
            "label": "Seal",
            "summary": (
                f"Referral {last['referral']} stopped before it saved anything "
                f"({last['transaction_outcome']}): OpenEMR showed a matching chart, so no duplicate was "
                f"created. Referrals 1 to {len(verified)} each have a VERIFIED Seal: a separate read of "
                "OpenEMR confirmed all 11 saved fields. Each Seal signs its run's report with the demo key."
            ),
            "href": last["seal"],
        },
        "source": {
            "runs_path": CERTIFIED_PATH,
            "commit": CERTIFIED_COMMIT,
            "bundle_content_digest": digest,
            "profile": "standard",
            "model_calls": 0,
            "runs": sources,
            "note": (
                "Each iteration is one certified engine run of the referral body, in run order. "
                "The trace ends with the referral that stopped."
            ),
        },
    }


def main() -> int:
    global BUNDLE, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, help="the certified runs (summary.json and one directory per referral)")
    ap.add_argument("--bundle", type=Path, default=BUNDLE, help="the compiled referral bundle the runs used")
    ap.add_argument("--out", type=Path, default=OUT, help="fixture directory (default: fixtures/openemr-referral)")
    args = ap.parse_args()
    BUNDLE = args.bundle.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if out != OUT and not (out / "worklist.csv").exists():
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(OUT / "worklist.csv", out / "worklist.csv")
    OUT = out
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        loop_dir = Path(tmp) / "loop"
        flow(
            "for-each", str(BUNDLE),
            "--records", str(OUT / "worklist.csv"),
            "--out", str(loop_dir),
            "--loop-var", "referral",
            "--name", "openemr-new-referral-for-each",
        )
        public, local = Path(tmp) / "public.json", Path(tmp) / "local.json"
        flow("visualize", str(loop_dir), "--format", "json", "--profile", "public-synthetic", "-o", str(public))
        flow("visualize", str(loop_dir), "--format", "json", "-o", str(local))
        spec = json.loads(public.read_text())
        folds = fold(json.loads(local.read_text()))
    (OUT / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    folds["source"] = {
        # A bundle outside the repository is named by what it is; its content
        # digest below identifies it exactly.
        "body_bundle": (
            str(BUNDLE.relative_to(REPO))
            if BUNDLE.is_relative_to(REPO)
            else "openemr/new-referral, certified for clinical-write"
        ),
        "body_content_digest": manifest["content_digest"],
        "source_recording_sha256": manifest["provenance"]["source_recording_sha256"],
        "loop": "openadapt-flow for-each --records fixtures/openemr-referral/worklist.csv",
        "spec": "openadapt-flow visualize --format json --profile public-synthetic",
    }
    (OUT / "folds.json").write_text(json.dumps(folds, indent=2) + "\n")
    shutil.rmtree(OUT / "media", ignore_errors=True)
    (OUT / "media.json").write_text(json.dumps(recorded_media(spec), indent=2) + "\n")
    if args.runs:
        (OUT / "trace.json").write_text(json.dumps(trace(spec, args.runs.expanduser(), manifest["content_digest"]), indent=2) + "\n")
    print(f"Wrote {OUT}: spec, folds, media{', trace' if args.runs else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
